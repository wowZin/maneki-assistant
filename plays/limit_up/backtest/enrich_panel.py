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


def enrich_panel(panel_path: str | None = None, output_path: str | None = None) -> pd.DataFrame:
    """加载 panel，join 衍生特征，输出 enriched panel。

    Args:
        panel_path: 原 panel 路径，默认 out/panel.csv
        output_path: 输出路径，默认 out/panel_enriched.csv

    Returns:
        增强后的 DataFrame
    """
    panel_path = Path(panel_path) if panel_path else OUT_DIR / "panel.csv"
    output_path = Path(output_path) if output_path else OUT_DIR / "panel_enriched.csv"

    print(f"加载 panel: {panel_path}")
    panel = pd.read_csv(panel_path)
    panel["date"] = panel["date"].astype(str)
    panel["code"] = panel["code"].astype(str)
    print(f"  {len(panel)} rows, {panel.code.nunique()} codes, {panel.date.nunique()} dates")

    print("加载 daily bars...")
    bars = load_daily_bars()
    print(f"  {len(bars)} rows")

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

    # 统计缺失率
    miss = enriched[feature_cols[2:]].isnull().mean().sort_values(ascending=False)
    print(f"\n特征缺失率 (top 10):")
    print(miss.head(10))
    print(f"\n输出: {output_path}")
    print(f"  {len(enriched)} rows, {len(enriched.columns)} columns")
    print(f"  新增特征: {len(feature_cols) - 2}")

    return enriched


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))
    enrich_panel()
