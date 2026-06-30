#!/usr/bin/env python3
"""评分验证脚本 — 拆纬度前后对比用

用法:
  # 1. 保存 baseline（改代码前跑）
  python plays/limit_up/verify.py --save baseline.json

  # 2. 改完代码后跑对比
  python plays/limit_up/verify.py --check baseline.json

  # 3. 只看当前结果（不保存不对比）
  python plays/limit_up/verify.py

  # 4. 指定股票
  python plays/limit_up/verify.py --codes 000518.SZ,603893.SH,000001.SZ

  # 5. 从推送/分析文件读取股票列表
  python plays/limit_up/verify.py --from-file data/pushed/20260528_1130.json
"""

import json
import sys
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_DIR))
sys.path.insert(0, str(PROJECT_DIR / "scripts"))

DIMS = ["fundamental", "technical", "fundflow", "sentiment", "shortterm"]


def _discover_today_stocks() -> list[tuple[str, str, str]]:
    """自动发现当天推送的股票列表"""
    from datetime import datetime
    today = datetime.now().strftime("%Y%m%d")
    data_dir = Path(__file__).resolve().parent / "data"

    # 优先从 pushed/ 目录读取当天推送记录
    pushed_dir = data_dir / "pushed"
    pushed_files = sorted(pushed_dir.glob(f"{today}*.json")) if pushed_dir.exists() else []
    if pushed_files:
        latest = pushed_files[-1]
        items = json.loads(latest.read_text())
        if isinstance(items, list):
            codes = [(it.get("code", ""), it.get("name", ""), "推送")
                     for it in items if it.get("code")]
            if codes:
                return codes

    # 兜底：从 analysis/ 目录读取当天最新分析结果
    analysis_dir = data_dir / "analysis"
    analysis_files = sorted(analysis_dir.glob(f"{today}*.json")) if analysis_dir.exists() else []
    if analysis_files:
        latest = analysis_files[-1]
        items = json.loads(latest.read_text())
        if isinstance(items, list):
            codes = [(it.get("code", ""), it.get("name", ""), "分析")
                     for it in items if it.get("code")]
            if codes:
                return codes

    return []


def parse_stocks_from_args(args: list[str]) -> list[tuple[str, str, str]]:
    """从命令行参数或文件解析股票列表"""
    codes = []
    for i, arg in enumerate(args):
        if arg == "--codes" and i + 1 < len(args):
            for c in args[i + 1].split(","):
                c = c.strip()
                codes.append((c, c, ""))
            return codes
        if arg == "--from-file" and i + 1 < len(args):
            path = Path(args[i + 1])
            if not path.is_absolute():
                path = PROJECT_DIR / "plays" / "limit_up" / path
            if path.exists():
                items = json.loads(path.read_text())
                if isinstance(items, list):
                    for item in items:
                        code = item.get("code", "")
                        name = item.get("name", "")
                        codes.append((code, name, ""))
            return codes
    return _discover_today_stocks()


def run_all_scores(stocks: list):
    """跑所有股票的5维度评分"""
    # 动态导入（确保用的是当前代码）
    sys.path.insert(0, str(PROJECT_DIR / "plays" / "limit_up"))
    from plays.limit_up.pipeline import score_fundamental, score_technical, score_fundflow, score_sentiment
    from plays.limit_up.strategies.shortterm import score_shortterm

    funcs = {
        "fundamental": score_fundamental,
        "technical": score_technical,
        "fundflow": score_fundflow,
        "sentiment": score_sentiment,
        "shortterm": score_shortterm,
    }

    results = {}
    for code, name, tag in stocks:
        stock_result = {"name": name, "tag": tag, "scores": {}, "reasons": {}, "time": ""}
        for dim in DIMS:
            fn = funcs[dim]
            try:
                t0 = time.time()
                s, r = fn(code)
                elapsed = round(time.time() - t0, 2)
                stock_result["scores"][dim] = s
                stock_result["reasons"][dim] = r
                stock_result["time"] = elapsed
            except Exception as e:
                stock_result["scores"][dim] = -1
                stock_result["reasons"][dim] = f"ERROR: {e}"
        results[code] = stock_result
    return results


def print_results(results: dict):
    """打印结果"""
    print(f"\n{'='*90}")
    print(f"评分验证 — {len(results)} 只股票 × {len(DIMS)} 维度")
    print(f"{'='*90}")

    for code, data in results.items():
        print(f"\n📊 {code} {data['name']} ({data['tag']})")
        for dim in DIMS:
            s = data["scores"].get(dim, "?")
            r = (data["reasons"].get(dim, "") or "")[:60]
            print(f"  {dim:<12} {s:>5}  {r}")
        if data.get("time"):
            print(f"  {'耗时':<12} {data['time']}s")


def compare(baseline: dict, current: dict) -> bool:
    """对比 baseline 和当前结果"""
    all_match = True
    diffs = []

    for code in baseline:
        if code not in current:
            diffs.append(f"{code}: 缺失")
            all_match = False
            continue

        b = baseline[code]
        c = current[code]

        for dim in DIMS:
            bs = b["scores"].get(dim)
            cs = c["scores"].get(dim)
            br = (b["reasons"].get(dim) or "")[:30]
            cr = (c["reasons"].get(dim) or "")[:30]

            if bs != cs:
                diffs.append(f"{code} {dim}: score {bs} → {cs}  |  {br} → {cr}")
                all_match = False
            elif br != cr:
                # reason 轻微差异可能正常（如时间戳），只记录不报错
                pass

    if all_match:
        print(f"\n✅ 完全一致！{len(baseline)} 只股票 × {len(DIMS)} 维度全部吻合")
    else:
        print(f"\n❌ 发现 {len(diffs)} 处差异:")
        for d in diffs:
            print(f"  {d}")

    return all_match


def main():
    # 跳过脚本名，取剩余参数
    raw_args = sys.argv[1:]

    mode = "run"
    baseline_path = None
    for arg in raw_args:
        if arg == "--save":
            mode = "save"
        elif arg == "--check":
            mode = "check"
        elif not arg.startswith("--") and not arg.startswith("-"):
            # 不是 flag 也不是 --codes/--from-file 的值（那些由 parse 内部消费）
            pass

    # 解析股票列表
    test_stocks = parse_stocks_from_args(raw_args)

    # 查找 --save/--check 后的文件路径
    for i, arg in enumerate(raw_args):
        if arg in ("--save", "--check") and i + 1 < len(raw_args):
            candidate = raw_args[i + 1]
            if not candidate.startswith("--"):
                baseline_path = candidate

    if mode == "save":
        print(f"📝 保存 baseline ({len(test_stocks)}只股票)...")
        results = run_all_scores(test_stocks)
        path = baseline_path or "baseline.json"
        with open(path, "w") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        print_results(results)
        print(f"\n✅ baseline 已保存: {path}")
        print(f"   MD5: {hash(json.dumps(results, sort_keys=True))}")

    elif mode == "check":
        path = baseline_path or "baseline.json"
        if not Path(path).exists():
            print(f"❌ baseline 文件不存在: {path}")
            return

        with open(path) as f:
            baseline = json.load(f)
        print(f"📖 读取 baseline: {path} ({len(baseline)} 只股票)")

        print(f"\n🔄 运行当前代码 ({len(test_stocks)}只股票)...")
        current = run_all_scores(test_stocks)

        print("\n📊 当前结果:")
        print_results(current)

        print("\n🔍 对比中...")
        match = compare(baseline, current)
        if not match:
            sys.exit(1)

    else:
        print(f"📊 评分验证 ({len(test_stocks)}只股票)...")
        results = run_all_scores(test_stocks)
        print_results(results)


if __name__ == "__main__":
    main()
