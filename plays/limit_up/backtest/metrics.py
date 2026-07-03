"""回测评估指标 — 仅依赖 pandas/numpy（无 scipy）。

核心指标：
- rank_ic：维度分 vs 标签 的 Spearman 秩相关（单调预测力）
- pearson_ic：线性相关
- bucket_stats：按分数分桶的命中率/平均收益（理想为单调）
- precision_at_k：Top-K 高分股命中率与平均收益
- win_rate：正收益占比
- auc：分数对二元标签的区分度（Mann-Whitney U，无需 scipy）
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def _clean_pair(scores: pd.Series, labels: pd.Series) -> tuple[pd.Series, pd.Series]:
    df = pd.DataFrame({"s": scores, "l": labels}).replace([np.inf, -np.inf], np.nan).dropna()
    return df["s"], df["l"]


def rank_ic(scores: pd.Series, labels: pd.Series) -> float | None:
    """Spearman 秩相关。样本 < 3 或某列方差为 0 返回 None。

    手动实现（排名后取 Pearson），避免 pandas spearman 对 scipy 的依赖。
    """
    s, l = _clean_pair(scores, labels)
    if len(s) < 3 or s.nunique() < 2 or l.nunique() < 2:
        return None
    return float(s.rank().corr(l.rank(), method="pearson"))


def pearson_ic(scores: pd.Series, labels: pd.Series) -> float | None:
    s, l = _clean_pair(scores, labels)
    if len(s) < 3 or s.nunique() < 2 or l.nunique() < 2:
        return None
    return float(s.corr(l, method="pearson"))


def bucket_stats(
    df: pd.DataFrame, score_col: str, label_cols: list[str], n_buckets: int = 5
) -> pd.DataFrame:
    """按 score_col 分 n_buckets 个分位桶，统计各桶的样本数与各标签均值。

    返回每桶一行的 DataFrame，bucket=0 为最低分桶，n_buckets-1 为最高分桶。
    """
    work = df.dropna(subset=[score_col]).copy()
    if len(work) < n_buckets:
        return pd.DataFrame()
    # 用 rank 再切，避免重复值导致 qcut 报错
    work["_rank"] = work[score_col].rank(method="first")
    work["bucket"] = pd.qcut(work["_rank"], n_buckets, labels=False)
    agg = {"n": (score_col, "size"), "score_mean": (score_col, "mean")}
    for c in label_cols:
        agg[f"{c}_mean"] = (c, "mean")
    out = work.groupby("bucket").agg(**agg).reset_index()
    return out


def precision_at_k(df: pd.DataFrame, score_col: str, label_col: str, k: int) -> dict:
    """Top-K 高分股的标签均值（命中率或平均收益）。"""
    work = df.dropna(subset=[score_col, label_col])
    if work.empty:
        return {"k": k, "n": 0, "value": None}
    top = work.nlargest(min(k, len(work)), score_col)
    return {"k": k, "n": len(top), "value": float(top[label_col].mean())}


def win_rate(returns: pd.Series, thr: float = 0.0) -> dict:
    """正收益占比与平均盈/亏。"""
    r = returns.replace([np.inf, -np.inf], np.nan).dropna()
    if r.empty:
        return {"n": 0, "win_rate": None, "avg_win": None, "avg_loss": None, "avg": None}
    wins = r[r > thr]
    losses = r[r <= thr]
    return {
        "n": int(len(r)),
        "win_rate": float((r > thr).mean()),
        "avg_win": float(wins.mean()) if len(wins) else 0.0,
        "avg_loss": float(losses.mean()) if len(losses) else 0.0,
        "avg": float(r.mean()),
    }


def auc(scores: pd.Series, labels: pd.Series) -> float | None:
    """二元标签下 score 的 AUC，等价于 Mann-Whitney U / (n_pos*n_neg)。

    用秩和计算，处理并列值（average rank），无需 scipy。
    """
    s, l = _clean_pair(scores, labels)
    l = (l > 0).astype(int)
    n_pos = int(l.sum())
    n_neg = int(len(l) - n_pos)
    if n_pos == 0 or n_neg == 0:
        return None
    ranks = s.rank(method="average")
    sum_pos = float(ranks[l == 1].sum())
    auc_val = (sum_pos - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)
    return auc_val


def dimension_report(df: pd.DataFrame, dim_cols: list[str], label_cols: list[str]) -> pd.DataFrame:
    """对每个维度列、每个标签列，输出 rank_ic 表（长表）。"""
    rows = []
    for dim in dim_cols:
        for lab in label_cols:
            rows.append(
                {
                    "dimension": dim,
                    "label": lab,
                    "rank_ic": rank_ic(df[dim], df[lab]),
                    "pearson_ic": pearson_ic(df[dim], df[lab]),
                    "n": int(df[[dim, lab]].dropna().shape[0]),
                }
            )
    return pd.DataFrame(rows)
