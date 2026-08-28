#!/usr/bin/env python3
"""查 10 只净流入票的 fund_accumulate 各项条件，定位卡点。"""
import json
import pandas as pd

state = json.load(open("/root/maneki-agent/plays/watchdog/data/state.json"))
snap = json.load(open("/dev/shm/ws_snap.json"))
panel = pd.read_parquet("/root/maneki-agent/wiki/raw/limit-up/panel/20260828.parquet")

panel_codes = [c for c, v in state.items() if v.get("source") == "panel"]
rows = []
for c in panel_codes:
    short = c.split(".")[0]
    d = snap.get(short)
    if not d:
        continue
    bnet = float(d.get("big_net_amount") or 0)
    snet = float(d.get("super_net_amount") or 0)
    tot = bnet + snet
    if tot <= 0:
        continue
    last = float(d.get("last") or 0)
    high = float(d.get("high") or 0)
    bb = float(d.get("big_buy_amount") or 0)
    bs = float(d.get("big_sell_amount") or 0)
    # 涨幅（用面板 auc_pct 或 pre_close 算不了，用 high/low 粗略）
    r = panel[panel["code"] == c]
    name = r["name"].iloc[0] if len(r) else "?"
    pct = (last / high - 1) * 100 if high > 0 else 0  # 距高点
    rows.append((c, name, tot, last, high, pct, bb > bs))

print("10 只净流入票的 fund_accumulate 条件:")
print(f"{'code':<10}{'name':<10}{'净流入':>12}{'last':>8}{'high':>8}{'距高点%':>9}{'主动买':>8}")
for c, name, tot, last, high, pct, buy_gt in sorted(rows, key=lambda x: -x[2]):
    print(f"{c:<10}{name:<10}{tot:>12.0f}{last:>8.2f}{high:>8.2f}{pct:>8.1f}%{'买>卖' if buy_gt else '卖>买':>8}")
