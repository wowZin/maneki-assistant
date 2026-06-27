"""全因子扫描评估 — 测试所有候选因子的预测力并进行排名。

使用方法:
    python plays/limit_up/backtest/evaluate_all_factors.py

输出:
    - 终端：每个因子的 IC / chasing / Precision@K
    - out/all_factors_report.md：完整 markdown 报告
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from .evaluate_factor import load_panel, evaluate_factor, format_report
from .factor_lib import STANDALONE_FACTORS, ADJUSTMENT_FACTORS, GUARDRAIL_FACTORS, factor_chasing_guardrail_v2

OUT = Path(__file__).resolve().parent / "out"
OUT.mkdir(exist_ok=True)

DIMS = ["fundamental", "technical", "fundflow", "sentiment", "shortterm"]
LABELS = ["fwd_ret_1", "fwd_ret_3", "fwd_max_3", "hit_limit_3"]


def evaluate_standalone(panel: pd.DataFrame, name: str, fn) -> dict:
    """评估独立因子（作为 score）。"""
    return evaluate_factor(panel, fn, name, base_col=None, combine="replace")


def evaluate_adjustment(panel: pd.DataFrame, name: str, fn, base_dim: str) -> dict:
    """评估调整因子（加到维度分上）。"""
    return evaluate_factor(panel, fn, name, base_col=base_dim, combine="add")


def evaluate_all(panel: pd.DataFrame) -> list[dict]:
    """评估全部因子，返回排序后的结果列表。"""
    results = []

    # 1. Baseline: 各维度原始分
    for dim in DIMS:
        r = evaluate_factor(panel, lambda row, d=dim: row.get(d, 0), dim, combine="replace")
        r["category"] = "baseline"
        results.append(r)

    # 2. Baseline: current total
    r = evaluate_factor(panel, lambda row: row.get("total", 0), "total_current", combine="replace")
    r["category"] = "baseline"
    results.append(r)

    # 3. 独立因子
    for name, fn in STANDALONE_FACTORS.items():
        try:
            r = evaluate_standalone(panel, name, fn)
            r["category"] = "standalone"
            results.append(r)
        except Exception as e:
            print(f"  [SKIP] {name}: {e}")

    # 4. 调整因子
    for name, fn in ADJUSTMENT_FACTORS.items():
        base = name.split("_")[0]  # technical_anti_chasing → technical
        if base not in DIMS:
            base = "technical"
        try:
            r = evaluate_adjustment(panel, name, fn, base)
            r["category"] = "adjustment"
            results.append(r)
        except Exception as e:
            print(f"  [SKIP] {name}: {e}")

    return results


def rank_factors(results: list[dict]) -> pd.DataFrame:
    """将结果按 hit_limit_3 RankIC 排序。"""
    rows = []
    for r in results:
        ic = r.get("overall_ic", {})
        chasing = r.get("chasing_ic", {})
        pk = r.get("precision_at_k", {})

        row = {
            "factor": r["factor"],
            "category": r.get("category", ""),
            "n": r.get("n", 0),
            "ic_hit_limit_3": ic.get("hit_limit_3", {}).get("rank_ic"),
            "ic_fwd_ret_3": ic.get("fwd_ret_3", {}).get("rank_ic"),
            "ic_fwd_ret_1": ic.get("fwd_ret_1", {}).get("rank_ic"),
            "ic_fwd_max_3": ic.get("fwd_max_3", {}).get("rank_ic"),
            "ic_trailing_10": chasing.get("trailing_10"),
            "chasing_score": r.get("chasing_score"),
            "hit@10": pk.get("hit_limit_3@10"),
            "hit@20": pk.get("hit_limit_3@20"),
            "hit@50": pk.get("hit_limit_3@50"),
            "fwd3@10": pk.get("fwd_ret_3@10"),
            "fwd3@20": pk.get("fwd_ret_3@20"),
            "fwd3@50": pk.get("fwd_ret_3@50"),
        }
        rows.append(row)

    df = pd.DataFrame(rows).sort_values("ic_hit_limit_3", ascending=False)
    return df


def generate_report(panel: pd.DataFrame, ranking: pd.DataFrame) -> str:
    """生成完整的 markdown 评估报告。"""
    lines = []
    w = lines.append

    w("# 全因子扫描评估报告\n")
    w(f"- 样本：{len(panel)} 条记录，{panel.code.nunique()} 只股票，{panel.date.nunique()} 个交易日")
    w(f"- 基线 hit_limit_3 均值：{panel['hit_limit_3'].mean():.4f}")
    w(f"- 基线 fwd_ret_3 均值：{panel['fwd_ret_3'].mean():.4f}\n")

    w("## 因子排名（按 hit_limit_3 RankIC 降序）\n")
    display_cols = [
        "factor", "category", "ic_hit_limit_3", "ic_fwd_ret_3",
        "ic_fwd_max_3", "ic_trailing_10", "chasing_score",
        "hit@10", "hit@20", "fwd3@10", "fwd3@20",
    ]
    w(ranking[display_cols].round(4).to_markdown(index=False))
    w("")

    # 最佳因子 TOP 10
    w("## TOP 10 最佳因子详情\n")
    for _, row in ranking.head(10).iterrows():
        w(f"### {row['factor']}\n")
        w(f"- IC hit_limit_3: {row['ic_hit_limit_3']:.4f}")
        w(f"- IC fwd_ret_3: {row['ic_fwd_ret_3']:.4f}")
        w(f"- Chasing score: {row['chasing_score']:.4f}")
        w(f"- hit@10: {row['hit@10']}, hit@20: {row['hit@20']}")
        w(f"- fwd3@10: {row['fwd3@10']:.4f}, fwd3@20: {row['fwd3@20']:.4f}")
        w("")

    # 对比：old total vs best standalone
    w("## 对比：当前 total vs 最佳新因子\n")
    old_total = ranking[ranking["factor"] == "total_current"]
    if not old_total.empty:
        old = old_total.iloc[0]
        w(f"### 当前 total")
        w(f"- IC hit_limit_3: {old['ic_hit_limit_3']:.4f}")
        w(f"- hit@10: {old['hit@10']}, hit@20: {old['hit@20']}")
        w("")

    best_new = ranking[ranking["category"] == "standalone"].head(1)
    if not best_new.empty:
        b = best_new.iloc[0]
        w(f"### 最佳新因子: {b['factor']}")
        w(f"- IC hit_limit_3: {b['ic_hit_limit_3']:.4f}")
        w(f"- hit@10: {b['hit@10']}, hit@20: {b['hit@20']}")
        w(f"- 改善: IC {b['ic_hit_limit_3'] - old['ic_hit_limit_3']:.4f}")
        w("")

    return "\n".join(lines)


def main():
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

    # 优先使用 enriched panel
    enriched_path = OUT / "panel_enriched.csv"
    if enriched_path.exists():
        print(f"使用 enriched panel: {enriched_path}")
        panel = pd.read_csv(enriched_path)
    else:
        print("使用原始 panel (无衍生特征)")
        panel = load_panel()

    print(f"Panel: {len(panel)} rows, {len(panel.columns)} cols")
    print(f"评估 {len(STANDALONE_FACTORS) + len(ADJUSTMENT_FACTORS) + 6} 个因子...\n")

    results = evaluate_all(panel)
    ranking = rank_factors(results)

    # 终端输出
    print("\n=== 因子排名 TOP 20 ===\n")
    cols = ["factor", "category", "ic_hit_limit_3", "ic_fwd_ret_3", "chasing_score", "hit@10", "hit@20"]
    print(ranking[cols].head(20).round(4).to_markdown(index=False))

    # 写报告
    report = generate_report(panel, ranking)
    report_path = OUT / "all_factors_report.md"
    report_path.write_text(report, encoding="utf-8")
    print(f"\n完整报告: {report_path}")

    # 输出 ranking csv 供后续使用
    ranking.to_csv(OUT / "factor_ranking.csv", index=False)
    print(f"因子排名 CSV: {OUT / 'factor_ranking.csv'}")

    return ranking


if __name__ == "__main__":
    main()
