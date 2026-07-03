"""涨停基因 + 成交额组合。"""
from __future__ import annotations

import pandas as pd

from plays.limit_up.factors._helpers import safe


def factor_limit_gene_amount(row) -> float:
    gene20 = safe(row.get("limit_up_count_20d"), 0.0)
    gene60 = safe(row.get("limit_up_count_60d"), 0.0)
    avg_amount = safe(row.get("avg_amount_5d"), 0.0)
    std10 = safe(row.get("pct_chg_std_10d"), 0.0)

    if gene20 < 1 and gene60 < 2:
        return 0.0

    score = 0.0
    if gene20 >= 3:
        score += 12.0
    elif gene20 >= 2:
        score += 8.0
    elif gene20 >= 1:
        score += 4.0

    if gene60 >= 5:
        score += 8.0
    elif gene60 >= 3:
        score += 4.0

    if avg_amount >= 1_500_000:
        score += 10.0
    elif avg_amount >= 800_000:
        score += 6.0
    elif avg_amount >= 300_000:
        score += 3.0

    if std10 >= 4.5:
        score += 6.0
    elif std10 >= 3.0:
        score += 3.0

    return round(score, 2)
