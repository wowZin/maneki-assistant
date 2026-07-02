"""综合质量加分：多维度共振。"""
from __future__ import annotations

import pandas as pd

from plays.limit_up.factors._helpers import safe


def factor_total_quality_bonus(row) -> float:
    dims = ["fundamental", "technical", "fundflow", "sentiment", "shortterm"]
    high_dims = sum(1 for d in dims if safe(row.get(d)) >= 50)

    if high_dims >= 4:
        return 12.0
    if high_dims >= 3:
        return 6.0
    return 0.0
