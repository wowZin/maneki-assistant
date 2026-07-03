"""质量组合因子 v3：从历史高胜率样本中挖掘出的高置信共振规则。

v2 的问题：通过拉高考核阈值减少推送，本质上是在“过滤”，导致真实命中率/胜率始终低于 50%。
v3 改为从历史涨停优质股中提炼两条高置信规则：

- 100 分档：极限动量 + 高维度分（高换手、技术面/短线分双高、10 日动量不过高）。
- 95  分档：涨停基因 + 高换手 + 温和动量 + 不追高 + 技术面/短线分优秀。

只有同时满足严格条件时才给高分，从而把 2026-06-30 那种“高位置+高情绪+换手不足”的追高失败案例直接打 0 分。
推送阈值对应提升至 95，确保只有高置信档才出库。
"""

from __future__ import annotations

from plays.limit_up.factors._helpers import safe


def factor_quality_combo(row) -> float:
    """返回 0/95/100 的高置信质量分。

    依赖列：turnover_rate, trailing_10, position_20d, pct_chg_score_day,
           technical, shortterm, fundflow, limit_up_count_20d。
    """
    turnover = safe(row.get("turnover_rate"), 5.0)
    t10 = safe(row.get("trailing_10"), 0.0)
    pos = safe(row.get("position_20d"), 0.5)
    pc = safe(row.get("pct_chg_score_day"), 0.0)
    tech = safe(row.get("technical"), 0.0)
    st = safe(row.get("shortterm"), 0.0)
    fundflow = safe(row.get("fundflow"), 0.0)
    lg20 = safe(row.get("limit_up_count_20d"), 0.0)

    # 资金维度是区分高置信机会与追高失败的最强过滤；资金分不足直接 0 分
    if fundflow < 10.0:
        return 0.0

    # 100 分档：极限活跃 + 维度双高 + 10 日动量不过高
    if (
        turnover >= 18.0
        and t10 <= 0.20
        and tech >= 30.0
        and st >= 30.0
    ):
        return 100.0

    # 95 分档：涨停基因 + 高换手 + 温和动量 + 不追高 + 维度优秀
    if (
        turnover >= 12.0
        and 0.05 <= t10 <= 0.20
        and pos <= 0.70
        and 0.0 <= pc <= 5.0
        and tech >= 30.0
        and st >= 25.0
        and lg20 >= 2.0
    ):
        return 95.0

    return 0.0
