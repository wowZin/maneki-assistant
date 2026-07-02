"""基本面重构评分：奖励大盘 + 成长估值 + 概念丰富。"""
from __future__ import annotations

import pandas as pd

from plays.limit_up.factors._helpers import safe


def factor_fundamental_rebuilt(row) -> float:
    circ_mv = safe(row.get("circ_mv"), 0.0)
    pb = safe(row.get("pb"), 0.0)
    pe = safe(row.get("pe"), 0.0)
    n_cpt = safe(row.get("n_concepts"), 0.0)

    score = 0.0
    mv_yi = circ_mv / 10000
    if mv_yi >= 200:
        score += 20
    elif mv_yi >= 100:
        score += 15
    elif mv_yi >= 50:
        score += 10
    elif mv_yi >= 20:
        score += 5

    if pb > 8:
        score += 10
    elif pb > 5:
        score += 7
    elif pb > 3:
        score += 3

    if pe > 50 or pe <= 0:
        score += 8
    elif pe > 30:
        score += 4

    if n_cpt >= 5:
        score += 10
    elif n_cpt >= 3:
        score += 5

    return min(80.0, score)
