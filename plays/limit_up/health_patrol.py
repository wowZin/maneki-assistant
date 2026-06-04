#!/usr/bin/env python3
"""开盘日健康巡检：检查pipeline/飞书Bot/代理状态，异常时自动重启"""
import json
import sys
import subprocess
import time
from pathlib import Path
from datetime import datetime

PLAY_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PLAY_DIR.parent.parent
ANALYSIS_DIR = PLAY_DIR / "data" / "analysis"
SCRIPTS_DIR = PROJECT_DIR / "scripts"

now = datetime.now()
today_str = now.strftime("%Y%m%d")
is_weekday = now.weekday() < 5
is_trading = is_weekday and (
    (now.hour == 9 and now.minute >= 30) or  
    (10 <= now.hour < 11) or
    (now.hour == 11 and now.minute < 30) or
    (13 <= now.hour < 15)
)

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

# ── 3. 检查卡死的pipeline进程 ──
result = subprocess.run(
    ["ps", "-eo", "pid,etimes,args"],
    capture_output=True, text=True, timeout=5
)
stuck_pids = []
for line in result.stdout.split("\n"):
    if "pipeline.py" in line and "--from-file" not in line:
        parts = line.strip().split()
        if len(parts) >= 2:
            try:
                runtime = int(parts[1])  # 运行秒数
                if runtime > 600:  # 超过10分钟
                    stuck_pids.append(parts[0])
            except Exception:
                pass

if stuck_pids:
    issues.append(f"pipeline进程({','.join(stuck_pids)})运行超时")
    for pid in stuck_pids:
        subprocess.run(["kill", "-9", pid], capture_output=True)
    actions.append("已杀超时进程")

# ── 4. 执行修复 ──
def restart_bot():
    """重启飞书Bot"""
    subprocess.run(["fuser", "-k", "8080/tcp"], capture_output=True, timeout=10)
    time.sleep(2)
    subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "feishu_bot.main:app",
         "--host", "0.0.0.0", "--port", "8080"],
        cwd=str(PROJECT_DIR)
    )
    return "✅ 飞书Bot已重启"

if actions:
    print(f"⚠️ [{now.strftime('%H:%M')}] 巡检发现问题 ({len(issues)}项)")
    for i in issues:
        print(f"  · {i}")
    
    if "重启bot+pipeline" in actions or "重启bot" in actions:
        print(f"\n→ 执行: {restart_bot()}")
    
    if "重启bot+pipeline" in actions:
        print("→ 已杀超时pipeline进程,下一轮cron自动拉起")
    
    print(f"\n✅ 已完成 {len([a for a in actions if a])}项修复")
else:
    status = "非交易时段" if not is_trading else "正常运行"
    if latest_file:
        mtime = datetime.fromtimestamp(latest_file.stat().st_mtime)
        age_m = (now - mtime).total_seconds() / 60
        print(f"✅ [{now.strftime('%H:%M')}] {status} - 最新扫描:{latest_file.name[9:13]}({age_m:.0f}分钟前)")
    else:
        print(f"✅ [{now.strftime('%H:%M')}] {status}")
