#!/usr/bin/env python3
"""重算历史面板的板块特征（sector_* + n_concepts），使用修复后的概念缓存。

2026-07-31：概念缓存修复（过滤 700xxx/883xxx 非概念指数）后，
历史面板的 sector 特征仍是旧污染值（n_concepts p50=58 异常）。
本脚本对每天面板全市场重算板块特征并写回。
"""
import sys
from pathlib import Path

import pandas as pd

PROJECT_DIR = Path("/root/maneki-agent")
sys.path.insert(0, str(PROJECT_DIR))

from plays.limit_up.strategies import factor_ctx

DATES = ["20260701", "20260702", "20260703", "20260706", "20260707",
         "20260708", "20260709", "20260710", "20260713", "20260714",
         "20260715", "20260716", "20260717", "20260720", "20260721",
         "20260722", "20260723", "20260724",
         "20260727", "20260728", "20260729", "20260730", "20260731"]
PANEL_DIR = PROJECT_DIR / "wiki" / "raw" / "limit-up" / "panel"


def recompute(code_short: str, trade_date: str) -> dict:
    m = factor_ctx.get_concept_momentum(code_short, trade_date=trade_date)
    return {
        "sector_heat": m.get("ret1_avg", 0.0),
        "sector_rank": m.get("up_ratio", 0.0),
        "n_concepts": m.get("n_concepts", 0),
        "sector_ret3": m.get("ret3_avg", 0.0),
        "sector_up_ratio": m.get("up_ratio", 0.0),
        "sector_streak": m.get("up_streak_max", 0),
    }


def main():
    factor_ctx.load_concept_data_from_cache()
    print("概念缓存已加载")

    for date in DATES:
        panel_file = PANEL_DIR / f"{date}.parquet"
        if not panel_file.exists():
            print(f"[{date}] 无面板，跳过")
            continue
        df = pd.read_parquet(panel_file)
        print(f"[{date}] {len(df)} 只，重算板块特征...", flush=True)

        vals = {c: [] for c in ["sector_heat", "sector_rank", "n_concepts",
                                "sector_ret3", "sector_up_ratio", "sector_streak"]}
        for code in df["code"]:
            short = code.split(".")[0]
            m = recompute(short, date)
            for c in vals:
                vals[c].append(m[c])

        for c, v in vals.items():
            df[c] = v

        df.to_parquet(panel_file, index=False)
        print(f"  [{date}] 完成, n_concepts p50={df['n_concepts'].median():.0f}")


if __name__ == "__main__":
    main()
