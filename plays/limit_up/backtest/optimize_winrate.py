"""胜率优化评估 — 针对 fwd_ret_3 优选因子组合。

目标：在 Top-10 推送中胜率从 63% 提升到 73%+。
"""

from __future__ import annotations

import itertools
from pathlib import Path

import numpy as np
import pandas as pd

OUT = Path(__file__).resolve().parent / "out"


def load_v3_panel() -> pd.DataFrame:
    """加载 V3 面板并计算所有因子。"""
    path = OUT / "panel_enriched_v3.csv"
    df = pd.read_csv(path)
    df["date"] = df["date"].astype(str)
    df["code"] = df["code"].astype(str)

    from .factor_lib import STANDALONE_FACTORS
    for name, fn in STANDALONE_FACTORS.items():
        try:
            df[name] = df.apply(fn, axis=1)
        except Exception:
            pass
    return df


def compute_rank_ic(df, score_col, label_col):
    """按日期分组 RankIC。"""
    ics = []
    for d, g in df.groupby("date"):
        valid = g[[score_col, label_col]].dropna()
        if len(valid) < 10:
            continue
        ic = valid[score_col].corr(valid[label_col], method="spearman")
        if not np.isnan(ic):
            ics.append(ic)
    return float(np.mean(ics)) if ics else 0.0


def per_day_topk(df, score_col, k):
    """每日 Top-K。"""
    frames = []
    for d, g in df.groupby("date"):
        top = g.nlargest(min(k, len(g)), score_col)
        frames.append(top)
    return pd.concat(frames, ignore_index=True)


def evaluate_winrate(df, score_cols, ks=(5, 10, 15, 20, 30)):
    """评估多个评分列在各 K 值下的表现。"""
    rows = []
    for col in score_cols:
        if col not in df.columns:
            continue
        row = {"score": col}
        # IC
        row["ic_fwd3"] = compute_rank_ic(df, col, "fwd_ret_3")
        row["ic_hit"] = compute_rank_ic(df, col, "hit_limit_3")
        row["ic_fwd_max3"] = compute_rank_ic(df, col, "fwd_max_3")
        row["ic_trail10"] = compute_rank_ic(df, col, "trailing_10")
        row["chasing"] = row["ic_trail10"] - row["ic_fwd3"]

        for k in ks:
            topk = per_day_topk(df, col, k)
            row[f"k{k}_n"] = len(topk)
            row[f"k{k}_hit"] = topk["hit_limit_3"].mean()
            row[f"k{k}_fwd3"] = topk["fwd_ret_3"].mean()
            row[f"k{k}_fwd_max3"] = topk["fwd_max_3"].mean()
            row[f"k{k}_winrate"] = (topk["fwd_ret_3"] > 0).mean()

        rows.append(row)
    return pd.DataFrame(rows)


def multi_objective_search(df, factor_pool, topk=10):
    """多目标搜索：同时最大化 winrate 和 hit rate。

    对因子池做归一化后网格搜索权重组合。
    """
    # 归一化所有因子到 0-100
    norm_cols = {}
    for col in factor_pool:
        if col not in df.columns:
            continue
        raw = df[col].dropna()
        if raw.std() > 0 and raw.max() > raw.min():
            df[f"{col}_n"] = (raw - raw.min()) / (raw.max() - raw.min()) * 100
            norm_cols[col] = f"{col}_n"
        else:
            norm_cols[col] = col

    candidates = []
    values = [0.0, 0.25, 0.5, 0.75, 1.0]
    cols = list(norm_cols.keys())
    n = len(cols)

    total_combos = 0
    for r in range(2, min(n + 1, 5)):
        for combo in itertools.combinations(range(n), r):
            for weights in itertools.product(values, repeat=r):
                if sum(weights) == 0:
                    continue
                total_combos += 1
                if total_combos > 3000:
                    break

                w_dict = {cols[combo[i]]: weights[i] for i in range(r)}
                name = "opt_" + "_".join(f"{cols[combo[i]]}={weights[i]:.2f}" for i in range(r))

                df[name] = sum(
                    df[norm_cols[cols[combo[i]]]] * weights[i] for i in range(r)
                ) / sum(weights)

                topk_df = per_day_topk(df, name, topk)
                winrate = (topk_df["fwd_ret_3"] > 0).mean()
                hit = topk_df["hit_limit_3"].mean()
                fwd3 = topk_df["fwd_ret_3"].mean()

                # 复合目标：winrate 权重 0.6, hit 权重 0.4
                objective = winrate * 0.6 + hit * 0.4

                candidates.append({
                    "factors": w_dict,
                    "winrate": winrate,
                    "hit": hit,
                    "fwd3": fwd3,
                    "objective": objective,
                })

                del df[name]
            if total_combos > 3000:
                break
        if total_combos > 3000:
            break

    # 清理
    for col in norm_cols.values():
        if col.endswith("_n") and col in df.columns:
            del df[col]

    return pd.DataFrame(candidates).sort_values("objective", ascending=False)


def main():
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

    print("=" * 70)
    print("胜率优化评估")
    print("=" * 70)

    df = load_v3_panel()
    print(f"数据: {len(df)} rows, {len(df.columns)} cols\n")

    # ── 1. 候选评分列评估 ──
    score_cols = [
        "total", "new_total_v2", "balanced_total", "aggressive_total",
        "return_optimized_total", "quality_value_total",
        "shortterm", "fundamental", "technical",
        "circ_mv_tier", "fundamental_quality",
    ]
    score_cols = [c for c in score_cols if c in df.columns]

    print("1. 候选评分列评估 (Top-10 胜率排序)")
    eval_df = evaluate_winrate(df, score_cols)
    eval_df = eval_df.sort_values("k10_winrate", ascending=False)

    display = ["score", "ic_fwd3", "ic_hit", "k10_winrate", "k10_hit", "k10_fwd3",
               "k20_winrate", "k20_hit", "k5_winrate", "k5_hit"]
    print(eval_df[display].round(4).to_markdown(index=False))

    # ── 2. 多目标权重搜索 ──
    print("\n2. 多目标权重搜索 (最大化 0.6×winrate + 0.4×hit)")
    factor_pool = [
        "shortterm", "fundamental", "technical",
        "circ_mv_tier", "fundamental_quality",
        "limit_up_gene_composite", "pullback_from_peak",
        "sentiment_contrarian", "net_mf_signal",
        "turnover_penalty", "volume_ratio_penalty",
    ]
    factor_pool = [c for c in factor_pool if c in df.columns]
    print(f"   因子池: {factor_pool}")

    search_results = multi_objective_search(df, factor_pool, topk=10)
    print(f"   搜索组合数: {len(search_results)}")
    print("\n   TOP 15 组合:")
    top = search_results.head(15)
    for _, row in top.iterrows():
        print(f"   obj={row['objective']:.4f} winrate={row['winrate']:.4f} hit={row['hit']:.4f} fwd3={row['fwd3']:.4f}")
        print(f"     {row['factors']}")

    # ── 3. 最佳组合详细评估 ──
    if len(search_results) > 0:
        best = search_results.iloc[0]
        print(f"\n3. 最佳组合详细评估")
        print(f"   winrate={best['winrate']:.4f}, hit={best['hit']:.4f}, fwd3={best['fwd3']:.4f}")

        # 用最佳权重创建综合评分
        w_dict = best["factors"]
        from .factor_lib import STANDALONE_FACTORS

        # Recompute factors to ensure they exist
        for name, fn in STANDALONE_FACTORS.items():
            try:
                if name not in df.columns:
                    df[name] = df.apply(fn, axis=1)
            except:
                pass

        # Normalize and combine
        for col in w_dict:
            raw = df[col].dropna()
            if raw.std() > 0 and raw.max() > raw.min():
                df[f"{col}_n2"] = (raw - raw.min()) / (raw.max() - raw.min()) * 100

        df["best_combo"] = sum(
            df[f"{c}_n2"] * w for c, w in w_dict.items()
        ) / sum(w_dict.values())

        # Evaluate best combo at all K
        for k in [5, 10, 15, 20, 30]:
            topk = per_day_topk(df, "best_combo", k)
            hit = topk["hit_limit_3"].mean()
            wr = (topk["fwd_ret_3"] > 0).mean()
            fwd3 = topk["fwd_ret_3"].mean()
            fwd_max3 = topk["fwd_max_3"].mean()
            n_per_day = len(topk) / df["date"].nunique()
            print(f"   Top-{k:2d}: daily={n_per_day:.1f} hit={hit:.4f} winrate={wr:.4f} fwd3={fwd3:.4f} fwd_max3={fwd_max3:.4f}")

        # Compare to old baseline
        print(f"\n4. 对比旧推送")
        old_push = df[
            (df["total"] >= 30) & (df["sentiment"] >= 35) &
            (df["fundflow"] >= 35) & (df["total"] >= 40)
        ]
        old_hit = old_push["hit_limit_3"].mean()
        old_wr = (old_push["fwd_ret_3"] > 0).mean()
        old_fwd3 = old_push["fwd_ret_3"].mean()

        best_k10 = per_day_topk(df, "best_combo", 10)
        new_wr = (best_k10["fwd_ret_3"] > 0).mean()
        new_hit = best_k10["hit_limit_3"].mean()
        new_fwd3 = best_k10["fwd_ret_3"].mean()

        print(f"   {'':20s} {'hit':>8s} {'winrate':>8s} {'fwd3':>8s}")
        print(f"   {'旧推送':20s} {old_hit:8.4f} {old_wr:8.4f} {old_fwd3:8.4f}")
        print(f"   {'新Top-10':20s} {new_hit:8.4f} {new_wr:8.4f} {new_fwd3:8.4f}")
        print(f"   {'改善':20s} {new_hit-old_hit:+8.4f} {new_wr-old_wr:+8.4f} {new_fwd3-old_fwd3:+8.4f}")

        wr_improvement = (new_wr - old_wr) * 100
        if wr_improvement >= 10:
            print(f"\n   ✅ 胜率改善 {wr_improvement:.1f}pp ≥ 10pp，目标达成！")
        else:
            print(f"\n   ⚠️ 胜率改善 {wr_improvement:.1f}pp，距目标还差 {10-wr_improvement:.1f}pp")

    # Save results
    eval_df.to_csv(OUT / "winrate_evaluation.csv", index=False)
    search_results.to_csv(OUT / "winrate_search.csv", index=False)
    print(f"\n结果已保存到 {OUT}")


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))
    main()
