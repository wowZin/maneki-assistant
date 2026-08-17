#!/usr/bin/env python3
"""
盘口深度分析 — 诱多/诱空检测工具

盘中用 jvQuant WS L10 实时盘口，盘后用 jvQuant SQL 历史盘口。
用于"追吗"这类买入前决策场景。

用法：
  python3 plays/watchdog/pan_analyzer.py 603906.SH        # 实时分析
  python3 plays/watchdog/pan_analyzer.py 603906.SH --after-hours  # 强制盘后模式
"""

import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_DIR))

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("pan_analyzer")


def _short(code: str) -> str:
    return code.replace(".SH", "").replace(".SZ", "")


def _norm(code: str) -> str:
    if "." in code:
        return code
    return f"{code}.SH" if code.startswith("6") else f"{code}.SZ"


def _is_trading_time() -> bool:
    """是否交易时段（含午休，WS 缓存可用）。"""
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    h, m = now.hour, now.minute
    # 包含: 集合竞价(9:15) ~ 收盘(15:00)，午休也包含（WS缓存有上午数据）
    if h < 9 or (h == 9 and m < 15):
        return False
    if h >= 15:
        return False
    return True


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


# ═══════════════════════════════════════════════════════════════
# 盘中分析（jvQuant WS L10）
# ═══════════════════════════════════════════════════════════════

def _analyze_realtime(code: str) -> dict:
    """盘中实时盘口分析：订阅 L10 → 读数 → 退订。

    2026-08-17：ws_daemon 在跑时读共享内存 L1（不建连防互踢）；
    ws_daemon 不在才临时建连（无主连接可踢）。
    """
    from scripts.jvquant_ws_client import daemon_alive, daemon_get_market, daemon_get_vwap

    short = _short(code)
    if daemon_alive():
        market = daemon_get_market(code)
        vwap = daemon_get_vwap(code)
        bid_ask_ratio = 0.0
        if market:
            try:
                bp = [float(x or 0) for x in (market.get("bid_price") or [0] * 10)]
                ap = [float(x or 0) for x in (market.get("ask_price") or [0] * 10)]
                bid_ask_ratio = bp[0] / ap[0] if ap and ap[0] > 0 else 1.0
            except Exception:
                pass
    else:
        from scripts.jvquant_ws_client import _get_ws
        ws = _get_ws()

        # 订阅 L10 + L2（逐笔）
        ws.subscribe_l10([short])
        ws.subscribe_l2([short])
        time.sleep(2.5)  # 等数据到位

        market = ws.get_market(code)
        vwap = ws.get_vwap(code)
        bid_ask_ratio = ws.get_bid_ask_ratio(code)

        # 退订
        ws.unsubscribe_l10([short])
        ws.unsubscribe_l2([short])

    if not market:
        return {"error": "无法获取盘口数据，请确认代码正确且盘中时段"}

    last = float(market.get("last", 0))
    pre_close = float(market.get("pre_close", 0))
    pct = ((last / pre_close - 1) * 100) if pre_close > 0 else 0

    # 解析10档盘口
    bid_prices = [float(market.get(f"bid_price[{i}]", market.get(f"b{i}p", 0))) for i in range(10)]
    bid_qtys = [float(market.get(f"bid_qty[{i}]", market.get(f"b{i}", 0))) for i in range(10)]
    ask_prices = [float(market.get(f"ask_price[{i}]", market.get(f"s{i}p", 0))) for i in range(10)]
    ask_qtys = [float(market.get(f"ask_qty[{i}]", market.get(f"s{i}", 0))) for i in range(10)]

    # 兼容：低版本可能只有bid_price[0]即买一
    bid_prices = [p for p in bid_prices if p > 0]
    ask_prices = [p for p in ask_prices if p > 0]
    bid_qtys = bid_qtys[:len(bid_prices)]
    ask_qtys = ask_qtys[:len(ask_prices)]

    bid_total = sum(bid_qtys)
    ask_total = sum(ask_qtys)
    depth_ratio = (ask_total / bid_total) if bid_total > 0 else 99.0

    # 近档阻力：当前价往上1%、2%、3%位置累计卖量
    near_ask = sum(
        q for p, q in zip(ask_prices, ask_qtys)
        if p <= last * 1.01
    )
    mid_ask = sum(
        q for p, q in zip(ask_prices, ask_qtys)
        if p <= last * 1.02
    )
    far_ask = sum(
        q for p, q in zip(ask_prices, ask_qtys)
        if p <= last * 1.03
    )

    # 判断
    trap_signals = []
    score = 0  # 正=诱多风险高，负=诱空风险高

    if depth_ratio >= 3.0:
        trap_signals.append(f"卖盘比买盘高{depth_ratio:.1f}倍")
        score += 20
    elif depth_ratio >= 2.0:
        trap_signals.append(f"卖盘偏高({depth_ratio:.1f}:1)")
        score += 10

    if near_ask > 0 and mid_ask > near_ask * 3:
        trap_signals.append(f"近档({last*1.01:.2f})卖量{int(near_ask)}手，再上一档{int(mid_ask-near_ask)}手 — 突破后有阻力")
        score += 15

    if bid_ask_ratio < 0.5:
        trap_signals.append(f"买一/卖一比={bid_ask_ratio:.2f}，买盘薄弱")
        score += 10

    # VWAP 分析
    vs_vwap = ((last / vwap - 1) * 100) if vwap and vwap > 0 else 0
    if vs_vwap > 2.0 and depth_ratio > 2.0:
        trap_signals.append(f"价格高于VWAP({vs_vwap:+.1f}%)但卖压重 — 诱多可能")
        score += 15
    elif vs_vwap < -2.0 and depth_ratio < 0.5:
        trap_signals.append(f"价格低于VWAP({vs_vwap:.1f}%)但买盘强 — 诱空可能")

    # 结论
    if score >= 30:
        verdict = "❌ 诱多信号强"
        advice = "建议观望"
    elif score >= 15:
        verdict = "⚠️ 有诱多风险"
        advice = "谨慎，观察买盘能否消化卖压"
    elif score <= -15:
        verdict = "🛟 疑似诱空"
        advice = "低位有承接，可关注"
    else:
        verdict = "✅ 盘口正常"
        advice = "无明显诱多/诱空信号"

    return {
        "code": code,
        "last": last,
        "pct_chg": round(pct, 2),
        "vwap": round(vwap, 2) if vwap else 0,
        "vs_vwap": round(vs_vwap, 2),
        "depth_ratio": round(depth_ratio, 2),
        "bid_total": int(bid_total),
        "ask_total": int(ask_total),
        "resistance_near": int(near_ask),
        "resistance_mid": int(mid_ask - near_ask),
        "resistance_far": int(far_ask - mid_ask),
        "bid_ask_ratio": round(bid_ask_ratio, 2),
        "signals": trap_signals,
        "verdict": verdict,
        "advice": advice,
        "mode": "realtime",
    }


# ═══════════════════════════════════════════════════════════════
# 盘后分析（jvQuant SQL 历史10档）
# ═══════════════════════════════════════════════════════════════

def _analyze_after_hours(code: str) -> dict:
    """盘后分析：用 jvQuant SQL 逐笔+K线。"""
    from scripts.jvquant_client import get_jvquant_client

    client = get_jvquant_client()
    short = _short(code)

    # K线数据
    kl = client.get_kline(short, freq="day", count=10)
    if not kl:
        return {"error": "无数据"}
    last_k = kl[-1]
    close = float(last_k.get("close", 0))
    pct = float(last_k.get("pct_chg", 0))
    turnover = float(last_k.get("turnover_rate", 0))
    amount = float(last_k.get("amount", 0))

    # 近期趋势
    closes = [float(k.get("close", 0)) for k in kl]
    pcts = [float(k.get("pct_chg", 0)) for k in kl]
    avg_pct_3d = sum(pcts[-3:]) / 3 if len(pcts) >= 3 else pct
    trend = "up" if len(closes) >= 2 and closes[-1] > closes[-3] else "down"

    # 逐笔分析（最后200笔）
    orders = client.get_order_book(short, offset=0)
    near_buy = 0
    near_sell = 0
    if orders:
        buy_total = sum(o.get("volume", 0) for o in orders if o.get("type") == "B")
        sell_total = sum(o.get("volume", 0) for o in orders if o.get("type") == "S")
        near_price = close
        # 近价成交（收盘价±0.5%范围内）
        near_buy = sum(o.get("volume", 0) for o in orders
                       if o.get("type") == "B" and abs(o.get("price", 0) - near_price) / near_price < 0.005)
        near_sell = sum(o.get("volume", 0) for o in orders
                        if o.get("type") == "S" and abs(o.get("price", 0) - near_price) / near_price < 0.005)
        near_ratio = (near_buy / near_sell) if near_sell > 0 else 99
        net_order = buy_total - sell_total
    else:
        near_ratio = 1.0
        net_order = 0

    # 分钟数据：尾盘30分钟买卖强度
    try:
        md = client.get_minute_data(short, "2026-07-08", 1)
        tail_bars = []
        if md and md.get("series"):
            bars = md["series"][0].get("bars", [])
            tail_bars = [b for b in bars if b[0] >= "14:30"]
    except Exception:
        tail_bars = []

    trap_signals = []
    score = 0

    # 近价成交比
    if near_ratio < 0.5 and near_sell > 0:
        trap_signals.append(f"收盘附近卖盘主导(买/卖={near_ratio:.2f})")
        score += 15
    elif near_ratio > 2.0 and near_buy > 0:
        trap_signals.append(f"收盘附近买盘强(买/卖={near_ratio:.2f})")
        score -= 15

    # 尾盘异动
    if tail_bars:
        tail_vol = sum(b[3] for b in tail_bars if len(b) >= 4)
        tail_chg = ((tail_bars[-1][1] / tail_bars[0][1] - 1) * 100) if tail_bars else 0
        if tail_chg > 1.5 and tail_vol > 0:
            trap_signals.append(f"尾盘拉升{tail_chg:+.1f}%放量")
            score += 10
        elif tail_chg < -1.5 and tail_vol > 0:
            trap_signals.append(f"尾盘跳水{tail_chg:.1f}%")
            score += 15

    # 短线过热
    if trend == "up" and avg_pct_3d > 3:
        trap_signals.append(f"近3日平均涨幅{avg_pct_3d:+.1f}%，短线过热")
        score += 10

    if score >= 25:
        verdict = "❌ 不建议追"
        advice = "卖压重或短线过热"
    elif score >= 12:
        verdict = "⚠️ 谨慎"
        advice = "有压力，等开盘再看实时盘口"
    elif score <= -12:
        verdict = "🛟 有承接"
        advice = "买盘强可关注"
    else:
        verdict = "✅ 均衡"
        advice = "无明显异常"

    return {
        "code": code,
        "last": close,
        "pct_chg": round(pct, 2),
        "turnover": round(turnover, 2),
        "near_trade_ratio": round(near_ratio, 2),
        "net_order": int(net_order),
        "trend_3d": round(avg_pct_3d, 2),
        "signals": trap_signals,
        "verdict": verdict,
        "advice": advice,
        "mode": "after_hours",
    }


# ═══════════════════════════════════════════════════════════════
# 统一入口
# ═══════════════════════════════════════════════════════════════

def analyze(code: str, after_hours: bool = False) -> dict:
    code = _norm(code)
    name = _code_name(code)

    if after_hours or not _is_trading_time():
        result = _analyze_after_hours(code)
    else:
        result = _analyze_realtime(code)

    result["name"] = name
    return result


def format_result(result: dict) -> str:
    """格式化为可读文本。"""
    if "error" in result:
        return f"❌ {result.get('name', '')}({result['code']}): {result['error']}"

    lines = [
        f"📊 {result['name']}({result['code']}) 盘口分析",
        f"现价: {result['last']:.2f} | 涨幅: {result['pct_chg']:+.2f}%",
        f"模式: {'🟢 盘中实时' if result.get('mode') == 'realtime' else '🔵 盘后历史'}",
    ]

    if result.get("mode") == "realtime":
        lines.append(f"VWAP: {result['vwap']:.2f} | 距VWAP: {result.get('vs_vwap',0):+.1f}%")
        lines.append(f"盘口深度: 买{result['bid_total']}手 vs 卖{result['ask_total']}手 ({result['depth_ratio']:.1f}:1)")
        if result.get("resistance_near") is not None:
            lines.append(f"阻力分布: 近{result['resistance_near']}手 | 中{result.get('resistance_mid',0)}手 | 远{result.get('resistance_far',0)}手")
    else:
        lines.append(f"换手率: {result.get('turnover',0):.1f}% | 近价买/卖比: {result.get('near_trade_ratio',1):.2f}")
        lines.append(f"3日趋势: {result.get('trend_3d',0):+.1f}% | 逐笔净: {'买' if result.get('net_order',0) > 0 else '卖'}{abs(result.get('net_order',0))}手")

    if result.get("near_trade_ratio"):
        lines.append(f"近价买卖比: {result['near_trade_ratio']:.2f}")

    if result.get("signals"):
        for s in result["signals"]:
            lines.append(f"  📌 {s}")

    lines.append(f"")
    lines.append(f"{result['verdict']} — {result['advice']}")

    return "\n".join(lines)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="盘口深度分析")
    parser.add_argument("code", help="股票代码，如 603906.SH 或 603906")
    parser.add_argument("--after-hours", action="store_true", help="强制盘后模式")
    args = parser.parse_args()

    result = analyze(args.code, after_hours=args.after_hours)
    print(format_result(result))


if __name__ == "__main__":
    main()
