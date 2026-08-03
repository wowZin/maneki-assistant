#!/usr/bin/env python3
"""回测新/旧模型在过去一周的当日涨停命中率。

模拟 pipeline 09:30 视角：用当日面板（T-1 特征 + 09:25 竞价刷新）评分，
主板 Top-K，对比当日涨停（review.get_today_limit_up 口径）。
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
MODELS = {
    "生产(旧)": LimitUpModel.load(Path("/root/maneki-agent/plays/limit_up/data/backtest/models")),
    "修复v2(生产)": LimitUpModel.load(Path("/root/maneki-agent/plays/limit_up/data/backtest/models")),
}


def main():
    print(f"{'日期':<10}" + "".join(f"{name:<14}" for name in MODELS))
    agg = {name: {"top3": [], "top5": [], "top10": [], "limit_total": []} for name in MODELS}
    for d in DATES:
        panel_file = PROJECT_DIR / "wiki" / "raw" / "limit-up" / "panel" / f"{d}.parquet"
        if not panel_file.exists():
            print(f"{d}: 无面板")
            continue
        panel = pd.read_parquet(panel_file)
        main = panel[panel["code"].str.startswith(("00", "60"))].copy()
        limit = set(get_today_limit_up(d))
        main["is_limit"] = main["code"].isin(limit)
        line = f"{d:<10}"
        for name, m in MODELS.items():
            main["model_score"] = m.predict_score(main)
            top3 = main.nlargest(3, "model_score")
            top5 = main.nlargest(5, "model_score")
            top10 = main.nlargest(10, "model_score")
            h3 = int(top3["is_limit"].sum())
            h5 = int(top5["is_limit"].sum())
            h10 = int(top10["is_limit"].sum())
            agg[name]["top3"].append(h3 / 3)
            agg[name]["top5"].append(h5 / 5)
            agg[name]["top10"].append(h10 / 10)
            agg[name]["limit_total"].append(len(limit))
            line += f"{h3}/3 {h5}/5 {h10}/10".ljust(14)
        line += f" 涨停{len(limit)}"
        print(line)

        # 打印修复v2模型的 Top3 明细
        m = MODELS["修复v2"]
        main["model_score"] = m.predict_score(main)
        top3 = main.nlargest(3, "model_score")
        print("    修复v2 Top3:", end="")
        for _, r in top3.iterrows():
            mark = "✅" if r["is_limit"] else "—"
            auc = r.get("auc_pct", float("nan"))
            auc_s = f"{auc:.1f}%" if pd.notna(auc) else "—"
            print(f" {r['code']}({r['model_score']:.0f}分/竞价{auc_s}){mark}", end="")
        print()

    print()
    print("=== 两周汇总（命中率）===")
    for name in MODELS:
        a = agg[name]
        n = len(a["top3"])
        print(f"{name}:")
        print(f"  Top3  {sum(a['top3'])/n*100:.1f}%  ({sum(round(x*3) for x in a['top3'])}/{n*3})")
        print(f"  Top5  {sum(a['top5'])/n*100:.1f}%  ({sum(round(x*5) for x in a['top5'])}/{n*5})")
        print(f"  Top10 {sum(a['top10'])/n*100:.1f}%  ({sum(round(x*10) for x in a['top10'])}/{n*10})")


if __name__ == "__main__":
    main()
