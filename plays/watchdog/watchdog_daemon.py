#!/usr/bin/env python3
"""盯盘常驻进程 — 整合 surge 扫描 + 持仓盯盘 + 交易信号。

作用：
  1. 判断交易日/交易时段
  2. Surge 扫描新候选股 → 自动加入盯盘池
  3. 盯盘引擎监控持仓股 → 推送入场/出场/异常信号
  4. 状态持久化，崩溃恢复
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
PLAY_DIR = Path(__file__).resolve().parent
LOG_DIR = PROJECT_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(PROJECT_DIR))

from plays.limit_up.utils import is_trading_time
from scripts.tu_share import call_tushare

_running = True
_FORCE = False


def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")
    with open(LOG_DIR / "watchdog_daemon.log", "a") as f:
        f.write(f"[{ts}] {msg}\n")


def _signal_handler(sig, frame):
    global _running
    log(f"收到信号 {sig}，关闭中...")
    _running = False


def _is_trade_day(date_str: str) -> bool:
    try:
        r = call_tushare("trade_cal", {"cal_date": date_str}, "is_open")
        items = r.get("data", {}).get("items", [])
        return items and items[0][0] == 1 if items else False
    except Exception:
        return False


def run_watchdog_engine():
    """启动原 watchdog 引擎（子进程）。"""
    log("[引擎] 启动 watchdog...")
    engine = os.path.join(os.path.dirname(__file__), "watchdog.py")
    proc = subprocess.Popen(
        [sys.executable, engine],
        cwd=str(PROJECT_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    log(f"[引擎] PID={proc.pid}")

    # 实时输出日志
    def _reader():
        for line in iter(proc.stdout.readline, ""):
            if line:
                log(f"[引擎] {line.rstrip()}")
    t = threading.Thread(target=_reader, daemon=True)
    t.start()
    return proc


def surge_monitor():
    """surge 扫描线程 — 发现异动股自动加盯盘。"""
    log("[surge] 线程启动")
    while _running:
        try:
            if not is_trading_time():
                time.sleep(60)
                continue

            # 载入盯盘状态
            from plays.watchdog.watchdog_client import _load_state as _ld, _save_state as _sv
            state = _ld()

            # surge 扫描
            try:
                from plays.limit_up.pipeline import scan_surge
                candidates = scan_surge()
                if candidates:
                    log(f"[surge] {len(candidates)} 只候选")
                    new_added = 0
                    for c in candidates[:10]:
                        code = c.get("code", "")
                        if code and code not in state:
                            state[code] = {
                                "name": c.get("name", ""),
                                "added_ts": datetime.now().isoformat(),
                                "source": "surge",
                            }
                            new_added += 1
                    if new_added:
                        _sv(state)
                        log(f"  + 新增盯盘 {new_added} 只")
            except ImportError as e:
                log(f"[surge] scan_surge 不可用: {e}")

            time.sleep(120)
        except Exception as e:
            log(f"[surge] 异常: {e}")
            time.sleep(30)


def surge_monitor_run_once():
    """强制模式：执行一次 surge 扫描并加入盯盘。"""
    log("[surge] 一次性扫描...")
    from plays.watchdog.watchdog_client import _load_state as _ld, _save_state as _sv
    state = _ld()
    try:
        from plays.limit_up.pipeline import scan_surge
        candidates = scan_surge()
        if candidates:
            log(f"[surge] {len(candidates)} 只候选")
            for c in candidates[:10]:
                code = c.get("code", "")
                if code and code not in state:
                    state[code] = {
                        "name": c.get("name", ""),
                        "added_ts": datetime.now().isoformat(),
                        "source": "surge",
                    }
            _sv(state)
            log(f"  已保存，当前盯盘 {len(state)} 只")
    except ImportError as e:
        log(f"[surge] scan_surge 不可用: {e}")


def main_loop():
    global _running, _FORCE
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)
    log("盯盘守护进程启动")

    if _FORCE:
        log("[强制模式] 执行一次完整扫描后退出")
        surge_monitor_run_once()
        return

    engine_proc = None
    surge_t = None

    while _running:
        td = datetime.now().strftime("%Y%m%d")

        # 非交易日等待
        if not _is_trade_day(td):
            if engine_proc:
                engine_proc.terminate()
                engine_proc = None
            log(f"非交易日 {td}，等 1 小时")
            time.sleep(3600)
            continue

        # 启动引擎
        if engine_proc is None or engine_proc.poll() is not None:
            engine_proc = run_watchdog_engine()

        # 启动 surge 扫描
        if surge_t is None or not surge_t.is_alive():
            surge_t = threading.Thread(target=surge_monitor, daemon=True)
            surge_t.start()

        time.sleep(10)

    # 清理
    if engine_proc:
        engine_proc.terminate()
        engine_proc.wait(timeout=5)
    log("盯盘守护进程已停止")


def main():
    global _FORCE
    import argparse
    parser = argparse.ArgumentParser(description="盯盘守护进程 (Surge + Trade)")
    parser.add_argument("--force", action="store_true", help="强制模式：执行一次完整扫描后退出")
    args = parser.parse_args()
    if args.force:
        _FORCE = True
    log("[watchdog_daemon] 启动")
    main_loop()


if __name__ == "__main__":
    main()
