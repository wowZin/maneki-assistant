#!/usr/bin/env python3
"""诊断 fund_accumulate 没触发：90 只有行情票的大单净流入 + 29 只缺失票特征。"""
import json
import pandas as pd

state = json.load(open("/root/maneki-agent/plays/watchdog/data/state.json"))
snap = json.load(open("/dev/shm/ws_snap.json"))
panel = pd.read_parquet("/root/maneki-agent/wiki/raw/limit-up/panel/20260828.parquet")

panel_codes = [c for c, v in state.items() if v.get("source") == "panel"]
snap_keys = set(snap.keys())

# 90 只有行情的票 big_net_amount 分布
covered = []
for c in panel_codes:
    short = c.split(".")[0]
    if short in snap_keys:
        d = snap[short]
        bnet = float(d.get("big_net_amount") or 0)
        snet = float(d.get("super_net_amount") or 0)
        covered.append((c, bnet, snet, bnet + snet))

pos = [x for x in covered if x[3] > 0]
neg = [x for x in covered if x[3] <= 0]
print(f"有行情 panel 票: {len(covered)} 只")
print(f"  大单+超大单净流入 >0: {len(pos)} 只")
print(f"  净流入 <=0(净流出): {len(neg)} 只")

# 29 只缺失票的特征（name + 是否 ST）
missing = [c for c in panel_codes if c.split(".")[0] not in snap_keys]
print(f"\n缺失 29 只票的特征:")
for c in missing[:30]:
    short = c.split(".")[0]
    row = panel[panel["code"] == c]
    name = row["name"].iloc[0] if len(row) else "?"
    print(f"  {short} {name}")
