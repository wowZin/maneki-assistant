#!/usr/bin/env python3
"""maneki 交易日巡检纯脚本（cron no_agent 模式执行）。

替代原 maneki-trading-patrol skill 的 LLM agent 巡检——2026-08-10 教训：
agent 巡检会卡在命令审批（pending_approval），无人值守时整个巡检废掉。
本脚本纯文件/进程检查，零审批、零副作用（自动重启 surge 除外——那是自愈）。

输出约定（cron no_agent 语义）：
- 全部健康 → 无输出（静默，不打扰）
- 有故障 → 输出中文故障摘要（会被 cron deliver 转发）+ 飞书直推

只读铁律（2026-08-05 教训）：绝不碰 ws_daemon/jvQuant WS 连接；
判断健康只看文件 mtime / pid / 进程状态。
"""

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path("/root/maneki-agent")
sys.path.insert(0, str(ROOT))

FAILURES: list[str] = []
NOTIFIED = False


def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _is_trade_day(td: str) -> bool:
    try:
        from plays.limit_up.utils import _is_trade_day as _itd
        return bool(_itd(td))
    except Exception:
        return True  # 判断失败按交易日处理（宁可多查不可漏查）


def _check(label: str, ok: bool, detail: str = "") -> None:
    if not ok:
        FAILURES.append(f"{label}: {detail}")
        print(f"❌ {_now()} {label}: {detail}")
    else:
        print(f"✅ {_now()} {label}")


def _notify(text: str) -> None:
    """飞书直推（复用 pipeline._notify_text）。"""
    global NOTIFIED
    try:
        from plays.limit_up.pipeline import _notify_text
        _notify_text(text)
        NOTIFIED = True
    except Exception as e:
        print(f"  [notify] 飞书推送失败: {e}")


def main() -> int:
    now = datetime.now()
    hhmm = int(now.strftime("%H%M"))
    td = now.strftime("%Y%m%d")
    today = now.strftime("%m-%d")

    # 非交易日静默
    if not _is_trade_day(td):
        print(f"[patrol] {today} 非交易日，跳过")
        return 0

    # 交易时段窗口（09:35 前/午休/15:00 后只做基础检查，不做 surge 卡死判定）
    trading = (935 <= hhmm < 1130) or (1300 <= hhmm < 1500)
    morning_done = hhmm >= 935

    print(f"[patrol] {today} {now.strftime('%H:%M')} 交易日 巡检开始 (trading={trading})")

    # ── 1. 面板检查 ──
    panel = ROOT / "wiki" / "raw" / "limit-up" / "panel" / f"{td}.parquet"
    if morning_done:
        if panel.exists():
            try:
                import pandas as pd
                df = pd.read_parquet(panel)
                has_score = "model_score" in df.columns and df["model_score"].notna().sum() > 0
                _check(f"面板 {td}.parquet", has_score,
                       f"存在但 model_score 空/缺失 ({len(df)}只)")
            except Exception as e:
                _check(f"面板 {td}.parquet", False, f"读取失败: {e}")
        else:
            _check(f"面板 {td}.parquet", False, "文件不存在（panel_builder 可能失败）")

    # ── 2. analysis 检查 ──
    ana = ROOT / "plays" / "limit_up" / "data" / "analysis" / f"{td}.json"
    if morning_done:
        if ana.exists():
            try:
                recs = json.loads(ana.read_text())
                _check(f"analysis {td}.json", len(recs) >= 1000, f"记录数 {len(recs)} < 1000")
            except Exception as e:
                _check(f"analysis {td}.json", False, f"解析失败: {e}")
        else:
            _check(f"analysis {td}.json", False, "文件不存在（pipeline 未执行成功）")

    # ── 3. pushed 检查 ──
    pushed = ROOT / "plays" / "limit_up" / "data" / "pushed"
    if morning_done:
        pushed_files = list(pushed.glob(f"{td}_*.json"))
        if not pushed_files:
            _check(f"pushed {td}", False, "无当日推送文件")
        else:
            _check(f"pushed {td}", True, f"{len(pushed_files)} 个文件")

    # ── 4. surge 检查（交易时段核心）──
    hb = ROOT / "plays" / "limit_up" / "data" / "health" / "surge_heartbeat"
    pidf = ROOT / "plays" / "limit_up" / "data" / "health" / "surge_scanner.pid"
    if trading:
        alive = False
        try:
            pid = int(pidf.read_text().strip()) if pidf.exists() else 0
            if pid:
                os.kill(pid, 0)
                alive = True
        except Exception:
            alive = False
        if not alive:
            _check("surge 进程", False, "pid 不存在/进程死亡")
        else:
            hb_age = (now - datetime.fromtimestamp(hb.stat().st_mtime)).total_seconds() \
                if hb.exists() else 9999
            if hb_age > 180:
                _check("surge 心跳", False, f"{hb_age:.0f}s 未更新（进程假死，卡 futex）")
                # 自愈：重启 surge（只读巡检中唯一允许的动作）
                print(f"  [patrol] 自动重启 maneki-surge（心跳 {hb_age:.0f}s 过期）")
                try:
                    subprocess.run(["systemctl", "restart", "maneki-surge"],
                                   capture_output=True, timeout=30)
                    _notify(f"🔍 [巡检自愈] {today} {_now()}\n"
                            f"故障: surge 进程假死（心跳 {hb_age:.0f}s 未更新）\n"
                            f"修复: 已自动重启 maneki-surge")
                except Exception as e:
                    print(f"  [patrol] 重启失败: {e}")
            else:
                _check("surge 心跳", True, f"{hb_age:.0f}s")

        # state.json watching 数是否在增长（surge 正常写入的佐证）
        state = ROOT / "plays" / "watchdog" / "data" / "state.json"
        if state.exists():
            try:
                st = json.loads(state.read_text())
                watching = sum(1 for v in st.values() if v.get("status") in ("watching", "alerted"))
                _check("state.json 可解析", True, f"watching={watching} entered={sum(1 for v in st.values() if v.get('status')=='entered')}")
            except Exception as e:
                _check("state.json", False, f"解析失败: {e}")

    # ── 5. watchdog / ws_snap 检查 ──
    try:
        r = subprocess.run(["systemctl", "is-active", "maneki-watchdog"],
                           capture_output=True, timeout=15, text=True)
        _check("maneki-watchdog", r.stdout.strip() == "active", f"status={r.stdout.strip()}")
    except Exception as e:
        _check("maneki-watchdog", False, f"查询失败: {e}")

    snap = Path("/dev/shm/ws_snap.json")
    if snap.exists():
        age = (now - datetime.fromtimestamp(snap.stat().st_mtime)).total_seconds()
        _check("ws_snap 新鲜度", age < 300, f"{age:.0f}s 未更新（ws_daemon 可能挂了）")
    else:
        _check("ws_snap", False, "/dev/shm/ws_snap.json 不存在")

    # ── 汇总 ──
    if FAILURES:
        summary = f"🔍 [巡检] {today} {_now()} 发现 {len(FAILURES)} 项异常\n" + "\n".join(FAILURES[:6])
        if not NOTIFIED:
            _notify(summary)
        print(summary)
        return 1
    print(f"[patrol] {today} 全部健康，静默")
    return 0


if __name__ == "__main__":
    sys.exit(main())
