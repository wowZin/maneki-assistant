"""tests for plays/watchdog/signals.py"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

from plays.watchdog.signals import (
    check_entry, check_exit, check_abnormal,
    compute_factor_scores, is_worth_watching,
)


def _make_row(pct: float, gap: float, turnover: float, vol_ratio: float,
              position: float = 0.6, trailing: float = 0.1,
              pullback10: float = 0.05, pullback20: float = 0.08,
              limit20: float = 2.0, last_price: float = 10.0,
              vwap: float = 10.0, **kwargs):
    return {
        "last_price": last_price,
        "pct_chg_score_day": pct,
        "gap_up": gap,
        "gap_up_pit": gap,
        "turnover_rate": turnover,
        "turnover_rate_f": turnover,
        "vol_ratio_proxy": vol_ratio,
        "volume_ratio": vol_ratio,
        "amount_ratio": 1.5,
        "vwap": vwap,
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


def _make_l1_row(last: float = 10.5, vwap: float = 10.3, pct: float = 3.0,
                 turnover: float = 8.0, vol_ratio: float = 1.6,
                 bid1: float = 0, ask1: float = 0,
                 inner_vol: float = 0, outer_vol: float = 0):
    """check_entry 所需的完整实时行。

    生产端 watchdog.py 在 realtime_row 基础上补 last/bid1/ask1/inner_vol/outer_vol
    后再调 check_entry，这里对齐该行为。
    """
    row = _make_row(pct=pct, gap=1.0, turnover=turnover, vol_ratio=vol_ratio,
                    last_price=last, vwap=vwap)
    row.update({
        "last": last,
        "bid1": bid1,
        "ask1": ask1,
        "inner_vol": inner_vol,
        "outer_vol": outer_vol,
    })
    return row


def test_vwap_break_entry():
    """信号1：价突破 VWAP → vwap_break。

    （原 test_breakout_entry：断言过时——信号类型 "breakout" 已在全因子重构中
    重命名为 vwap_break；旧用例数据缺 L1/L2 字段且 model_score=8.92 过不了 40 分闸。）
    """
    row = _make_l1_row(last=10.5, vwap=10.3, pct=3.0)
    triggered, sig_type, reason = check_entry(row, {"model_score": 50.0})
    assert triggered, reason
    assert sig_type == "vwap_break"


def test_volume_surge_entry():
    """信号2：放量拉升 → volume_surge（last<VWAP 以绕过信号1，只验证信号2）。

    （原 test_sprint_entry：断言过时——信号类型 "sprint" 已重命名为 volume_surge。）
    """
    row = _make_l1_row(last=10.2, vwap=10.3, pct=8.0, turnover=15.0, vol_ratio=3.0)
    triggered, sig_type, reason = check_entry(row, {"model_score": 50.0})
    assert triggered, reason
    assert sig_type == "volume_surge"


def test_entry_model_score_gate_boundary():
    """model_score 全局闸边界：39.9 拒 / 40.0 过（ENTRY_CONFIG['min_model_score']=40）。"""
    row = _make_l1_row(last=10.5, vwap=10.3, pct=3.0)
    triggered, _, _ = check_entry(row, {"model_score": 39.9})
    assert not triggered
    triggered, sig_type, _ = check_entry(row, {"model_score": 40.0})
    assert triggered
    assert sig_type == "vwap_break"


def test_stop_loss():
    triggered, reason = check_exit(10.0, 10.5, 9.7, 5, 10.0, {})
    assert triggered
    assert "固定止损" in reason


def test_trailing_stop():
    triggered, reason = check_exit(10.0, 11.0, 10.6, 5, 10.0, {})
    assert triggered
    assert "移动止损" in reason


def test_time_stop_held_minutes_boundary():
    """held_minutes 是真实持仓分钟（非轮数）：59 不触发 / 60 触发时间止损。

    EXIT_CONFIG['time_force_exit_minutes']=60。构造无止损/止盈/回撤干扰的持仓：
    现价10.1（+1%），未破固定止损9.8、未触移动止损9.894、止盈线5%未到、
    涨幅1%<3%不启用回调出场、scores 为空无反转/高位出场。
    """
    # 59 分钟：不触发任何出场
    triggered, reason = check_exit(10.0, 10.2, 10.1, 59, 10.0, {})
    assert not triggered, reason
    # 60 分钟：触发时间止损
    triggered, reason = check_exit(10.0, 10.2, 10.1, 60, 10.0, {})
    assert triggered
    assert "时间止损" in reason


def test_is_worth_watching():
    row = _make_row(pct=4.0, gap=2.0, turnover=15.0, vol_ratio=1.8,
                    technical=35.0, shortterm=28.0, fundflow=15.0,
                    limit20=3.0, position=0.6, trailing=0.12)
    scores = compute_factor_scores(row)
    ok, reason = is_worth_watching(row, scores)
    assert ok


# ── 异常状态置信度 ──

def test_abnormal_low_position_no_critical():
    """低位放量下跌，即使资金流出大，也不应直接 critical（可能是诱空）。"""
    row = _make_row(pct=-5.0, gap=-1.0, turnover=15.0, vol_ratio=2.0,
                    position=0.2, last_price=9.5, vwap=10.0)
    abn, level, reason = check_abnormal(row, {}, -80_000_000, 1.5, 0)
    assert not abn or level != "critical", reason


def test_abnormal_high_position_critical():
    """高位 + 大跌 + 资金流出 + 抛压 → critical。"""
    row = _make_row(pct=-6.0, gap=-2.0, turnover=18.0, vol_ratio=2.5,
                    position=0.9, last_price=9.4, vwap=10.0,
                    limit20=3.0)
    abn, level, reason = check_abnormal(row, {}, -60_000_000, 3.0, 0)
    assert abn
    assert level == "critical", reason


def test_abnormal_consecutive_outflow():
    """连续 3 轮净流出，加分后应触发 warning 或 critical。"""
    row = _make_row(pct=-2.0, gap=-0.5, turnover=10.0, vol_ratio=1.6,
                    position=0.7, last_price=9.8, vwap=10.0)
    abn, level, reason = check_abnormal(
        row, {}, -15_000_000, 2.5, 0,
        netflow_history=[-10_000_000, -20_000_000, -15_000_000]
    )
    assert abn


def test_abnormal_holding_drop():
    """持仓后急跌放量 → warning。

    （断言过时修正：原断言 critical。当前置信度=65：持仓急跌25 + 跌破VWAP15 +
    量比10 + 大跌15，净流出-200万不到-500万档计0分、抛压比1.2计0分，
    65 < critical_score=70，正确级别是 warning。）
    """
    row = _make_row(pct=-6.0, gap=-2.0, turnover=15.0, vol_ratio=2.5,
                    position=0.75, last_price=9.4, vwap=10.0,
                    limit20=3.0)
    abn, level, reason = check_abnormal(row, {}, -2_000_000, 1.2, 10.0)
    assert abn
    assert level == "warning", reason


def test_abnormal_holding_drop_critical():
    """持仓急跌放量 + 大额净流出 → critical（65 + 净流出-2500万档12分 = 77 ≥ 70）。"""
    row = _make_row(pct=-6.0, gap=-2.0, turnover=15.0, vol_ratio=2.5,
                    position=0.75, last_price=9.4, vwap=10.0,
                    limit20=3.0)
    abn, level, reason = check_abnormal(row, {}, -25_000_000, 1.2, 10.0)
    assert abn
    assert level == "critical", reason


def test_abnormal_bear_trap():
    """诱空：上一轮跌破VWAP超1%，本轮快速回拉≥0.8%且缩量 → bear_trap。

    双扫描确认：prev 9.8/10.0=-2.0% → curr 9.95/10.0=-0.5%，回拉1.5%≥0.8%；
    量比 1.5→1.0 缩量（<0.8倍）。置信度0 < 70，返回 bear_trap 而非 warning/critical。
    """
    row = _make_row(pct=-1.0, gap=-0.5, turnover=5.0, vol_ratio=1.0,
                    position=0.4, last_price=9.95, vwap=10.0)
    abn, level, reason = check_abnormal(
        row, {}, 0.0, 1.0, 0.0,
        prev_last=9.8, prev_vwap=10.0, prev_vol_ratio=1.5,
    )
    assert abn
    assert level == "bear_trap", reason


def test_abnormal_normal():
    """平静盘面：无净流出、VWAP上方、量比正常 → 不异常，level 为空。"""
    row = _make_row(pct=0.5, gap=0.2, turnover=3.0, vol_ratio=1.0,
                    position=0.5, last_price=10.1, vwap=10.0)
    abn, level, reason = check_abnormal(row, {}, 1_000_000, 1.0, 0.0)
    assert not abn
    assert level == ""


if __name__ == "__main__":
    test_quality_combo_high_score()
    test_vwap_break_entry()
    test_volume_surge_entry()
    test_entry_model_score_gate_boundary()
    test_stop_loss()
    test_trailing_stop()
    test_time_stop_held_minutes_boundary()
    test_is_worth_watching()
    test_abnormal_low_position_no_critical()
    test_abnormal_high_position_critical()
    test_abnormal_consecutive_outflow()
    test_abnormal_holding_drop()
    test_abnormal_holding_drop_critical()
    test_abnormal_bear_trap()
    test_abnormal_normal()
    print("signals tests passed")
