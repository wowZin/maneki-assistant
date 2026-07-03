"""技术面重构评分：强化换手/波动/位置/涨停基因/成交额，弱化深度回调/连阴/上影。"""
from __future__ import annotations

import pandas as pd

from plays.limit_up.factors._helpers import safe


def factor_technical_rebuilt(row) -> float:
    turnover = safe(row.get("turnover_rate"), 0.0)
    std10 = safe(row.get("pct_chg_std_10d"), 0.0)
    pos = safe(row.get("position_20d"), 0.5)
    gene60 = safe(row.get("limit_up_count_60d"), 0.0)
    gene20 = safe(row.get("limit_up_count_20d"), 0.0)
    avg_amount = safe(row.get("avg_amount_5d"), 0.0)
    pb10 = safe(row.get("pullback_10d"), 0.0)
    upper_shadow = safe(row.get("upper_shadow_pct"), 0.0)

    score = 0.0
    if turnover >= 10:
        score += 15
    elif turnover >= 5:
        score += 8

    if std10 >= 5:
        score += 15
    elif std10 >= 3.5:
        score += 8

    if 0.40 <= pos <= 0.80:
        score += 15
    elif 0.25 <= pos <= 0.90:
        score += 8

    score += min(gene60, 6) * 3.0
    score += min(gene20, 4) * 2.5

    if avg_amount >= 1_000_000:
        score += 10
    elif avg_amount >= 300_000:
        score += 5

    if pb10 > 0.15:
        score -= 10
    elif pb10 > 0.10:
        score -= 5
    if upper_shadow > 50:
        score -= 8

    return max(0.0, min(80.0, score))
