#!/usr/bin/env python3
"""0818 离场机制回测：昨天买入的票，今天 4 种离场机制 vs 实际卖出。

验证体系第 1 层（上线前回测）——模拟:
  固定止损(入场价-4%) / 移动止损(+3%后最高-3%锁利) / 高位出场(开盘卖) / 回撤2%(现状)
用 THS 今日分时重建走势，对比实际交割单卖出价。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, "/root/maneki-agent")
from scripts.ths_client import get_ths_client

TRADES_0817 = "/root/maneki-agent/plays/trading/data/reports/20260817.json"
TRADES_0818 = "/root/maneki-agent/plays/trading/data/reports/20260818.json"

# 昨天买入（今天可能被卖）：(name, code, buy_price, buy_time)
YEST_BUYS = [
    ("金时科技", "002951.SZ", 18.36), ("林平发展", "603284.SH", 45.72),
    ("星网锐捷", "002396.SZ", 33.90), ("合锻智能", "603011.SH", 26.39),
    ("立新能源", "001258.SZ", 14.35), ("北方铜业", "000737.SZ", 14.52),
    ("紫光股份", "000938.SZ", 40.66), ("贤丰控股", "002141.SZ", 6.80),
    ("兴业科技", "002674.SZ", 30.00), ("金牛化工", "600722.SH", 11.91),
    ("惠科股份", "001399.SZ", 25.12), ("长电科技", "600584.SH", 85.66),
    ("至纯科技", "603690.SH", 27.26),
]


def _today_intraday(client, short: str) -> list[tuple[str, float]]:
    """THS 当日分时 → [(HH:MM:SS, price)]（points: [[930, 18.1], ...]）"""
    try:
        r = client.get_index_intraday(short)
        points = (r or {}).get("points", [])
        out = []
        for x in points:
            if len(x) < 2:
                continue
            hhmm = int(x[0])
            t = f"{hhmm // 100:02d}:{hhmm % 100:02d}:00"
            out.append((t, float(x[1])))
        return out
    except Exception:
        return []


def simulate(px: list[tuple[str, float]], buy: float) -> dict:
    """四种机制在分时序列上的首个触发点。返回 {机制: (触发时间, 卖出价)}"""
    hi = buy  # 持仓最高（含买入价；昨日高点缺省用买入价）
    tripped3 = False  # 是否达到 +3% 盈利（移动止损武装）
    out = {}
    for t, p in px:
        hi = max(hi, p)
        if p >= buy * 1.03:
            tripped3 = True
        # 高位出场（开盘卖）：09:30-09:45 内任意时点主动卖（简化=开盘第一分钟）
        if "高位出场" not in out and t < "09:31:00":
            out["高位出场(开盘卖)"] = (t, p)
        # 固定止损：跌破买入价 -4%
        if "固定止损(-4%)" not in out and p <= buy * 0.96:
            out["固定止损(-4%)"] = (t, p)
        # 移动止损：+3% 后最高回撤 3% 锁利
        if "移动止损(+3%后-3%)" not in out and tripped3 and p <= hi * 0.97:
            out["移动止损(+3%后-3%)"] = (t, p)
        # 回撤 2%（现状）
        if "回撤2%(现状)" not in out and p <= hi * 0.98:
            out["回撤2%(现状)"] = (t, p)
    return out


def main():
    # 实际卖出（0818 交割单）
    actual = {}
    for t in json.loads(Path(TRADES_0818).read_text()):
        if t.get("direction") == "卖出":
            actual[t["code"]] = float(t["price"])

    client = get_ths_client()
    print(f"{'股票':8s} {'买入':>6s} | {'固定止损':>14s} {'移动止损':>18s} {'高位出场':>14s} {'回撤2%(现状)':>14s} | {'实际卖出':>10s}")
    print("-" * 100)
    for name, code, buy, *_ in YEST_BUYS:
        short = code.split(".")[0]
        px = _today_intraday(client, short)
        if not px:
            print(f"{name:8s} 分时拉取失败")
            continue
        sim = simulate(px, buy)
        fmt = lambda k: f"{sim[k][0][:5]}@{sim[k][1]:.2f}" if k in sim else "—"
        act = f"{actual.get(code):.2f}" if code in actual else "未卖"
        print(f"{name:8s} {buy:6.2f} | {fmt('固定止损(-4%)'):>14s} {fmt('移动止损(+3%后-3%)'):>18s} {fmt('高位出场(开盘卖)'):>14s} {fmt('回撤2%(现状)'):>14s} | {act:>10s}")


if __name__ == "__main__":
    main()
