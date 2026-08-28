#!/usr/bin/env python3
"""查 10 只净流入票当前卡在 fund_accumulate 哪个条件：涨幅 + 主动买。"""
import json
import pandas as pd

state = json.load(open("/root/maneki-agent/plays/watchdog/data/state.json"))
snap = json.load(open("/dev/shm/ws_snap.json"))
# 昨收从 daily 20260827 读
daily = pd.read_parquet("/root/maneki-agent/wiki/raw/limit-up/panel/daily/20260827.parquet",
                        columns=["ts_code", "close"])
close_map = dict(zip(daily["ts_code"], daily["close"]))

rows = []
for c, v in state.items():
    if v.get("source") != "panel":
        continue
    short = c.split(".")[0]
    d = snap.get(short)
    if not d or not isinstance(d, dict):
        continue
    bnet = float(d.get("big_net_amount") or 0) + float(d.get("super_net_amount") or 0)
    if bnet <= 0:
        continue
    last = float(d.get("last") or 0)
    bb = float(d.get("big_buy_amount") or 0)
    bs = float(d.get("big_sell_amount") or 0)
    pc = close_map.get(c)
    pct = (last / pc - 1) * 100 if pc else None
    rows.append((short, bnet, last, pc, pct, bb > bs))

print("净流入票的 fund_accumulate 条件（涨幅 vs 主动买）:")
print(f"{'code':<8}{'净流入':>12}{'last':>8}{'昨收':>8}{'涨幅%':>8}{'主动买':>8}")
for short, bnet, last, pc, pct, buy_gt in sorted(rows, key=lambda x: -x[1]):
    pct_s = f"{pct:.1f}" if pct is not None else "?"
    flag = "涨超5%" if (pct is not None and pct > 5) else ""
    print(f"{short:<8}{bnet:>12.0f}{last:>8.2f}{(pc or 0):>8.2f}{pct_s:>8}{'买>卖' if buy_gt else '卖>买':>8} {flag}")
