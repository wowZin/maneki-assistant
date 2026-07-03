"""
盯盘信号计算
============

复用 plays.limit_up.factors 中的因子函数，对实时构造的面板行计算：
- 入场信号（突破 / 放量拉升 / 涨停冲刺）
- 出场信号（止损 / 止盈 / 时间 / 反转）
- 异常状态（资金离场 / 抛压）

实时数据源：scripts.jvquant_ws_client.JvQuantWSClient
资金流向：scripts.jvquant_client.JvQuantClient
"""

from __future__ import annotations

from plays.limit_up.factors.optimized.quality_combo import factor_quality_combo
from plays.limit_up.factors.optimized.quality_gate import factor_quality_gate
from plays.limit_up.factors.shortterm.intraday import factor_intraday_strength
from plays.limit_up.factors.shortterm.turnover import factor_turnover_momentum
from plays.limit_up.factors.technical.breakout import factor_breakout_quality
from plays.limit_up.factors.technical.volume import (
    factor_amount_acceleration,
    factor_amount_surge,
    factor_vol_expansion_quality,
)
from plays.watchdog.indicators import minute_momentum


# ── 入场信号配置 ──
ENTRY_CONFIG = {
    "breakout": {
        "min_gap_up": 0.0,        # 开盘缺口下限（%）
        "min_pct": 2.0,           # 当前涨幅下限（%）
        "min_vol_ratio": 1.3,     # 量比下限
        "min_turnover": 5.0,      # 换手率下限（%）
        "min_breakout_score": 10.0,
        "min_quality_combo": 0.0,
    },
    "surge": {
        "min_minute_chg": 2.0,    # 近N分钟涨幅下限（%）
        "min_minute_vol_ratio": 1.5,
        "min_intraday_score": 10.0,
        "min_turnover": 3.0,
    },
    "sprint": {
        "min_pct": 7.0,
        "min_turnover": 12.0,
        "min_turnover_momentum": 12.0,
        "min_technical": 30.0,
        "min_shortterm": 25.0,
    },
}


# ── 出场信号配置 ──
EXIT_CONFIG = {
    "stop_loss_pct": 0.98,        # 固定止损：入场价 × 0.98
    "trailing_stop_pct": 0.97,    # 移动止损：最高价 × 0.97
    "take_profit_1": 0.05,        # 第一止盈位 +5%
    "take_profit_2": 0.10,        # 第二止盈位 +10%
    "time_stop_minutes": 30,      # 时间止损预警（分钟）
    "time_force_exit_minutes": 60,  # 时间止损强制出场（分钟）
}


# ── 异常状态配置 ──
ABNORMAL_CONFIG = {
    "critical_score": 70,         # critical 阈值
    "warning_score": 45,          # warning 阈值
}


def compute_factor_scores(row: dict) -> dict:
    """计算实时面板行的各因子得分。"""
    return {
        "quality_combo": factor_quality_combo(row),
        "quality_gate": factor_quality_gate(row),
        "intraday_strength": factor_intraday_strength(row),
        "vol_expansion": factor_vol_expansion_quality(row),
        "turnover_momentum": factor_turnover_momentum(row),
        "breakout_quality": factor_breakout_quality(row),
        "amount_acceleration": factor_amount_acceleration(row),
        "amount_surge": factor_amount_surge(row),
    }


def check_entry(row: dict, scores: dict, klines: list[dict]) -> tuple[bool, str, str]:
    """检查是否触发入场信号。

    返回: (is_triggered, signal_type, reason)
    """
    cfg = ENTRY_CONFIG
    pct = row.get("pct_chg_score_day", 0.0)
    turnover = row.get("turnover_rate", 0.0)
    vol_ratio = row.get("vol_ratio_proxy", 1.0)
    quality_combo = scores.get("quality_combo", 0.0)

    # 模式 A：突破
    bc = cfg["breakout"]
    gap = row.get("gap_up", 0.0)
    if (
        gap >= bc["min_gap_up"]
        and pct >= bc["min_pct"]
        and vol_ratio >= bc["min_vol_ratio"]
        and turnover >= bc["min_turnover"]
        and scores.get("breakout_quality", 0.0) >= bc["min_breakout_score"]
        and quality_combo >= bc["min_quality_combo"]
    ):
        return True, "breakout", (
            f"突破: 涨幅{pct:.1f}% 换手{turnover:.1f}% 量比{vol_ratio:.2f} "
            f"breakout={scores.get('breakout_quality', 0):.0f}"
        )

    # 模式 B：放量拉升
    sc = cfg["surge"]
    mom = minute_momentum(klines, n=5)
    if (
        mom["chg_pct"] >= sc["min_minute_chg"]
        and mom["vol_ratio"] >= sc["min_minute_vol_ratio"]
        and scores.get("intraday_strength", 0.0) >= sc["min_intraday_score"]
        and turnover >= sc["min_turnover"]
    ):
        return True, "surge", (
            f"放量拉升: 5分钟{mom['chg_pct']:.1f}% 5分钟量比{mom['vol_ratio']:.2f} "
            f"intraday={scores.get('intraday_strength', 0):.0f}"
        )

    # 模式 C：涨停冲刺
    sp = cfg["sprint"]
    if (
        pct >= sp["min_pct"]
        and turnover >= sp["min_turnover"]
        and scores.get("turnover_momentum", 0.0) >= sp["min_turnover_momentum"]
        and row.get("technical", 0.0) >= sp["min_technical"]
        and row.get("shortterm", 0.0) >= sp["min_shortterm"]
    ):
        return True, "sprint", (
            f"涨停冲刺: 涨幅{pct:.1f}% 换手{turnover:.1f}% "
            f"turnover_momentum={scores.get('turnover_momentum', 0):.0f}"
        )

    return False, "", ""


def check_exit(
    entry_price: float,
    highest_since_entry: float,
    current_price: float,
    bars_held: int,
    vwap: float,
    scores: dict,
    config: dict | None = None,
) -> tuple[bool, str]:
    """检查是否触发出场信号。

    返回: (is_triggered, reason)
    """
    cfg = config or EXIT_CONFIG

    if entry_price <= 0 or current_price <= 0:
        return False, ""

    # 固定止损
    if current_price <= entry_price * cfg["stop_loss_pct"]:
        return True, f"固定止损: 入场{entry_price:.2f} 现价{current_price:.2f}"

    # 移动止损
    if highest_since_entry > entry_price:
        stop = highest_since_entry * cfg["trailing_stop_pct"]
        if current_price <= stop:
            return True, (
                f"移动止损: 最高{highest_since_entry:.2f} 止损{stop:.2f} "
                f"现价{current_price:.2f}"
            )

    # 分批止盈
    gain_pct = (current_price / entry_price - 1)
    if gain_pct >= cfg["take_profit_2"]:
        return True, f"止盈二档(+{gain_pct*100:.1f}%): 建议全平"
    if gain_pct >= cfg["take_profit_1"]:
        # 第一档只提醒不平仓，由上层判断是否已提醒过
        return True, f"止盈一档(+{gain_pct*100:.1f}%): 建议平50%"

    # 时间止损
    if bars_held >= cfg["time_force_exit_minutes"]:
        return True, f"时间止损: 持仓{bars_held}分钟未达目标"

    # 反转出场：盘中强度转负且跌破 VWAP
    intraday = scores.get("intraday_strength", 0.0)
    if intraday < 0 and current_price < vwap:
        return True, f"日内反转: 强度转负且跌破VWAP({vwap:.2f})"

    return False, ""


# ── 异常状态多因子置信度 ──

def _score_netflow(netflow: float) -> int:
    """资金流得分：净流出越大分越高。"""
    if netflow < -100_000_000:
        return 30
    if netflow < -50_000_000:
        return 20
    if netflow < -20_000_000:
        return 12
    if netflow < -5_000_000:
        return 6
    return 0


def _score_price_action(row: dict) -> tuple[int, list[str]]:
    """价格行为得分：跌破 VWAP、放量、急跌。"""
    score = 0
    reasons = []
    current_price = row.get("last_price", 0.0)
    vwap = row.get("vwap", 0.0)
    vol_ratio = row.get("vol_ratio_proxy", 1.0)
    pct = row.get("pct_chg_score_day", 0.0)

    if vwap > 0 and current_price > 0:
        vs_vwap = (current_price / vwap - 1) * 100
        if vs_vwap <= -2.0:
            score += 15
            reasons.append(f"跌破VWAP{vs_vwap:.1f}%")
        elif vs_vwap <= -1.0:
            score += 8
            reasons.append(f"偏弱VWAP{vs_vwap:.1f}%")

    if vol_ratio >= 1.5:
        score += 10
        reasons.append(f"量比{vol_ratio:.1f}")

    if pct <= -5.0:
        score += 15
        reasons.append(f"大跌{pct:.1f}%")
    elif pct <= -3.0:
        score += 8
        reasons.append(f"下跌{pct:.1f}%")

    return score, reasons


def _score_position(row: dict) -> tuple[int, list[str]]:
    """位置得分：高位更容易是真出货，低位更可能是诱空。"""
    pos = row.get("position_20d", 0.5)
    limit20 = row.get("limit_up_count_20d", 0.0)
    pct = row.get("pct_chg_score_day", 0.0)
    score = 0
    reasons = []

    if pos >= 0.85:
        score += 15
        reasons.append(f"高位pos{pos:.2f}")
    elif pos >= 0.70:
        score += 8
        reasons.append(f"中高位pos{pos:.2f}")

    if limit20 >= 2 and pct < -3.0:
        score += 12
        reasons.append(f"涨停基因{limit20:.0f}次后大跌")

    if pos <= 0.30:
        score -= 20
        reasons.append("低位保护")

    return score, reasons


def _score_ask_bid(ask_bid_ratio: float) -> tuple[int, str]:
    if ask_bid_ratio >= 4.0:
        return 15, f"抛压{ask_bid_ratio:.1f}:1"
    if ask_bid_ratio >= 3.0:
        return 10, f"抛压{ask_bid_ratio:.1f}:1"
    if ask_bid_ratio >= 2.0:
        return 6, f"抛压{ask_bid_ratio:.1f}:1"
    return 0, ""


def _score_consecutive_outflow(netflow_history: list[float]) -> tuple[int, str]:
    """连续净流出加分：最近 3 轮有 2 轮净流出。"""
    if len(netflow_history) < 3:
        return 0, ""
    recent = netflow_history[-3:]
    outflow_count = sum(1 for n in recent if n < 0)
    if outflow_count >= 3:
        return 15, "连续3轮净流出"
    if outflow_count >= 2:
        return 8, "近3轮2次净流出"
    return 0, ""


def _score_holding_drop(row: dict, entry_price: float) -> tuple[int, str]:
    """持仓中的急跌放量。"""
    if entry_price <= 0:
        return 0, ""
    current_price = row.get("last_price", 0.0)
    vol_ratio = row.get("vol_ratio_proxy", 1.0)
    drop_pct = (current_price / entry_price - 1) * 100
    if drop_pct <= -5.0 and vol_ratio >= 1.5:
        return 25, f"持仓急跌{drop_pct:.1f}%放量"
    if drop_pct <= -3.0 and vol_ratio >= 1.5:
        return 15, f"持仓下跌{drop_pct:.1f}%放量"
    return 0, ""


def check_abnormal(
    row: dict,
    scores: dict,
    netflow: float,
    ask_bid_ratio: float,
    entry_price: float,
    netflow_history: list[float] | None = None,
    config: dict | None = None,
) -> tuple[bool, str, str]:
    """多因子置信度检测异常状态（资金离场/抛压）。

    返回: (is_abnormal, level, reason)
    level: "" | "warning" | "critical"
    """
    cfg = config or ABNORMAL_CONFIG
    details: list[str] = []
    total = 0

    # 1. 资金流
    nf_score = _score_netflow(netflow)
    if nf_score > 0:
        total += nf_score
        details.append(f"大单净流出{netflow/10000:.0f}万(-{nf_score})")

    # 2. 价格行为
    pa_score, pa_reasons = _score_price_action(row)
    total += pa_score
    details.extend(pa_reasons)

    # 3. 位置
    pos_score, pos_reasons = _score_position(row)
    total += pos_score
    details.extend(pos_reasons)

    # 4. 盘口压力
    ab_score, ab_reason = _score_ask_bid(ask_bid_ratio)
    if ab_score > 0:
        total += ab_score
        details.append(ab_reason)

    # 5. 连续净流出
    co_score, co_reason = _score_consecutive_outflow(netflow_history or [])
    if co_score > 0:
        total += co_score
        details.append(co_reason)

    # 6. 持仓急跌
    hd_score, hd_reason = _score_holding_drop(row, entry_price)
    if hd_score > 0:
        total += hd_score
        details.append(hd_reason)

    if total >= cfg["critical_score"]:
        return True, "critical", f"异常置信度{total}: {'; '.join(details[:5])}"
    if total >= cfg["warning_score"]:
        return True, "warning", f"异常置信度{total}: {'; '.join(details[:5])}"

    return False, "", ""


def is_worth_watching(row: dict, scores: dict) -> tuple[bool, str]:
    """判断一只股票是否值得进入盯盘候选池。

    默认：quality_combo >= 85 或 turnover_momentum >= 12 且 intraday_strength >= 10
    """
    qc = scores.get("quality_combo", 0.0)
    tm = scores.get("turnover_momentum", 0.0)
    is_ = scores.get("intraday_strength", 0.0)

    if qc >= 85:
        return True, f"quality_combo={qc:.0f}"
    if tm >= 12 and is_ >= 10:
        return True, f"turnover_momentum={tm:.0f}, intraday_strength={is_:.0f}"

    return False, f"quality_combo={qc:.0f}, 活跃度不足"
