#!/usr/bin/env python3
"""短线博弈评分 v3 — 四因子：涨停基因 + 开盘博弈 + 位置波动 + 连板溢价。

签名: score_shortterm(code: str, trade_date: str | None = None) -> tuple
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from scripts.tu_share import call_tushare
from plays.limit_up.utils import safe_float


# ── 日期工具 ─────────────────────────────────────

_TODAY_OVERRIDE: str | None = None


def _today() -> str:
    if _TODAY_OVERRIDE:
        return _TODAY_OVERRIDE
    return datetime.now().strftime("%Y%m%d")


def _set_trade_date(trade_date: str | None):
    global _TODAY_OVERRIDE
    _TODAY_OVERRIDE = trade_date


def _to_df(api: str, params: dict, fields: str = ""):
    r = call_tushare(api, params, fields)
    items = r.get("data", {}).get("items", [])
    cols = r.get("data", {}).get("fields", [])
    return [dict(zip(cols, row)) for row in items]


def _get_daily(code: str, days: int = 30) -> list[dict]:
    end = _today()
    start = (datetime.strptime(end, "%Y%m%d") - timedelta(days=days * 2)).strftime("%Y%m%d")
    rows = _to_df("daily", {"ts_code": code, "start_date": start, "end_date": end},
                  "trade_date,open,high,low,close,pre_close,pct_chg,vol,amount")
    rows.sort(key=lambda x: x.get("trade_date", ""), reverse=True)
    return rows


def _get_limit_dates(code: str, days: int) -> list[str]:
    """近 N 日涨停日期列表。"""
    end = _today()
    start = (datetime.strptime(end, "%Y%m%d") - timedelta(days=days)).strftime("%Y%m%d")
    rows = _to_df("limit_list_d", {
        "ts_code": code,
        "start_date": start,
        "end_date": end,
        "limit_type": "U",
    }, "trade_date,limit_times")
    return sorted([str(r.get("trade_date", "")) for r in rows if r.get("trade_date")], reverse=True)


def _get_auction(code: str) -> dict:
    """获取当日集合竞价数据。"""
    rows = _to_df("stk_auction", {"ts_code": code, "trade_date": _today()},
                  "vol,price,amount,turnover_rate,pre_close")
    return rows[0] if rows else {}


def _extract_pit_features(code: str) -> dict:
    """从日线提取 PIT 特征：position_20d, trailing_10, std10。"""
    rows = _get_daily(code, 30)
    feats = {
        "position_20d": 0.5,
        "trailing_10": 0.0,
        "pct_chg_std_10d": 0.0,
        "max_pct_chg_5d": 0.0,
    }
    if len(rows) < 10:
        return feats

    closes = [safe_float(r.get("close", 0)) for r in rows[:20] if safe_float(r.get("close", 0)) > 0]
    highs = [safe_float(r.get("high", 0)) for r in rows[:20] if safe_float(r.get("high", 0)) > 0]
    lows = [safe_float(r.get("low", 0)) for r in rows[:20] if safe_float(r.get("low", 0)) > 0]
    pcts = [safe_float(r.get("pct_chg", 0)) for r in rows[:10]]

    if len(closes) >= 5 and highs and lows:
        h20, l20, c0 = max(highs), min(lows), closes[0]
        if h20 > l20:
            feats["position_20d"] = (c0 - l20) / (h20 - l20)

    if len(closes) >= 10 and closes[9] > 0:
        feats["trailing_10"] = (closes[0] / closes[9] - 1) * 100

    if len(pcts) >= 5:
        feats["pct_chg_std_10d"] = float(__import__("numpy").std(pcts, ddof=0)) if len(pcts) >= 2 else 0.0
        feats["max_pct_chg_5d"] = max(pcts[:5])

    return feats


# ── 主评分 ───────────────────────────────────────

def score_shortterm(code: str, fundflow_data: dict = None, trade_date: str | None = None) -> tuple:
    """短线博弈评分 (0-100)

    四因子：涨停基因 30 + 开盘博弈 25 + 位置波动 25 + 连板溢价 20
    """
    old = _TODAY_OVERRIDE
    _set_trade_date(trade_date)

    try:
        score = 0.0
        reasons = []

        # ── 数据 ──
        daily_rows = _get_daily(code, 30)
        if not daily_rows:
            return 0, "无日线数据"

        dr = daily_rows[0]
        pct = safe_float(dr.get("pct_chg", 0))
        pre_c = safe_float(dr.get("pre_close", 0))
        op = safe_float(dr.get("open", 0))
        open_pct = ((op / pre_c) - 1) * 100 if pre_c > 0 else 0

        feats = _extract_pit_features(code)
        position = feats["position_20d"]
        t10 = feats["trailing_10"]
        std10 = feats["pct_chg_std_10d"]

        limit_20d_dates = _get_limit_dates(code, 20)
        limit_60d_dates = _get_limit_dates(code, 60)
        count20 = len(limit_20d_dates)
        count60 = len(limit_60d_dates)

        auc = _get_auction(code)
        auc_vol_ratio = safe_float(auc.get("turnover_rate", 0)) / 100  # stk_auction.turnover_rate 是%形式
        auc_turnover = safe_float(auc.get("turnover_rate", 0))

        # ── 1. 涨停基因 30分 ──
        d1 = 0.0
        if count20 >= 3:
            d1 += 15
        elif count20 == 2:
            d1 += 10
        elif count20 == 1:
            d1 += 5
        if count60 >= 5:
            d1 += 10
        elif 3 <= count60 <= 4:
            d1 += 6
        elif 1 <= count60 <= 2:
            d1 += 3

        # 断板反包：间隔2-9日再次涨停
        if len(limit_60d_dates) >= 2:
            for i in range(len(limit_60d_dates) - 1):
                d1_date = datetime.strptime(limit_60d_dates[i], "%Y%m%d")
                d2_date = datetime.strptime(limit_60d_dates[i + 1], "%Y%m%d")
                gap = (d1_date - d2_date).days
                if 2 <= gap <= 9:
                    d1 += 8
                    break

        d1 = min(30, d1)
        score += d1
        reasons.append(f"涨停基因{d1:.0f}(20日{count20}次/60日{count60}次)")

        # ── 2. 开盘博弈 25分 ──
        d2 = 0.0
        if open_pct >= 5:
            d2 += 15
        elif open_pct >= 3:
            d2 += 10
        elif open_pct >= 1:
            d2 += 5
        elif open_pct < -1:
            d2 -= 5

        # 竞价量比：用 auction turnover_rate 作为代理（若数据为百分比则除以100）
        if auc_vol_ratio >= 3:
            d2 += 8
        elif auc_vol_ratio >= 1.5:
            d2 += 4
        if auc_turnover >= 0.5:
            d2 += 5
        elif auc_turnover >= 0.2:
            d2 += 2

        d2 = max(-10, min(25, d2))
        score += d2
        reasons.append(f"开盘博弈{d2:.0f}(跳空{open_pct:.1f}%/竞价换手{auc_turnover:.2f}%)")

        # ── 3. 位置波动 25分 ──
        d3 = 0.0
        if 0.30 <= position <= 0.70:
            d3 += 10
        elif 0.20 <= position < 0.30 or 0.70 < position <= 0.80:
            d3 += 5
        if position > 0.85:
            d3 -= 5

        if 5 <= t10 <= 25:
            d3 += 8
        elif t10 > 35:
            d3 -= 5

        if std10 > 4:
            d3 += 7
        elif std10 > 2:
            d3 += 4

        d3 = max(-10, min(25, d3))
        score += d3
        reasons.append(f"位置波动{d3:.0f}(pos{position:.2f}/t10{t10:.1f}%/std{std10:.1f})")

        # ── 4. 连板溢价 20分 ──
        d4 = 0.0
        yesterday_limit = False
        if len(limit_20d_dates) >= 1:
            y_str = (datetime.strptime(_today(), "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")
            if limit_20d_dates[0] == y_str:
                yesterday_limit = True

        if yesterday_limit:
            if open_pct > 0:
                d4 += 12
            else:
                d4 += 8

        # 当前连板数：从 limit_step 取
        try:
            step_rows = _to_df("limit_step", {"ts_code": code, "trade_date": _today()}, "nums")
            continuity = safe_float(step_rows[0].get("nums", 0)) if step_rows else 0
        except Exception:
            continuity = 0

        if continuity == 2:
            d4 += 10
        elif continuity == 3:
            d4 += 15
        elif continuity >= 4:
            d4 += 12

        # 涨停基因动量 + 技术确认（从 technical 维度取分，避免重复计算复杂逻辑）
        # 此处仅用 limit_up_count 已涵盖，不额外调用 technical

        # 追高削弱
        if t10 > 35 or position > 0.85:
            d4 *= 0.7

        d4 = max(0, min(20, d4))
        score += d4
        reasons.append(f"连板溢价{d4:.0f}(昨日涨停{int(yesterday_limit)}/连板{int(continuity)}板)")

    finally:
        _set_trade_date(old)

    fs = max(0, min(100, round(score, 1)))
    return fs, " | ".join(reasons)


if __name__ == "__main__":
    import sys
    code = sys.argv[1] if len(sys.argv) > 1 else "000001.SZ"
    print(score_shortterm(code))
