"""total_score 唯一总分测试。"""

from __future__ import annotations

import pandas as pd
import pytest

from plays.limit_up.total import total_score, TOTAL_SCORE_COMPONENTS
from plays.limit_up.factors import REGISTRY


@pytest.fixture
def tier100_row():
    return pd.Series({
        "turnover_rate": 20.0,
        "trailing_10": 0.15,
        "position_20d": 0.60,
        "pct_chg_score_day": 4.0,
        "technical": 35.0,
        "shortterm": 32.0,
        "fundflow": 15.0,
        "limit_up_count_20d": 3,
    })


@pytest.fixture
def tier95_row():
    return pd.Series({
        "turnover_rate": 15.0,
        "trailing_10": 0.12,
        "position_20d": 0.60,
        "pct_chg_score_day": 4.0,
        "technical": 35.0,
        "shortterm": 28.0,
        "fundflow": 12.0,
        "limit_up_count_20d": 3,
    })


def test_total_score_equals_quality_combo(tier100_row, tier95_row):
    """total_score 当前唯一组件为 quality_combo，两者结果一致。"""
    assert total_score(tier100_row) == REGISTRY["quality_combo"](tier100_row) == 100.0
    assert total_score(tier95_row) == REGISTRY["quality_combo"](tier95_row) == 95.0


def test_total_score_excludes_chasing():
    """trailing 过高（追高）时 quality_combo 为 0，total_score 亦为 0。"""
    chasing = pd.Series({
        "turnover_rate": 20.0,
        "trailing_10": 0.50,
        "position_20d": 0.60,
        "pct_chg_score_day": 4.0,
        "technical": 35.0,
        "shortterm": 32.0,
        "fundflow": 15.0,
        "limit_up_count_20d": 3,
    })
    assert total_score(chasing) == 0.0


def test_total_score_never_negative():
    """total_score 保证非负。"""
    assert total_score(pd.Series({})) >= 0


def test_total_score_components_are_deterministic(tier100_row):
    """相同 row 多次调用结果一致，且组件确实在 REGISTRY 中。"""
    a = total_score(tier100_row)
    b = total_score(tier100_row)
    assert a == b
    assert set(TOTAL_SCORE_COMPONENTS) == {"quality_combo"}
    for name in TOTAL_SCORE_COMPONENTS:
        assert name in REGISTRY
