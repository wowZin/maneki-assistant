"""Sentiment 多信号 Ensemble（mine 备用）。"""
from __future__ import annotations

import pandas as pd

from plays.limit_up.factors._helpers import safe
from plays.limit_up.factors.sentiment.position_combo import factor_sentiment_position_combo
from plays.limit_up.factors.sentiment.volatility_combo import factor_sentiment_volatility_combo
from plays.limit_up.factors.sentiment.amount_combo import factor_sentiment_amount_combo


def factor_sentiment_ensemble(row) -> float:
    score = (
        factor_sentiment_position_combo(row) * 0.4
        + factor_sentiment_volatility_combo(row) * 0.3
        + factor_sentiment_amount_combo(row) * 0.3
    )
    return round(max(0.0, score), 2)
