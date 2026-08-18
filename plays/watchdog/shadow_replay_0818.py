#!/usr/bin/env python3
"""影子回放：新组合离场机制（高位出场+固定止损+回撤2%）在 0818 隔夜仓上
按 watchdog 轮询节奏模拟，对比实际卖出。验证体系第 3 层（真实函数验证）。"""
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, "/root/maneki-agent")
from plays.watchdog.confirm import check_sell_confirm
from scripts.ths_client import get_ths_client

TRADES_0817 = "/root/maneki-agent/plays/trading/data/reports/20260817.json"
TRADES_0818 = "/root/maneki-agent/plays/trading/data/reports/20260818.json"

YEST_BUYS = [
    ("金时科技", "002951.SZ", 18.36), ("林平发展", "603284.SH", 45.72),
    ("星网锐捷", "002396.SZ", 33.90), ("合锻智能", "603011.SH", 26.39),
    ("立新能源", "001258.SZ", 14.35), ("北方铜业", "000737.SZ", 14.52),
    ("紫光股份", "000938.SZ", 40.66), ("贤丰控股", "002141.SZ", 6.80),
    ("兴业科技", "002674.SZ", 30.00), ("金牛化工", "600722.SH", 11.91),
    ("惠科股份", "001399.SZ", 25.12), ("长电科技", "600584.SH", 85.66),
    ("至纯科技", "603690.SH", 27.26),
]


def intraday(client, short):
    try:
        r = client.get_index_intraday(short)
        points = (r or {}).get("points", [])
        out = []
        for x in points:
            if len(x) < 2:
                continue
            hhmm = int(x[0])
            out.append((f"{hhmm // 100:02d}:{hhmm % 100:02d}:00", float(x[1])))
        return out
    except Exception:
        return []


def replay(px, buy, prev_close):
    """模拟 watchdog 每轮调 check_sell_confirm（60s 采样 ≈ 分时）。"""
    hi = buy
    prev_last = buy
    pc = 0
    for t, p in px:
        hi = max(hi, p)
        now = datetime.strptime(f"2026-08-18 {t}", "%Y-%m-%d %H:%M:%S")
        ok, reason, pc = check_sell_confirm(
            hi, p, prev_last, pc,
            entry_price=buy, prev_close=prev_close, is_overnight=True, now=now)
        prev_last = p
        if ok:
            return t, p, reason
    return None, None, None


def main():
    actual = {}
    for t in json.loads(Path(TRADES_0818).read_text()):
        if t.get("direction") == "卖出":
            actual[t["code"]] = float(t["price"])
    prev_close = {}
    for t in json.loads(Path(TRADES_0817).read_text()):
        if t.get("direction") == "买入":
            pass
    # 昨收 ≈ 买入价所在日收盘：用 0817 收盘（tushare 拉过 close_20260817）
    closes = json.load(open("/tmp/close_20260817.json"))

    client = get_ths_client()
    print(f"{'股票':8s} {'买入':>6s} {'昨收':>6s} | {'新机制卖出':>16s} | {'实际卖出':>8s} {'差':>6s}")
    print("-" * 70)
    tot = 0.0
    for name, code, buy in YEST_BUYS:
        short = code.split(".")[0]
        px = intraday(client, short)
        cl = 0.0
        _c = closes.get(code)
        if _c:
            cl = float(_c[0]) or buy
        else:
            cl = buy
        if not px:
            print(f"{name:8s} 分时拉取失败")
            continue
        t, p, r = replay(px, buy, float(cl))
        act = actual.get(code)
        if t and act:
            diff = (p - act) * 200
            tot += diff
            print(f"{name:8s} {buy:6.2f} {cl:6.2f} | {t}@{p:.2f} {r[:18]:18s} | {act:8.2f} {diff:+6.0f}元")
        elif t:
            print(f"{name:8s} {buy:6.2f} {cl:6.2f} | {t}@{p:.2f} {r[:18]:18s} | 未卖(实际)")
        else:
            print(f"{name:8s} {buy:6.2f} {cl:6.2f} | 未触发            | {act or '未卖'}")
    print(f"\n已卖出对比合计: {tot:+.0f} 元（新机制 vs 实际，13 只隔夜仓）")


if __name__ == "__main__":
    main()
