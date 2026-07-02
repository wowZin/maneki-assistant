"""候选因子原型库 v2。

基于 enriched panel 的衍生特征，构建具有预测力的因子。
所有因子函数接收一行 panel Series，返回 float（score 或 adjustment）。

设计原则：
- 目标标签是 hit_limit_3（未来3日是否涨停）和 fwd_ret_3（未来3日收益）
- 奖励的是"即将上涨"，不是"已经涨过"
- 每个因子的 chasing_score（ic_trailing_10 - ic_fwd_ret_3）越小越好
"""

from __future__ import annotations

import pandas as pd


# ===== 辅助函数 =====

def _safe(v, default=0.0):
    """安全取值，None/NaN 返回 default。"""
    if v is None:
        return default
    try:
        if pd.isna(v):
            return default
        return float(v)
    except (ValueError, TypeError):
        return default


def trailing_penalty(trailing_10, thr_high=0.30, thr_mid=0.20,
                     penalty_high=-15.0, penalty_mid=-10.0):
    """基于 trailing_10 的阶梯惩罚。"""
    t = _safe(trailing_10)
    if t > thr_high:
        return penalty_high
    if t > thr_mid:
        return penalty_mid
    return 0.0


# ═══════════════════════════════════════════════════════════
# 第一类：涨停基因因子（核心预测因子）
# ═══════════════════════════════════════════════════════════

def factor_limit_up_gene_20d(row: pd.Series) -> float:
    """涨停基因-20日：近20日涨停次数越多，再次涨停概率越高。

    预期 IC: 正（hit_limit_3）
    """
    cnt = _safe(row.get("limit_up_count_20d"))
    if cnt >= 5:
        return 25.0
    if cnt >= 3:
        return 18.0
    if cnt >= 2:
        return 12.0
    if cnt >= 1:
        return 6.0
    return 0.0


def factor_limit_up_gene_60d(row: pd.Series) -> float:
    """涨停基因-60日：更长周期涨停频次。

    预期 IC: 正（hit_limit_3），但比20日更平滑。
    """
    cnt = _safe(row.get("limit_up_count_60d"))
    if cnt >= 8:
        return 25.0
    if cnt >= 5:
        return 18.0
    if cnt >= 3:
        return 12.0
    if cnt >= 1:
        return 6.0
    return 0.0


def factor_limit_up_gene_composite(row: pd.Series) -> float:
    """涨停基因复合：短周期+长周期加权。

    limit_up_count_20d 权重大于 60d，反映近期活跃度。
    """
    cnt20 = _safe(row.get("limit_up_count_20d"))
    cnt60 = _safe(row.get("limit_up_count_60d"))
    # 20日每次+3分，60日每次+1分（去重20日的）
    recent = min(cnt20, 6) * 3.0
    older = min(max(cnt60 - cnt20, 0), 8) * 1.5
    return min(recent + older, 25.0)


# ═══════════════════════════════════════════════════════════
# 第二类：回调质量因子（反追高）
# ═══════════════════════════════════════════════════════════

def factor_pullback_quality(row: pd.Series) -> float:
    """回调质量：适度回调+缩量=健康洗盘。

    - pullback_10d 在 5-15%：适度回调
    - 量比 < 0.8：缩量
    - 两者同时满足 → 洗盘结束即将反弹

    预期 IC: 正（fwd_ret_3），chasing_score 为负（反追高）。
    """
    pb = _safe(row.get("pullback_10d"), 1.0)
    vol_ratio = _safe(row.get("vol_ratio_proxy"), 1.0)

    if 0.05 <= pb <= 0.15 and vol_ratio < 0.8:
        return 18.0
    if 0.03 <= pb <= 0.20 and vol_ratio < 1.0:
        return 10.0
    if pb > 0.20:
        # 深度回调 → 弱势
        return -5.0
    return 0.0


def factor_position_optimal(row: pd.Series) -> float:
    """最优位置：处于20日区间的30-70%分位。

    - 太低（<20%）：破位风险
    - 太高（>80%）：追高风险
    - 30-70%：有上涨空间且不追高

    预期 IC: 正，chasing_score 为负。
    """
    pos = _safe(row.get("position_20d"), 0.5)
    if 0.30 <= pos <= 0.70:
        return 10.0
    if pos > 0.85:
        return -10.0  # 高位惩罚
    if pos < 0.15:
        return -5.0   # 低位风险
    return 0.0


def factor_pullback_from_peak(row: pd.Series) -> float:
    """峰位回调幅度：从20日高点回调 3-8% 是理想买点。

    注意：此因子天然反追高。
    """
    pb20 = _safe(row.get("pullback_20d"), 0.0)
    if 0.03 <= pb20 <= 0.08:
        return 15.0
    if 0.08 < pb20 <= 0.15:
        return 8.0
    if pb20 < 0.02:
        return -5.0  # 几乎在新高，追高风险
    return 0.0


# ═══════════════════════════════════════════════════════════
# 第三类：量能结构因子
# ═══════════════════════════════════════════════════════════

def factor_vol_expansion_quality(row: pd.Series) -> float:
    """放量质量：放量+温和涨幅=主力吸筹，放量+暴涨=出货。

    预期 IC: 正（fwd_ret_3），chasing_score 适中。
    """
    vol_r = _safe(row.get("vol_ratio_proxy"), 1.0)
    pct = _safe(row.get("pct_chg_score_day"))
    pb = _safe(row.get("pullback_10d"), 0.0)

    # 回调后放量温和上涨 → 最佳
    if vol_r > 1.5 and 2.0 <= pct <= 7.0 and pb > 0.03:
        return 18.0
    # 放量上涨但位置偏高 → 次优
    if vol_r > 1.3 and 2.0 <= pct <= 5.0:
        return 10.0
    # 放量滞涨 → 警告
    if vol_r > 1.5 and pct < 0:
        return -10.0
    return 0.0


def factor_amount_acceleration(row: pd.Series) -> float:
    """资金加速度：成交额连续3日递增+股价上涨。

    反映资金持续流入。
    """
    inc = _safe(row.get("amount_3d_increasing"))
    pct = _safe(row.get("pct_chg_score_day"))
    vol_r = _safe(row.get("vol_ratio_proxy"), 1.0)

    if inc and pct > 0 and vol_r > 1.2:
        return 15.0
    if inc and pct > 0:
        return 8.0
    return 0.0


def factor_amount_surge(row: pd.Series) -> float:
    """成交额突增：当日成交额/5日均值 > 2。

    巨量通常是变盘信号，配合涨幅方向使用。
    """
    ratio = _safe(row.get("amount_ratio"), 1.0)
    pct = _safe(row.get("pct_chg_score_day"))
    pb = _safe(row.get("pullback_10d"), 0.0)

    if ratio > 2.0 and pct > 3.0 and pb > 0.03:
        return 12.0  # 回调后巨量拉升
    if ratio > 2.5 and pct > 5.0:
        return 5.0   # 高位巨量需谨慎
    if ratio < 0.4:
        return -5.0  # 极度缩量
    return 0.0


# ═══════════════════════════════════════════════════════════
# 第四类：形态/反转因子
# ═══════════════════════════════════════════════════════════

def factor_reversal_signal(row: pd.Series) -> float:
    """弱转强信号：昨日跌>1%+今日涨>2%+放量>1.2倍。

    预期 IC: 正（短期反转往往延续）。
    """
    rev = _safe(row.get("reversal_signal"))
    if rev:
        return 20.0
    return 0.0


def factor_gap_up_quality(row: pd.Series) -> float:
    """跳空高开质量：高开2%+ + 量比>1.5 + 非极端高位。

    预期 IC: 正（hit_limit_3），跳空高开是强势信号。
    """
    gap = _safe(row.get("gap_up_pit", row.get("gap_up")))
    vol_r = _safe(row.get("vol_ratio_proxy"), 1.0)
    pos = _safe(row.get("position_20d"), 0.5)
    pb = _safe(row.get("pullback_10d"), 0.0)

    if gap > 3.0 and vol_r > 1.5 and pos < 0.75:
        return 18.0  # 中低位跳空高开
    if gap > 2.0 and vol_r > 1.3 and pb > 0.03:
        return 12.0  # 回调后跳空
    if gap > 5.0 and pos > 0.85:
        return -5.0  # 高位跳空追高风险
    return 0.0


def factor_consecutive_strength(row: pd.Series) -> float:
    """连阳强度：连续阳线天数+涨幅。

    连阳但不过热 → 趋势健康。
    """
    cons_up = _safe(row.get("consecutive_up"))
    pct_5d = _safe(row.get("avg_pct_chg_5d"))

    if cons_up >= 3 and 0.5 <= pct_5d <= 3.0:
        return 10.0  # 温和连阳
    if cons_up >= 5 and pct_5d > 3.0:
        return -5.0  # 过热
    return 0.0


# ═══════════════════════════════════════════════════════════
# 第五类：波动率因子
# ═══════════════════════════════════════════════════════════

def factor_volatility_contraction(row: pd.Series) -> float:
    """波动收敛：近5日波动 < 近10日波动 → 蓄势待发。

    预期 IC: 正（低波动后往往出方向）。
    """
    std5 = _safe(row.get("pct_chg_std_5d"), 99.0)
    std10 = _safe(row.get("pct_chg_std_10d"), 0.0)

    if std10 > 0 and std5 / std10 < 0.6:
        return 8.0  # 波动显著收敛
    if std10 > 0 and std5 / std10 < 0.8:
        return 4.0
    return 0.0


def factor_low_amplitude_breakout(row: pd.Series) -> float:
    """窄幅突破：近5日振幅<5% + 今日放量涨。

    窄幅整理后放量突破是经典启动信号。
    """
    amp = _safe(row.get("amplitude"), 0.0)
    vol_r = _safe(row.get("vol_ratio_proxy"), 1.0)
    pct = _safe(row.get("pct_chg_score_day"))

    if amp < 5.0 and vol_r > 1.5 and pct > 2.0:
        return 15.0
    if amp < 3.0 and vol_r > 1.2:
        return 10.0
    return 0.0


# ═══════════════════════════════════════════════════════════
# 第六类：上影线/形态因子
# ═══════════════════════════════════════════════════════════

def factor_upper_shadow_risk(row: pd.Series) -> float:
    """上影线风险：长上影线+暴量 → 出货信号。

    预期 IC: 负（对 hit_limit_3），用作惩罚。
    """
    shadow = _safe(row.get("upper_shadow_pct"), 0.0)
    vol_r = _safe(row.get("vol_ratio_proxy"), 1.0)
    pct = _safe(row.get("pct_chg_score_day"))

    if shadow > 60 and vol_r > 1.5 and pct < 5.0:
        return -15.0  # 暴量长上影=出货
    if shadow > 50 and vol_r > 1.3:
        return -8.0
    return 0.0


def factor_large_amplitude_risk(row: pd.Series) -> float:
    """大振幅风险：振幅>12% + 收盘涨幅<5% → 分歧巨大。

    高振幅低收盘意味着多空分歧严重，次日容易低开。
    """
    amp = _safe(row.get("amplitude"), 0.0)
    pct = _safe(row.get("pct_chg_score_day"))

    if amp > 12.0 and pct < 5.0:
        return -12.0
    if amp > 10.0 and pct < 3.0:
        return -8.0
    return 0.0


# ═══════════════════════════════════════════════════════════
# 第七类：跨维度交互因子
# ═══════════════════════════════════════════════════════════

def factor_dimension_divergence(row: pd.Series) -> float:
    """维度背离：shortterm高分 + technical中性/低分。

    shortterm强说明短线资金认可，technical中性说明不在追高位。
    这种组合比两者都高更有价值。
    """
    st = _safe(row.get("shortterm"))
    tech = _safe(row.get("technical"))

    if st >= 60 and tech <= 40:
        return 12.0  # 短线强+技术位不高=理想
    if st >= 50 and tech <= 35:
        return 8.0
    return 0.0


def factor_sentiment_contrarian(row: pd.Series) -> float:
    """情绪逆向：sentiment低+其他维度高。

    sentiment IC 为负意味着高 sentiment 反而不利于未来收益。
    低 sentiment 但有其他维度支撑 → 被忽视的好票。
    """
    sent = _safe(row.get("sentiment"))
    st = _safe(row.get("shortterm"))
    tech = _safe(row.get("technical"))

    if sent <= 30 and (st >= 50 or tech >= 50):
        return 10.0
    if sent <= 20 and st >= 40:
        return 8.0
    return 0.0


def factor_total_quality_bonus(row: pd.Series) -> float:
    """综合质量加分：多维度共振确认。

    四维度同时>=50 → 强确认信号。
    """
    dims = ["fundamental", "technical", "fundflow", "sentiment", "shortterm"]
    high_dims = sum(1 for d in dims if _safe(row.get(d)) >= 50)

    if high_dims >= 4:
        return 12.0
    if high_dims >= 3:
        return 6.0
    return 0.0


# ═══════════════════════════════════════════════════════════
# 第十一类：收益率优化因子（基于基本面+资金流新数据）
# ═══════════════════════════════════════════════════════════

def factor_circ_mv_tier(row: pd.Series) -> float:
    """流通市值分层因子。

    大市值股票在涨停候选池中有更高的收益率和命中率。
    circ_mv (万元) → 分层加分。

    IC fwd_ret_3: +0.12, IC hit_limit_3: +0.09
    """
    circ_mv = _safe(row.get("circ_mv"), 0.0)  # 万元
    if circ_mv <= 0:
        # fallback: 从 total_mv 估算
        circ_mv = _safe(row.get("total_mv"), 0.0) * 0.7

    if circ_mv >= 500_0000:  # 500亿+
        return 20.0
    if circ_mv >= 200_0000:  # 200亿+
        return 16.0
    if circ_mv >= 100_0000:  # 100亿+
        return 12.0
    if circ_mv >= 50_0000:   # 50亿+
        return 8.0
    if circ_mv >= 20_0000:   # 20亿+
        return 4.0
    if circ_mv >= 10_0000:   # 10亿+
        return 0.0
    return -5.0  # 极小盘风险


def factor_fundamental_quality(row: pd.Series) -> float:
    """基本面质量复合因子。

    组合 PE（正向：低PE更好）、PB（低PB更好）、盈利收益率。
    预期 IC fwd_ret_3: 正
    """
    pe = _safe(row.get("pe"), 999.0)
    pb = _safe(row.get("pb"), 999.0)
    ey = _safe(row.get("earnings_yield"), 0.0)

    score = 0.0
    reasons = []

    # PE: 0<PE<30 为佳，PE>100 或负PE 减分
    if 0 < pe <= 15:
        score += 10.0
    elif 15 < pe <= 30:
        score += 6.0
    elif 30 < pe <= 60:
        score += 2.0
    elif pe > 100 or pe <= 0:
        score -= 5.0

    # PB: PB<3 为佳
    if 0 < pb <= 1.5:
        score += 8.0
    elif 1.5 < pb <= 3.0:
        score += 4.0
    elif 3.0 < pb <= 6.0:
        score += 1.0
    elif pb > 10:
        score -= 3.0

    # 盈利收益率（1/PE）
    if ey > 0.05:  # PE<20
        score += 7.0
    elif ey > 0.03:  # PE<33
        score += 3.0

    return score


def factor_turnover_penalty(row: pd.Series) -> float:
    """高换手率惩罚因子。

    换手率>20% 说明筹码松动，次日容易低开。
    turnover_rate 是百分数（如 5.32 表示 5.32%）。

    预期 IC fwd_ret_3: 正（负向因子取负值）
    """
    turnover = _safe(row.get("turnover_rate"), 5.0)

    if turnover > 30:
        return -15.0
    if turnover > 20:
        return -10.0
    if turnover > 15:
        return -5.0
    return 0.0


def factor_volume_ratio_penalty(row: pd.Series) -> float:
    """高量比惩罚因子。

    量比>3 说明过度放量，追高风险极大。
    量比<0.5 说明无量，也无机会。

    预期 IC fwd_ret_3: 正（负向因子取负值）
    """
    vol_ratio = _safe(row.get("volume_ratio"), 1.0)
    # fallback to proxy
    if vol_ratio == 0:
        vol_ratio = _safe(row.get("vol_ratio_proxy"), 1.0)

    if vol_ratio > 4.0:
        return -12.0
    if vol_ratio > 3.0:
        return -8.0
    if vol_ratio > 2.5:
        return -5.0
    if vol_ratio < 0.4:
        return -3.0
    return 0.0


def factor_net_mf_signal(row: pd.Series) -> float:
    """主力资金信号。

    主力净流入/流通市值比率正向预测收益。
    """
    net_mf_ratio = _safe(row.get("net_mf_ratio"), 0.0)

    if net_mf_ratio > 0.5:
        return 10.0
    if net_mf_ratio > 0.2:
        return 6.0
    if net_mf_ratio > 0.05:
        return 3.0
    if net_mf_ratio < -0.5:
        return -10.0
    if net_mf_ratio < -0.2:
        return -5.0
    return 0.0


def factor_elg_inflow_signal(row: pd.Series) -> float:
    """超大单资金流入信号。

    超大单净买入/流通市值比率。
    超大单通常代表机构行为，正向预测收益。
    """
    circ_mv = _safe(row.get("circ_mv"), 1.0)
    elg_net = _safe(row.get("elg_net"), 0.0)  # 超大单净买入（万元）

    if circ_mv > 0:
        ratio = elg_net / circ_mv * 100  # 百分比
        if ratio > 0.3:
            return 12.0
        if ratio > 0.1:
            return 8.0
        if ratio > 0.03:
            return 4.0
        if ratio < -0.3:
            return -12.0
        if ratio < -0.1:
            return -6.0
    return 0.0


# ═══════════════════════════════════════════════════════════
# 第十二类：收益率最优综合评分
# ═══════════════════════════════════════════════════════════

def factor_return_optimized_total(row: pd.Series) -> float:
    """收益率优化综合评分：最大化 fwd_ret_3 IC 同时保持 hit_limit_3 IC。

    设计原则：
    - shortterm 仍为核心但降权（防止过度追高）
    - fundamental 大幅提权（fwd_ret_3 IC最高）
    - circ_mv 新增强因子（同时预测收益和命中）
    - 换手率/量比惩罚（反追高）
    - 主力资金信号加分
    - 增强追高护栏
    """
    st = _safe(row.get("shortterm"))
    tech = _safe(row.get("technical"))
    fund = _safe(row.get("fundamental"))
    t10 = _safe(row.get("trailing_10_pit", row.get("trailing_10")))
    t5 = _safe(row.get("trailing_5_pit", row.get("trailing_5")))
    pos = _safe(row.get("position_20d"), 0.5)
    pb = _safe(row.get("pullback_10d"), 0.1)
    sent = _safe(row.get("sentiment"))

    # ── 核心得分 ──
    score = 0.0

    # shortterm: 降权到 1.2（从 1.8），减少追高成分
    score += st * 1.2

    # fundamental: 大幅提权到 1.2（从 0.6），利用其 fwd_ret_3 IC=0.10
    score += fund * 1.2

    # technical: 保持 0.6
    score += tech * 0.6

    # ── 新增因子 ──
    score += factor_circ_mv_tier(row) * 1.0        # 0-20 → 0-20
    score += factor_fundamental_quality(row) * 0.8  # 0-25 → 0-20
    score += factor_limit_up_gene_composite(row) * 0.8  # 0-25 → 0-20
    score += factor_pullback_from_peak(row) * 0.6   # 0-15 → 0-9
    score += factor_sentiment_contrarian(row) * 0.8  # 0-10 → 0-8
    score += factor_net_mf_signal(row) * 0.6         # 0-10 → 0-6

    # ── 惩罚项 ──
    score += factor_turnover_penalty(row) * 0.8       # 0 to -12
    score += factor_volume_ratio_penalty(row) * 0.6    # 0 to -7.2

    # ── 追高护栏（乘法） ──
    penalty = 1.0
    if t10 > 0.30:
        penalty *= 0.70
    elif t10 > 0.20:
        penalty *= 0.82
    elif t10 > 0.10:
        penalty *= 0.92
    if t5 > 0.15:
        penalty *= 0.88
    if pos > 0.85 and pb < 0.03:
        penalty *= 0.78
    if sent > 60 and t10 > 0.15:
        penalty *= 0.82

    score *= penalty

    return round(score, 2)


def factor_quality_value_total(row: pd.Series) -> float:
    """质量价值综合评分：更偏向基本面质量+低换手+大市值。

    适合追求高胜率、接受略低命中率的策略。
    """
    st = _safe(row.get("shortterm"))
    tech = _safe(row.get("technical"))
    fund = _safe(row.get("fundamental"))
    fundflow = _safe(row.get("fundflow"))
    t10 = _safe(row.get("trailing_10_pit", row.get("trailing_10")))
    t5 = _safe(row.get("trailing_5_pit", row.get("trailing_5")))
    pos = _safe(row.get("position_20d"), 0.5)

    score = 0.0

    # 核心：更加均衡
    score += st * 1.0         # 短线降权
    score += fund * 1.5       # 基本面最高权重
    score += tech * 0.5       # 技术面降权
    score += fundflow * 0.3   # 资金面轻权重

    # 新增因子
    score += factor_circ_mv_tier(row) * 1.2        # 市值重点加分
    score += factor_fundamental_quality(row) * 1.0  # 基本面质量
    score += factor_limit_up_gene_composite(row) * 0.5
    score += factor_pullback_from_peak(row) * 0.8
    score += factor_sentiment_contrarian(row) * 1.0
    score += factor_net_mf_signal(row) * 0.8
    score += factor_elg_inflow_signal(row) * 0.6

    # 惩罚项（更严格）
    score += factor_turnover_penalty(row) * 1.0
    score += factor_volume_ratio_penalty(row) * 0.8

    # 追高护栏（更严格）
    penalty = 1.0
    if t10 > 0.25:
        penalty *= 0.65
    elif t10 > 0.15:
        penalty *= 0.80
    elif t10 > 0.08:
        penalty *= 0.92
    if t5 > 0.12:
        penalty *= 0.85
    if pos > 0.80:
        penalty *= 0.85

    score *= penalty
    return round(score, 2)

def factor_chasing_guardrail_v2(
    total: float,
    trailing_5: float | None = None,
    trailing_10: float | None = None,
    position_20d: float | None = None,
    pullback_10d: float | None = None,
) -> float:
    """增强版追高护栏：综合 trailing + position + pullback。

    相比 v1 仅看 trailing_10，v2 引入位置和回调维度：
    - 高位置(trailing_10>20% 或 position_20d>80%) → 显著惩罚
    - 无回调(pullback_10d<2%) → 追高惩罚
    - 低位(trailing_10<0% 且 position_20d<30%) → 轻微加分

    Returns:
        adjusted total
    """
    t5 = _safe(trailing_5)
    t10 = _safe(trailing_10)
    pos = _safe(position_20d, 0.5)
    pb = _safe(pullback_10d, 0.1)

    adj = total
    penalty = 1.0

    # 位置惩罚
    if t10 > 0.30:
        penalty *= 0.80
    elif t10 > 0.20:
        penalty *= 0.90
    elif t10 > 0.10:
        penalty *= 0.95

    if pos > 0.85 and pb < 0.03:
        penalty *= 0.85  # 高位无回调→强追高

    if t5 > 0.15:
        penalty *= 0.92  # 短期急涨

    # 低位加分
    if t10 < -0.05 and pos < 0.30:
        penalty *= 1.05  # 低位轻微加分

    return adj * penalty


# ═══════════════════════════════════════════════════════════
# 第九类：new_total 替代因子（完整替代现有 total）
# ═══════════════════════════════════════════════════════════

def factor_new_total_v2(row: pd.Series) -> float:
    """基于 enriched features 的全新综合评分（替代原有 total）。

    设计思路：
    - shortterm 仍是核心（hit_limit_3 IC最高）
    - fundamental 对冲（fwd_ret_3 IC最高），提升胜率
    - 涨停基因 + 技术面确认
    - 回调位置反追高
    - 输出 scale 对齐原 total（0-100 量级）
    """
    st = _safe(row.get("shortterm"))
    tech = _safe(row.get("technical"))
    fund = _safe(row.get("fundamental"))
    t10 = _safe(row.get("trailing_10_pit", row.get("trailing_10")))
    pos = _safe(row.get("position_20d"), 0.5)
    pb = _safe(row.get("pullback_10d"), 0.1)

    # ── 核心得分 ──
    # shortterm: 0-50 → 乘 1.8 映射到 0-90 range
    score = st * 1.8

    # fundamental: 0-50 → 乘 0.6 (fwd_ret_3 IC最高，但 hit IC低，小权重参与)
    score += fund * 0.6

    # ── 技术面：经追高惩罚 ──
    tech_penalty = 1.0
    if t10 > 0.25 and pos > 0.80:
        tech_penalty = 0.3
    elif t10 > 0.20:
        tech_penalty = 0.6
    elif t10 > 0.10:
        tech_penalty = 0.8
    score += tech * tech_penalty * 0.8

    # ── 新增因子 ──
    score += factor_limit_up_gene_composite(row) * 1.2  # 0-25 → 0-30
    score += factor_pullback_quality(row) * 0.8         # 0-18 → 0-14.4
    score += factor_vol_expansion_quality(row) * 0.8    # 0-18 → 0-14.4
    score += factor_reversal_signal(row) * 1.0          # 0-20 → 0-20
    score += factor_sentiment_contrarian(row) * 0.8     # 0-10 → 0-8

    # ── 新增：概念动量 + 机构因子 ──
    score += factor_concept_momentum(row) * 0.6       # 概念轮动
    score += factor_concept_up_streak(row) * 0.3      # 概念持续性
    score += factor_inst_following(row) * 0.4         # 机构跟随

    return round(score, 2)


def factor_balanced_total(row: pd.Series) -> float:
    """均衡型综合评分：兼顾 hit rate 和 win rate。

    与 new_total_v2 的区别：
    - 给 fundamental 更高权重（提升 fwd_ret_3 IC）
    - 加入 sentiment_contrarian 对冲
    - 更严格的追高惩罚
    - 目标：hit_limit_3 IC ≥ 0.18 且 fwd_ret_3 IC ≥ 0.03
    """
    st = _safe(row.get("shortterm"))
    tech = _safe(row.get("technical"))
    fund = _safe(row.get("fundamental"))
    sent = _safe(row.get("sentiment"))
    t10 = _safe(row.get("trailing_10_pit", row.get("trailing_10")))
    t5 = _safe(row.get("trailing_5_pit", row.get("trailing_5")))
    pos = _safe(row.get("position_20d"), 0.5)
    pb = _safe(row.get("pullback_10d"), 0.1)

    # ── 核心得分 ──
    score = st * 1.6        # shortterm 仍为核心 (0-50 → 0-80)
    score += fund * 1.0     # fundamental 权重提升 (0-50 → 0-50)
    score += tech * 0.5     # technical 降权 (0-50 → 0-25)

    # ── 新增因子 ──
    score += factor_limit_up_gene_composite(row) * 1.0   # 0-25
    score += factor_pullback_from_peak(row) * 0.8        # 0-15 → 0-12
    score += factor_sentiment_contrarian(row) * 1.0      # 0-10
    score += factor_gap_up_quality(row) * 0.6            # 0-18 → 0-10.8

    # ── 新增：概念动量（弱市关键）+ 机构因子 ──
    score += factor_concept_momentum(row) * 0.8         # 概念共振（提权）
    score += factor_concept_up_streak(row) * 0.3        # 概念持续性
    score += factor_concept_turnover(row) * 0.3         # 概念换手热度
    score += factor_inst_consistency(row) * 0.3         # 机构共识
    score += factor_top_list_quality(row) * 0.2         # 龙虎榜质量

    # ── 追高惩罚（乘法） ──
    penalty = 1.0
    if t10 > 0.30:
        penalty *= 0.75
    elif t10 > 0.20:
        penalty *= 0.85
    elif t10 > 0.10:
        penalty *= 0.93
    if t5 > 0.15:
        penalty *= 0.90
    if pos > 0.85 and pb < 0.03:
        penalty *= 0.80  # 高位无回调 → 强追高
    if sent > 60 and t10 > 0.15:
        penalty *= 0.85  # 情绪过热+已涨

    score *= penalty

    return round(score, 2)


def factor_aggressive_total(row: pd.Series) -> float:
    """激进型综合评分：最大化 hit_rate。

    适用场景：用户愿意接受更低胜率以换取更高命中率。
    """
    st = _safe(row.get("shortterm"))
    tech = _safe(row.get("technical"))
    t10 = _safe(row.get("trailing_10_pit", row.get("trailing_10")))
    pos = _safe(row.get("position_20d"), 0.5)

    score = st * 2.0                 # 0-50 → 0-100
    score += tech * 0.8              # 0-50 → 0-40
    score += factor_limit_up_gene_composite(row) * 1.5   # 0-25 → 0-37.5
    score += factor_reversal_signal(row) * 1.2           # 0-20 → 0-24
    score += factor_gap_up_quality(row) * 0.8            # 0-18 → 0-14.4

    # 追高惩罚(较轻)
    penalty = 1.0
    if t10 > 0.35:
        penalty *= 0.75
    elif t10 > 0.25:
        penalty *= 0.85
    if pos > 0.90:
        penalty *= 0.85

    score *= penalty
    return round(score, 2)


# ═══════════════════════════════════════════════════════════
# 第十类：维度追高惩罚因子（与原有维度分组合使用）
# ═══════════════════════════════════════════════════════════

def factor_technical_anti_chasing(row: pd.Series) -> float:
    """技术维度反追高调整。

    根据 trailing_10 + position_20d 综合判断惩罚力度。
    """
    t10 = _safe(row.get("trailing_10_pit", row.get("trailing_10")))
    pos = _safe(row.get("position_20d"), 0.5)
    pb = _safe(row.get("pullback_10d"), 0.1)

    if t10 > 0.30 and pos > 0.85:
        return -20.0
    if t10 > 0.25 and pos > 0.75:
        return -15.0
    if t10 > 0.20 and pb < 0.02:
        return -10.0
    if t10 > 0.15:
        return -5.0
    return 0.0


def factor_shortterm_anti_chasing(row: pd.Series) -> float:
    """短线维度反追高调整（比 technical 轻）。"""
    t10 = _safe(row.get("trailing_10_pit", row.get("trailing_10")))
    t5 = _safe(row.get("trailing_5_pit", row.get("trailing_5")))

    if t10 > 0.30 and t5 > 0.15:
        return -15.0
    if t10 > 0.25:
        return -10.0
    if t10 > 0.15:
        return -5.0
    return 0.0


def factor_sentiment_anti_chasing(row: pd.Series) -> float:
    """情绪维度反追高：高情绪+高位置=最危险。"""
    t10 = _safe(row.get("trailing_10_pit", row.get("trailing_10")))
    sent = _safe(row.get("sentiment"))

    if sent > 60 and t10 > 0.15:
        return -15.0  # 情绪高+已涨高=最可能见顶
    if sent > 50 and t10 > 0.10:
        return -8.0
    return 0.0


# ═══════════════════════════════════════════════════════════
# 第十二类：概念动量因子（新增）
# ═══════════════════════════════════════════════════════════

def factor_concept_momentum(row: pd.Series) -> float:
    """概念动量：股票所属强势概念的综合动量评分。

    使用概念最大动量（最强概念驱动）和概念广度（多概念共振）。
    """
    ret1 = _safe(row.get("cpt_ret_1d_max"), 0.0)
    ret3 = _safe(row.get("cpt_ret_3d_max"), 0.0)
    ret5 = _safe(row.get("cpt_ret_5d_max"), 0.0)
    ret1_avg = _safe(row.get("cpt_ret_1d_avg"), 0.0)
    ret3_avg = _safe(row.get("cpt_ret_3d_avg"), 0.0)
    n_cpt = _safe(row.get("n_concepts"), 0.0)
    up_ratio = _safe(row.get("cpt_up_ratio"), 0.5)

    # 核心：最强概念近3日动量（主要信号）
    score = ret3 * 2.5  # 概念3日动量权重最高

    # 辅助：1日动量（短期爆发）+ 5日趋势
    if ret1 > 2.0:  # 当日大涨的概念
        score += ret1 * 0.8
    elif ret1 < -2.0:  # 当日大跌但3日仍强 → 可能是回调买点
        if ret3 > 3.0:
            score += ret3 * 0.3  # 不给负分，等待确认

    if ret5 > 5.0:  # 5日持续强势
        score += 5.0
    elif ret5 < -5.0:
        score -= 8.0  # 概念持续走弱

    # 平均动量（多概念共振）
    if ret3_avg > 2.0 and ret1_avg > 0:
        score += ret3_avg * 1.0  # 多概念都在涨 → 板块轮动确认

    # 概念广度
    if n_cpt >= 5:
        if up_ratio > 0.6:  # >60%概念上涨 → 广泛共振
            score += 8.0
        elif up_ratio > 0.4:
            score += 4.0
    elif n_cpt >= 3:
        if up_ratio > 0.6:
            score += 5.0
    else:  # 概念少 → 专注度高但不加分（可能是冷门概念）
        pass

    return round(score, 2)


def factor_concept_up_streak(row: pd.Series) -> float:
    """概念上涨持续性：最强概念连续上涨天数。

    连续上涨 → 概念趋势确立，炒作情绪高涨。
    """
    streak = _safe(row.get("cpt_up_streak_max"), 0.0)
    ret1 = _safe(row.get("cpt_ret_1d_max"), 0.0)

    score = 0.0
    if streak >= 3 and ret1 > 0:
        score = 12.0  # 连续3天上涨+今天还在涨 → 强趋势
    elif streak >= 2 and ret1 > 0:
        score = 7.0
    elif streak >= 2:
        score = 3.0  # 连续涨但今天回调 → 可能是进场机会

    return score


def factor_concept_turnover(row: pd.Series) -> float:
    """概念换手率：概念板块交易活跃度。

    高换手+正涨幅 → 资金积极涌入概念。
    高换手+负涨幅 → 资金出逃。
    """
    turn = _safe(row.get("cpt_turn_5d_max"), 0.0)
    ret3 = _safe(row.get("cpt_ret_3d_max"), 0.0)

    if turn > 15 and ret3 > 2:
        return 10.0  # 高活跃+正收益 = 资金涌入
    elif turn > 10 and ret3 > 0:
        return 5.0
    elif turn > 20:  # 极高换手但没涨 → 出货嫌疑
        return -8.0
    elif turn < 3:  # 极低换手 → 无人关注
        return -5.0

    return 0.0


# ═══════════════════════════════════════════════════════════
# 第十三类：龙虎榜机构因子（新增）
# ═══════════════════════════════════════════════════════════

def factor_inst_following(row: pd.Series) -> float:
    """机构跟随信号：龙虎榜机构买卖强度。

    机构净买入+多机构参与 → 机构看好，胜率高。
    """
    net = _safe(row.get("inst_net_amount"), 0.0)
    n_buyers = _safe(row.get("n_inst_buyers"), 0.0)
    buy_ratio = _safe(row.get("inst_buy_ratio"), 0.5)
    score = _safe(row.get("inst_score"), 0.0)

    # 综合 score 已经是 0-10 的量级，直接使用
    return score


def factor_top_list_quality(row: pd.Series) -> float:
    """龙虎榜质量：上榜个股的买卖质量。

    净买入+高涨幅上榜 → 强势股获机构认可。
    净卖出上榜 → 出货信号。
    """
    is_tl = _safe(row.get("is_top_list"), 0.0)
    if is_tl < 1:
        return 0.0  # 没上榜，不评分

    quality = _safe(row.get("tl_quality"), 0.0)
    net = _safe(row.get("tl_net_amount"), 0.0)

    # quality 0-10
    score = quality

    # 额外加分：上榜且大额净买入
    if net > 1e8:  # 净买入>1亿
        score += 3.0
    elif net > 5e7:  # >5000万
        score += 1.5

    # 上榜但净卖出 → 负分
    if net < -5e7:
        score -= 5.0

    return score


def factor_inst_consistency(row: pd.Series) -> float:
    """机构一致性：买方机构数量和质量。

    多家知名机构同时买入 → 强烈看多信号。
    """
    n_buyers = _safe(row.get("n_inst_buyers"), 0.0)
    buy_ratio = _safe(row.get("inst_buy_ratio"), 0.5)
    trade_count = _safe(row.get("inst_trade_count"), 0.0)

    score = 0.0
    if n_buyers >= 5:
        score = 12.0  # 5+机构买入 → 强共识
    elif n_buyers >= 3:
        score = 7.0
    elif n_buyers >= 1:
        score = 3.0

    # 买方占比加成
    if buy_ratio > 0.8 and trade_count >= 5:
        score += 3.0  # 高度一致的买盘

    return score


def factor_amount_power_pit(row: pd.Series) -> float:
    """量能爆发力（PIT）：成交额相对5日均值显著放大，且绝对成交额充足、位置不过高。

    因子挖掘发现 avg_amount_5d（IC ~0.20）比 amount_ratio 更有预测力，
    说明涨停股需要绝对活跃度。本因子结合相对放量+绝对水平。
    """
    amount_ratio = _safe(row.get("amount_ratio"), 1.0)
    avg_amount = _safe(row.get("avg_amount_5d"), 0.0)
    position = _safe(row.get("position_20d"), 0.5)
    pct_std10 = _safe(row.get("pct_chg_std_10d"), 0.0)

    score = 0.0
    # 高绝对成交额 + 明显放量 + 位置合理
    if avg_amount >= 2_000_000 and amount_ratio > 1.5 and 0.25 <= position <= 0.80:
        score += 18.0
    elif avg_amount >= 1_000_000 and amount_ratio > 1.3 and 0.20 <= position <= 0.85:
        score += 12.0
    elif avg_amount >= 500_000 and amount_ratio > 1.2 and pct_std10 > 3.5:
        score += 6.0

    # 极度缩量或无活跃度惩罚
    if amount_ratio < 0.6 or avg_amount < 100_000:
        score -= 5.0
    return score


def factor_volatility_activation_pit(row: pd.Series) -> float:
    """波动激活（PIT）：高波动+适中位置，预示启动。

    pct_chg_std_10d / position_20d 均为 Top-5 单变量 IC 特征。
    """
    std10 = _safe(row.get("pct_chg_std_10d"), 0.0)
    std5 = _safe(row.get("pct_chg_std_5d"), 0.0)
    position = _safe(row.get("position_20d"), 0.5)
    max5 = _safe(row.get("max_pct_chg_5d"), 0.0)

    score = 0.0
    if std10 > 4.5 and 0.30 <= position <= 0.70 and max5 > 5.0:
        score += 20.0
    elif std10 > 3.5 and 0.25 <= position <= 0.75 and max5 > 3.5:
        score += 12.0
    elif std5 > 3.0 and position > 0.20:
        score += 6.0

    # 极低波动通常没行情
    if std10 < 2.0:
        score -= 4.0
    return score


def factor_limit_gene_momentum_pit(row: pd.Series) -> float:
    """涨停基因共振（PIT）：有涨停基因 + 技术确认 + 未过度追涨。

    limit_up_count_20d/60d 是强预测特征，但需配合技术分和位置过滤追高。
    """
    gene20 = _safe(row.get("limit_up_count_20d"), 0.0)
    gene60 = _safe(row.get("limit_up_count_60d"), 0.0)
    tech = _safe(row.get("technical"), 0.0)
    t10 = _safe(row.get("trailing_10_pit", row.get("trailing_10")), 0.0)
    position = _safe(row.get("position_20d"), 0.5)

    score = 0.0
    # 涨停基因加分
    if gene20 >= 3:
        score += 15.0
    elif gene20 >= 2:
        score += 10.0
    elif gene20 >= 1:
        score += 5.0

    # 长周期基因平滑加分
    if gene60 >= 4:
        score += 8.0
    elif gene60 >= 2:
        score += 4.0

    # 技术确认
    if tech >= 40:
        score += 8.0
    elif tech >= 25:
        score += 4.0

    # 反追高：已大涨或位置过高则削弱
    if t10 > 0.35:
        score *= 0.60
    elif t10 > 0.25:
        score *= 0.80
    if position > 0.85:
        score *= 0.70

    return round(score, 2)


def factor_breakout_quality_pit(row: pd.Series) -> float:
    """突破质量（PIT）：接近10日高点、但20日维度仍有空间，且放量。

    因子挖掘显示 high_10d/20d 与涨停强相关，说明股价处于相对高位是强势股特征；
    但需避免已在20日最高点附近（无空间）。
    """
    pb10 = _safe(row.get("pullback_10d"), 0.0)
    pb20 = _safe(row.get("pullback_20d"), 0.0)
    position = _safe(row.get("position_20d"), 0.5)
    vol_ratio = _safe(row.get("vol_ratio_proxy"), 1.0)
    amount_ratio = _safe(row.get("amount_ratio"), 1.0)

    score = 0.0
    # 接近10日高点（pullback 小）+ 20日维度未超买 + 放量
    if pb10 < 0.05 and 0.03 <= pb20 <= 0.15 and position >= 0.60 and vol_ratio > 1.2:
        score += 18.0
    elif pb10 < 0.08 and 0.02 <= pb20 <= 0.20 and position >= 0.50 and (vol_ratio > 1.0 or amount_ratio > 1.2):
        score += 10.0
    elif pb10 < 0.15 and position >= 0.40:
        score += 4.0

    # 已在20日最高点附近且无回调 → 追高风险
    if pb20 < 0.02 and position > 0.85:
        score -= 8.0

    return score


def factor_trailing_momentum_pit(row: pd.Series) -> float:
    """趋势动量质量（PIT）：有上涨趋势但不过热，且位置有空间。

    因子挖掘显示 trailing_10_pit IC ~0.20，但单纯追高会亏。
    本因子奖励温和上涨趋势（5%-25%）+ 位置未超买。
    """
    t10 = _safe(row.get("trailing_10_pit", row.get("trailing_10")), 0.0)
    t5 = _safe(row.get("trailing_5_pit", row.get("trailing_5")), 0.0)
    position = _safe(row.get("position_20d"), 0.5)
    pb10 = _safe(row.get("pullback_10d"), 0.0)

    score = 0.0
    # 温和上涨趋势 + 位置有空间 + 未从10日高点深跌
    if 0.05 <= t10 <= 0.25 and position <= 0.75 and pb10 <= 0.10:
        score += 16.0
    elif 0.03 <= t10 <= 0.30 and position <= 0.80 and pb10 <= 0.15:
        score += 10.0
    elif t10 > 0.0 and t5 > 0.0 and position > 0.30:
        score += 4.0

    # 过热惩罚
    if t10 > 0.35:
        score -= 8.0
    elif t10 > 0.25 and position > 0.85:
        score -= 5.0

    return score


def factor_intraday_strength_pit(row: pd.Series) -> float:
    """盘中强度（PIT）：扫描时涨幅 + 开盘缺口 + 放量共振。

    使用扫描记录里的 pct_chg_score_day（盘中已知）和 PIT gap_up_pit / vol_ratio。
    """
    pct = _safe(row.get("pct_chg_score_day"), 0.0)
    gap = _safe(row.get("gap_up_pit", row.get("gap_up")), 0.0)
    vol_ratio = _safe(row.get("vol_ratio_proxy"), 1.0)
    position = _safe(row.get("position_20d"), 0.5)

    score = 0.0
    # 温和高开 + 盘中已涨 + 放量 + 位置合理
    if 1.5 <= gap <= 5.0 and 2.0 <= pct <= 7.0 and vol_ratio > 1.3 and 0.30 <= position <= 0.75:
        score += 18.0
    elif 0.5 <= gap <= 3.0 and 1.5 <= pct <= 5.0 and vol_ratio > 1.0 and position <= 0.80:
        score += 10.0
    elif pct > 0 and vol_ratio > 1.2 and position > 0.25:
        score += 4.0

    # 开盘过高或已涨太多 → 追高风险
    if gap > 7.0 or pct > 8.0:
        score -= 8.0

    return score


def factor_large_cap_limit_gene_pit(row: pd.Series) -> float:
    """大市值涨停基因（PIT）：流通市值大 + 有涨停基因 + 技术确认。

    PIT V4 评估显示 circ_mv_tier IC=0.10，说明大市值股涨停概率更高；
    结合涨停基因可进一步提纯。
    """
    circ_mv = _safe(row.get("circ_mv"), 0.0)
    gene20 = _safe(row.get("limit_up_count_20d"), 0.0)
    gene60 = _safe(row.get("limit_up_count_60d"), 0.0)
    tech = _safe(row.get("technical"), 0.0)

    score = 0.0
    # 大市值加分
    if circ_mv >= 500_0000:      # 500亿+
        score += 12.0
    elif circ_mv >= 200_0000:    # 200亿+
        score += 9.0
    elif circ_mv >= 100_0000:    # 100亿+
        score += 6.0
    elif circ_mv >= 50_0000:     # 50亿+
        score += 3.0

    # 涨停基因加分
    if gene20 >= 2:
        score += 10.0
    elif gene20 >= 1:
        score += 5.0
    if gene60 >= 3:
        score += 6.0
    elif gene60 >= 1:
        score += 3.0

    # 技术确认
    if tech >= 40:
        score += 6.0
    elif tech >= 25:
        score += 3.0

    return score


def factor_turnover_momentum_pit(row: pd.Series) -> float:
    """换手动量（PIT）：高换手+高量比+有波动，说明资金活跃。

    PIT V4 评估显示 turnover_penalty 是负 IC（-0.14），
    说明高换手反而利好涨停。本因子把高换手当作正向信号。
    """
    turnover = _safe(row.get("turnover_rate"), 5.0)
    vol_ratio = _safe(row.get("volume_ratio"), 1.0)
    std5 = _safe(row.get("pct_chg_std_5d"), 0.0)
    position = _safe(row.get("position_20d"), 0.5)

    score = 0.0
    if turnover >= 15 and vol_ratio >= 1.5 and std5 >= 3.0 and 0.30 <= position <= 0.80:
        score += 18.0
    elif turnover >= 10 and vol_ratio >= 1.2 and std5 >= 2.5 and position >= 0.25:
        score += 12.0
    elif turnover >= 5 and vol_ratio >= 1.0 and std5 >= 2.0:
        score += 5.0

    # 换手过低惩罚
    if turnover < 2:
        score -= 5.0

    return score


def factor_growth_momentum_pit(row: pd.Series) -> float:
    """成长动量（PIT）：高估值（偏成长）+ 短线技术强 + 高波动。

    PIT V4 评估显示 fundamental_quality 是强负 IC（-0.14），
    说明低 PE/PB 的价值股反而不易涨停。本因子奖励高 PE/高成长属性。
    """
    pe = _safe(row.get("pe"), 999.0)
    pb = _safe(row.get("pb"), 999.0)
    st = _safe(row.get("shortterm"), 0.0)
    tech = _safe(row.get("technical"), 0.0)
    std10 = _safe(row.get("pct_chg_std_10d"), 0.0)

    score = 0.0
    # 偏成长估值
    if pe > 50 or pe <= 0:   # 亏损或高估值成长股
        score += 6.0
    elif pe > 30:
        score += 3.0

    if pb > 5:
        score += 4.0
    elif pb > 3:
        score += 2.0

    # 动量确认
    if st >= 45 and tech >= 35 and std10 >= 3.5:
        score += 12.0
    elif st >= 35 and tech >= 25 and std10 >= 2.5:
        score += 6.0

    return score


def factor_balanced_total_pit(row: pd.Series) -> float:
    """均衡型 PIT 综合评分（替代原 balanced_total）。

    基于 PIT 数据挖掘结果，组合最有效的方向：
    - shortterm / technical 维度分
    - 大市值 + 涨停基因
    - 波动激活 + 换手动量
    - 成长动量
    - 概念动量 / 概念连涨 / 概念换手
    - 反追高惩罚

    权重来自 2026-06 panel grid search（overall RankIC 最大化）：
    shortterm=0.6, technical=0.2, large_cap=0.3, volatility=0.3,
    turnover=0.7, limit_gene=0.2, growth=0.3,
    concept_momentum=0.7, concept_up_streak=0.5, concept_turnover=0.7
    """
    st = _safe(row.get("shortterm"), 0.0)
    tech = _safe(row.get("technical"), 0.0)
    sent = _safe(row.get("sentiment"), 0.0)

    score = 0.0
    score += st * 0.6
    score += tech * 0.2

    score += factor_large_cap_limit_gene_pit(row) * 0.3
    score += factor_volatility_activation_pit(row) * 0.3
    score += factor_turnover_momentum_pit(row) * 0.7
    score += factor_limit_gene_momentum_pit(row) * 0.2
    score += factor_growth_momentum_pit(row) * 0.3

    score += factor_concept_momentum(row) * 0.7
    score += factor_concept_up_streak(row) * 0.5
    score += factor_concept_turnover(row) * 0.7

    # 追高惩罚（乘法）
    t10 = _safe(row.get("trailing_10_pit", row.get("trailing_10")), 0.0)
    t5 = _safe(row.get("trailing_5_pit", row.get("trailing_5")), 0.0)
    position = _safe(row.get("position_20d"), 0.5)
    pb10 = _safe(row.get("pullback_10d"), 0.0)

    penalty = 1.0
    if t10 > 0.30:
        penalty *= 0.75
    elif t10 > 0.20:
        penalty *= 0.85
    elif t10 > 0.10:
        penalty *= 0.93
    if t5 > 0.15:
        penalty *= 0.90
    if position > 0.85 and pb10 < 0.03:
        penalty *= 0.80
    if sent > 60 and t10 > 0.15:
        penalty *= 0.85

    score *= penalty
    return round(max(0.0, score), 1)


def factor_sentiment_adaptive_total_pit(row: pd.Series) -> float:
    """Sentiment-自适应综合评分（PIT）。

    条件挖掘显示 sentiment 是中轴变量，不同 sentiment 区间有效子因子方向不同：
    - 高情绪 (sentiment>=55): position_20d / trailing_10_pit / pullback_20d 有效，
      fundamental 呈负向。
    - 中情绪 (35<=sentiment<55): technical 是负向陷阱，shortterm+涨停基因更有效。
    - 低情绪 (sentiment<35): 整体命中率低，仅保留波动率+涨停基因，整体降权。
    """
    sent = _safe(row.get("sentiment"), 0.0)
    st = _safe(row.get("shortterm"), 0.0)
    tech = _safe(row.get("technical"), 0.0)
    fund = _safe(row.get("fundamental"), 0.0)
    fundflow = _safe(row.get("fundflow"), 0.0)

    pos = _safe(row.get("position_20d"), 0.5)
    t10 = _safe(row.get("trailing_10_pit", row.get("trailing_10")), 0.0)
    pb20 = _safe(row.get("pullback_20d"), 0.0)
    pb10 = _safe(row.get("pullback_10d"), 0.0)
    std5 = _safe(row.get("pct_chg_std_5d"), 0.0)
    std10 = _safe(row.get("pct_chg_std_10d"), 0.0)
    gene20 = _safe(row.get("limit_up_count_20d"), 0.0)
    gene60 = _safe(row.get("limit_up_count_60d"), 0.0)
    amount_ratio = _safe(row.get("amount_ratio"), 1.0)
    circ_mv = _safe(row.get("circ_mv"), 0.0)

    # 基础：sentiment 本身是最强单变量
    score = sent * 0.45

    if sent >= 55:
        # 高情绪区：位置/趋势/回调/量能/涨停基因
        if 0.40 <= pos <= 0.75:
            score += 12.0
        elif 0.25 <= pos <= 0.85:
            score += 6.0
        elif pos > 0.92:
            score -= 10.0

        if 0.05 <= t10 <= 0.25:
            score += 10.0
        elif 0.25 < t10 <= 0.40:
            score += 4.0
        elif t10 > 0.50:
            score -= 10.0

        if 0.03 <= pb20 <= 0.15:
            score += 8.0
        elif pb20 < 0.02:
            score -= 6.0

        score += min(gene20, 5) * 3.0 + min(max(gene60 - gene20, 0), 5) * 1.5

        if amount_ratio > 1.5:
            score += 6.0
        elif amount_ratio < 0.6:
            score -= 4.0

        # 高情绪区 fundamental 偏负：高基本面分反而抑制涨停
        if fund > 55:
            score -= 7.0
        elif fund < 30:
            score += 3.0

        # 大市值加分（与涨停基因共振）
        if circ_mv >= 100_0000 and gene20 >= 1:
            score += 5.0

    elif sent >= 35:
        # 中情绪区：shortterm + 涨停基因，technical 是陷阱
        score += st * 0.35
        score += min(gene20, 5) * 2.5

        if tech > 45:
            score -= 10.0
        elif tech > 35:
            score -= 5.0
        elif tech < 25:
            score += 4.0

        if fundflow > 50:
            score -= 5.0
        elif fundflow < 30:
            score += 3.0

        if 0.03 <= t10 <= 0.20:
            score += 5.0
        elif t10 > 0.35:
            score -= 6.0

        if 0.30 <= pos <= 0.70:
            score += 4.0

    else:
        # 低情绪区：整体低命中，轻参与，只留高波动+涨停基因
        score = score * 0.4
        if std5 > 3.5:
            score += 5.0
        if std10 > 4.0:
            score += 4.0
        if gene20 >= 2:
            score += 5.0
        if pb10 > 0.05:
            score += 3.0

    return round(max(0.0, score), 2)


def factor_sentiment_conditional_pit(row: pd.Series) -> float:
    """Sentiment-条件互补因子（PIT）：不含 sentiment 自身，按 sentiment 区间选择子因子。

    条件挖掘显示：
    - 高情绪 (sentiment>=55): position_20d / trailing_10_pit / pullback_20d 有效
    - 中情绪 (35<=sentiment<55): shortterm 正向，technical 是陷阱（负向）
    - 低情绪 (sentiment<35): pct_chg_std_5d/10d 等波动率因子略有效

    本因子与 sentiment 正交互补，推荐组合方式：score = sentiment + 0.5 * sentiment_conditional_pit
    """
    sent = _safe(row.get("sentiment"), 0.0)
    st = _safe(row.get("shortterm"), 0.0)
    tech = _safe(row.get("technical"), 0.0)
    fundflow = _safe(row.get("fundflow"), 0.0)

    pos = _safe(row.get("position_20d"), 0.5)
    t10 = _safe(row.get("trailing_10_pit", row.get("trailing_10")), 0.0)
    pb20 = _safe(row.get("pullback_20d"), 0.0)
    pb10 = _safe(row.get("pullback_10d"), 0.0)
    std5 = _safe(row.get("pct_chg_std_5d"), 0.0)
    std10 = _safe(row.get("pct_chg_std_10d"), 0.0)
    gene20 = _safe(row.get("limit_up_count_20d"), 0.0)
    gene60 = _safe(row.get("limit_up_count_60d"), 0.0)
    amount_ratio = _safe(row.get("amount_ratio"), 1.0)
    avg_amount = _safe(row.get("avg_amount_5d"), 0.0)
    circ_mv = _safe(row.get("circ_mv"), 0.0)

    score = 0.0

    if sent >= 55:
        # 高情绪区：位置/趋势/回调/量能是主要矛盾
        if 0.40 <= pos <= 0.75:
            score += 16.0
        elif 0.25 <= pos <= 0.85:
            score += 8.0
        elif pos > 0.92:
            score -= 12.0

        if 0.05 <= t10 <= 0.25:
            score += 12.0
        elif 0.25 < t10 <= 0.40:
            score += 5.0
        elif t10 > 0.50:
            score -= 12.0

        if 0.03 <= pb20 <= 0.15:
            score += 10.0
        elif pb20 < 0.02:
            score -= 8.0

        score += min(gene20, 5) * 3.5 + min(max(gene60 - gene20, 0), 5) * 1.5

        if amount_ratio > 1.5 and avg_amount > 200_000:
            score += 7.0
        elif amount_ratio > 1.2 and avg_amount > 100_000:
            score += 4.0

        # 大市值+基因共振
        if circ_mv >= 100_0000 and gene20 >= 1:
            score += 6.0

    elif sent >= 35:
        # 中情绪区：shortterm 是正向，technical/fundflow 是陷阱
        score += min(st / 5.0, 12.0)  # shortterm 越高越好，上限 12

        score += min(gene20, 4) * 3.0

        if tech > 45:
            score -= 14.0
        elif tech > 35:
            score -= 7.0
        elif tech < 25:
            score += 5.0

        if fundflow > 50:
            score -= 6.0
        elif fundflow < 30:
            score += 3.0

        if 0.03 <= t10 <= 0.20:
            score += 6.0
        elif t10 > 0.35:
            score -= 7.0

        if 0.30 <= pos <= 0.70:
            score += 5.0

    else:
        # 低情绪区：整体难涨停，只保留高波动+涨停基因+回调
        if std5 > 3.5:
            score += 7.0
        if std10 > 4.0:
            score += 5.0
        if gene20 >= 2:
            score += 6.0
        if pb10 > 0.05:
            score += 4.0
        # 低情绪区涨停基因也很珍贵
        score += min(gene20, 3) * 2.0

    return round(max(0.0, score), 2)


def factor_balanced_adaptive_total_pit(row: pd.Series) -> float:
    """Balanced-自适应综合评分（PIT）：以 balanced_total_pit 为稳健基础，按 sentiment 区间做条件增强。

    真实扫描验证显示 balanced_total_pit 的 hit@3 优于纯 sentiment-adaptive，
    说明五维聚合 + 反追高惩罚是稳健基础。本因子保留该基础，只在不同 sentiment 区间做差异化增强：
    - 高情绪 (sentiment >= 55): 用 position_20d / amount_ratio 做二次精选。
    - 中情绪 (35 <= sentiment < 55): technical 高时减分，shortterm+涨停基因加分。
    - 低情绪 (sentiment < 35): 整体降权，仅保留高波动/涨停基因。
    """
    st = _safe(row.get("shortterm"), 0.0)
    tech = _safe(row.get("technical"), 0.0)
    sent = _safe(row.get("sentiment"), 0.0)
    fund = _safe(row.get("fundflow"), 0.0)
    funda = _safe(row.get("fundamental"), 0.0)

    pos = _safe(row.get("position_20d"), 0.5)
    t10 = _safe(row.get("trailing_10_pit", row.get("trailing_10")), 0.0)
    t5 = _safe(row.get("trailing_5_pit", row.get("trailing_5")), 0.0)
    pb10 = _safe(row.get("pullback_10d"), 0.0)
    std5 = _safe(row.get("pct_chg_std_5d"), 0.0)
    std10 = _safe(row.get("pct_chg_std_10d"), 0.0)
    gene20 = _safe(row.get("limit_up_count_20d"), 0.0)
    amount_ratio = _safe(row.get("amount_ratio"), 1.0)

    # 稳健基础：balanced_total_pit 公式
    score = sent * 0.40
    score += st * 0.30
    score += tech * 0.20
    score += fund * 0.05
    score += funda * 0.05

    # 按 sentiment 区间的条件增强
    if sent >= 55:
        # 高情绪区：position / amount 二次精选
        if 0.40 <= pos <= 0.75:
            score += 4.0
        elif pos > 0.92:
            score -= 4.0
        if amount_ratio > 1.5:
            score += 3.0
        if gene20 >= 2:
            score += 2.0
    elif sent >= 35:
        # 中情绪区：technical 是陷阱，shortterm/基因增强
        if tech > 45:
            score -= 4.0
        elif tech < 25:
            score += 2.0
        score += min(gene20, 3) * 1.5
    else:
        # 低情绪区：整体降权，仅保留波动/基因
        score *= 0.85
        if std5 > 3.5:
            score += 2.0
        if gene20 >= 2:
            score += 2.0

    # 追高惩罚（乘法，与 balanced_total_pit 保持一致）
    penalty = 1.0
    if t10 > 0.30:
        penalty *= 0.75
    elif t10 > 0.20:
        penalty *= 0.85
    elif t10 > 0.10:
        penalty *= 0.93
    if t5 > 0.15:
        penalty *= 0.90
    if pos > 0.85 and pb10 < 0.03:
        penalty *= 0.80
    if sent > 60 and t10 > 0.15:
        penalty *= 0.85

    return round(max(0.0, score * penalty), 2)


def factor_balanced_total_pit_v2(row: pd.Series) -> float:
    """均衡型 PIT 综合评分 v2：基于真实扫描验证的权重优化。

    真实扫描验证（2026-06）显示，shortterm 权重 0.6 + sentiment 0.2 + technical 0.2
    的 hit@3（43.9%）显著优于原 balanced_total_pit（37.9%）。
    追高惩罚与 v1 保持一致。
    """
    st = _safe(row.get("shortterm"), 0.0)
    tech = _safe(row.get("technical"), 0.0)
    sent = _safe(row.get("sentiment"), 0.0)
    fund = _safe(row.get("fundflow"), 0.0)
    funda = _safe(row.get("fundamental"), 0.0)

    t10 = _safe(row.get("trailing_10_pit", row.get("trailing_10")), 0.0)
    t5 = _safe(row.get("trailing_5_pit", row.get("trailing_5")), 0.0)
    pos = _safe(row.get("position_20d"), 0.5)
    pb10 = _safe(row.get("pullback_10d"), 0.0)

    score = st * 0.50
    score += sent * 0.20
    score += tech * 0.40
    score += fund * 0.0
    score += funda * 0.0

    penalty = 1.0
    if t10 > 0.30:
        penalty *= 0.75
    elif t10 > 0.20:
        penalty *= 0.85
    elif t10 > 0.10:
        penalty *= 0.93
    if t5 > 0.15:
        penalty *= 0.90
    if pos > 0.85 and pb10 < 0.03:
        penalty *= 0.80
    if sent > 60 and t10 > 0.15:
        penalty *= 0.85

    return round(max(0.0, score * penalty), 2)


# ═══════════════════════════════════════════════════════════
# 第十七类：数据挖掘新因子（2026-07）
# ═══════════════════════════════════════════════════════════

def factor_concept_turn_5d_max(row: pd.Series) -> float:
    """概念换手热度：最强概念近5日平均换手率。

    子因子挖掘 IC(hit_limit_3)=+0.33, chasing_score=+0.15，是情绪面最强单因子。
    """
    return _safe(row.get("cpt_turn_5d_max"), 0.0)


def factor_concept_heat_combo(row: pd.Series) -> float:
    """概念热度复合：最强概念换手 + 3日涨幅 + 概念数量。

    IC(hit_limit_3)=+0.33, 比单概念动量更稳健。
    """
    turn = _safe(row.get("cpt_turn_5d_max"), 0.0)
    ret3 = _safe(row.get("cpt_ret_3d_max"), 0.0)
    n_cpt = _safe(row.get("n_concepts"), 0.0)
    return turn * 0.5 + ret3 * 1.0 + n_cpt * 1.0


def factor_concept_activity_combo(row: pd.Series) -> float:
    """概念活跃度共振：概念动量 + 个股换手。

    hit@10=0.6, hit@20=0.65，短线爆发力最强。
    """
    ret3 = _safe(row.get("cpt_ret_3d_max"), 0.0)
    turn = _safe(row.get("cpt_turn_5d_max"), 0.0)
    turnover = _safe(row.get("turnover_rate"), 0.0)
    return ret3 * 0.8 + turn * 0.4 + turnover * 0.3


def factor_activity_combo(row: pd.Series) -> float:
    """个股活跃度复合：换手率 + 10日波动 + 5日均成交额。

    捕捉高活跃、高波动、有资金关注的标的。
    """
    turnover = _safe(row.get("turnover_rate"), 0.0)
    std10 = _safe(row.get("pct_chg_std_10d"), 0.0)
    avg_amount = _safe(row.get("avg_amount_5d"), 0.0)
    return turnover * 0.5 + std10 * 1.0 + (avg_amount / 200000.0)


def factor_large_cap_growth_combo(row: pd.Series) -> float:
    """大盘成长复合：流通市值 + PB + PE。

    基本面维度反转信号：大盘、高 PB/PE 成长股更易涨停。
    IC(hit_limit_3)=+0.14, IC(fwd_ret_3)=+0.13，胜率端贡献稳定。
    """
    circ_mv = _safe(row.get("circ_mv"), 0.0)
    pb = _safe(row.get("pb"), 0.0)
    pe = _safe(row.get("pe"), 0.0)

    score = 0.0
    mv_yi = circ_mv / 10000
    if mv_yi >= 500:
        score += 15
    elif mv_yi >= 200:
        score += 12
    elif mv_yi >= 100:
        score += 9
    elif mv_yi >= 50:
        score += 6
    elif mv_yi >= 20:
        score += 3

    if pb > 10:
        score += 8
    elif pb > 5:
        score += 5
    elif pb > 3:
        score += 2

    if pe > 50 or pe <= 0:
        score += 6
    elif pe > 30:
        score += 3

    return score


def factor_fundamental_rebuilt(row: pd.Series) -> float:
    """基本面重构评分：纠正原 fundamental 的方向错误。

    原策略奖励小盘+低估值，数据证明应奖励大盘+成长+概念丰富。
    """
    circ_mv = _safe(row.get("circ_mv"), 0.0)
    pb = _safe(row.get("pb"), 0.0)
    pe = _safe(row.get("pe"), 0.0)
    n_cpt = _safe(row.get("n_concepts"), 0.0)

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


def factor_fundflow_rebuilt(row: pd.Series) -> float:
    """资金面重构评分：纠正原 fundflow 的方向错误。

    原策略过度关注中单/小单净流入并惩罚主力，数据证明换手率+绝对成交额才是核心。
    """
    turnover = _safe(row.get("turnover_rate"), 0.0)
    turnover_f = _safe(row.get("turnover_rate_f"), 0.0)
    amount = _safe(row.get("avg_amount_5d"), 0.0)
    net_mf_ratio = _safe(row.get("net_mf_ratio"), 0.0)

    score = 0.0
    if turnover >= 15:
        score += 25
    elif turnover >= 10:
        score += 18
    elif turnover >= 5:
        score += 10
    elif turnover >= 2:
        score += 4

    if turnover_f >= 15:
        score += 10
    elif turnover_f >= 8:
        score += 5

    if amount >= 2_000_000:
        score += 10
    elif amount >= 500_000:
        score += 5

    if net_mf_ratio > 0.3:
        score += 5
    elif net_mf_ratio > 0.1:
        score += 2

    return min(80.0, score)


def factor_technical_rebuilt(row: pd.Series) -> float:
    """技术面重构评分：强化强信号，弱化负向信号。

    强化：换手率、波动率、位置、涨停基因、成交额。
    弱化：深度回调、连阴、长上影。
    """
    turnover = _safe(row.get("turnover_rate"), 0.0)
    std10 = _safe(row.get("pct_chg_std_10d"), 0.0)
    pos = _safe(row.get("position_20d"), 0.5)
    gene60 = _safe(row.get("limit_up_count_60d"), 0.0)
    gene20 = _safe(row.get("limit_up_count_20d"), 0.0)
    avg_amount = _safe(row.get("avg_amount_5d"), 0.0)
    pb10 = _safe(row.get("pullback_10d"), 0.0)
    upper_shadow = _safe(row.get("upper_shadow_pct"), 0.0)

    score = 0.0
    if turnover >= 10:
        score += 15
    elif turnover >= 5:
        score += 8

    if std10 >= 5:
        score += 15
    elif std10 >= 3.5:
        score += 8

    if 0.40 <= pos <= 0.80:
        score += 15
    elif 0.25 <= pos <= 0.90:
        score += 8

    score += min(gene60, 6) * 3.0
    score += min(gene20, 4) * 2.5

    if avg_amount >= 1_000_000:
        score += 10
    elif avg_amount >= 300_000:
        score += 5

    if pb10 > 0.15:
        score -= 10
    elif pb10 > 0.10:
        score -= 5
    if upper_shadow > 50:
        score -= 8

    return max(0.0, min(80.0, score))


def factor_fundflow_rebuilt_v2(row: pd.Series) -> float:
    """资金面重构评分 v2：弱化资金流向方向，强化换手+成交额+大市值共振。

    子因子 IC 显示 turnover_rate(0.23)、turnover_rate_f(0.23)、avg_amount_5d(0.20)
    是资金端最强信号；而 net_mf_ratio 仅 0.02。买卖各档金额均正相关，说明
    "大资金参与"本身才是核心，方向不重要。
    """
    turnover = _safe(row.get("turnover_rate"), 0.0)
    turnover_f = _safe(row.get("turnover_rate_f"), 0.0)
    amount = _safe(row.get("avg_amount_5d"), 0.0)
    net_mf_ratio = _safe(row.get("net_mf_ratio"), 0.0)
    circ_mv = _safe(row.get("circ_mv"), 0.0)
    t10 = _safe(row.get("trailing_10_pit", row.get("trailing_10")), 0.0)

    score = 0.0
    # 换手率（30分）
    if turnover >= 15:
        score += 30
    elif turnover >= 10:
        score += 24
    elif turnover >= 6:
        score += 16
    elif turnover >= 3:
        score += 8
    elif turnover >= 1:
        score += 2

    # 自由流通换手（10分）
    if turnover_f >= 15:
        score += 10
    elif turnover_f >= 8:
        score += 5

    # 成交额（25分）
    if amount >= 2_000_000:
        score += 25
    elif amount >= 1_000_000:
        score += 18
    elif amount >= 500_000:
        score += 10
    elif amount >= 200_000:
        score += 4

    # 大市值+高换手共振（10分）：大盘活跃股更容易连板
    mv_yi = circ_mv / 10000
    if mv_yi >= 100 and turnover >= 8:
        score += 10
    elif mv_yi >= 50 and turnover >= 6:
        score += 6
    elif mv_yi >= 20 and turnover >= 5:
        score += 3

    # 资金流向方向轻权重（5分）
    if net_mf_ratio > 0.3:
        score += 5
    elif net_mf_ratio > 0.1:
        score += 2
    elif net_mf_ratio < -0.3:
        score -= 3

    # 追涨惩罚
    if t10 > 0.30:
        score *= 0.85
    elif t10 > 0.20:
        score *= 0.92

    return round(max(0.0, min(80.0, score)), 2)


def factor_fundamental_rebuilt_v2(row: pd.Series) -> float:
    """基本面重构评分 v2：进一步弱化业绩，强化市值+成长估值+概念数量。

    子因子 IC 显示 circ_mv(0.12)/pb(0.14)/pe(0.13)/n_concepts(0.25) 正向，
    而 earnings_yield(-0.10)/book_yield(-0.14) 负向。业绩增速噪音大，降权。
    """
    circ_mv = _safe(row.get("circ_mv"), 0.0)
    pb = _safe(row.get("pb"), 0.0)
    pe = _safe(row.get("pe"), 0.0)
    n_cpt = _safe(row.get("n_concepts"), 0.0)

    score = 0.0
    mv_yi = circ_mv / 10000

    # 市值（35分）
    if mv_yi >= 200:
        score += 35
    elif mv_yi >= 100:
        score += 28
    elif mv_yi >= 50:
        score += 20
    elif mv_yi >= 20:
        score += 10
    elif mv_yi >= 10:
        score += 4

    # 成长估值（30分）：高 PB/PE 作为成长/题材溢价
    growth = 0.0
    if pb > 8:
        growth += 18
    elif pb > 5:
        growth += 12
    elif pb > 3:
        growth += 5

    if pe > 50 or pe <= 0:
        growth += 12
    elif pe > 30:
        growth += 6
    score += min(30.0, growth)

    # 概念广度（25分）
    if n_cpt >= 8:
        score += 25
    elif n_cpt >= 5:
        score += 18
    elif n_cpt >= 3:
        score += 10
    elif n_cpt >= 1:
        score += 3

    # 自由流通股（5分）：流通盘小弹性大
    free_share = _safe(row.get("free_share"), 0.0)
    if 0 < free_share < 5_0000:  # 5亿股以下
        score += 5
    elif 0 < free_share < 10_0000:
        score += 2

    return round(min(80.0, score), 2)


def factor_technical_rebuilt_v2(row: pd.Series) -> float:
    """技术面重构评分 v2：在 technical_rebuilt 基础上降低追涨，加入位置/回调过滤。

    technical_rebuilt IC=0.25 但 chasing_score=0.64，主因过度奖励 5 日动量和均线。
    子因子 IC 显示 position_20d(0.21) 强正、pullback_10d(-0.11)/pullback_20d(-0.15)
    负向，应奖励中等位置+有回调蓄力，惩罚高位追高。

    v2.1 调整：降低 position 权重（position 与 trailing_10 高相关导致追涨），
    把权重挪给换手/波动/涨停基因/成交额等低追涨因子。
    """
    turnover = _safe(row.get("turnover_rate"), 0.0)
    std10 = _safe(row.get("pct_chg_std_10d"), 0.0)
    pos = _safe(row.get("position_20d"), 0.5)
    gene60 = _safe(row.get("limit_up_count_60d"), 0.0)
    gene20 = _safe(row.get("limit_up_count_20d"), 0.0)
    avg_amount = _safe(row.get("avg_amount_5d"), 0.0)
    pb10 = _safe(row.get("pullback_10d"), 0.0)
    pb20 = _safe(row.get("pullback_20d"), 0.0)
    t10 = _safe(row.get("trailing_10_pit", row.get("trailing_10")), 0.0)
    upper_shadow = _safe(row.get("upper_shadow_pct"), 0.0)

    score = 0.0

    # 换手（22分）
    if turnover >= 15:
        score += 22
    elif turnover >= 10:
        score += 17
    elif turnover >= 6:
        score += 11
    elif turnover >= 3:
        score += 5
    elif turnover >= 1.5:
        score += 2

    # 波动率（18分）
    if std10 >= 5:
        score += 18
    elif std10 >= 3.5:
        score += 12
    elif std10 >= 2.5:
        score += 5

    # 涨停基因（22分）
    score += min(gene60, 6) * 3.0
    score += min(gene20, 4) * 2.5

    # 成交额（12分）
    if avg_amount >= 1_000_000:
        score += 12
    elif avg_amount >= 500_000:
        score += 7
    elif avg_amount >= 300_000:
        score += 3

    # 位置（6分）：仅在中等位置区间给分，避免高位追涨
    if 0.35 <= pos <= 0.65:
        score += 6
    elif 0.25 <= pos <= 0.75:
        score += 2

    # 回调蓄力（8分）：低 pullback 说明有调整、不是纯追高
    if pb10 > 0.10:
        score += 6
    elif pb10 > 0.05:
        score += 3
    elif pb20 > 0.12:
        score += 3

    # 上影线惩罚
    if upper_shadow > 60:
        score -= 10
    elif upper_shadow > 40:
        score -= 5

    # 追高惩罚（乘法）：比 v1 更克制
    penalty = 1.0
    if t10 > 0.35:
        penalty *= 0.75
    elif t10 > 0.25:
        penalty *= 0.85
    elif t10 > 0.15:
        penalty *= 0.93
    if pos > 0.85 and pb10 < 0.03:
        penalty *= 0.80

    return round(max(0.0, min(80.0, score * penalty)), 2)


# ═══════════════════════════════════════════════════════════
# 第十八类：深度挖掘新因子（2026-07-02）
# ═══════════════════════════════════════════════════════════

def factor_cpt_amount_combo(row: pd.Series) -> float:
    """概念换手 + 成交额组合：当前最强单因子组合之一。

    子因子挖掘：cpt_turn_5d_max(IC=0.33) 与 avg_amount_5d(IC=0.20) 组合后
    IC(hit_limit_3)=+0.326, chasing=+0.275, 显著提升且追涨可控。
    """
    cpt_turn = _safe(row.get("cpt_turn_5d_max"), 0.0)
    avg_amount = _safe(row.get("avg_amount_5d"), 0.0)
    return round(cpt_turn + avg_amount / 160_000.0, 2)


def factor_cpt_amount_percentile(row: pd.Series) -> float:
    """概念换手 + 成交额（百分位阈值版）：IC 更高的稳健版本。

    基于 panel_enriched_pit_v4 分布硬编码阈值：
      - cpt_turn_5d_max >=14.6(≈top20%) → +15, >=12.2(≈top40%) → +8
      - avg_amount_5d   >=3007k(≈top20%) → +12, >=1005k(≈top40%) → +6
    验证：IC(hit_limit_3)=+0.336, chasing=+0.274, hit@20=0.40。
    """
    score = 0.0
    cpt_turn = _safe(row.get("cpt_turn_5d_max"), 0.0)
    avg_amount = _safe(row.get("avg_amount_5d"), 0.0)

    if cpt_turn >= 14.6:
        score += 15.0
    elif cpt_turn >= 12.2:
        score += 8.0

    if avg_amount >= 3_007_000:
        score += 12.0
    elif avg_amount >= 1_005_000:
        score += 6.0

    return round(score, 2)


def factor_cpt_amount_anti_chase(row: pd.Series) -> float:
    """概念换手 + 成交额组合（反追高版）。

    在 cpt_amount_combo 基础上加入 trailing_10 惩罚，IC 基本持平，
    但显著降低追涨风险。
    """
    score = factor_cpt_amount_combo(row)
    t10 = _safe(row.get("trailing_10_pit", row.get("trailing_10")), 0.0)
    if t10 > 0.30:
        score *= 0.75
    elif t10 > 0.20:
        score *= 0.85
    elif t10 > 0.10:
        score *= 0.95
    return round(max(0.0, score), 2)


def factor_cpt_streak_turn(row: pd.Series) -> float:
    """概念连涨 + 个股换手：捕捉概念发酵中个股接力信号。

    IC(hit_limit_3)=+0.267, hit@20=0.55。
    """
    cpt_streak = _safe(row.get("cpt_up_streak_max"), 0.0)
    turnover = _safe(row.get("turnover_rate"), 0.0)
    return round(cpt_streak * 2.0 + turnover * 0.5, 2)


def factor_mv_turn_cpt(row: pd.Series) -> float:
    """大市值 + 高换手 + 概念热：大盘活跃股的起爆信号。

    IC(hit_limit_3)=+0.283, chasing=+0.345。
    """
    circ_mv = _safe(row.get("circ_mv"), 0.0)
    turnover = _safe(row.get("turnover_rate"), 0.0)
    cpt_turn = _safe(row.get("cpt_turn_5d_max"), 0.0)
    # circ_mv 单位万元，转换为亿元量级
    return round(circ_mv / 100_0000.0 * 0.3 + turnover * 0.4 + cpt_turn * 0.6, 2)


def factor_residual_shortterm(row: pd.Series) -> float:
    """shortterm 对 trailing_10 的残差：去除追涨成分后的短线信号。

    单独 IC 仅 0.16，但 chasing 接近 0，适合作为综合分的稳定器。
    实现上用 shortterm * (1 - trailing_10) 近似残差效果。
    """
    st = _safe(row.get("shortterm"), 0.0)
    t10 = _safe(row.get("trailing_10_pit", row.get("trailing_10")), 0.0)
    return round(st * max(0.0, 1.0 - t10 * 2.0), 2)


def factor_residual_technical(row: pd.Series) -> float:
    """technical 对 trailing_10 的残差近似。"""
    tech = _safe(row.get("technical"), 0.0)
    t10 = _safe(row.get("trailing_10_pit", row.get("trailing_10")), 0.0)
    return round(tech * max(0.0, 1.0 - t10 * 2.0), 2)


def factor_balanced_ensemble(row: pd.Series) -> float:
    """平衡 ensemble：高 IC + 低追涨的折中方案。

    组合：cpt_amount_percentile + residual_shortterm + cpt_streak_turn
    """
    return round(
        factor_cpt_amount_percentile(row) * 0.5 +
        factor_residual_shortterm(row) * 0.3 +
        factor_cpt_streak_turn(row) * 0.3,
        2
    )


def factor_ultimate_total_v3(row: pd.Series) -> float:
    """终极综合评分 v3：融入深度挖掘出的低追涨组合。

    权重：cpt_amount_percentile(1.0) + residual_shortterm(0.6) + cpt_streak_turn(0.5) + mv_turn_cpt(0.3)
    """
    score = (
        factor_cpt_amount_percentile(row) * 1.0 +
        factor_residual_shortterm(row) * 0.6 +
        factor_cpt_streak_turn(row) * 0.5 +
        factor_mv_turn_cpt(row) * 0.3
    )
    return round(max(0.0, score), 2)


def factor_ultimate_total_v4(row: pd.Series) -> float:
    """终极综合评分 v4：极低追涨版本。

    以残差因子和低追涨组合为主，chasing 接近 0。
    """
    score = (
        factor_cpt_amount_anti_chase(row) * 1.0 +
        factor_residual_shortterm(row) * 0.8 +
        factor_residual_technical(row) * 0.5 +
        _safe(row.get("cpt_turn_5d_max"), 0.0) * 0.3
    )
    return round(max(0.0, score), 2)


def factor_new_total_mined_v2(row: pd.Series) -> float:
    """数据挖掘综合评分 v2：概念热度 + 个股活跃度 + 涨停基因 + 追高惩罚。

    在挖掘结果中 IC(hit_limit_3)=+0.27, chasing_score=+0.40，比原 total 大幅提升。
    """
    sent = _safe(row.get("sentiment"), 0.0)
    st = _safe(row.get("shortterm"), 0.0)

    t10 = _safe(row.get("trailing_10_pit", row.get("trailing_10")), 0.0)
    t5 = _safe(row.get("trailing_5_pit", row.get("trailing_5")), 0.0)
    pos = _safe(row.get("position_20d"), 0.5)
    pb10 = _safe(row.get("pullback_10d"), 0.1)

    score = sent * 0.40
    score += st * 0.40
    score += factor_concept_heat_combo(row) * 0.8
    score += factor_activity_combo(row) * 0.5
    score += factor_limit_up_gene_composite(row) * 0.4

    penalty = 1.0
    if t10 > 0.30:
        penalty *= 0.75
    elif t10 > 0.20:
        penalty *= 0.85
    elif t10 > 0.10:
        penalty *= 0.93
    if t5 > 0.15:
        penalty *= 0.90
    if pos > 0.85 and pb10 < 0.03:
        penalty *= 0.80

    return round(max(0.0, score * penalty), 2)


def factor_ultimate_total_v1(row: pd.Series) -> float:
    """终极综合评分 v1：网格搜索最优权重。

    权重: concept_turn_5d_max=1.0, activity_combo=0.3, limit_up_gene_composite=0.8
    IC(hit_limit_3)=+0.306, chasing_score=+0.234
    """
    score = (
        factor_concept_turn_5d_max(row) * 1.0 +
        factor_activity_combo(row) * 0.3 +
        factor_limit_up_gene_composite(row) * 0.8
    )

    t10 = _safe(row.get("trailing_10_pit", row.get("trailing_10")), 0.0)
    t5 = _safe(row.get("trailing_5_pit", row.get("trailing_5")), 0.0)
    pos = _safe(row.get("position_20d"), 0.5)
    pb10 = _safe(row.get("pullback_10d"), 0.1)

    penalty = 1.0
    if t10 > 0.30:
        penalty *= 0.75
    elif t10 > 0.20:
        penalty *= 0.85
    elif t10 > 0.10:
        penalty *= 0.93
    if t5 > 0.15:
        penalty *= 0.90
    if pos > 0.85 and pb10 < 0.03:
        penalty *= 0.80

    return round(max(0.0, score * penalty), 2)


def factor_ultimate_total_v2(row: pd.Series) -> float:
    """终极综合评分 v2：兼顾 IC 与 chasing_score 的稳健版本。

    权重: concept_turn_5d_max=1.0, activity_combo=0.3, limit_up_gene_composite=0.4
    IC(hit_limit_3)=+0.305, chasing_score=+0.194
    """
    score = (
        factor_concept_turn_5d_max(row) * 1.0 +
        factor_activity_combo(row) * 0.3 +
        factor_limit_up_gene_composite(row) * 0.4
    )

    t10 = _safe(row.get("trailing_10_pit", row.get("trailing_10")), 0.0)
    t5 = _safe(row.get("trailing_5_pit", row.get("trailing_5")), 0.0)
    pos = _safe(row.get("position_20d"), 0.5)
    pb10 = _safe(row.get("pullback_10d"), 0.1)

    penalty = 1.0
    if t10 > 0.30:
        penalty *= 0.75
    elif t10 > 0.20:
        penalty *= 0.85
    elif t10 > 0.10:
        penalty *= 0.93
    if t5 > 0.15:
        penalty *= 0.90
    if pos > 0.85 and pb10 < 0.03:
        penalty *= 0.80

    return round(max(0.0, score * penalty), 2)


# ═══════════════════════════════════════════════════════════
# 因子注册表
# ═══════════════════════════════════════════════════════════

# 独立因子（可直接作为 score）
STANDALONE_FACTORS = {
    "limit_up_gene_20d": factor_limit_up_gene_20d,
    "limit_up_gene_60d": factor_limit_up_gene_60d,
    "limit_up_gene_composite": factor_limit_up_gene_composite,
    "pullback_quality": factor_pullback_quality,
    "position_optimal": factor_position_optimal,
    "pullback_from_peak": factor_pullback_from_peak,
    "vol_expansion_quality": factor_vol_expansion_quality,
    "amount_acceleration": factor_amount_acceleration,
    "amount_surge": factor_amount_surge,
    "reversal_signal": factor_reversal_signal,
    "gap_up_quality": factor_gap_up_quality,
    "consecutive_strength": factor_consecutive_strength,
    "volatility_contraction": factor_volatility_contraction,
    "low_amplitude_breakout": factor_low_amplitude_breakout,
    "upper_shadow_risk": factor_upper_shadow_risk,
    "large_amplitude_risk": factor_large_amplitude_risk,
    "dimension_divergence": factor_dimension_divergence,
    "sentiment_contrarian": factor_sentiment_contrarian,
    "total_quality_bonus": factor_total_quality_bonus,
    "new_total_v2": factor_new_total_v2,
    "balanced_total": factor_balanced_total,
    "balanced_total_pit": factor_balanced_total_pit,
    "aggressive_total": factor_aggressive_total,
    "return_optimized_total": factor_return_optimized_total,
    "quality_value_total": factor_quality_value_total,
    "circ_mv_tier": factor_circ_mv_tier,
    "fundamental_quality": factor_fundamental_quality,
    "turnover_penalty": factor_turnover_penalty,
    "volume_ratio_penalty": factor_volume_ratio_penalty,
    "net_mf_signal": factor_net_mf_signal,
    "elg_inflow_signal": factor_elg_inflow_signal,
    # 第十三/十四类：概念动量 + 龙虎榜机构
    "concept_momentum": factor_concept_momentum,
    "concept_up_streak": factor_concept_up_streak,
    "concept_turnover": factor_concept_turnover,
    "inst_following": factor_inst_following,
    "top_list_quality": factor_top_list_quality,
    "inst_consistency": factor_inst_consistency,
    # 第十五类：PIT 数据挖掘新因子
    "amount_power_pit": factor_amount_power_pit,
    "volatility_activation_pit": factor_volatility_activation_pit,
    "limit_gene_momentum_pit": factor_limit_gene_momentum_pit,
    "breakout_quality_pit": factor_breakout_quality_pit,
    "trailing_momentum_pit": factor_trailing_momentum_pit,
    "intraday_strength_pit": factor_intraday_strength_pit,
    "large_cap_limit_gene_pit": factor_large_cap_limit_gene_pit,
    "turnover_momentum_pit": factor_turnover_momentum_pit,
    "growth_momentum_pit": factor_growth_momentum_pit,
    # 第十六类：sentiment-自适应综合评分
    "sentiment_adaptive_total_pit": factor_sentiment_adaptive_total_pit,
    "sentiment_conditional_pit": factor_sentiment_conditional_pit,
    "balanced_adaptive_total_pit": factor_balanced_adaptive_total_pit,
    "balanced_total_pit_v2": factor_balanced_total_pit_v2,
    # 第十七类：数据挖掘新因子（2026-07）
    "concept_turn_5d_max": factor_concept_turn_5d_max,
    "concept_heat_combo": factor_concept_heat_combo,
    "concept_activity_combo": factor_concept_activity_combo,
    "activity_combo": factor_activity_combo,
    "large_cap_growth_combo": factor_large_cap_growth_combo,
    "fundamental_rebuilt": factor_fundamental_rebuilt,
    "fundflow_rebuilt": factor_fundflow_rebuilt,
    "technical_rebuilt": factor_technical_rebuilt,
    "new_total_mined_v2": factor_new_total_mined_v2,
    "ultimate_total_v1": factor_ultimate_total_v1,
    "ultimate_total_v2": factor_ultimate_total_v2,
}

# 调整因子（加到现有维度分上）
ADJUSTMENT_FACTORS = {
    "technical_anti_chasing": factor_technical_anti_chasing,
    "shortterm_anti_chasing": factor_shortterm_anti_chasing,
    "sentiment_anti_chasing": factor_sentiment_anti_chasing,
}

# 护栏因子（guardrail 模式）
GUARDRAIL_FACTORS = {
    "chasing_guardrail_v2": factor_chasing_guardrail_v2,
}

# 全部因子
ALL_FACTORS = {
    **STANDALONE_FACTORS,
    **ADJUSTMENT_FACTORS,
}

# 向后兼容（旧版引用）
FACTORS = ALL_FACTORS
