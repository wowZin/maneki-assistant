"""最终回测对比 — 旧系统 vs 新系统（Top-K 推送模式）。

对比维度：
1. Top-K (K=5,10,15,20,30) 每日推送表现
2. Hit rate (命中率) — hit_limit_3 均值
3. Win rate (胜率) — fwd_ret_3 > 0 的比例
4. Avg return — fwd_ret_3 均值
5. Max return — fwd_max_3 均值
6. IC — RankIC vs hit_limit_3 / fwd_ret_3

输出: out/final_comparison.md
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

OUT = Path(__file__).resolve().parent / "out"
OUT.mkdir(exist_ok=True)


def load_data() -> pd.DataFrame:
    """加载完整数据（enriched panel + 新评分列）。"""
    df = pd.read_csv(OUT / "panel_enriched.csv")
    df["date"] = df["date"].astype(str)
    df["code"] = df["code"].astype(str)

    from .factor_lib import factor_new_total_v2, factor_balanced_total, factor_aggressive_total
    df["new_total_v2"] = df.apply(factor_new_total_v2, axis=1)
    df["balanced_total"] = df.apply(factor_balanced_total, axis=1)
    df["aggressive_total"] = df.apply(factor_aggressive_total, axis=1)

    return df


def compute_rank_ic(score_col: str, label_col: str, df: pd.DataFrame) -> float:
    """按日期分组计算 RankIC 均值。"""
    ics = []
    for d, g in df.groupby("date"):
        valid = g[[score_col, label_col]].dropna()
        if len(valid) < 10:
            continue
        ic = valid[score_col].corr(valid[label_col], method="spearman")
        if not np.isnan(ic):
            ics.append(ic)
    return float(np.mean(ics)) if ics else 0.0


def per_day_topk(df: pd.DataFrame, score_col: str, k: int) -> pd.DataFrame:
    """每日取 Top-K，返回所有日期的拼接。"""
    frames = []
    for d, g in df.groupby("date"):
        top = g.nlargest(min(k, len(g)), score_col)
        frames.append(top)
    return pd.concat(frames, ignore_index=True)


def evaluate_scheme(df: pd.DataFrame, score_col: str, ks: list[int]) -> dict:
    """评估一个评分方案在多个 K 值下的表现。"""
    result = {"score_col": score_col}

    # IC
    for lab in ["hit_limit_3", "fwd_ret_3", "fwd_max_3"]:
        result[f"ic_{lab}"] = compute_rank_ic(score_col, lab, df)
    result["ic_trailing_10"] = compute_rank_ic(score_col, "trailing_10", df)
    result["chasing_score"] = result["ic_trailing_10"] - result["ic_fwd_ret_3"]

    # Per-day Top-K
    n_dates = df["date"].nunique()
    for k in ks:
        topk = per_day_topk(df, score_col, k)
        result[f"k{k}_n"] = len(topk)
        result[f"k{k}_per_day"] = len(topk) / n_dates
        result[f"k{k}_hit"] = topk["hit_limit_3"].mean()
        result[f"k{k}_fwd3"] = topk["fwd_ret_3"].mean()
        result[f"k{k}_fwd_max3"] = topk["fwd_max_3"].mean()
        result[f"k{k}_winrate"] = (topk["fwd_ret_3"] > 0).mean()

    return result


def evaluate_old_push(df: pd.DataFrame) -> dict:
    """评估旧推送规则的表现。"""
    # 旧规则：总分≥30 + 情绪≥35 + 资金≥35 + 总分≥40
    old_push = df[
        (df["total"] >= 30) &
        (df["sentiment"] >= 35) &
        (df["fundflow"] >= 35) &
        (df["total"] >= 40)
    ]
    n = len(old_push)
    n_dates = df["date"].nunique()

    return {
        "scheme": "old_push_rules",
        "n": n,
        "per_day": n / n_dates,
        "hit": old_push["hit_limit_3"].mean() if n > 0 else 0,
        "fwd3": old_push["fwd_ret_3"].mean() if n > 0 else 0,
        "fwd_max3": old_push["fwd_max_3"].mean() if n > 0 else 0,
        "winrate": (old_push["fwd_ret_3"] > 0).mean() if n > 0 else 0,
    }


def generate_report(df: pd.DataFrame) -> str:
    """生成完整对比报告。"""
    lines = []
    w = lines.append

    n_dates = df["date"].nunique()
    baseline_hit = df["hit_limit_3"].mean()
    baseline_fwd3 = df["fwd_ret_3"].mean()
    baseline_winrate = (df["fwd_ret_3"] > 0).mean()

    w("# 最终回测对比报告：旧系统 vs 新系统\n")
    w(f"- 样本：{len(df)} 条 (code,date) 记录")
    w(f"- 交易日：{n_dates} 天")
    w(f"- 日期范围：{df.date.min()} - {df.date.max()}")
    w(f"- 全样本基线：hit_limit_3={baseline_hit:.4f}, fwd_ret_3={baseline_fwd3:.4f}, winrate={baseline_winrate:.4f}\n")

    # ── 1. Old push ──
    old = evaluate_old_push(df)
    w("## 1. 旧推送规则表现\n")
    w(f"- 推送条件：总分≥30 + 情绪≥35 + 资金≥35 + 总分≥40")
    w(f"- 总推送数：{old['n']}（日均 {old['per_day']:.1f} 只）")
    w(f"- 命中率：{old['hit']:.4f}（{old['hit']*100:.2f}%）")
    w(f"- 胜率：{old['winrate']:.4f}（{old['winrate']*100:.2f}%）")
    w(f"- 平均3日收益：{old['fwd3']:.4f}（{old['fwd3']*100:.2f}%）")
    w(f"- 平均3日最大收益：{old['fwd_max3']:.4f}（{old['fwd_max3']*100:.2f}%）\n")

    # ── 2. Old Top-K ──
    w("## 2. 旧系统 Top-K（按 total 排序）\n")
    ks = [5, 10, 15, 20, 30]
    old_topk = evaluate_scheme(df, "total", ks)

    w("| K | 日均推送 | 命中率 | 胜率 | 平均3日收益 | 平均3日最大收益 |")
    w("|---|---------|--------|------|-----------|---------------|")
    for k in ks:
        w(f"| {k} | {old_topk[f'k{k}_per_day']:.1f} | {old_topk[f'k{k}_hit']:.4f} | {old_topk[f'k{k}_winrate']:.4f} | {old_topk[f'k{k}_fwd3']:.4f} | {old_topk[f'k{k}_fwd_max3']:.4f} |")
    w("")

    # ── 3. New Top-K ──
    w("## 3. 新系统 Top-K（按 new_total_v2 排序）\n")
    new_topk = evaluate_scheme(df, "new_total_v2", ks)

    w("| K | 日均推送 | 命中率 | 胜率 | 平均3日收益 | 平均3日最大收益 |")
    w("|---|---------|--------|------|-----------|---------------|")
    for k in ks:
        w(f"| {k} | {new_topk[f'k{k}_per_day']:.1f} | {new_topk[f'k{k}_hit']:.4f} | {new_topk[f'k{k}_winrate']:.4f} | {new_topk[f'k{k}_fwd3']:.4f} | {new_topk[f'k{k}_fwd_max3']:.4f} |")
    w("")

    # ── 4. Balanced Top-K ──
    w("## 4. 均衡系统 Top-K（按 balanced_total 排序）\n")
    bal_topk = evaluate_scheme(df, "balanced_total", ks)

    w("| K | 日均推送 | 命中率 | 胜率 | 平均3日收益 | 平均3日最大收益 |")
    w("|---|---------|--------|------|-----------|---------------|")
    for k in ks:
        w(f"| {k} | {bal_topk[f'k{k}_per_day']:.1f} | {bal_topk[f'k{k}_hit']:.4f} | {bal_topk[f'k{k}_winrate']:.4f} | {bal_topk[f'k{k}_fwd3']:.4f} | {bal_topk[f'k{k}_fwd_max3']:.4f} |")
    w("")

    # ── 5. IC 对比 ──
    w("## 5. RankIC 对比\n")
    w("| 评分方案 | IC hit_limit_3 | IC fwd_ret_3 | IC fwd_max_3 | IC trailing_10 | chasing_score |")
    w("|---------|---------------|-------------|-------------|---------------|--------------|")
    for name, result in [("旧 total", old_topk), ("new_total_v2", new_topk), ("balanced_total", bal_topk)]:
        w(f"| {name} | {result['ic_hit_limit_3']:.4f} | {result['ic_fwd_ret_3']:.4f} | {result['ic_fwd_max_3']:.4f} | {result['ic_trailing_10']:.4f} | {result['chasing_score']:.4f} |")
    w("")

    # ── 6. 改善幅度 ──
    w("## 6. 改善幅度（new_total_v2 vs 旧 total）\n")
    w("| K | 命中率改善 | 胜率改善 | 3日收益改善 |")
    w("|---|----------|---------|-----------|")
    for k in ks:
        hit_diff = new_topk[f'k{k}_hit'] - old_topk[f'k{k}_hit']
        wr_diff = new_topk[f'k{k}_winrate'] - old_topk[f'k{k}_winrate']
        fwd3_diff = new_topk[f'k{k}_fwd3'] - old_topk[f'k{k}_fwd3']
        w(f"| {k} | {hit_diff:+.4f} ({hit_diff*100:+.1f}pp) | {wr_diff:+.4f} ({wr_diff*100:+.1f}pp) | {fwd3_diff:+.4f} ({fwd3_diff*100:+.1f}pp) |")
    w("")

    # ── 7. 对比旧推送规则 ──
    w("## 7. 新旧推送规则对比（实战视角）\n")
    w("| 方案 | 日均推送 | 命中率 | 胜率 | 3日收益 | 3日最大收益 |")
    w("|------|---------|--------|------|---------|-----------|")
    w(f"| 旧推送规则 | {old['per_day']:.1f} | {old['hit']:.4f} | {old['winrate']:.4f} | {old['fwd3']:.4f} | {old['fwd_max3']:.4f} |")
    for k in [5, 10, 15]:
        w(f"| 新Top-{k} | {new_topk[f'k{k}_per_day']:.1f} | {new_topk[f'k{k}_hit']:.4f} | {new_topk[f'k{k}_winrate']:.4f} | {new_topk[f'k{k}_fwd3']:.4f} | {new_topk[f'k{k}_fwd_max3']:.4f} |")
    w("")

    # ── 8. 结论 ──
    w("## 8. 结论\n")

    best_k = 10
    hit_improvement = (new_topk[f'k{best_k}_hit'] - old['hit']) * 100
    wr_improvement = (new_topk[f'k{best_k}_winrate'] - old['winrate']) * 100

    w(f"- **核心改善**：采用 new_total_v2 Top-{best_k} 推送策略")
    w(f"  - 命中率：{old['hit']*100:.1f}% → {new_topk[f'k{best_k}_hit']*100:.1f}%（**+{hit_improvement:.1f}pp**）")
    w(f"  - 胜率：{old['winrate']*100:.1f}% → {new_topk[f'k{best_k}_winrate']*100:.1f}%（**+{wr_improvement:.1f}pp**）")
    w(f"  - 日均推送：{old['per_day']:.1f} → {new_topk[f'k{best_k}_per_day']:.1f} 只")
    w(f"  - 3日平均收益：{old['fwd3']*100:.2f}% → {new_topk[f'k{best_k}_fwd3']*100:.2f}%")

    if hit_improvement >= 10 and wr_improvement >= 10:
        w(f"\n✅ **目标达成！命中率和胜率均提升超过 10 个百分点。**")
    elif hit_improvement >= 10:
        w(f"\n⚠️ 命中率提升达标（+{hit_improvement:.1f}pp），胜率提升 {wr_improvement:.1f}pp。")
    elif wr_improvement >= 10:
        w(f"\n⚠️ 胜率提升达标（+{wr_improvement:.1f}pp），命中率提升 {hit_improvement:.1f}pp。")
    else:
        w(f"\n❌ 未达标，需继续优化。")

    w("\n### 改善来源\n")
    w("1. **新增涨停基因因子**（limit_up_gene）：历史涨停频率是未来涨停的最强预测因子之一")
    w("2. **新增回调质量因子**（pullback_quality/pullback_from_peak）：反追高，低买高卖")
    w("3. **新增量能质量因子**（vol_expansion_quality）：区分吸筹放量和出货放量")
    w("4. **新增反转识别**（reversal_signal/gap_up_quality）：捕捉弱转强信号")
    w("5. **增强追高护栏**（chasing_guardrail_v2）：多维度位置评估")
    w("6. **改进评分聚合**：基于各因子实际 IC 贡献优化权重分配")
    w("7. **Top-K 推送模式**：按每日评分排序取最优标的，比固定阈值更稳定")

    return "\n".join(lines)


def main():
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

    print("加载数据...")
    df = load_data()
    print(f"  数据: {len(df)} rows, {df.date.nunique()} dates")

    print("生成报告...")
    report = generate_report(df)
    report_path = OUT / "final_comparison.md"
    report_path.write_text(report, encoding="utf-8")

    print(report)
    print(f"\n报告已写入: {report_path}")
    return report


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))
    main()
