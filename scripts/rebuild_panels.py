#!/usr/bin/env python3
"""批量重建历史面板—含 intraday 特征。
用法: python scripts/rebuild_panels.py --start 20260105 --end 20260722 --workers 2
"""
import argparse, sys, time
from pathlib import Path
from datetime import datetime, timedelta
from concurrent.futures import ProcessPoolExecutor, as_completed

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from plays.limit_up.pipeline import _is_trade_day

def build_one(date_str: str) -> str:
    """构建一天的面板，返回状态。"""
    import os, subprocess
    env = os.environ.copy()
    env["_PANEL_DATE"] = date_str
    r = subprocess.run(
        [sys.executable, "-u", str(PROJECT_DIR / "scripts" / "run_panel_builder.py")],
        capture_output=True, text=True, timeout=600, env=env,
    )
    if r.returncode == 0 and "面板已保存" in r.stdout:
        return f"✓ {date_str}"
    return f"✗ {date_str}: {r.stderr[-200:] if r.stderr else r.stdout[-200:]}"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    start = datetime.strptime(args.start, "%Y%m%d")
    end = datetime.strptime(args.end, "%Y%m%d")
    dates = []
    d = start
    while d <= end:
        ds = d.strftime("%Y%m%d")
        if _is_trade_day(ds):
            dates.append(ds)
        d += timedelta(days=1)

    print(f"交易日: {len(dates)} 天 ({args.start}~{args.end})")
    if args.dry_run:
        for ds in dates[:5]: print(f"  {ds}")
        print(f"  ...共 {len(dates)} 天")
        return

    t0 = time.time()
    done, fail = 0, 0
    total = len(dates)
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(build_one, ds): ds for ds in dates}
        for fut in as_completed(futs):
            result = fut.result()
            if result.startswith("✓"): done += 1
            else: fail += 1
            pct = (done + fail) / total
            bar = "█" * int(pct * 20) + "░" * (20 - int(pct * 20))
            elapsed = time.time() - t0
            eta = elapsed / (done + fail) * (total - done - fail) if (done + fail) > 0 else 0
            print(f"\r  [{done+fail}/{total}] {bar} {pct*100:.0f}%  ✓{done} ✗{fail}  {elapsed/60:.0f}m  ETA:{eta/60:.0f}m", end="", flush=True)

    print(f"\n完成: {done}✓ {fail}✗, 耗时 {(time.time()-t0)/60:.0f}min")


if __name__ == "__main__":
    main()
