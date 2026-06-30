#!/usr/bin/env python3
"""
回测框架 — 基于 wiki 历史扫描信号 + 当前策略验证命中率/胜率

原理:
  1. 读取 wiki/raw/analysis/ 每个交易日的所有扫描文件
  2. 每天唯一股票只评分一次（缓存复用）
  3. 逐轮模拟 ScoreGap 推送
  4. jvQuant 分钟数据按扫描时间截断（模拟盘中实时）
  5. 与次日 tushare 涨停/涨幅对比

用法:
  python plays/limit_up/backtest_v2.py --days 5       # 近5天
  python plays/limit_up/backtest_v2.py --days 20      # 近20天

输出: data/backtest/report_{timestamp}.json
"""

import argparse, json, os, sys, time, numpy as np
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_DIR))
sys.path.insert(0, str(PROJECT_DIR / "scripts"))

WIKI_DIR = PROJECT_DIR / "wiki" / "raw" / "analysis"
BT_DIR = Path(__file__).resolve().parent / "data" / "backtest"
BT_DIR.mkdir(parents=True, exist_ok=True)

WEIGHTS = {"f": 0.5, "t": 0.5, "fl": 1.5, "s": 1.0, "st": 0.5}
DIM_MAP = {"f": "fundamental", "t": "technical", "fl": "fundflow",
           "s": "sentiment", "st": "shortterm"}

# ═══════════════════════════════════════════════════════════
# Phase 0: 数据加载
# ═══════════════════════════════════════════════════════════

def load_scans(days=20):
    """加载 wiki 扫描文件, 返回 {date: [(file, time_str, codes)]}"""
    files = sorted(os.listdir(WIKI_DIR))
    by_date = {}
    for fn in files:
        if not fn.endswith('.json'): continue
        date = fn[:8]
        time_str = fn[9:13]  # HHMM
        by_date.setdefault(date, []).append((fn, time_str))
    dates = sorted(by_date.keys())[-days:]
    return dates, by_date


def load_labels(dates):
    """拉取次日涨停和涨跌幅"""
    from scripts.tu_share import call_tushare, clear_tushare_cache
    lu, pct = {}, {}
    for d in dates:
        nd = (datetime.strptime(d, "%Y%m%d") + timedelta(days=1)).strftime("%Y%m%d")
        if nd not in lu:
            clear_tushare_cache()
            r = call_tushare("limit_list_d", {"trade_date": nd, "limit_type": "U"}, "ts_code")
            lu[nd] = set(it[0] for it in r.get("data", {}).get("items", []) if it)
        if nd not in pct:
            clear_tushare_cache()
            r = call_tushare("daily", {"trade_date": nd}, "ts_code,pct_chg")
            pct[nd] = {it[0]: float(it[1]) for it in r.get("data", {}).get("items", [])}
    return lu, pct


# ═══════════════════════════════════════════════════════════
# Phase 1: 逐股评分 (每天唯一股只评一次)
# ═══════════════════════════════════════════════════════════

def score_all_stocks(daily_codes, trade_date):
    """对一批股票评分, 返回 {code: {dim: score, total: float}}"""
    from plays.limit_up.strategies.fundamental import score_fundamental
    from plays.limit_up.strategies.technical import score_technical
    from plays.limit_up.strategies.fundflow import score_fundflow
    from plays.limit_up.strategies.sentiment import score_sentiment
    from plays.limit_up.strategies.shortterm import score_shortterm

    funcs = {"f": score_fundamental, "t": score_technical,
             "fl": score_fundflow, "s": score_sentiment, "st": score_shortterm}
    cache = {}

    def score_one(code):
        scores = {}
        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = {pool.submit(fn, code): name for name, fn in funcs.items()}
            for future in as_completed(futures):
                dim = futures[future]
                try: s, r = future.result(timeout=30)
                except: s = 0
                scores[dim] = s
        dc = [(scores.get(d, 0), WEIGHTS.get(d, 1.0)) for d in funcs]
        dc.sort(key=lambda x: x[0] * x[1], reverse=True)
        top3 = dc[:3]
        total = sum(s * w for s, w in top3) / sum(w for _, w in top3)
        return {"scores": scores, "total": total}

    for code in sorted(daily_codes):
        cache[code] = score_one(code)

    return cache


# ═══════════════════════════════════════════════════════════
# Phase 2: 逐轮 ScoreGap 模拟
# ═══════════════════════════════════════════════════════════

def enrich_intraday(score_cache, scan_data, trade_date):
    """用 jvQuant 分钟数据为高分股补充日内指标 (按扫描时间截断)"""
    from scripts.jvquant_client import JvQuantClient
    client = JvQuantClient()
    date_fmt = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:8]}"

    # 收集候选: 所有扫描中总分前10的唯一股票
    all_codes = set()
    for fn, time_str, codes in scan_data:
        all_codes.update(codes)
    ranked = sorted(all_codes, key=lambda c: score_cache.get(c, {}).get("total", 0), reverse=True)
    to_enrich = [c for c in ranked[:15] if score_cache.get(c, {}).get("total", 0) > 35]

    if not to_enrich: return
    intraday = {}  # {code: {scan_time: metrics}}

    for code in to_enrich:
        try:
            md = client.get_minute_data(code, date_fmt, 1)
            if not md.get("series"): continue
            bars = md["series"][0]["bars"]
            if not bars: continue

            intraday[code] = {}
            for fn, time_str, codes in scan_data:
                if code not in codes: continue
                # 截断到扫描时刻
                scan_time = f"{time_str[:2]}:{time_str[2:]}"
                filtered = [b for b in bars if b[0] <= scan_time]
                if len(filtered) < 3: continue

                prices = [b[1] for b in filtered]
                volumes = [b[3] for b in filtered]
                total_vol = sum(volumes)
                total_amt = sum(p * v for p, v in zip(prices, volumes))
                vwap = total_amt / total_vol if total_vol > 0 else prices[-1]

                # 日内指标
                intraday[code][time_str] = {
                    "price": prices[-1],
                    "open": prices[0],
                    "high": max(prices),
                    "low": min(prices),
                    "vwap": round(vwap, 2),
                    "return_since_open": round((prices[-1] / prices[0] - 1) * 100, 2),
                    "vwap_position": round((prices[-1] / vwap - 1) * 100, 2) if vwap > 0 else 0,
                    "vol": total_vol,
                }
                # 尾盘检测 (14:30后扫描)
                if scan_time >= "14:30":
                    tail = [b for b in filtered if b[0] >= "14:30"]
                    tail_vol = sum(b[3] for b in tail)
                    intraday[code][time_str]["tail_vol_ratio"] = (
                        round(tail_vol / total_vol, 3) if total_vol > 0 else 0)
        except Exception:
            pass

    # 将日内指标注入 score_cache
    for code, metrics_by_time in intraday.items():
        if code in score_cache:
            score_cache[code]["intraday"] = metrics_by_time


def simulate_push(scan_codes, score_cache, scan_time_str=None, gap=0.90):
    """对一轮扫描的候选股做 ScoreGap 推送决策, 含日内调整"""
    results = []
    for c in scan_codes:
        if c not in score_cache: continue
        sc = score_cache[c]
        total = sc["total"]
        # 日内调整: 有 jvQuant 分钟数据时, 根据盘中走势微调
        intra = sc.get("intraday", {}).get(scan_time_str, {}) if scan_time_str else {}
        if intra:
            # 价格在VWAP上方→加分, 下方→扣分
            vwap_pos = intra.get("vwap_position", 0)
            if vwap_pos > 1: total += 3
            elif vwap_pos < -2: total -= 5
            # 尾盘放量下跌→扣分
            if intra.get("tail_vol_ratio", 0) > 0.25 and vwap_pos < 0:
                total -= 8
        results.append({"code": c, "total": total})
    if not results: return []
    results.sort(key=lambda x: x["total"], reverse=True)
    threshold = results[0]["total"] * gap
    return [r for r in results if r["total"] >= threshold]


# ═══════════════════════════════════════════════════════════
# Phase 3: 主循环
# ═══════════════════════════════════════════════════════════

def run(days=5):
    dates, by_date = load_scans(days)
    lu, pct = load_labels(dates)

    total_scans = sum(len(by_date[d]) for d in dates)
    total_stocks = 0
    for d in dates:
        codes = set()
        for fn, _ in by_date[d]:
            with open(WIKI_DIR / fn) as f:
                data = json.load(f)
            for item in (data if isinstance(data, list) else []):
                if isinstance(item, dict) and "code" in item:
                    codes.add(item["code"])
        total_stocks += len(codes)
    print(f"回测: {len(dates)}天 [{dates[0]}->{dates[-1]}], "
          f"{total_scans}轮扫描, ~{total_stocks}只次待评分")
    print(f"标签: {len(lu)}天涨停 + {len(pct)}天涨跌幅\n")

    day_results = []
    all_push_details = []

    for date in dates:
        # — 收集当天唯一股票 —
        daily_codes = set()
        scan_data = []  # [(fn, time_str, codes)]
        for fn, time_str in by_date[date]:
            try:
                with open(WIKI_DIR / fn) as f:
                    data = json.load(f)
                codes = [d["code"] for d in data if isinstance(d, dict) and "code" in d]
                daily_codes.update(codes)
                scan_data.append((fn, time_str, codes))
            except Exception:
                pass
        if not daily_codes: continue

        # — 评分 —
        t0 = time.time()
        score_cache = score_all_stocks(daily_codes, date)
        elapsed = time.time() - t0

        # — jvQuant 日内数据补充 (高分股+按扫描时间截断) —
        enrich_intraday(score_cache, scan_data, date)

        # — 逐轮模拟 —
        nd = (datetime.strptime(date, "%Y%m%d") + timedelta(days=1)).strftime("%Y%m%d")
        next_lu = lu.get(nd, set())
        next_pct = pct.get(nd, {})

        day_pushes = set()
        day_hits = set()
        day_wins = set()
        push_total = 0

        for fn, time_str, codes in scan_data:
            pushed = simulate_push(codes, score_cache, time_str)
            push_total += len(pushed)
            for r in pushed:
                code = r["code"]
                if code not in day_pushes:
                    day_pushes.add(code)
                    if code in next_lu:
                        day_hits.add(code)
                    if next_pct.get(code, 0) > 2:
                        day_wins.add(code)
                    all_push_details.append({
                        "date": date, "scan": fn,
                        "code": code, "total": round(r["total"], 1),
                        "is_hit": code in next_lu,
                        "next_pct": round(next_pct.get(code, 0), 2),
                    })

        day_results.append({
            "date": date, "scans": len(scan_data),
            "unique_stocks": len(daily_codes),
            "push_total": push_total, "push_unique": len(day_pushes),
            "hits": len(day_hits), "wins": len(day_wins),
            "score_time_s": round(elapsed, 0),
        })

        # 打印
        pushed_str = ", ".join(
            "{} ({:.1f})".format(c, score_cache[c]["total"])
            for c in sorted(day_pushes, key=lambda x: score_cache[x]["total"], reverse=True)[:5]
        )
        hit_str = "✓{}".format(len(day_hits)) if day_hits else "✗"
        print(f"  {date}: {len(daily_codes)}股 {len(scan_data)}轮 {elapsed:.0f}s "
              f"推{len(day_pushes)}只 {hit_str}涨停 涨>2%{len(day_wins)}  [{pushed_str}]")

    # — 汇总 —
    total_unique = sum(d["push_unique"] for d in day_results)
    total_hits = sum(d["hits"] for d in day_results)
    total_wins = sum(d["wins"] for d in day_results)
    denom = max(1, total_unique)

    hrs = [d["hits"] / max(1, d["push_unique"]) for d in day_results if d["push_unique"] > 0]
    wrs = [d["wins"] / max(1, d["push_unique"]) for d in day_results if d["push_unique"] > 0]

    summary = {
        "days": len(day_results), "total_scans": total_scans,
        "total_pushes": total_unique, "total_hits": total_hits, "total_wins": total_wins,
        "hit_rate": round(total_hits / denom, 4),
        "win_rate": round(total_wins / denom, 4),
        "avg_hit_rate": round(np.mean(hrs), 4) if hrs else 0,
        "avg_win_rate": round(np.mean(wrs), 4) if wrs else 0,
        "avg_daily_pushes": round(total_unique / len(day_results), 1),
    }

    print(f"\n{'='*55}")
    print(f"汇总: 推送{total_unique}只 涨停{total_hits}({summary['hit_rate']:.1%}) "
          f"涨>2%{total_wins}({summary['win_rate']:.1%})")
    print(f"日均: {summary['avg_daily_pushes']}只 "
          f"命中率{summary['avg_hit_rate']:.1%} 胜率{summary['avg_win_rate']:.1%}")

    # — 维度贡献分析 —
    if all_push_details:
        hit_codes = {d["code"] for d in all_push_details if d["is_hit"]}
        miss_codes = {d["code"] for d in all_push_details if not d["is_hit"]}
        dim_analysis = {}
        for dim in DIM_MAP:
            hit_scores = []
            miss_scores = []
            for date in dates:
                for code in (hit_codes & set(score_cache.keys()) if date == all_push_details[0]["date"] else hit_codes):
                    pass  # need score_cache per day
            dim_analysis[dim] = {"note": "cross-day analysis needs per-day score cache"}
        # Simple: aggregate all push details
        print(f"\n推送明细 (前10):")
        for d in sorted(all_push_details, key=lambda x: x["total"], reverse=True)[:10]:
            tag = "✓HIT" if d["is_hit"] else ("↑" if d["next_pct"] > 2 else "↓")
            print(f"  {d['date']} {d['code']} {d['total']:.1f}分 {d['next_pct']:+.1f}% {tag}")

    report = {
        "config": {"days": days, "weights": WEIGHTS, "scoregap": 0.90},
        "summary": summary,
        "daily": day_results,
        "push_details": all_push_details,
    }
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    outfile = BT_DIR / f"report_{ts}.json"
    with open(outfile, "w") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n报告: {outfile}")
    return report


def main():
    ap = argparse.ArgumentParser(description="回测框架")
    ap.add_argument("--days", type=int, default=5, help="回测天数 (默认5)")
    args = ap.parse_args()
    run(days=args.days)


if __name__ == "__main__":
    main()
