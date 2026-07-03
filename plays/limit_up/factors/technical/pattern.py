"""形态/反转因子：弱转强 / 跳空高开 / 连阳强度。"""
from __future__ import annotations

import pandas as pd

from plays.limit_up.factors._helpers import safe


def factor_reversal_signal(row) -> float:
    rev = safe(row.get("reversal_signal"), 0.0)
    if rev:
        return 20.0
    return 0.0


def factor_gap_up_quality(row) -> float:
    gap = safe(row.get("gap_up_pit", row.get("gap_up")), 0.0)
    vol_r = safe(row.get("vol_ratio_proxy"), 1.0)
    pos = safe(row.get("position_20d"), 0.5)
    pb = safe(row.get("pullback_10d"), 0.0)

    if gap > 3.0 and vol_r > 1.5 and pos < 0.75:
        return 18.0
    if gap > 2.0 and vol_r > 1.3 and pb > 0.03:
        return 12.0
    if gap > 5.0 and pos > 0.85:
        return -5.0
    return 0.0


def factor_consecutive_strength(row) -> float:
    cons_up = safe(row.get("consecutive_up"), 0.0)
    pct_5d = safe(row.get("avg_pct_chg_5d"), 0.0)

    if cons_up >= 3 and 0.5 <= pct_5d <= 3.0:
        return 10.0
    if cons_up >= 5 and pct_5d > 3.0:
        return -5.0
    return 0.0
