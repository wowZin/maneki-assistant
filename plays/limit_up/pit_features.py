"""PIT（时点严格）特征构建。

为生产 pipeline、回测面板、训练集提供统一的特征计算入口。
所有特征只使用 score_date 当日及之前已公布的数据，禁止未来函数。

约定：
- daily.amount 单位：千元（Tushare 原生）。
- moneyflow.*_amount 单位：万元（Tushare 原生）。
- daily_basic.circ_mv 单位：万元（Tushare 原生）。
- 特征内部做必要换算，输出特征无单位依赖。
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np


def _safe_float(val, default: float = 0.0) -> float:
    if val is None:
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def _find_idx(dates: list[str], target: str) -> int | None:
    try:
        return dates.index(target)
    except ValueError:
        return None


def _basic_on_or_before(basic_by_date: dict[str, dict] | None, date: str) -> dict:
    if not basic_by_date:
        return {}
    if date in basic_by_date:
        return basic_by_date[date]
    for d in sorted(basic_by_date.keys(), reverse=True):
        if d <= date:
            return basic_by_date[d]
    return {}


def _mf_on_or_before(mf_by_date: dict[str, dict] | None, date: str) -> dict:
    if not mf_by_date:
        return {}
    if date in mf_by_date:
        return mf_by_date[date]
    for d in sorted(mf_by_date.keys(), reverse=True):
        if d <= date:
            return mf_by_date[d]
    return {}


def _top_on_or_before(top_by_date: dict[str, Any] | None, date: str) -> Any | None:
    """龙虎榜/席位数据按日期查找（值可能是 dict 或 list[dict]）。"""
    if not top_by_date:
        return None
    if date in top_by_date:
        return top_by_date[date]
    for d in sorted(top_by_date.keys(), reverse=True):
        if d <= date:
            return top_by_date[d]
    return None


def _consecutive_limit_height(pcts: list[float], end_idx: int) -> int:
    """从 end_idx 向前数连续涨停（pct_chg >= 9.8）的天数。"""
    h = 0
    for j in range(end_idx, -1, -1):
        if pcts[j] is not None and pcts[j] >= 9.8:
            h += 1
        else:
            break
    return h


def _limit_count_in_window(pcts: list[float], end_idx: int, window: int) -> int:
    """end_idx 往前 window 个交易日内 pct_chg >= 9.8 的天数。"""
    lo = max(0, end_idx - window + 1)
    return sum(1 for p in pcts[lo : end_idx + 1] if p is not None and p >= 9.8)


def _candle_features(rows: list[dict], idx: int) -> dict:
    """基于 idx 日 OHLC 计算 K 线形态特征。"""
    out = {
        "close_pos": 0.5,
        "body_ratio": 0.0,
        "upper_ratio": 0.0,
        "lower_ratio": 0.0,
        "amplitude": 0.0,
    }
    if idx < 0 or idx >= len(rows):
        return out
    r = rows[idx]
    o = _safe_float(r.get("open"), 0.0)
    h = _safe_float(r.get("high"), 0.0)
    l = _safe_float(r.get("low"), 0.0)
    c = _safe_float(r.get("close"), 0.0)
    if h <= l or c == 0:
        return out
    rng = h - l
    out["amplitude"] = rng / c
    out["close_pos"] = (c - l) / rng
    out["body_ratio"] = abs(c - o) / rng
    out["upper_ratio"] = (h - max(o, c)) / rng
    out["lower_ratio"] = (min(o, c) - l) / rng
    return out


def build_pit_features(
    code: str,
    score_date: str,
    daily_rows: list[dict],
    basic_by_date: dict[str, dict] | None = None,
    moneyflow_by_date: dict[str, dict] | None = None,
    auction_by_date: dict[str, dict] | None = None,
    intraday_by_date: dict[str, dict] | None = None,
    concept_momentum: dict[str, Any] | None = None,
    top_list_by_date: dict[str, dict] | None = None,
    top_inst_by_date: dict[str, dict] | None = None,
    pit_mode: bool = True,
) -> dict:
    """为某只股票在 score_date 构建 PIT 特征字典。

    Args:
        code: 带后缀代码，如 "000001.SZ"。
        score_date: 评分日（YYYYMMDD）。生产中是扫描当天 T；特征使用 T-1 收盘。
        daily_rows: 按 trade_date 升序排列的日线 dict 列表。
        basic_by_date: {trade_date: daily_basic_row}，万元单位。
        moneyflow_by_date: {trade_date: moneyflow_row}，万元单位。
        auction_by_date: {trade_date: stk_auction_row}。
        concept_momentum: get_concept_momentum() 返回的概念动量 dict。
        pit_mode: True 表示用 score_date 前一个交易日收盘作为特征基准（与生产一致）。
                  False 表示用 score_date 当天（仅用于兼容旧训练集逻辑，不建议新模型使用）。

    Returns:
        dict: 所有特征字段，缺失时填合理默认值。
    """
    # 默认特征值
    feats: dict[str, Any] = {
        "position_20d": 0.5,
        "trailing_10": 0.0,
        "trailing_5": 0.0,
        "pct_chg_std_10d": 0.0,
        "pct_chg_std_5d": 0.0,
        "max_pct_chg_5d": 0.0,
        "limit_up_count_20d": 0.0,
        "limit_up_count_60d": 0.0,
        "max_step": 0.0,
        "was_limit": 0.0,
        "avg_amount_5d": 0.0,
        "pct_chg_score_day": 0.0,
        "turnover_rate": 5.0,
        "volume_ratio": 1.0,
        "prev_turnover": 5.0,
        "prev_vol_ratio": 1.0,
        "vol_accel": 0.0,
        "circ_mv": 0.0,
        "cmv_yi": 0.0,
        "pe": 999.0,
        "pb": 999.0,
        "pullback_10d": 0.1,
        "pullback_20d": 0.1,
        "prev_pct": 0.0,
        "pct_5d": 0.0,
        "positive_5d": 0.0,
        "close_pos": 0.5,
        "body_ratio": 0.0,
        "upper_ratio": 0.0,
        "lower_ratio": 0.0,
        "amplitude": 0.0,
        "net_mf_amount": 0.0,
        "net_mf_ratio": 0.0,
        "buy_elg_ratio": 0.5,
        "buy_lg_ratio": 0.5,
        "mf_net": 0.0,
        "mf_accel": 0.0,
        "mf_pct": 0.0,
        "sector_heat": 0.0,
        "sector_rank": 0.0,
        "n_concepts": 0.0,
        "auc_amount": 0.0,
        "auc_vol": 0.0,
        "auc_amt_ratio": 0.0,
        "auc_vol_ratio": 0.0,
        # 日内分时特征（T-1）
        "id_vwap_dev": 0.0,
        "id_range": 0.0,
        "id_morning_vol_ratio": 0.5,
        "id_afternoon_strength": 1.0,
        "id_tail_vol_ratio": 0.1,
        "id_amount_ratio": 0.0,
        # 龙虎榜 PIT 特征（T-1）
        "dt_is_listed": 0.0,
        "dt_net_amount": 0.0,
        "dt_net_rate": 0.0,
        "dt_l_buy_ratio": 0.0,
        "dt_n_exalter": 0.0,
        "dt_inst_net_buy": 0.0,
        "dt_hot_net_buy": 0.0,
        "dt_inst_sell_ratio": 0.0,
    }

    if not daily_rows:
        return feats

    # 确保升序
    rows = sorted(daily_rows, key=lambda r: r.get("trade_date", ""))
    dates = [str(r.get("trade_date", "")) for r in rows]
    closes = [_safe_float(r.get("close")) for r in rows]
    highs = [_safe_float(r.get("high")) for r in rows]
    lows = [_safe_float(r.get("low")) for r in rows]
    pcts = [_safe_float(r.get("pct_chg")) for r in rows]
    amounts = [_safe_float(r.get("amount")) for r in rows]  # 千元

    i = _find_idx(dates, score_date)
    if i is None:
        # 盘中扫描：daily 数据只有到 T-1，score_date（今天）不在数据中
        # 回退到最后一条可用数据，直接用作特征日（不需要 T-1 偏移）
        if dates:
            i = len(dates) - 1
            pit_i = i
        else:
            return feats
    else:
        # PIT 基准日：默认 T-1
        pit_i = i - 1 if pit_mode and i >= 1 else i

    if pit_i < 0:
        pit_i = i

    c0 = closes[pit_i]
    if c0 == 0:
        c0 = 1.0

    # ═══════════════════════════════════════════════════════
    # 价格位置与动量
    # ═══════════════════════════════════════════════════════
    lo20 = max(0, pit_i - 19)
    hs = [h for h in highs[lo20 : pit_i + 1] if h]
    ls = [l for l in lows[lo20 : pit_i + 1] if l]
    if hs and ls and max(hs) > min(ls):
        feats["position_20d"] = (c0 - min(ls)) / (max(hs) - min(ls))

    feats["trailing_10"] = (closes[pit_i] / closes[pit_i - 10] - 1.0) if pit_i >= 10 and closes[pit_i - 10] else 0.0
    feats["trailing_5"] = (closes[pit_i] / closes[pit_i - 5] - 1.0) if pit_i >= 5 and closes[pit_i - 5] else 0.0

    pcts_window = [p for p in pcts[lo20 : pit_i + 1] if p is not None]
    if len(pcts_window) >= 2:
        feats["pct_chg_std_10d"] = float(np.std(pcts_window, ddof=0))
    pcts5 = [p for p in pcts[max(0, pit_i - 4) : pit_i + 1] if p is not None]
    if len(pcts5) >= 2:
        feats["pct_chg_std_5d"] = float(np.std(pcts5, ddof=0))
        feats["max_pct_chg_5d"] = max(pcts5)

    # 回撤
    def _pullback(window: int) -> float:
        whs = [h for h in highs[max(0, pit_i - window + 1) : pit_i + 1] if h]
        if whs and c0:
            hmax = max(whs)
            return max(0.0, (hmax - c0) / hmax) if hmax > 0 else 0.1
        return 0.1

    feats["pullback_10d"] = _pullback(10)
    feats["pullback_20d"] = _pullback(20)

    # 成交额（千元 -> 元）
    amt_seq = [a for a in amounts[max(0, pit_i - 4) : pit_i + 1] if a]
    if amt_seq:
        feats["avg_amount_5d"] = float(np.mean(amt_seq)) * 1000

    # 当日涨幅（score_date 当天，生产中是盘中观察值）
    feats["pct_chg_score_day"] = pcts[i] if i < len(pcts) else 0.0

    # ═══════════════════════════════════════════════════════
    # daily_basic 特征（万元单位）
    # ═══════════════════════════════════════════════════════
    db_pit = _basic_on_or_before(basic_by_date, dates[pit_i])
    db_prev = _basic_on_or_before(basic_by_date, dates[pit_i - 1]) if pit_i >= 1 else {}

    feats["turnover_rate"] = _safe_float(db_pit.get("turnover_rate"), 5.0)
    feats["volume_ratio"] = _safe_float(db_pit.get("volume_ratio"), 1.0)
    feats["prev_turnover"] = _safe_float(db_prev.get("turnover_rate"), feats["turnover_rate"])
    feats["prev_vol_ratio"] = _safe_float(db_prev.get("volume_ratio"), feats["volume_ratio"])

    if feats["prev_vol_ratio"] != 0:
        feats["vol_accel"] = feats["volume_ratio"] / feats["prev_vol_ratio"] - 1.0

    feats["circ_mv"] = _safe_float(db_pit.get("circ_mv"), 0.0)
    feats["cmv_yi"] = feats["circ_mv"] / 10000.0
    feats["pe"] = _safe_float(db_pit.get("pe"), 999.0)
    feats["pb"] = _safe_float(db_pit.get("pb"), 999.0)

    # ═══════════════════════════════════════════════════════
    # 涨停基因 / 连板高度
    # ═══════════════════════════════════════════════════════
    feats["limit_up_count_20d"] = float(_limit_count_in_window(pcts, pit_i, 20))
    feats["limit_up_count_60d"] = float(_limit_count_in_window(pcts, pit_i, 60))
    feats["max_step"] = float(_consecutive_limit_height(pcts, pit_i))
    feats["was_limit"] = 1.0 if pcts[pit_i] is not None and pcts[pit_i] >= 9.8 else 0.0

    # ═══════════════════════════════════════════════════════
    # 短期价格动量
    # ═══════════════════════════════════════════════════════
    feats["prev_pct"] = pcts[pit_i - 1] if pit_i >= 1 else 0.0
    past5 = [p for p in pcts[max(0, pit_i - 4) : pit_i + 1] if p is not None]
    if past5:
        feats["pct_5d"] = sum(past5)
        feats["positive_5d"] = float(sum(1 for p in past5 if p > 0))

    # ═══════════════════════════════════════════════════════
    # K 线形态（基于 PIT 日）
    # ═══════════════════════════════════════════════════════
    candle = _candle_features(rows, pit_i)
    feats.update(candle)

    # ═══════════════════════════════════════════════════════
    # 资金流（万元 -> 需要与 daily.amount 千元对齐时换算）
    # ═══════════════════════════════════════════════════════
    mf_pit = _mf_on_or_before(moneyflow_by_date, dates[pit_i])
    mf_prev = _mf_on_or_before(moneyflow_by_date, dates[pit_i - 1]) if pit_i >= 1 else {}

    def _mf(key: str, d: dict) -> float:
        return _safe_float(d.get(key), 0.0)

    net_mf = _mf("net_mf_amount", mf_pit)
    buy_elg = _mf("buy_elg_amount", mf_pit)
    sell_elg = _mf("sell_elg_amount", mf_pit)
    buy_lg = _mf("buy_lg_amount", mf_pit)
    sell_lg = _mf("sell_lg_amount", mf_pit)

    feats["net_mf_amount"] = net_mf
    # moneyflow 万元 -> 元：net_mf * 10000；daily.amount 千元 -> 元：amount * 1000
    t1_amount_yuan = amounts[pit_i] * 1000 if pit_i >= 0 and amounts[pit_i] else 0.0
    if t1_amount_yuan > 0:
        feats["net_mf_ratio"] = (net_mf * 10000.0) / t1_amount_yuan
        feats["mf_pct"] = net_mf / amounts[pit_i]  # 万元 / 千元 = 10 * ratio，保留便于解释
    feats["buy_elg_ratio"] = buy_elg / (buy_elg + sell_elg) if (buy_elg + sell_elg) > 0 else 0.5
    feats["buy_lg_ratio"] = buy_lg / (buy_lg + sell_lg) if (buy_lg + sell_lg) > 0 else 0.5
    feats["mf_net"] = net_mf
    net_mf_prev = _mf("net_mf_amount", mf_prev)
    denom = abs(net_mf_prev) if net_mf_prev else 1.0
    feats["mf_accel"] = (net_mf - net_mf_prev) / denom

    # ═══════════════════════════════════════════════════════
    # 板块/概念动量
    # ═══════════════════════════════════════════════════════
    cm = concept_momentum or {}
    feats["sector_heat"] = _safe_float(cm.get("ret1_avg"), 0.0)
    # sector_rank：概念数越多→越热门
    feats["sector_rank"] = math.tanh(feats.get("n_concepts", 0) / 50.0)
    feats["n_concepts"] = _safe_float(cm.get("n_concepts"), 0.0)

    # ═══════════════════════════════════════════════════════
    # 龙虎榜 PIT 特征（T-1 日上榜数据）
    # ═══════════════════════════════════════════════════════
    tl_pit = _top_on_or_before(top_list_by_date, dates[pit_i]) if top_list_by_date else None
    ti_pit = _top_on_or_before(top_inst_by_date, dates[pit_i]) if top_inst_by_date else None

    if isinstance(tl_pit, dict):
        feats["dt_is_listed"] = 1.0
        net_amount = _safe_float(tl_pit.get("net_amount"), 0.0)
        amount = _safe_float(tl_pit.get("amount"), 0.0)
        l_buy = _safe_float(tl_pit.get("l_buy"), 0.0)
        l_amount = _safe_float(tl_pit.get("l_amount"), 0.0)
        feats["dt_net_amount"] = net_amount
        feats["dt_net_rate"] = _safe_float(tl_pit.get("net_rate"), 0.0)
        feats["dt_l_buy_ratio"] = l_buy / l_amount if l_amount > 0 else 0.0

    if ti_pit is not None:
        ti_rows = ti_pit if isinstance(ti_pit, list) else [ti_pit]

        inst_net_buy = 0.0
        hot_net_buy = 0.0
        inst_sell = 0.0
        for row in ti_rows:
            if not isinstance(row, dict):
                continue
            exalter = str(row.get("exalter", ""))
            net_buy = _safe_float(row.get("net_buy"), 0.0)
            if "机构" in exalter or "专用" in exalter:
                inst_net_buy += net_buy
            else:
                hot_net_buy += net_buy
            if net_buy < 0 and ("机构" in exalter or "专用" in exalter):
                inst_sell += abs(net_buy)

        feats["dt_n_exalter"] = float(len(ti_rows))
        feats["dt_inst_net_buy"] = inst_net_buy
        feats["dt_hot_net_buy"] = hot_net_buy
        amount = _safe_float(tl_pit.get("amount"), 0.0) if isinstance(tl_pit, dict) else 0.0
        if amount > 0:
            feats["dt_inst_sell_ratio"] = inst_sell / amount

    # ═══════════════════════════════════════════════════════
    # 竞价特征（如提供，Tushare stk_auction amount 单位为元，vol 单位为股）
    # ═══════════════════════════════════════════════════════
    auc = _mf_on_or_before(auction_by_date, dates[i]) if i >= 0 else {}
    auc_amount = _safe_float(auc.get("amount"), 0.0)  # 元
    auc_vol = _safe_float(auc.get("vol"), 0.0)        # 股
    feats["auc_amount"] = auc_amount
    feats["auc_vol"] = auc_vol
    if feats["avg_amount_5d"] > 0:
        feats["auc_amt_ratio"] = auc_amount / feats["avg_amount_5d"]
    t1_vol = _safe_float(rows[pit_i].get("vol"), 0.0) if pit_i >= 0 else 0.0
    if t1_vol > 0:
        feats["auc_vol_ratio"] = auc_vol / t1_vol

    # ═══════════════════════════════════════════════════════
    # 日内分时特征（T-1 jvQuant 分钟数据聚合）
    # ═══════════════════════════════════════════════════════
    iday = _mf_on_or_before(intraday_by_date, dates[pit_i]) if pit_i >= 0 else {}
    if iday:
        vwap = _safe_float(iday.get("vwap"), 0.0)
        close_id = _safe_float(iday.get("close"), 0.0)
        high_id = _safe_float(iday.get("high"), 0.0)
        low_id = _safe_float(iday.get("low"), 0.0)
        amount_est = _safe_float(iday.get("amount_est"), 0.0)
        if vwap > 0 and close_id > 0:
            feats["id_vwap_dev"] = close_id / vwap - 1.0
        if low_id > 0 and high_id > 0:
            feats["id_range"] = high_id / low_id - 1.0
        feats["id_morning_vol_ratio"] = _safe_float(iday.get("morning_vol_ratio"), 0.5)
        feats["id_afternoon_strength"] = _safe_float(iday.get("afternoon_strength"), 1.0)
        feats["id_tail_vol_ratio"] = _safe_float(iday.get("tail_vol_ratio"), 0.1)
        if feats["avg_amount_5d"] > 0:
            # amount_est 与 avg_amount_5d 均为元，直接求比率
            feats["id_amount_ratio"] = amount_est / feats["avg_amount_5d"]

    return feats


__all__ = ["build_pit_features"]
