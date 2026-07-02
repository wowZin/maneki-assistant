#!/usr/bin/env python3
"""PIT 验证：用真实扫描信号验证训练出的权重。

与 validate_balanced.py 的区别：
- 不重新从日线计算 balanced_total（避免再次使用前视收盘）
- 直接用扫描记录里的五维度分，按 pit_weights.json 的权重算 total
- 按 Top-K 推送，对比真实涨停标签
"""

from __future__ import annotations

import argparse
import glob
import json
import os
from pathlib import Path

import pandas as pd


PLAY_DIR = Path(__file__).resolve().parent.parent
ANALYSIS_DIR = PLAY_DIR / "data" / "analysis"
CACHE_DIR = Path(__file__).resolve().parent / "cache"
LIMIT_PCT = 9.8
DIMS = ["fundamental", "technical", "fundflow", "sentiment", "shortterm"]


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


def compute_total(record: dict, weights: dict[str, float]) -> float:
    """Top-3 加权平均综合评分。"""
    s = record.get("scores", {})
    dc = [(float(s.get(d, 0) or 0), weights.get(d, 1.0)) for d in DIMS]
    dc.sort(key=lambda x: x[0] * x[1], reverse=True)
    top3 = dc[:3]
    denom = sum(w for _, w in top3)
    if denom == 0:
        return 0.0
    return sum(s * w for s, w in top3) / denom


def _next_dates(dates: list[str], n: int = 3) -> dict[str, list[str]]:
    """为每个日期返回之后 n 个交易日的列表。"""
    nxt: dict[str, list[str]] = {}
    for i, d in enumerate(dates):
        nxt[d] = dates[i + 1: i + 1 + n]
    return nxt


def validate(weights_path: str | Path, k: int = 3):
    weights_path = Path(weights_path)
    with open(weights_path) as f:
        weights_data = json.load(f)

    candidates_weights = weights_data.get("candidates", [])
    if not candidates_weights:
        print("无有效权重")
        return

    # 默认使用排名第一的候选权重
    best = candidates_weights[0]
    weights = best["weights"]
    print(f"\n[验证] 权重: {weights}")

    bars = load_daily_bars()
    all_dates = sorted(bars["trade_date"].unique().tolist())
    next_dates = _next_dates(all_dates, 3)

    files = load_analysis_files()
    print(f"读取 {len(files)} 个分析文件")

    # 每个交易日合并所有扫描记录，同代码取时间戳最大的一次
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
        # 默认验证近 20 个交易日，与训练期一致
        if date < "20260601" or date > "20260630":
            continue
        recs = [r for _, r in code_map.values()]
        # 为每条记录计算 PIT total
        scored = []
        for r in recs:
            code = r.get("code", "")
            total = compute_total(r, weights)
            scored.append((total, r))

        if not scored:
            continue

        scored.sort(key=lambda x: x[0], reverse=True)
        pushed = scored[:k]

        for total, r in pushed:
            code = r.get("code", "")
            name = r.get("name", "")

            # 命中：未来 3 个交易日出现涨停
            fut_dates = next_dates.get(date, [])
            fut = bars[(bars["ts_code"] == code) & (bars["trade_date"].isin(fut_dates))]
            hit = int((fut["pct_chg"] >= LIMIT_PCT).any()) if not fut.empty else 0

            # 胜率：下一个交易日收盘 > 评分日收盘 × 1.001
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
                "pit_total": round(total, 2), "hit": hit, "win": win,
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

    # 保存明细
    out_dir = Path(__file__).resolve().parent / "data"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "validate_pit_result.json"
    with open(out_file, "w") as f:
        json.dump({
            "k": k,
            "weights": weights,
            "total_pushed": total_pushed,
            "hit_rate": round(hr, 4),
            "win_rate": round(wr, 4),
            "records": records,
        }, f, ensure_ascii=False, indent=2)
    print(f"\n已保存: {out_file}")


def main():
    parser = argparse.ArgumentParser(description="PIT 验证期回放")
    parser.add_argument("--weights", default="plays/limit_up/backtest/data/pit_weights.json")
    parser.add_argument("--k", type=int, default=3, help="每日推送数量")
    args = parser.parse_args()
    validate(args.weights, k=args.k)


if __name__ == "__main__":
    main()
