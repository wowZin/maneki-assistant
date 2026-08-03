#!/usr/bin/env python3
"""统计过去一周涨停票在当前模型下的分值分布。

对每天（0727~0731）当日涨停的票，用当日面板（09:30 视角）评分，
统计 model_score 分布。
"""
import sys
from pathlib import Path

import pandas as pd

PROJECT_DIR = Path("/root/maneki-agent")
sys.path.insert(0, str(PROJECT_DIR))

from plays.limit_up.backtest.model import LimitUpModel
from plays.limit_up.review import get_today_limit_up

DATES = ["20260720", "20260721", "20260722", "20260723", "20260724",
         "20260727", "20260728", "20260729", "20260730", "20260731"]

model = LimitUpModel.load(Path("/root/maneki-agent/plays/limit_up/data/backtest/models"))


def main():
    all_scores = []
    for d in DATES:
        panel_file = PROJECT_DIR / "wiki" / "raw" / "limit-up" / "panel" / f"{d}.parquet"
        if not panel_file.exists():
            continue
        panel = pd.read_parquet(panel_file)
        main = panel[panel["code"].str.startswith(("00", "60"))].copy()
        limit = set(get_today_limit_up(d))
        main["is_limit"] = main["code"].isin(limit)
        main["model_score"] = model.predict_score(main)

        lim = main[main["is_limit"]].copy()
        lim["date"] = d
        if "auc_pct" not in lim.columns:
            lim["auc_pct"] = float("nan")
        all_scores.append(lim[["date", "code", "model_score", "auc_pct"]])

    df = pd.concat(all_scores, ignore_index=True)
    df.columns = ["date", "code", "model_score", "auc_pct"]
    print(f"统计区间 {DATES[0]}~{DATES[-1]} 涨停票总数: {len(df)}（主板，在面板有评分）")
    print()
    s = df["model_score"]
    print("=== 涨停票 model_score 分布 ===")
    print(f"min={s.min():.1f}  p10={s.quantile(0.1):.1f}  p25={s.quantile(0.25):.1f}")
    print(f"p50={s.median():.1f}  p75={s.quantile(0.75):.1f}  p90={s.quantile(0.9):.1f}")
    print(f"p95={s.quantile(0.95):.1f}  max={s.max():.1f}  mean={s.mean():.1f}")
    print()
    print("=== 分桶 ===")
    bins = [(90, 99, ">=90"), (80, 90, "80-90"), (70, 80, "70-80"), (60, 70, "60-70"),
            (50, 60, "50-60"), (40, 50, "40-50"), (30, 40, "30-40"), (0, 30, "<30")]
    n = len(df)
    for lo, hi, lab in bins:
        cnt = int(((s >= lo) & (s < hi)).sum())
        print(f"  {lab:<6}: {cnt:>3} 只 ({cnt/n*100:.1f}%)")
    print()
    print("=== 每日涨停票分位 ===")
    for d in DATES:
        sub = df[df["date"] == d]
        if sub.empty:
            continue
        ss = sub["model_score"]
        print(f"  {d}: n={len(sub):>3}  min={ss.min():.0f}  p50={ss.median():.0f}  p90={ss.quantile(0.9):.0f}  max={ss.max():.0f}")


if __name__ == "__main__":
    main()
