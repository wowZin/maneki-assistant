"""model_score 因子单测。"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pandas as pd

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from plays.limit_up.factors.optimized.model_score import (
    factor_model_score,
    clear_model_cache,
)
from plays.limit_up.factors.optimized.quality_combo import factor_quality_combo
from plays.limit_up.backtest.model import LimitUpModel, DEFAULT_FEATURES


def _dummy_row():
    return {
        "turnover_rate": 15.0,
        "trailing_10": 0.10,
        "trailing_5": 0.05,
        "position_20d": 0.5,
        "pullback_10d": 0.1,
        "pct_chg_score_day": 3.0,
        "technical": 60.0,
        "shortterm": 50.0,
        "fundflow": 20.0,
        "limit_up_count_20d": 3.0,
    }


def test_fallback_without_model():
    clear_model_cache()
    os.environ["LIMIT_UP_MODEL_PATH"] = str(Path(tempfile.gettempdir()) / "nonexistent_model_dir")
    row = _dummy_row()
    score = factor_model_score(row)
    # 无模型时应回退到 quality_combo
    assert score == factor_quality_combo(row)


def test_model_score_with_dummy_artifact():
    from sklearn.tree import DecisionTreeClassifier

    clear_model_cache()
    # 构造一个最小训练集
    n = 30
    df = pd.DataFrame({
        "hit_limit_3": [1] * (n // 2) + [0] * (n - n // 2),
        "fwd_ret_3": [0.05] * (n // 2) + [-0.02] * (n - n // 2),
    })
    for c in DEFAULT_FEATURES:
        df[c] = 0.0
    # 让模型能区分：涨停样本某个特征大
    df.loc[df["hit_limit_3"] == 1, "turnover_rate"] = 20.0
    df.loc[df["hit_limit_3"] == 0, "turnover_rate"] = 5.0
    df.loc[df["hit_limit_3"] == 1, "prev_turnover"] = 15.0
    df.loc[df["hit_limit_3"] == 0, "prev_turnover"] = 3.0
    df["fwd_ret_3_positive"] = (df["fwd_ret_3"] > 0).astype(int)

    hit_est = DecisionTreeClassifier(max_depth=2, random_state=42)
    win_est = DecisionTreeClassifier(max_depth=2, random_state=42)
    model = LimitUpModel(
        hit_estimator=hit_est,
        win_estimator=win_est,
        blend_hit=1.0,
        blend_win=0.0,
    )
    model.fit(df)

    with tempfile.TemporaryDirectory() as tmp:
        model_dir = Path(tmp)
        model.save(model_dir)
        os.environ["LIMIT_UP_MODEL_PATH"] = str(model_dir)
        clear_model_cache()

        row = _dummy_row()
        row["prev_turnover"] = 15.0
        score = factor_model_score(row)
        assert 0 <= score <= 100
        # 高换手样本应比低换手样本分高
        row_low = dict(row)
        row_low["turnover_rate"] = 2.0
        row_low["prev_turnover"] = 1.0
        score_low = factor_model_score(row_low)
        assert score_low < score


def test_model_score_differs_with_intraday_features():
    """验证模型分会随 id_* 特征变化（模型路径真正用到了分时数据）。"""
    from sklearn.tree import DecisionTreeClassifier

    clear_model_cache()
    # 构造最小可区分数据集：id_range 是主要区分特征，其余特征相同
    df = pd.DataFrame({
        "hit_limit_3": [1, 1, 0, 0],
        "fwd_ret_3": [0.05, 0.05, -0.02, -0.02],
    })
    for c in DEFAULT_FEATURES:
        df[c] = 0.0
    df["id_range"] = [0.12, 0.11, 0.03, 0.02]
    df["fwd_ret_3_positive"] = (df["fwd_ret_3"] > 0).astype(int)

    hit_est = DecisionTreeClassifier(random_state=42)
    win_est = DecisionTreeClassifier(random_state=42)
    model = LimitUpModel(
        hit_estimator=hit_est,
        win_estimator=win_est,
        blend_hit=1.0,
        blend_win=0.0,
    )
    model.fit(df)

    with tempfile.TemporaryDirectory() as tmp:
        model_dir = Path(tmp)
        model.save(model_dir)
        os.environ["LIMIT_UP_MODEL_PATH"] = str(model_dir)
        clear_model_cache()

        row = _dummy_row()
        row["id_range"] = 0.12
        score_high = factor_model_score(row)
        row_low = dict(row)
        row_low["id_range"] = 0.03
        score_low = factor_model_score(row_low)
        assert 0 <= score_high <= 100
        assert 0 <= score_low <= 100
        assert score_high != score_low


if __name__ == "__main__":
    test_fallback_without_model()
    test_model_score_with_dummy_artifact()
    test_model_score_differs_with_intraday_features()
    print("test_model_score OK")
