#!/usr/bin/env python3
"""
真实扫描信号验证：用 balanced_total_pit_v2 作为排序分，
按 Top-K 推送后计算命中率/胜率。
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd


PROJECT_DIR = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from plays.limit_up.backtest.factor_lib import factor_balanced_total_pit_v2
from plays.limit_up.backtest.validate_adaptive import _extract_pit_features
from plays.limit_up.backtest.validate_balanced import load_daily_bars, load_daily_basic_bars

PLAY_DIR = Path(__file__).resolve().parent.parent
ANALYSIS_DIR = PLAY_DIR / "data" / "analysis"
CACHE_DIR = Path(__file__).resolve().parent / "cache"
LIMIT_PCT = 9.8


def load_analysis_files(analysis_dir: Path | str | None = None) -> list[tuple[str, str, list[dict]]]:
    """读取 analysis 文件，返回 (date, timestamp, records) 列表。"""
    analysis_dir = Path(analysis_dir) if analysis_dir else ANALYSIS_DIR
    out = []
    for f in sorted(glob.glob(str(analysis_dir / "*.json"))):
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


def _next_dates(dates: list[str], n: int = 3) -> dict[str, list[str]]:
    return {d: dates[i + 1: i + 1 + n] for i, d in enumerate(dates)}


def _compute_v2(record: dict, code: str, score_date: str, bars: pd.DataFrame, basic_bars: pd.DataFrame) -> float:
    s = record.get("scores", {})
    feats = _extract_pit_features(code, score_date, bars, basic_bars)
    row = {
        "sentiment": float(s.get("sentiment", 0)),
        "shortterm": float(s.get("shortterm", 0)),
        "technical": float(s.get("technical", 0)),
        "fundflow": float(s.get("fundflow", 0)),
        "fundamental": float(s.get("fundamental", 0)),
        **feats,
    }
    return factor_balanced_total_pit_v2(pd.Series(row))


def validate(k: int = 3, analysis_dir: Path | str | None = None):
    bars = load_daily_bars()
    basic_bars = load_daily_basic_bars()
    all_dates = sorted(bars["trade_date"].unique().tolist())
    next_dates = _next_dates(all_dates, 3)

    files = load_analysis_files(analysis_dir)
    print(f"读取 {len(files)} 个分析文件 (from {analysis_dir or ANALYSIS_DIR})")

    best_by_code: dict[str, dict[str, tuple[str, dict]]] = {}
    for date, ts, recs in files:
        if date not in best_by_code:
            best_by_code[date] = {}
        for r in recs:
            code = r.get("code", "")
            if not code:
                continue
            if code not in best_by_code[date] or ts > best_by_code[date][code][0]:
                best_by_code[date][code] = (ts, r)

    total_pushed = 0
    total_hit = 0
    total_win = 0
    records = []

    for date, code_map in sorted(best_by_code.items()):
        if date < "20260601" or date > "20260630":
            continue
        recs = [r for _, r in code_map.values()]
        scored = []
        for r in recs:
            code = r.get("code", "")
            bt = _compute_v2(r, code, date, bars, basic_bars)
            scored.append((bt, r))

        if not scored:
            continue

        scored.sort(key=lambda x: x[0], reverse=True)
        pushed = scored[:k]

        for bt, r in pushed:
            code = r.get("code", "")
            name = r.get("name", "")

            fut_dates = next_dates.get(date, [])
            fut = bars[(bars["ts_code"] == code) & (bars["trade_date"].isin(fut_dates))]
            hit = int((fut["pct_chg"] >= LIMIT_PCT).any()) if not fut.empty else 0

            score_day = bars[(bars["ts_code"] == code) & (bars["trade_date"] == date)]
            next_day = bars[(bars["ts_code"] == code) & (bars["trade_date"].isin(fut_dates[:1]))]
            win = 0
            if not score_day.empty and not next_day.empty:
                buy_close = float(score_day.iloc[0]["close"])
                sell_close = float(next_day.iloc[0]["close"])
                if buy_close > 0 and sell_close > buy_close * 1.001:
                    win = 1

            total_pushed += 1
            total_hit += hit
            total_win += win
            records.append({
                "date": date, "code": code, "name": name,
                "balanced_total_v2": round(bt, 2), "hit": hit, "win": win,
            })

    if total_pushed == 0:
        print("无推送记录")
        return

    hr = total_hit / total_pushed
    wr = total_win / total_pushed
    print(f"\n{'指标':<10} {'值':>8}")
    print("-" * 20)
    print(f"{'推送总数':<10} {total_pushed:>8}")
    print(f"{'命中数':<10} {total_hit:>8}")
    print(f"{'命中率':<10} {hr:>7.1%}")
    print(f"{'胜局数':<10} {total_win:>8}")
    print(f"{'胜率':<10} {wr:>7.1%}")

    out_dir = Path(__file__).resolve().parent / "data"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "validate_balanced_v2_result.json"
    with open(out_file, "w") as f:
        json.dump({
            "k": k,
            "total_pushed": total_pushed,
            "hit_rate": round(hr, 4),
            "win_rate": round(wr, 4),
            "records": records,
        }, f, ensure_ascii=False, indent=2)
    print(f"\n已保存: {out_file}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="balanced_total_v2 真实扫描信号验证")
    parser.add_argument("--k", type=int, default=3, help="每日推送数量")
    parser.add_argument("--analysis-dir", default=str(ANALYSIS_DIR), help="analysis 目录路径")
    args = parser.parse_args()
    validate(k=args.k, analysis_dir=args.analysis_dir)
