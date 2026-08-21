#!/usr/bin/env python3
"""回测：回调低吸策略（方向2）vs 当前确认器（追拉升）。

回调低吸逻辑：
1. 强势票：当日从昨收涨幅 ≥ STRONG_PCT（有资金拉升过）
2. 回调：当前价从当日高点回落 ≥ PULLBACK%（回调到支撑）
3. 企稳：回调后连续 STABLE 轮不创新低 → 买入
评估：买入后 30min/到收盘 收益 + 次日开盘收益
"""
import pandas as pd
from pathlib import Path

DAYS = ["20260814", "20260817", "20260818", "20260819", "20260820"]
SP = Path("plays/limit_up/data/snapshot_log")

def backtest(strong_pct, pullback, stable, hold_min):
    """返回 (笔数, 30min均收益, 胜率, 到收盘均, 次日开盘均)"""
    total = []
    for d in DAYS:
        df = pd.read_parquet(SP / f"{d}.parquet")
        df["ts"] = df["ts"].astype(str)
        df["price"] = df["price"].astype(float)
        df["pct"] = df["pct_chg"].astype(float)
        for code, g in df.groupby("code"):
            g = g.sort_values("ts").reset_index(drop=True)
            prices = g["price"].tolist()
            times = g["ts"].tolist()
            if len(prices) < 20:
                continue
            prev_close = prices[0] / (1 + g["pct"].iloc[0] / 100) if g["pct"].iloc[0] > -99 else prices[0]
            hi_sofar = 0.0
            lo_since_peak = 0.0
            # 状态: 拉升过 -> 回调 -> 企稳
            triggered = False
            for i in range(1, len(prices)):
                last = prices[i]
                hi_sofar = max(hi_sofar, last)
                up_from_prev = (hi_sofar / prev_close - 1) * 100 if prev_close else 0
                if up_from_prev < strong_pct:
                    continue  # 还没拉升过（强势条件不满足）
                # 从高点回落幅度
                drop = (last / hi_sofar - 1) * 100
                if drop <= -pullback:
                    # 进入回调区：记录回调段最低点，等待企稳
                    if lo_since_peak == 0.0:
                        lo_since_peak = last
                    else:
                        lo_since_peak = min(lo_since_peak, last)
                    # 企稳: 从回调最低点反弹 ≥ STABLE 轮持续不破低
                    rebound = (last / lo_since_peak - 1) * 100
                    if rebound >= 0.5 and i + hold_min < len(prices) and not triggered:
                        triggered = True
                        p0 = last
                        p30 = prices[min(i + hold_min, len(prices) - 1)]
                        p_close = prices[-1]
                        total.append({"day": d, "code": code, "p0": p0,
                                      "p30": (p30 / p0 - 1) * 100,
                                      "close": (p_close / p0 - 1) * 100,
                                      "prev": prev_close,
                                      "hi": hi_sofar})
                        break
    return total

for strong, pull, stable, hold in [(3, 2, 0, 30), (3, 3, 0, 30), (4, 3, 0, 30),
                                   (3, 2, 2, 30), (3, 3, 2, 30), (5, 4, 2, 30)]:
    r = backtest(strong, pull, stable, hold)
    if not r:
        print(f"strong{strong}% pull{pull}% hold{hold}min: 无样本")
        continue
    n = len(r)
    p30 = sum(x["p30"] for x in r) / n
    close = sum(x["close"] for x in r) / n
    win = sum(1 for x in r if x["p30"] > 0) / n * 100
    print(f"strong{strong}% 回调{pull}% hold{hold}min: {n}笔 "
          f"30min均{p30:+.2f}% 胜率{win:.0f}% 到收盘{close:+.2f}%")
