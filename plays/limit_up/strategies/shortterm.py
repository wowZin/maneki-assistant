#!/usr/bin/env python3
"""
短线博弈评分 — 聚焦首板预测

相比旧版（偏重连板基因），新版聚焦首板场景：
  竞价异动25 + 分歧转一致25 + 首板基因20 + 资金共振20 + 连板溢价10

签名: score_shortterm(code: str, fundflow_data=None, trade_date=None) -> tuple
"""
import sys, json, math
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from scripts.tu_share import call_tushare
from plays.limit_up.utils import safe_float


# ── 日期工具 ─────────────────────────────────────

def _today() -> str:
    if _TODAY_OVERRIDE:
        return _TODAY_OVERRIDE
    return datetime.now().strftime("%Y%m%d")

_TODAY_OVERRIDE: str | None = None


def _query_date() -> str:
    if _TODAY_OVERRIDE:
        return _TODAY_OVERRIDE
    from scripts.tu_share import get_last_trade_date_with_data
    return get_last_trade_date_with_data()


# ── 数据获取 ─────────────────────────────────────

def _to_df(api: str, params: dict, fields: str = ""):
    r = call_tushare(api, params, fields)
    items = r.get("data", {}).get("items", [])
    cols = r.get("data", {}).get("fields", [])
    return [dict(zip(cols, row)) for row in items]


def _get_daily(code: str, days=30):
    start = _today()
    start = (datetime.strptime(start, "%Y%m%d") - timedelta(days=days * 2)).strftime("%Y%m%d")
    rows = _to_df("daily", {"ts_code": code, "start_date": start, "end_date": _query_date()},
                  "trade_date,open,high,low,close,pre_close,pct_chg,vol,amount")
    rows.sort(key=lambda x: x.get("trade_date", ""), reverse=True)
    return rows


# ── 主评分 ───────────────────────────────────────

def score_shortterm(code: str, fundflow_data: dict = None, trade_date: str | None = None) -> tuple:
    """短线博弈评分 (0-100)

    竞价异动25 + 分歧转一致25 + 首板基因20 + 资金共振20 + 连板溢价10
    """
    global _TODAY_OVERRIDE
    if trade_date:
        _old = _TODAY_OVERRIDE
        _TODAY_OVERRIDE = trade_date
    else:
        _old = None

    today = _today()
    code_short = code.replace(".SH", "").replace(".SZ", "")
    score = 0.0
    parts = []
    reasons = []

    try:
        # ── 数据 ──
        dr = _get_daily(code)[0] if _get_daily(code) else {}
        pct = safe_float(dr.get("pct_chg", 0))
        pre_c = safe_float(dr.get("pre_close", 0))
        op = safe_float(dr.get("open", 0))
        open_pct = ((op / pre_c) - 1) * 100 if pre_c > 0 else 0

        # 昨日数据用于量比
        yd_rows = _to_df("daily", {"ts_code": code, "start_date": (datetime.strptime(today,"%Y%m%d")-timedelta(days=1)).strftime("%Y%m%d"), "end_date": today},
                         "vol,amount")
        y_vol = safe_float(yd_rows[1].get("vol", 0)) if len(yd_rows) >= 2 else 0

        db = _to_df("daily_basic", {"ts_code": code, "trade_date": today},
                    "turnover_rate,turnover_rate_f,volume_ratio,circ_mv")
        dbr = db[0] if db else {}
        turnover = safe_float(dbr.get("turnover_rate_f", 0)) or safe_float(dbr.get("turnover_rate", 0))
        vol_ratio = safe_float(dbr.get("volume_ratio", 0))

        auc = _to_df("stk_auction", {"ts_code": code, "trade_date": today},
                     "vol,price,amount,turnover_rate,pre_close")
        aur = auc[0] if auc else {}

        mf = _to_df("moneyflow", {"ts_code": code, "trade_date": today},
                    "buy_elg_amount,sell_elg_amount,buy_lg_amount,sell_lg_amount,net_mf_amount")
        mfr = mf[0] if mf else {}

        # ── 1. 竞价异动 25分 ──
        d1 = 0.0; d1r = []
        if open_pct >= 3:
            d1 += 15; d1r.append(f"高开{open_pct:.1f}%+15")
        elif open_pct >= 1:
            d1 += 8; d1r.append(f"高开{open_pct:.1f}%+8")
        elif open_pct < -1:
            d1 -= 5; d1r.append(f"低开{open_pct:.1f}%-5")

        auc_vol = safe_float(aur.get("vol", 0))
        if y_vol > 0:
            auc_ratio = auc_vol / y_vol * 100
            if auc_ratio > 5:
                d1 += 10; d1r.append(f"竞价活跃{auc_ratio:.1f}%+10")
            elif auc_ratio > 2:
                d1 += 5; d1r.append(f"竞价有量{auc_ratio:.1f}%+5")
        auc_tr = safe_float(aur.get("turnover_rate", 0))
        if auc_tr > 0.5:
            d1 += 5; d1r.append(f"竞价换手{auc_tr:.2f}%+5")
        # 如果是今日无数据（非交易日/未开盘），d1 保持 0
        d1 = max(-10, min(25, d1))
        score += d1; parts.append(f"[竞价{d1:.0f}] {'; '.join(d1r) if d1r else '一般'}")
        reasons.append(f"竞价{d1:.0f}")

        # ── 2. 分歧转一致 25分 ──
        d2 = 0.0; d2r = []
        if dr:
            if 0 < pct < 5 and turnover > 8:
                d2 += 10; d2r.append(f"分歧活跃(换手{turnover:.1f}%)+10")
            elif 0 < pct < 5 and turnover > 3:
                d2 += 5; d2r.append(f"温和分歧(换手{turnover:.1f}%)+5")
            if mfr:
                mn = (safe_float(mfr.get("buy_elg_amount",0)) - safe_float(mfr.get("sell_elg_amount",0))
                      + safe_float(mfr.get("buy_lg_amount",0)) - safe_float(mfr.get("sell_lg_amount",0)))
                if mn > 0:
                    d2 += 10; d2r.append(f"主力净+{mn/10000:.0f}万+10")
            if vol_ratio > 1.5 and pct >= 7:
                d2 += 10; d2r.append(f"放量冲板(量比{vol_ratio:.1f})+10")
            elif vol_ratio > 1.0 and pct >= 5:
                d2 += 5; d2r.append(f"温和推升(量比{vol_ratio:.1f})+5")
        d2 = max(-10, min(25, d2))
        score += d2; parts.append(f"[一致{d2:.0f}] {'; '.join(d2r) if d2r else '无数据'}")
        reasons.append(f"一致{d2:.0f}")

        # ── 3. 首板基因 20分 ──
        d3 = 0.0; d3r = []
        if dr:
            # 放量突破（量比+涨幅）
            if vol_ratio > 2.0 and pct >= 5:
                d3 += 10; d3r.append(f"放量突破(量比{vol_ratio:.1f})+10")
            elif vol_ratio > 1.5 and pct >= 3:
                d3 += 5; d3r.append(f"温和放量(量比{vol_ratio:.1f})+5")
            # 价格收敛突破：前N日振幅收敛+今日放量
            daily_rows = _get_daily(code, 10)
            if len(daily_rows) >= 5:
                prev_high = max(safe_float(r.get("high",0)) for r in daily_rows[1:6])
                prev_low = min(safe_float(r.get("low",0)) for r in daily_rows[1:6])
                prev_close = safe_float(daily_rows[1].get("close",0))
                if prev_close > 0 and prev_high > prev_low:
                    amp = (prev_high - prev_low) / prev_close * 100
                    if amp < 5:  # 前5日振幅<5%
                        d3 += 8; d3r.append(f"收敛突破(前振幅{amp:.1f}%)+8")
                    elif amp < 10:
                        d3 += 3; d3r.append(f"蓄力突破(前振幅{amp:.1f}%)+3")
        d3 = max(0, min(20, d3))
        score += d3; parts.append(f"[基因{d3:.0f}] {'; '.join(d3r) if d3r else '一般'}")
        reasons.append(f"基因{d3:.0f}")

        # ── 4. 资金共振 20分 ──
        d4 = 0.0; d4r = []
        if mfr:
            mn = (safe_float(mfr.get("buy_elg_amount",0)) - safe_float(mfr.get("sell_elg_amount",0))
                  + safe_float(mfr.get("buy_lg_amount",0)) - safe_float(mfr.get("sell_lg_amount",0)))
            nm = safe_float(mfr.get("net_mf_amount",0))
            if mn > 0:
                d4 += 10; d4r.append(f"主力净+{mn/10000:.0f}万+10")
            elif mn < 0:
                d4 -= 5; d4r.append(f"主力净-{abs(mn)/10000:.0f}万-5")
            md = nm - mn
            if md > 0:
                d4 += 10; d4r.append(f"中单净+{md/10000:.0f}万+10")
            elif md < 0:
                d4 -= 5; d4r.append(f"中单净-{abs(md)/10000:.0f}万-5")
        d4 = max(-15, min(20, d4))
        score += d4; parts.append(f"[共振{d4:.0f}] {'; '.join(d4r) if d4r else '无'}")
        reasons.append(f"共振{d4:.0f}")

        # ── 5. 连板溢价 10分 ──
        d5 = 0.0; d5r = []
        ul_rows = _to_df("limit_list_d", {"ts_code": code, "start_date": (datetime.strptime(today,"%Y%m%d")-timedelta(days=30)).strftime("%Y%m%d"),
                                          "end_date": today, "limit_type": "U"},
                         "trade_date,limit_times")
        ul_dates = sorted(set(r.get("trade_date","") for r in ul_rows), reverse=True) if ul_rows else []
        if len(ul_dates) >= 2:
            # 昨天也涨停 = 连板
            y_str = (datetime.strptime(today,"%Y%m%d")-timedelta(days=1)).strftime("%Y%m%d")
            if ul_dates[0] == y_str:
                d5 += 10; d5r.append(f"连板+10")
            else:
                d5 += 3; d5r.append("首板+3")
        elif ul_dates:
            d5 += 3; d5r.append("首板+3")
        else:
            d5r.append("无涨停记录")
        d5 = max(0, min(10, d5))
        score += d5; parts.append(f"[溢价{d5:.0f}] {'; '.join(d5r)}")
        reasons.append(f"溢价{d5:.0f}")

    finally:
        if trade_date:
            _TODAY_OVERRIDE = _old

    fs = max(0, min(100, round(score, 1)))
    return fs, " | ".join(parts)
