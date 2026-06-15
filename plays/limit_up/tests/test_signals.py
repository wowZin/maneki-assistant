"""信号模块单元测试 — 对齐 linter 重构后的 API"""
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from plays.limit_up.signals import (
    check_concept_resonance, check_auction_rush,
    check_volume_breakout, check_limit_dna,
    check_smallcap, check_morning_power, check_whale_hunt,
    check_all_signals, signal_combination_judge,
    triggered_signals, signals_summary,
    SIGNAL_REGISTRY, SIGNAL_LABELS, SIGNAL_PRIORITY,
)


# ═══════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════

def _ctx(**overrides) -> dict:
    """构建最小化测试上下文，字段对齐 signals.py SignalContext"""
    ctx = {
        "ths_quote": None,
        "l2_net_flow": None,
        "l2_available": False,
        "hot_concept_tags": [],
        "concept_limit_counts": {},
        "hot_list_items": [],
        "basic_info": {},
        "auction_data": None,
        "prev_day_vol": 0,
        "limit_history_20d": 0,
        "limit_history_60d_max": 0,
        "is_morning": False,
        "is_afternoon": False,
        "scan_pct": 0.0,
    }
    ctx.update(overrides)
    return ctx


# ═══════════════════════════════════════════
# 概念共振
# ═══════════════════════════════════════════

def test_concept_resonance_strong():
    ctx = _ctx(
        hot_concept_tags=["PCB", "5G"],
        concept_limit_counts={"PCB": 6, "5G": 2},
    )
    r = check_concept_resonance("000001.SZ", ctx)
    assert r["triggered"] is True
    assert r["confidence"] == 1.0
    assert "PCB" in r["detail"]


def test_concept_resonance_medium():
    ctx = _ctx(
        hot_concept_tags=["芯片"],
        concept_limit_counts={"芯片": 3},
    )
    r = check_concept_resonance("000001.SZ", ctx)
    assert r["triggered"] is True
    assert r["confidence"] == 0.8


def test_concept_resonance_weak():
    ctx = _ctx(
        hot_concept_tags=["新能源"],
        concept_limit_counts={"新能源": 1},
    )
    r = check_concept_resonance("000001.SZ", ctx)
    assert r["triggered"] is True
    assert r["confidence"] == 0.4


def test_concept_resonance_none():
    ctx = _ctx(hot_concept_tags=["冷门"], concept_limit_counts={})
    r = check_concept_resonance("000001.SZ", ctx)
    assert r["triggered"] is False


def test_concept_resonance_no_tags():
    ctx = _ctx(hot_concept_tags=[])
    r = check_concept_resonance("000001.SZ", ctx)
    assert r["triggered"] is False


# ═══════════════════════════════════════════
# 竞价抢筹 — uses prev_day_vol + auction_data.vol
# ═══════════════════════════════════════════

def test_auction_rush_strong():
    ctx = _ctx(
        auction_data={"vol": 300000, "price": 10.5, "pre_close": 10.0},
        prev_day_vol=100000,
    )
    r = check_auction_rush("000001.SZ", ctx)
    assert r["triggered"] is True
    assert r["confidence"] == 1.0


def test_auction_rush_medium():
    ctx = _ctx(
        auction_data={"vol": 150000, "price": 10.3, "pre_close": 10.0},
        prev_day_vol=100000,
    )
    r = check_auction_rush("000001.SZ", ctx)
    assert r["triggered"] is True
    assert r["confidence"] == 0.7


def test_auction_rush_no_data():
    ctx = _ctx(auction_data=None, prev_day_vol=0)
    r = check_auction_rush("000001.SZ", ctx)
    assert r["triggered"] is False


# ═══════════════════════════════════════════
# 量价突破
# ═══════════════════════════════════════════

def test_volume_breakout_strong():
    ctx = _ctx(ths_quote={"vol_ratio": 3.5, "pct_chg": 8.0})
    r = check_volume_breakout("000001.SZ", ctx)
    assert r["triggered"] is True
    assert r["confidence"] >= 0.85


def test_volume_breakout_medium():
    ctx = _ctx(ths_quote={"vol_ratio": 2.5, "pct_chg": 5.5})
    r = check_volume_breakout("000001.SZ", ctx)
    assert r["triggered"] is True
    assert r["confidence"] >= 0.7


def test_volume_breakout_none():
    ctx = _ctx(ths_quote={"vol_ratio": 0.8, "pct_chg": 1.0})
    r = check_volume_breakout("000001.SZ", ctx)
    assert r["triggered"] is False


# ═══════════════════════════════════════════
# 连板基因
# ═══════════════════════════════════════════

def test_limit_dna_strong():
    ctx = _ctx(limit_history_20d=3)
    r = check_limit_dna("000001.SZ", ctx)
    assert r["triggered"] is True
    assert r["confidence"] == 1.0


def test_limit_dna_medium():
    ctx = _ctx(limit_history_20d=2)
    r = check_limit_dna("000001.SZ", ctx)
    assert r["triggered"] is True
    assert r["confidence"] == 0.85


def test_limit_dna_weak():
    ctx = _ctx(limit_history_20d=1)
    r = check_limit_dna("000001.SZ", ctx)
    assert r["triggered"] is True
    assert r["confidence"] == 0.6


def test_limit_dna_none():
    ctx = _ctx(limit_history_20d=0)
    r = check_limit_dna("000001.SZ", ctx)
    assert r["triggered"] is False


# ═══════════════════════════════════════════
# 小盘弹性
# ═══════════════════════════════════════════

def test_smallcap_tiny():
    ctx = _ctx(basic_info={"circ_mv": 150000})  # 15亿 (万元)
    r = check_smallcap("000001.SZ", ctx)
    assert r["triggered"] is True
    assert r["confidence"] == 1.0


def test_smallcap_mid():
    ctx = _ctx(basic_info={"circ_mv": 300000})  # 30亿
    r = check_smallcap("000001.SZ", ctx)
    assert r["triggered"] is True
    assert r["confidence"] == 0.7


def test_smallcap_large():
    ctx = _ctx(basic_info={"circ_mv": 2000000})  # 200亿
    r = check_smallcap("000001.SZ", ctx)
    assert r["triggered"] is False


# ═══════════════════════════════════════════
# 早盘强势 — uses is_morning flag
# ═══════════════════════════════════════════

def test_morning_power_strong():
    ctx = _ctx(is_morning=True, ths_quote={"pct_chg": 7.5})
    r = check_morning_power("000001.SZ", ctx)
    assert r["triggered"] is True
    assert r["confidence"] >= 0.7


def test_morning_power_afternoon():
    ctx = _ctx(is_morning=False, is_afternoon=True, ths_quote={"pct_chg": 8.0})
    r = check_morning_power("000001.SZ", ctx)
    assert r["triggered"] is False


# ═══════════════════════════════════════════
# 主力抢筹
# ═══════════════════════════════════════════

def test_whale_hunt_strong():
    ctx = _ctx(
        basic_info={"circ_mv": 300000},  # 30亿 (万元)
        l2_net_flow=20000000,  # 2000万元净流入
    )
    r = check_whale_hunt("000001.SZ", ctx)
    assert r["triggered"] is True
    assert r["confidence"] >= 0.7


def test_whale_hunt_none():
    ctx = _ctx(basic_info={"circ_mv": 300000}, l2_net_flow=0)
    r = check_whale_hunt("000001.SZ", ctx)
    assert r["triggered"] is False


# ═══════════════════════════════════════════
# 信号注册 & 批量检测
# ═══════════════════════════════════════════

def test_all_signals_registered():
    assert len(SIGNAL_REGISTRY) == 7
    assert len(SIGNAL_LABELS) == 7
    assert len(SIGNAL_PRIORITY) == 7


def test_check_all_signals():
    ctx = _ctx(
        hot_concept_tags=["PCB"],
        concept_limit_counts={"PCB": 5},
        basic_info={"circ_mv": 200000},
        limit_history_20d=2,
    )
    results = check_all_signals("000001.SZ", ctx)
    assert "concept_resonance" in results
    assert results["concept_resonance"]["triggered"] is True
    assert results["smallcap"]["triggered"] is True
    assert results["limit_dna"]["triggered"] is True


def test_triggered_signals_count():
    ctx = _ctx(
        hot_concept_tags=["PCB"],
        concept_limit_counts={"PCB": 5},
        basic_info={"circ_mv": 200000},
    )
    results = check_all_signals("000001.SZ", ctx)
    names = triggered_signals(results)
    assert len(names) >= 2  # concept_resonance + smallcap at minimum


def test_signals_summary():
    ctx = _ctx(
        hot_concept_tags=["PCB"],
        concept_limit_counts={"PCB": 5},
        basic_info={"circ_mv": 200000},
    )
    results = check_all_signals("000001.SZ", ctx)
    summary = signals_summary(results)
    assert "概念共振" in summary or "PCB" in summary


# ═══════════════════════════════════════════
# 信号组合判断
# ═══════════════════════════════════════════

def test_combo_A_matches():
    """概念共振 + 竞价抢筹 + 小盘弹性 → 规则A"""
    signals = {
        "concept_resonance": {"triggered": True, "confidence": 0.9, "detail": "PCB"},
        "auction_rush": {"triggered": True, "confidence": 0.7, "detail": "竞价抢筹"},
        "volume_breakout": {"triggered": False, "confidence": 0.0, "detail": ""},
        "limit_dna": {"triggered": False, "confidence": 0.0, "detail": ""},
        "smallcap": {"triggered": True, "confidence": 0.8, "detail": "小盘"},
        "morning_power": {"triggered": False, "confidence": 0.0, "detail": ""},
        "whale_hunt": {"triggered": False, "confidence": 0.0, "detail": ""},
    }
    should_push, combo, conf = signal_combination_judge(signals)
    assert should_push is True
    assert "A" in combo


def test_combo_no_match():
    """只有小盘弹性，无其他信号 → 不推送"""
    signals = {
        "concept_resonance": {"triggered": False, "confidence": 0.0, "detail": ""},
        "auction_rush": {"triggered": False, "confidence": 0.0, "detail": ""},
        "volume_breakout": {"triggered": False, "confidence": 0.0, "detail": ""},
        "limit_dna": {"triggered": False, "confidence": 0.0, "detail": ""},
        "smallcap": {"triggered": True, "confidence": 0.7, "detail": "小盘"},
        "morning_power": {"triggered": False, "confidence": 0.0, "detail": ""},
        "whale_hunt": {"triggered": False, "confidence": 0.0, "detail": ""},
    }
    should_push, combo, conf = signal_combination_judge(signals)
    assert should_push is False


def test_combo_multi_signal():
    """4+信号触发 → 多信号兜底"""
    signals = {}
    for name in ["concept_resonance", "auction_rush", "smallcap", "limit_dna"]:
        signals[name] = {"triggered": True, "confidence": 0.7, "detail": ""}
    for name in ["volume_breakout", "morning_power", "whale_hunt"]:
        signals[name] = {"triggered": False, "confidence": 0.0, "detail": ""}
    should_push, combo, conf = signal_combination_judge(signals)
    assert should_push is True  # rule A triggered (concept+auction+smallcap)
