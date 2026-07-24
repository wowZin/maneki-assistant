#!/usr/bin/env python3
"""全量因子训练：基础60特征 + stk_factor_pro全量 + cyq_perf筹码特征，重训 XGBoost。

不做 IC 筛选 —— 弱特征间的组合信号由 XGBoost 自己找，粗暴 IC 过滤会丢掉组合信息。

数据源：
- wiki/raw/limit-up/training/training_set.csv（基础60特征 + 标签）
- wiki/raw/limit-up/panel/stk_factor_pro.parquet（261字段，pull_factor_pro_full.py 生成）
- wiki/raw/limit-up/panel/cyq_perf.parquet（筹码：winner_rate + cost分布）

用法：
    python plays/limit_up/backtest/train_factor_pro.py
    python plays/limit_up/backtest/train_factor_pro.py --out plays/limit_up/data/backtest/models/factor_full_v1
"""
import argparse
import json
import sys
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_DIR))

import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score

from plays.limit_up.backtest.metrics import rank_ic
from plays.limit_up.backtest.model import LimitUpModel, default_estimator
from plays.limit_up.backtest.training import FEATURE_COLS, TRAINING_CSV

PANEL_DIR = PROJECT_DIR / "wiki" / "raw" / "limit-up" / "panel"
FACTOR_PARQUET = PANEL_DIR / "stk_factor_pro.parquet"
CYQ_PARQUET = PANEL_DIR / "cyq_perf.parquet"

TRAIN_END = "20260625"  # 与此前实验保持同一切分，保证可比


def load_factors() -> pd.DataFrame:
    """stk_factor_pro 全量字段，剔除标识列和与基础特征重复的列。"""
    df = pd.read_parquet(FACTOR_PARQUET)
    df["trade_date"] = df["trade_date"].astype(str)
    dupes = {"turnover_rate", "volume_ratio", "pe", "pb", "circ_mv"}  # 基础特征已有
    cols = [c for c in df.columns if c not in dupes]
    return df[cols]


# 价格尺度列族（量纲=元，横截面不可比）→ 转成 x/close_qfq-1，只留 qfq 版本
# bfq/hfq 是同一序列的缩放，纯冗余，丢弃
_PRICE_PATTERNS = (
    "open_", "high_", "low_", "close_",
    "ma_bfq_", "ma_hfq_", "ma_qfq_",
    "ema_bfq_", "ema_hfq_", "ema_qfq_",
    "boll_", "ktn_", "taq_", "expma_", "xsii_", "bbi_",
    "macd_", "dfma_", "dpo_", "madpo_", "mtm_", "mtmma_",
    "asi_", "asit_", "atr_",
)
# 累积量/股本/复权因子：横截面无意义，丢弃
_DROP_COLS = {
    "adj_factor", "total_share", "float_share", "free_share", "pre_close",
    "obv_bfq", "obv_hfq", "obv_qfq",
    "open", "high", "low", "close", "change",
}


def normalize_factors(df: pd.DataFrame) -> pd.DataFrame:
    """量纲修正：价格尺度列 → 相对现价偏离率；其余原样保留。"""
    close = df["close_qfq"]
    out = {}
    for c in df.columns:
        if c in ("ts_code", "trade_date"):
            out[c] = df[c]
            continue
        if c in _DROP_COLS:
            continue
        if any(c.startswith(p) for p in _PRICE_PATTERNS):
            if not c.endswith("_qfq"):
                continue  # bfq/hfq 冗余，丢弃
            if c == "close_qfq":
                continue  # 现价本身横截面无意义
            out[f"{c}_relclose"] = df[c] / close - 1.0
        else:
            out[c] = df[c]
    return pd.DataFrame(out)


def build_cyq_features(cyq: pd.DataFrame, close_map: pd.DataFrame) -> pd.DataFrame:
    """筹码峰结构特征（cyq_perf 派生）。close_map 提供当日现价用于量纲归一。"""
    cyq = cyq.merge(close_map, on=["ts_code", "trade_date"], how="left")
    close = cyq["close_qfq"]
    band = (cyq["cost_95pct"] - cyq["cost_5pct"]).replace(0, np.nan)
    out = cyq[["ts_code", "trade_date", "winner_rate"]].copy()
    # 筹码集中度：90%成本带越窄越集中
    out["cyq_concentration"] = band / (cyq["cost_95pct"] + cyq["cost_5pct"])
    # 现价格在成本带中的位置：0=贴成本底 1=顶穿成本顶
    out["cyq_price_pos"] = (close - cyq["cost_5pct"]) / band
    # 现价相对筹码中位/均价的偏离（峰上=浮盈，峰下=被套）
    out["cyq_dev_cost50"] = close / cyq["cost_50pct"] - 1.0
    out["cyq_dev_avg"] = close / cyq["weight_avg"] - 1.0
    # 各成本分位相对现价的偏离：筹码峰群在现价上/下方多远
    for p in ["5pct", "15pct", "50pct", "85pct", "95pct"]:
        out[f"cyq_cost_{p}_rel"] = cyq[f"cost_{p}"] / close - 1.0
    # 峰形偏度：下半带宽 vs 上半带宽
    lower = (cyq["cost_50pct"] - cyq["cost_15pct"]).replace(0, np.nan)
    upper = (cyq["cost_85pct"] - cyq["cost_50pct"]).replace(0, np.nan)
    out["cyq_skew"] = lower / upper
    return out


def merge_all() -> pd.DataFrame:
    t = pd.read_csv(TRAINING_CSV, dtype={"trade_date": str, "code": str})
    factors_raw = load_factors()
    close_map = factors_raw[["ts_code", "trade_date", "close_qfq"]].copy()
    factors = normalize_factors(factors_raw)  # 只作用于 factor 列，不碰基础特征

    cyq = pd.read_parquet(CYQ_PARQUET)
    cyq["trade_date"] = cyq["trade_date"].astype(str)
    cyq_feats = build_cyq_features(cyq, close_map)

    df = t.merge(factors, left_on=["code", "trade_date"],
                 right_on=["ts_code", "trade_date"], how="left")
    df = df.drop(columns=["ts_code"], errors="ignore")
    df = df.merge(cyq_feats, left_on=["code", "trade_date"],
                  right_on=["ts_code", "trade_date"], how="left")
    df = df.drop(columns=["ts_code"], errors="ignore")
    if {"kdj_k_qfq", "kdj_d_qfq"} <= set(df.columns):
        df["kdj_diff"] = df["kdj_k_qfq"] - df["kdj_d_qfq"]
    return df


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path,
                    default=PROJECT_DIR / "plays/limit_up/data/backtest/models/factor_full_v1")
    ap.add_argument("--train-end", default=TRAIN_END)
    ap.add_argument("--estimator", default="xgboost", choices=["xgboost", "hist"])
    args = ap.parse_args()

    t0 = time.time()
    print("[train] 合并训练集 + factor_pro全量 + cyq筹码 ...")
    df = merge_all()
    df["fwd_ret_3_positive"] = (df["fwd_ret_3"] > 0).astype(int)
    print(f"[train] 合并后 {df.shape}，耗时 {time.time()-t0:.0f}s")

    train = df[df.trade_date <= args.train_end].reset_index(drop=True)
    test = df[df.trade_date > args.train_end].reset_index(drop=True)
    print(f"[train] 训练 {len(train)} 行 / 验证 {len(test)} 行（切分点 {args.train_end}）")

    # 特征列：基础60 + 全部数值列（排除标识/标签/中间列）
    exclude = set(train.columns) & {
        "trade_date", "code", "name", "fwd_ret_1", "fwd_ret_2", "fwd_ret_3",
        "fwd_max_1", "fwd_max_2", "fwd_max_3", "hit_limit_1", "hit_limit_2",
        "hit_limit_3", "label", "fwd_ret_3_positive",
    }
    feat_cols = [
        c for c in train.columns
        if c not in exclude
        and pd.api.types.is_numeric_dtype(train[c])
        and train[c].notna().mean() > 0.5  # 覆盖率过半才用
    ]
    for c in FEATURE_COLS:
        if c in train.columns and c not in feat_cols:
            feat_cols.append(c)
    print(f"[train] 特征数: {len(feat_cols)}（基础 {len([c for c in feat_cols if c in FEATURE_COLS])}）")

    model = LimitUpModel(
        feature_cols=feat_cols,
        hit_estimator=default_estimator(args.estimator),
        win_estimator=default_estimator(args.estimator),
        blend_hit=0.6,
        blend_win=0.4,
    )
    print("[train] 拟合 hit+win 双模型 ...")
    model.fit(train)

    # ── 评估（与生产模型同口径：blend分 → AUC/IC/TopK）──
    from plays.limit_up.backtest.model import evaluate_model
    metrics = evaluate_model(model, test)
    print(json.dumps(metrics, indent=2, ensure_ascii=False, default=str))

    fi = model.feature_importance()
    print("\nTop 20 特征重要性 (hit头):")
    for i, (f, v) in enumerate(sorted(fi.items(), key=lambda kv: -kv[1])[:20]):
        tag = ""
        if f.startswith("cyq") or f in ("winner_rate", "weight_avg"):
            tag = " [筹码]"
        elif f not in FEATURE_COLS:
            tag = " [factor_pro]"
        print(f"  {i+1:2d}. {f}: {v:.4f}{tag}")

    args.out.mkdir(parents=True, exist_ok=True)
    model.save(args.out)
    (args.out / "validation_report.json").write_text(
        json.dumps(metrics, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"\n[train] 已保存到 {args.out}（未动生产模型）")


if __name__ == "__main__":
    main()
