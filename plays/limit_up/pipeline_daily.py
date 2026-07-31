#!/usr/bin/env python3
"""每日预测守护进程 — 全内置版。

时间线：
  00:30  概念缓存 → 面板构建（panel_builder）
  09:26  竞价刷新面板 → 模型评分（XGBoost 64特征）→ 推送
  期间   sleep

生产管线复用: _refresh_panel_auction + morning_pass 直接从 pipeline.py 调用。
面板构建通过 subprocess 执行 panel_builder.py。
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
PLAY_DIR = Path(__file__).resolve().parent
PANEL_DIR = PROJECT_DIR / "wiki" / "raw" / "limit-up" / "panel"
LOG_DIR = PROJECT_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(PROJECT_DIR))

from plays.limit_up.utils import _is_trade_day

_running = True
_FORCE = False

# ── 今日步骤完成标记（防同一时间段重复跑）──
_done_concept = False
_done_panel = False
_done_morning = False


def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")
    with open(LOG_DIR / "daily.log", "a") as f:
        f.write(f"[{ts}] {msg}\n")


def _signal_handler(sig, frame):
    global _running
    log(f"收到信号 {sig}，关闭中...")
    _running = False


# ═══════════════════════════════════════════════
# 步骤 1: 概念缓存
# ═══════════════════════════════════════════════


def step_concept_cache():
    global _done_concept
    log("[00:30] 加载概念缓存...")
    from plays.limit_up.strategies import factor_ctx
    cd, cm = factor_ctx.load_concept_data_from_cache()
    log(f"  概念行情 {len(cd)} 行, 成分股 {len(cm)} 行 ✓")
    _done_concept = True


# ═══════════════════════════════════════════════
# 步骤 2: 面板构建（内置，不依赖外部 cron）
# ═══════════════════════════════════════════════


def step_build_panel(today: str):
    """执行 panel_builder.py 构建今日面板，供 09:26 评分使用。"""
    global _done_panel
    log("[00:30+] 开始构建面板...")

    builder = str(PLAY_DIR / "panel_builder.py")
    t0 = time.time()

    proc = subprocess.run(
        [sys.executable, builder],
        cwd=str(PROJECT_DIR),
        capture_output=True, text=True, timeout=900,
    )

    elapsed = time.time() - t0
    for line in proc.stdout.splitlines():
        if any(kw in line for kw in ("保存", "完成", "error", "失败", "✓", "⚠", "预评")):
            log(f"  [panel] {line.strip()}")
    if proc.stderr:
        for line in proc.stderr.splitlines():
            log(f"  [panel:err] {line.strip()}")
    if proc.returncode != 0:
        log(f"⚠️ 面板构建异常，退出码 {proc.returncode}（耗时 {elapsed:.0f}s）")
    else:
        panel_file = PANEL_DIR / f"{today}.parquet"
        if panel_file.exists():
            sz = panel_file.stat().st_size
            log(f"  面板构建完成 ✓ {panel_file.name} ({sz/1024:.0f}KB, {elapsed:.0f}s)")
            _done_panel = True
        else:
            log(f"⚠️ 面板构建完成但文件不存在: {panel_file}")


# ═══════════════════════════════════════════════
# 步骤 3: 早盘评分（复用生产管线）
# ═══════════════════════════════════════════════


def step_morning_score(today: str):
    """竞价刷新面板 → XGBoost 模型评分 → 推送。"""
    global _done_morning
    log("[09:26] 开始早盘评分流程...")

    # 1. 等面板就绪（panel 应在步骤 2 已建好，最多等 5 分钟余量）
    panel_file = PANEL_DIR / f"{today}.parquet"
    deadline = time.time() + 300
    while not panel_file.exists() and time.time() < deadline:
        log("  面板尚未就绪，等待 10s...")
        time.sleep(10)

    if not panel_file.exists():
        log("❌ 面板 5 分钟后仍未就绪，跳过早盘评分")
        return

    # 2. 竞价数据刷新面板（失败不阻断，面板有 T-1 夜间值兜底）
    try:
        from plays.limit_up.pipeline import _refresh_panel_auction
        _refresh_panel_auction(today)
    except Exception as e:
        log(f"⚠️ 竞价刷新异常（不阻断）: {e}")

    # 3. morning pass — 模型评分 + 推送
    try:
        from plays.limit_up.pipeline import morning_pass
        morning_pass(today)
        log("[09:26+] 早盘评分完成 ✓")
    except Exception as e:
        tb = traceback.format_exc(limit=3)
        log(f"❌ 早盘评分失败: {e}\n{tb}")

    _done_morning = True


# ═══════════════════════════════════════════════
# 主循环
# ═══════════════════════════════════════════════


def main_loop():
    global _running, _FORCE, _done_concept, _done_panel, _done_morning

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)
    log("每日预测守护进程启动")

    if _FORCE:
        log("[强制模式] 忽略时间节点，按序执行后退出")
        td = datetime.now().strftime("%Y%m%d")
        step_concept_cache()
        step_build_panel(td)
        step_morning_score(td)
        log("[强制模式] 全部完成")
        return

    while _running:
        now = datetime.now()
        td = now.strftime("%Y%m%d")
        hhmm = now.hour * 100 + now.minute

        if not _is_trade_day(td):
            _done_concept = False
            _done_panel = False
            _done_morning = False
            time.sleep(1800)
            continue

        # 00:30 概念缓存（只跑一次）
        if 30 <= hhmm < 100 and now.hour == 0 and not _done_concept:
            step_concept_cache()
            time.sleep(60)

        # 概念完后立即建面板（只跑一次）
        if 30 <= hhmm < 300 and now.hour == 0 and _done_concept and not _done_panel:
            step_build_panel(td)
            time.sleep(60)

        # 09:26 早盘评分（只跑一次）
        if 926 <= hhmm < 935 and now.hour == 9 and _done_panel and not _done_morning:
            step_morning_score(td)
            time.sleep(60)

        # 收盘后重置标记（跨天就绪）
        if hhmm >= 1500:
            _done_concept = False
            _done_panel = False
            _done_morning = False

        time.sleep(30)


def main():
    global _FORCE
    parser = argparse.ArgumentParser(description="每日预测守护进程")
    parser.add_argument("--force", action="store_true",
                        help="强制模式：忽略时间节点，按序执行所有步骤后退出")
    args = parser.parse_args()
    if args.force:
        _FORCE = True
    log("[daily] 启动")
    main_loop()


if __name__ == "__main__":
    main()
