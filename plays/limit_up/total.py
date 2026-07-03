"""打板玩法唯一总分聚合。

对外唯一入口 `total_score(row)`。

公式（训练集优化后）：
  total_score = round(max(0.0, 1.0 * factor_quality_combo(row)), 2)

`factor_quality_combo` 由 `plays.limit_up.factors.optimized.quality_combo` 实现。
它不是单一阈值，而是把涨停基因、换手、动量、位置、当日涨幅、技术分
六个因子组合成 60/80/100 的阶梯评分；高分档在多因子共振时出现，
从而过滤追高失败案例。推送阈值建议 ≥ 85（只推 100 分档）。
"""

from __future__ import annotations

from plays.limit_up.factors import TOTAL_SCORE_COMPONENTS, REGISTRY


def total_score(row) -> float:
    """打板玩法唯一总分。row 可以是 pd.Series 或 dict。"""
    score = 0.0
    for name, weight in TOTAL_SCORE_COMPONENTS.items():
        score += REGISTRY[name](row) * weight
    return round(max(0.0, score), 2)


__all__ = ["total_score", "TOTAL_SCORE_COMPONENTS"]
