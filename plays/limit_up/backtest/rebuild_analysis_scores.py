#!/usr/bin/env python3
"""
用当前策略代码重新对历史 analysis 记录打分，生成新的 analysis JSON 并重建 panel。

用法:
    python plays/limit_up/backtest/rebuild_analysis_scores.py

流程:
1. 读取 plays/limit_up/data/analysis/*.json
2. 按日期分组，对每只股票调用 pipeline._score_one 重新打分
3. 调用 pipeline._compute_balanced_total_batch 计算新的 balanced_total
4. 写入 plays/limit_up/data/analysis_rebuilt/
5. 调用 dataset.build_panel 生成 out/panel_rebuilt.csv

注意: 会调用 Tushare 和同花顺接口，耗时较长。
"""
from __future__ import annotations

import glob
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent.parent
PLAY_DIR = Path(__file__).resolve().parent.parent
ANALYSIS_DIR = PLAY_DIR / "data" / "analysis"
REBUILT_DIR = PLAY_DIR / "data" / "analysis_rebuilt"
REBUILT_DIR.mkdir(exist_ok=True)

import sys

sys.path.insert(0, str(PROJECT_DIR))

from plays.limit_up.pipeline import (
    _score_one,
    _compute_balanced_total_batch,
    _compute_balanced_total_v2_batch,
    _compute_sentiment_adaptive_total_batch,
    _compute_ultimate_total_batch,
    _compute_deep_total_batch,
    _fetch_nv2_data,
)
from plays.limit_up.strategies import factor_ctx

WEIGHTS = {
    "fundamental": 1.5,
    "technical": 1.0,
    "fundflow": 1.0,
    "sentiment": 1.2,
    "shortterm": 1.5,
}


def load_analysis_files() -> list[tuple[str, str, list[dict]]]:
    out = []
    for f in sorted(glob.glob(str(ANALYSIS_DIR / "*.json"))):
        fname = os.path.basename(f)
        if fname.startswith("v2_"):
            continue
        parts = fname.replace(".json", "").split("_")
        if len(parts) != 2:
            continue
        date, ts = parts
        try:
            recs = json.load(open(f))
        except Exception:
            continue
        if not isinstance(recs, list) or not recs or recs[0].get("_empty"):
            continue
        out.append((date, ts, recs))
    return out


def rescore_date(date: str, recs: list[dict], max_workers: int = 1) -> list[dict]:
    """对某一天的所有记录重新打分（单线程，避免 Tushare 并发超限）。"""
    # 去重 code
    seen = {}
    for r in recs:
        code = r.get("code")
        if code:
            seen[code] = r
    unique_recs = list(seen.values())

    # 预拉取 NV2 数据
    codes = [r["code"] for r in unique_recs]
    _fetch_nv2_data(codes)

    # 预加载概念数据到 factor_ctx
    cache_dir = Path(__file__).resolve().parent / "cache"
    factor_ctx.load_concept_data_from_cache(cache_dir)

    results = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(_score_one, {"code": r["code"], "name": r.get("name", ""), "pct_chg": r.get("pct_chg", 0)}, False, WEIGHTS): r
            for r in unique_recs
        }
        for future in as_completed(futures):
            r = futures[future]
            try:
                scored = future.result()
                # 保留原始字段
                scored["name"] = r.get("name", scored.get("name", ""))
                scored["pct_chg"] = r.get("pct_chg", scored.get("pct_chg", 0))
                results.append(scored)
            except Exception as e:
                print(f"  {r.get('code')} 打分失败: {e}")
                results.append(r)

    # 计算各类综合评分
    _compute_balanced_total_batch(results, pit_mode=False)
    _compute_balanced_total_v2_batch(results, pit_mode=False)
    _compute_sentiment_adaptive_total_batch(results, pit_mode=False)
    _compute_ultimate_total_batch(results, pit_mode=False)
    _compute_deep_total_batch(results, pit_mode=False)
    return results


def main():
    files = load_analysis_files()
    print(f"读取 {len(files)} 个分析文件")

    for date, ts, recs in sorted(files):
        # 仅重建 20260601 之后的数据
        if date < "20260601":
            continue
        print(f"\n[{date}] 重新打分: {len(recs)} 条记录")
        start = time.time()
        new_recs = rescore_date(date, recs)
        out_file = REBUILT_DIR / f"{date}_{ts}.json"
        with open(out_file, "w") as f:
            json.dump(new_recs, f, ensure_ascii=False, indent=2)
        print(f"  已保存: {out_file} ({len(new_recs)} 条, {time.time()-start:.1f}s)")

    print("\n重建面板...")
    from plays.limit_up.backtest.dataset import build_panel
    panel = build_panel(analysis_dir=REBUILT_DIR)
    panel.to_csv(Path(__file__).resolve().parent / "out" / "panel_rebuilt.csv", index=False)
    print(f"panel_rebuilt.csv: {len(panel)} 行")


if __name__ == "__main__":
    main()
