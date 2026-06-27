"""标签计算 — 纯函数，不依赖 tushare / .env，可独立单测。

对每条 (code, score_date) 评分记录，基于该股票的日线序列计算：
- 未来收益（forward）：评分日之后 N 个交易日的收益，用于验证预测力
- 追高识别（trailing）：评分日之前 N 个交易日的涨幅，用于识别"已经涨高了才推荐"
- 是否命中涨停（hit_limit）：评分日之后 N 日内是否出现涨停

约定：传入的 daily 序列必须按 trade_date 升序，且包含评分日当天。
所有"未来"标签严格使用评分日之后的数据，杜绝前视偏差。
"""

from __future__ import annotations

LIMIT_PCT = 9.8  # 主板涨停近似阈值（pct_chg >= 9.8 视为涨停）


def _idx_of(dates: list[str], score_date: str) -> int:
    """返回 score_date 在升序日期序列中的位置；找不到返回 -1。"""
    try:
        return dates.index(score_date)
    except ValueError:
        return -1


def forward_return(closes: list[float], i: int, n: int) -> float | None:
    """评分日收盘 close[i] 到 close[i+n] 的累计收益率。数据不足返回 None。"""
    if i < 0 or i + n >= len(closes):
        return None
    base = closes[i]
    if base is None or base <= 0:
        return None
    fut = closes[i + n]
    if fut is None:
        return None
    return fut / base - 1.0


def forward_max_return(highs: list[float], closes: list[float], i: int, n: int) -> float | None:
    """评分日收盘到未来 1..n 日内最高价的最大涨幅（打板可达收益）。"""
    if i < 0 or i + n >= len(highs):
        return None
    base = closes[i]
    if base is None or base <= 0:
        return None
    window = [h for h in highs[i + 1: i + n + 1] if h is not None]
    if not window:
        return None
    return max(window) / base - 1.0


def hit_limit(pct_chgs: list[float], i: int, n: int, thr: float = LIMIT_PCT) -> int | None:
    """未来 1..n 日内是否出现涨停（pct_chg >= thr）。1/0；数据不足返回 None。"""
    if i < 0 or i + n >= len(pct_chgs):
        return None
    window = pct_chgs[i + 1: i + n + 1]
    window = [p for p in window if p is not None]
    if not window:
        return None
    return int(any(p >= thr for p in window))


def trailing_return(closes: list[float], i: int, n: int) -> float | None:
    """评分日之前 n 个交易日的累计涨幅：close[i] / close[i-n] - 1。

    用于追高识别：值越大说明评分时股票已涨越多。
    注意 close[i] 是评分日当天收盘，属于已知信息（盘后/盘中均已发生），
    不构成前视偏差。
    """
    if i - n < 0 or i >= len(closes):
        return None
    base = closes[i - n]
    if base is None or base <= 0:
        return None
    cur = closes[i]
    if cur is None:
        return None
    return cur / base - 1.0


def compute_labels(
    dates: list[str],
    closes: list[float],
    highs: list[float],
    pct_chgs: list[float],
    score_date: str,
) -> dict:
    """对单只股票在 score_date 计算全部标签。

    返回 dict，缺失项为 None。调用方负责传入升序对齐的四个序列。
    """
    i = _idx_of(dates, score_date)
    return {
        "fwd_ret_1": forward_return(closes, i, 1),
        "fwd_ret_3": forward_return(closes, i, 3),
        "fwd_max_3": forward_max_return(highs, closes, i, 3),
        "hit_limit_3": hit_limit(pct_chgs, i, 3),
        "trailing_5": trailing_return(closes, i, 5),
        "trailing_10": trailing_return(closes, i, 10),
        "_aligned": i >= 0,
    }
