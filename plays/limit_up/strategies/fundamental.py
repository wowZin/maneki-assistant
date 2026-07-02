#!/usr/bin/env python3
"""基本面分析  — 催化剂导向评分 (Catalyst-focused, not Quality-focused)

Redesign rationale (based on backtest finding d=-0.26 for V1):
- V1 rewarded "quality" stocks (high ROE, low debt, good valuation) → these rarely surge
-  rewards "catalyst potential" (earnings surprise, small cap, accumulation, concept breadth)
- Market cap uses log-transform (aligned with backtest: circ_mv_log d=+0.47)
- Vetoes reduced: 5→2, removing goodwill/debt/non-recurring filters

四维度:
  1. 小市值动量 (35%): 流通市值对数变换 → 小盘溢价
  2. 业绩突变 (30%): 扣非净利润增速 + 困境反转
  3. 筹码集中 (20%): 股东户数下降 + 涨停基因
  4. 题材广度 (15%): 概念板块数量 → 事件催化剂
"""

import math
import sys
from pathlib import Path
from datetime import datetime, timedelta

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_DIR))
sys.path.insert(0, str(PROJECT_DIR / "scripts"))

from scripts.tu_share import call_tushare  # noqa: E402
from plays.limit_up.utils import safe_float, safe_int_none  # noqa: E402

# ── 模块级概念标签缓存（避免逐股重复HTTP请求） ──
_CONCEPT_COUNT_CACHE: dict[str, int] = {}
_CONCEPT_CACHE_LOADED = False


def _load_concept_tags_bulk() -> dict[str, int]:
    """批量加载同花顺热门榜概念标签 → {code_short: concept_count}

    仅在首次调用时请求一次 THS 热榜接口，后续调用直接读缓存。
    """
    global _CONCEPT_COUNT_CACHE, _CONCEPT_CACHE_LOADED
    if _CONCEPT_CACHE_LOADED:
        return _CONCEPT_COUNT_CACHE

    _CONCEPT_CACHE_LOADED = True
    try:
        from scripts.ths_client import get_ths_client  # noqa: E402
        ths = get_ths_client()
        if ths.has_cookie:
            all_tags = ths.get_concept_tags()  # {code_short: [concept_name, ...]}
            for cs, tags in all_tags.items():
                _CONCEPT_COUNT_CACHE[cs] = len(tags)
    except Exception:
        pass

    return _CONCEPT_COUNT_CACHE


def _get_concept_count(code: str) -> int:
    """获取个股的概念标签数量

    数据源优先级:
      1. 同花顺热门榜概念标签 (批量缓存，仅覆盖~100只热门股)
      2. 申万行业 (stock_basic) — 至少有1个行业 = 1个概念
      3. 兜底: 0
    """
    code_short = code.replace(".SH", "").replace(".SZ", "")

    # 优先从 THS 批量缓存读取
    cache = _load_concept_tags_bulk()
    if code_short in cache:
        return cache[code_short]

    # 降级: 从 Tushare stock_basic 获取行业（默认1个概念）
    try:
        resp = call_tushare("stock_basic", {"ts_code": code}, "industry")
        items = resp.get("data", {}).get("items", [])
        if items and items[0] and items[0][0]:
            _CONCEPT_COUNT_CACHE[code_short] = 1
            return 1
    except Exception:
        pass

    _CONCEPT_COUNT_CACHE[code_short] = 0
    return 0


def score_fundamental(code: str, trade_date: str | None = None) -> tuple[int | float, str]:
    """基本面评分 v2：大盘成长 + 概念催化导向 (0-100)

    因子挖掘发现：涨停股在流通市值、PB、PE 上均呈正向 IC，
    说明"大盘+高估值成长股+概念丰富"才是基本面端的正确方向。
    本版从"小盘+低估值"修正为"中大盘+成长属性+概念催化"。

    Args:
        code: 带后缀的股票代码，如 "000001.SZ"

    Returns:
        (score, reason): score 0-100, reason 为简要文字说明
    """
    # ═══════════════════════════════════════════════════════════
    # 1. 数据获取
    # ═══════════════════════════════════════════════════════════

    # 1.1 daily_basic: 流通市值 + PB + PE
    circ_mv = 0.0
    pb = 0.0
    pe = 999.0
    try:
        from plays.limit_up.strategies import factor_ctx
        basic = factor_ctx.get_daily_basic(code)
        if basic:
            circ_mv = safe_float(basic.get("circ_mv", 0))  # 万元
            pb = safe_float(basic.get("pb", 0))
            pe = safe_float(basic.get("pe", 999.0))
        else:
            resp = call_tushare("daily_basic", {"ts_code": code}, "circ_mv,pb,pe")
            items = resp.get("data", {}).get("items", [])
            if items:
                flds = resp.get("data", {}).get("fields", [])
                d = dict(zip(flds, items[0]))
                circ_mv = safe_float(d.get("circ_mv", 0))
                pb = safe_float(d.get("pb", 0))
                pe = safe_float(d.get("pe", 999.0))
    except Exception:
        circ_mv, pb, pe = 0.0, 0.0, 999.0

    # 1.2 fina_indicator: 盈利增速（取最新期）
    try:
        resp = call_tushare("fina_indicator", {"ts_code": code},
                            "end_date,dt_netprofit_yoy,n_income,dt_netprofit,or_yoy")
        fina_data = resp.get("data", {})
        fina_fields = fina_data.get("fields", [])
        fina_items = fina_data.get("items", [])
        fina_latest = dict(zip(fina_fields, fina_items[0])) if fina_items else {}
    except Exception:
        fina_latest = {}

    # 1.3 概念标签数量
    concept_count = _get_concept_count(code)

    # 1.4 share_float: 未来解禁检查（60日窗口）
    unlock_ratio_max = 0.0
    unlock_date_str = ""
    try:
        start_dt = trade_date if trade_date else datetime.now()
        end_dt = start_dt + timedelta(days=60)
        resp = call_tushare("share_float", {
            "ts_code": code,
            "start_date": start_dt.strftime("%Y%m%d"),
            "end_date": end_dt.strftime("%Y%m%d")
        }, "float_date,float_share,float_ratio")
        float_items = resp.get("data", {}).get("items", [])
        if float_items:
            flds = resp.get("data", {}).get("fields", [])
            for item in float_items:
                d = dict(zip(flds, item)) if flds else {}
                ratio = safe_float(d.get("float_ratio", 0))
                if ratio > unlock_ratio_max:
                    unlock_ratio_max = ratio
                    unlock_date_str = str(d.get("float_date", ""))
    except Exception:
        pass

    # ═══════════════════════════════════════════════════════════
    # 2. 否决检查
    # ═══════════════════════════════════════════════════════════
    risk_flags = []

    if unlock_ratio_max > 10:
        risk_flags.append(f"未来解禁{unlock_ratio_max:.1f}%({unlock_date_str})")

    profit_yoy = safe_float(fina_latest.get("dt_netprofit_yoy"))
    dt_latest = safe_float(fina_latest.get("dt_netprofit"))
    dt_prev = None  # 不再强制取上期，简化
    ni_latest = safe_float(fina_latest.get("n_income"))

    is_turnaround = (dt_latest is not None and dt_latest > 0 and
                     profit_yoy is not None and profit_yoy < -50 and
                     dt_latest < 0)

    if profit_yoy is not None and profit_yoy < -50:
        if dt_latest is not None and dt_latest < 0 and not is_turnaround:
            risk_flags.append(f"扣非连续亏损(yoy={profit_yoy:.0f}%)")

    if risk_flags:
        return 0, f"否决: {'; '.join(risk_flags)}"

    # ═══════════════════════════════════════════════════════════
    # 3. 四维度评分（修正方向）
    # ═══════════════════════════════════════════════════════════
    factors: dict[str, float] = {}
    reasons: list[str] = []

    # 3.1 市值规模（30%）：中大盘加分，微盘不加分
    cap_score = 0.0
    mv_yi = circ_mv / 10000 if circ_mv else 0
    if mv_yi >= 200:
        cap_score = 1.0
        reasons.append(f"大盘(流通{mv_yi:.0f}亿)+20")
    elif mv_yi >= 100:
        cap_score = 0.85
        reasons.append(f"中大盘(流通{mv_yi:.0f}亿)+17")
    elif mv_yi >= 50:
        cap_score = 0.65
        reasons.append(f"中盘(流通{mv_yi:.0f}亿)+13")
    elif mv_yi >= 20:
        cap_score = 0.40
        reasons.append(f"中小盘(流通{mv_yi:.0f}亿)+8")
    elif mv_yi >= 10:
        cap_score = 0.20
        reasons.append(f"小盘(流通{mv_yi:.0f}亿)+4")
    else:
        cap_score = 0.0
        reasons.append(f"袖珍盘(流通{mv_yi:.1f}亿)+0")
    factors["cap"] = cap_score

    # 3.2 成长估值（25%）：高 PB/PE 作为成长信号
    growth_score = 0.0
    if pe > 50 or pe <= 0:
        growth_score = 1.0
        reasons.append(f"高成长/亏损股(PE={pe:.1f})+15")
    elif pe > 30:
        growth_score = 0.75
        reasons.append(f"成长股(PE={pe:.1f})+11")
    elif pe > 15:
        growth_score = 0.40
        reasons.append(f"均衡估值(PE={pe:.1f})+6")

    if pb > 8:
        growth_score = min(1.0, growth_score + 0.25)
        reasons.append(f"高 PB 成长(PB={pb:.1f})+10")
    elif pb > 5:
        growth_score = min(1.0, growth_score + 0.15)
        reasons.append(f"偏高 PB(PB={pb:.1f})+6")
    elif pb > 3:
        growth_score = min(1.0, growth_score + 0.05)
    factors["growth"] = min(1.0, growth_score)

    # 3.3 业绩突变（25%）：扣非净利润高增或困境反转
    earnings_score = 0.3
    profit_yoy_val = safe_float(fina_latest.get("dt_netprofit_yoy"))
    or_yoy = safe_float(fina_latest.get("or_yoy"))
    if profit_yoy_val is not None and profit_yoy_val != 0:
        if profit_yoy_val > 100:
            earnings_score = 1.0
            reasons.append(f"扣非暴增+{profit_yoy_val:.0f}%")
        elif profit_yoy_val > 50:
            earnings_score = 0.8
            reasons.append(f"扣非高增+{profit_yoy_val:.0f}%")
        elif profit_yoy_val > 20:
            earnings_score = 0.6
            reasons.append(f"扣非增长+{profit_yoy_val:.0f}%")
        elif profit_yoy_val > 0:
            earnings_score = 0.45
        elif profit_yoy_val < -30:
            earnings_score = 0.0
            reasons.append(f"扣非下滑{profit_yoy_val:.0f}%")
    if or_yoy is not None and or_yoy > 30:
        earnings_score = min(1.0, earnings_score + 0.15)
        reasons.append(f"营收+{or_yoy:.0f}%")
    factors["earnings"] = earnings_score

    # 3.4 题材广度（20%）：概念数量
    concept_score = 0.0
    if concept_count >= 10:
        concept_score = 1.0
        reasons.append(f"概念丰富({concept_count}个)")
    elif concept_count >= 7:
        concept_score = 0.85
        reasons.append(f"概念较多({concept_count}个)")
    elif concept_count >= 5:
        concept_score = 0.65
        reasons.append(f"{concept_count}个概念")
    elif concept_count >= 3:
        concept_score = 0.40
        reasons.append(f"{concept_count}个概念")
    elif concept_count >= 1:
        concept_score = 0.20
    else:
        concept_score = 0.0
        reasons.append("概念缺失")
    factors["concept"] = concept_score

    # ═══════════════════════════════════════════════════════════
    # 4. 综合评分
    # ═══════════════════════════════════════════════════════════
    # v2.1 权重：子因子 IC 显示 earnings_yield / book_yield 为负，
    # 业绩增速噪音大；强化市值+成长估值+概念数量。
    weights = {
        "cap": 0.35,
        "growth": 0.30,
        "earnings": 0.10,
        "concept": 0.25,
    }
    base_score = sum(factors[k] * weights[k] for k in factors) * 100

    # 共振加分
    bonus = 0.0
    if factors.get("cap", 0) >= 0.65 and factors.get("growth", 0) >= 0.6:
        bonus += 10
        reasons.append("共振:大盘+成长+10")
    if factors.get("concept", 0) >= 0.65 and factors.get("earnings", 0) >= 0.6:
        bonus += 8
        reasons.append("共振:概念+业绩+8")

    final_score = min(100.0, base_score + bonus)

    if final_score >= 75:
        level = "高"
    elif final_score >= 55:
        level = "中"
    elif final_score >= 35:
        level = "低"
    else:
        level = "无"

    seen: set[str] = set()
    unique_reasons: list[str] = []
    for r in reasons:
        if r not in seen:
            seen.add(r)
            unique_reasons.append(r)

    reason_str = f"[{level}] " + "; ".join(unique_reasons[:6]) if unique_reasons else f"[{level}] 数据不足"

    return round(final_score, 1), reason_str
