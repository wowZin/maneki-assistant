"""量能结构因子：放量质量 / 资金加速 / 成交额突增。"""
from __future__ import annotations

import pandas as pd

from plays.limit_up.factors._helpers import safe


def factor_vol_expansion_quality(row) -> float:
    vol_r = safe(row.get("vol_ratio_proxy"), 1.0)
    pct = safe(row.get("pct_chg_score_day"), 0.0)
    pb = safe(row.get("pullback_10d"), 0.0)

    if vol_r > 1.5 and 2.0 <= pct <= 7.0 and pb > 0.03:
        return 18.0
    if vol_r > 1.3 and 2.0 <= pct <= 5.0:
        return 10.0
    if vol_r > 1.5 and pct < 0:
        return -10.0
    return 0.0


def factor_amount_acceleration(row) -> float:
    inc = safe(row.get("amount_3d_increasing"), 0.0)
    pct = safe(row.get("pct_chg_score_day"), 0.0)
    vol_r = safe(row.get("vol_ratio_proxy"), 1.0)

    if inc and pct > 0 and vol_r > 1.2:
        return 15.0
    if inc and pct > 0:
        return 8.0
    return 0.0


def factor_amount_surge(row) -> float:
    ratio = safe(row.get("amount_ratio"), 1.0)
    pct = safe(row.get("pct_chg_score_day"), 0.0)
    pb = safe(row.get("pullback_10d"), 0.0)

    if ratio > 2.0 and pct > 3.0 and pb > 0.03:
        return 12.0
    if ratio > 2.5 and pct > 5.0:
        return 5.0
    if ratio < 0.4:
        return -5.0
    return 0.0
