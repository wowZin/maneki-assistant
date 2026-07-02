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


def report(df: pd.DataFrame):
    """输出命中率、胜率、IC 表格。"""
    df = df.dropna(subset=["total_score", "hit_limit_3", "fwd_ret_3"])
    if df.empty:
        print("[report] 无有效样本")
        return

    n_days = df["date"].nunique()
    n_rounds = len(df)
    n_codes = df["code"].nunique()

    print("\n" + "=" * 68)
    print(f"回测报告 · {df['date'].min()} ~ {df['date'].max()}")
    print("=" * 68)
    print(f"样本: {n_rounds} 轮扫描 × {n_codes} 股票 × {n_days} 交易日")
    print(f"基线 hit_rate = {df['hit_limit_3'].mean():.2%}, 基线 avg_ret = {df['fwd_ret_3'].mean():.2%}")

    # ── Top-K 每轮取前 K 只 ──
    print(f"\n[Top-K 命中率与胜率]  (每轮扫描按 total_score 排序取 Top-K)")
    print(f"  {'K':>3} | {'日均池':>6} | {'命中率':>7} | {'胜率':>7} | {'3日平均收益':>10} | {'3日最大收益':>10}")
    print("  " + "-" * 66)
    for k in (3, 5, 10, 20):
        hits, wins, rets, maxs, pools = [], [], [], [], []
        # 一个"轮次" = 同一 (date, scan_time)
        for (date, scan_time), sub in df.groupby(["date", "scan_time"]):
            if len(sub) < 3:
                continue
            top = sub.nlargest(min(k, len(sub)), "total_score")
            hits.append(top["hit_limit_3"].mean())
            wins.append((top["fwd_ret_3"] > 0).mean())
            rets.append(top["fwd_ret_3"].mean())
            if "fwd_max_3" in top.columns:
                maxs.append(top["fwd_max_3"].mean())
            pools.append(len(sub))
        if not hits:
            continue
        print(f"  {k:>3} | {np.mean(pools):>6.0f} | {np.mean(hits):>7.2%} | "
              f"{np.mean(wins):>7.2%} | {np.mean(rets):>10.2%} | "
              f"{np.mean(maxs) if maxs else 0:>10.2%}")

    # ── 阈值推送模式 ──
    print(f"\n[阈值推送模式]  (total_score >= 阈值)")
    print(f"  {'阈值':>4} | {'总推送':>6} | {'日均':>6} | {'命中率':>7} | {'胜率':>7} | {'3日平均收益':>10}")
    print("  " + "-" * 64)
    for thr in (25, 30, 35, 40, 45, 50):
        sub = df[df["total_score"] >= thr]
        if sub.empty:
            print(f"  {thr:>4} | {'0':>6} | {'0':>6} | {'-':>7} | {'-':>7} | {'-':>10}")
            continue
        avg_daily = len(sub) / n_days
        print(f"  {thr:>4} | {len(sub):>6d} | {avg_daily:>6.1f} | "
              f"{sub['hit_limit_3'].mean():>7.2%} | "
              f"{(sub['fwd_ret_3'] > 0).mean():>7.2%} | "
              f"{sub['fwd_ret_3'].mean():>10.2%}")

    # ── IC (供参考) ──
    print(f"\n[排序 IC]")
    ic_hit = rank_ic(df["total_score"], df["hit_limit_3"])
    ic_fwd = rank_ic(df["total_score"], df["fwd_ret_3"])
    ic_fmax = rank_ic(df["total_score"], df.get("fwd_max_3", pd.Series([np.nan] * len(df))))
    print(f"  RankIC(hit_limit_3) = {ic_hit:.4f}")
    print(f"  RankIC(fwd_ret_3)   = {ic_fwd:.4f}")
    print(f"  RankIC(fwd_max_3)   = {ic_fmax:.4f}")


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
