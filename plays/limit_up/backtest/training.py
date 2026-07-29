#!/usr/bin/env python3
"""训练集：正负样本持久化 CSV，供因子挖掘 / 权重优化 / 推送策略优化使用。

存储：wiki/raw/limit-up/training/training_set.csv（单一持久 CSV，git 跟踪）

样本定义：
- 正样本（label=1）: 当日全市场主板 + 非 ST 涨停股（Tushare limit_list_d）
- 负样本（label=0）: wiki analysis 所有轮次并集 - 剔除当日涨停股

特征：只从面板重算（wiki/raw/limit-up/panel/daily + daily_basic），
不依赖 pipeline 历史 5 维分（保证正负样本口径完全一致）。

标签：fwd_ret_3, hit_limit_3, fwd_max_3（未来 3 日）

用法：
    # 一次性 build 历史全量
    python plays/limit_up/backtest/training.py build --start 20260519 --end 20260702

    # 检查覆盖
    python plays/limit_up/backtest/training.py check --start 20260601 --end 20260702

    # pick（缺日期自动补）
    python plays/limit_up/backtest/training.py pick --dates 20260615,20260618

    # Python 调用
    from plays.limit_up.backtest.training import ensure_and_pick
    df = ensure_and_pick(dates=['20260615','20260618'])
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from plays.limit_up.backtest.dataset import (
    ANALYSIS_DIRS,
    pull_auction_bars,
    pull_daily_bars,
    pull_daily_basic_bars,
    pull_intraday_metrics,
    pull_moneyflow_bars,
    pull_top_list_bars,
    pull_top_inst_bars,
    _trade_dates,
)
from plays.limit_up.pit_features import build_pit_features
from plays.limit_up.strategies import factor_ctx

TRAINING_DIR = PROJECT_DIR / "wiki" / "raw" / "limit-up" / "training"
TRAINING_DIR.mkdir(parents=True, exist_ok=True)
TRAINING_CSV = TRAINING_DIR / "training_set.csv"


# ══════════════════════════════════════════════════════
# 样本源
# ══════════════════════════════════════════════════════

def get_scanned_codes(trade_date: str) -> set[str]:
    """当日 wiki analysis 所有轮次里扫描过的股票（去重后的并集）。"""
    codes: set[str] = set()
    for base in ANALYSIS_DIRS:
        if not base.exists():
            continue
        for f in sorted(glob.glob(str(base / f"{trade_date}*.json"))):
            try:
                recs = json.load(open(f))
                for r in recs:
                    if isinstance(r, dict) and r.get("code"):
                        codes.add(r["code"])
            except json.JSONDecodeError as e:
                print(f"  [warn] 解析 analysis 文件失败 {f}: {e}")
            except OSError as e:
                print(f"  [warn] 读取 analysis 文件失败 {f}: {e}")
            except Exception as e:
                print(f"  [warn] 处理 analysis 文件异常 {f}: {e}")
    return codes


def get_limit_up_codes(trade_date: str) -> set[str]:
    """当日全市场涨停股（主板 + 非 ST），从 Tushare limit_list_d 拉取。"""
    from scripts.tu_share import call_tushare
    from plays.limit_up.utils import is_tradable_stock

    resp = call_tushare(
        "limit_list_d",
        {"trade_date": trade_date, "limit_type": "U"},
        "ts_code,trade_date,name",
    )
    items = resp.get("data", {}).get("items", [])
    fields = resp.get("data", {}).get("fields", [])
    codes: set[str] = set()
    for row in items:
        d = dict(zip(fields, row))
        code = d.get("ts_code", "")
        name = d.get("name", "") or ""
        if is_tradable_stock(code, name):
            codes.add(code)
    return codes


# ══════════════════════════════════════════════════════
# 特征 + 标签计算（从面板）
# ══════════════════════════════════════════════════════

FEATURE_COLS = [
    "position_20d", "trailing_10", "trailing_5",
    "pct_chg_std_10d", "pct_chg_std_5d", "max_pct_chg_5d",
    "limit_up_count_20d", "limit_up_count_60d", "max_step", "was_limit",
    "avg_amount_5d", "pct_chg_score_day",
    "turnover_rate", "volume_ratio", "prev_turnover", "prev_vol_ratio", "vol_accel",
    "circ_mv", "cmv_yi", "pe", "pb",
    "pullback_10d", "pullback_20d",
    "prev_pct", "pct_5d", "positive_5d",
    "close_pos", "body_ratio", "upper_ratio", "lower_ratio", "amplitude",
    "net_mf_amount", "net_mf_ratio", "buy_elg_ratio", "buy_lg_ratio",
    "mf_net", "mf_accel", "mf_pct",
    "sector_heat", "sector_rank", "n_concepts",
    "sector_ret3", "sector_up_ratio", "sector_streak",
    "auc_amount", "auc_vol", "auc_amt_ratio", "auc_vol_ratio",
    # 日内分时特征（T-1）
    "id_vwap_dev", "id_range", "id_morning_vol_ratio", "id_afternoon_strength",
    "id_tail_vol_ratio", "id_amount_ratio",
    # 龙虎榜 PIT 特征
    "dt_is_listed", "dt_net_amount", "dt_net_rate", "dt_l_buy_ratio",
    "dt_n_exalter", "dt_inst_net_buy", "dt_hot_net_buy", "dt_inst_sell_ratio",
    # 五维度分：从 analysis 记录提取，缺失时填 0
    "fundamental", "technical", "fundflow", "sentiment", "shortterm",
]
LABEL_COLS = ["fwd_ret_3", "hit_limit_3", "fwd_max_3"]


def get_dim_scores_by_code(trade_date: str) -> dict[str, dict[str, float]]:
    """从当日 analysis 记录中提取每只股票最新的五维度分。

    返回: {code: {"fundamental": x, "technical": x, ...}}
    同一股票多轮扫描时，保留 total 最高的一轮。
    """
    dim_cols = ["fundamental", "technical", "fundflow", "sentiment", "shortterm"]
    out: dict[str, dict[str, float]] = {}
    for base in ANALYSIS_DIRS:
        if not base.exists():
            continue
        for f in sorted(glob.glob(str(base / f"{trade_date}*.json"))):
            try:
                recs = json.load(open(f))
                for r in recs:
                    if not isinstance(r, dict) or not r.get("code"):
                        continue
                    code = r["code"]
                    sc = r.get("scores", {}) or {}
                    dims = {d: float(sc.get(d, 0.0) or 0.0) for d in dim_cols}
                    total = float(r.get("total", 0.0) or 0.0)
                    existing = out.get(code)
                    if existing is None or total > existing.get("_total", 0.0):
                        dims["_total"] = total
                        out[code] = dims
            except Exception:
                continue
    # 删除内部用的 _total
    for code in out:
        out[code].pop("_total", None)
    return out


def _build_limit_gene_index(limit_by_date: dict[str, set[str]]) -> dict[str, list[str]]:
    """{ts_code: [涨停日期, ...] 升序}"""
    by_code: dict[str, list[str]] = defaultdict(list)
    for date, codes in limit_by_date.items():
        for c in codes:
            by_code[c].append(date)
    for c in by_code:
        by_code[c].sort()
    return by_code


def _extract_row(
    code: str,
    trade_date: str,
    daily_by_code: dict[str, list[dict]],
    dbasic_by_code_date: dict[str, dict[str, dict]],
    mf_by_code_date: dict[str, dict[str, dict]],
    auction_by_code_date: dict[str, dict[str, dict]],
    intraday_by_code_date: dict[str, dict[str, dict]],
    top_list_by_code_date: dict[str, dict[str, dict]],
    top_inst_by_code_date: dict[str, dict[str, list[dict]]],
    limit_by_code: dict[str, list[str]],
    dim_scores: dict[str, dict[str, float]],
    dates_all: list[str],
) -> dict | None:
    """在 trade_date 提取一只股票的面板行 + 未来标签（PIT）。"""
    daily_rows = daily_by_code.get(code, [])
    if not daily_rows:
        return None

    # 该股票的日线时序按 trade_date 升序
    rows_sorted = sorted(daily_rows, key=lambda r: r.get("trade_date", ""))
    dates_asc = [r["trade_date"] for r in rows_sorted]

    if trade_date not in dates_asc:
        return None
    i = dates_asc.index(trade_date)
    c0 = rows_sorted[i].get("close")
    if c0 is None:
        return None

    # PIT 基准日：与 build_pit_features(pit_mode=True) 保持一致，即 T-1
    pit_i = i - 1 if i >= 1 else i
    pit_date = dates_asc[pit_i] if 0 <= pit_i < len(dates_asc) else trade_date
    code_short = code.split(".")[0]
    concept_momentum = factor_ctx.get_concept_momentum(code_short, trade_date=pit_date)

    feat = build_pit_features(
        code=code,
        score_date=trade_date,
        daily_rows=rows_sorted,
        basic_by_date=dbasic_by_code_date.get(code, {}),
        moneyflow_by_date=mf_by_code_date.get(code, {}),
        auction_by_date=auction_by_code_date.get(code, {}),
        intraday_by_date=intraday_by_code_date.get(code, {}),
        concept_momentum=concept_momentum,
        top_list_by_date=top_list_by_code_date.get(code, {}),
        top_inst_by_date=top_inst_by_code_date.get(code, {}),
        pit_mode=True,
    )

    # 补充五维度分（从 analysis 记录，缺失时保持 0）
    dim_cols = ["fundamental", "technical", "fundflow", "sentiment", "shortterm"]
    dims = dim_scores.get(code, {})
    for d in dim_cols:
        feat[d] = float(dims.get(d, 0.0) or 0.0)

    # 未来 3 日标签
    fwd_ret_3 = None
    fwd_max_3 = None
    hit_limit_3 = None
    closes = [r.get("close") for r in rows_sorted]
    pcts = [r.get("pct_chg") for r in rows_sorted]
    if i + 3 < len(rows_sorted):
        base = closes[i]
        c_end = closes[i + 3]
        if base and c_end:
            fwd_ret_3 = float(c_end / base - 1.0)
        fut_closes = [c for c in closes[i+1:i+4] if c is not None]
        if base and fut_closes:
            fwd_max_3 = float(max(fut_closes) / base - 1.0)
        fut_pcts = [p for p in pcts[i+1:i+4] if p is not None]
        if fut_pcts:
            hit_limit_3 = int(any(p >= 9.8 for p in fut_pcts))

    out = {
        "code": code,
        "trade_date": trade_date,
        **feat,
        "fwd_ret_3": fwd_ret_3,
        "hit_limit_3": hit_limit_3,
        "fwd_max_3": fwd_max_3,
    }
    return out


# ══════════════════════════════════════════════════════
# 单日 build
# ══════════════════════════════════════════════════════

def build_one_day(trade_date: str, lookback: int = 90) -> pd.DataFrame:
    """为单个交易日构建训练样本（正样本 + 负样本 + 特征 + 标签）。"""
    print(f"[build] {trade_date}")

    # 1. 候选池：当日被扫描到且当日未涨停的股票
    #    训练目标与生产推送目标一致：预测“今天还没涨停的票，未来 3 日会不会涨停”。
    scanned = get_scanned_codes(trade_date)
    limit_up = get_limit_up_codes(trade_date)
    candidates = scanned - limit_up
    print(f"  扫描候选={len(scanned)} 当日涨停={len(limit_up)} 未涨停候选={len(candidates)}")

    if not candidates:
        return pd.DataFrame()

    # 0. 加载概念缓存（PIT 动量特征依赖）
    if factor_ctx._CONCEPT_DAILY_CACHE is None or factor_ctx._CONCEPT_MEMBER_CACHE is None:
        try:
            factor_ctx.load_concept_data_from_cache()
            print(f"  概念缓存已加载")
        except Exception as e:
            print(f"  [warn] 概念缓存加载失败: {e}; sector_* 特征将使用默认值")

    # 0.5 从 analysis 记录读取五维度分
    dim_scores = get_dim_scores_by_code(trade_date)

    # 1. 拉/读面板数据（往前 lookback 天用于计算特征，往后 3 天用于标签）
    from datetime import datetime, timedelta
    d_dt = datetime.strptime(trade_date, "%Y%m%d")
    start = (d_dt - timedelta(days=lookback + 15)).strftime("%Y%m%d")
    end = (d_dt + timedelta(days=10)).strftime("%Y%m%d")
    dates_all = _trade_dates(start, end)

    codes_list = sorted(candidates)
    print(f"  拉/读 daily ({start}~{end})...")
    daily = pull_daily_bars(codes_list, start, end)
    print(f"  拉/读 daily_basic ({start}~{end})...")
    dbasic = pull_daily_basic_bars(codes_list, start, end)
    print(f"  拉/读 moneyflow ({start}~{end})...")
    mf = pull_moneyflow_bars(codes_list, start, end)
    print(f"  拉/读 top_list ({start}~{end})...")
    top_list = pull_top_list_bars(codes_list, start, end)
    print(f"  拉/读 top_inst ({start}~{end})...")
    top_inst = pull_top_inst_bars(codes_list, start, end)
    print(f"  拉/读 auction ({start}~{end})...")
    auction = pull_auction_bars(codes_list, start, end)

    # 2. 索引
    daily_by_code: dict[str, list[dict]] = defaultdict(list)
    for _, r in daily.iterrows():
        daily_by_code[r["ts_code"]].append(r.to_dict())

    available_dates = sorted(daily["trade_date"].unique())
    pit_idx = available_dates.index(trade_date) - 1 if trade_date in available_dates else -1
    pit_date = available_dates[pit_idx] if pit_idx >= 0 else trade_date

    print(f"  拉/读 intraday metrics ({pit_date})...")
    intraday = pull_intraday_metrics(codes_list, [pit_date])

    dbasic_by_code_date: dict[str, dict[str, dict]] = defaultdict(dict)
    for _, r in dbasic.iterrows():
        dbasic_by_code_date[r["ts_code"]][r["trade_date"]] = r.to_dict()

    mf_by_code_date: dict[str, dict[str, dict]] = defaultdict(dict)
    for _, r in mf.iterrows():
        mf_by_code_date[r["ts_code"]][r["trade_date"]] = r.to_dict()

    top_list_by_code_date: dict[str, dict[str, dict]] = defaultdict(dict)
    for _, r in top_list.iterrows():
        top_list_by_code_date[r["ts_code"]][r["trade_date"]] = r.to_dict()

    top_inst_by_code_date: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for _, r in top_inst.iterrows():
        top_inst_by_code_date[r["ts_code"]][r["trade_date"]].append(r.to_dict())

    auction_by_code_date: dict[str, dict[str, dict]] = defaultdict(dict)
    for _, r in auction.iterrows():
        auction_by_code_date[r["ts_code"]][r["trade_date"]] = r.to_dict()

    intraday_by_code_date: dict[str, dict[str, dict]] = defaultdict(dict)
    for _, r in intraday.iterrows():
        intraday_by_code_date[r["ts_code"]][r["trade_date"]] = r.to_dict()

    # 3. 涨停基因索引（需要 lookback 期内所有股票的涨停日期）
    #    对每天的涨停股取并集
    from scripts.tu_share import call_tushare
    print(f"  拉涨停记录（lookback {lookback} 天）...")
    limit_by_date: dict[str, set[str]] = {}
    resp = call_tushare(
        "limit_list_d",
        {"start_date": start, "end_date": trade_date, "limit_type": "U"},
        "ts_code,trade_date",
    )
    items = resp.get("data", {}).get("items", [])
    fields = resp.get("data", {}).get("fields", [])
    for row in items:
        d = dict(zip(fields, row))
        date = d.get("trade_date")
        code = d.get("ts_code")
        if date and code:
            limit_by_date.setdefault(date, set()).add(code)
    limit_by_code = _build_limit_gene_index(limit_by_date)

    # 4. 逐股提取样本并打标签
    rows_out = []
    for code in codes_list:
        row = _extract_row(code, trade_date, daily_by_code, dbasic_by_code_date,
                            mf_by_code_date, auction_by_code_date, intraday_by_code_date,
                            top_list_by_code_date,
                            top_inst_by_code_date, limit_by_code, dim_scores, dates_all)
        if row is None:
            continue
        # 正样本：当日未涨停，但未来 3 日内涨停
        # 负样本：当日未涨停，未来 3 日也未涨停
        # 数据不足导致 hit_limit_3 缺失则跳过
        hit = row.get("hit_limit_3")
        if hit is None:
            continue
        row["label"] = 1 if hit == 1 else 0
        rows_out.append(row)

    df = pd.DataFrame(rows_out)
    if df.empty:
        return df

    # 列顺序整理
    order = ["code", "trade_date", "label"] + FEATURE_COLS + LABEL_COLS
    df = df[[c for c in order if c in df.columns]]
    print(f"  样本 {len(df)} 行 (正 {(df['label']==1).sum()}, 负 {(df['label']==0).sum()})")
    return df


# ══════════════════════════════════════════════════════
# CSV 全集管理
# ══════════════════════════════════════════════════════

def load_all() -> pd.DataFrame:
    if not TRAINING_CSV.exists():
        return pd.DataFrame(columns=["code", "trade_date", "label"] + FEATURE_COLS + LABEL_COLS)
    return pd.read_csv(TRAINING_CSV, dtype={"trade_date": str, "code": str})


def existing_dates() -> list[str]:
    df = load_all()
    if df.empty:
        return []
    return sorted(df["trade_date"].astype(str).unique().tolist())


def check_coverage(dates: list[str]) -> dict:
    have = set(existing_dates())
    want = set(dates)
    return {
        "present": sorted(want & have),
        "missing": sorted(want - have),
    }


def build(dates: list[str], force: bool = False):
    """为指定日期构建训练样本并追加到全集 CSV。已存在的日期默认跳过（force=True 覆盖）。"""
    existing = set(existing_dates())
    to_build = [d for d in dates if force or d not in existing]
    if not to_build:
        print(f"[build] 所有 {len(dates)} 天已存在")
        return
    print(f"[build] 需构建 {len(to_build)} 天: {to_build[0]} ~ {to_build[-1]}")

    new_frames = []
    for d in to_build:
        df = build_one_day(d)
        if not df.empty:
            new_frames.append(df)

    if not new_frames:
        print("[build] 无新数据")
        return

    new_df = pd.concat(new_frames, ignore_index=True)

    # 合并到 CSV：force 时先删除旧日期，再追加新日期；非 force 时按 (code, trade_date) 去重
    all_df = load_all()
    if force:
        to_remove = set(to_build)
        all_df = all_df[~all_df["trade_date"].astype(str).isin(to_remove)].copy()
    combined = pd.concat([all_df, new_df], ignore_index=True)
    combined = combined.drop_duplicates(subset=["code", "trade_date"], keep="last")
    combined = combined.sort_values(["trade_date", "code"]).reset_index(drop=True)
    combined.to_csv(TRAINING_CSV, index=False)
    print(f"[build] 完成，CSV 现有 {len(combined)} 行 ({combined['trade_date'].nunique()} 天)")


def pick(dates: list[str]) -> pd.DataFrame:
    """从全集 CSV 挑出指定日期的样本。缺日期不自动补，由调用方决定。"""
    df = load_all()
    if df.empty:
        return df
    return df[df["trade_date"].astype(str).isin(set(dates))].reset_index(drop=True)


def ensure_and_pick(dates: list[str]) -> pd.DataFrame:
    """pick 前自动 build 缺失日期。"""
    cov = check_coverage(dates)
    if cov["missing"]:
        print(f"[ensure] 缺日期 {cov['missing']}，触发 build...")
        build(cov["missing"])
    return pick(dates)


# ══════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════

def _cmd_build(args):
    if args.dates:
        dates = args.dates.split(",")
    elif args.start and args.end:
        all_dates = _trade_dates(args.start, args.end)
        dates = all_dates
    else:
        print("需要 --dates 或 --start/--end")
        return
    build(dates, force=args.force)


def _cmd_check(args):
    if args.start and args.end:
        dates = _trade_dates(args.start, args.end)
    else:
        dates = args.dates.split(",")
    cov = check_coverage(dates)
    print(f"目标 {len(dates)} 天")
    print(f"  已有 {len(cov['present'])} 天: {cov['present'][:5]}{'...' if len(cov['present']) > 5 else ''}")
    print(f"  缺失 {len(cov['missing'])} 天: {cov['missing']}")


def _cmd_pick(args):
    dates = args.dates.split(",")
    if args.ensure:
        df = ensure_and_pick(dates)
    else:
        df = pick(dates)
        cov = check_coverage(dates)
        if cov["missing"]:
            print(f"[warn] 缺日期未补: {cov['missing']}")
    print(f"[pick] {len(df)} 行")
    if not df.empty:
        print(df[["code", "trade_date", "label", "pct_chg_score_day", "hit_limit_3"]].head(10))
        print(f"\n分布: label=1 {(df['label']==1).sum()}, label=0 {(df['label']==0).sum()}")


def _cmd_stats(args):
    df = load_all()
    if df.empty:
        print("训练集为空")
        return
    print(f"训练集全集: {len(df)} 行, {df['trade_date'].nunique()} 天")
    print(f"日期范围: {df['trade_date'].min()} ~ {df['trade_date'].max()}")
    print(f"正样本: {(df['label']==1).sum()} ({(df['label']==1).mean():.1%})")
    print(f"负样本: {(df['label']==0).sum()} ({(df['label']==0).mean():.1%})")
    print(f"\n有效标签行 (hit_limit_3 非空): {df['hit_limit_3'].notna().sum()}")
    valid = df.dropna(subset=["hit_limit_3"])
    if not valid.empty:
        print(f"  正样本 hit@3 命中率: {valid[valid['label']==1]['hit_limit_3'].mean():.2%}")
        print(f"  负样本 hit@3 命中率: {valid[valid['label']==0]['hit_limit_3'].mean():.2%}")


def main():
    parser = argparse.ArgumentParser(description="训练集管理")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_build = sub.add_parser("build", help="构建训练样本并追加到 CSV")
    p_build.add_argument("--start", help="起始日 YYYYMMDD")
    p_build.add_argument("--end", help="结束日 YYYYMMDD")
    p_build.add_argument("--dates", help="逗号分隔日期")
    p_build.add_argument("--force", action="store_true", help="覆盖已有日期")
    p_build.set_defaults(func=_cmd_build)

    p_check = sub.add_parser("check", help="检查日期覆盖情况")
    p_check.add_argument("--start")
    p_check.add_argument("--end")
    p_check.add_argument("--dates")
    p_check.set_defaults(func=_cmd_check)

    p_pick = sub.add_parser("pick", help="pick 指定日期的样本")
    p_pick.add_argument("--dates", required=True)
    p_pick.add_argument("--ensure", action="store_true", help="缺日期自动 build")
    p_pick.set_defaults(func=_cmd_pick)

    p_stats = sub.add_parser("stats", help="训练集全集统计")
    p_stats.set_defaults(func=_cmd_stats)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
