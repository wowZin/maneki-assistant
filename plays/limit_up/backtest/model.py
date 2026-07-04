"""limit_up 非线性评分模型。

使用树模型学习 PIT 特征到 3 日涨停/正收益概率的映射，输出 0–100 连续分。
Estimator 可插拔，默认 scikit-learn HistGradientBoostingClassifier。
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

# macOS 上 xgboost 需要 libomp；若已安装则预先注入库路径
_libomp = Path("/usr/local/opt/libomp/lib")
if _libomp.exists():
    os.environ.setdefault("DYLD_LIBRARY_PATH", str(_libomp))

import joblib

# 默认特征列：与 training.py FEATURE_COLS 保持一致
# NOTE: 修改训练特征时，必须同步更新此处列表
DEFAULT_FEATURES = [
    "position_20d", "trailing_10", "trailing_5",
    "pct_chg_std_10d", "pct_chg_std_5d", "max_pct_chg_5d",
    "limit_up_count_20d", "limit_up_count_60d", "max_step", "was_limit",
    "avg_amount_5d", "pct_chg_score_day",
    "turnover_rate", "volume_ratio", "prev_turnover", "prev_vol_ratio", "vol_accel",
    "circ_mv", "cmv_yi", "pe", "pb",
    "pullback_10d", "pullback_20d",
    "prev_pct", "pct_5d", "positive_5d",
    "close_pos", "body_ratio", "upper_ratio", "lower_ratio", "amplitude",
    "net_mf_amount", "net_mf_ratio", "buy_elg_ratio", "buy_lg_ratio",
    "mf_net", "mf_accel", "mf_pct",
    "sector_heat", "sector_rank", "n_concepts",
    "auc_amount", "auc_vol", "auc_amt_ratio", "auc_vol_ratio",
    # 龙虎榜 PIT 特征
    "dt_is_listed", "dt_net_amount", "dt_net_rate", "dt_l_buy_ratio",
    "dt_n_exalter", "dt_inst_net_buy", "dt_hot_net_buy", "dt_inst_sell_ratio",
    # 五维度分：从 analysis 记录提取，强先验信号
    "fundamental", "technical", "fundflow", "sentiment", "shortterm",
]


def _clean_features(df: pd.DataFrame, feature_cols: list[str]) -> pd.DataFrame:
    """清洗特征：只保留需要的列，处理 inf/NaN。"""
    cols = [c for c in feature_cols if c in df.columns]
    x = df[cols].copy()
    x = x.replace([np.inf, -np.inf], np.nan)
    # 用中位数填充缺失（树模型可处理 NaN，但部分 estimator 不行；中位数更稳）
    for c in cols:
        median = x[c].median()
        if pd.isna(median):
            median = 0.0
        x[c] = x[c].fillna(median)
    return x


def load_training_data(
    csv_path: Path,
    train_start: str | None = None,
    train_end: str | None = None,
    test_start: str | None = None,
    test_end: str | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """读取训练集 CSV 并做时间切分。

    目标列：
      - hit_limit_3 用于训练命中率模型
      - fwd_ret_3 > 0 用于训练胜率模型
    """
    df = pd.read_csv(csv_path, dtype={"trade_date": str, "code": str})
    df["fwd_ret_3_positive"] = (df["fwd_ret_3"] > 0).astype(int)

    if train_start:
        df = df[df["trade_date"] >= train_start]
    if test_end:
        df = df[df["trade_date"] <= test_end]

    train = df[df["trade_date"] <= train_end] if train_end else df.copy()
    test = df[df["trade_date"] >= test_start] if test_start else df.copy()
    # 去重交集
    test = test[~test.index.isin(train.index)].copy() if not test.empty else test
    return train.reset_index(drop=True), test.reset_index(drop=True)


def default_estimator(estimator: str = "hist"):
    """返回默认 estimator（可替换为 xgboost/lightgbm）。

    Args:
        estimator: "hist" 或 "xgboost"。
    """
    if estimator == "xgboost":
        import os
        libomp = Path("/usr/local/opt/libomp/lib")
        if libomp.exists():
            os.environ.setdefault("DYLD_LIBRARY_PATH", str(libomp))
        import xgboost as xgb
        return xgb.XGBClassifier(
            n_estimators=500,
            max_depth=5,
            learning_rate=0.03,
            subsample=0.9,
            colsample_bytree=0.9,
            scale_pos_weight=2.0,
            eval_metric="logloss",
            random_state=42,
            n_jobs=4,
            reg_alpha=0.05,
            reg_lambda=1.0,
            min_child_weight=5,
        )
    from sklearn.ensemble import HistGradientBoostingClassifier
    return HistGradientBoostingClassifier(
        max_iter=500,
        learning_rate=0.05,
        max_depth=5,
        min_samples_leaf=10,
        l2_regularization=0.5,
        early_stopping=True,
        validation_fraction=0.1,
        n_iter_no_change=15,
        class_weight="balanced",
        random_state=42,
    )


class LimitUpModel:
    """打板模型封装：分别预测 3 日涨停概率与 3 日正收益概率，混合为 0–100 分。"""

    def __init__(
        self,
        feature_cols: list[str] | None = None,
        hit_estimator: Any = None,
        win_estimator: Any = None,
        blend_hit: float = 0.6,
        blend_win: float = 0.4,
    ):
        self.feature_cols = feature_cols or DEFAULT_FEATURES
        self.hit_estimator = hit_estimator
        self.win_estimator = win_estimator
        self.blend_hit = blend_hit
        self.blend_win = blend_win
        self._fill_values: dict[str, float] = {}

    def fit(self, df: pd.DataFrame) -> "LimitUpModel":
        """在训练集上拟合 hit/win 两个模型。"""
        x = _clean_features(df, self.feature_cols)
        for c in x.columns:
            self._fill_values[c] = float(x[c].median())

        if self.hit_estimator is not None:
            mask = df["hit_limit_3"].notna()
            self.hit_estimator.fit(x[mask], df.loc[mask, "hit_limit_3"].astype(int))

        if self.win_estimator is not None:
            mask = df["fwd_ret_3"].notna()
            self.win_estimator.fit(x[mask], df.loc[mask, "fwd_ret_3_positive"].astype(int))

        return self

    def _prepare_x(self, obj: dict | pd.Series | pd.DataFrame) -> pd.DataFrame:
        """把输入对象转成模型可接受的 DataFrame。"""
        if isinstance(obj, pd.DataFrame):
            x = obj[[c for c in self.feature_cols if c in obj.columns]].copy()
        elif isinstance(obj, pd.Series):
            x = pd.DataFrame([obj.to_dict()])
            x = x[[c for c in self.feature_cols if c in x.columns]]
        else:
            x = pd.DataFrame([dict(obj)])
            x = x[[c for c in self.feature_cols if c in x.columns]]
        x = x.replace([np.inf, -np.inf], np.nan)
        for c in self.feature_cols:
            if c not in x.columns:
                x[c] = self._fill_values.get(c, 0.0)
            x[c] = x[c].fillna(self._fill_values.get(c, 0.0))
        return x[self.feature_cols]

    def predict_proba(self, obj: dict | pd.Series | pd.DataFrame) -> pd.DataFrame:
        """返回每行的 [p_hit, p_win]。"""
        x = self._prepare_x(obj)
        probs = pd.DataFrame(index=x.index)
        if self.hit_estimator is not None:
            probs["p_hit"] = self.hit_estimator.predict_proba(x)[:, 1]
        else:
            probs["p_hit"] = 0.0
        if self.win_estimator is not None:
            probs["p_win"] = self.win_estimator.predict_proba(x)[:, 1]
        else:
            probs["p_win"] = 0.0
        return probs

    def predict_score(self, obj: dict | pd.Series | pd.DataFrame) -> float | np.ndarray:
        """返回 0–100 的混合模型分。"""
        probs = self.predict_proba(obj)
        score = (
            self.blend_hit * probs["p_hit"] + self.blend_win * probs["p_win"]
        ) * 100.0
        if isinstance(score, pd.Series):
            return score.clip(0, 100).values
        return float(np.clip(score, 0, 100))

    def feature_importance(self) -> dict[str, float]:
        """从 estimator 中提取特征重要性（若支持）。"""
        out = {}
        est = self.hit_estimator
        if est is None:
            return out
        if hasattr(est, "feature_importances_"):
            for c, v in zip(self.feature_cols, est.feature_importances_):
                out[c] = float(v)
        elif hasattr(est, "coef_"):
            for c, v in zip(self.feature_cols, est.coef_.flatten()):
                out[c] = float(abs(v))
        return out

    def save(self, model_dir: Path) -> None:
        """保存模型、特征列表、填充值。"""
        model_dir.mkdir(parents=True, exist_ok=True)
        joblib.dump(
            {
                "feature_cols": self.feature_cols,
                "hit_estimator": self.hit_estimator,
                "win_estimator": self.win_estimator,
                "blend_hit": self.blend_hit,
                "blend_win": self.blend_win,
                "fill_values": self._fill_values,
            },
            model_dir / "limit_up_model.joblib",
        )
        (model_dir / "model_features.json").write_text(
            json.dumps(self.feature_cols, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        fi = self.feature_importance()
        if fi:
            (model_dir / "feature_importance.json").write_text(
                json.dumps(dict(sorted(fi.items(), key=lambda kv: -kv[1])), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    @classmethod
    def load(cls, model_dir: Path) -> "LimitUpModel":
        """从目录加载模型。"""
        payload = joblib.load(model_dir / "limit_up_model.joblib")
        m = cls(
            feature_cols=payload["feature_cols"],
            hit_estimator=payload["hit_estimator"],
            win_estimator=payload["win_estimator"],
            blend_hit=payload["blend_hit"],
            blend_win=payload["blend_win"],
        )
        m._fill_values = payload.get("fill_values", {})
        return m


def evaluate_model(model: LimitUpModel, df: pd.DataFrame) -> dict:
    """在验证集上计算 AUC、Top-K 命中率/胜率、Rank IC。"""
    from sklearn.metrics import roc_auc_score
    from plays.limit_up.backtest.metrics import rank_ic, precision_at_k, win_rate

    x = _clean_features(df, model.feature_cols)
    probs = model.predict_proba(x)
    df = df.copy().reset_index(drop=True)
    df["model_score"] = (
        model.blend_hit * probs["p_hit"] + model.blend_win * probs["p_win"]
    ) * 100.0

    out = {}
    for target in ("hit_limit_3", "fwd_ret_3_positive"):
        valid = df.dropna(subset=[target, "model_score"])
        if len(valid) > 1 and valid[target].nunique() > 1:
            out[f"auc_{target}"] = float(roc_auc_score(valid[target].astype(int), valid["model_score"]))

    out["ic_hit_limit_3"] = rank_ic(df["model_score"], df["hit_limit_3"])
    out["ic_fwd_ret_3"] = rank_ic(df["model_score"], df["fwd_ret_3"])

    # 按日期分组 Top-K（训练集是一天一只，近似评估）
    top_k = {3: [], 5: []}
    for date, sub in df.dropna(subset=["model_score"]).groupby("trade_date"):
        for k in top_k:
            if len(sub) < k:
                continue
            top = sub.nlargest(k, "model_score")
            hit = top["hit_limit_3"].mean() if top["hit_limit_3"].notna().any() else None
            wr = (top["fwd_ret_3"] > 0).mean() if top["fwd_ret_3"].notna().any() else None
            top_k[k].append({"hit": hit, "win": wr})

    for k, vals in top_k.items():
        hits = [v["hit"] for v in vals if v["hit"] is not None]
        wins = [v["win"] for v in vals if v["win"] is not None]
        out[f"top{k}_hit_rate"] = float(np.mean(hits)) if hits else None
        out[f"top{k}_win_rate"] = float(np.mean(wins)) if wins else None

    return out
