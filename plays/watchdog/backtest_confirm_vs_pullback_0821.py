#!/usr/bin/env python3
"""对比回测：新回调低吸确认器 vs 旧追拉升确认器（0814-0820 snapshot 全量）"""
import sys
import pandas as pd
from pathlib import Path

sys.path.insert(0, ".")
from plays.watchdog import confirm as cf

DAYS = ["20260814", "20260817", "20260818", "20260819", "20260820"]
SP = Path("plays/limit_up/data/snapshot_log")

def simulate(use_pullback):
    """模拟确认器状态机：watching 票每轮喂价格/量，trigger→stand→ready 买入。
    返回 [(买入价, 30min后价, 收盘价, day, code)]"""
    out = []
    for d in DAYS:
        df = pd.read_parquet(SP / f"{d}.parquet")
        df["ts"] = df["ts"].astype(str)
        for code, g in df.groupby("code"):
            g = g.sort_values("ts").reset_index(drop=True)
            prices = g["price"].astype(float).tolist()
            vols = g["vol_ratio"].astype(float).fillna(0).tolist()
            times = g["ts"].tolist()
            if len(prices) < 20:
                continue
            prev_close = prices[0] / (1 + g["pct_chg"].astype(float).iloc[0] / 100) if g["pct_chg"].astype(float).iloc[0] > -99 else prices[0]
            day_high = 0.0
            hist, vhist = [], []
            base, cnt = 0.0, 0
            bought = False
            for i in range(len(prices)):
                p = prices[i]
                day_high = max(day_high, p)
                hist.append(p); vhist.append(vols[i])
                if len(hist) > 15: hist = hist[-15:]
                if len(vhist) > 15: vhist = vhist[-15:]
                if bought:
                    continue
                if use_pullback:
                    action, b, c = cf.check_buy_confirm(hist, vhist, base, cnt, day_high, prev_close)
                else:
                    action, b, c = cf.check_buy_confirm(hist, vhist, base, cnt)
                if action == "trigger":
                    base, cnt = b, 0
                elif action == "stand":
                    cnt = c
                elif action == "ready":
                    # 买入点 = 当前价
                    p30 = prices[min(i + 30, len(prices) - 1)]
                    p_close = prices[-1]
                    out.append({"day": d, "code": code, "p0": p,
                                "p30": (p30 / p - 1) * 100,
                                "close": (p_close / p - 1) * 100})
                    bought = True
                elif action == "reset":
                    base, cnt = 0.0, 0
    return out

for name, pb in [("旧:追拉升", False), ("新:回调低吸", True)]:
    r = simulate(pb)
    if not r:
        print(f"{name}: 无触发")
        continue
    n = len(r)
    p30 = sum(x["p30"] for x in r) / n
    close = sum(x["close"] for x in r) / n
    win = sum(1 for x in r if x["p30"] > 0) / n * 100
    win_c = sum(1 for x in r if x["close"] > 0) / n * 100
    print(f"{name}: {n}笔 30min均{p30:+.2f}% 胜率{win:.0f}% | 到收盘{close:+.2f}% 胜率{win_c:.0f}%")
    for d in DAYS:
        sub = [x for x in r if x["day"] == d]
        if sub:
            print(f"    {d}: {len(sub)}笔 30min{sum(x['p30'] for x in sub)/len(sub):+.2f}%")
