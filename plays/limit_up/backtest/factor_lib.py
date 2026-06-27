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
    gap = _safe(row.get("gap_up"))
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
    t10 = _safe(row.get("trailing_10"))
    t5 = _safe(row.get("trailing_5"))
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
    t10 = _safe(row.get("trailing_10"))
    t5 = _safe(row.get("trailing_5"))
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
    t10 = _safe(row.get("trailing_10"))
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
    t10 = _safe(row.get("trailing_10"))
    t5 = _safe(row.get("trailing_5"))
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
    t10 = _safe(row.get("trailing_10"))
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
    t10 = _safe(row.get("trailing_10"))
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
    t10 = _safe(row.get("trailing_10"))
    t5 = _safe(row.get("trailing_5"))

    if t10 > 0.30 and t5 > 0.15:
        return -15.0
    if t10 > 0.25:
        return -10.0
    if t10 > 0.15:
        return -5.0
    return 0.0


def factor_sentiment_anti_chasing(row: pd.Series) -> float:
    """情绪维度反追高：高情绪+高位置=最危险。"""
    t10 = _safe(row.get("trailing_10"))
    sent = _safe(row.get("sentiment"))

    if sent > 60 and t10 > 0.15:
        return -15.0  # 情绪高+已涨高=最可能见顶
    if sent > 50 and t10 > 0.10:
        return -8.0
    return 0.0


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
    "aggressive_total": factor_aggressive_total,
    "return_optimized_total": factor_return_optimized_total,
    "quality_value_total": factor_quality_value_total,
    "circ_mv_tier": factor_circ_mv_tier,
    "fundamental_quality": factor_fundamental_quality,
    "turnover_penalty": factor_turnover_penalty,
    "volume_ratio_penalty": factor_volume_ratio_penalty,
    "net_mf_signal": factor_net_mf_signal,
    "elg_inflow_signal": factor_elg_inflow_signal,
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
