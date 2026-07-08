#!/usr/bin/env python3
"""
盯盘助手客户端 — 与后台 watchdog.service 通信

通过直接读写 state.json 实现，后台守护进程每30秒检测变更后自动加载。

用法:
  python3 plays/watchdog/watchdog_client.py --add 000001.SZ
  python3 plays/watchdog/watchdog_client.py --remove 000001.SZ
  python3 plays/watchdog/watchdog_client.py --list
  python3 plays/watchdog/watchdog_client.py --clear
  python3 plays/watchdog/watchdog_client.py --status
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from plays.watchdog.watchdog import WatchState, STATE_FILE, MAX_WATCH, _norm, _short  # noqa


def _load_state() -> dict[str, WatchState]:
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            data = json.load(f)
        return {code: WatchState.from_dict(d) for code, d in data.items()}
    return {}


def _save_state(states: dict[str, WatchState]):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {code: st.to_dict() for code, st in states.items()}
    with open(STATE_FILE, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _resolve_name(code: str) -> str:
    try:
        from scripts.tu_share import call_tushare
        resp = call_tushare("stock_basic", {"ts_code": code}, "ts_code,name")
        items = resp.get("data", {}).get("items", [])
        if items and len(items[0]) > 1:
            return items[0][1]
    except Exception:
        pass
    return code


def cmd_add(codes: list[str]) -> str:
    codes = [_norm(c) for c in codes]
    states = _load_state()
    msgs = []
    for code in codes:
        if code in states:
            msgs.append(f"{code} 已在盯盘中")
            continue
        if len(states) >= MAX_WATCH:
            msgs.append(f"盯盘已达上限({MAX_WATCH}只)，无法添加 {code}")
            continue
        name = _resolve_name(code)
        st = WatchState(code, name)
        states[code] = st
        msgs.append(f"开始盯盘 {name}({code})")
    _save_state(states)
    return "\n".join(msgs)


def cmd_remove(codes: list[str]) -> str:
    codes = [_norm(c) for c in codes]
    states = _load_state()
    msgs = []
    for code in codes:
        if code in states:
            st = states.pop(code)
            msgs.append(f"停止盯盘 {st.name}({code})")
        else:
            msgs.append(f"{code} 未在盯盘中")
    _save_state(states)
    return "\n".join(msgs)


def cmd_list() -> str:
    states = _load_state()
    if not states:
        return "当前无盯盘标的"
    lines = ["📋 盯盘列表:"]
    icon_map = {"watching": "👁", "alerted": "⏳", "entered": "📈", "exited": "🔚"}
    for code, st in states.items():
        icon = icon_map.get(st.status, "❓")
        lines.append(f"  {icon} {st.name}({code}) [{st.status}]")
    return "\n".join(lines)


def cmd_clear() -> str:
    states = _load_state()
    count = len(states)
    _save_state({})
    return f"已清空{count}只盯盘标的"


def cmd_status() -> str:
    states = _load_state()
    if not states:
        return "盯盘守护进程: 运行中 | 盯盘数量: 0"
    codes_info = ", ".join(f"{st.name}({code})" for code, st in list(states.items())[:5])
    if len(states) > 5:
        codes_info += f" ... 共{len(states)}只"
    return (
        f"盯盘守护进程: 运行中\n"
        f"盯盘数量: {len(states)}/{MAX_WATCH}\n"
        f"标的: {codes_info}"
    )


def main():
    parser = argparse.ArgumentParser(description="盯盘助手客户端")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--add", nargs="+", help="添加盯盘标的（如 000001.SZ）")
    group.add_argument("--remove", nargs="+", help="移除盯盘标的")
    group.add_argument("--list", action="store_true", help="查看盯盘列表")
    group.add_argument("--clear", action="store_true", help="清空所有盯盘")
    group.add_argument("--status", action="store_true", help="查看守护进程状态")
    args = parser.parse_args()

    if args.add:
        print(cmd_add(args.add))
    elif args.remove:
        print(cmd_remove(args.remove))
    elif args.list:
        print(cmd_list())
    elif args.clear:
        print(cmd_clear())
    elif args.status:
        print(cmd_status())


if __name__ == "__main__":
    main()
