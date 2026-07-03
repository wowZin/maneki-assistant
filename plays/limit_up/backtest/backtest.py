#!/usr/bin/env python3
"""固定回测框架 — 输入日期，输出命中率与胜率。

数据源：每天 pipeline 产出的 analysis JSON（同一天多轮扫描每轮独立）
数据路径：wiki/raw/limit-up/analysis/ (历史归档) + plays/limit_up/data/analysis/ (当日)

流程：
1. 装载指定日期区间的 analysis 记录（全部轮次）
2. 拉未来 3 日 daily 数据算 hit_limit_3 / fwd_ret_3 标签
3. 用 total_score() 重算总分
4. 输出 Top-K 命中率、胜率、平均收益，以及阈值推送模式的数据

用法：
    # 回测最近 20 个有数据的交易日
    python plays/limit_up/backtest/backtest.py --days 20

    # 回测指定日期区间
    python plays/limit_up/backtest/backtest.py --start 20260601 --end 20260630

    # 回测指定几个交易日
    python plays/limit_up/backtest/backtest.py --dates 20260601,20260602,20260603
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from plays.limit_up.backtest.dataset import build_panel, load_analysis_records
from plays.limit_up.backtest.metrics import rank_ic


def _fmt_pct(x):
    """百分比格式；NaN 显示占位符。"""
    return "-" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x:.2%}"


def _fmt_ic(x):
    return "-" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x:.4f}"


def report(df: pd.DataFrame):
    """输出命中率、胜率、IC 表格。未来 1/2/3 日各自独立，数据不足时占位符。"""
    df = df.dropna(subset=["total_score"])
    if df.empty:
        print("[report] 无有效样本（total_score 全空）")
        return

    n_days = df["date"].nunique()
    n_rounds = df.groupby(["date", "scan_time"]).ngroups
    n_codes = df["code"].nunique()

    print("\n" + "=" * 84)
    print(f"回测报告 · {df['date'].min()} ~ {df['date'].max()}")
    print("=" * 84)
    print(f"样本: {n_rounds} 轮扫描 × {n_codes} 股票 × {n_days} 交易日")

    # 各 N 日的可用样本量
    for n in (1, 2, 3):
        col = f"hit_limit_{n}"
        if col in df.columns:
            valid = df[col].notna().sum()
            base = df[col].mean()
            print(f"  未来 {n} 日: {valid} 条有标签, 基线 hit_rate = {_fmt_pct(base) if valid else '-'}")

    # ── Top-K 命中率与胜率（对 N=1/2/3 分别）──
    print(f"\n[Top-K 命中率]  (每轮扫描按 total_score 排序取 Top-K)")
    print(f"  {'K':>3} | {'日均池':>6} | {'命中@1':>7} | {'命中@2':>7} | {'命中@3':>7} | "
          f"{'胜@1':>6} | {'胜@2':>6} | {'胜@3':>6}")
    print("  " + "-" * 78)
    for k in (3, 5, 10, 20):
        hits_by_n = {1: [], 2: [], 3: []}
        wins_by_n = {1: [], 2: [], 3: []}
        pools = []
        for (date, scan_time), sub in df.groupby(["date", "scan_time"]):
            if len(sub) < 3:
                continue
            top = sub.nlargest(min(k, len(sub)), "total_score")
            pools.append(len(sub))
            for n in (1, 2, 3):
                hcol = f"hit_limit_{n}"
                rcol = f"fwd_ret_{n}"
                if hcol in top.columns and top[hcol].notna().any():
                    hits_by_n[n].append(top[hcol].mean())
                if rcol in top.columns and top[rcol].notna().any():
                    wins_by_n[n].append((top[rcol] > 0).mean())
        if not pools:
            continue

        def _avg(seq):
            return np.mean(seq) if seq else None

        h1, h2, h3 = _avg(hits_by_n[1]), _avg(hits_by_n[2]), _avg(hits_by_n[3])
        w1, w2, w3 = _avg(wins_by_n[1]), _avg(wins_by_n[2]), _avg(wins_by_n[3])
        print(f"  {k:>3} | {np.mean(pools):>6.0f} | "
              f"{_fmt_pct(h1):>7} | {_fmt_pct(h2):>7} | {_fmt_pct(h3):>7} | "
              f"{_fmt_pct(w1):>6} | {_fmt_pct(w2):>6} | {_fmt_pct(w3):>6}")

    # ── Top-K 平均收益 ──
    print(f"\n[Top-K 平均收益]  (未来 N 日累计收益)")
    print(f"  {'K':>3} | {'平均@1':>8} | {'平均@2':>8} | {'平均@3':>8} | "
          f"{'最大@1':>8} | {'最大@2':>8} | {'最大@3':>8}")
    print("  " + "-" * 78)
    for k in (3, 5, 10, 20):
        rets_by_n = {1: [], 2: [], 3: []}
        maxs_by_n = {1: [], 2: [], 3: []}
        for (date, scan_time), sub in df.groupby(["date", "scan_time"]):
            if len(sub) < 3:
                continue
            top = sub.nlargest(min(k, len(sub)), "total_score")
            for n in (1, 2, 3):
                rcol = f"fwd_ret_{n}"
                mcol = f"fwd_max_{n}"
                if rcol in top.columns and top[rcol].notna().any():
                    rets_by_n[n].append(top[rcol].mean())
                if mcol in top.columns and top[mcol].notna().any():
                    maxs_by_n[n].append(top[mcol].mean())

        def _avg(seq):
            return np.mean(seq) if seq else None

        r1, r2, r3 = _avg(rets_by_n[1]), _avg(rets_by_n[2]), _avg(rets_by_n[3])
        m1, m2, m3 = _avg(maxs_by_n[1]), _avg(maxs_by_n[2]), _avg(maxs_by_n[3])
        print(f"  {k:>3} | {_fmt_pct(r1):>8} | {_fmt_pct(r2):>8} | {_fmt_pct(r3):>8} | "
              f"{_fmt_pct(m1):>8} | {_fmt_pct(m2):>8} | {_fmt_pct(m3):>8}")

    # ── 阈值推送模式 ──
    print(f"\n[阈值推送模式]  (total_score >= 阈值)")
    print(f"  {'阈值':>4} | {'总推送':>6} | {'日均':>6} | "
          f"{'命中@1':>7} | {'命中@2':>7} | {'命中@3':>7} | "
          f"{'胜率@3':>7} | {'收益@3':>7}")
    print("  " + "-" * 76)
    for thr in (25, 30, 35, 40, 45, 50, 60, 70, 85, 95, 100):
        sub = df[df["total_score"] >= thr]
        if sub.empty:
            print(f"  {thr:>4} | {'0':>6} | {'0':>6} | "
                  f"{'-':>7} | {'-':>7} | {'-':>7} | {'-':>7} | {'-':>7}")
            continue
        avg_daily = len(sub) / n_days
        h1 = sub["hit_limit_1"].mean() if "hit_limit_1" in sub.columns and sub["hit_limit_1"].notna().any() else None
        h2 = sub["hit_limit_2"].mean() if "hit_limit_2" in sub.columns and sub["hit_limit_2"].notna().any() else None
        h3 = sub["hit_limit_3"].mean() if "hit_limit_3" in sub.columns and sub["hit_limit_3"].notna().any() else None
        w3 = (sub["fwd_ret_3"] > 0).mean() if "fwd_ret_3" in sub.columns and sub["fwd_ret_3"].notna().any() else None
        r3 = sub["fwd_ret_3"].mean() if "fwd_ret_3" in sub.columns and sub["fwd_ret_3"].notna().any() else None
        print(f"  {thr:>4} | {len(sub):>6d} | {avg_daily:>6.1f} | "
              f"{_fmt_pct(h1):>7} | {_fmt_pct(h2):>7} | {_fmt_pct(h3):>7} | "
              f"{_fmt_pct(w3):>7} | {_fmt_pct(r3):>7}")

    # ── IC ──
    print(f"\n[排序 IC]  (每 N 日独立)")
    for n in (1, 2, 3):
        hcol = f"hit_limit_{n}"
        rcol = f"fwd_ret_{n}"
        mcol = f"fwd_max_{n}"
        parts = []
        if hcol in df.columns and df[hcol].notna().any():
            parts.append(f"IC(hit_limit_{n})={_fmt_ic(rank_ic(df['total_score'], df[hcol]))}")
        if rcol in df.columns and df[rcol].notna().any():
            parts.append(f"IC(fwd_ret_{n})={_fmt_ic(rank_ic(df['total_score'], df[rcol]))}")
        if mcol in df.columns and df[mcol].notna().any():
            parts.append(f"IC(fwd_max_{n})={_fmt_ic(rank_ic(df['total_score'], df[mcol]))}")
        if parts:
            print(f"  未来 {n} 日: " + " | ".join(parts))
        else:
            print(f"  未来 {n} 日: 无标签数据")


def resolve_dates(args) -> list[str] | None:
    """从命令行参数解析目标交易日列表。"""
    if args.dates:
        return sorted(args.dates.split(","))
    if args.start and args.end:
        # 通过 analysis 目录里的实际文件推断可用日期
        all_dates = sorted(set(load_analysis_records()["date"].unique().tolist()))
        return [d for d in all_dates if args.start <= d <= args.end]
    if args.days:
        all_dates = sorted(set(load_analysis_records()["date"].unique().tolist()))
        return all_dates[-args.days:]
    return None


def main():
    parser = argparse.ArgumentParser(description="打板玩法回测")
    parser.add_argument("--days", type=int, help="回测最近 N 个有数据的交易日")
    parser.add_argument("--start", help="回测起始日 YYYYMMDD")
    parser.add_argument("--end", help="回测结束日 YYYYMMDD")
    parser.add_argument("--dates", help="指定交易日列表，逗号分隔（YYYYMMDD,YYYYMMDD,...）")
    args = parser.parse_args()

    dates = resolve_dates(args)
    if not dates:
        parser.error("必须指定 --days / --start+--end / --dates 之一")

    print(f"[回测] 目标交易日 ({len(dates)} 天): {dates[0]} ~ {dates[-1]}")

    print("[面板] 装载 analysis 数据（全部轮次）+ join 未来标签 + 重算 total_score...")
    panel = build_panel(dates=dates)
    print(f"[面板] 完成 {len(panel)} 行")

    report(panel)


if __name__ == "__main__":
    main()
