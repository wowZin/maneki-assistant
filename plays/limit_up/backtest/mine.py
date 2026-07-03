#!/usr/bin/env python3
"""因子挖掘：计算训练集中各特征与标签的 IC / Cohen's d。

用法：
    python plays/limit_up/backtest/mine.py --label hit_limit_3
    python plays/limit_up/backtest/mine.py --label fwd_ret_3 --top 20
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from plays.limit_up.backtest.training import FEATURE_COLS, LABEL_COLS, load_all
from plays.limit_up.backtest.metrics import rank_ic, pearson_ic

OUT_DIR = Path(__file__).resolve().parent / "out"
OUT_DIR.mkdir(exist_ok=True)


def _cohens_d(series: pd.Series, label: pd.Series) -> float | None:
    """对二元 label 计算 Cohen's d（效应量）。"""
    df = pd.DataFrame({"x": series, "y": label}).replace([np.inf, -np.inf], np.nan).dropna()
    if df.empty or df["y"].nunique() < 2:
        return None
    pos = df[df["y"] == 1]["x"]
    neg = df[df["y"] == 0]["x"]
    if len(pos) < 2 or len(neg) < 2:
        return None
    pooled_std = np.sqrt(((len(pos) - 1) * pos.var(ddof=1) + (len(neg) - 1) * neg.var(ddof=1)) /
                         (len(pos) + len(neg) - 2))
    if pooled_std == 0:
        return None
    return float((pos.mean() - neg.mean()) / pooled_std)


def _evaluate(df: pd.DataFrame, label_col: str, top: int | None = None) -> pd.DataFrame:
    rows = []
    for feat in FEATURE_COLS:
        if feat not in df.columns:
            continue
        ri = rank_ic(df[feat], df[label_col])
        pi = pearson_ic(df[feat], df[label_col])
        cd = _cohens_d(df[feat], df[label_col]) if df[label_col].dropna().nunique() == 2 else None
        rows.append({
            "feature": feat,
            "rank_ic": ri,
            "pearson_ic": pi,
            "cohens_d": cd,
            "n": int(df[[feat, label_col]].dropna().shape[0]),
        })
    out = pd.DataFrame(rows)
    out["abs_rank_ic"] = out["rank_ic"].abs()
    out = out.sort_values("abs_rank_ic", ascending=False).drop(columns=["abs_rank_ic"])
    if top:
        out = out.head(top)
    return out


def main():
    parser = argparse.ArgumentParser(description="训练集因子挖掘")
    parser.add_argument("--label", default="hit_limit_3",
                        help=f"目标标签，可选 {LABEL_COLS}")
    parser.add_argument("--top", type=int, help="只输出 TOP-N 因子")
    parser.add_argument("--out", help="输出 CSV 路径")
    args = parser.parse_args()

    df = load_all()
    if df.empty:
        print("训练集为空")
        return

    if args.label not in df.columns:
        print(f"标签 {args.label} 不存在于训练集")
        return

    print(f"[mine] 训练集 {len(df)} 行，评估特征 vs {args.label}")
    result = _evaluate(df, args.label, top=args.top)
    print(result.to_string(index=False))

    out_path = args.out or OUT_DIR / f"mine_{args.label}.csv"
    result.to_csv(out_path, index=False)
    print(f"\n已保存: {out_path}")


if __name__ == "__main__":
    main()
