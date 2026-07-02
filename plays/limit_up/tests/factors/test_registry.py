"""因子注册表与合成行的通用性测试。

对每个 REGISTRY 因子跑：
1. 类型检查（返回 float 或 int）
2. 边界检查（非因子设计不允许的极端值）
3. 合成 row + 极端 row 都能不抛异常
"""

from __future__ import annotations

import math

import pandas as pd
import pytest

from plays.limit_up.factors import DIMENSIONS, REGISTRY, TOTAL_SCORE_COMPONENTS


def test_registry_matches_dimensions():
    """DIMENSIONS 中列的因子必须都在 REGISTRY，反之亦然。"""
    from_dims = {f for names in DIMENSIONS.values() for f in names}
    from_reg = set(REGISTRY.keys())
    only_in_dims = from_dims - from_reg
    only_in_reg = from_reg - from_dims
    assert not only_in_dims, f"DIMENSIONS 列出但未注册: {only_in_dims}"
    assert not only_in_reg, f"REGISTRY 有但 DIMENSIONS 未列: {only_in_reg}"


def test_total_score_components_registered():
    for name in TOTAL_SCORE_COMPONENTS:
        assert name in REGISTRY, f"total_score 组件 {name} 未在 REGISTRY 中"


@pytest.mark.parametrize("factor_name", sorted(REGISTRY.keys()))
def test_factor_returns_numeric(factor_name, synthetic_row):
    """每个因子在合成 row 上必须返回数值型。"""
    fn = REGISTRY[factor_name]
    v = fn(synthetic_row)
    assert isinstance(v, (int, float)), f"{factor_name} 返回非数值: {type(v)}"
    assert not math.isnan(v), f"{factor_name} 返回 NaN"
    assert -1000 <= v <= 1000, f"{factor_name} 返回超出合理范围: {v}"


@pytest.mark.parametrize("factor_name", sorted(REGISTRY.keys()))
def test_factor_handles_empty_row(factor_name):
    """空 row（全 None）不能抛异常，应回落到 default。"""
    empty_row = pd.Series({}, dtype=object)
    fn = REGISTRY[factor_name]
    # 允许返回 0 或负值（例如 position_optimal 空 row → default 0.5 → 未加分）
    v = fn(empty_row)
    assert isinstance(v, (int, float))
    assert not math.isnan(v)


@pytest.mark.parametrize("factor_name", sorted(REGISTRY.keys()))
def test_factor_handles_extreme_high(factor_name):
    """极高值 row 不抛异常，因子应给上限或惩罚。"""
    extreme = pd.Series({
        "sentiment": 100.0, "shortterm": 100.0, "technical": 100.0,
        "fundflow": 100.0, "fundamental": 100.0,
        "position_20d": 0.99, "trailing_10": 0.60, "trailing_5": 0.30,
        "pct_chg_std_10d": 12.0, "pct_chg_std_5d": 10.0,
        "limit_up_count_20d": 10, "limit_up_count_60d": 25,
        "avg_amount_5d": 10_000_000, "turnover_rate": 50.0,
        "turnover_rate_f": 40.0, "volume_ratio": 5.0,
        "circ_mv": 1_000_000, "pb": 20.0, "pe": 200.0, "n_concepts": 15,
        "pullback_10d": 0.02, "pullback_20d": 0.03,
        "vol_ratio_proxy": 3.0, "amount_ratio": 5.0,
        "amount_3d_increasing": 1, "pct_chg_score_day": 9.5,
        "gap_up": 8.0, "consecutive_up": 8, "avg_pct_chg_5d": 5.0,
        "reversal_signal": 1, "upper_shadow_pct": 70,
        "net_mf_ratio": 0.6, "first_board": 0,
    })
    fn = REGISTRY[factor_name]
    v = fn(extreme)
    assert isinstance(v, (int, float))
    assert not math.isnan(v)
    assert -1000 <= v <= 1000


def test_chasing_guardrail_signature():
    """guardrail 是内部因子，签名与其它因子不同（接 total + kwargs）。"""
    from plays.limit_up.factors import factor_chasing_guardrail
    # 无追高 → 1.0 系数下不改变
    r = factor_chasing_guardrail(100.0, trailing_5=0.0, trailing_10=0.0,
                                  position_20d=0.5, pullback_10d=0.1)
    assert isinstance(r, float)
    assert 60 <= r <= 110
    # 追高 → 显著惩罚
    penalized = factor_chasing_guardrail(100.0, trailing_5=0.20, trailing_10=0.40,
                                          position_20d=0.95, pullback_10d=0.01)
    assert penalized < 100, f"追高情况下应有惩罚，实际 {penalized}"
