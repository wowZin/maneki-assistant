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


def _subscribe_chunked(ws, level: str, codes: list[str], chunk: int = 10):
    """分批订阅（每批 chunk 只，批间 0.3s）。

    2026-07-27 实证：一次性 add_lv1(34只) 单命令石沉大海（服务端无报错无推送），
    分批订阅后 L1 推送恢复。L2 逐笔同样分批。订阅失败抛异常由调用方入重试集。"""
    fn = ws.subscribe_l1 if level == "l1" else ws.subscribe_l2
    for i in range(0, len(codes), chunk):
        fn(codes[i:i + chunk])
        if i + chunk < len(codes):
            time.sleep(0.3)


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
        _subscribe_chunked(ws, "l1", shorts)
    if l2_shorts:
        _subscribe_chunked(ws, "l2", l2_shorts)
    print(f"[ws_daemon] WS 已连接, L1={len(shorts)} L2={len(l2_shorts)} 分批订阅完成")

    snap_interval = 1.0  # 快照刷新间隔(秒)
    last_snap = 0.0
    last_sub_check = 0.0
    _retry_l1: set = set()  # L1 订阅失败重试集
    _retry_l2: set = set()  # L2 订阅失败重试集
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
            time.sleep(30)  # 盘前/午休：慢节奏，但照写快照（WS 仍推盘口数据）
            # 继续执行下面的订阅/快照逻辑（不 continue），接午间快照
        # 检查新订阅（每秒）
        # 2026-07-27 修复：订阅失败不再静默吞掉（surge 批量 23 只时约一半失败被吞，
        # 本地标记已订阅但实际没订上 → 18 只盯盘票全天无快照）。
        # 失败的进入重试集，下轮继续；重连后全量重订。
        if now - last_sub_check >= 2:
            new_shorts, new_l2 = _read_sub()
            # 新票批量收集后分批订阅（surge 一轮+20只时逐条命令易被服务端丢弃）
            todo_l1 = [s for s in new_shorts if s not in shorts and s not in _retry_l1]
            if todo_l1:
                try:
                    _subscribe_chunked(ws, "l1", todo_l1)
                    shorts.extend(todo_l1)
                    print(f"[ws_daemon] L1新增订阅 {len(todo_l1)} 只")
                except Exception as e:
                    print(f"[ws_daemon] L1批量订阅失败: {e}，转逐只重试")
                    _retry_l1.update(todo_l1)
            for s in list(_retry_l1):
                try:
                    ws.subscribe_l1([s])
                    shorts.append(s)
                    _retry_l1.discard(s)
                    print(f"[ws_daemon] L1重试成功 {s}")
                except Exception:
                    time.sleep(0.2)  # 逐只重试也限速
            todo_l2 = [s for s in new_l2 if s not in l2_shorts and s not in _retry_l2]
            if todo_l2:
                try:
                    _subscribe_chunked(ws, "l2", todo_l2)
                    l2_shorts.extend(todo_l2)
                except Exception as e:
                    print(f"[ws_daemon] L2批量订阅失败: {e}，转逐只重试")
                    _retry_l2.update(todo_l2)
            for s in list(_retry_l2):
                try:
                    ws.subscribe_l2([s])
                    l2_shorts.append(s)
                    _retry_l2.discard(s)
                except Exception:
                    time.sleep(0.2)
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
                    # 恢复订阅（分批，防单命令超限）
                    if shorts: _subscribe_chunked(ws, "l1", shorts)
                    if l2_shorts: _subscribe_chunked(ws, "l2", l2_shorts)
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
