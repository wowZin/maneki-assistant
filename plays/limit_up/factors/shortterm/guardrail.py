"""追高护栏：综合 trailing + position + pullback。返回乘性调整系数应用到基础分。"""
from __future__ import annotations

import pandas as pd

from plays.limit_up.factors._helpers import safe


def factor_chasing_guardrail(
    total: float,
    trailing_5: float | None = None,
    trailing_10: float | None = None,
    position_20d: float | None = None,
    pullback_10d: float | None = None,
) -> float:
    t5 = safe(trailing_5)
    t10 = safe(trailing_10)
    pos = safe(position_20d, 0.5)
    pb = safe(pullback_10d, 0.1)

    adj = total
    penalty = 1.0

    if t10 > 0.30:
        penalty *= 0.80
    elif t10 > 0.20:
        penalty *= 0.90
    elif t10 > 0.10:
        penalty *= 0.95

    if pos > 0.85 and pb < 0.03:
        penalty *= 0.85

    if t5 > 0.15:
        penalty *= 0.92

    if t10 < -0.05 and pos < 0.30:
        penalty *= 1.05

    return adj * penalty
