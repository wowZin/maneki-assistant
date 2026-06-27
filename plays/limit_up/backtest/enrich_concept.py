"""概念动量 + 龙虎榜机构数据拉取（v2 - 修正API字段名）。

数据源:
- ths_daily: 概念板块每日行情 (pct_change, turnover_rate)
- ths_member: 概念→成分股映射 (ts_code=概念码, con_code=股票码)
- top_inst: 龙虎榜机构席位成交明细
- top_list: 龙虎榜个股汇总
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import requests

OUT_DIR = Path(__file__).resolve().parent / "out"
CACHE_DIR = Path(__file__).resolve().parent / "cache"
CACHE_DIR.mkdir(exist_ok=True)

TOKEN = "ebba208f5d60f9e86a1fcb39cf6dad5dca63c5288e82637ad59c5ac7"


def _tushare(api: str, params: dict, fields: str = "", timeout: int = 20) -> dict:
    """Call Tushare API directly (bypass proxy)."""
    for k in ['http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'all_proxy', 'ALL_PROXY']:
        os.environ.pop(k, None)
    payload = {"api_name": api, "token": TOKEN, "params": params}
    if fields:
        payload["fields"] = fields
    r = requests.post("https://api.tushare.pro", json=payload, timeout=timeout)
    return r.json()


def _get_trade_dates(start: str, end: str) -> list[str]:
    """Get trading dates in range."""
    r = _tushare("trade_cal", {"exchange": "SSE", "start_date": start, "end_date": end})
    items = r.get("data", {}).get("items", [])
    fields = r.get("data", {}).get("fields", [])
    if not items:
        return []
    dates = []
    for item in items:
        row = dict(zip(fields, item))
        if int(row.get("is_open", 0)) == 1:
            dates.append(str(row["cal_date"]))
    return sorted(dates)


# ============================================================
# 1. 概念板块数据
# ============================================================

def pull_concept_daily(trade_dates: list[str]) -> pd.DataFrame:
    """拉取概念板块每日行情（逐日调用）。

    Returns: ts_code(概念码), trade_date, pct_change, turnover_rate
    """
    frames = []
    for d in trade_dates:
        try:
            r = _tushare("ths_daily", {"trade_date": d})
            items = r.get("data", {}).get("items", [])
            fields = r.get("data", {}).get("fields", [])
            if items and fields:
                sub = pd.DataFrame(items, columns=fields)
                frames.append(sub)
            time.sleep(0.2)
        except Exception as e:
            print(f"  ths_daily {d}: {e}")

    if not frames:
        return pd.DataFrame()

    df = pd.concat(frames, ignore_index=True)
    for c in ["pct_change", "turnover_rate", "close", "vol"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df["trade_date"] = df["trade_date"].astype(str)
    df = df.rename(columns={"ts_code": "cpt_code"})  # concept code
    print(f"  ths_daily: {len(df)} rows, {df['trade_date'].nunique()} dates, {df['cpt_code'].nunique()} concepts")
    return df


def pull_concept_members() -> pd.DataFrame:
    """拉取概念→成分股映射。

    用 ts_code 过滤（拉某只股票所属概念），但对回测来说需要全量映射。
    改为遍历主流概念前缀批量拉取。

    Returns: cpt_code(概念码), stock_code(成分股代码), stock_name
    """
    # 尝试拉取全量: ths_member with empty params returns 6000 rows at a time
    # Use looping with prefixes to get all members
    frames = []
    prefixes = [str(i) for i in range(70, 90)]  # 70-89 cover most THS concepts

    for pfx in prefixes:
        try:
            r = _tushare("ths_member", {"ts_code": pfx}, timeout=15)
            items = r.get("data", {}).get("items", [])
            fields = r.get("data", {}).get("fields", [])
            if items and fields:
                sub = pd.DataFrame(items, columns=fields)
                if len(sub) > 0:
                    frames.append(sub)
            time.sleep(0.2)
        except Exception as e:
            print(f"  ths_member prefix={pfx}: {e}")

    if not frames:
        return pd.DataFrame()

    df = pd.concat(frames, ignore_index=True)
    # fields: ts_code(概念码), con_code(股票代码), con_name(股票名称)
    df = df.rename(columns={"ts_code": "cpt_code", "con_code": "stock_code", "con_name": "stock_name"})
    df = df.drop_duplicates(subset=["cpt_code", "stock_code"])
    print(f"  ths_member: {len(df)} rows, {df['stock_code'].nunique()} stocks, {df['cpt_code'].nunique()} concepts")
    return df


# ============================================================
# 2. 龙虎榜数据
# ============================================================

def pull_top_inst(trade_dates: list[str]) -> pd.DataFrame:
    """拉取龙虎榜机构席位明细。"""
    frames = []
    for d in trade_dates:
        try:
            r = _tushare("top_inst", {"trade_date": d})
            items = r.get("data", {}).get("items", [])
            fields = r.get("data", {}).get("fields", [])
            if items and fields:
                sub = pd.DataFrame(items, columns=fields)
                frames.append(sub)
            time.sleep(0.15)
        except Exception as e:
            print(f"  top_inst {d}: {e}")

    if not frames:
        return pd.DataFrame()

    df = pd.concat(frames, ignore_index=True)
    # fields: buy, sell, net_buy, buy_rate, sell_rate
    for c in ["buy", "sell", "net_buy", "buy_rate", "sell_rate"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df["trade_date"] = df["trade_date"].astype(str)
    print(f"  top_inst: {len(df)} rows, {df['trade_date'].nunique()} dates")
    return df


def pull_top_list(trade_dates: list[str]) -> pd.DataFrame:
    """拉取龙虎榜个股汇总。"""
    frames = []
    for d in trade_dates:
        try:
            r = _tushare("top_list", {"trade_date": d})
            items = r.get("data", {}).get("items", [])
            fields = r.get("data", {}).get("fields", [])
            if items and fields:
                sub = pd.DataFrame(items, columns=fields)
                frames.append(sub)
            time.sleep(0.15)
        except Exception as e:
            print(f"  top_list {d}: {e}")

    if not frames:
        return pd.DataFrame()

    df = pd.concat(frames, ignore_index=True)
    for c in ["close", "pct_change", "turnover_rate", "amount", "net_amount", "buy_amount", "sell_amount"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df["trade_date"] = df["trade_date"].astype(str)
    print(f"  top_list: {len(df)} rows, {df['trade_date'].nunique()} dates")
    return df


# ============================================================
# 3. 特征工程：概念动量
# ============================================================

def compute_stock_concept_features(
    concept_daily: pd.DataFrame,
    concept_members: pd.DataFrame,
) -> pd.DataFrame:
    """计算每只股票的每日概念动量特征。

    对每个概念，计算其近N日涨跌幅、上涨比率等。
    对每只股票，聚合其所属概念的动量（取最大/平均/加权）。
    """
    if concept_daily.empty or concept_members.empty:
        return pd.DataFrame()

    cd = concept_daily.copy()
    cd = cd.sort_values(["cpt_code", "trade_date"])

    # 按概念计算滚动特征
    features = []
    for cpt, grp in cd.groupby("cpt_code"):
        grp = grp.sort_values("trade_date")
        # 1日动量
        grp["cpt_ret_1d"] = grp["pct_change"]
        # 3日累计
        grp["cpt_ret_3d"] = grp["pct_change"].rolling(3, min_periods=1).sum()
        # 5日累计
        grp["cpt_ret_5d"] = grp["pct_change"].rolling(5, min_periods=1).sum()
        # 动量持续性（连续上涨天数）
        up = (grp["pct_change"] > 0).astype(int)
        grp["cpt_up_streak"] = up.groupby((up != up.shift()).cumsum()).cumsum()
        # 换手率变化（量能）
        if "turnover_rate" in grp.columns:
            grp["cpt_turn_5d"] = grp["turnover_rate"].rolling(5, min_periods=1).mean()
        features.append(grp)

    cd = pd.concat(features, ignore_index=True)

    # Join with members
    cm = concept_members[["cpt_code", "stock_code"]].drop_duplicates()
    merged = cd.merge(cm, on="cpt_code", how="inner")

    # 股票级聚合：每个股票取其所属概念的最强动量
    # Only aggregate numeric columns
    mom_cols = [c for c in merged.columns if c.startswith("cpt_") and merged[c].dtype in ('float64', 'int64', 'float32', 'int32')]
    if not mom_cols:
        print("  无概念动量数值列")
        return pd.DataFrame()

    # 策略1: 取最强概念动量（max）
    max_mom = merged.groupby(["stock_code", "trade_date"])[mom_cols].max().reset_index()
    max_mom = max_mom.rename(columns={c: c + "_max" for c in mom_cols})

    # 策略2: 取概念平均动量
    avg_mom = merged.groupby(["stock_code", "trade_date"])[mom_cols].mean().reset_index()
    avg_mom = avg_mom.rename(columns={c: c + "_avg" for c in mom_cols})

    # 策略3: 股票所属概念数量（概念广度 = 越多概念共振越好）
    n_cpt = merged.groupby(["stock_code", "trade_date"]).size().reset_index(name="n_concepts")

    # 策略4: 强势概念占比（所属概念中上涨的比例）
    if "cpt_ret_1d" in merged.columns:
        up_ratio = merged.groupby(["stock_code", "trade_date"]).apply(
            lambda g: (g["cpt_ret_1d"] > 0).mean()
        ).reset_index(name="cpt_up_ratio")
    else:
        up_ratio = None

    # Merge all
    result = max_mom.merge(avg_mom, on=["stock_code", "trade_date"], how="outer")
    result = result.merge(n_cpt, on=["stock_code", "trade_date"], how="outer")
    if up_ratio is not None:
        result = result.merge(up_ratio, on=["stock_code", "trade_date"], how="outer")

    print(f"  概念动量特征: {len(result)} rows, {len(result.columns)} cols")
    return result


# ============================================================
# 4. 特征工程：龙虎榜机构
# ============================================================

def compute_inst_features(
    top_inst: pd.DataFrame,
    top_list: pd.DataFrame,
) -> pd.DataFrame:
    """计算龙虎榜机构特征。"""
    features_parts = []

    if not top_inst.empty:
        ti = top_inst.copy()

        # 按股票+日期聚合
        agg = ti.groupby(["ts_code", "trade_date"]).agg(
            inst_net_amount=("net_buy", "sum"),
            inst_buy_amount=("buy", "sum"),
            inst_sell_amount=("sell", "sum"),
            inst_trade_count=("ts_code", "count"),
        ).reset_index()

        # 买方机构数
        buyers = ti[ti["net_buy"] > 0].groupby(["ts_code", "trade_date"]).size().reset_index(name="n_inst_buyers")
        agg = agg.merge(buyers, on=["ts_code", "trade_date"], how="left")
        agg["n_inst_buyers"] = agg["n_inst_buyers"].fillna(0)

        # 买方占比
        total = agg["inst_buy_amount"] + agg["inst_sell_amount"]
        agg["inst_buy_ratio"] = (agg["inst_buy_amount"] / total.replace(0, 1)).clip(0, 1)

        # 是否有机构净买入
        agg["has_inst_net_buy"] = (agg["inst_net_amount"] > 0).astype(int)

        # 机构参与度评分 (0-10)
        agg["inst_score"] = (
            (agg["n_inst_buyers"] >= 3).astype(int) * 3 +
            (agg["inst_buy_ratio"] > 0.6).astype(int) * 3 +
            (agg["inst_net_amount"] > 0).astype(int) * 4
        )

        features_parts.append(agg)

    if not top_list.empty:
        tl = top_list[["ts_code", "trade_date", "net_amount", "l_buy",
                        "l_sell", "turnover_rate", "pct_change"]].copy()
        tl = tl.rename(columns={
            "net_amount": "tl_net_amount",
            "l_buy": "tl_buy_amount",
            "l_sell": "tl_sell_amount",
            "turnover_rate": "tl_turnover",
            "pct_change": "tl_pct_change",
        })
        tl["is_top_list"] = 1

        # 龙虎榜质量评分
        tl["tl_quality"] = (
            (tl["tl_net_amount"] > 0).astype(int) * 5 +
            (tl["tl_buy_amount"] > tl["tl_sell_amount"]).astype(int) * 3 +
            (tl["tl_pct_change"] > 3).astype(int) * 2
        )

        features_parts.append(tl)

    if not features_parts:
        return pd.DataFrame()

    result = features_parts[0]
    for f in features_parts[1:]:
        result = result.merge(f, on=["ts_code", "trade_date"], how="outer")

    for c in result.columns:
        if c not in ["ts_code", "trade_date"]:
            result[c] = result[c].fillna(0)

    print(f"  龙虎榜特征: {len(result)} rows, {len(result.columns)} cols")
    return result


# ============================================================
# 5. 主流程
# ============================================================

def enrich_with_concept_and_inst(
    panel_path: str | None = None,
    output_path: str | None = None,
) -> pd.DataFrame:
    """主流程。"""
    if panel_path is None:
        panel_path = str(OUT_DIR / "panel_enriched_v3.csv")
    panel_path = Path(panel_path)

    print(f"加载面板: {panel_path}")
    df = pd.read_csv(panel_path)
    df["date"] = df["date"].astype(str)
    print(f"  {len(df)} rows, {df['date'].nunique()} 日期")

    dmin, dmax = df["date"].min(), df["date"].max()
    # 往前多取15个交易日用于 rolling 计算
    start_dt = datetime.strptime(dmin, "%Y%m%d") - timedelta(days=25)
    start = start_dt.strftime("%Y%m%d")

    trade_dates = _get_trade_dates(start, dmax)
    print(f"  交易日范围: {trade_dates[0]} ~ {trade_dates[-1]} ({len(trade_dates)} 天)")

    # ===== 拉取概念数据 =====
    print("\n[1/4] 拉取概念板块行情...")
    concept_daily = pull_concept_daily(trade_dates)
    if not concept_daily.empty:
        concept_daily.to_parquet(CACHE_DIR / "concept_daily.parquet")

    print("\n[2/4] 拉取概念成分股映射...")
    cache_member = CACHE_DIR / "concept_members.parquet"
    if cache_member.exists():
        concept_members = pd.read_parquet(cache_member)
        print(f"  从缓存加载: {len(concept_members)} rows")
    else:
        concept_members = pull_concept_members()
        if not concept_members.empty:
            concept_members.to_parquet(cache_member)

    # ===== 拉取龙虎榜数据 =====
    print("\n[3/4] 拉取龙虎榜数据...")
    top_inst = pull_top_inst(trade_dates)
    top_list = pull_top_list(trade_dates)

    # ===== 计算特征并 join =====
    print("\n[4/4] 计算特征并 join...")
    new_feats = []

    # 概念动量
    if not concept_daily.empty and not concept_members.empty:
        cpt_feat = compute_stock_concept_features(concept_daily, concept_members)
        if not cpt_feat.empty:
            new_feats.append(cpt_feat)

    # 龙虎榜
    inst_feat = compute_inst_features(top_inst, top_list)
    if not inst_feat.empty:
        new_feats.append(inst_feat)

    if not new_feats:
        print("无新特征")
        return df

    for feat_df in new_feats:
        feat_df["trade_date"] = feat_df["trade_date"].astype(str)
        # Join on stock code + date
        join_col = "stock_code" if "stock_code" in feat_df.columns else "ts_code"
        df = df.merge(
            feat_df,
            left_on=["code", "date"],
            right_on=[join_col, "trade_date"],
            how="left",
            suffixes=("", "_feat"),
        )
        for c in [join_col, "trade_date"]:
            if c in df.columns:
                df = df.drop(columns=[c], errors="ignore")

    # Fill NaN with 0 for new feature columns
    new_col_patterns = ["cpt_", "inst_", "tl_", "n_concepts", "is_top_list",
                        "has_inst", "n_inst_buyers", "cpt_up_ratio"]
    for c in df.columns:
        if any(c.startswith(p) or c == p for p in new_col_patterns):
            df[c] = df[c].fillna(0)

    # 输出
    if output_path is None:
        output_path = str(OUT_DIR / "panel_enriched_v4.csv")
    df.to_csv(output_path, index=False)

    new_cols = [c for c in df.columns if any(c.startswith(p) or c == p for p in new_col_patterns)]
    print(f"\n输出: {output_path}")
    print(f"  {len(df)} rows, {len(df.columns)} cols (新增 {len(new_cols)} 特征)")
    return df


if __name__ == "__main__":
    enrich_with_concept_and_inst()
