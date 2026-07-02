"""因子评估工具。

给定 panel 和一个因子函数，计算该因子（或与现有维度分组合后）对标签的
RankIC / PearsonIC / 分桶命中率 / Precision@K。

所有评估基于已有的 `out/panel.csv`，不拉取新数据，确保快速迭代。
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import pandas as pd

from . import metrics as M

PANEL_PATH = Path(__file__).resolve().parent / "out" / "panel.csv"
LABELS = ["fwd_ret_1", "fwd_ret_3", "fwd_max_3", "hit_limit_3"]


def load_panel(path: Path | str | None = None) -> pd.DataFrame:
    """读取 panel csv。"""
    path = Path(path) if path else PANEL_PATH
    return pd.read_csv(path)


def evaluate_factor(
    panel: pd.DataFrame,
    factor_func: Callable[[pd.Series], float],
    factor_name: str,
    base_col: str | None = None,
    combine: str = "add",
    labels: list[str] | None = None,
    group_by_date: bool = True,
) -> dict:
    """评估单个因子。

    Args:
        panel: 带标签的面板 DataFrame
        factor_func: 接收一行 Series 返回 float 的函数
        factor_name: 因子名
        base_col: 若指定，将因子与现有维度分组合（如 technical）
        combine: 组合方式，"add" | "replace" | "guardrail"
            - add:      new_score = base_col + factor
            - replace:  new_score = factor
            - guardrail: new_score = guardrail(base_col, trailing_5, trailing_10)
        labels: 要评估的标签列表，默认 LABELS
        group_by_date: 是否按 date 分组计算 IC 后汇总

    Returns:
        dict，包含 overall/grouped IC、分桶命中率、Precision@K
    """
    labels = labels or LABELS
    df = panel.copy()

    if combine == "guardrail":
        df[factor_name] = df.apply(
            lambda r: factor_func(r.get(base_col) if base_col else r.get("total"),
                                  r.get("trailing_5"), r.get("trailing_10")),
            axis=1,
        )
    else:
        df[factor_name] = df.apply(factor_func, axis=1)

    if base_col and combine == "add":
        score_col = f"{base_col}_adj"
        df[score_col] = df[base_col] + df[factor_name]
    elif base_col and combine == "replace":
        score_col = factor_name
    else:
        score_col = factor_name

    result = {
        "factor": factor_name,
        "base_col": base_col,
        "combine": combine,
        "n": int(df[[score_col] + labels].dropna().shape[0]),
    }

    # Overall IC
    result["overall_ic"] = {
        lab: {
            "rank_ic": M.rank_ic(df[score_col], df[lab]),
            "pearson_ic": M.pearson_ic(df[score_col], df[lab]),
        }
        for lab in labels
    }

    # Chasing diagnostics: score vs trailing returns (PIT first)
    t5_col = "trailing_5_pit" if "trailing_5_pit" in df.columns else "trailing_5"
    t10_col = "trailing_10_pit" if "trailing_10_pit" in df.columns else "trailing_10"
    result["chasing_ic"] = {
        "trailing_5": M.rank_ic(df[score_col], df[t5_col]),
        "trailing_10": M.rank_ic(df[score_col], df[t10_col]),
    }
    result["chasing_score"] = (
        (result["chasing_ic"].get("trailing_10") or 0)
        - (result["overall_ic"].get("fwd_ret_3", {}).get("rank_ic") or 0)
    )

    # Grouped by date IC (mean/std)
    if group_by_date and "date" in df.columns:
        ic_rows = []
        for d, g in df.groupby("date"):
            if len(g) < 5:
                continue
            for lab in labels:
                ic = M.rank_ic(g[score_col], g[lab])
                if ic is not None:
                    ic_rows.append({"date": d, "label": lab, "rank_ic": ic})
        ic_df = pd.DataFrame(ic_rows)
        if not ic_df.empty:
            result["grouped_ic"] = (
                ic_df.groupby("label")["rank_ic"].agg(["mean", "std", "count"])
                .round(4)
                .to_dict("index")
            )

    # Bucket hit rate (on hit_limit_3)
    if "hit_limit_3" in df.columns:
        bs = M.bucket_stats(df, score_col, ["hit_limit_3", "fwd_ret_3", "fwd_max_3"], n_buckets=5)
        result["bucket"] = bs.round(4).to_dict("records") if not bs.empty else []

    # Precision@K
    result["precision_at_k"] = {}
    for k in (10, 20, 50):
        result["precision_at_k"][f"hit_limit_3@{k}"] = M.precision_at_k(
            df, score_col, "hit_limit_3", k
        )["value"]
        result["precision_at_k"][f"fwd_ret_3@{k}"] = M.precision_at_k(
            df, score_col, "fwd_ret_3", k
        )["value"]

    return result


def evaluate_guardrail_scheme(
    panel: pd.DataFrame,
    labels: list[str] | None = None,
) -> dict:
    """评估全局追高护栏对 total 的改善效果。"""
    from .factor_lib import factor_chasing_guardrail_total

    return evaluate_factor(
        panel,
        factor_chasing_guardrail_total,
        "total_guardrailed",
        base_col="total",
        combine="guardrail",
        labels=labels,
    )


def format_report(result: dict) -> str:
    """把 evaluate_factor 结果格式化为可读 markdown。"""
    lines = []
    w = lines.append
    w(f"## 因子评估：{result['factor']}")
    if result["base_col"]:
        w(f"- 基准列：{result['base_col']}，组合方式：{result['combine']}")
    w(f"- 样本数：{result['n']}\n")

    w("### Overall RankIC")
    ic = pd.DataFrame(result["overall_ic"]).T.round(4)
    w(ic.to_markdown())
    w("")

    w("### Chasing IC (score vs trailing return)")
    chasing = pd.DataFrame([result["chasing_ic"]]).T.round(4)
    chasing.columns = ["rank_ic"]
    chasing["chasing_score"] = [
        result["chasing_score"] if idx == "trailing_10" else None
        for idx in chasing.index
    ]
    w(chasing.to_markdown())
    w("\n> chasing_score = ic_trailing_10 - ic_fwd_ret_3，越大说明追高越严重。\n")

    if "grouped_ic" in result:
        w("### 按日期分组 RankIC (mean ± std)")
        gi = pd.DataFrame(result["grouped_ic"]).T.round(4)
        w(gi.to_markdown())
        w("")

    if result.get("bucket"):
        w("### 分桶命中率")
        bs = pd.DataFrame(result["bucket"])
        w(bs.to_markdown(index=False))
        w("")

    w("### Precision@K")
    pk = pd.DataFrame([result["precision_at_k"]]).T.round(4)
    pk.columns = ["value"]
    w(pk.to_markdown())
    w("")

    return "\n".join(lines)


if __name__ == "__main__":
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))
    from plays.limit_up.backtest.factor_lib import (
        factor_technical_position_penalty,
        factor_fundflow_trailing_penalty,
        factor_shortterm_trailing_penalty,
    )

    panel = load_panel()
    print(f"panel: {len(panel)} rows\n")

    for name, fn in [
        ("technical_position_penalty", factor_technical_position_penalty),
        ("fundflow_trailing_penalty", factor_fundflow_trailing_penalty),
        ("shortterm_trailing_penalty", factor_shortterm_trailing_penalty),
    ]:
        base = name.split("_")[0]
        r = evaluate_factor(panel, fn, name, base_col=base, combine="add")
        print(format_report(r))
        print("\n" + "=" * 60 + "\n")

    # 全局追高护栏
    gr = evaluate_guardrail_scheme(panel)
    print(format_report(gr))
