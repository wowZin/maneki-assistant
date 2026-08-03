#!/usr/bin/env python3
"""最近一周每天 Top3 详情（当日涨停v1 + 生产旧模型对比）。"""
import sys
from pathlib import Path

import pandas as pd

PROJECT_DIR = Path("/root/maneki-agent")
sys.path.insert(0, str(PROJECT_DIR))

from plays.limit_up.backtest.model import LimitUpModel
from plays.limit_up.review import get_today_limit_up

DATES = ["20260727", "20260728", "20260729", "20260730", "20260731"]

m_new = LimitUpModel.load(Path("/root/maneki-agent/plays/limit_up/data/backtest/models"))
m_old = LimitUpModel.load(Path("/root/maneki-agent/plays/limit_up/data/backtest/models"))


def main():
    for d in DATES:
        panel_file = PROJECT_DIR / "wiki" / "raw" / "limit-up" / "panel" / f"{d}.parquet"
        if not panel_file.exists():
            print(f"{d}: 无面板")
            continue
        panel = pd.read_parquet(panel_file)
        main = panel[panel["code"].str.startswith(("00", "60"))].copy()
        limit = set(get_today_limit_up(d))
        main["is_limit"] = main["code"].isin(limit)

        m_new.predict_score(main) if False else None
        main["score_new"] = m_new.predict_score(main)
        main["score_old"] = m_old.predict_score(main)

        print(f"=== {d}  涨停 {len(limit)} 只 ===")
        print(f"{'排名':<4}{'代码':<12}{'名称':<8}{'新模型分':>8}{'旧模型分':>8}{'竞价涨幅':>8}  涨停")
        for name, col in (("新模型", "score_new"), ("旧模型", "score_old")):
            top3 = main.nlargest(3, col)
            for rank, (_, r) in enumerate(top3.iterrows(), 1):
                auc = r.get("auc_pct", float("nan"))
                auc_s = f"{auc:+.1f}%" if pd.notna(auc) else "  —"
                mark = "✅" if r["is_limit"] else "—"
                nm = str(r.get("name", ""))[:6]
                print(f"{name}{rank:<2}{r['code']:<12}{nm:<8}{r[col]:>8.1f}{r['score_old'] if name=='新模型' else r['score_new']:>8.1f}{auc_s:>8}  {mark}")
        print()


if __name__ == "__main__":
    main()
