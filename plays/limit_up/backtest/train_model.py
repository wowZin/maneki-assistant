#!/usr/bin/env python3
"""训练 limit_up 非线性评分模型。

用法：
    python plays/limit_up/backtest/train_model.py \
        --train-start 20260519 --train-end 20260620 \
        --test-start 20260621 --test-end 20260702
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd
PROJECT_DIR = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from plays.limit_up.backtest.model import (
    LimitUpModel,
    default_estimator,
    evaluate_model,
    load_training_data,
)

TRAINING_CSV = PROJECT_DIR / "wiki" / "raw" / "limit-up" / "training" / "training_set.csv"
MODEL_DIR = Path(__file__).resolve().parent.parent / "data" / "backtest" / "models"


def main():
    parser = argparse.ArgumentParser(description="训练 limit_up 模型")
    parser.add_argument("--train-start", default="20260519")
    parser.add_argument("--train-end", default="20260620")
    parser.add_argument("--test-start", default="20260621")
    parser.add_argument("--test-end", default="20260702")
    parser.add_argument("--blend-hit", type=float, default=0.6)
    parser.add_argument("--blend-win", type=float, default=0.4)
    parser.add_argument("--model-dir", type=Path, default=MODEL_DIR)
    parser.add_argument("--panel", type=Path, help="从 backtest out/panel.csv 训练（而非 training_set.csv）")
    parser.add_argument("--estimator", choices=["hist", "xgboost"], default="xgboost",
                        help="基学习器类型")
    args = parser.parse_args()

    if args.panel:
        print(f"[train] 加载面板 {args.panel}")
        df = pd.read_csv(args.panel, dtype={"date": str, "code": str})
        df["trade_date"] = df["date"]
        df["fwd_ret_3_positive"] = (df["fwd_ret_3"] > 0).astype(int)
        df = df[(df["trade_date"] >= args.train_start) & (df["trade_date"] <= args.test_end)]
        train_df = df[df["trade_date"] <= args.train_end]
        test_df = df[df["trade_date"] >= args.test_start]
        test_df = test_df[~test_df.index.isin(train_df.index)].copy()
    else:
        print(f"[train] 加载训练集 {TRAINING_CSV}")
        train_df, test_df = load_training_data(
            TRAINING_CSV,
            train_start=args.train_start,
            train_end=args.train_end,
            test_start=args.test_start,
            test_end=args.test_end,
        )

    train_df = train_df.reset_index(drop=True)
    test_df = test_df.reset_index(drop=True)

    print(f"[train] 训练 {args.train_start}~{args.train_end}: {len(train_df)} 行")
    print(f"[train] 验证 {args.test_start}~{args.test_end}: {len(test_df)} 行")

    if train_df.empty:
        raise RuntimeError("训练集为空")

    model = LimitUpModel(
        hit_estimator=default_estimator(args.estimator),
        win_estimator=default_estimator(args.estimator),
        blend_hit=args.blend_hit,
        blend_win=args.blend_win,
    )
    print("[train] 拟合模型...")
    model.fit(train_df)

    print("[train] 验证集评估...")
    metrics = evaluate_model(model, test_df)
    print(json.dumps(metrics, indent=2, ensure_ascii=False, default=str))

    args.model_dir.mkdir(parents=True, exist_ok=True)
    model.save(args.model_dir)
    report_path = args.model_dir / "validation_report.json"
    report_path.write_text(
        json.dumps(metrics, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    print(f"[train] 模型已保存到 {args.model_dir}")


if __name__ == "__main__":
    main()
