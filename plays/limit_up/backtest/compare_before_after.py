"""Before/After 对比脚本。

基于已有的 panel.csv，对比：
- Before: panel 中已有的 `total`（旧权重、旧维度分、旧推送规则）
- After: 用新权重 + 各维度 trailing 惩罚代理 + 全局追高护栏 + 新推送规则重新计算的综合分

注意：本脚本不重新运行各维度策略函数，而是基于 panel 中已有的维度分做后验调整，
用于快速验证权重/护栏/阈值方案的效果。
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from .evaluate_factor import load_panel
from . import metrics as M

OLD_WEIGHTS = {
    "fundamental": 1.5,
    "technical": 1.0,
    "fundflow": 1.0,
    "sentiment": 1.2,
    "shortterm": 1.5,
}
NEW_WEIGHTS = {
    "fundamental": 0.8,
    "technical": 1.3,
    "fundflow": 0.8,
    "sentiment": 0.7,
    "shortterm": 2.0,
}
DIMS = ["fundamental", "technical", "fundflow", "sentiment", "shortterm"]
LABELS = ["fwd_ret_1", "fwd_ret_3", "fwd_max_3", "hit_limit_3"]


def top3_total_from_scores(scores: dict, weights: dict) -> float:
    dc = [(scores[d], weights[d]) for d in DIMS]
    dc.sort(key=lambda x: x[0] * x[1], reverse=True)
    top3 = dc[:3]
    wsum = sum(w for _, w in top3)
    return sum(s * w for s, w in top3) / wsum if wsum > 0 else 0


def top3_total(row: pd.Series, weights: dict) -> float:
    return top3_total_from_scores({d: row[d] for d in DIMS}, weights)


def dim_penalty(row: pd.Series, dim: str) -> float:
    """根据文档对各维度加 trailing 惩罚的代理。"""
    t = row.get("trailing_10")
    if t is None or pd.isna(t):
        return 0.0
    if dim == "technical":
        if t > 0.30:
            return -15.0
        if t > 0.20:
            return -10.0
        if t > 0.15:
            return -5.0
    if dim == "shortterm":
        if t > 0.25:
            return -10.0
        if t > 0.15:
            return -5.0
    if dim == "fundflow":
        if t > 0.25:
            return -10.0
        if t > 0.15:
            return -6.0
    return 0.0


def guardrail(total: float, trailing_5: float | None, trailing_10: float | None) -> float:
    if trailing_10 is None or pd.isna(trailing_10):
        trailing_10 = 0.0
    if trailing_5 is None or pd.isna(trailing_5):
        trailing_5 = 0.0
    adj = total
    if trailing_10 > 0.25:
        adj *= 0.85
    elif trailing_10 > 0.15:
        adj *= 0.95
    if trailing_5 > 0.15:
        adj *= 0.90
    return adj


def new_total(row: pd.Series) -> float:
    """新综合分：各维度分别加 trailing 惩罚代理 → 新权重 Top3 → 全局追高护栏。"""
    adj_scores = {d: row[d] + dim_penalty(row, d) for d in DIMS}
    base = top3_total_from_scores(adj_scores, NEW_WEIGHTS)
    return guardrail(base, row.get("trailing_5"), row.get("trailing_10"))


def old_push_pass(row: pd.Series) -> bool:
    """旧推送规则：total>=30 + sentiment>=35 + fundflow>=35 + total>=40"""
    total = row.get("total", 0)
    if total < 30:
        return False
    if row.get("sentiment", 0) < 35:
        return False
    if row.get("fundflow", 0) < 35:
        return False
    if total < 40:
        return False
    return True


def new_push_pass(row: pd.Series) -> bool:
    """新推送规则：guardrailed_total>=35 + 非(trailing_10>25% 且 <50) + (shortterm>=40 或 technical>=40)"""
    gt = row.get("new_total", 0)
    if gt < 35:
        return False
    t10 = row.get("trailing_10")
    if t10 is not None and t10 > 0.25 and gt < 50:
        return False
    if row.get("shortterm", 0) < 40 and row.get("technical", 0) < 40:
        return False
    return True


def metrics_table(df: pd.DataFrame, score_col: str, name: str) -> dict:
    out = {"name": name, "n": len(df)}
    for lab in LABELS:
        out[f"ic_{lab}"] = M.rank_ic(df[score_col], df[lab])
    out["ic_trailing_10"] = M.rank_ic(df[score_col], df["trailing_10"])
    out["chasing_score"] = (out["ic_trailing_10"] or 0) - (out["ic_fwd_ret_3"] or 0)
    for k in (10, 20, 50):
        out[f"hit@{k}"] = M.precision_at_k(df, score_col, "hit_limit_3", k)["value"]
        out[f"fwd3@{k}"] = M.precision_at_k(df, score_col, "fwd_ret_3", k)["value"]

    # 推送规则表现
    if score_col == "total":
        mask = df.apply(old_push_pass, axis=1)
    elif score_col == "new_total":
        mask = df.apply(new_push_pass, axis=1)
    else:
        mask = pd.Series(False, index=df.index)
    sub = df[mask]
    out["push_n"] = len(sub)
    out["push_hit"] = sub["hit_limit_3"].mean() if len(sub) else None
    out["push_fwd3"] = sub["fwd_ret_3"].mean() if len(sub) else None
    return out


def run() -> str:
    panel = load_panel()
    panel["new_total"] = panel.apply(new_total, axis=1)

    rows = [
        metrics_table(panel, "total", "before_old_total"),
        metrics_table(panel, "new_total", "after_new_total"),
    ]
    # 也展示只用新权重不加惩罚的效果
    panel["new_weight_only"] = panel.apply(lambda r: top3_total(r, NEW_WEIGHTS), axis=1)
    rows.append(metrics_table(panel, "new_weight_only", "after_new_weight_only"))

    df = pd.DataFrame(rows).round(4)

    lines = []
    w = lines.append
    w("# Before/After 回测对比报告\n")
    w("- **before_old_total**: panel 中已有的 `total`（旧权重/旧规则）")
    w("- **after_new_weight_only**: 仅应用新权重")
    w("- **after_new_total**: 新权重 + 各维度 trailing 惩罚代理 + 全局追高护栏 + 新推送规则\n")
    w(df.to_markdown(index=False))
    w("")

    # 推送规则对比
    old_push = panel[panel.apply(old_push_pass, axis=1)]
    new_push = panel[panel.apply(new_push_pass, axis=1)]
    w("## 推送池对比\n")
    w(f"| 方案 | n | hit_limit_3 | avg_fwd_ret_3 | avg_fwd_max_3 |")
    w("|------|---|-------------|---------------|---------------|")
    for name, sub in [("旧规则", old_push), ("新规则", new_push)]:
        if len(sub):
            w(f"| {name} | {len(sub)} | {sub['hit_limit_3'].mean():.4f} | {sub['fwd_ret_3'].mean():.4f} | {sub['fwd_max_3'].mean():.4f} |")
        else:
            w(f"| {name} | 0 | - | - | - |")
    w("")

    report = "\n".join(lines)
    out_path = Path(__file__).resolve().parent / "out" / "compare_before_after.md"
    out_path.write_text(report, encoding="utf-8")
    return report


if __name__ == "__main__":
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))
    print(run())
    print(f"\n报告已写入 {Path(__file__).resolve().parent / 'out' / 'compare_before_after.md'}")
