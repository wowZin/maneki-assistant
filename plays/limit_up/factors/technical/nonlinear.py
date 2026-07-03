"""技术面非线性组合：位置 * 波动 * 成交额，奖励中高位置 + 高波动 + 高成交额。"""
from __future__ import annotations

import pandas as pd

from plays.limit_up.factors._helpers import safe


def factor_technical_nonlinear(row) -> float:
    pos = safe(row.get("position_20d"), 0.5)
    std10 = safe(row.get("pct_chg_std_10d"), 0.0)
    avg_amount = safe(row.get("avg_amount_5d"), 0.0)
    t10 = safe(row.get("trailing_10_pit", row.get("trailing_10")), 0.0)
    gene20 = safe(row.get("limit_up_count_20d"), 0.0)

    score = 0.0
    if 0.45 <= pos <= 0.80:
        score += 10.0
    elif 0.30 <= pos <= 0.90:
        score += 5.0
    elif pos > 0.95:
        score -= 5.0

    if std10 >= 5.5:
        score += 12.0
    elif std10 >= 4.0:
        score += 7.0
    elif std10 >= 2.5:
        score += 3.0

    if avg_amount >= 1_500_000:
        score += 10.0
    elif avg_amount >= 800_000:
        score += 6.0
    elif avg_amount >= 300_000:
        score += 3.0

    if gene20 >= 2:
        score += 6.0
    elif gene20 >= 1:
        score += 3.0

    if t10 > 0.35:
        score *= 0.75
    elif t10 > 0.25:
        score *= 0.85

    return round(max(0.0, score), 2)
