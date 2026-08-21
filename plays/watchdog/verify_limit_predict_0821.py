#!/usr/bin/env python3
"""验证模型预测涨停效果：全天最大 panel_score 分段的当日实际涨停率。

再回测：高分票（当日 max panel 前 N）次日开盘买次日开盘卖。
"""
import pandas as pd
from pathlib import Path

DAYS = ["20260814", "20260817", "20260818", "20260819", "20260820", "20260821"]
SP = Path("plays/limit_up/data/snapshot_log")

print("=== 模型预测效果（当日 max panel_score 分段 vs 当日收盘表现）===")
seg_stats = {50: [], 60: [], 70: [], 80: []}
for day in DAYS:
    df = pd.read_parquet(SP / f"{day}.parquet")
    df["ts"] = df["ts"].astype(str)
    df["panel"] = df["panel_score"].astype(float)
    df["pct"] = df["pct_chg"].astype(float)
    last = df.sort_values("ts").groupby("code").agg(
        panel=("panel", "max"), pct=("pct", "last")).reset_index()
    for _, r in last.iterrows():
        for seg in seg_stats:
            if r["panel"] >= seg:
                seg_stats[seg].append({"day": day, "code": r["code"],
                                       "panel": r["panel"], "pct": r["pct"]})
                break
for seg, rows in seg_stats.items():
    if not rows:
        continue
    d = pd.DataFrame(rows)
    lim = (d["pct"] >= 9.8).sum()
    big = (d["pct"] >= 5).sum()
    dn = (d["pct"] <= -2).sum()
    print(f"当日max panel≥{seg}: {len(d)}只 实际涨停{lim}({lim/len(d)*100:.0f}%) "
          f"涨5%+{big}笔 跌超2%:{dn}笔 均{d.pct.mean():+.1f}%")

print("\n=== 回测：当日 max panel 高分票 → 次日开盘买入 → 次日开盘卖 ===")
# 简化: D日高分票, D+1开盘买(次日仍可能触发), D+2开盘卖? 不——用户口径: 开盘买预测涨停票, 次日开盘跑。
# 用 D 日 09:30 截面 panel≥N（模型在开盘时点能预测到的）→ D+1 开盘卖（已做,样本少）
# 补充: D 日全天 max panel≥70 的票, D+1 开盘买(若未涨停) → D+2 开盘卖
for seg in (60, 70, 80):
    rows = []
    for i, day in enumerate(DAYS[:-1]):
        nxt = DAYS[i + 1]
        if nxt not in DAYS:
            continue
        df = pd.read_parquet(SP / f"{day}.parquet")
        df["panel"] = df["panel_score"].astype(float)
        top = df.groupby("code")["panel"].max()
        top = top[top >= seg].index.tolist()
        nxt_df = pd.read_parquet(SP / f"{nxt}.parquet")
        nxt_df["ts"] = nxt_df["ts"].astype(str)
        nxt_open = nxt_df[nxt_df["ts"] <= "09:35:00"].sort_values("ts").groupby("code").first()
        for c in top:
            r = nxt_open.loc[c] if c in nxt_open.index else None
            if r is None:
                continue
            rows.append({"day": day, "code": c, "panel": df[df.code == c]["panel"].max(),
                         "open": float(r["price"]), "pct": float(r["pct_chg"])})
    if rows:
        d = pd.DataFrame(rows)
        print(f"D日panel≥{seg}({len(d)}只次日可买): 次日开盘均涨{d.pct.mean():+.2f}% "
              f"涨停{(d.pct>=9.8).sum()} 跌超2%:{(d.pct<=-2).sum()}")
