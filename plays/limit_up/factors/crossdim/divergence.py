"""维度背离：shortterm 高 + technical 中性。"""
from __future__ import annotations

import pandas as pd

from plays.limit_up.factors._helpers import safe


def factor_dimension_divergence(row) -> float:
    st = safe(row.get("shortterm"))
    tech = safe(row.get("technical"))

    if st >= 60 and tech <= 40:
        return 12.0
    if st >= 50 and tech <= 35:
        return 8.0
    return 0.0
