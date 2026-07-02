"""涨停基因因子：20日 / 60日 / 复合。"""
from __future__ import annotations

import pandas as pd

from plays.limit_up.factors._helpers import safe


def factor_limit_up_gene_20d(row) -> float:
    cnt = safe(row.get("limit_up_count_20d"))
    if cnt >= 5:
        return 25.0
    if cnt >= 3:
        return 18.0
    if cnt >= 2:
        return 12.0
    if cnt >= 1:
        return 6.0
    return 0.0


def factor_limit_up_gene_60d(row) -> float:
    cnt = safe(row.get("limit_up_count_60d"))
    if cnt >= 8:
        return 25.0
    if cnt >= 5:
        return 18.0
    if cnt >= 3:
        return 12.0
    if cnt >= 1:
        return 6.0
    return 0.0


def factor_limit_up_gene_composite(row) -> float:
    cnt20 = safe(row.get("limit_up_count_20d"))
    cnt60 = safe(row.get("limit_up_count_60d"))
    recent = min(cnt20, 6) * 3.0
    older = min(max(cnt60 - cnt20, 0), 8) * 1.5
    return min(recent + older, 25.0)
