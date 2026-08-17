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
from datetime import datetime, timedelta
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
            # 2026-08-17：kill -9 后等进程死透，否则紧跟的 _watchdog_already_
            # running pgrep 仍匹配 zombie → 误判"已在运行"跳过启动（引擎不启动）
            time.sleep(0.5)
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

        # 2026-08-17 收盘后不拉起引擎（幽灵单根治配套）：
        # 引擎 15:05 自退后 daemon 10s 又拉起 → 收盘后死循环空转
        #（0812 记录：幽灵单载体 + CPU 空转，main_loop 只查交易日不查时段）。
        now = datetime.now()
        _hhmm = now.hour * 100 + now.minute
        if not (920 <= _hhmm < 1505):  # 09:20-15:05 交易时段窗口
            _kill_stale_engines()
            engine_proc = None
            target = now.replace(hour=9, minute=20, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            sleep_s = max((target - now).total_seconds(), 60)
            log(f"非交易时段，休眠 {sleep_s/3600:.1f}h 到 {target.strftime('%m-%d %H:%M')}")
            time.sleep(sleep_s)
            continue

        # 交易时段管理引擎
        if engine_proc is None or engine_proc.poll() is not None:
            engine_proc = run_watchdog_engine()

        time.sleep(10)

    # 清理：先强杀所有残留引擎再退出（2026-08-17 幽灵单根治）。
    # 原实现 terminate()+wait(5)——引擎收到 SIGTERM 若卡在 check_order 网络
    # 请求 5 秒未死 → daemon 退出，引擎变孤儿继续跑 _check_pending_buy 复查
    # 写假交割单（8/17 实测：4 次部署重启产生 5 笔珍宝岛"挂单成交"幽灵单，
    # 写入时间全落在 restart 前 1-2 秒）。SIGKILL 立即死，不留复查窗口。
    _kill_stale_engines()
    if engine_proc:
        try:
            engine_proc.kill()
            engine_proc.wait(timeout=3)
        except Exception:
            pass
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
