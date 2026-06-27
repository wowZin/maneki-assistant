"""基本面+资金流数据拉取 — 为回测面板增加 PE/PB/市值/主力资金等特征。

数据源：
- daily_basic: PE, PB, total_mv, circ_mv, turnover_rate, turnover_rate_f, volume_ratio
- moneyflow: 主力净流入/超大单/大单/中单/小单

策略：按 trade_date 批量拉取，10 个交易日 = 20 次 API 调用。
"""

from __future__ import annotations

import time
from pathlib import Path

import pandas as pd

OUT_DIR = Path(__file__).resolve().parent / "out"
CACHE_DIR = Path(__file__).resolve().parent / "cache"
CACHE_DIR.mkdir(exist_ok=True)


def pull_daily_basic_by_date(trade_date: str) -> pd.DataFrame:
    """按交易日拉取 daily_basic（PE/PB/市值/换手率/量比）。"""
    from scripts.tu_share import call_tushare

    fields = "ts_code,trade_date,pe,pb,total_mv,circ_mv,turnover_rate,turnover_rate_f,volume_ratio,free_share"
    result = call_tushare("daily_basic", {"trade_date": trade_date}, fields)
    items = result.get("data", {}).get("items", [])
    fields_list = result.get("data", {}).get("fields", [])
    if not items or not fields_list:
        return pd.DataFrame()
    df = pd.DataFrame(items, columns=fields_list)
    for c in ["pe", "pb", "total_mv", "circ_mv", "turnover_rate", "turnover_rate_f", "volume_ratio", "free_share"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df["trade_date"] = df["trade_date"].astype(str)
    return df


def pull_moneyflow_by_date(trade_date: str) -> pd.DataFrame:
    """按交易日拉取 moneyflow（主力资金流向）。"""
    from scripts.tu_share import call_tushare

    fields = "ts_code,trade_date,net_mf_amount,net_mf_vol,buy_lg_amount,buy_lg_vol,sell_lg_amount,sell_lg_vol,buy_elg_amount,buy_elg_vol,sell_elg_amount,sell_elg_vol,buy_sm_amount,sell_sm_amount"
    result = call_tushare("moneyflow", {"trade_date": trade_date}, fields)
    items = result.get("data", {}).get("items", [])
    fields_list = result.get("data", {}).get("fields", [])
    if not items or not fields_list:
        return pd.DataFrame()
    df = pd.DataFrame(items, columns=fields_list)
    for c in df.columns:
        if c not in ("ts_code", "trade_date"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df["trade_date"] = df["trade_date"].astype(str)
    return df


def enrich_with_fundamental(panel_path: str | None = None, output_path: str | None = None) -> pd.DataFrame:
    """拉取 daily_basic + moneyflow，join 到 panel，输出 enriched panel。

    Args:
        panel_path: 输入 panel 路径，默认使用已增强的 panel_enriched.csv
        output_path: 输出路径
    """
    panel_path = Path(panel_path) if panel_path else OUT_DIR / "panel_enriched.csv"
    output_path = Path(output_path) if output_path else OUT_DIR / "panel_enriched_v3.csv"

    print(f"加载 panel: {panel_path}")
    panel = pd.read_csv(panel_path)
    panel["date"] = panel["date"].astype(str)
    panel["code"] = panel["code"].astype(str)
    dates = sorted(panel["date"].unique())
    print(f"  {len(panel)} rows, {len(dates)} dates: {dates[0]} - {dates[-1]}")

    # ── 1. Pull daily_basic ──
    print("\n[1/2] Pulling daily_basic...")
    basic_frames = []
    for d in dates:
        print(f"  {d}...", end=" ", flush=True)
        try:
            df = pull_daily_basic_by_date(d)
            basic_frames.append(df)
            print(f"{len(df)} rows")
        except Exception as e:
            print(f"ERROR: {e}")
        time.sleep(0.3)  # rate limit
    basic_all = pd.concat(basic_frames, ignore_index=True) if basic_frames else pd.DataFrame()
    if not basic_all.empty:
        basic_all = basic_all.rename(columns={"ts_code": "code", "trade_date": "date"})

    # ── 2. Pull moneyflow ──
    print("\n[2/2] Pulling moneyflow...")
    mf_frames = []
    for d in dates:
        print(f"  {d}...", end=" ", flush=True)
        try:
            df = pull_moneyflow_by_date(d)
            mf_frames.append(df)
            print(f"{len(df)} rows")
        except Exception as e:
            print(f"ERROR: {e}")
        time.sleep(0.3)
    mf_all = pd.concat(mf_frames, ignore_index=True) if mf_frames else pd.DataFrame()
    if not mf_all.empty:
        mf_all = mf_all.rename(columns={"ts_code": "code", "trade_date": "date"})

    # ── 3. Join ──
    print("\n[3/3] Joining...")
    enriched = panel.copy()

    if not basic_all.empty:
        basic_cols = ["code", "date"] + [c for c in basic_all.columns if c not in ("code", "date")]
        enriched = enriched.merge(basic_all[basic_cols], on=["code", "date"], how="left")
        print(f"  daily_basic: {len(basic_cols)-2} new cols")

    if not mf_all.empty:
        mf_cols = ["code", "date"] + [c for c in mf_all.columns if c not in ("code", "date")]
        enriched = enriched.merge(mf_all[mf_cols], on=["code", "date"], how="left")
        print(f"  moneyflow: {len(mf_cols)-2} new cols")

    # 计算衍生基本面因子
    enriched = _compute_derived_fundamental(enriched)

    enriched.to_csv(output_path, index=False)
    print(f"\n输出: {output_path}")
    print(f"  {len(enriched)} rows, {len(enriched.columns)} columns")

    # 缺失率统计
    new_cols = [c for c in enriched.columns if c not in panel.columns]
    if new_cols:
        miss = enriched[new_cols].isnull().mean().sort_values(ascending=False)
        print(f"  新列缺失率: {miss.head(10).to_dict()}")

    return enriched


def _compute_derived_fundamental(df: pd.DataFrame) -> pd.DataFrame:
    """从 daily_basic 数据计算衍生基本面因子。"""
    # PE 倒数 = 盈利收益率（越高越好，但需处理负PE）
    if "pe" in df.columns:
        df["earnings_yield"] = df["pe"].apply(
            lambda x: 1.0 / x if x and x > 0 else 0.0
        )

    # PB 倒数
    if "pb" in df.columns:
        df["book_yield"] = df["pb"].apply(
            lambda x: 1.0 / x if x and x > 0 else 0.0
        )

    # 流通市值对数（小市值效应）
    if "circ_mv" in df.columns:
        df["log_circ_mv"] = df["circ_mv"].apply(
            lambda x: __import__("math").log(x) if x and x > 0 else 0.0
        )

    # 换手率是否有效（>2%且<30%）
    if "turnover_rate" in df.columns:
        df["turnover_healthy"] = df["turnover_rate"].apply(
            lambda x: 1.0 if x and 2 <= x <= 30 else 0.0
        )

    # 主力资金流向衍生特征
    if "net_mf_amount" in df.columns and "circ_mv" in df.columns:
        # 主力净流入/流通市值（标准化）
        df["net_mf_ratio"] = df.apply(
            lambda r: (r.get("net_mf_amount", 0) or 0) / (r.get("circ_mv", 1) or 1) * 10000
            if r.get("circ_mv") and r["circ_mv"] > 0 else 0.0,
            axis=1,
        )

    if "buy_lg_amount" in df.columns and "sell_lg_amount" in df.columns:
        # 大单买卖比
        df["lg_buy_sell_ratio"] = df.apply(
            lambda r: (r.get("buy_lg_amount", 0) or 0) / (abs(r.get("sell_lg_amount", 0)) + 1)
            if r.get("sell_lg_amount") and abs(r["sell_lg_amount"]) > 0 else 0.0,
            axis=1,
        )

    if "buy_elg_amount" in df.columns and "sell_elg_amount" in df.columns:
        # 超大单净买入
        df["elg_net"] = df.apply(
            lambda r: (r.get("buy_elg_amount", 0) or 0) - abs(r.get("sell_elg_amount", 0) or 0),
            axis=1,
        )

    return df


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))
    enrich_with_fundamental()
