#!/usr/bin/env python3
"""开盘日健康巡检：检查 pipeline daemon / 飞书 Bot / 代理状态，异常时自动重启。

注意：新版 pipeline.py 是常驻 daemon，不再按运行时长杀进程；改为检查心跳文件。
"""
import json
import sys
import subprocess
import time
import argparse
from pathlib import Path
from datetime import datetime

PLAY_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PLAY_DIR.parent.parent
ANALYSIS_DIR = PLAY_DIR / "data" / "analysis"
HEALTH_DIR = PLAY_DIR / "data" / "health"
HEARTBEAT_FILE = HEALTH_DIR / "pipeline_heartbeat.json"
PIDFILE = HEALTH_DIR / "pipeline_daemon.pid"

now = datetime.now()
today_str = now.strftime("%Y%m%d")
is_weekday = now.weekday() < 5
is_trading = is_weekday and (
    (now.hour == 9 and now.minute >= 30) or
    (10 <= now.hour < 11) or
    (now.hour == 11 and now.minute < 30) or
    (13 <= now.hour < 15)
)


def _pid_exists(pid: str) -> bool:
    try:
        pid_int = int(pid)
        subprocess.run(["kill", "-0", str(pid_int)], capture_output=True, check=True)
        return True
    except Exception:
        return False


def main(dry_run: bool = False):
    issues = []
    actions = []

    # ── 1. 检查扫描文件 ──
    files = sorted(ANALYSIS_DIR.glob(f"{today_str}*.json"), reverse=True)
    latest_file = files[0] if files else None

    if is_trading and not latest_file:
        issues.append("今天无任何扫描文件")
        actions.append("重启bot+pipeline")
    elif is_trading and latest_file:
        mtime = datetime.fromtimestamp(latest_file.stat().st_mtime)
        age_min = (now - mtime).total_seconds() / 60
        if age_min > 90:
            issues.append(f"最近扫描在{age_min:.0f}分钟前({latest_file.name[9:13]})")
            actions.append("重启bot+pipeline")
        elif age_min > 45:
            issues.append(f"最近扫描在{age_min:.0f}分钟前(略久)")

    # ── 2. 检查飞书Bot ──
    try:
        import urllib.request
        resp = urllib.request.urlopen("http://localhost:8080/health", timeout=5)
        health = json.loads(resp.read())
        if health.get("status") != "ok":
            issues.append("飞书Bot状态异常")
            actions.append("重启bot")
    except Exception as e:
        issues.append(f"飞书Bot不可达({e})")
        actions.append("重启bot")

    # ── 3. 检查 pipeline daemon 心跳 ──
    daemon_ok = False
    daemon_pid = None
    if PIDFILE.exists():
        daemon_pid = PIDFILE.read_text().strip()

    if HEARTBEAT_FILE.exists():
        try:
            hb = json.loads(HEARTBEAT_FILE.read_text())
            hb_ts = hb.get("epoch", 0)
            hb_pid = str(hb.get("pid", ""))
            age_sec = time.time() - hb_ts
            if age_sec <= 300 and _pid_exists(hb_pid):
                daemon_ok = True
            else:
                if not _pid_exists(hb_pid):
                    issues.append(f"pipeline daemon(pid={hb_pid}) 已不存在")
                else:
                    issues.append(f"pipeline daemon 心跳超时({age_sec:.0f}s)")
                actions.append("重启pipeline")
        except Exception as e:
            issues.append(f"心跳文件解析失败({e})")
            actions.append("重启pipeline")
    else:
        if is_trading:
            issues.append("pipeline daemon 心跳文件不存在")
            actions.append("重启pipeline")

    # 只杀一次性 pipeline.py（非 daemon）且运行超 600s 的遗留进程
    result = subprocess.run(
        ["ps", "-eo", "pid,etimes,args"],
        capture_output=True, text=True, timeout=5
    )
    stuck_pids = []
    for line in result.stdout.split("\n"):
        if "pipeline.py" in line and "--daemon" not in line and "--from-file" not in line:
            parts = line.strip().split()
            if len(parts) >= 2:
                try:
                    runtime = int(parts[1])
                    if runtime > 600:
                        stuck_pids.append(parts[0])
                except Exception:
                    pass

    if stuck_pids:
        issues.append(f"一次性 pipeline 进程({','.join(stuck_pids)})运行超时")
        if not dry_run:
            for pid in stuck_pids:
                subprocess.run(["kill", "-9", pid], capture_output=True)
        actions.append("已杀超时一次性进程")

    # ── 4. 执行修复 ──
    def restart_bot():
        if dry_run:
            return "✅ [dry-run] 飞书Bot将重启"
        subprocess.run(["fuser", "-k", "8080/tcp"], capture_output=True, timeout=10)
        time.sleep(2)
        subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "feishu_bot.main:app",
             "--host", "0.0.0.0", "--port", "8080"],
            cwd=str(PROJECT_DIR)
        )
        return "✅ 飞书Bot已重启"

    def restart_pipeline():
        if dry_run:
            return "✅ [dry-run] pipeline daemon 将重启"
        # 先尝试杀掉旧 daemon
        if daemon_pid and _pid_exists(daemon_pid):
            subprocess.run(["kill", "-9", daemon_pid], capture_output=True)
            time.sleep(1)
        subprocess.Popen(
            [sys.executable, "plays/limit_up/pipeline.py", "--daemon"],
            cwd=str(PROJECT_DIR)
        )
        return "✅ pipeline daemon 已重启"

    if actions:
        print(f"⚠️ [{now.strftime('%H:%M')}] 巡检发现问题 ({len(issues)}项)")
        for i in issues:
            print(f"  · {i}")

        if "重启bot+pipeline" in actions or "重启bot" in actions:
            print(f"\n→ 执行: {restart_bot()}")

        if "重启bot+pipeline" in actions or "重启pipeline" in actions:
            print(f"\n→ 执行: {restart_pipeline()}")

        if "已杀超时一次性进程" in actions:
            print("→ 已清理超时一次性 pipeline 进程")

        print(f"\n✅ 已完成 {len([a for a in actions if a])}项修复")
    else:
        status = "非交易时段" if not is_trading else "正常运行"
        if latest_file:
            mtime = datetime.fromtimestamp(latest_file.stat().st_mtime)
            age_m = (now - mtime).total_seconds() / 60
            print(f"✅ [{now.strftime('%H:%M')}] {status} - 最新扫描:{latest_file.name[9:13]}({age_m:.0f}分钟前)")
        elif daemon_ok:
            print(f"✅ [{now.strftime('%H:%M')}] {status} - daemon心跳正常")
        else:
            print(f"✅ [{now.strftime('%H:%M')}] {status}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="limit_up 健康巡检")
    parser.add_argument("--dry-run", action="store_true", help="只检查不执行修复")
    args = parser.parse_args()
    main(dry_run=args.dry_run)
