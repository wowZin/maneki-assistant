#!/usr/bin/env python3
"""
真实扫描信号验证：用 sentiment_adaptive_total_pit 作为排序分，
按 Top-K 推送后计算命中率/胜率。

与 validate_balanced.py 的区别：
- 使用 factor_lib.factor_sentiment_adaptive_total_pit 替代 balanced_total_pit
- 基于 sentiment 中轴 + 区间条件子因子做二次排序
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

from plays.limit_up.backtest.factor_lib import factor_sentiment_adaptive_total_pit

PLAY_DIR = Path(__file__).resolve().parent.parent
ANALYSIS_DIR = PLAY_DIR / "data" / "analysis"
CACHE_DIR = Path(__file__).resolve().parent / "cache"
LIMIT_PCT = 9.8


def load_daily_bars() -> pd.DataFrame:
    """加载本地缓存的日线数据。"""
    parquets = sorted([p for p in CACHE_DIR.glob("daily_*.parquet") if "basic" not in p.name])
    if not parquets:
        raise FileNotFoundError("无 daily parquet 缓存，先运行 dataset.build_panel()")
    df = pd.read_parquet(parquets[-1])
    df["trade_date"] = df["trade_date"].astype(str)
    numeric_cols = ["open", "high", "low", "close", "pre_close", "pct_chg", "vol", "amount"]
    for c in numeric_cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)


def load_daily_basic_bars() -> pd.DataFrame:
    """加载本地缓存的 daily_basic 数据。"""
    parquets = sorted(CACHE_DIR.glob("dbasic_*.parquet"))
    if not parquets:
        from plays.limit_up.backtest.dataset import pull_daily_basic_bars
        daily_parquets = sorted(CACHE_DIR.glob("daily_*.parquet"))
        if not daily_parquets:
            raise FileNotFoundError("无 daily parquet，无法确定 daily_basic 拉取范围")
        name = daily_parquets[-1].stem
        parts = name.split("_")
        start, end = parts[1], parts[2]
        all_codes = load_daily_bars()["ts_code"].unique().tolist()
        df = pull_daily_basic_bars(all_codes, start, end)
    else:
        df = pd.read_parquet(parquets[-1])
    df["trade_date"] = df["trade_date"].astype(str)
    for c in ["pe", "pb", "circ_mv", "turnover_rate", "volume_ratio"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)


def load_analysis_files() -> list[tuple[str, str, list[dict]]]:
    """读取 analysis 文件，返回 (date, timestamp, records) 列表。"""
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


def _safe_float(val, default=0.0):
    if val is None:
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def _extract_pit_features(
    code: str,
    score_date: str,
    bars: pd.DataFrame,
    basic_bars: pd.DataFrame,
) -> dict:
    """从本地缓存提取 PIT 特征，与 pipeline._extract_pit_features 对齐。"""
    g = bars[bars["ts_code"] == code]
    g = g[g["trade_date"] <= score_date].sort_values("trade_date", ascending=False)
    daily_rows = g.to_dict("records")

    feats = {
        "trailing_10": 0.0,
        "trailing_5": 0.0,
        "position_20d": 0.5,
        "pullback_10d": 0.1,
        "pullback_20d": 0.1,
        "pct_chg_std_10d": 0.0,
        "pct_chg_std_5d": 0.0,
        "max_pct_chg_5d": 0.0,
        "limit_up_count_20d": 0.0,
        "limit_up_count_60d": 0.0,
        "circ_mv": 0.0,
        "pe": 999.0,
        "pb": 999.0,
        "turnover_rate": 5.0,
        "volume_ratio": 1.0,
        "avg_amount_5d": 0.0,
        "amount_ratio": 1.0,
    }

    if not daily_rows or len(daily_rows) < 2:
        return feats

    def trailing(days: int) -> float:
        if len(daily_rows) < days:
            return 0.0
        try:
            return float(daily_rows[0]["close"]) / float(daily_rows[days - 1]["close"]) - 1.0
        except Exception:
            return 0.0

    feats["trailing_10"] = trailing(10)
    feats["trailing_5"] = trailing(5)

    try:
        highs = [float(r["high"]) for r in daily_rows[:20]]
        lows = [float(r["low"]) for r in daily_rows[:20]]
        closes = [float(r["close"]) for r in daily_rows[:20]]
        h20, l20, c0 = max(highs), min(lows), closes[0]
        if h20 > l20:
            feats["position_20d"] = (c0 - l20) / (h20 - l20)
    except Exception:
        pass

    def pullback(days: int) -> float:
        if len(daily_rows) < 2:
            return 0.1
        try:
            highs = [float(r["high"]) for r in daily_rows[:days]]
            c0 = float(daily_rows[0]["close"])
            h = max(highs)
            return max(0.0, (h - c0) / h) if h > 0 else 0.1
        except Exception:
            return 0.1

    feats["pullback_10d"] = pullback(10)
    feats["pullback_20d"] = pullback(20)

    try:
        pcts = [_safe_float(r.get("pct_chg"), 0.0) for r in daily_rows[:10]]
        if len(pcts) >= 5:
            feats["pct_chg_std_10d"] = float(np.std(pcts, ddof=0)) if len(pcts) >= 2 else 0.0
            feats["max_pct_chg_5d"] = max(pcts[:5])
        pcts5 = [_safe_float(r.get("pct_chg"), 0.0) for r in daily_rows[:5]]
        if len(pcts5) >= 2:
            feats["pct_chg_std_5d"] = float(np.std(pcts5, ddof=0))
    except Exception:
        pass

    # amount_ratio / avg_amount_5d
    try:
        amounts = [_safe_float(r.get("amount"), 0.0) for r in daily_rows[:5]]
        if amounts and all(a >= 0 for a in amounts):
            feats["avg_amount_5d"] = sum(amounts) / len(amounts)
            if feats["avg_amount_5d"] > 0 and amounts:
                feats["amount_ratio"] = amounts[0] / feats["avg_amount_5d"]
    except Exception:
        pass

    # daily_basic
    if not basic_bars.empty:
        bg = basic_bars[(basic_bars["ts_code"] == code) & (basic_bars["trade_date"] <= score_date)]
        bg = bg.sort_values("trade_date", ascending=False)
        if not bg.empty:
            basic = bg.iloc[0]
            feats["circ_mv"] = _safe_float(basic.get("circ_mv"), 0.0)
            feats["pe"] = _safe_float(basic.get("pe"), 999.0)
            feats["pb"] = _safe_float(basic.get("pb"), 999.0)
            feats["turnover_rate"] = _safe_float(basic.get("turnover_rate"), 5.0)
            feats["volume_ratio"] = _safe_float(basic.get("volume_ratio"), 1.0)

    return feats


def _limit_up_count(code: str, score_date: str, bars: pd.DataFrame, days: int) -> int:
    """近 N 个交易日（含今日）涨停次数。"""
    g = bars[bars["ts_code"] == code]
    if g.empty:
        return 0
    past = g[g["trade_date"] <= score_date].tail(days)
    return int((past["pct_chg"] >= LIMIT_PCT).sum())


def _compute_adaptive(record: dict, code: str, score_date: str, bars: pd.DataFrame, basic_bars: pd.DataFrame) -> float:
    """用 sentiment_adaptive_total_pit 计算综合评分。"""
    s = record.get("scores", {})
    feats = _extract_pit_features(code, score_date, bars, basic_bars)

    row = {
        "sentiment": _safe_float(s.get("sentiment"), 0.0),
        "shortterm": _safe_float(s.get("shortterm"), 0.0),
        "technical": _safe_float(s.get("technical"), 0.0),
        "fundamental": _safe_float(s.get("fundamental"), 0.0),
        "fundflow": _safe_float(s.get("fundflow"), 0.0),
        **feats,
    }
    return factor_sentiment_adaptive_total_pit(pd.Series(row))


def _next_dates(dates: list[str], n: int = 3) -> dict[str, list[str]]:
    nxt: dict[str, list[str]] = {}
    for i, d in enumerate(dates):
        nxt[d] = dates[i + 1: i + 1 + n]
    return nxt


def validate(k: int = 3):
    bars = load_daily_bars()
    basic_bars = load_daily_basic_bars()
    all_dates = sorted(bars["trade_date"].unique().tolist())
    next_dates = _next_dates(all_dates, 3)

    files = load_analysis_files()
    print(f"读取 {len(files)} 个分析文件")

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
            adaptive = _compute_adaptive(r, code, date, bars, basic_bars)
            scored.append((adaptive, r))

        if not scored:
            continue

        scored.sort(key=lambda x: x[0], reverse=True)
        pushed = scored[:k]

        for adaptive, r in pushed:
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
                "adaptive_total": round(adaptive, 2), "hit": hit, "win": win,
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
    out_file = out_dir / "validate_adaptive_result.json"
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
    parser = argparse.ArgumentParser(description="sentiment_adaptive_total 真实扫描信号验证")
    parser.add_argument("--k", type=int, default=3, help="每日推送数量")
    args = parser.parse_args()
    validate(k=args.k)
