#!/usr/bin/env python3
"""盯盘守护进程 — 管理 watchdog.py 引擎。

职责：
  1. 交易日检测 → 启动/停止 watchdog.py 引擎子进程
  2. 引擎输出实时转发到 daemon 日志
  3. state.json 由 surge_monitor 或飞书指令写入，引擎自动拾取

注意：surge 扫描已独立到 surge_monitor.py，本进程不包含。
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
PLAY_DIR = Path(__file__).resolve().parent
LOG_DIR = PROJECT_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(PROJECT_DIR))

from scripts.tu_share import call_tushare

_running = True
_FORCE = False


def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")
    with open(LOG_DIR / "watchdog.log", "a") as f:
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


def _watchdog_already_running() -> bool:
    """检查是否已有 watchdog.py 在运行（防多实例）。"""
    try:
        r = subprocess.run(
            ["pgrep", "-f", r"watchdog/watchdog\.py"],
            capture_output=True, text=True, timeout=5)
        return bool(r.stdout.strip())
    except Exception:
        return False


def _kill_stale_engines():
    """强杀残留 watchdog 引擎进程。

    2026-08-13 修复（幽灵单根因）：systemctl restart 时旧引擎子进程可能残留
    （daemon 收 SIGTERM 后 engine terminate wait(5) 超时 → 引擎变孤儿继续跑
    约 1-2 分钟）。残留引擎不执行 _load_state 的"重启清空 pending"，带内存
    pending_buy_order_id 复查 → 把历史委托误判成新成交写幽灵交割单
    （8/13 珍宝岛 2 笔：10:04:52 我 restart 后 2 分钟、10:38:39 又一次重启后）。
    启动引擎前一律清掉残留，只允许当前 daemon 拉起的引擎存在。
    """
    try:
        r = subprocess.run(
            ["pgrep", "-f", r"watchdog/watchdog\.py"],
            capture_output=True, text=True, timeout=5)
        pids = [p for p in r.stdout.split() if p.isdigit()]
        if pids:
            log(f"[引擎] 清理残留引擎进程: {','.join(pids)}")
            subprocess.run(["kill", "-9"] + pids, capture_output=True, timeout=5)
    except Exception:
        pass


def run_watchdog_engine():
    """启动原 watchdog 引擎（子进程）。"""
    _kill_stale_engines()
    if _watchdog_already_running():
        log("[引擎] watchdog.py 已在运行，跳过启动")
        return None

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

    def _reader():
        for line in iter(proc.stdout.readline, ""):
            if line:
                log(f"[引擎] {line.rstrip()}")
    t = threading.Thread(target=_reader, daemon=True)
    t.start()
    return proc


def main_loop():
    global _running, _FORCE
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)
    log("盯盘守护进程启动")

    engine_proc = None

    while _running:
        td = datetime.now().strftime("%Y%m%d")

        if not _is_trade_day(td):
            if engine_proc:
                log("[引擎] 非交易日，停止引擎")
                engine_proc.terminate()
                engine_proc.wait(timeout=5)
                engine_proc = None
            log(f"非交易日 {td}，等 1 小时")
            time.sleep(3600)
            continue

        # 交易时段管理引擎
        if engine_proc is None or engine_proc.poll() is not None:
            engine_proc = run_watchdog_engine()

        time.sleep(10)

    # 清理
    if engine_proc:
        engine_proc.terminate()
        engine_proc.wait(timeout=5)
    log("盯盘守护进程已停止")


def main():
    global _FORCE
    import argparse
    parser = argparse.ArgumentParser(description="盯盘守护进程")
    args = parser.parse_args()
    log("[watchdog] 启动")
    main_loop()


if __name__ == "__main__":
    main()
