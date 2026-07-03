"""tests for plays/watchdog/signals.py"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

from plays.watchdog.signals import check_entry, check_exit, compute_factor_scores, is_worth_watching


def _make_row(pct: float, gap: float, turnover: float, vol_ratio: float,
              position: float = 0.6, trailing: float = 0.1,
              pullback10: float = 0.05, pullback20: float = 0.08,
              limit20: float = 2.0, **kwargs):
    return {
        "pct_chg_score_day": pct,
        "gap_up": gap,
        "gap_up_pit": gap,
        "turnover_rate": turnover,
        "turnover_rate_f": turnover,
        "vol_ratio_proxy": vol_ratio,
        "volume_ratio": vol_ratio,
        "amount_ratio": 1.5,
        "vwap": 10.0,
        "position_20d": position,
        "trailing_10": trailing,
        "trailing_10_pit": trailing,
        "trailing_5": trailing * 0.5,
        "pullback_10d": pullback10,
        "pullback_20d": pullback20,
        "pct_chg_std_10d": 3.0,
        "pct_chg_std_5d": 2.5,
        "max_pct_chg_5d": 4.0,
        "avg_amount_5d": 1_000_000.0,
        "limit_up_count_20d": limit20,
        "limit_up_count_60d": 3.0,
        "circ_mv": 10_000_000.0,
        "pe": 20.0,
        "pb": 2.0,
        "fundamental": 50.0,
        "technical": 60.0,
        "fundflow": 70.0,
        "sentiment": 40.0,
        "shortterm": 55.0,
        **kwargs,
    }


def test_quality_combo_high_score():
    row = _make_row(pct=4.0, gap=2.0, turnover=15.0, vol_ratio=1.8,
                    technical=35.0, shortterm=28.0, fundflow=15.0,
                    limit20=3.0, position=0.6, trailing=0.12)
    scores = compute_factor_scores(row)
    assert scores["quality_combo"] == 95.0, scores


def test_breakout_entry():
    row = _make_row(pct=3.0, gap=1.0, turnover=8.0, vol_ratio=1.6,
                    pullback10=0.02, pullback20=0.08, position=0.65)
    scores = compute_factor_scores(row)
    triggered, sig_type, reason = check_entry(row, scores, [])
    assert triggered
    assert sig_type == "breakout"


def test_sprint_entry():
    row = _make_row(pct=8.0, gap=3.0, turnover=15.0, vol_ratio=3.0,
                    technical=35.0, shortterm=28.0, fundflow=15.0,
                    limit20=3.0, position=0.6, trailing=0.12,
                    pullback10=0.1, pullback20=0.25)
    scores = compute_factor_scores(row)
    triggered, sig_type, reason = check_entry(row, scores, [])
    assert triggered
    assert sig_type == "sprint"


def test_stop_loss():
    triggered, reason = check_exit(10.0, 10.5, 9.7, 5, 10.0, {})
    assert triggered
    assert "固定止损" in reason


def test_trailing_stop():
    triggered, reason = check_exit(10.0, 11.0, 10.6, 5, 10.0, {})
    assert triggered
    assert "移动止损" in reason


def test_is_worth_watching():
    row = _make_row(pct=4.0, gap=2.0, turnover=15.0, vol_ratio=1.8,
                    technical=35.0, shortterm=28.0, fundflow=15.0,
                    limit20=3.0, position=0.6, trailing=0.12)
    scores = compute_factor_scores(row)
    ok, reason = is_worth_watching(row, scores)
    assert ok


if __name__ == "__main__":
    test_quality_combo_high_score()
    test_breakout_entry()
    test_sprint_entry()
    test_stop_loss()
    test_trailing_stop()
    test_is_worth_watching()
    print("signals tests passed")
