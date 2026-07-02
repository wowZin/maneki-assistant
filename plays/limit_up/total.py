"""打板玩法唯一总分聚合。

对外唯一入口 `total_score(row)`。所有历史版本（`new_total_v2 / balanced_total /
balanced_total_v2 / sentiment_adaptive_total / ultimate_total_v1~v5 / cpt_* /
balanced_ensemble` 等）已废弃。

公式：
  total_score = round(max(0.0,
      0.4 * factor_sentiment_amount_boosted(row)
    + 0.5 * factor_sentiment_position_combo(row)
    + 0.7 * factor_sentiment_volatility_combo(row)
  ), 2)

权重来源：回测集 `plays/limit_up/backtest/out/all_factors_report.md` 上的网格
搜索，IC hit_limit_3 = 0.338，chasing_score = 0.185。
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
