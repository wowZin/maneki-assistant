#!/usr/bin/env python3
"""一次性清理 — 将 plays/limit_up/data/ 下的残留文件 mv 到 wiki/raw/limit-up/。

旧的 _sync_raw_data（main 分支）用 copy2 且只取最后 2 轮，导致大量分析数据保留在
data/analysis 下而未被同步到 wiki/raw。这个脚本做一次性清理。

用法：
    python3 wiki/gc_stale_data.py          # 清理所有非今日残留
    python3 wiki/gc_stale_data.py --dry    # 只看不动
"""
import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
PLAY_DATA = PROJECT_DIR / "plays" / "limit_up" / "data"
RAW_ROOT = PROJECT_DIR / "wiki" / "raw" / "limit-up"

# 需要清理的子目录
KINDS = ("analysis", "pushed", "signals", "reports", "weights")


def gc(dry_run: bool = False) -> int:
    today = datetime.now().strftime("%Y%m%d")
    total_moved = 0
    skipped_today = 0
    skipped_exists = 0

    for kind in KINDS:
        src_dir = PLAY_DATA / kind
        if not src_dir.exists():
            continue
        dst_dir = RAW_ROOT / kind
        dst_dir.mkdir(parents=True, exist_ok=True)

        for f in sorted(list(src_dir.glob("*.json")) + list(src_dir.glob("*.md"))):
            # 提取文件名的日期前缀：YYYYMMDD_HHMM → YYYYMMDD
            stem = f.stem
            parts = stem.split("_")
            date_prefix = parts[0] if parts else ""

            if len(date_prefix) != 8 or not date_prefix.isdigit():
                continue  # 非日期格式的文件（如 v2_xxx），跳过
            if date_prefix == today:
                skipped_today += 1  # 今日文件留给 _relocate_raw_data
                continue

            dst = dst_dir / f.name
            if dst.exists():
                # 目标已存在 → 说明旧 compile 已处理过（copy2 遗留），删源文件即可
                f.unlink()
                skipped_exists += 1
                continue

            if dry_run:
                print(f"  [dry] {kind}/{f.name} → wiki/raw/limit-up/{kind}/")
            else:
                shutil.move(str(f), str(dst))
                print(f"  [mv] {kind}/{f.name} → wiki/raw/limit-up/{kind}/")
            total_moved += 1

    print(f"\n结果：移除了 {total_moved} 个文件{'（模拟）' if dry_run else ''}")
    print(f"  跳过今日文件: {skipped_today}")
    print(f"  目标已存在·删源: {skipped_exists}")
    return total_moved


def main():
    parser = argparse.ArgumentParser(description="清理 data/ 下残留文件到 wiki/raw/")
    parser.add_argument("--dry", action="store_true", help="模拟运行，不实际移动")
    args = parser.parse_args()

    print(f"📦 清理残留数据 — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"源: {PLAY_DATA}")
    print(f"目标: {RAW_ROOT}")
    print(f"{'[模拟模式]' if args.dry else '[执行模式]'}")
    print()

    gc(dry_run=args.dry)


if __name__ == "__main__":
    main()
