#!/usr/bin/env python3
"""WS 数据守护进程 — 独占 jvQuant WS 连接，共享内存供 watchdog/pipeline 读取。

用法:
    python3 scripts/ws_daemon.py                    # 启动 daemon
    python3 scripts/ws_daemon.py --shorts 600519    # 初始订阅

数据:
    /dev/shm/ws_sub.json     → {"shorts": ["600519"], "l2_shorts": []}
    /dev/shm/ws_snap.json    → {"600519": {last, bid_price, ask_price, ...}, 
                                "600519_vwap": 38.5, ...}
"""

import json
import os
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

SHM = Path("/dev/shm")
SUB_FILE = SHM / "ws_sub.json"
SNAP_FILE = SHM / "ws_snap.json"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.jvquant_ws_client import JvQuantWSClient

_running = True


def _signal_handler(sig, frame):
    global _running
    _running = False


def _read_sub() -> tuple[list[str], list[str]]:
    """读取订阅配置。"""
    try:
        data = json.loads(SUB_FILE.read_text())
        return data.get("shorts", []), data.get("l2_shorts", [])
    except Exception:
        return [], []


def _write_snap(shorts: list[str], ws_client):
    """快照写入共享内存（原子写入：tempfile + rename）。"""
    snap: dict = {}
    for short in shorts[:400]:  # 最多400只，超限也不影响排序
        try:
            mkt = ws_client.get_market(short)
            if mkt:
                snap[short] = mkt
            vwap = ws_client.get_vwap(short)
            if vwap:
                snap[f"{short}_vwap"] = vwap
        except Exception:
            pass
    try:
        tmp = SNAP_FILE.with_name("ws_snap.tmp")
        tmp.write_text(json.dumps(snap, default=str))
        tmp.rename(SNAP_FILE)  # 原子 rename
    except Exception:
        pass
    return len(snap)


def main():
    global _running
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    shorts, l2_shorts = _read_sub()
    print(f"[ws_daemon] 启动, 初始订阅 L1={len(shorts)} L2={len(l2_shorts)}")

    # 引入交易日判断
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from plays.limit_up.utils import _is_trading_session, _is_trade_day

    # 非交易日直接退，不连 WS
    _today = datetime.now().strftime("%Y%m%d")
    if not _is_trade_day(_today):
        print(f"[ws_daemon] 非交易日({_today}), 退出")
        return

    ws = JvQuantWSClient()
    ws.connect()
    if shorts:
        ws.subscribe_l1(shorts)
    if l2_shorts:
        ws.subscribe_l2(l2_shorts)
    print("[ws_daemon] WS 已连接")

    snap_interval = 1.0  # 快照刷新间隔(秒)
    last_snap = 0.0
    last_sub_check = 0.0
    last_health_check = 0.0

    while _running:
        now = time.time()
        # 非交易时段等待（盘前等待开盘,盘中正常,盘后退出）
        _hhmm = int(datetime.now().strftime("%H%M"))
        _today = datetime.now().strftime("%Y%m%d")
        if not _is_trade_day(_today):
            print(f"[ws_daemon] 非交易日({_today}), 退出")
            break
        if _hhmm >= 1500:
            print(f"[ws_daemon] 收盘({_hhmm}), 退出")
            break
        if _hhmm < 925 or (1130 <= _hhmm < 1300):
            time.sleep(30)  # 盘前/午休等待
            continue
        # 检查新订阅（每秒）
        if now - last_sub_check >= 2:
            new_shorts, new_l2 = _read_sub()
            for s in new_shorts:
                if s not in shorts:
                    shorts.append(s)
                    try:
                        ws.subscribe_l1([s])
                    except Exception:
                        pass
            for s in new_l2:
                if s not in l2_shorts:
                    l2_shorts.append(s)
                    try:
                        ws.subscribe_l2([s])
                    except Exception:
                        pass
            # 清理退订 L1
            for s in shorts[:]:
                if s not in new_shorts:
                    shorts.remove(s)
                    try:
                        ws.unsubscribe_l1([s])
                    except Exception:
                        pass
            # 清理退订 L2
            for s in l2_shorts[:]:
                if s not in new_l2:
                    l2_shorts.remove(s)
                    try:
                        ws.unsubscribe_l2([s])
                    except Exception:
                        pass
            last_sub_check = now

        # WS 连接检测（每 30s）
        if now - last_health_check >= 30:
            try:
                if not ws.is_connected():
                    print(f"[ws_daemon] WS 断连，尝试重连...")
                    ws.disconnect()
                    ws = JvQuantWSClient()
                    ws.connect()
                    # 恢复订阅
                    if shorts: ws.subscribe_l1(shorts)
                    if l2_shorts: ws.subscribe_l2(l2_shorts)
            except Exception as e:
                print(f"[ws_daemon] WS 重连失败: {e}")
            last_health_check = now

        # 快照
        if now - last_snap >= snap_interval and shorts:
            n = _write_snap(shorts, ws)
            last_snap = now

        time.sleep(0.1)

    ws.disconnect()
    print("[ws_daemon] 已停止")


if __name__ == "__main__":
    main()
