#!/usr/bin/env python3
"""
验证期：用真实扫描信号验证优化效果

从 data/analysis/ 加载历史扫描信号，用优化后的权重重新评分，
ScoreGap 推送后计算命中率和胜率。

用法:
    python plays/limit_up/backtest/validate.py --weights optimal_weights.json
    python plays/limit_up/backtest/validate.py --weights optimal_weights.json --start 20260620 --end 20260630
"""
import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from plays.limit_up.pipeline_feishu import push_feishu
from plays.limit_up.backtest.data import fetch_data


def _get_score_field(candidate: dict) -> float:
    """兼容旧 analysis 文件的 total 和新文件的 total_score。"""
    return candidate.get("total_score", candidate.get("total", 0))


def validate(weights_path: str | Path, start: str | None = None, end: str | None = None):
    """用真实扫描信号验证优化效果"""
    # 加载最优权重
    with open(weights_path) as f:
        top_weights = json.load(f)

    best = top_weights[0] if top_weights else None
    if not best:
        print("无有效权重")
        return

    weights = best["weights"]
    gap = best["gap"]
    print(f"\n[验证] 权重: {weights}  gap={gap}")

    # 加载历史扫描信号
    analysis_dir = Path(__file__).resolve().parent.parent / "data" / "analysis"
    scan_files = sorted(analysis_dir.glob("*.json"))

    print(f"找到 {len(scan_files)} 个信号文件")

    # 验证需要的数据（涨停列表 + 日线）
    cache = fetch_data(start or "20260601", end or "20260701")

    total_pushed = 0
    total_hit = 0
    total_win = 0
    push_records = []

    for sf in scan_files:
        fname = sf.name
        date_str = fname[:8]  # YYYYMMDD
        if start and date_str < start:
            continue
        if end and date_str > end:
            continue

        try:
            candidates = json.loads(sf.read_text())
        except Exception:
            continue

        if not candidates or not isinstance(candidates, list):
            continue
        # 跳过空结果文件
        if candidates[0].get("_empty"):
            continue

        # 排序按总分（兼容旧 total 和新 total_score）
        candidates.sort(key=lambda x: _get_score_field(x), reverse=True)
        max_total = _get_score_field(candidates[0])
        threshold = max_total * gap

        pushed = [c for c in candidates if _get_score_field(c) >= threshold][:5]

        if not pushed:
            continue

        # 查当日涨停
        limit_set = _get_limit_set(date_str, cache)
        next_date = _get_next_trade_day(date_str, cache)

        for r in pushed:
            code = r["code"]
            hit = code in limit_set
            win = _check_win(code, date_str, next_date, cache) if next_date else False

            if hit:
                total_hit += 1
            if win:
                total_win += 1

            push_records.append({
                "date": date_str,
                "code": code,
                "name": r.get("name", ""),
                "total_score": _get_score_field(r),
                "hit": hit,
                "win": win,
            })

        total_pushed += len(pushed)

    # 汇总
    hr = total_hit / total_pushed if total_pushed else 0
    wr = total_win / total_pushed if total_pushed else 0
    print(f"\n{'指标':<10} {'值':>8}")
    print("-" * 20)
    print(f"{'推送总数':<10} {total_pushed:>8}")
    print(f"{'命中数':<10} {total_hit:>8}")
    print(f"{'命中率':<10} {hr:>7.1%}")
    print(f"{'胜局数':<10} {total_win:>8}")
    print(f"{'胜率':<10} {wr:>7.1%}")
    print(f"{'综合评分':<10} {(hr*0.5+wr*0.5):>7.1%}")

    # 保存
    out_dir = Path(__file__).resolve().parent / "data"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "validate_result.json"
    with open(out_file, "w") as f:
        json.dump({
            "weights": weights,
            "gap": gap,
            "total_pushed": total_pushed,
            "hit_rate": round(hr, 4),
            "win_rate": round(wr, 4),
            "records": push_records,
        }, f, ensure_ascii=False, indent=2)
    print(f"\n已保存: {out_file}")
    return hr, wr


def _get_limit_set(date: str, cache: dict) -> set[str]:
    """获取当日涨停股集合"""
    rows = cache.get("limit_list_d", {}).get(date, [])
    return {r.get("ts_code", "") for r in rows if str(r.get("limit", "")).upper() == "U"}


def _get_next_trade_day(date: str, cache: dict) -> str | None:
    """获取下一交易日"""
    cal = cache.get("trade_cal", {})
    dates = sorted(cal.keys())
    idx = dates.index(date) if date in dates else -1
    if idx >= 0 and idx + 1 < len(dates):
        next_d = dates[idx + 1]
        if cal[next_d].get("is_open") == 1:
            return next_d
    return None


def _check_win(code: str, buy_date: str, sell_date: str, cache: dict) -> bool:
    """T+1 检查：卖出日收盘 > 买入日收盘 × 1.001"""
    daily = cache.get("daily", {})
    buy_rows = [r for r in daily.get(buy_date, []) if r.get("ts_code") == code]
    sell_rows = [r for r in daily.get(sell_date, []) if r.get("ts_code") == code]
    if not buy_rows or not sell_rows:
        return False
    buy_close = buy_rows[0].get("close", 0)
    sell_close = sell_rows[0].get("close", 0)
    if not buy_close or not sell_close:
        return False
    return float(sell_close) > float(buy_close) * 1.001


def main():
    parser = argparse.ArgumentParser(description="验证期回放")
    parser.add_argument("--weights", default="optimal_weights.json",
                        help="最优权重文件路径（默认 backtest/data/optimal_weights.json）")
    parser.add_argument("--start", help="开始日期（过滤 analysis 文件）")
    parser.add_argument("--end", help="结束日期")
    args = parser.parse_args()

    # 拼接默认路径
    weights_path = Path(args.weights)
    if not weights_path.is_absolute():
        weights_path = Path(__file__).resolve().parent / "data" / args.weights

    if not weights_path.exists():
        print(f"权重文件不存在: {weights_path}")
        return

    validate(weights_path, args.start, args.end)


if __name__ == "__main__":
    main()
