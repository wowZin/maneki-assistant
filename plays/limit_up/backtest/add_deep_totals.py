#!/usr/bin/env python3
"""为已有的 analysis_rebuilt 文件补充深度挖掘综合分。

用法:
    python plays/limit_up/backtest/add_deep_totals.py

流程:
1. 读取 plays/limit_up/data/analysis_rebuilt/*.json
2. 调用 pipeline._compute_deep_total_batch 计算新 total
3. 覆盖写回 JSON
4. 重新生成 out/panel_rebuilt.csv
"""
from __future__ import annotations

import glob
import json
import os
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent.parent
PLAY_DIR = Path(__file__).resolve().parent.parent
ANALYSIS_DIR = PLAY_DIR / "data" / "analysis_rebuilt"

import sys
sys.path.insert(0, str(PROJECT_DIR))

from plays.limit_up.pipeline import _compute_deep_total_batch
from plays.limit_up.strategies import factor_ctx


def main():
    # 加载概念缓存（PIT 概念数据必需）
    cache_dir = Path(__file__).resolve().parent / "cache"
    factor_ctx.load_concept_data_from_cache(cache_dir)

    files = sorted(glob.glob(str(ANALYSIS_DIR / "*.json")))
    print(f"读取 {len(files)} 个 rebuilt 分析文件")

    for f in files:
        fname = os.path.basename(f)
        try:
            recs = json.load(open(f))
        except Exception:
            continue
        if not isinstance(recs, list) or not recs or recs[0].get("_empty"):
            continue

        # date 来自文件名 YYYYMMDD_HHMM
        date = fname.split("_")[0]

        # 计算深度综合分（PIT）
        _compute_deep_total_batch(recs, pit_mode=False, trade_date=date)

        with open(f, "w") as out:
            json.dump(recs, out, ensure_ascii=False, indent=2)
        print(f"  已更新: {fname} ({len(recs)} 条)")

    print("\n重建面板...")
    from plays.limit_up.backtest.dataset import build_panel
    panel = build_panel(analysis_dir=ANALYSIS_DIR)
    out_path = Path(__file__).resolve().parent / "out" / "panel_rebuilt.csv"
    panel.to_csv(out_path, index=False)
    print(f"panel_rebuilt.csv: {len(panel)} 行, {len(panel.columns)} 列")
    print("columns:", sorted(panel.columns.tolist()))


if __name__ == "__main__":
    main()
