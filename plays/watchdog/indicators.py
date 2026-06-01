"""
策略指标计算 V1.1
===============
双引擎动量-均值回归混合策略：KAMA + ADX + 布林带 + RSI + VWAP
从Tushare日线数据计算，供watchdog使用。

V1.1 修复:
- check_trend Close>SMA20 比较逻辑补全
- check_exit_signal 增加 current_price 参数，移动止损实际返回信号
- bollinger带宽百分位严格排名(不含自身) + 向量化std
- RSI除零保护改用 np.finfo(float).eps
- calc_all 入口统一类型转换, 支持 asset_group 参数
- check_entry_score 增加 direction 参数(多/空) + signal_high(做空对称)
- check_exit_signal 时间止损补全浮盈条件
- KAMA/EMA/ATR/ADX 可选 numba JIT 加速
"""

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

# 可选 numba JIT 加速
try:
    from numba import njit  # pyright: ignore[reportMissingImports]
    _HAS_NUMBA = True
except ImportError:
    def njit(f=None, **kwargs):
        return f
    _HAS_NUMBA = False


# ---- KAMA (Kaufman Adaptive Moving Average) ----

@njit
def _kama_core(close, n, fast, slow):
    result = np.full(len(close), np.nan)
    # rolling volatility
    vols = np.zeros(len(close) - n)
    for i in range(n, len(close)):
        s = 0.0
        for j in range(i - n + 1, i + 1):
            s += abs(close[j] - close[j-1])
        vols[i - n] = s
    dirs = np.abs(close[n:] - close[:-n])
    fast_sc = 2.0 / (fast + 1)
    slow_sc = 2.0 / (slow + 1)
    er = np.zeros(len(dirs))
    for i in range(len(dirs)):
        er[i] = dirs[i] / vols[i] if vols[i] != 0 else 0.0
    sc = (er * (fast_sc - slow_sc) + slow_sc) ** 2
    result[n] = np.mean(close[:n+1])
    for i in range(n + 1, len(close)):
        result[i] = result[i-1] + sc[i-n-1] * (close[i] - result[i-1])
    return result

# ---- KAMA (Kaufman Adaptive Moving Average) ----

def kama(close: np.ndarray, n: int = 10, fast: int = 2, slow: int = 30) -> np.ndarray:
    """Kaufman自适应均线"""
    close = np.ascontiguousarray(close, dtype=np.float64)
    if len(close) < n + 1:
        return np.full_like(close, np.nan)
    return _kama_core(close, n, fast, slow)


# ---- EMA ----

@njit
def _ema_core(series, period):
    result = np.full(len(series), np.nan)
    # 跳过前导NaN，找到第一个有效值
    start = 0
    while start < len(series) and np.isnan(series[start]):
        start += 1
    if start + period > len(series):
        return result
    result[start + period - 1] = np.mean(series[start:start + period])
    alpha = 2 / (period + 1)
    for i in range(start + period, len(series)):
        result[i] = alpha * series[i] + (1 - alpha) * result[i-1]
    return result


def ema(series: np.ndarray, period: int) -> np.ndarray:
    series = np.ascontiguousarray(series, dtype=np.float64)
    if len(series) < period:
        return np.full_like(series, np.nan, dtype=float)
    return _ema_core(series, period)


# ---- SMA ----

def sma(series: np.ndarray, period: int) -> np.ndarray:
    series = np.ascontiguousarray(series, dtype=np.float64)
    result = np.full_like(series, np.nan, dtype=float)
    if len(series) < period:
        return result
    cumsum = np.cumsum(np.insert(series, 0, 0))
    result[period-1:] = (cumsum[period:] - cumsum[:-period]) / period
    return result


# ---- ADX ----

@njit
def _adx_core(high, low, close, period):
    n = len(close)
    tr = np.zeros(n)
    plus_dm = np.zeros(n)
    minus_dm = np.zeros(n)
    for i in range(1, n):
        hl = high[i] - low[i]
        hc = abs(high[i] - close[i-1])
        lc = abs(low[i] - close[i-1])
        tr[i] = max(hl, hc, lc)
        up = high[i] - high[i-1]
        dn = low[i-1] - low[i]
        plus_dm[i] = up if up > dn and up > 0 else 0
        minus_dm[i] = dn if dn > up and dn > 0 else 0

    atr = np.full(n, np.nan)
    atr[period] = np.mean(tr[1:period+1])
    for i in range(period+1, n):
        atr[i] = (atr[i-1] * (period-1) + tr[i]) / period

    smoothed_plus = np.full(n, np.nan)
    smoothed_minus = np.full(n, np.nan)
    smoothed_plus[period] = np.sum(plus_dm[1:period+1])
    smoothed_minus[period] = np.sum(minus_dm[1:period+1])
    for i in range(period+1, n):
        smoothed_plus[i] = (smoothed_plus[i-1] * (period-1) + plus_dm[i]) / period
        smoothed_minus[i] = (smoothed_minus[i-1] * (period-1) + minus_dm[i]) / period

    di_plus = np.where(atr > 0, 100 * smoothed_plus / atr, 0.0)
    di_minus = np.where(atr > 0, 100 * smoothed_minus / atr, 0.0)

    denom = di_plus + di_minus
    dx = np.zeros(n)
    for i in range(n):
        dx[i] = 100 * abs(di_plus[i] - di_minus[i]) / denom[i] if denom[i] > 0 else 0.0

    adx_arr = np.full(n, np.nan)
    adx_arr[2*period-1] = np.mean(dx[period:2*period])
    for i in range(2*period, n):
        adx_arr[i] = (adx_arr[i-1] * (period-1) + dx[i]) / period
    return adx_arr, di_plus, di_minus


def adx(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14):
    """返回 (ADX, +DI, -DI)"""
    high = np.ascontiguousarray(high, dtype=np.float64)
    low = np.ascontiguousarray(low, dtype=np.float64)
    close = np.ascontiguousarray(close, dtype=np.float64)
    if len(close) < period + 1:
        return np.full(len(close), np.nan), np.full(len(close), np.nan), np.full(len(close), np.nan)
    return _adx_core(high, low, close, period)


# ---- ATR ----

@njit
def _atr_core(high, low, close, period):
    n = len(close)
    result = np.full(n, np.nan)
    tr = np.zeros(n)
    for i in range(1, n):
        tr[i] = max(high[i]-low[i], abs(high[i]-close[i-1]), abs(low[i]-close[i-1]))
    result[period] = np.mean(tr[1:period+1])
    for i in range(period+1, n):
        result[i] = (result[i-1] * (period-1) + tr[i]) / period
    return result


def atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 20) -> np.ndarray:
    high = np.ascontiguousarray(high, dtype=np.float64)
    low = np.ascontiguousarray(low, dtype=np.float64)
    close = np.ascontiguousarray(close, dtype=np.float64)
    if len(close) < period + 1:
        return np.full(len(close), np.nan)
    return _atr_core(high, low, close, period)


# ---- Bollinger Bands ----

def bollinger(close: np.ndarray, period: int = 20, std_dev: float = 2.0):
    """返回 (mid, upper, lower, bandwidth_percentile)
    V1.1: 向量化std + 百分位不含自身(无前视偏差)
    """
    close = np.ascontiguousarray(close, dtype=np.float64)
    mid = sma(close, period)
    n = len(close)
    upper = np.full(n, np.nan)
    lower = np.full(n, np.nan)
    bw = np.full(n, np.nan)
    bw_pct = np.full(n, np.nan)

    if n >= period:
        windows = sliding_window_view(close, period)
        stds = np.std(windows, axis=1)
        valid = slice(period - 1, n)
        upper[valid] = mid[valid] + std_dev * stds
        lower[valid] = mid[valid] - std_dev * stds
        bw[valid] = np.where(mid[valid] > 0,
                              (upper[valid] - lower[valid]) / mid[valid], 0)

    # 严格百分位: 当前值在历史窗口中的排名(不含自身)
    for i in range(period, n):
        start = max(period - 1, i - 20)
        hist = bw[start:i]
        valid_hist = hist[~np.isnan(hist)]
        if len(valid_hist) > 0:
            bw_pct[i] = np.searchsorted(np.sort(valid_hist), bw[i], side="right") / len(valid_hist)

    return mid, upper, lower, bw_pct


# ---- RSI ----

def rsi(close: np.ndarray, period: int = 3) -> np.ndarray:
    close = np.ascontiguousarray(close, dtype=np.float64)
    n = len(close)
    result = np.full(n, np.nan)
    if n < period + 1:
        return result
    diff = np.diff(close)
    gain = np.maximum(diff, 0)
    loss = np.maximum(-diff, 0)
    avg_gain = np.full(n, np.nan)
    avg_loss = np.full(n, np.nan)
    avg_gain[period] = np.mean(gain[:period])
    avg_loss[period] = np.mean(loss[:period])
    for i in range(period+1, n):
        avg_gain[i] = (avg_gain[i-1] * (period-1) + gain[i-1]) / period
        avg_loss[i] = (avg_loss[i-1] * (period-1) + loss[i-1]) / period
    safe_loss = np.where(avg_loss[period:] == 0, np.finfo(float).eps, avg_loss[period:])
    result[period:] = 100 - 100 / (1 + avg_gain[period:] / safe_loss)
    return result


# ---- 综合计算 ----

STOCK_GROUP_PARAMS = {
    "A": {"kama_n": 10, "kama_fast": 2, "kama_slow": 30,
          "rsi_period": 3, "atr_period": 20, "bb_period": 20, "adx_period": 14},
    "commodity": {"kama_n": 14, "kama_fast": 2, "kama_slow": 40,
                  "rsi_period": 5, "atr_period": 20, "bb_period": 20, "adx_period": 14},
    "forex": {"kama_n": 20, "kama_fast": 3, "kama_slow": 50,
              "rsi_period": 7, "atr_period": 20, "bb_period": 20, "adx_period": 14},
}


def calc_all(df, asset_group: str = "A"):
    """
    输入 df: dict of numpy arrays (open, high, low, close, volume, pre_close)
    返回 dict of numpy arrays
    """
    group = STOCK_GROUP_PARAMS.get(asset_group)
    if group is None:
        raise ValueError(f"未知资产组: {asset_group}, 可选: {list(STOCK_GROUP_PARAMS.keys())}")

    c = np.ascontiguousarray(df["close"], dtype=np.float64)
    h = np.ascontiguousarray(df["high"], dtype=np.float64)
    l = np.ascontiguousarray(df["low"], dtype=np.float64)  # noqa: E741
    v = np.ascontiguousarray(df.get("volume", df.get("vol", np.zeros_like(c))), dtype=np.float64)  # noqa: F841

    k = kama(c, group["kama_n"], group["kama_fast"], group["kama_slow"])
    k_ema = ema(k, 20)
    s20 = sma(c, 20)
    adx_arr, plus, minus = adx(h, l, c, group["adx_period"])
    a = atr(h, l, c, group["atr_period"])
    bb_mid, bb_up, bb_lo, bb_pct = bollinger(c, group["bb_period"])
    r = rsi(c, group["rsi_period"])

    return {
        "kama": k, "kama_ema": k_ema, "sma20": s20,
        "adx": adx_arr, "di_plus": plus, "di_minus": minus,
        "atr20": a,
        "bb_mid": bb_mid, "bb_upper": bb_up, "bb_lower": bb_lo, "bb_bw_pct": bb_pct,
        "rsi3": r,
        "close": c,  # 保留供check_trend使用
    }


# ---- 策略信号判断 ----

def check_trend(inds, i: int = -1) -> tuple[bool, str]:
    """Step 1 趋势过滤: KAMA > EMA(KAMA), Close > SMA(20), ADX>20且+DI>-DI"""
    close_arr = inds.get("close")
    if close_arr is None or len(close_arr) <= abs(i):
        return False, "缺少收盘价数据"

    kama_val = inds["kama"][i]
    kama_ema_val = inds["kama_ema"][i]
    sma_val = inds["sma20"][i]
    adx_val = inds["adx"][i]
    di_p = inds["di_plus"][i]
    di_m = inds["di_minus"][i]
    close_val = close_arr[i]

    reasons = []
    ok = True
    if np.isnan(kama_val) or np.isnan(kama_ema_val):
        return False, "指标数据不足"
    if kama_val <= kama_ema_val:
        ok = False
        reasons.append("KAMA≤EMA")
    if np.isnan(sma_val) or close_val <= sma_val:
        ok = False
        reasons.append(f"Close{close_val:.2f}≤SMA20({sma_val:.2f})")
    if not np.isnan(adx_val) and adx_val <= 20:
        ok = False
        reasons.append(f"ADX{adx_val:.1f}≤20")
    if not np.isnan(di_p) and not np.isnan(di_m) and di_p <= di_m:
        ok = False
        reasons.append("+DI≤-DI")

    return ok, "; ".join(reasons) if reasons else "趋势确认"


def check_pullback(inds, close_i: float, i: int = -1) -> tuple[bool, str]:
    """Step 2 回调待机: RSI<阈值 AND 收盘价≤布林下轨"""
    rsi_val = inds["rsi3"][i]
    bb_lower = inds["bb_lower"][i]
    bw_pct = inds["bb_bw_pct"][i]

    if np.isnan(rsi_val) or np.isnan(bb_lower):
        return False, "指标数据不足"

    rsi_threshold = 20 if (not np.isnan(bw_pct) and bw_pct < 0.3) else 15
    if rsi_val < rsi_threshold and close_i <= bb_lower:
        return True, f"回调到位(RSI{rsi_val:.1f}<{rsi_threshold}, {close_i:.2f}≤下轨{bb_lower:.2f})"
    return False, f"未触发(RSI{rsi_val:.1f}, 下轨{bb_lower:.2f})"


def check_entry_score(inds, atr_val: float, vwap: float, open_price: float,
                      signal_low: float, signal_high: float, current_price: float,
                      current_vol: float, avg_vol_20: float, direction: int = 1) -> tuple[int, str]:
    """Step 3 计分入场: A价格验证 + B放量 + C未过度溢价
    direction: 1=做多, -1=做空
    signal_low/signal_high: Step2触发时的价格参考点
    """
    score = 0
    reasons = []
    # A. 价格验证（多空对称）
    if direction == 1:
        if current_price > signal_low + 0.3 * atr_val:
            score += 1
            reasons.append("价验")
    else:
        if current_price < signal_high - 0.3 * atr_val:
            score += 1
            reasons.append("价验")
    # B. 成交量放大
    if avg_vol_20 > 0 and current_vol > avg_vol_20 * 1.1:
        score += 1
        reasons.append("放量")
    # C. 未过度溢价: direction * (VWAP - 开盘价) < 0.5*ATR
    if vwap > 0:
        premium = direction * (vwap - open_price)
        if premium < 0.5 * atr_val:
            score += 1
            reasons.append("未溢价")
    return score, f"计分{score}/3 ({' '.join(reasons)})" if reasons else f"计分{score}/3"


def check_exit_signal(inds, entry_price: float, highest_since_entry: float,
                      bars_held: int, atr_val: float, current_price: float,
                      max_profit_since_entry: float | None = None) -> tuple[bool, str]:
    """出场规则: 移动止损 / 条件时间止损 / 趋势反转"""
    # 移动止损
    if highest_since_entry > entry_price:
        stop_price = highest_since_entry - 2 * atr_val
        if current_price <= stop_price:
            return True, f"移动止损(最高{highest_since_entry:.2f}, 止损{stop_price:.2f}, 现价{current_price:.2f})"

    # 条件时间止损: 持仓>15根K线 AND (ADX<20 OR 最大浮盈<0.5*ATR)
    adx_val = inds["adx"][-1]
    profit_check = True
    if max_profit_since_entry is not None:
        profit_check = max_profit_since_entry < 0.5 * atr_val
    if bars_held > 15 and ((not np.isnan(adx_val) and adx_val < 20) or profit_check):
        max_p_str = f"{max_profit_since_entry:.2f}" if max_profit_since_entry is not None else "?"
        return True, f"时间止损(持仓{bars_held}根, ADX{adx_val:.1f}, 最大浮盈{max_p_str})"

    # 趋势反转
    kama_val = inds["kama"][-1]
    kama_ema_val = inds["kama_ema"][-1]
    di_p = inds["di_plus"][-1]
    di_m = inds["di_minus"][-1]
    if not np.isnan(kama_val) and not np.isnan(kama_ema_val):
        if kama_val < kama_ema_val and not np.isnan(di_p) and not np.isnan(di_m) and di_m > di_p:
            return True, "趋势反转(KAMA下穿EMA, -DI>+DI)"

    return False, ""