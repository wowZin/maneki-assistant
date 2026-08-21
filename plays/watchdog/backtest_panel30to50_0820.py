#!/usr/bin/env python3
"""回测：主闸 30-50 全部买入（等权满仓）→ 次日开盘卖出，滚动一周。

- 每日 09:30 截面 panel_score ∈ [30,50) 的票全部买入
- 总资金 10 万等权分配（每只 100000/N）
- T+1: 次日 09:30 开盘卖出 → 滚动复投
"""
import pandas as pd
from pathlib import Path

DAYS = ["20260814", "20260817", "20260818", "20260819", "20260820", "20260821"]
SP = Path("plays/limit_up/data/snapshot_log")
CAPITAL = 100000.0

def open_slice(day: str) -> pd.DataFrame:
    df = pd.read_parquet(SP / f"{day}.parquet")
    df["ts"] = df["ts"].astype(str)
    morning = df[df["ts"] <= "09:35:00"]
    if morning.empty:
        return pd.DataFrame(columns=["code", "price", "panel_score"])
    return morning.sort_values("ts").groupby("code").first().reset_index()

# 组合滚动：cash 每天 = 前一天卖出回笼的资金；买入 = 当日 30-50 全买
cash = CAPITAL
total_pnl = 0.0
daily_log = []
for i, day in enumerate(DAYS[:-1]):
    nxt = DAYS[i + 1]
    buy_slice = open_slice(day)
    sell_slice = open_slice(nxt)
    if buy_slice.empty or sell_slice.empty:
        continue
    sell_map = dict(zip(sell_slice["code"], sell_slice["price"]))
    pool = buy_slice[(buy_slice["panel_score"] >= 30) & (buy_slice["panel_score"] < 50)]
    if pool.empty:
        daily_log.append((day, 0, 0.0, 0.0, cash))
        continue
    n = len(pool)
    per = cash / n  # 当日资金等权分配
    day_pnl = 0.0
    wins = 0
    valid = 0
    for _, r in pool.iterrows():
        nxt_price = sell_map.get(r["code"])
        if not nxt_price or nxt_price <= 0:
            continue
        valid += 1
        # 等权: 买 per 元 → 股数 = per/price, 卖出 = 股数×next_price
        shares = per / r["price"]
        proceeds = shares * nxt_price
        pnl = proceeds - per
        day_pnl += pnl
        total_pnl += pnl
        if pnl > 0:
            wins += 1
    cash += day_pnl  # 卖出回笼 + 盈亏
    ret = day_pnl / CAPITAL * 100
    daily_log.append((day, valid, day_pnl, ret, cash))
    print(f"{day}: 买{valid}只(等权) 当日盈亏{day_pnl:+.0f}元({ret:+.2f}%) 胜率{wins/max(valid,1)*100:.0f}% 累计{cash:.0f}")

print(f"\n=== 主闸30-50 全部买入 一周总计 ===")
print(f"期末资金 {cash:.0f} 元 | 总盈亏 {total_pnl:+.0f} 元 ({total_pnl/CAPITAL*100:+.2f}%)")
print(f"对比: 实际系统本周约 -1.1万~-1.2万")
