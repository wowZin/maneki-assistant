#!/usr/bin/env python3
"""
统一股票分析 — 五维度评分 + XGBoost模型分 + 盘口深度分析（盘中/盘后）

一次启动、一次数据拉取、一把出所有结果。

用法：
  python3 plays/watchdog/stock_analyzer.py 603906.SH
  python3 plays/watchdog/stock_analyzer.py 603906.SH --json   # JSON 输出
"""

import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_DIR))

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("stock_analyzer")


def _short(code: str) -> str:
    return code.replace(".SH", "").replace(".SZ", "")


def _norm(code: str) -> str:
    if "." in code:
        return code
    return f"{code}.SH" if code.startswith("6") else f"{code}.SZ"


def _code_name(code: str) -> str:
    try:
        from scripts.tu_share import call_tushare
        resp = call_tushare("stock_basic", {"ts_code": code}, "ts_code,name")
        items = resp.get("data", {}).get("items", [])
        if items:
            return items[0][1]
    except Exception:
        pass
    return code


def _is_trading_time() -> bool:
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    h, m = now.hour, now.minute
    if h < 9 or (h == 9 and m < 15):
        return False
    if h >= 15:
        return False
    return True


# ═══════════════════════════════════════════════════════════════
# 五维度评分
# ═══════════════════════════════════════════════════════════════

def _score_5dims(code: str, funcs: dict | None = None) -> tuple[dict, dict]:
    """并行五维度评分。可传预加载的 funcs 避免重复导入。"""
    if funcs is None:
        from plays.limit_up.strategies.fundamental import score_fundamental
        from plays.limit_up.strategies.technical import score_technical
        from plays.limit_up.strategies.fundflow import score_fundflow
        from plays.limit_up.strategies.sentiment import score_sentiment
        from plays.limit_up.strategies.shortterm import score_shortterm
        funcs = {
            "fundamental": score_fundamental,
            "technical": score_technical,
            "fundflow": score_fundflow,
            "sentiment": score_sentiment,
            "shortterm": score_shortterm,
        }
    scores = {}
    reasons = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = {pool.submit(fn, code): dim for dim, fn in funcs.items()}
        for f in as_completed(futs):
            dim = futs[f]
            try:
                s, r = f.result(timeout=30)
                scores[dim] = s
                reasons[dim] = r
            except Exception as e:
                scores[dim] = 0.0
                reasons[dim] = f"异常:{e}"
    return scores, reasons


# ═══════════════════════════════════════════════════════════════
# XGBoost 模型分
# ═══════════════════════════════════════════════════════════════

def _build_feats(code: str) -> dict:
    """从 Tushare 拉数据构建特征。"""
    from scripts.tu_share import call_tushare

    daily_resp = call_tushare(
        "daily", {"ts_code": code, "limit": 120},
        "trade_date,open,high,low,close,pre_close,vol,amount,pct_chg",
    )
    daily_items = daily_resp.get("data", {}).get("items", [])
    daily_fields = daily_resp.get("data", {}).get("fields", [])
    daily_rows = [dict(zip(daily_fields, row)) for row in daily_items]
    daily_rows.sort(key=lambda x: x.get("trade_date", ""))

    basic_resp = call_tushare(
        "daily_basic", {"ts_code": code, "limit": 1},
        "ts_code,trade_date,pe,pb,circ_mv,turnover_rate,volume_ratio",
    )
    basic_items = basic_resp.get("data", {}).get("items", [])
    basic_fields = basic_resp.get("data", {}).get("fields", [])
    basic_by_date = {}
    if basic_items:
        row = dict(zip(basic_fields, basic_items[0]))
        basic_by_date[row.get("trade_date", "")] = row

    from plays.limit_up.pit_features import build_pit_features
    today_str = datetime.now().strftime("%Y%m%d")
    return build_pit_features(
        code=code,
        score_date=today_str,
        daily_rows=daily_rows,
        basic_by_date=basic_by_date,
    )


def _model_score(feats: dict, scores: dict) -> float:
    """计算 XGBoost 模型分。"""
    from plays.limit_up.factors.optimized.model_score import factor_model_score
    feats = dict(feats)
    feats["fundamental"] = scores.get("fundamental", 0)
    feats["technical"] = scores.get("technical", 0)
    feats["fundflow"] = scores.get("fundflow", 0)
    feats["sentiment"] = scores.get("sentiment", 0)
    feats["shortterm"] = scores.get("shortterm", 0)
    return round(float(factor_model_score(feats)), 2)


# ═══════════════════════════════════════════════════════════════
# 盘口深度分析（盘中 L10 / 盘后 SQL）
# ═══════════════════════════════════════════════════════════════

def _pan_realtime(code: str) -> dict:
    """盘中盘口分析。"""
    from scripts.jvquant_ws_client import _get_ws

    short = _short(code)
    ws = _get_ws()
    ws.subscribe_l10([short])
    ws.subscribe_l2([short])
    time.sleep(2.5)

    market = ws.get_market(code)
    vwap = ws.get_vwap(code)
    bid_ask_ratio = ws.get_bid_ask_ratio(code)
    ws.unsubscribe_l10([short])
    ws.unsubscribe_l2([short])

    if not market:
        return {"error": "无法获取盘口数据"}

    last = float(market.get("last", 0))
    pre_close = float(market.get("pre_close", 0))
    pct = ((last / pre_close - 1) * 100) if pre_close > 0 else 0

    bid_prices = [float(market.get(f"b{i}p", 0)) for i in range(1, 11)]
    ask_prices = [float(market.get(f"s{i}p", 0)) for i in range(1, 11)]
    bid_qtys = [float(market.get(f"b{i}", 0)) for i in range(1, 11)]
    ask_qtys = [float(market.get(f"s{i}", 0)) for i in range(1, 11)]

    bid_total = sum(bid_qtys)
    ask_total = sum(ask_qtys)
    depth_ratio = (ask_total / bid_total) if bid_total > 0 else 99.0

    near_ask = sum(q for p, q in zip(ask_prices, ask_qtys) if p > 0 and p <= last * 1.01)
    mid_ask = sum(q for p, q in zip(ask_prices, ask_qtys) if p > 0 and p <= last * 1.02)
    far_ask = sum(q for p, q in zip(ask_prices, ask_qtys) if p > 0 and p <= last * 1.03)

    vs_vwap = ((last / vwap - 1) * 100) if vwap and vwap > 0 else 0
    signals = []
    score = 0

    if depth_ratio >= 3.0:
        signals.append(f"卖盘比买盘高{depth_ratio:.1f}倍")
        score += 20
    elif depth_ratio >= 2.0:
        signals.append(f"卖盘偏高({depth_ratio:.1f}:1)")
        score += 10

    if near_ask > 0 and mid_ask > near_ask * 3:
        signals.append(f"近档卖量{int(near_ask)}手，再上一档{int(mid_ask-near_ask)}手")
        score += 15

    if vs_vwap > 2.0 and depth_ratio > 2.0:
        signals.append(f"价高VWAP({vs_vwap:+.1f}%)卖压重")
        score += 15
    elif vs_vwap < -2.0 and depth_ratio < 0.5:
        signals.append(f"价低VWAP({vs_vwap:.1f}%)买盘强")

    if score >= 30:
        verdict, advice = "❌ 诱多信号强", "建议观望"
    elif score >= 15:
        verdict, advice = "⚠️ 有诱多风险", "谨慎"
    elif score <= -15:
        verdict, advice = "🛟 疑似诱空", "低位有承接"
    else:
        verdict, advice = "✅ 盘口正常", "无明显异常"

    return {
        "mode": "🟢 盘中实时",
        "last": round(last, 2),
        "pct_chg": round(pct, 2),
        "vwap": round(vwap, 2) if vwap else 0,
        "vs_vwap": round(vs_vwap, 2),
        "depth": f"买{int(bid_total)}手 vs 卖{int(ask_total)}手 ({depth_ratio:.1f}:1)",
        "resistance": f"近{int(near_ask)}中{int(mid_ask-near_ask)}远{int(far_ask-mid_ask)}手",
        "signals": signals,
        "verdict": verdict,
        "advice": advice,
    }


def _pan_afterhours(code: str) -> dict:
    """盘后盘口分析。"""
    from scripts.jvquant_client import get_jvquant_client

    client = get_jvquant_client()
    short = _short(code)

    kl = client.get_kline(short, freq="day", count=10)
    if not kl:
        return {"error": "无数据"}
    last_k = kl[-1]
    close = float(last_k.get("close", 0))
    pct = float(last_k.get("pct_chg", 0))

    pcts = [float(k.get("pct_chg", 0)) for k in kl]
    avg_pct_3d = sum(pcts[-3:]) / 3 if len(pcts) >= 3 else pct
    trend = "up" if avg_pct_3d > 0 else "down"

    orders = client.get_order_book(short, offset=0)
    near_ratio = 1.0
    net_order = 0
    if orders:
        buy_total = sum(o.get("volume", 0) for o in orders if o.get("type") == "B")
        sell_total = sum(o.get("volume", 0) for o in orders if o.get("type") == "S")
        net_order = buy_total - sell_total

    try:
        md = client.get_minute_data(short, datetime.now().strftime("%Y-%m-%d"), 1)
        tail_bars = []
        if md and md.get("series"):
            bars = md["series"][0].get("bars", [])
            tail_bars = [b for b in bars if b[0] >= "14:30"]
    except Exception:
        tail_bars = []

    signals = []
    score = 0

    if near_ratio < 0.5:
        signals.append(f"近价卖盘主导(买/卖={near_ratio:.2f})")
        score += 15
    elif near_ratio > 2:
        signals.append(f"近价买盘强(买/卖={near_ratio:.2f})")
        score -= 15

    if tail_bars:
        tail_chg = ((tail_bars[-1][1] / tail_bars[0][1] - 1) * 100) if len(tail_bars) >= 2 else 0
        if tail_chg > 1.5:
            signals.append(f"尾盘拉升{tail_chg:+.1f}%")
            score += 10
        elif tail_chg < -1.5:
            signals.append(f"尾盘跳水{tail_chg:.1f}%")
            score += 15

    if trend == "up" and avg_pct_3d > 3:
        signals.append(f"3日涨{avg_pct_3d:+.1f}%过热")
        score += 10

    # ── 龙虎榜对比：今日买入 vs 今日流出 ──
    try:
        from scripts.tu_share import call_tushare
        from datetime import timedelta

        # 查今日龙虎榜（call_tushare 自动修正非交易日）
        inst = call_tushare("top_inst", {"trade_date": datetime.now().strftime("%Y%m%d"), "ts_code": code},
                           "exalter,buy,sell,net_buy")
        inst_items = inst.get("data", {}).get("items", [])
        lb_date = datetime.now().strftime("%Y%m%d")
        if not inst_items:
            # 今日没上榜 → 试上一交易日（传昨天日期，call_tushare 自动修正）
            lb_date = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
            inst = call_tushare("top_inst", {"trade_date": lb_date, "ts_code": code},
                               "exalter,buy,sell,net_buy")
            inst_items = inst.get("data", {}).get("items", [])

        if inst_items:
            total_net = sum(float(x[3] or 0) for x in inst_items)
            today_ff = client.get_fundflow_single(short, datetime.now().strftime("%Y-%m-%d"))
            main_net = float(today_ff.get("main_net", 0) or 0) if today_ff else 0
            lbl = f"{lb_date}" if lb_date == datetime.now().strftime("%Y%m%d") else f"最近({lb_date})"
            if total_net / 10000 > 1000 and main_net < -1000:
                signals.append(f"{lbl}龙虎榜净买{total_net/10000:.0f}万，今日主力净卖{abs(main_net):.0f}万 — 拉高出货")
                score += 20
            elif total_net / 10000 > 1000 and main_net > 0:
                signals.append(f"{lbl}龙虎榜净买{total_net/10000:.0f}万，今日主力继续买入")
                score -= 10
    except Exception:
        pass

    if score >= 25:
        verdict, advice = "❌ 不建议追", "卖压重或过热"
    elif score >= 12:
        verdict, advice = "⚠️ 谨慎", "有压力"
    elif score <= -12:
        verdict, advice = "🛟 有承接", "买盘强可关注"
    else:
        verdict, advice = "✅ 均衡", "无明显异常"

    return {
        "mode": "🔵 盘后历史",
        "last": round(close, 2),
        "pct_chg": round(pct, 2),
        "trend_3d": round(avg_pct_3d, 2),
        "net_order": int(net_order),
        "near_ratio": round(near_ratio, 2) if near_ratio else 1.0,
        "signals": signals,
        "verdict": verdict,
        "advice": advice,
    }


# ═══════════════════════════════════════════════════════════════
# 统一入口
# ═══════════════════════════════════════════════════════════════

def analyze(code: str) -> dict:
    """统一分析：评分 + 模型分 + 盘口。一次调用全部出。"""
    code = _norm(code)
    name = _code_name(code)

    # Step 0: 先拉 Tushare 日线数据（供 factor_ctx 初始化 + 后面模型复用）
    try:
        from scripts.tu_share import call_tushare
        daily_resp = call_tushare(
            "daily", {"ts_code": code, "limit": 120},
            "trade_date,open,high,low,close,pre_close,vol,amount,pct_chg",
        )
        daily_items = daily_resp.get("data", {}).get("items", [])
        daily_fields = daily_resp.get("data", {}).get("fields", [])
        _daily_rows = [dict(zip(daily_fields, row)) for row in daily_items]
        _daily_rows.sort(key=lambda x: x.get("trade_date", ""))

        basic_resp = call_tushare(
            "daily_basic", {"ts_code": code, "limit": 1},
            "ts_code,trade_date,pe,pb,circ_mv,turnover_rate,volume_ratio",
        )
        basic_items = basic_resp.get("data", {}).get("items", [])
        basic_fields = basic_resp.get("data", {}).get("fields", [])
        _basic_by_date = {}
        if basic_items:
            row = dict(zip(basic_fields, basic_items[0]))
            _basic_by_date[row.get("trade_date", "")] = row

        # 初始化 factor_ctx（供 scoring 使用）
        from plays.limit_up.strategies import factor_ctx
        factor_ctx.set_daily(code, _daily_rows)
        factor_ctx.set_daily_basic(code, _basic_by_date)
        # 涨停基因
        pcts = [float(r.get("pct_chg", 0)) for r in _daily_rows]
        limit_20d = sum(1 for p in pcts[-20:] if p >= 9.8)
        limit_60d = sum(1 for p in pcts[-60:] if p >= 9.8)
        factor_ctx.set_limit_counts(code, limit_20d, limit_60d)
        if factor_ctx._CONCEPT_DAILY_CACHE is None:
            factor_ctx.load_concept_data_from_cache()
    except Exception as e:
        logger.warning("factor_ctx 初始化失败: %s", e)
        _daily_rows = []
        _basic_by_date = {}

    # Step 1: 五维度评分（用已加载的函数，不重导入）
    from plays.limit_up.strategies.fundamental import score_fundamental
    from plays.limit_up.strategies.technical import score_technical
    from plays.limit_up.strategies.fundflow import score_fundflow
    from plays.limit_up.strategies.sentiment import score_sentiment
    from plays.limit_up.strategies.shortterm import score_shortterm
    _score_funcs = {
            "fundamental": score_fundamental,
            "technical": score_technical,
            "fundflow": score_fundflow,
            "sentiment": score_sentiment,
            "shortterm": score_shortterm,
        }

    # Step 1: 五维度评分（用已加载的函数，不重导入）
    scores, reasons = _score_5dims(code, _score_funcs)

    # Step 2: XGBoost 模型分（复用已拉的 Tushare 数据）
    try:
        feats = _build_feats(code)
        total = _model_score(feats, scores)
    except Exception as e:
        logger.warning(f"模型分失败: {e}")
        total = 0.0

    # Step 3: 盘口分析
    if _is_trading_time():
        pan = _pan_realtime(code)
    else:
        pan = _pan_afterhours(code)

    return {
        "code": code,
        "name": name,
        "scores": scores,
        "reasons": {k: v.split(";")[0] if v else "" for k, v in reasons.items()},
        "model_score": total,
        "pan": pan,
    }


def format_result(result: dict) -> str:
    """格式化为可读文本。"""
    labels = {
        "fundamental": "基本面", "technical": "技术面",
        "fundflow": "资金面", "sentiment": "情绪面", "shortterm": "短线博弈",
    }
    ms = result["model_score"]
    star = "⭐⭐⭐⭐⭐" if ms >= 55 else "⭐⭐⭐⭐" if ms >= 45 else "⭐⭐⭐" if ms >= 35 else ""

    lines = [
        f"📊 {result['name']}({result['code']})",
        f"综合评级 {star} ({ms}分)" if star else f"综合评分 {ms}分",
    ]
    for dim, label in labels.items():
        s = result["scores"].get(dim, 0)
        r = result["reasons"].get(dim, "")
        lines.append(f"{label} {s:.0f}分 — {r}" if r else f"{label} {s:.0f}分")

    pan = result.get("pan", {})
    if "error" not in pan:
        lines.append("")
        lines.append(f"📊 盘口分析（{pan.get('mode','')}）")
        if pan.get("vwap") is not None:
            lines.append(f"现价{pan['last']} VWAP{pan['vwap']} 距{pan.get('vs_vwap',0):+.1f}%")
        if pan.get("depth"):
            lines.append(f"盘口: {pan['depth']}")
        if pan.get("resistance"):
            lines.append(f"阻力: {pan['resistance']}")
        if pan.get("trend_3d") is not None:
            lines.append(f"3日趋势{pan['trend_3d']:+.1f}% 逐笔净{'买' if pan.get('net_order',0)>0 else '卖'}{abs(pan.get('net_order',0))}手")
        for s in pan.get("signals", []):
            lines.append(f"  📌 {s}")
        lines.append("")
        lines.append(f"{pan['verdict']} — {pan['advice']}")

    return "\n".join(lines)


def main():
    import argparse
    from contextlib import redirect_stdout

    # 抑制三方导入的脏输出
    with open(os.devnull, 'w') as devnull:
        with redirect_stdout(devnull):
            parser = argparse.ArgumentParser(description="统一股票分析")
            parser.add_argument("code", nargs="?", help="股票代码，如 603906.SH")
            parser.add_argument("--json", action="store_true", help="JSON 输出")
            parser.add_argument("--query", help="股票名称或自然语言查询，如'兴业科技'或'追不追浪潮信息'")
            args = parser.parse_args()

            # 确定股票代码
            code = args.code
            if not code and args.query:
                # 从 query 中提取股票代码或名称
                import re
                codes = re.findall(r'\b(\d{6})\b', args.query)
                if codes:
                    short = codes[0]
                    code = f"{short}.SH" if short.startswith("6") else f"{short}.SZ"
                else:
                    # 用 Tushare 查股票名
                    from scripts.tu_share import call_tushare
                    # 提取中文名称
                    names = re.findall(r'[\u4e00-\u9fff]{2,4}', args.query)
                    for name in names:
                        if name in ("分析", "看看", "点评", "怎么样", "如何", "值得",
                                    "明天", "今天", "一下", "股票", "风险", "可以",
                                    "不能", "能不能", "推荐"):
                            continue
                        resp = call_tushare("stock_basic", {"name": name}, "ts_code,name")
                        items = resp.get("data", {}).get("items", [])
                        if items:
                            code = items[0][0]
                            break

            if not code:
                print(json.dumps({"error": "无法识别股票"}))
                return

            result = analyze(code)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(format_result(result))


if __name__ == "__main__":
    main()
