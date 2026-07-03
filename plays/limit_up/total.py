"""打板玩法唯一总分聚合。

对外唯一入口 `total_score(row)`。

当前默认组件（可通过环境变量切换）：
- `quality_combo`（默认）：硬阈值 0/95/100 高置信规则。
- `model_score`（设置 LIMIT_UP_USE_MODEL=true）：树模型输出的 0–100 连续分，
  模型不可用时自动回退到 `quality_combo`。

`factor_model_score` 由 `plays.limit_up.factors.optimized.model_score` 实现。
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
