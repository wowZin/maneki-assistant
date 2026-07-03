#!/usr/bin/env python3
"""首板预测因子 — 专门预测首次涨停"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from scripts.tu_share import call_tushare
from plays.limit_up.utils import safe_float


def _call(api, params, fields=""):
    r = call_tushare(api, params, fields)
    items = r.get("data", {}).get("items", [])
    cols = r.get("data", {}).get("fields", [])
    return [dict(zip(cols, row)) for row in items]


def score_first_board(code: str, trade_date: str | None = None) -> tuple:
    """首板预测评分 (0-100)

    竞价异动30 + 分歧转一致30 + 资金抢筹20 + 板块共振20
    """
    today = trade_date or datetime.now().strftime("%Y%m%d")
    code_short = code.replace(".SH", "").replace(".SZ", "")
    score = 0.0
    parts = []

    # ── 数据 ──
    d = _call("daily", {"ts_code": code, "start_date": today, "end_date": today},
              "open,high,low,close,pre_close,pct_chg,vol,amount")
    dr = d[0] if d else {}
    pct = safe_float(dr.get("pct_chg", 0))
    pre_c = safe_float(dr.get("pre_close", 0))
    op = safe_float(dr.get("open", 0))
    open_pct = ((op / pre_c) - 1) * 100 if pre_c > 0 else 0

    dy = _call("daily", {"ts_code": code, "start_date": (datetime.strptime(today,"%Y%m%d")-timedelta(days=1)).strftime("%Y%m%d"),
                         "end_date": today}, "vol,amount")
    yr = dy[0] if len(dy) > 1 else (dy[0] if len(dy) == 1 else {})
    y_vol = safe_float(yr.get("vol", 0))

    db = _call("daily_basic", {"ts_code": code, "trade_date": today},
               "turnover_rate,turnover_rate_f,volume_ratio,circ_mv")
    dbr = db[0] if db else {}
    turnover = safe_float(dbr.get("turnover_rate_f", 0)) or safe_float(dbr.get("turnover_rate", 0))
    vol_ratio = safe_float(dbr.get("volume_ratio", 0))

    auc = _call("stk_auction", {"ts_code": code, "trade_date": today},
                "vol,price,amount,turnover_rate,pre_close")
    aur = auc[0] if auc else {}

    mf = _call("moneyflow", {"ts_code": code, "trade_date": today},
               "buy_elg_amount,sell_elg_amount,buy_lg_amount,sell_lg_amount,net_mf_amount")
    mfr = mf[0] if mf else {}

    # 概念
    from plays.limit_up.pipeline import _HOT_CONCEPT_CACHE, _HOT_LIST_ITEMS
    concepts = _HOT_CONCEPT_CACHE.get(code_short, []) if _HOT_CONCEPT_CACHE else []

    # ═══ 1. 竞价异动 30分 ═══
    d1 = 0.0
    d1r = []
    if open_pct >= 3:
        d1 += 15; d1r.append(f"高开{open_pct:.1f}%+15")
    elif open_pct >= 1:
        d1 += 8; d1r.append(f"高开{open_pct:.1f}%+8")
    elif open_pct < -1:
        d1 -= 5; d1r.append(f"低开{open_pct:.1f}%-5")

    auc_vol = safe_float(aur.get("vol", 0))
    if y_vol > 0:
        ar = auc_vol / y_vol * 100
        if ar > 5:
            d1 += 10; d1r.append(f"竞价活跃{ar:.1f}%+10")
        elif ar > 2:
            d1 += 5; d1r.append(f"竞价有量{ar:.1f}%+5")

    auc_tr = safe_float(aur.get("turnover_rate", 0))
    if auc_tr > 0.5:
        d1 += 5; d1r.append(f"竞价换手{auc_tr:.2f}%+5")
    d1 = max(-10, min(30, d1)); score += d1
    parts.append(f"[竞价{d1:.0f}] {'; '.join(d1r) if d1r else '无数据'}")

    # ═══ 2. 分歧转一致 30分 ═══
    d2 = 0.0; d2r = []
    if dr and dbr:
        if 0 < pct < 5 and turnover > 10:
            d2 += 10; d2r.append(f"分歧活跃(换手{turnover:.1f}%)+10")
        elif 0 < pct < 5 and turnover > 5:
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
    d2 = max(-10, min(30, d2)); score += d2
    parts.append(f"[一致{d2:.0f}] {'; '.join(d2r) if d2r else '无数据'}")

    # ═══ 3. 资金抢筹 20分 ═══
    d3 = 0.0; d3r = []
    if mfr:
        mn = (safe_float(mfr.get("buy_elg_amount",0)) - safe_float(mfr.get("sell_elg_amount",0))
              + safe_float(mfr.get("buy_lg_amount",0)) - safe_float(mfr.get("sell_lg_amount",0)))
        nm = safe_float(mfr.get("net_mf_amount",0))
        if mn > 0:
            d3 += 10; d3r.append(f"主力净买{mn/10000:.0f}万+10")
        elif mn < 0:
            d3 -= 5; d3r.append(f"主力净卖{abs(mn)/10000:.0f}万-5")
        md = nm - mn  # 中单净额 = 总净额 - 主力净额
        if md > 0:
            d3 += 10; d3r.append(f"中单净买{md/10000:.0f}万+10")
        elif md < 0:
            d3 -= 5; d3r.append(f"中单净卖{abs(md)/10000:.0f}万-5")
    else:
        d3r.append("无资金数据")
    d3 = max(-15, min(20, d3)); score += d3
    parts.append(f"[资金{d3:.0f}] {'; '.join(d3r) if d3r else ''}")

    # ═══ 4. 板块共振 20分 ═══
    d4 = 0.0; d4r = []
    if concepts and _HOT_LIST_ITEMS:
        # 今日涨停概念
        ul_cpts = set()
        for s in _HOT_LIST_ITEMS:
            if safe_float(s.get("pct_chg",0)) >= 9.5:
                for t in s.get("tag",{}).get("concept_tag",[]):
                    ul_cpts.add(t)
        matched = [c for c in concepts if c in ul_cpts]
        if matched:
            d4 += 15; d4r.append(f"概念涨停+15")
        else:
            hot = sum(1 for s in _HOT_LIST_ITEMS if safe_float(s.get("pct_chg",0)) >= 5
                      and any(c in concepts for c in s.get("tag",{}).get("concept_tag",[])))
            if hot >= 3:
                d4 += 8; d4r.append(f"板块升温({hot}只>5%)+8")
            elif hot >= 1:
                d4 += 3; d4r.append(f"板块异动({hot}只)+3")
            else:
                d4r.append("概念无热度")
    else:
        d4r.append("无概念数据")
    d4 = max(0, min(20, d4)); score += d4
    parts.append(f"[板块{d4:.0f}] {'; '.join(d4r) if d4r else ''}")

    fs = max(0, min(100, round(score, 1)))
    return fs, " | ".join(parts)
