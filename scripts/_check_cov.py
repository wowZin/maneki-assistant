#!/usr/bin/env python3
"""查 119 只 panel 票在 ws_snap 的覆盖率，以及 ws_daemon 订阅状态。"""
import json

state = json.load(open("/root/maneki-agent/plays/watchdog/data/state.json"))
snap = json.load(open("/dev/shm/ws_snap.json"))

panel_codes = [c for c, v in state.items() if v.get("source") == "panel"]
# ws_snap 键可能是 6 位短码
snap_keys = set(snap.keys())

covered = [c for c in panel_codes if c.split(".")[0] in snap_keys]
missing = [c for c in panel_codes if c.split(".")[0] not in snap_keys]

print(f"panel 票: {len(panel_codes)} 只")
print(f"ws_snap 快照: {len(snap_keys)} 只")
print(f"panel 票在快照中(有行情): {len(covered)} 只")
print(f"panel 票缺失(无行情): {len(missing)} 只")
print(f"\n缺失的前 30 只: {[c.split('.')[0] for c in missing[:30]]}")
