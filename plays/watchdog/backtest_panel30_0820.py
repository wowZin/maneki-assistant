#!/usr/bin/env python3
"""回测：近一周主闸(panel_score≥30) 开盘买入 1 手 → 次日开盘卖出。

验证简化策略 vs 实际交割单（0820 用户要求）：
- 买入点：每日 09:30 最早快照（开盘买入），panel_score≥30
- 卖出点：次日 09:30 快照开盘价（T+1 可卖）
- 100 股/笔，不含手续费
"""
import pandas as pd
from pathlib import Path

DAYS = ["20260814", "20260817", "20260818", "20260819", "20260820", "20260821"]
SP = Path("plays/limit_up/data/snapshot_log")

def open_slice(day: str) -> pd.DataFrame:
    """当日 09:30-09:35 最早截面（开盘时点）"""
    df = pd.read_parquet(SP / f"{day}.parquet")
    df["ts"] = df["ts"].astype(str)
    morning = df[df["ts"] <= "09:35:00"]
    if morning.empty:
        return pd.DataFrame(columns=["code", "price", "panel_score"])
    return morning.sort_values("ts").groupby("code").first().reset_index()

trades = []
for i, day in enumerate(DAYS[:-1]):
    nxt = DAYS[i + 1]
    buy_slice = open_slice(day)
    sell_slice = open_slice(nxt)
    if buy_slice.empty or sell_slice.empty:
        continue
    sell_map = dict(zip(sell_slice["code"], sell_slice["price"]))
    pool = buy_slice[buy_slice["panel_score"] >= 30]
    for _, r in pool.iterrows():
        nxt_price = sell_map.get(r["code"])
        if not nxt_price or nxt_price <= 0:
            continue
        pnl = (nxt_price - r["price"]) / r["price"] * 100
        trades.append({
            "day": day, "code": r["code"], "panel": round(float(r["panel_score"]), 1),
            "open": float(r["price"]), "next_open": float(nxt_price),
            "pnl_pct": pnl, "pnl_yuan": pnl / 100 * 100,  # 1手100股: pnl%×100/100=pct元
        })

df = pd.DataFrame(trades)
if df.empty:
    print("无交易样本")
    raise SystemExit

print(f"=== 主闸≥30 开盘买1手→次日开盘卖（近一周 {len(df)} 笔）===")
print(f"平均 {df.pnl_pct.mean():+.2f}%  中位 {df.pnl_pct.median():+.2f}%  "
      f"胜率 {(df.pnl_pct>0).mean()*100:.0f}%  "
      f"1手合计 {df.pnl_yuan.sum():+.0f}元")
print(f"日分布:")
for d in DAYS[:-1]:
    sub = df[df.day == d]
    if len(sub):
        print(f"  {d}: {len(sub)}笔 均{sub.pnl_pct.mean():+.2f}% 胜率{(sub.pnl_pct>0).mean()*100:.0f}%")
print(f"\n=== 分面板分段（30-40/40-50/50+）===")
for lo, hi in [(30, 40), (40, 50), (50, 200)]:
    sub = df[(df.panel >= lo) & (df.panel < hi)]
    if len(sub):
        print(f"  {lo}-{hi}: {len(sub)}笔 均{sub.pnl_pct.mean():+.2f}% 胜率{(sub.pnl_pct>0).mean()*100:.0f}%")
print(f"\n=== 亏损>2% 的票（什么特征）===")
bad = df[df.pnl_pct <= -2].sort_values("pnl_pct")
print(f"  {len(bad)}笔 平均面板分 {bad.panel.mean():.1f}")
for _, r in bad.head(8).iterrows():
    print(f"  {r.day} {r.code} panel={r.panel} {r.pnl_pct:+.1f}%")
