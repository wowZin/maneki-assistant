"""综合优化脚本 — 权重搜索 + 阈值优化 + 推送模拟。

目标：
1. 找到最优因子组合和权重，最大化 hit_limit_3 IC 和 Precision@K
2. 找到最优推送阈值，最大化推送命中率和胜率
3. 输出完整对比报告

使用方法:
    PYTHONPATH=. python3 -c "
    import sys; sys.path.insert(0,'.')
    from plays.limit_up.backtest.optimize_system import main
    main()
    "
"""

from __future__ import annotations

import itertools
from pathlib import Path

import numpy as np
import pandas as pd

OUT = Path(__file__).resolve().parent / "out"
OUT.mkdir(exist_ok=True)

DIMS = ["fundamental", "technical", "fundflow", "sentiment", "shortterm"]
LABELS = ["fwd_ret_1", "fwd_ret_3", "fwd_max_3", "hit_limit_3"]


def load_enriched_panel() -> pd.DataFrame:
    """加载 enriched panel 并在内存中计算新因子列。"""
    path = OUT / "panel_enriched.csv"
    if not path.exists():
        raise FileNotFoundError(f"先运行 enrich_panel.py: {path}")
    df = pd.read_csv(path)
    df["date"] = df["date"].astype(str)
    df["code"] = df["code"].astype(str)

    # 计算新因子列
    from .factor_lib import STANDALONE_FACTORS
    for name, fn in STANDALONE_FACTORS.items():
        try:
            df[name] = df.apply(fn, axis=1)
        except Exception as e:
            print(f"  [WARN] factor {name} failed: {e}")

    return df


def compute_rank_ic(score_col: str, label_col: str, df: pd.DataFrame) -> float:
    """计算 RankIC，按 date 分组后取均值。"""
    ics = []
    for d, g in df.groupby("date"):
        valid = g[[score_col, label_col]].dropna()
        if len(valid) < 10:
            continue
        ic = valid[score_col].corr(valid[label_col], method="spearman")
        if not np.isnan(ic):
            ics.append(ic)
    return float(np.mean(ics)) if ics else 0.0


def compute_topk_stats(df: pd.DataFrame, score_col: str, k: int) -> dict:
    """按 score_col 排序取 Top-K，计算 hit rate 和 avg return。"""
    topk = df.nlargest(k, score_col)
    return {
        "n": len(topk),
        "hit_limit_3": topk["hit_limit_3"].mean(),
        "fwd_ret_3": topk["fwd_ret_3"].mean(),
        "fwd_max_3": topk["fwd_max_3"].mean(),
        "win_rate_fwd_3": (topk["fwd_ret_3"] > 0).mean(),
        "score_min": topk[score_col].min(),
        "score_max": topk[score_col].max(),
    }


def compute_threshold_stats(df: pd.DataFrame, score_col: str, threshold: float) -> dict:
    """模拟推送：按阈值筛选后统计。"""
    subset = df[df[score_col] >= threshold]
    if len(subset) == 0:
        return {"push_n": 0, "push_hit": 0, "push_fwd3": 0, "push_win_rate": 0}
    return {
        "push_n": len(subset),
        "push_hit": subset["hit_limit_3"].mean(),
        "push_fwd3": subset["fwd_ret_3"].mean(),
        "push_fwd_max3": subset["fwd_max_3"].mean(),
        "push_win_rate": (subset["fwd_ret_3"] > 0).mean(),
    }


def evaluate_score_column(df: pd.DataFrame, score_col: str, label_cols=None) -> dict:
    """对任意 score 列做完整评估。"""
    label_cols = label_cols or LABELS
    result = {
        "score_col": score_col,
        "n_valid": int(df[score_col].notna().sum()),
    }
    # IC
    for lab in label_cols:
        result[f"ic_{lab}"] = compute_rank_ic(score_col, lab, df)
    result["ic_trailing_10"] = compute_rank_ic(score_col, "trailing_10", df)
    result["chasing_score"] = result["ic_trailing_10"] - result.get("ic_fwd_ret_3", 0)

    # Precision@K
    for k in (10, 20, 50):
        s = compute_topk_stats(df, score_col, k)
        result[f"hit@{k}"] = s["hit_limit_3"]
        result[f"fwd3@{k}"] = s["fwd_ret_3"]
        result[f"winrate@{k}"] = s["win_rate_fwd_3"]

    # Push simulation at various thresholds
    for thr in (25, 30, 35, 40, 45, 50):
        s = compute_threshold_stats(df, score_col, thr)
        result[f"push_n@t{thr}"] = s["push_n"]
        result[f"push_hit@t{thr}"] = s["push_hit"]
        result[f"push_fwd3@t{thr}"] = s["push_fwd3"]
        result[f"push_winrate@t{thr}"] = s["push_win_rate"]

    return result


def grid_search_weights(df: pd.DataFrame, score_cols: list[str]) -> pd.DataFrame:
    """对给定因子列做权重网格搜索。

    搜索范围：每个因子权重 0.0 ~ 1.0，步长 0.25。
    目标：最大化 hit_limit_3 IC。
    """
    n = len(score_cols)
    candidates = []
    values = [0.0, 0.25, 0.5, 0.75, 1.0]

    # 仅搜索 2-4 因子组合（5因子全组合太大）
    # 先评估每个因子的归一化得分（0-100 scale）
    for col in score_cols:
        raw = df[col].dropna()
        if raw.std() > 0:
            df[f"{col}_norm"] = (raw - raw.min()) / (raw.max() - raw.min()) * 100
        else:
            df[f"{col}_norm"] = 50.0

    norm_cols = [f"{c}_norm" for c in score_cols]

    # 对每个因子组合做搜索
    total_combos = 0
    for r in range(2, min(n + 1, 5)):
        for combo in itertools.combinations(range(n), r):
            for weights in itertools.product(values, repeat=r):
                if sum(weights) == 0:
                    continue
                total_combos += 1
                if total_combos > 5000:
                    break

                w_dict = {score_cols[combo[i]]: weights[i] for i in range(r)}
                col_name = "combo_" + "_".join(
                    f"{score_cols[combo[i]]}={weights[i]:.2f}" for i in range(r)
                )

                # 加权组合
                df[col_name] = sum(
                    df[f"{score_cols[combo[i]]}_norm"] * weights[i] for i in range(r)
                ) / sum(weights)

                ic_hit = compute_rank_ic(col_name, "hit_limit_3", df)
                ic_fwd3 = compute_rank_ic(col_name, "fwd_ret_3", df)
                ic_trail = compute_rank_ic(col_name, "trailing_10", df)
                tk = compute_topk_stats(df, col_name, 20)

                candidates.append({
                    "factors": w_dict,
                    "score_col": col_name,
                    "ic_hit_limit_3": ic_hit,
                    "ic_fwd_ret_3": ic_fwd3,
                    "ic_trailing_10": ic_trail,
                    "chasing_score": ic_trail - ic_fwd3,
                    "hit@20": tk["hit_limit_3"],
                    "fwd3@20": tk["fwd_ret_3"],
                    "winrate@20": tk["win_rate_fwd_3"],
                })

                # 清理临时列
                del df[col_name]
            if total_combos > 5000:
                break
        if total_combos > 5000:
            break

    # 清理归一化列
    for c in norm_cols:
        del df[c]

    return pd.DataFrame(candidates).sort_values("ic_hit_limit_3", ascending=False)


def simulate_push_rules(df: pd.DataFrame, score_col: str, configs: list[dict]) -> pd.DataFrame:
    """模拟多套推送规则的表现。"""
    rows = []
    for cfg in configs:
        subset = df.copy()
        # 应用规则
        mask = subset[score_col] >= cfg.get("min_score", 30)
        if "min_shortterm" in cfg and "shortterm" in subset.columns:
            mask &= subset["shortterm"] >= cfg["min_shortterm"]
        if "max_trailing_10" in cfg:
            mask &= subset["trailing_10"].fillna(0) <= cfg["max_trailing_10"]
        if "min_limit_up_gene" in cfg and "limit_up_gene_composite" in subset.columns:
            mask &= subset["limit_up_gene_composite"] >= cfg["min_limit_up_gene"]
        if "max_position_20d" in cfg and "position_20d" in subset.columns:
            mask &= subset["position_20d"].fillna(0.5) <= cfg["max_position_20d"]

        pushed = subset[mask]
        n = len(pushed)
        rows.append({
            **cfg,
            "push_n": n,
            "push_hit": pushed["hit_limit_3"].mean() if n > 0 else 0,
            "push_fwd3": pushed["fwd_ret_3"].mean() if n > 0 else 0,
            "push_fwd_max3": pushed["fwd_max_3"].mean() if n > 0 else 0,
            "push_win_rate": (pushed["fwd_ret_3"] > 0).mean() if n > 0 else 0,
        })

    return pd.DataFrame(rows).sort_values("push_hit", ascending=False)


def main():
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

    print("=" * 70)
    print("综合优化：权重搜索 + 阈值优化 + 推送模拟")
    print("=" * 70)

    df = load_enriched_panel()
    print(f"\n[数据] {len(df)} rows, {len(df.columns)} cols")
    print(f"  日期范围: {df.date.min()} - {df.date.max()}")
    print(f"  基线 hit_limit_3: {df['hit_limit_3'].mean():.4f}")
    print(f"  基线 fwd_ret_3: {df['fwd_ret_3'].mean():.4f}")

    # ── 1. 各列完整评估 ──
    print("\n" + "=" * 70)
    print("1. 候选因子列完整评估")
    print("=" * 70)

    candidate_cols = [
        "total",  # 当前总分
        "shortterm",
        "technical",
        "fundamental",
        "sentiment",
        "fundflow",
        "new_total_v2",
        "limit_up_gene_composite",
        "limit_up_gene_20d",
        "limit_up_gene_60d",
        "pullback_from_peak",
        "sentiment_contrarian",
        "vol_expansion_quality",
        "reversal_signal",
    ]

    eval_rows = []
    for col in candidate_cols:
        if col in df.columns:
            r = evaluate_score_column(df, col)
            eval_rows.append(r)

    eval_df = pd.DataFrame(eval_rows).sort_values("ic_hit_limit_3", ascending=False)
    display_cols = [
        "score_col", "ic_hit_limit_3", "ic_fwd_ret_3",
        "ic_trailing_10", "chasing_score",
        "hit@10", "hit@20", "fwd3@10", "fwd3@20",
        "winrate@10", "winrate@20",
    ]
    print(eval_df[display_cols].round(4).to_markdown(index=False))

    # ── 2. 权重搜索 ──
    print("\n" + "=" * 70)
    print("2. 权重网格搜索（最大化 hit_limit_3 IC）")
    print("=" * 70)

    search_cols = [
        "shortterm", "limit_up_gene_composite", "technical",
        "pullback_from_peak", "sentiment_contrarian",
    ]
    search_cols = [c for c in search_cols if c in df.columns]
    print(f"搜索因子: {search_cols}")

    grid_results = grid_search_weights(df, search_cols)
    print(f"搜索组合数: {len(grid_results)}")

    top_grid = grid_results.head(15)
    print("\nTOP 15 权重组合:")
    gcols = ["factors", "ic_hit_limit_3", "ic_fwd_ret_3", "chasing_score", "hit@20", "fwd3@20", "winrate@20"]
    print(top_grid[gcols].round(4).to_markdown(index=False))

    # ── 3. 推送规则优化 ──
    print("\n" + "=" * 70)
    print("3. 推送规则模拟")
    print("=" * 70)

    # 使用 new_total_v2 或 best combo
    best_score_col = "new_total_v2"
    if best_score_col not in df.columns:
        best_score_col = "total"

    push_configs = [
        # 名称, 配置
        {"name": "当前规则", "min_score": 30},
        {"name": "当前+追高限制", "min_score": 30, "max_trailing_10": 0.15},
        {"name": "高确信(总分≥35)", "min_score": 35},
        {"name": "高确信+追高限制", "min_score": 35, "max_trailing_10": 0.15},
        {"name": "高确信+追高+涨停基因", "min_score": 35, "max_trailing_10": 0.15, "min_limit_up_gene": 6},
        {"name": "严格(总分≥40)", "min_score": 40},
        {"name": "严格+追高限制", "min_score": 40, "max_trailing_10": 0.15},
        {"name": "严格+追高+涨停基因", "min_score": 40, "max_trailing_10": 0.15, "min_limit_up_gene": 6},
        {"name": "极严格(≥45+低追高)", "min_score": 45, "max_trailing_10": 0.10},
        {"name": "短线强+低追高", "min_score": 30, "min_shortterm": 50, "max_trailing_10": 0.15},
        {"name": "短线强+涨停基因+低追高", "min_score": 30, "min_shortterm": 50, "max_trailing_10": 0.15, "min_limit_up_gene": 6},
    ]

    push_results = simulate_push_rules(df, best_score_col, push_configs)
    print(f"\n使用评分列: {best_score_col}")
    pcols = ["name", "push_n", "push_hit", "push_fwd3", "push_fwd_max3", "push_win_rate"]
    print(push_results[pcols].round(4).to_markdown(index=False))

    # ── 4. 对比旧规则 ──
    print("\n" + "=" * 70)
    print("4. 新旧对比")
    print("=" * 70)

    old_total = evaluate_score_column(df, "total")
    new_total_v2 = evaluate_score_column(df, "new_total_v2") if "new_total_v2" in df.columns else None

    print("\n| 指标 | 旧 total | 新 total_v2 | 改善 |")
    print("|------|----------|-------------|------|")
    for key in ["ic_hit_limit_3", "ic_fwd_ret_3", "hit@10", "hit@20", "fwd3@10", "fwd3@20", "winrate@10", "winrate@20"]:
        old_v = old_total.get(key, 0)
        new_v = new_total_v2.get(key, 0) if new_total_v2 else 0
        diff = new_v - old_v
        sign = "+" if diff > 0 else ""
        print(f"| {key} | {old_v:.4f} | {new_v:.4f} | {sign}{diff:.4f} |")

    # ── 5. 输出优化结果 ──
    print("\n" + "=" * 70)
    print("5. 保存结果")
    print("=" * 70)

    # 保存评估结果
    eval_df.to_csv(OUT / "score_evaluation.csv", index=False)
    grid_results.to_csv(OUT / "weight_grid_search.csv", index=False)
    push_results.to_csv(OUT / "push_simulation.csv", index=False)

    # 生成报告
    report = generate_optimization_report(df, eval_df, grid_results, push_results, old_total, new_total_v2)
    report_path = OUT / "optimization_report.md"
    report_path.write_text(report, encoding="utf-8")
    print(f"报告: {report_path}")
    print("完成!")


def generate_optimization_report(df, eval_df, grid_results, push_results, old_total, new_total_v2) -> str:
    """生成 markdown 优化报告。"""
    lines = []
    w = lines.append

    w("# 系统优化报告\n")
    w(f"- 样本：{len(df)} 条记录")
    w(f"- 基线 hit_limit_3: {df['hit_limit_3'].mean():.4f}")
    w(f"- 基线 fwd_ret_3: {df['fwd_ret_3'].mean():.4f}\n")

    w("## 1. 因子评估排名\n")
    display_cols = [
        "score_col", "ic_hit_limit_3", "ic_fwd_ret_3",
        "ic_trailing_10", "chasing_score",
        "hit@10", "hit@20", "winrate@10", "winrate@20",
    ]
    w(eval_df[display_cols].round(4).to_markdown(index=False))
    w("")

    w("## 2. 最优权重组合 TOP 10\n")
    gcols = ["factors", "ic_hit_limit_3", "ic_fwd_ret_3", "chasing_score", "hit@20", "fwd3@20", "winrate@20"]
    w(grid_results.head(10)[gcols].round(4).to_markdown(index=False))
    w("")

    w("## 3. 推送规则模拟\n")
    pcols = ["name", "push_n", "push_hit", "push_fwd3", "push_fwd_max3", "push_win_rate"]
    w(push_results[pcols].round(4).to_markdown(index=False))
    w("")

    w("## 4. 新旧对比\n")
    w("| 指标 | 旧 total | 新 total_v2 | 改善 |")
    w("|------|----------|-------------|------|")
    for key in ["ic_hit_limit_3", "ic_fwd_ret_3", "hit@10", "hit@20", "fwd3@10", "fwd3@20"]:
        old_v = old_total.get(key, 0)
        new_v = new_total_v2.get(key, 0) if new_total_v2 else 0
        diff = new_v - old_v
        sign = "+" if diff > 0 else ""
        w(f"| {key} | {old_v:.4f} | {new_v:.4f} | {sign}{diff:.4f} |")

    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))
    main()
