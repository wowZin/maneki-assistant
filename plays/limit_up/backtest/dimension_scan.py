"""维度扫描 — 为阶段3优化提供量化依据。

对每个维度 D：
1. 输出 baseline RankIC（vs fwd_ret_3 / hit_limit_3 / fwd_max_3 / trailing_5 / trailing_10）
2. 扫描 trailing penalty 参数，找到能降低 chasing_score 且不过度损失 hit_limit_3 IC 的参数
3. 测试新权重组合对 total 的改善
4. 输出 markdown 报告到 out/dimension_scan_report.md
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from .evaluate_factor import load_panel
from . import metrics as M

OUT = Path(__file__).resolve().parent / "out"
OUT.mkdir(exist_ok=True)

DIMS = ["fundamental", "technical", "fundflow", "sentiment", "shortterm"]
LABELS = ["fwd_ret_1", "fwd_ret_3", "fwd_max_3", "hit_limit_3"]


def baseline_ic(panel: pd.DataFrame, dim: str) -> dict:
    """计算单个维度的 baseline IC。"""
    out = {"dimension": dim}
    for lab in LABELS:
        out[f"ic_{lab}"] = M.rank_ic(panel[dim], panel[lab])
    out["ic_trailing_5"] = M.rank_ic(panel[dim], panel["trailing_5"])
    out["ic_trailing_10"] = M.rank_ic(panel[dim], panel["trailing_10"])
    out["chasing_score"] = (out["ic_trailing_10"] or 0) - (out["ic_fwd_ret_3"] or 0)
    return out


def penalized_ic(panel: pd.DataFrame, dim: str, thr_high: float, thr_mid: float,
                 pen_high: float, pen_mid: float) -> dict:
    """给维度 D 加 trailing_10 阶梯惩罚后计算 IC。"""
    df = panel.copy()

    def _pen(row):
        t = row.get("trailing_10")
        if t is None or pd.isna(t):
            return 0.0
        if t > thr_high:
            return pen_high
        if t > thr_mid:
            return pen_mid
        return 0.0

    df["pen"] = df.apply(_pen, axis=1)
    df[f"{dim}_adj"] = df[dim] + df["pen"]

    out = {
        "dim": dim,
        "thr_high": thr_high,
        "thr_mid": thr_mid,
        "pen_high": pen_high,
        "pen_mid": pen_mid,
    }
    out["ic_hit"] = M.rank_ic(df[f"{dim}_adj"], df["hit_limit_3"])
    out["ic_fwd3"] = M.rank_ic(df[f"{dim}_adj"], df["fwd_ret_3"])
    out["ic_fwd_max3"] = M.rank_ic(df[f"{dim}_adj"], df["fwd_max_3"])
    out["ic_trail10"] = M.rank_ic(df[f"{dim}_adj"], df["trailing_10"])
    out["chasing_score"] = (out["ic_trail10"] or 0) - (out["ic_fwd3"] or 0)
    return out


def scan_penalty(panel: pd.DataFrame, dim: str) -> pd.DataFrame:
    """扫描多组惩罚参数。"""
    configs = [
        # (thr_high, thr_mid, pen_high, pen_mid)
        (0.30, 0.20, -15.0, -10.0),
        (0.30, 0.20, -25.0, -15.0),
        (0.40, 0.25, -20.0, -10.0),
        (0.40, 0.25, -30.0, -15.0),
        (0.25, 0.15, -15.0, -10.0),
        (0.25, 0.15, -25.0, -15.0),
        (0.20, 0.10, -20.0, -10.0),
    ]
    rows = [penalized_ic(panel, dim, *c) for c in configs]
    return pd.DataFrame(rows)


def evaluate_total_weights(panel: pd.DataFrame, weights: dict[str, float]) -> dict:
    """用给定权重计算新的 total 并评估。"""
    df = panel.copy()

    def _top3_total(row):
        dc = [(row[d], weights[d]) for d in DIMS]
        dc.sort(key=lambda x: x[0] * x[1], reverse=True)
        top3 = dc[:3]
        wsum = sum(w for _, w in top3)
        return sum(s * w for s, w in top3) / wsum if wsum > 0 else 0

    df["new_total"] = df.apply(_top3_total, axis=1)
    out = {"weights": weights}
    for lab in LABELS:
        out[f"ic_{lab}"] = M.rank_ic(df["new_total"], df[lab])
    out["ic_trailing_10"] = M.rank_ic(df["new_total"], df["trailing_10"])
    out["chasing_score"] = (out["ic_trailing_10"] or 0) - (out["ic_fwd_ret_3"] or 0)
    for k in (10, 20, 50):
        out[f"hit@{k}"] = M.precision_at_k(df, "new_total", "hit_limit_3", k)["value"]
        out[f"fwd3@{k}"] = M.precision_at_k(df, "new_total", "fwd_ret_3", k)["value"]
    return out


def scan_weights(panel: pd.DataFrame) -> pd.DataFrame:
    """扫描几组权重方案。"""
    schemes = {
        "current": {"fundamental": 1.5, "technical": 1.0, "fundflow": 1.0,
                    "sentiment": 1.2, "shortterm": 1.5},
        "plan_v1": {"fundamental": 0.8, "technical": 1.3, "fundflow": 0.8,
                    "sentiment": 0.7, "shortterm": 2.0},
        "shortterm_focus": {"fundamental": 0.5, "technical": 1.0, "fundflow": 0.5,
                            "sentiment": 0.5, "shortterm": 2.5},
        "tech_short_focus": {"fundamental": 0.8, "technical": 1.5, "fundflow": 0.6,
                             "sentiment": 0.6, "shortterm": 2.0},
    }
    rows = []
    for name, w in schemes.items():
        r = evaluate_total_weights(panel, w)
        r["scheme"] = name
        rows.append(r)
    return pd.DataFrame(rows)


def run() -> str:
    panel = load_panel()

    lines = []
    w = lines.append
    w("# 五维度因子扫描报告\n")
    w(f"- 样本：{len(panel)} 条 (code,date) 记录")
    w(f"- 维度：{', '.join(DIMS)}")
    w(f"- 标签：{', '.join(LABELS)}\n")

    # 1. Baseline IC
    w("## 1. 各维度 Baseline IC\n")
    base_rows = [baseline_ic(panel, d) for d in DIMS]
    base_df = pd.DataFrame(base_rows).round(4)
    w(base_df.to_markdown(index=False))
    w("\n> chasing_score = ic_trailing_10 - ic_fwd_ret_3，越大越说明在追高。\n")

    # 2. Penalty scan per dimension
    w("## 2. Trailing 惩罚参数扫描\n")
    for dim in DIMS:
        w(f"### {dim}\n")
        df = scan_penalty(panel, dim).round(4)
        w(df.to_markdown(index=False))
        w("\n")

    # 3. Weight scheme scan
    w("## 3. 权重方案扫描\n")
    wdf = scan_weights(panel)
    # Flatten weights for display
    wdf_display = wdf.copy()
    wdf_display["weights"] = wdf_display["weights"].apply(lambda x: "/".join(f"{k}={v}" for k, v in x.items()))
    wdf_display = wdf_display.round(4)
    w(wdf_display.to_markdown(index=False))
    w("\n")

    report = "\n".join(lines)
    (OUT / "dimension_scan_report.md").write_text(report, encoding="utf-8")
    return report


if __name__ == "__main__":
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))
    print(run())
    print(f"\n报告已写入 {OUT / 'dimension_scan_report.md'}")
