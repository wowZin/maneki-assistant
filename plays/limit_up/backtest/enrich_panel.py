"""面板增强工具 — 基于已有 daily.parquet 计算衍生特征列。

不调用新 API，仅从 daily OHLCV 数据中提取：
- 涨停基因 (limit_up_count_Nd)
- 位置/回调 (pullback, position_in_range)
- 量能 (vol_ratio_proxy, amount_ratio, vol_expanding)
- 形态 (consecutive_up/down, gap_up, reversal)
- 波动 (pct_chg_std, max_pct_chg)
- 量价背离 (vol_price_divergence)

全部计算 point-in-time：仅使用评分日及之前的数据。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

OUT_DIR = Path(__file__).resolve().parent / "out"
CACHE_DIR = Path(__file__).resolve().parent / "cache"
LIMIT_PCT = 9.8  # 涨停阈值


def load_daily_bars() -> pd.DataFrame:
    """加载已缓存的日线数据。"""
    parquets = sorted(CACHE_DIR.glob("daily_*.parquet"))
    if not parquets:
        raise FileNotFoundError("无 daily 缓存，先运行 dataset.build_panel()")
    df = pd.read_parquet(parquets[-1])
    df["trade_date"] = df["trade_date"].astype(str)
    return df.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)


def compute_features_for_code(
    code_bars: pd.DataFrame,
) -> pd.DataFrame:
    """对单只股票的日线序列计算所有衍生特征（point-in-time）。

    code_bars 必须按 trade_date 升序排列。
    返回相同行数的 DataFrame，每行包含该交易日的衍生特征。
    """
    df = code_bars.copy()
    n = len(df)

    closes = df["close"].values
    highs = df["high"].values
    lows = df["low"].values
    opens = df["open"].values
    vols = df["vol"].values
    amounts = df["amount"].values
    pcts = df["pct_chg"].values

    # ── 辅助：rolling window 函数 ──
    def _rolling(arr, window: int, fn, min_periods: int = None):
        """对 arr 做滚动 window 计算，结果长度与 arr 相同，前面不足 window 的为 NaN。"""
        if min_periods is None:
            min_periods = window
        result = np.full(len(arr), np.nan)
        for i in range(len(arr)):
            start = max(0, i - window + 1)
            seg = arr[start : i + 1]
            if len(seg) >= min_periods:
                result[i] = fn(seg)
        return result

    # ── 位置/回调特征 ──
    df["high_10d"] = _rolling(highs, 10, np.max, 1)
    df["high_20d"] = _rolling(highs, 20, np.max, 1)
    df["low_10d"] = _rolling(lows, 10, np.min, 1)
    df["low_20d"] = _rolling(lows, 20, np.min, 1)

    with np.errstate(divide="ignore", invalid="ignore"):
        df["pullback_10d"] = np.where(
            df["high_10d"] > 0,
            (df["high_10d"] - closes) / df["high_10d"],
            np.nan,
        )
        df["pullback_20d"] = np.where(
            df["high_20d"] > 0,
            (df["high_20d"] - closes) / df["high_20d"],
            np.nan,
        )
        range_20d = df["high_20d"] - df["low_20d"]
        df["position_20d"] = np.where(
            range_20d > 0,
            (closes - df["low_20d"]) / range_20d,
            np.nan,
        )

    # ── 涨跌幅统计 ──
    df["max_pct_chg_5d"] = _rolling(pcts, 5, np.max, 1)
    df["max_pct_chg_10d"] = _rolling(pcts, 10, np.max, 1)
    df["min_pct_chg_5d"] = _rolling(pcts, 5, np.min, 1)
    df["avg_pct_chg_5d"] = _rolling(pcts, 5, np.mean, 1)
    df["pct_chg_std_5d"] = _rolling(pcts, 5, np.std, 1)
    df["pct_chg_std_10d"] = _rolling(pcts, 10, np.std, 1)

    # ── 量能特征 ──
    df["avg_vol_5d"] = _rolling(vols, 5, np.mean, 1)
    df["avg_vol_10d"] = _rolling(vols, 10, np.mean, 1)
    df["avg_amount_5d"] = _rolling(amounts, 5, np.mean, 1)

    with np.errstate(divide="ignore", invalid="ignore"):
        df["vol_ratio_proxy"] = np.where(
            df["avg_vol_5d"] > 0,
            vols / df["avg_vol_5d"],
            np.nan,
        )
        df["amount_ratio"] = np.where(
            df["avg_amount_5d"] > 0,
            amounts / df["avg_amount_5d"],
            np.nan,
        )

    # 量能趋势：成交额连续3日递增
    amount_3d_inc = np.zeros(n, dtype=int)
    for i in range(2, n):
        if amounts[i] > amounts[i - 1] > amounts[i - 2]:
            amount_3d_inc[i] = 1
    df["amount_3d_increasing"] = amount_3d_inc

    # ── 连阳/连阴 ──
    consecutive_up = np.zeros(n, dtype=int)
    consecutive_down = np.zeros(n, dtype=int)
    for i in range(n):
        cnt_up = 0
        cnt_down = 0
        for j in range(i, -1, -1):
            if pcts[j] > 0:
                cnt_up += 1
            else:
                break
        for j in range(i, -1, -1):
            if pcts[j] < 0:
                cnt_down += 1
            else:
                break
        consecutive_up[i] = cnt_up
        consecutive_down[i] = cnt_down
    df["consecutive_up"] = consecutive_up
    df["consecutive_down"] = consecutive_down

    # ── 缺口 ──
    df["gap_up"] = np.where(
        (opens > 0) & (df["pre_close"] > 0),
        (opens / df["pre_close"] - 1.0) * 100,
        np.nan,
    )
    df["gap_up_bool"] = (df["gap_up"] > 2.0).astype(int)

    # ── 弱转强（昨跌今涨+放量） ──
    reversal = np.zeros(n, dtype=int)
    for i in range(1, n):
        if pcts[i - 1] < -1.0 and pcts[i] > 2.0 and vols[i] > vols[i - 1] * 1.2:
            reversal[i] = 1
    df["reversal_signal"] = reversal

    # ── 涨停基因：近N日涨停次数 ──
    limit_up = (pcts >= LIMIT_PCT).astype(int)
    df["limit_up_count_5d"] = _rolling(limit_up, 5, np.sum, 1)
    df["limit_up_count_10d"] = _rolling(limit_up, 10, np.sum, 1)
    df["limit_up_count_20d"] = _rolling(limit_up, 20, np.sum, 1)
    df["limit_up_count_60d"] = _rolling(limit_up, 60, np.sum, 1)

    # ── 量价背离 ──
    # 价平量增：近3日价格变化<2%但量比>1.5 → 可能吸筹
    vol_price_div = np.zeros(n, dtype=float)
    for i in range(2, n):
        price_chg_3d = abs(closes[i] / closes[i - 2] - 1.0)
        vol_ratio = vols[i] / (np.mean(vols[max(0, i - 5) : i]) + 1e-9)
        if price_chg_3d < 0.02 and vol_ratio > 1.5:
            vol_price_div[i] = 1.0  # 吸筹信号
        elif price_chg_3d > 0.05 and vol_ratio < 0.7:
            vol_price_div[i] = -1.0  # 缩量涨 → 动能减弱
    df["vol_price_divergence"] = vol_price_div

    # ── 振幅 ──
    with np.errstate(divide="ignore", invalid="ignore"):
        df["amplitude"] = np.where(
            df["pre_close"] > 0,
            (highs - lows) / df["pre_close"] * 100,
            np.nan,
        )

    # ── 上影线比例 ──
    with np.errstate(divide="ignore", invalid="ignore"):
        body_high = np.maximum(opens, closes)
        upper_shadow = highs - body_high
        df["upper_shadow_pct"] = np.where(
            (highs - lows) > 0,
            upper_shadow / (highs - lows) * 100,
            np.nan,
        )

    return df


def _build_prev_date_map(bars: pd.DataFrame, offset: int = 1) -> dict[str, str | None]:
    """构建每个交易日向前 offset 个交易日的映射。"""
    all_dates = sorted(bars["trade_date"].dropna().unique().tolist())
    prev_map: dict[str, str | None] = {d: None for d in all_dates}
    for i, d in enumerate(all_dates):
        if i - offset >= 0:
            prev_map[d] = all_dates[i - offset]
    return prev_map


def compute_features_pit(
    panel: pd.DataFrame,
    bars: pd.DataFrame,
    as_of_offset: int = 1,
    use_today_open: bool = True,
) -> pd.DataFrame:
    """计算早盘 PIT 特征：所有 trailing/位置/量能/涨停基因截止到 T-offset；gap 可选用当日开盘。"""
    # 1. 计算全量特征序列
    feature_dfs = []
    for code, g in bars.groupby("ts_code"):
        feat = compute_features_for_code(g)
        feature_dfs.append(feat)
    bars_with_feat = pd.concat(feature_dfs, ignore_index=True)

    # 2. 交易日历映射
    prev_date_map = _build_prev_date_map(bars, as_of_offset)

    # 3. 面板行映射到 PIT 日期
    merged = panel.copy()
    merged["_pit_date"] = merged["date"].map(prev_date_map)

    # 4. 从 PIT 日期取基础特征（不含 gap_up，会单独重算）
    base_feature_cols = [
        "ts_code", "trade_date",
        # 位置/回调
        "high_10d", "high_20d", "low_10d", "low_20d",
        "pullback_10d", "pullback_20d", "position_20d",
        # 涨跌幅统计
        "max_pct_chg_5d", "max_pct_chg_10d", "min_pct_chg_5d",
        "avg_pct_chg_5d", "pct_chg_std_5d", "pct_chg_std_10d",
        # 量能
        "avg_vol_5d", "avg_vol_10d", "avg_amount_5d",
        "vol_ratio_proxy", "amount_ratio", "amount_3d_increasing",
        # 形态
        "consecutive_up", "consecutive_down",
        "reversal_signal",
        # 涨停基因
        "limit_up_count_5d", "limit_up_count_10d",
        "limit_up_count_20d", "limit_up_count_60d",
        # 量价背离/振幅/影线
        "vol_price_divergence",
        "amplitude", "upper_shadow_pct",
    ]
    pit_features = bars_with_feat[base_feature_cols].rename(
        columns={"ts_code": "code", "trade_date": "_pit_date"}
    )

    # 5. 左连接 PIT 特征
    merged = merged.merge(pit_features, on=["code", "_pit_date"], how="left")

    # 5.1 计算 PIT trailing 收益（close[T-offset] / close[T-offset-5/10] - 1）
    trailing_frames = []
    for code, g in bars.groupby("ts_code"):
        g = g.sort_values("trade_date").reset_index(drop=True)
        with np.errstate(divide="ignore", invalid="ignore"):
            g["trailing_5_pit"] = np.where(
                g["close"].shift(5) > 0,
                g["close"] / g["close"].shift(5) - 1.0,
                np.nan,
            )
            g["trailing_10_pit"] = np.where(
                g["close"].shift(10) > 0,
                g["close"] / g["close"].shift(10) - 1.0,
                np.nan,
            )
        trailing_frames.append(g[["ts_code", "trade_date", "trailing_5_pit", "trailing_10_pit"]])
    trailing_df = pd.concat(trailing_frames, ignore_index=True).rename(
        columns={"ts_code": "code", "trade_date": "_pit_date"}
    )
    merged = merged.merge(trailing_df, on=["code", "_pit_date"], how="left")

    # 6. 单独计算当日开盘缺口（若扫描时间 >= 09:30）
    if use_today_open and "scan_time" in merged.columns:
        merged["_scan_time_int"] = (
            pd.to_numeric(merged["scan_time"], errors="coerce").fillna(0).astype(int)
        )
        today_bars = bars[["ts_code", "trade_date", "open", "pre_close"]].rename(
            columns={"ts_code": "code", "trade_date": "date"}
        )
        merged = merged.merge(today_bars, on=["code", "date"], how="left")

        with np.errstate(divide="ignore", invalid="ignore"):
            can_use_open = merged["_scan_time_int"] >= 930
            merged["gap_up_pit"] = np.where(
                can_use_open & (merged["pre_close"] > 0),
                (merged["open"] / merged["pre_close"] - 1.0) * 100,
                np.nan,
            )
            merged["gap_up_bool_pit"] = (merged["gap_up_pit"] > 2.0).astype(int)
        merged = merged.drop(
            columns=["open", "pre_close", "_scan_time_int"], errors="ignore"
        )
    else:
        merged["gap_up_pit"] = np.nan
        merged["gap_up_bool_pit"] = 0

    # 7. 删除原始 labels.py 中的 trailing_10/trailing_5（基于 T 收盘，盘中不可用）
    merged = merged.drop(columns=["trailing_10", "trailing_5"], errors="ignore")
    merged = merged.drop(columns=["_pit_date"], errors="ignore")
    return merged

def enrich_panel(
    panel_path: str | None = None,
    output_path: str | None = None,
    pit_mode: bool = False,
    as_of_offset: int = 1,
    use_today_open: bool = True,
) -> pd.DataFrame:
    """加载 panel，join 衍生特征，输出 enriched panel。

    Args:
        panel_path: 原 panel 路径，默认 out/panel.csv
        output_path: 输出路径，默认 out/panel_enriched.csv（pit_mode 下默认 out/panel_enriched_pit.csv）
        pit_mode: 是否启用早盘 point-in-time 特征截断
        as_of_offset: PIT 向前偏移交易日数，默认 1（即 T-1）
        use_today_open: PIT 模式下是否允许用当日开盘计算 gap_up

    Returns:
        增强后的 DataFrame
    """
    panel_path = Path(panel_path) if panel_path else OUT_DIR / "panel.csv"
    if output_path:
        output_path = Path(output_path)
    else:
        output_path = OUT_DIR / ("panel_enriched_pit.csv" if pit_mode else "panel_enriched.csv")

    print(f"加载 panel: {panel_path}")
    panel = pd.read_csv(panel_path)
    panel["date"] = panel["date"].astype(str)
    panel["code"] = panel["code"].astype(str)
    print(f"  {len(panel)} rows, {panel.code.nunique()} codes, {panel.date.nunique()} dates")

    print("加载 daily bars...")
    bars = load_daily_bars()
    print(f"  {len(bars)} rows")

    if pit_mode:
        print(f"计算 PIT 衍生特征 (as_of_offset={as_of_offset}, use_today_open={use_today_open})...")
        enriched = compute_features_pit(
            panel, bars, as_of_offset=as_of_offset, use_today_open=use_today_open
        )
        feature_cols = [c for c in enriched.columns if c not in panel.columns]
    else:
        print("计算衍生特征 (按 code 分组)...")
        feature_dfs = []
        for code, g in bars.groupby("ts_code"):
            feat = compute_features_for_code(g)
            feature_dfs.append(feat)
        bars_with_feat = pd.concat(feature_dfs, ignore_index=True)
        print(f"  完成，{len(bars_with_feat)} rows, {len(bars_with_feat.columns)} columns")

        # 选择要 join 的特征列
        feature_cols = [
            "ts_code", "trade_date",
            # 位置/回调
            "high_10d", "high_20d", "low_10d", "low_20d",
            "pullback_10d", "pullback_20d", "position_20d",
            # 涨跌幅统计
            "max_pct_chg_5d", "max_pct_chg_10d", "min_pct_chg_5d",
            "avg_pct_chg_5d", "pct_chg_std_5d", "pct_chg_std_10d",
            # 量能
            "avg_vol_5d", "avg_vol_10d", "avg_amount_5d",
            "vol_ratio_proxy", "amount_ratio", "amount_3d_increasing",
            # 形态
            "consecutive_up", "consecutive_down",
            "gap_up", "gap_up_bool", "reversal_signal",
            # 涨停基因
            "limit_up_count_5d", "limit_up_count_10d",
            "limit_up_count_20d", "limit_up_count_60d",
            # 量价背离
            "vol_price_divergence",
            # 振幅/影线
            "amplitude", "upper_shadow_pct",
        ]

        feat_subset = bars_with_feat[feature_cols].copy()
        feat_subset = feat_subset.rename(columns={"ts_code": "code", "trade_date": "date"})

        # Join
        enriched = panel.merge(feat_subset, on=["code", "date"], how="left")

    enriched.to_csv(output_path, index=False)

    # 统计缺失率（跳过原始标识列）
    miss_cols = [c for c in feature_cols if c not in ("ts_code", "trade_date")]
    miss = enriched[miss_cols].isnull().mean().sort_values(ascending=False)
    print(f"\n特征缺失率 (top 10):")
    print(miss.head(10))
    print(f"\n输出: {output_path}")
    print(f"  {len(enriched)} rows, {len(enriched.columns)} columns")
    print(f"  新增特征: {len(miss_cols)}")

    return enriched


if __name__ == "__main__":
    import argparse
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

    parser = argparse.ArgumentParser(description="面板特征增强")
    parser.add_argument("--pit", action="store_true", help="启用早盘 point-in-time 特征截断")
    parser.add_argument("--offset", type=int, default=1, help="PIT 向前偏移交易日数")
    parser.add_argument("--no-today-open", action="store_true", help="PIT 模式下不使用当日开盘")
    args = parser.parse_args()

    enrich_panel(
        pit_mode=args.pit,
        as_of_offset=args.offset,
        use_today_open=not args.no_today_open,
    )
