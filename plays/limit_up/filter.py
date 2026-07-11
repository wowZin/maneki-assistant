"""
实时过滤 — 只保留实时可判断的规则。

静态规则（ST/次新/板块/市值）已移到 pool_builder.py 的候选池构建中。
只保留：
  1. 一字板判断（依赖实时行情）
"""

from __future__ import annotations

from typing import Any


def filter_realtime(quote: dict[str, Any]) -> tuple[bool, str]:
    """实时过滤。

    Args:
        quote: get_batch_quotes 返回的单只股票行情

    Returns:
        (是否被排除, 排除理由)
    """
    try:
        pct = float(quote.get("pct_chg", 0) or 0)
        turnover = float(quote.get("turnover", 0) or 0)
        limit_up_price = float(quote.get("limit_up", 0) or 0)
        price = float(quote.get("price", 0) or 0)

        # 规则7: 一字板（涨停但无人卖出）
        if pct >= 9.5 and limit_up_price > 0 and price >= limit_up_price:
            if turnover < 0.5:
                return True, "一字板涨停(换手<0.5%)"

        limit_down_price = float(quote.get("limit_down", 0) or 0)
        if pct <= -9.5 and limit_down_price > 0 and price <= limit_down_price:
            if turnover < 0.5:
                return True, "一字跌停(换手<0.5%)"

    except (ValueError, TypeError, KeyError):
        pass

    return False, ""


# 向后兼容：旧 pipeline 会调用 filter_candidates 做静态过滤。
# 这些规则已迁移到 pool_builder.py（候选池构建阶段），
# 因此本函数不再重复实现。如需静态过滤，请使用 pool_builder.ensure_pool()。
def filter_candidates(candidates: list[dict]) -> list[dict]:
    """保持旧接口兼容，但仅做恒等返回。

    静态过滤（ST/次新/板块/市值）已在候选池构建时完成。
    """
    return candidates
