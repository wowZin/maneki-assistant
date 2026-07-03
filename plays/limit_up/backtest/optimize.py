#!/usr/bin/env python3
"""
权重搜索：在训练期数据上搜索最优五维度权重

用法:
    python plays/limit_up/backtest/optimize.py --train train_20260601_20260620.json
    python plays/limit_up/backtest/optimize.py --start 20260601 --end 20260620 --sample 10
"""
import argparse
import json
import sys
import itertools
from datetime import datetime
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_DIR))


# 默认权重搜索范围
DEFAULT_WEIGHT_RANGE = [0.1, 0.3, 0.5, 0.7, 1.0, 1.5, 2.0, 2.5, 3.0]
DEFAULT_GAP_THRESHOLDS = [0.85, 0.88, 0.90, 0.92, 0.95]


def load_training_data(path: str | Path) -> list[dict]:
    """加载训练期分析结果"""
    with open(path) as f:
        return json.load(f)


def _run_pipeline():
    """如果没给 --train，先跑 analyze"""
    pass


def optimize(records: list[dict], weight_ranges: dict[str, list[float]] | None = None,
             gap_thresholds: list[float] | None = None) -> list[dict]:
    """
    搜索最优权重组合。

    Args:
        records: analyze.py 输出的训练记录 [{code, date, scores, total, ...}]
        weight_ranges: 各维度搜索范围，默认全部使用 DEFAULT_WEIGHT_RANGE

    Returns:
        按综合评分排序的权重组合列表 [{weights, gap, hit_rate, score, ...}]
    """
    if weight_ranges is None:
        dims = ["fundamental", "technical", "fundflow", "sentiment", "shortterm"]
        weight_ranges = {d: DEFAULT_WEIGHT_RANGE for d in dims}
    if gap_thresholds is None:
        gap_thresholds = DEFAULT_GAP_THRESHOLDS

    # 预计算每只股的维度分
    for rec in records:
        rec["_scores_arr"] = {k: v for k, v in rec.get("scores", {}).items()}

    results = []
    total_combos = 0

    dim_names = list(weight_ranges.keys())

    # 生成所有权重组合（笛卡尔积）
    weight_values = [weight_ranges[d] for d in dim_names]
    for weights_tuple in itertools.product(*weight_values):
        weights = dict(zip(dim_names, weights_tuple))
        total_combos += 1

        # 对每条训练记录算 total
        totals = []
        for rec in records:
            sc = rec["_scores_arr"]
            dc = [(sc.get(d, 0), weights.get(d, 1.0)) for d in dim_names]
            dc.sort(key=lambda x: x[0] * x[1], reverse=True)
            top3 = dc[:3]
            if sum(w for _, w in top3) == 0:
                continue
            total = sum(s * w for s, w in top3) / sum(w for _, w in top3)
            totals.append(total)

        if not totals:
            continue

        max_total = max(totals)

        for gap in gap_thresholds:
            threshold = max_total * gap
            captured = sum(1 for t in totals if t >= threshold)
            hit_rate = captured / len(totals) if totals else 0

            results.append({
                "weights": weights,
                "gap": gap,
                "hit_rate": round(hit_rate, 4),
                "captured": captured,
                "total_stocks": len(totals),
                "avg_total": round(sum(totals) / len(totals), 2),
            })

    # 按综合评分排序：hit_rate 越高越好，gap 越低越好（宽松阈值容易进但没意义）
    for r in results:
        r["score"] = round(r["hit_rate"] * 100 - (1 - r["gap"]) * 20, 2)

    results.sort(key=lambda x: x["score"], reverse=True)

    print(f"\n[搜索] 共 {total_combos} 组权重 × {len(gap_thresholds)} 个阈值 = {len(results)} 组合")
    print(f"\n{'排名':<4} {'短线':>5} {'基本面':>5} {'技术面':>5} {'资金流':>5} {'情绪':>5} {'gap':>4} {'命中率':>6} {'得分':>6}")
    print("-" * 55)

    for i, r in enumerate(results[:20]):
        w = r["weights"]
        print(f"{i+1:<4} {w.get('shortterm',0):>5.1f} {w.get('fundamental',0):>5.1f} "
              f"{w.get('technical',0):>5.1f} {w.get('fundflow',0):>5.1f} "
              f"{w.get('sentiment',0):>5.1f} {r['gap']:>.2f} {r['hit_rate']:>.1%} {r['score']:>6.1f}")

    return results


def main():
    parser = argparse.ArgumentParser(description="权重搜索")
    parser.add_argument("--train", help="训练数据文件路径")
    parser.add_argument("--start", help="开始日期（没有 --train 时自动跑）")
    parser.add_argument("--end", help="结束日期")
    parser.add_argument("--sample", type=int, default=10)
    parser.add_argument("--top", type=int, default=20, help="输出前 N 组权重")
    args = parser.parse_args()

    if args.train:
        records = load_training_data(args.train)
    else:
        # analyze.py 已在 2026-07-02 重构中删除；只支持 --train
        print("需要 --train <path>；先在生产 pipeline 或 wiki/raw/limit-up/analysis/ 收集面板")
        return

    print(f"加载 {len(records)} 条训练记录")
    results = optimize(records)

    # 保存最优组合
    out_dir = Path(__file__).resolve().parent / "data"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "optimal_weights.json"
    with open(out_file, "w") as f:
        json.dump(results[:args.top], f, ensure_ascii=False, indent=2)
    print(f"\nTop {args.top} 已保存: {out_file}")


if __name__ == "__main__":
    main()
