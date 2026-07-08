"""
盯盘通用技术指标与实时字段构造
================================

v2 不再使用 KAMA/ADX/布林带/RSI 组合，只保留最基础的：
- SMA/EMA
- ATR
- 实时字段构造（涨幅、缺口、量比、换手、回撤、位置）

实时数据源：scripts.jvquant_ws_client（L2 守护进程）
日线数据源：Tushare daily / daily_basic / limit_list_d
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def sma(series: np.ndarray, period: int) -> np.ndarray:
    """简单移动平均"""
    series = np.ascontiguousarray(series, dtype=np.float64)
    result = np.full_like(series, np.nan, dtype=float)
    if len(series) < period:
        return result
    cumsum = np.cumsum(np.insert(series, 0, 0))
    result[period - 1:] = (cumsum[period:] - cumsum[:-period]) / period
    return result


def ema(series: np.ndarray, period: int) -> np.ndarray:
    """指数移动平均"""
    series = np.ascontiguousarray(series, dtype=np.float64)
    result = np.full(len(series), np.nan)
    start = 0
    while start < len(series) and np.isnan(series[start]):
        start += 1
    if start + period > len(series):
        return result
    result[start + period - 1] = np.mean(series[start:start + period])
    alpha = 2 / (period + 1)
    for i in range(start + period, len(series)):
        result[i] = alpha * series[i] + (1 - alpha) * result[i - 1]
    return result


def atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 20) -> np.ndarray:
    """平均真实波幅"""
    n = len(close)
    result = np.full(n, np.nan)
    tr = np.zeros(n)
    for i in range(1, n):
        tr[i] = max(
            high[i] - low[i],
            abs(high[i] - close[i - 1]),
            abs(low[i] - close[i - 1]),
        )
    result[period] = np.mean(tr[1:period + 1])
    for i in range(period + 1, n):
        result[i] = (result[i - 1] * (period - 1) + tr[i]) / period
    return result


def rolling_std(series: np.ndarray, period: int) -> np.ndarray:
    """滚动标准差"""
    s = pd.Series(series)
    return s.rolling(window=period, min_periods=period).std(ddof=0).to_numpy()


def price_features(daily_rows: list[dict]) -> dict:
    """从日线序列提取盯盘所需的背景特征。

    daily_rows: 按 trade_date 升序排列的 dict 列表，字段含
                open/high/low/close/pre_close/pct_chg/vol/amount
    返回字段与 plays.limit_up.pipeline._extract_pit_features 对齐，
    便于直接传入 limit_up 因子函数。
    """
    if not daily_rows or len(daily_rows) < 20:
        return {
            "trailing_10": 0.0, "trailing_5": 0.0,
            "position_20d": 0.5, "pullback_10d": 0.1, "pullback_20d": 0.1,
            "pct_chg_std_10d": 0.0, "pct_chg_std_5d": 0.0, "max_pct_chg_5d": 0.0,
            "limit_up_count_20d": 0.0, "limit_up_count_60d": 0.0,
        }

    df = pd.DataFrame(daily_rows)
    for col in ["open", "high", "low", "close", "pre_close", "pct_chg", "vol", "amount"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    closes = df["close"].to_numpy()
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    pcts = df["pct_chg"].fillna(0).to_numpy()
    amounts = df["amount"].fillna(0).to_numpy() * 1000  # 千元 -> 元

    last = len(df) - 1

    def _trailing(days: int) -> float:
        if last - days < 0:
            return 0.0
        return float(closes[last] / closes[last - days] - 1.0) if closes[last - days] else 0.0

    def _pullback(days: int) -> float:
        start = max(0, last - days + 1)
        hmax = float(np.nanmax(highs[start:last + 1]))
        c0 = float(closes[last])
        if hmax > 0:
            return max(0.0, (hmax - c0) / hmax)
        return 0.1

    h20 = float(np.nanmax(highs[last - 19:last + 1]))
    l20 = float(np.nanmin(lows[last - 19:last + 1]))
    c0 = float(closes[last])
    position_20d = (c0 - l20) / (h20 - l20) if h20 > l20 else 0.5

    feats = {
        "trailing_10": _trailing(10),
        "trailing_5": _trailing(5),
        "position_20d": position_20d,
        "pullback_10d": _pullback(10),
        "pullback_20d": _pullback(20),
        "pct_chg_std_10d": float(np.std(pcts[last - 9:last + 1], ddof=0)) if len(pcts) >= 10 else 0.0,
        "pct_chg_std_5d": float(np.std(pcts[last - 4:last + 1], ddof=0)) if len(pcts) >= 5 else 0.0,
        "max_pct_chg_5d": float(np.nanmax(pcts[last - 4:last + 1])) if len(pcts) >= 5 else 0.0,
        "avg_amount_5d": float(np.nanmean(amounts[last - 4:last + 1])) if len(amounts) >= 5 else 0.0,
    }

    # 涨停基因
    feats["limit_up_count_20d"] = float(np.sum(pcts[last - 19:last + 1] >= 9.8))
    start60 = max(0, last - 59)
    feats["limit_up_count_60d"] = float(np.sum(pcts[start60:last + 1] >= 9.8))

    # 连阳天数 + 5日平均涨幅（供 consecutive_strength 因子使用）
    consecutive_up = 0
    for i in range(last, -1, -1):
        if pcts[i] > 0:
            consecutive_up += 1
        else:
            break
    feats["consecutive_up"] = float(consecutive_up)
    feats["avg_pct_chg_5d"] = float(np.nanmean(pcts[last - 4:last + 1])) if len(pcts) >= 5 else 0.0

    # 反转信号（供 reversal_signal 因子使用）
    # 定义：昨日上涨 but 之前 5 日有 3+ 天下跌 → 可能反转
    reversal = 0
    if len(pcts) >= 7:
        prev_5 = pcts[last - 6:last - 1]
        if pcts[last - 1] > 0 and np.sum(prev_5 < 0) >= 3:
            reversal = 1
    feats["reversal_signal"] = float(reversal)

    return feats


def realtime_row(
    code: str,
    market: dict,
    vwap: float,
    klines: list[dict],
    daily_features: dict,
    daily_basic: dict,
    dim_scores: dict,
    daily_rows: list[dict] | None = None,
) -> dict:
    """构造一条可传入 limit_up 因子函数的实时面板行。

    字段命名与 quality_combo / intraday_strength / vol_expansion / turnover_momentum 等因子对齐。
    """
    last = float(market.get("last") or 0)
    open_price = float(market.get("open") or market.get("open_price") or 0)
    pre_close = float(market.get("pre_close") or 0)

    # fallback：从日线取开盘价/昨收（非交易日测试数据常缺失）
    if daily_rows:
        latest = daily_rows[-1]
        if open_price <= 0:
            open_price = float(latest.get("open", 0))
        if pre_close <= 0:
            pre_close = float(latest.get("pre_close", 0))

    pct = ((last / pre_close - 1) * 100) if pre_close > 0 else 0.0
    gap = ((open_price / pre_close - 1) * 100) if pre_close > 0 else 0.0

    # 量比代理：当日累计成交量 / 近20日同期均量
    vol_ratio_proxy = 1.0
    today_volume = float(market.get("trade_volume") or market.get("volume") or 0)
    if daily_features.get("avg_amount_5d") and today_volume > 0:
        # 用金额比近似量比（无历史分钟成交量时）
        last_amount = float(market.get("trade_amount") or market.get("amount") or 0)
        if last_amount > 0 and daily_features["avg_amount_5d"] > 0:
            vol_ratio_proxy = last_amount / daily_features["avg_amount_5d"]

    # 换手率代理：当日累计成交额 / 流通市值（%）
    # circ_mv 单位万元（Tushare daily_basic），trade_amount 单位元（L2）
    turnover_rate = 0.0
    circ_mv = float(daily_basic.get("circ_mv") or 0)
    if circ_mv > 0:
        last_amount = float(market.get("trade_amount") or market.get("amount") or 0)
        turnover_rate = (last_amount / (circ_mv * 10000)) * 100

    # 成交额比
    amount_ratio = 1.0
    if daily_features.get("avg_amount_5d") and daily_features["avg_amount_5d"] > 0:
        last_amount = float(market.get("trade_amount") or market.get("amount") or 0)
        amount_ratio = last_amount / daily_features["avg_amount_5d"]

    row = {
        "code": code,
        "last_price": last,
        "pct_chg_score_day": pct,
        "gap_up": gap,
        "gap_up_pit": gap,
        "vol_ratio_proxy": vol_ratio_proxy,
        "volume_ratio": vol_ratio_proxy,
        "turnover_rate": turnover_rate,
        "turnover_rate_f": turnover_rate,
        "amount_ratio": amount_ratio,
        "vwap": vwap,
        "position_20d": daily_features.get("position_20d", 0.5),
        "trailing_10": daily_features.get("trailing_10", 0.0),
        "trailing_10_pit": daily_features.get("trailing_10", 0.0),
        "trailing_5": daily_features.get("trailing_5", 0.0),
        "pullback_10d": daily_features.get("pullback_10d", 0.1),
        "pullback_20d": daily_features.get("pullback_20d", 0.1),
        "pct_chg_std_10d": daily_features.get("pct_chg_std_10d", 0.0),
        "pct_chg_std_5d": daily_features.get("pct_chg_std_5d", 0.0),
        "max_pct_chg_5d": daily_features.get("max_pct_chg_5d", 0.0),
        "avg_amount_5d": daily_features.get("avg_amount_5d", 0.0),
        "limit_up_count_20d": daily_features.get("limit_up_count_20d", 0.0),
        "limit_up_count_60d": daily_features.get("limit_up_count_60d", 0.0),
        "circ_mv": circ_mv,
        "pe": float(daily_basic.get("pe") if daily_basic.get("pe") is not None else 999.0),
        "pb": float(daily_basic.get("pb") if daily_basic.get("pb") is not None else 999.0),
        "fundamental": dim_scores.get("fundamental", 0.0),
        "technical": dim_scores.get("technical", 0.0),
        "fundflow": dim_scores.get("fundflow", 0.0),
        "sentiment": dim_scores.get("sentiment", 0.0),
        "shortterm": dim_scores.get("shortterm", 0.0),
        # 新字段（供 pattern/trailing 因子使用）
        "reversal_signal": daily_features.get("reversal_signal", 0.0),
        "consecutive_up": daily_features.get("consecutive_up", 0.0),
        "avg_pct_chg_5d": daily_features.get("avg_pct_chg_5d", 0.0),
    }
    return row


def minute_momentum(klines: list[dict], n: int = 5) -> dict:
    """计算最近 n 根分钟 K 线的动量。

    返回 {"chg_pct": 涨幅%, "vol_ratio": 成交量/前n根均量, "bars": 有效bar数}
    """
    if len(klines) < n + 1:
        return {"chg_pct": 0.0, "vol_ratio": 1.0, "bars": len(klines)}

    recent = klines[-n:]
    prev = klines[-(n + 1):-1]

    c0 = recent[0].get("open") or recent[0].get("close") or 0
    c1 = recent[-1].get("close", 0)
    chg_pct = ((c1 / c0 - 1) * 100) if c0 > 0 else 0.0

    vol_recent = sum(float(b.get("volume") or 0) for b in recent)
    vol_prev = sum(float(b.get("volume") or 0) for b in prev)
    vol_ratio = (vol_recent / vol_prev) if vol_prev > 0 else 1.0

    return {"chg_pct": chg_pct, "vol_ratio": vol_ratio, "bars": len(recent)}
