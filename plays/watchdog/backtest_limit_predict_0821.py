#!/usr/bin/env python3
"""回测：模型预测涨停票（panel_score 高分段）开盘买入 → 次日开盘卖出。

- 每日 09:30 截面按 panel_score 取高分段（≥50/≥60/≥70 或 TopN）
- 开盘价买入 1 手，次日开盘价卖出（T+1）
- 验证模型预测涨停效果：各分段实际涨停率/大涨率
"""
import pandas as pd
from pathlib import Path

DAYS = ["20260814", "20260817", "20260818", "20260819", "20260820", "20260821"]
SP = Path("plays/limit_up/data/snapshot_log")

def open_slice(day: str) -> pd.DataFrame:
    df = pd.read_parquet(SP / f"{day}.parquet")
    df["ts"] = df["ts"].astype(str)
    morning = df[df["ts"] <= "09:35:00"]
    if morning.empty:
        return pd.DataFrame(columns=["code", "price", "panel_score", "pct_chg"])
    return morning.sort_values("ts").groupby("code").first().reset_index()

# 收集每段样本
segments = {50: [], 60: [], 70: [], 80: []}
for i, day in enumerate(DAYS[:-1]):
    nxt = DAYS[i + 1]
    buy_slice = open_slice(day)
    sell_slice = open_slice(nxt)
    if buy_slice.empty or sell_slice.empty:
        continue
    sell_map = dict(zip(sell_slice["code"], sell_slice["price"]))
    sell_pct = dict(zip(sell_slice["code"], sell_slice["pct_chg"]))
    for _, r in buy_slice.iterrows():
        nxt_price = sell_map.get(r["code"])
        if not nxt_price or nxt_price <= 0:
            continue
        pnl = (nxt_price - r["price"]) / r["price"] * 100
        for seg in segments:
            if r["panel_score"] >= seg:
                segments[seg].append({
                    "day": day, "code": r["code"], "panel": float(r["panel_score"]),
                    "pnl_pct": pnl, "next_pct": float(sell_pct.get(r["code"], 0)),
                })
                break  # 归入最高档（分段不重复）

print("=== 模型预测涨停效果验证（分段）===")
for seg, trades in segments.items():
    if not trades:
        continue
    df = pd.DataFrame(trades)
    limit = (df["next_pct"] >= 9.8).sum()  # 次日涨停
    big = (df["next_pct"] >= 5).sum()
    print(f"panel≥{seg}: {len(df)}笔 买→次日开 均{df.pnl_pct.mean():+.2f}% "
          f"胜率{(df.pnl_pct>0).mean()*100:.0f}% | 次日涨停{limit}({limit/len(df)*100:.0f}%) "
          f"涨5%+{big}笔 | 日亏>2%:{(df.pnl_pct<=-2).sum()}笔")

print("\n=== Top5（每日面板分最高5只开盘买）===")
top5 = []
for i, day in enumerate(DAYS[:-1]):
    nxt = DAYS[i + 1]
    buy_slice = open_slice(day)
    sell_slice = open_slice(nxt)
    if buy_slice.empty or sell_slice.empty:
        continue
    sell_map = dict(zip(sell_slice["code"], sell_slice["price"]))
    pool = buy_slice.nlargest(5, "panel_score")
    for _, r in pool.iterrows():
        nxt_price = sell_map.get(r["code"])
        if nxt_price and nxt_price > 0:
            top5.append({"day": day, "code": r["code"], "panel": float(r["panel_score"]),
                         "pnl_pct": (nxt_price - r["price"]) / r["price"] * 100})
if top5:
    df = pd.DataFrame(top5)
    print(f"Top5: {len(df)}笔 均{df.pnl_pct.mean():+.2f}% 胜率{(df.pnl_pct>0).mean()*100:.0f}%")
    for d in DAYS[:-1]:
        sub = df[df.day == d]
        if len(sub):
            print(f"  {d}: {len(sub)}笔 均{sub.pnl_pct.mean():+.2f}%")
