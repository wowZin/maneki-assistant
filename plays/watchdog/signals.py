"""
盯盘信号计算（v3 — 全因子重构）
============================

复用 plays.limit_up.factors 中全部 16 个可用因子，对实时构造的面板行计算：
- 入场信号（模型验证 + 突破 / 放量拉升 / 涨停冲刺）
- 出场信号（止损 / 止盈 / 回撤 / 反转 / 时间）
- 异常状态（资金离场 / 抛压 / 背离）

实时数据源：scripts.jvquant_ws_client.JvQuantWSClient
资金流向：scripts.jvquant_client.JvQuantClient
"""

from __future__ import annotations

# ── 基础因子 ──
from plays.limit_up.factors.optimized.quality_combo import factor_quality_combo
from plays.limit_up.factors.optimized.quality_gate import factor_quality_gate
from plays.limit_up.factors.optimized.model_score import factor_model_score
# ── 情绪面 ──
from plays.limit_up.factors.sentiment.ensemble import factor_sentiment_ensemble
# ── 长短线 ──
from plays.limit_up.factors.shortterm.intraday import factor_intraday_strength
from plays.limit_up.factors.shortterm.turnover import factor_turnover_momentum
from plays.limit_up.factors.shortterm.trailing import factor_trailing_momentum
from plays.limit_up.factors.shortterm.growth import factor_growth_momentum
# ── 技术面 ──
from plays.limit_up.factors.technical.breakout import factor_breakout_quality
from plays.limit_up.factors.technical.volume import (
    factor_amount_acceleration,
    factor_amount_surge,
    factor_vol_expansion_quality,
)
from plays.limit_up.factors.technical.pullback import (
    factor_pullback_quality,
    factor_pullback_from_peak,
    factor_position_optimal,
)
from plays.limit_up.factors.technical.pattern import (
    factor_reversal_signal,
    factor_gap_up_quality,
    factor_consecutive_strength,
)
# ── 跨维度 ──
from plays.limit_up.factors.crossdim.divergence import factor_dimension_divergence
# ── 资金面/基本面 ──
from plays.limit_up.factors.fundflow.rebuilt import factor_fundflow_rebuilt
from plays.limit_up.factors.fundamental.rebuilt import factor_fundamental_rebuilt

from plays.watchdog.indicators import minute_momentum


# ── 入场信号配置 ──
ENTRY_CONFIG = {
    "breakout": {
        "min_gap_up": 0.0,
        "min_pct": 2.0,
        "min_vol_ratio": 1.3,
        "min_turnover": 5.0,
        "min_breakout_score": 10.0,
        "min_quality_combo": 0.0,
    },
    "surge": {
        "min_minute_chg": 2.0,
        "min_minute_vol_ratio": 1.5,
        "min_intraday_score": 10.0,
        "min_turnover": 3.0,
    },
    "sprint": {
        "min_pct": 7.0,
        "min_turnover": 12.0,
        "min_turnover_momentum": 12.0,
    },
    # 模型分全局门槛：所有入场模式必须 >= 此值才允许触发
    "min_model_score": 40.0,
}

# ── 出场信号配置 ──
EXIT_CONFIG = {
    "stop_loss_pct": 0.98,          # 固定止损
    "trailing_stop_pct": 0.97,      # 移动止损
    "take_profit_1": 0.05,          # 第一止盈 +5%
    "take_profit_2": 0.10,          # 第二止盈 +10%
    "time_stop_minutes": 30,        # 时间止损预警（分钟）
    "time_force_exit_minutes": 60,  # 强制出场（分钟）
    # 回调出场
    "pullback_entry_gain": 0.03,    # 入场后涨幅 >= 3% 才启用回调出场
    "min_pullback_exit": 0.05,      # 从最高回撤 5% → 出场
    "min_reversal_exit": 15.0,      # reversal_signal 阈值
}

# ── 异常状态配置 ──
ABNORMAL_CONFIG = {
    "critical_score": 70,
    "warning_score": 45,
}


def compute_factor_scores(row: dict) -> dict:
    """计算实时面板行的全部因子得分（16 个因子）。"""
    return {
        # 优化组合
        "model_score": factor_model_score(row),
        "quality_combo": factor_quality_combo(row),
        "quality_gate": factor_quality_gate(row),
        # 短线
        "intraday_strength": factor_intraday_strength(row),
        "turnover_momentum": factor_turnover_momentum(row),
        "trailing_momentum": factor_trailing_momentum(row),
        "growth_momentum": factor_growth_momentum(row),
        # 技术面
        "breakout_quality": factor_breakout_quality(row),
        "vol_expansion": factor_vol_expansion_quality(row),
        "amount_acceleration": factor_amount_acceleration(row),
        "amount_surge": factor_amount_surge(row),
        "pullback_quality": factor_pullback_quality(row),
        "pullback_from_peak": factor_pullback_from_peak(row),
        "position_optimal": factor_position_optimal(row),
        "reversal_signal": factor_reversal_signal(row),
        "gap_up_quality": factor_gap_up_quality(row),
        "consecutive_strength": factor_consecutive_strength(row),
        # 情绪 + 跨维度
        "sentiment_ensemble": factor_sentiment_ensemble(row),
        "dimension_divergence": factor_dimension_divergence(row),
        # 资金 + 基本面
        "fundflow_rebuilt": factor_fundflow_rebuilt(row),
        "fundamental_rebuilt": factor_fundamental_rebuilt(row),
    }


def check_entry(row: dict, scores: dict, klines: list[dict]) -> tuple[bool, str, str]:
    """检查是否触发入场信号。

    全局前置条件：model_score >= min_model_score
    返回: (is_triggered, signal_type, reason)
    """
    cfg = ENTRY_CONFIG

    # ── 全局模型分门槛 ──
    model_score = scores.get("model_score", 0.0)
    if model_score < cfg["min_model_score"]:
        return False, "", ""

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
        extra = []
        sent = scores.get("sentiment_ensemble", 0.0)
        if sent < 10:
            extra.append(f"情绪偏低({sent:.0f})")
        elif sent >= 20:
            extra.append(f"情绪支撑({sent:.0f})")
        div = scores.get("dimension_divergence", 0.0)
        if div >= 8:
            extra.append(f"背离({div:.0f})")
        reason = (
            f"突破: {pct:.1f}% 换手{turnover:.1f}% 量比{vol_ratio:.2f} "
            f"模型{model_score:.0f} breakout={scores.get('breakout_quality', 0):.0f}"
        )
        if extra:
            reason += " | " + " ".join(extra)
        return True, "breakout", reason

    # 模式 B：放量拉升
    sc = cfg["surge"]
    mom = minute_momentum(klines, n=5)
    if (
        mom["chg_pct"] >= sc["min_minute_chg"]
        and mom["vol_ratio"] >= sc["min_minute_vol_ratio"]
        and scores.get("intraday_strength", 0.0) >= sc["min_intraday_score"]
        and turnover >= sc["min_turnover"]
    ):
        extra = []
        sent = scores.get("sentiment_ensemble", 0.0)
        if sent >= 20:
            extra.append(f"情绪支撑({sent:.0f})")
        div = scores.get("dimension_divergence", 0.0)
        if div >= 8:
            extra.append(f"注意背离({div:.0f})")
        reason = (
            f"放量拉升: 5分钟{mom['chg_pct']:.1f}% 5分钟量比{mom['vol_ratio']:.2f} "
            f"模型{model_score:.0f} intraday={scores.get('intraday_strength', 0):.0f}"
        )
        if extra:
            reason += " | " + " ".join(extra)
        return True, "surge", reason

    # 模式 C：涨停冲刺（短线专用）
    sp = cfg["sprint"]
    if (
        pct >= sp["min_pct"]
        and turnover >= sp["min_turnover"]
        and scores.get("turnover_momentum", 0.0) >= sp["min_turnover_momentum"]
    ):
        extra = []
        tr = scores.get("trailing_momentum", 0.0)
        if tr >= 15:
            extra.append(f"动量强({tr:.0f})")
        gr = scores.get("growth_momentum", 0.0)
        if gr >= 10:
            extra.append(f"成长加速({gr:.0f})")
        div = scores.get("dimension_divergence", 0.0)
        if div >= 8:
            extra.append(f"注意背离({div:.0f})")
        reason = (
            f"涨停冲刺: {pct:.1f}% 换手{turnover:.1f}% "
            f"模型{model_score:.0f} turnover_momentum={scores.get('turnover_momentum', 0):.0f}"
        )
        if extra:
            reason += " | " + " ".join(extra)
        return True, "sprint", reason

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

    新增回调出场 + 反转出场。
    返回: (is_triggered, reason)
    """
    cfg = config or EXIT_CONFIG

    if entry_price <= 0 or current_price <= 0:
        return False, ""

    gain_pct = (current_price / entry_price - 1)

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
    if gain_pct >= cfg["take_profit_2"]:
        return True, f"止盈二档(+{gain_pct*100:.1f}%): 建议全平"
    if gain_pct >= cfg["take_profit_1"]:
        return True, f"止盈一档(+{gain_pct*100:.1f}%): 建议平50%"

    # 时间止损
    if bars_held >= cfg["time_force_exit_minutes"]:
        return True, f"时间止损: 持仓{bars_held}分钟未达目标"

    # ── 新增：回调出场（入场后已有盈利，从高点回落）──
    if gain_pct >= cfg["pullback_entry_gain"] and highest_since_entry > entry_price:
        pullback_pct = (highest_since_entry - current_price) / highest_since_entry
        if pullback_pct >= cfg["min_pullback_exit"]:
            # 结合 pullback_quality / pullback_from_peak / reversal_signal 判断
            pb_quality = scores.get("pullback_quality", 0.0)
            pb_peak = scores.get("pullback_from_peak", 0.0)
            reversal = scores.get("reversal_signal", 0.0)
            combined = pb_quality + pb_peak + reversal

            # 只有因子信号也确认回调时才出场，避免假摔
            if reversal >= cfg["min_reversal_exit"]:
                return True, (
                    f"反转回调: 回撤{pullback_pct*100:.1f}% "
                    f"最高{highest_since_entry:.2f} 现价{current_price:.2f} "
                    f"reversal={reversal:.0f}"
                )
            if combined >= 15 and pullback_pct >= cfg["min_pullback_exit"]:
                return True, (
                    f"回调出场: 回撤{pullback_pct*100:.1f}% "
                    f"最高{highest_since_entry:.2f} 现价{current_price:.2f} "
                    f"pb={pb_quality:.0f}+{pb_peak:.0f} reversal={reversal:.0f}"
                )
            # 大幅回撤无条件出场
            if pullback_pct >= 0.10:
                return True, (
                    f"深度回撤: 回撤{pullback_pct*100:.1f}% "
                    f"最高{highest_since_entry:.2f} 现价{current_price:.2f}"
                )

    # 反转出场：盘中强度转负 + 跌破 VWAP
    intraday = scores.get("intraday_strength", 0.0)
    if intraday < 0 and current_price < vwap:
        return True, f"日内反转: 强度转负且跌破VWAP({vwap:.2f})"

    # 高位最优位置出场
    pos_opt = scores.get("position_optimal", 0.0)
    if pos_opt <= -10 and gain_pct >= 0.02:
        return True, (
            f"高位出场: position_optimal={pos_opt:.0f} "
            f"入场{entry_price:.2f}→现价{current_price:.2f} (+{gain_pct*100:.1f}%)"
        )

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


def _check_bear_trap(
    row: dict,
    scores: dict,
    netflow: float,
    prev_last: float,
    prev_vwap: float,
    prev_vol_ratio: float,
) -> tuple[bool, str]:
    """检测是否为诱空。

    多扫描对比 + 单扫描特征综合判断。
    返回: (is_bear_trap, reason)
    """
    current = row.get("last_price", 0.0)
    vwap = row.get("vwap", 0.0)
    vol_ratio = row.get("vol_ratio_proxy", 1.0)
    pct = row.get("pct_chg_score_day", 0.0)
    position = row.get("position_20d", 0.5)
    pullback = row.get("pullback_10d", 0.1)
    netflow_out = netflow < -5_000_000

    reasons = []

    # ── 双扫描确认：上一轮跌破VWAP，本轮拉回 ──
    # 这是最强的诱空信号，不需要位置限制
    if prev_last > 0 and prev_vwap > 0:
        prev_vs_vwap = (prev_last / prev_vwap - 1) * 100
        curr_vs_vwap = (current / vwap - 1) * 100 if vwap > 0 else 0
        # 上一轮跌破VWAP(>1%)，本轮有显著回拉
        if prev_vs_vwap <= -1.0 and (curr_vs_vwap - prev_vs_vwap) >= 0.8:
            reasons.append(f"快速回拉VWAP({prev_vs_vwap:.1f}%→{curr_vs_vwap:.1f}%)")
            if prev_vol_ratio > 0 and vol_ratio < prev_vol_ratio * 0.8:
                reasons.append(f"缩量({prev_vol_ratio:.1f}→{vol_ratio:.1f})")
            return True, "诱空: " + " | ".join(reasons[:3])

    # ── 单扫描特征 ──
    # 核心条件：从近期高点有明显回撤 + 跌幅不极端 + 量不大

    # 先算 VWAP 价差：诱空不会远离VWAP，真破位会
    vs_vwap = (current / vwap - 1) * 100 if vwap > 0 else 0

    # 如果远离VWAP超过3%且没有回升迹象 → 真破位，不是诱空
    if vs_vwap <= -3.0:
        return False, ""

    # 条件A：中低位大幅回踩缩量
    if pct < -3.0 and position < 0.65 and pullback >= 0.03:
        if vs_vwap > -3.0:  # 远离VWAP不超3%
            if vol_ratio < 1.5:  # 没有异常放量
                reasons.append(f"回踩缩量(pb={pullback:.0%} pos={position:.2f})")
                if not netflow_out:
                    reasons.append("资金无大量流出")
                return True, "诱空: " + " | ".join(reasons[:3])

    # 条件B：大幅急跌但缩量（跌停没封死，VWAP没远离）
    if pct < -7.0 and vol_ratio < 1.0 and vs_vwap > -2.5:
        reasons.append(f"急跌缩量(chg={pct:.0f}% vol={vol_ratio:.1f} vsVWAP={vs_vwap:.1f}%)")
        return True, "诱空(分歧): " + " | ".join(reasons[:3])

    return False, ""


def check_abnormal(
    row: dict,
    scores: dict,
    netflow: float,
    ask_bid_ratio: float,
    entry_price: float,
    netflow_history: list[float] | None = None,
    prev_last: float = 0.0,
    prev_vwap: float = 0.0,
    prev_vol_ratio: float = 0.0,
    config: dict | None = None,
) -> tuple[bool, str, str]:
    """多因子置信度检测异常状态（资金离场/抛压/诱空）。

    返回: (is_abnormal, level, reason)
    level: "" | "bear_trap" | "warning" | "critical"
    """
    cfg = config or ABNORMAL_CONFIG
    details: list[str] = []
    total = 0

    # 1. 资金流（手动打分 + 因子分）
    nf_score = _score_netflow(netflow)
    if nf_score > 0:
        total += nf_score
        details.append(f"大单净流出{netflow/10000:.0f}万(-{nf_score})")

    # 因子资金分 ≤ 10 且净流出来 → 强化信号
    fundflow = scores.get("fundflow_rebuilt", 50.0)
    if nf_score > 0 and fundflow <= 10:
        total += 10
        details.append(f"资金因子确认({fundflow:.0f})")

    # 2. 价格行为
    pa_score, pa_reasons = _score_price_action(row)
    total += pa_score
    details.extend(pa_reasons)

    # 3. 位置（复用因子）
    pos_opt = scores.get("position_optimal", 0.0)
    if pos_opt <= -10:
        total += 12
        details.append(f"高位风险({pos_opt:.0f})")
    elif pos_opt <= -5:
        total += 6
        details.append(f"偏高位置({pos_opt:.0f})")

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

    # 7. 维度背离 → 异常加分
    div = scores.get("dimension_divergence", 0.0)
    if div >= 8:
        total += 8
        details.append(f"维度背离({div:.0f})")

    # 8. 诱空检测：低位回拉 + 缩量 → 覆盖 warning，但不覆盖 critical
    bear_trap, bt_reason = _check_bear_trap(row, scores, netflow,
                                              prev_last, prev_vwap, prev_vol_ratio)
    if bear_trap and total < cfg["critical_score"]:
        return True, "bear_trap", bt_reason

    if total >= cfg["critical_score"]:
        return True, "critical", f"异常置信度{total}: {'; '.join(details[:5])}"
    if total >= cfg["warning_score"]:
        return True, "warning", f"异常置信度{total}: {'; '.join(details[:5])}"

    return False, "", ""


def is_worth_watching(row: dict, scores: dict) -> tuple[bool, str]:
    """判断一只股票是否值得进入盯盘候选池。

    增强：加入 model_score 和 fundamental_rebuilt 作为辅助筛选。
    """
    qc = scores.get("quality_combo", 0.0)
    tm = scores.get("turnover_momentum", 0.0)
    is_ = scores.get("intraday_strength", 0.0)
    ms = scores.get("model_score", 0.0)

    if qc >= 85:
        return True, f"quality_combo={qc:.0f} model_score={ms:.0f}"
    if tm >= 12 and is_ >= 10:
        return True, f"turnover_momentum={tm:.0f} intraday_strength={is_:.0f} model_score={ms:.0f}"

    return False, f"quality_combo={qc:.0f} model_score={ms:.0f} 活跃度不足"
