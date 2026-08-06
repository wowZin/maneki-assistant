#!/usr/bin/env python3
"""把 07-31 被盘后汰换误删的 4 只持仓找回（entered 状态）。

事故背景：07-31 11:30 午休 _eod_purge() 误触发 + 15:00 收盘汰换，
把当天买入的 600986/603226/603679/605598 从盯盘列表删掉。
这 4 只真实持仓存在但已失管。本脚本按当日成交价写回 entered 状态，
watchdog 自动接手离场管理（T+1 已解冻，下一轮即检查离场信号）。
"""
import json
from datetime import datetime
from pathlib import Path

STATE_FILE = Path("/root/maneki-agent/plays/watchdog/data/state.json")

# 07-31 实盘成交记录（watchdog.log + 交割单 20260731.json 核对）
# (code, name, entry_price, entry_at)
RECORDS = [
    ("600986.SH", "浙文互联", 7.50, "2026-07-31 10:18:05"),
    ("603226.SH", "菲林格尔", 45.71, "2026-07-31 10:18:08"),
    ("603679.SH", "华体科技", 19.08, "2026-07-31 10:18:11"),
    ("605598.SH", "上海港湾", 24.82, "2026-07-31 10:18:14"),
]


def main():
    states = json.loads(STATE_FILE.read_text())
    now_iso = datetime.now().isoformat()
    for code, name, entry, entry_at in RECORDS:
        if code in states:
            print(f"{code} 已在 state.json（{states[code].get('status')}），跳过")
            continue
        states[code] = {
            "code": code, "name": name,
            "added_at": now_iso,
            "status": "entered",
            "entry_price": entry,
            "source": "surge",
            "entry_pushed_date": "20260731",
            "t1_blocked_date": "20260731",  # 昨天买入，T+1 已解冻（今天≠昨天）
            "entry_at": entry_at,
            "highest_since_entry": entry,
            "bars_held": 0,
            "signal_type": "surge",
            "signal_reason": "手动找回 07-31 汰换误删持仓",
            "signal_at": "10:18",
            "last_alert_at": "",
            "last_abnormal_level": "",
            "last_abnormal_pushed_at": 0,
            "netflow_history": [],
            "daily_basic": {},
            "dim_scores": {},
            "last_daily_update": "",
        }
        print(f"找回 {code} {name} entry={entry} entered")
    # 原子写（与 watchdog/surge 一致，避免并发截断）
    tmp = STATE_FILE.with_name("state.json.tmp")
    tmp.write_text(json.dumps(states, ensure_ascii=False, indent=2))
    tmp.rename(STATE_FILE)
    print(f"\n已写回 {len(RECORDS)} 只，state.json 现有 {len(states)} 条")


if __name__ == "__main__":
    main()
