"""资金面重构评分：换手率 + 绝对成交额是核心，方向次要。"""
from __future__ import annotations

import pandas as pd

from plays.limit_up.factors._helpers import safe


def factor_fundflow_rebuilt(row) -> float:
    turnover = safe(row.get("turnover_rate"), 0.0)
    turnover_f = safe(row.get("turnover_rate_f"), 0.0)
    amount = safe(row.get("avg_amount_5d"), 0.0)
    net_mf_ratio = safe(row.get("net_mf_ratio"), 0.0)

    score = 0.0
    if turnover >= 15:
        score += 25
    elif turnover >= 10:
        score += 18
    elif turnover >= 5:
        score += 10
    elif turnover >= 2:
        score += 4

    if turnover_f >= 15:
        score += 10
    elif turnover_f >= 8:
        score += 5

    if amount >= 2_000_000:
        score += 10
    elif amount >= 500_000:
        score += 5

    if net_mf_ratio > 0.3:
        score += 5
    elif net_mf_ratio > 0.1:
        score += 2

    return min(80.0, score)
