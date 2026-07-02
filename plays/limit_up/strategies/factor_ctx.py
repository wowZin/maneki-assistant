"""策略共享的数据上下文（PIT 运行时）。

由 pipeline.py 在评分前统一填充缓存，策略文件只读不写，避免重复 API 调用。
不依赖 pipeline.py 或 backtest/ 的具体实现。
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

# ── 模块级缓存 ─────────────────────────────────────
_DAILY_CACHE: dict[str, list[dict]] = {}
_DAILY_BASIC_CACHE: dict[str, dict[str, dict]] = {}
_LIMIT_20D_CACHE: dict[str, int] = {}
_LIMIT_60D_CACHE: dict[str, int] = {}

_CONCEPT_DAILY_CACHE: pd.DataFrame | None = None
_CONCEPT_MEMBER_CACHE: pd.DataFrame | None = None


# ── 写入接口（仅 pipeline.py 调用） ─────────────────

def set_daily(code: str, rows: list[dict]):
    """设置某只股票的日线序列，按 trade_date 降序。"""
    _DAILY_CACHE[code] = rows


def set_daily_basic(code: str, by_date: dict[str, dict]):
    """设置某只股票 daily_basic 字典 {trade_date: row}。"""
    _DAILY_BASIC_CACHE[code] = by_date


def set_limit_counts(code: str, count_20d: int, count_60d: int):
    """设置涨停次数缓存。"""
    _LIMIT_20D_CACHE[code] = count_20d
    _LIMIT_60D_CACHE[code] = count_60d


def set_concept_data(concept_daily: pd.DataFrame | None, concept_members: pd.DataFrame | None):
    """设置概念行情与成分股映射。"""
    global _CONCEPT_DAILY_CACHE, _CONCEPT_MEMBER_CACHE
    _CONCEPT_DAILY_CACHE = concept_daily
    _CONCEPT_MEMBER_CACHE = concept_members


def clear_all():
    """清空所有缓存。"""
    global _CONCEPT_DAILY_CACHE, _CONCEPT_MEMBER_CACHE
    _DAILY_CACHE.clear()
    _DAILY_BASIC_CACHE.clear()
    _LIMIT_20D_CACHE.clear()
    _LIMIT_60D_CACHE.clear()
    _CONCEPT_DAILY_CACHE = None
    _CONCEPT_MEMBER_CACHE = None


# ── 读取接口（策略使用） ────────────────────────────

def get_daily(code: str) -> list[dict]:
    return _DAILY_CACHE.get(code, [])


def get_daily_basic(code: str, trade_date: str | None = None) -> dict | None:
    """获取某只股票某交易日 daily_basic；未指定日期则取最近一条。"""
    by_date = _DAILY_BASIC_CACHE.get(code, {})
    if not by_date:
        return None
    if trade_date and trade_date in by_date:
        return by_date[trade_date]
    # 取最新日期
    latest = sorted(by_date.keys(), reverse=True)
    return by_date[latest[0]] if latest else None


def get_limit_up_count(code: str, days: int = 20) -> int:
    if days <= 20:
        return _LIMIT_20D_CACHE.get(code, 0)
    return _LIMIT_60D_CACHE.get(code, 0)


def _safe_float(val, default: float = 0.0) -> float:
    if val is None:
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def get_price_features(code: str) -> dict:
    """提取常用价格特征：trailing_10/5, position_20d, pullback_10d/20d, std10/5, max_pct_chg_5d。"""
    rows = _DAILY_CACHE.get(code, [])
    rows = sorted(rows, key=lambda x: x.get("trade_date", ""), reverse=True)
    feats = {
        "trailing_10": 0.0,
        "trailing_5": 0.0,
        "position_20d": 0.5,
        "pullback_10d": 0.1,
        "pullback_20d": 0.1,
        "pct_chg_std_10d": 0.0,
        "pct_chg_std_5d": 0.0,
        "max_pct_chg_5d": 0.0,
    }
    if len(rows) < 2:
        return feats

    def trailing(days: int) -> float:
        if len(rows) < days:
            return 0.0
        try:
            return float(rows[0]["close"]) / float(rows[days - 1]["close"]) - 1.0
        except Exception:
            return 0.0

    feats["trailing_10"] = trailing(10)
    feats["trailing_5"] = trailing(5)

    try:
        highs = [float(r["high"]) for r in rows[:20] if r.get("high")]
        lows = [float(r["low"]) for r in rows[:20] if r.get("low")]
        closes = [float(r["close"]) for r in rows[:20] if r.get("close")]
        if highs and lows and closes:
            h20, l20, c0 = max(highs), min(lows), closes[0]
            if h20 > l20:
                feats["position_20d"] = (c0 - l20) / (h20 - l20)
    except Exception:
        pass

    def pullback(days: int) -> float:
        if len(rows) < 2:
            return 0.1
        try:
            highs = [float(r["high"]) for r in rows[:days] if r.get("high")]
            c0 = float(rows[0]["close"])
            h = max(highs)
            return max(0.0, (h - c0) / h) if h > 0 else 0.1
        except Exception:
            return 0.1

    feats["pullback_10d"] = pullback(10)
    feats["pullback_20d"] = pullback(20)

    try:
        pcts = [_safe_float(r.get("pct_chg"), 0.0) for r in rows[:10]]
        if len(pcts) >= 5:
            feats["pct_chg_std_10d"] = float(pd.Series(pcts).std(ddof=0)) if len(pcts) >= 2 else 0.0
            feats["max_pct_chg_5d"] = max(pcts[:5])
        pcts5 = [_safe_float(r.get("pct_chg"), 0.0) for r in rows[:5]]
        if len(pcts5) >= 2:
            feats["pct_chg_std_5d"] = float(pd.Series(pcts5).std(ddof=0))
    except Exception:
        pass

    return feats


def get_concept_momentum(code_short: str, trade_date: str | None = None) -> dict:
    """获取股票所属概念的最新动量（使用 concept_daily 最近日期）。

    Args:
        code_short: 股票短代码，如 "000001"
        trade_date: 指定交易日 YYYYMMDD。传入时仅使用该日期及之前的数据，
                    用于 PIT 回测；不传时使用缓存中最新日期，用于实时评分。

    返回 {"ret3_max", "ret1_max", "ret5_max", "ret3_avg", "ret1_avg", "up_ratio",
          "up_streak_max", "turn_5d_max", "turn_5d_avg", "n_concepts"}
    """
    result = {
        "ret3_max": 0.0,
        "ret1_max": 0.0,
        "ret5_max": 0.0,
        "ret3_avg": 0.0,
        "ret1_avg": 0.0,
        "up_ratio": 0.5,
        "up_streak_max": 0,
        "turn_5d_max": 0.0,
        "turn_5d_avg": 0.0,
        "n_concepts": 0,
    }
    if _CONCEPT_MEMBER_CACHE is None or _CONCEPT_DAILY_CACHE is None:
        return result
    if _CONCEPT_MEMBER_CACHE.empty or _CONCEPT_DAILY_CACHE.empty:
        return result

    # 股票所属概念
    members = _CONCEPT_MEMBER_CACHE[_CONCEPT_MEMBER_CACHE["stock_code"] == code_short]
    if members.empty:
        return result
    cpt_codes = members["cpt_code"].unique().tolist()

    # 概念日线数据
    cd = _CONCEPT_DAILY_CACHE[_CONCEPT_DAILY_CACHE["cpt_code"].isin(cpt_codes)].copy()
    if cd.empty:
        return result
    cd["trade_date"] = cd["trade_date"].astype(str)

    # PIT：过滤到指定日期及之前
    if trade_date:
        cd = cd[cd["trade_date"] <= str(trade_date)]
        if cd.empty:
            return result

    # 最新可用日期
    latest_date = cd["trade_date"].max()
    latest = cd[cd["trade_date"] == latest_date]
    if latest.empty:
        return result

    pcts = latest["pct_change"].apply(lambda x: _safe_float(x, 0.0))
    turns = latest["turnover_rate"].apply(lambda x: _safe_float(x, 0.0))
    result["ret1_max"] = pcts.max()
    result["ret1_avg"] = pcts.mean()
    result["n_concepts"] = len(pcts)
    result["up_ratio"] = (pcts > 0).mean() if len(pcts) > 0 else 0.5
    result["turn_5d_max"] = turns.max()
    result["turn_5d_avg"] = turns.mean()

    # 近5日累计 / 连续上涨 / 5日换手（使用 latest_date 及之前的数据）
    streaks = []
    ret3s = []
    ret5s = []
    turn5s = []
    for cpt in cpt_codes:
        g = cd[cd["cpt_code"] == cpt].sort_values("trade_date")
        if len(g) >= 1:
            p = g["pct_change"].apply(lambda x: _safe_float(x, 0.0))
            t = g["turnover_rate"].apply(lambda x: _safe_float(x, 0.0))
            ret3s.append(p.tail(3).sum())
            ret5s.append(p.tail(5).sum())
            turn5s.append(t.tail(5).mean())
            up = (p > 0).astype(int)
            streak = 0
            for v in up.tolist()[::-1]:
                if v:
                    streak += 1
                else:
                    break
            streaks.append(streak)

    if ret3s:
        result["ret3_max"] = max(ret3s)
        result["ret3_avg"] = sum(ret3s) / len(ret3s)
        result["ret5_max"] = max(ret5s)
        result["up_streak_max"] = max(streaks)
        result["turn_5d_max"] = max(turn5s) if turn5s else result["turn_5d_max"]
        result["turn_5d_avg"] = sum(turn5s) / len(turn5s) if turn5s else result["turn_5d_avg"]

    return result


def load_concept_data_from_cache(cache_dir: str | Path):
    """从 backtest/cache 加载概念数据（pipeline 初始化时调用）。"""
    cache_dir = Path(cache_dir)
    daily_f = cache_dir / "concept_daily.parquet"
    member_f = cache_dir / "concept_members.parquet"
    cd = pd.read_parquet(daily_f) if daily_f.exists() else pd.DataFrame()
    cm = pd.read_parquet(member_f) if member_f.exists() else pd.DataFrame()
    set_concept_data(cd, cm)
    return cd, cm
