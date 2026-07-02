"""total_score 唯一总分测试。"""

from __future__ import annotations

import math
import random

import pandas as pd
import pytest

from plays.limit_up.total import total_score, TOTAL_SCORE_COMPONENTS
from plays.limit_up.factors import REGISTRY


def test_total_score_formula(synthetic_row):
    """total_score = 0.4*A + 0.5*B + 0.7*C（round to 2）"""
    expected = round(max(0.0,
        REGISTRY["sentiment_amount_boosted"](synthetic_row) * 0.4
        + REGISTRY["sentiment_position_combo"](synthetic_row) * 0.5
        + REGISTRY["sentiment_volatility_combo"](synthetic_row) * 0.7,
    ), 2)
    assert total_score(synthetic_row) == expected


def test_total_score_equivalent_to_v5():
    """确认 total_score 与原 factor_ultimate_total_v5 在多组随机 row 上完全等价。"""
    from plays.limit_up.backtest.factor_lib import factor_ultimate_total_v5

    random.seed(42)
    for _ in range(50):
        row = pd.Series({
            "sentiment": random.uniform(0, 80),
            "position_20d": random.uniform(0, 1),
            "trailing_10": random.uniform(-0.1, 0.5),
            "pct_chg_std_10d": random.uniform(0, 10),
            "limit_up_count_20d": random.randint(0, 6),
            "avg_amount_5d": random.uniform(0, 5_000_000),
        })
        assert abs(total_score(row) - round(max(0.0, factor_ultimate_total_v5(row)), 2)) < 0.01


def test_total_score_low_sentiment_zero():
    """sentiment<30 时，两个 combo 组件返回 0，total_score 只剩 amount_boosted*0.4。"""
    low_sent = pd.Series({"sentiment": 20.0, "avg_amount_5d": 0.0})
    v = total_score(low_sent)
    assert v == 8.0  # 20 * 0.4 = 8


def test_total_score_never_negative(synthetic_row):
    """total_score 保证非负。"""
    zero_row = pd.Series({"sentiment": 0.0})
    assert total_score(zero_row) >= 0


def test_total_score_components_are_deterministic(synthetic_row):
    """相同 row 多次调用结果一致。"""
    a = total_score(synthetic_row)
    b = total_score(synthetic_row)
    assert a == b


def test_total_score_uses_registry_components():
    """公式的三个组件确实是 REGISTRY 中的因子，权重表非空。"""
    assert set(TOTAL_SCORE_COMPONENTS) == {
        "sentiment_amount_boosted",
        "sentiment_position_combo",
        "sentiment_volatility_combo",
    }
    for name in TOTAL_SCORE_COMPONENTS:
        assert name in REGISTRY
