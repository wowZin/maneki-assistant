#!/usr/bin/env python3
"""评估 surge 主闸池阈值（SURGE_PANEL_SCORE）在新模型分布下的取舍。

对一个月每天：不同阈值 → 主闸池大小（THS 压力）+ 当日涨停覆盖率（召回）。
"""
import sys
from pathlib import Path

import pandas as pd

PROJECT_DIR = Path("/root/maneki-agent")
sys.path.insert(0, str(PROJECT_DIR))

from plays.limit_up.backtest.model import LimitUpModel
from plays.limit_up.review import get_today_limit_up

DATES = ["20260701", "20260702", "20260703", "20260706", "20260707",
         "20260708", "20260709", "20260710", "20260713", "20260714",
         "20260715", "20260716", "20260717", "20260720", "20260721",
         "20260722", "20260723", "20260724",
         "20260727", "20260728", "20260729", "20260730", "20260731"]
PANEL_DIR = PROJECT_DIR / "wiki" / "raw" / "limit-up" / "panel"

model = LimitUpModel.load(Path("/root/maneki-agent/plays/limit_up/data/backtest/models"))
THRESHOLDS = [10, 15, 20, 25, 30, 35, 40, 50]


def main():
    # {thr: {"pool": [size...], "recall": [覆盖涨停/总涨停...]}}
    stats = {t: {"pool": [], "recall": [], "limit_total": []} for t in THRESHOLDS}
    for d in DATES:
        panel_file = PANEL_DIR / f"{d}.parquet"
        if not panel_file.exists():
            continue
        panel = pd.read_parquet(panel_file)
        main = panel[panel["code"].str.startswith(("00", "60"))].copy()
        limit = set(get_today_limit_up(d))
        main["is_limit"] = main["code"].isin(limit)
        main["model_score"] = model.predict_score(main)

        n_limit = int(main["is_limit"].sum())
        for t in THRESHOLDS:
            pool = main[main["model_score"] >= t]
            covered = int(pool["is_limit"].sum())
            stats[t]["pool"].append(len(pool))
            stats[t]["recall"].append(covered / n_limit if n_limit else 0)
            stats[t]["limit_total"].append(n_limit)

    print(f"{'阈值':<6}{'主闸池日均':>10}{'THS压力':>10}{'涨停覆盖':>10}{'vs全市场涨停':>14}")
    for t in THRESHOLDS:
        pool = stats[t]["pool"]
        recall = stats[t]["recall"]
        covered = sum(int(r * l) for r, l in zip(stats[t]["recall"], stats[t]["limit_total"]))
        total_lim = sum(stats[t]["limit_total"])
        print(f"{t:<6}{sum(pool)/len(pool):>8.0f}只{'':>4}{covered/total_lim*100:>9.1f}%{total_lim:>8}只(月)")


if __name__ == "__main__":
    main()
