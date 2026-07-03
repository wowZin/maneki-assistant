"""质量门因子：基于训练集提炼的涨停高胜率组合。

核心假设（PIT，与生产 pipeline 的 T-1 特征对齐）：
- 高换手（turnover_rate）是涨停的最强单信号之一；
- 涨停基因（limit_up_count_20d）代表股性；
- 10 日累涨 trailing_10 需要在“有 momentum 但不过分追高”区间；
- 20 日位置 position_20d 不能过高；
- 技术面 technical 需至少中等偏上。

该因子采用“阶梯式”评分：只有同时满足多个严格条件时才给高分，
天然过滤掉 2026-06-30 那种“高位置+高情绪+换手不足”的追高失败案例。
"""

from __future__ import annotations

from plays.limit_up.factors._helpers import safe


def factor_quality_gate(row) -> float:
    """返回 0-100 的质量门分数；高分仅当 PIT 条件同时满足。

    依赖列：limit_up_count_20d, turnover_rate, trailing_10,
           position_20d, technical, pct_chg_score_day。
    """
    lg20 = safe(row.get("limit_up_count_20d"), 0.0)
    turnover = safe(row.get("turnover_rate"), 5.0)
    t10 = safe(row.get("trailing_10"), 0.0)
    pos = safe(row.get("position_20d"), 0.5)
    tech = safe(row.get("technical"), 0.0)
    pc = safe(row.get("pct_chg_score_day"), 0.0)

    # 10 日动量必须在“有趋势但非暴涨”区间；否则直接 0 分
    if not (0.05 <= t10 <= 0.30):
        return 0.0

    # 阶梯条件：越严格分数越高
    if (
        lg20 >= 1
        and turnover >= 18.0
        and 0.10 <= t10 <= 0.20
        and pos <= 0.90
        and tech >= 30.0
    ):
        score = 100.0
    elif (
        lg20 >= 1
        and turnover >= 15.0
        and 0.10 <= t10 <= 0.25
        and pos <= 0.90
        and tech >= 30.0
    ):
        score = 80.0
    elif (
        lg20 >= 1
        and turnover >= 12.0
        and 0.05 <= t10 <= 0.30
        and pos <= 0.85
        and tech >= 30.0
    ):
        score = 60.0
    else:
        return 0.0

    # 反追高护栏（乘性）
    if t10 > 0.25:
        score *= 0.85
    if pos > 0.90:
        score *= 0.75
    if pc > 6.0 and pos > 0.75:
        score *= 0.85

    return round(max(0.0, score), 2)
