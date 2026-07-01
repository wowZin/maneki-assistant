#!/usr/bin/env python3
"""
训练期：对历史涨停股进行因子分析

用法:
    python plays/limit_up/backtest/analyze.py --start 20260601 --end 20260620 --sample 10

通过 call_tushare patch 将策略的 Tushare 查询指向历史日期。
每只涨停股的评分约需 3-5s（Tushare 调用延迟），sample=10 时约 30-50s/交易日。
"""
import argparse
import json
import sys
import random
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from plays.limit_up.backtest.data import fetch_data


def analyze(start: str, end: str, use_jvquant: bool = False, sample: int = 10):
    """对历史涨停股做五维度评分"""
    cache = fetch_data(start, end, use_jvquant=use_jvquant, force=False)

    trade_dates = sorted([
        d for d, cal in cache["trade_cal"].items()
        if start <= d <= end and cal.get("is_open") == 1
    ])
    print(f"\n[分析] 共 {len(trade_dates)} 个交易日")

    all_records = []

    for date in trade_dates:
        limit_codes = _get_limit_stocks(date, cache)
        if not limit_codes:
            continue

        sampled = random.sample(limit_codes, min(sample, len(limit_codes)))
        print(f"  {date}: {len(limit_codes)} 只涨停 → 分析 {len(sampled)} 只", end="", flush=True)

        # 清 Tushare 缓存 + 填充策略依赖的全局缓存
        _TUSHARE_CACHE.clear()
        _populate_strategy_caches(date, cache)

        for code in sampled:
            rec = _score_one_stock(code, date)
            if rec:
                all_records.append(rec)
                print(".", end="", flush=True)
        print()

    _print_stats(all_records)

    if all_records:
        out_dir = Path(__file__).resolve().parent / "data"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / f"train_{start}_{end}.json"
        with open(out_file, "w") as f:
            json.dump(all_records, f, ensure_ascii=False, indent=2)
        print(f"已保存: {out_file}")


def _get_limit_stocks(date: str, cache: dict) -> list[str]:
    """获取当日主板非ST涨停股"""
    limit_rows = cache.get("limit_list_d", {}).get(date, [])
    sb_map = cache.get("stock_basic_map", {})
    codes = []
    for r in limit_rows:
        if str(r.get("limit", "")).upper() != "U":
            continue
        code = r.get("ts_code", "")
        pure = code.split(".")[0]
        if not (pure.startswith("0") or pure.startswith("2") or pure.startswith("6")) or pure.startswith("688"):
            continue
        basic = sb_map.get(code, {})
        name = str(basic.get("name", ""))
        if "ST" in name or "*ST" in name:
            continue
        list_date = str(basic.get("list_date", ""))
        if list_date and len(list_date) == 8:
            try:
                days = (datetime.strptime(date, "%Y%m%d") - datetime.strptime(list_date, "%Y%m%d")).days
                if days < 60:
                    continue
            except Exception:
                pass
        codes.append(code)
    return codes


def _populate_strategy_caches(date: str, cache: dict):
    """从 Tushare 缓存填充策略依赖的全局缓存（_THS_QUOTE_CACHE / _HOT_CONCEPT_CACHE / _HOT_LIST_ITEMS）"""
    from plays.limit_up.pipeline import (
        _THS_QUOTE_CACHE, _HOT_CONCEPT_CACHE, _HOT_LIST_ITEMS,
    )

    # 1. _THS_QUOTE_CACHE: 从当日 daily_basic 填充 pct_chg
    _THS_QUOTE_CACHE.clear()
    daily_rows = cache.get("daily", {}).get(date, [])
    for r in daily_rows:
        code_short = r.get("ts_code", "").replace(".SH", "").replace(".SZ", "")
        _THS_QUOTE_CACHE[code_short] = {
            "pct_chg": r.get("pct_chg", 0),
        }

    # 2. _HOT_CONCEPT_CACHE: 从 stock_basic 的 industry 作为概念代理
    _HOT_CONCEPT_CACHE.clear()
    sb_map = cache.get("stock_basic_map", {})
    for ts_code, info in sb_map.items():
        code_short = ts_code.replace(".SH", "").replace(".SZ", "")
        industry = str(info.get("industry", ""))
        if industry and industry != "nan":
            _HOT_CONCEPT_CACHE[code_short] = [industry]
        else:
            _HOT_CONCEPT_CACHE[code_short] = []

    # 3. _HOT_LIST_ITEMS: 从当日 daily 构造候选股列表（涨幅>0的非涨停）
    _HOT_LIST_ITEMS.clear()
    limit_set = {r.get("ts_code", "") for r in cache.get("limit_list_d", {}).get(date, [])
                 if str(r.get("limit", "")).upper() == "U"}
    for r in daily_rows:
        code = r.get("ts_code", "")
        pct = float(r.get("pct_chg", 0))
        if 0 <= pct < 9.5 and code not in limit_set:
            code_short = code.replace(".SH", "").replace(".SZ", "")
            industry_tags = _HOT_CONCEPT_CACHE.get(code_short, [])
            _HOT_LIST_ITEMS.append({
                "code": code_short,
                "name": sb_map.get(code, {}).get("name", ""),
                "pct_chg": pct,
                "tag": {"concept_tag": industry_tags},
            })


# 跨评分调用的 Tushare 结果缓存（同一天多只股共享，避免重复请求）
_TUSHARE_CACHE: dict[str, dict] = {}



def _score_one_stock(code: str, date: str) -> dict | None:
    """对一只股票跑五维度评分，传入 trade_date 使策略以历史日期视角运行"""
    from plays.limit_up.strategies.fundamental import score_fundamental
    from plays.limit_up.strategies.technical import score_technical
    from plays.limit_up.strategies.fundflow import score_fundflow
    from plays.limit_up.strategies.sentiment import score_sentiment
    from plays.limit_up.strategies.shortterm import score_shortterm

    fns = {
        "fundamental": (score_fundamental, {"trade_date": date}),
        "technical": (score_technical, {"trade_date": date}),
        "fundflow": (score_fundflow, {"trade_date": date}),
        "sentiment": (score_sentiment, {"trade_date": date}),
        "shortterm": (score_shortterm, {"trade_date": date}),
    }

    scores = {}
    reasons = {}
    for dim_name, (fn, kwargs) in fns.items():
        try:
            s, r = fn(code, **kwargs)
            scores[dim_name] = float(s)
            reasons[dim_name] = r
        except Exception as e:
            scores[dim_name] = 0.0
            reasons[dim_name] = f"评分异常: {e}"

    # total（加权 Top3 择优）
    weights = {"fundamental": 0.3, "technical": 0.7, "fundflow": 0.3, "sentiment": 0.5, "shortterm": 2.0}
    dc = [(scores[d], weights.get(d, 1.0)) for d in fns]
    dc.sort(key=lambda x: x[0] * x[1], reverse=True)
    top3 = dc[:3]
    total = sum(s * w for s, w in top3) / sum(w for _, w in top3) if sum(w for _, w in top3) > 0 else 0

    return {
        "code": code,
        "date": date,
        "scores": scores,
        "reasons": reasons,
        "total": round(total, 2),
    }


def _print_stats(records):
    if not records:
        return
    import numpy as np
    dims = ["fundamental", "technical", "fundflow", "sentiment", "shortterm"]
    print(f"\n{'维度':<12} {'均值':>6} {'中位数':>6} {'25%':>6} {'75%':>6}")
    print("-" * 36)
    for d in dims:
        vals = np.array([r["scores"].get(d, 0) for r in records], dtype=float)
        print(f"{d:<12} {np.mean(vals):>6.1f} {np.median(vals):>6.1f} {np.percentile(vals,25):>6.1f} {np.percentile(vals,75):>6.1f}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--sample", type=int, default=10, help="每交易日最多分析股数")
    parser.add_argument("--use-jvquant", action="store_true")
    args = parser.parse_args()
    analyze(args.start, args.end, args.use_jvquant, args.sample)


if __name__ == "__main__":
    main()
