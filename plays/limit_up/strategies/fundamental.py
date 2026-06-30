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


def score_fundamental(code: str) -> tuple[int | float, str]:
    """基本面评分：催化剂导向 (0-100)

    从"选好公司"转向"选爆发力"——不奖励稳定高质量的公司，
    而是寻找具备短期爆发催化剂的标的。

    Args:
        code: 带后缀的股票代码，如 "000001.SZ"

    Returns:
        (score, reason): score 0-100, reason 为简要文字说明
    """
    # ═══════════════════════════════════════════════════════════
    # 1. 数据获取 (精简: 6个API vs 旧版12个)
    # ═══════════════════════════════════════════════════════════

    # 1.1 daily_basic: 流通市值
    try:
        resp = call_tushare("daily_basic", {"ts_code": code}, "circ_mv")
        items = resp.get("data", {}).get("items", [])
        if items:
            flds = resp.get("data", {}).get("fields", [])
            d = dict(zip(flds, items[0]))
            circ_mv = safe_float(d.get("circ_mv"))  # 万元
        else:
            circ_mv = 0
    except Exception:
        circ_mv = 0

    # 1.2 fina_indicator: 盈利+财务 (取2期)
    try:
        resp = call_tushare("fina_indicator", {"ts_code": code},
                            "end_date,roe,dt_netprofit_yoy,n_income,dt_netprofit,or_yoy,debt_to_assets")
        fina_data = resp.get("data", {})
        fina_fields = fina_data.get("fields", [])
        fina_items = fina_data.get("items", [])
        fina_latest = dict(zip(fina_fields, fina_items[0])) if fina_items else {}
        fina_prev = dict(zip(fina_fields, fina_items[1])) if len(fina_items) > 1 else {}
    except Exception:
        fina_latest, fina_prev = {}, {}

    # 1.3 stk_holdernumber: 股东户数 (取2期)
    try:
        resp = call_tushare("stk_holdernumber", {"ts_code": code},
                            "ann_date,end_date,holder_num")
        hld = resp.get("data", {})
        hld_fields = hld.get("fields", [])
        hld_items = hld.get("items", [])
        holder_latest = dict(zip(hld_fields, hld_items[0])) if hld_items else {}
        holder_prev = dict(zip(hld_fields, hld_items[1])) if len(hld_items) > 1 else {}
    except Exception:
        holder_latest, holder_prev = {}, {}

    # 1.4 概念标签: 多源降级 (THS热榜 → 申万行业)
    concept_count = _get_concept_count(code)

    # 1.5 share_float: 未来解禁检查 (60日窗口)
    unlock_ratio_max = 0.0
    unlock_date_str = ""
    try:
        start_dt = datetime.now()
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
    # 2. 否决检查 (仅2条，从旧版5条大幅精简)
    # ═══════════════════════════════════════════════════════════
    risk_flags = []
    is_vetoed = False

    # ── 否决1: 未来60日大额解禁(>10%流通盘) ──
    if unlock_ratio_max > 10:
        is_vetoed = True
        risk_flags.append(
            f"未来解禁{unlock_ratio_max:.1f}%({unlock_date_str})"
        )

    # ── 否决2: 连续亏损且无困境反转 ──
    profit_yoy = safe_float(fina_latest.get("dt_netprofit_yoy"))
    dt_latest = safe_float(fina_latest.get("dt_netprofit"))
    dt_prev = safe_float(fina_prev.get("dt_netprofit"))
    ni_latest = safe_float(fina_latest.get("n_income"))
    ni_prev = safe_float(fina_prev.get("n_income"))

    # 困境反转: 最新期(扣非或净利润)转正、上期为负
    is_turnaround = (
        (dt_latest > 0 and dt_prev < 0)
        or (ni_latest > 0 and ni_prev < 0)
    )

    # 连续亏损: 扣非净利同比大幅下降 + 最近2期均为负
    if profit_yoy is not None and profit_yoy < -50:
        if dt_latest < 0 and dt_prev < 0:
            if not is_turnaround:
                is_vetoed = True
                risk_flags.append(
                    f"扣非连续亏损(yoy={profit_yoy:.0f}%)"
                )

    if is_vetoed:
        return 0, f"否决: {'; '.join(risk_flags)}"

    # ═══════════════════════════════════════════════════════════
    # 3. 四维度评分
    # ═══════════════════════════════════════════════════════════
    factors: dict[str, float] = {}
    reasons: list[str] = []

    # ── 3.1 小市值动量 (35%) ──
    # 对数变换: circ_mv (万元) → log10
    # A股分布: 微盘3-10亿(4.5-5.0), 小盘10-50亿(5.0-5.7),
    #           中盘50-200亿(5.7-6.3), 大盘200-1000亿(6.3-7.0)
    # 公式: 1.5 - (log10 - 4.0)*0.4, clamped [0, 1]
    if circ_mv > 0:
        log_mv = math.log10(circ_mv)
        cap_score = max(0.0, min(1.0, 1.5 - (log_mv - 4.0) * 0.4))

        circ_yi = circ_mv / 10000  # 万元→亿元
        if circ_yi < 10:
            cap_label = f"袖珍盘(流通{circ_yi:.1f}亿)"
        elif circ_yi < 50:
            cap_label = f"小盘(流通{circ_yi:.1f}亿)"
        elif circ_yi < 100:
            cap_label = f"中盘(流通{circ_yi:.1f}亿)"
        elif circ_yi < 500:
            cap_label = f"中大盘(流通{circ_yi:.0f}亿)"
        else:
            cap_label = f"大盘(流通{circ_yi:.0f}亿)"
    else:
        cap_score = 0.3   # 数据缺失，中性偏保守
        cap_label = "流通市值未知"

    factors["cap"] = cap_score
    reasons.append(cap_label)

    # ── 3.2 业绩突变 (30%) ──
    earnings_score = 0.4  # 基准
    earnings_reasons: list[str] = []

    # 扣非净利润增速 (核心因子)
    if profit_yoy is not None and profit_yoy != 0:
        if profit_yoy > 100:
            earnings_score += 0.35
            earnings_reasons.append(f"扣非暴增+{profit_yoy:.0f}%")
        elif profit_yoy > 50:
            earnings_score += 0.25
            earnings_reasons.append(f"扣非高增+{profit_yoy:.0f}%")
        elif profit_yoy > 20:
            earnings_score += 0.15
            earnings_reasons.append(f"扣非增长+{profit_yoy:.0f}%")
        elif profit_yoy > 0:
            earnings_score += 0.05
        elif profit_yoy < -30:
            earnings_score -= 0.15
            earnings_reasons.append(f"扣非下降{profit_yoy:.0f}%")

    # 营收增速 (辅助确认)
    rev_yoy = safe_float(fina_latest.get("or_yoy"))
    if rev_yoy is not None and rev_yoy > 30:
        earnings_score += 0.10
        earnings_reasons.append(f"营收+{rev_yoy:.0f}%")
    elif rev_yoy is not None and rev_yoy < -15:
        earnings_score -= 0.05

    # 困境反转加分
    if is_turnaround:
        earnings_score += 0.15
        earnings_reasons.append("困境反转")

    factors["earnings"] = max(0.0, min(1.0, earnings_score))
    reasons.extend(earnings_reasons)

    # ── 3.3 筹码集中 (20%) ──
    chip_score = 0.5  # 基准
    chip_reasons: list[str] = []

    # 股东户数环比变化
    if holder_latest.get("holder_num") and holder_prev.get("holder_num"):
        try:
            h_now = safe_float(holder_latest["holder_num"])
            h_before = safe_float(holder_prev["holder_num"])
            if h_before > 0:
                holder_chg = (h_before - h_now) / h_before
                if holder_chg >= 0.10:
                    chip_score += 0.35
                    chip_reasons.append(
                        f"股东-{holder_chg*100:.1f}%(大幅集中)"
                    )
                elif holder_chg >= 0.05:
                    chip_score += 0.25
                    chip_reasons.append(
                        f"股东-{holder_chg*100:.1f}%(集中)"
                    )
                elif holder_chg >= 0.02:
                    chip_score += 0.10
                    chip_reasons.append(
                        f"股东-{holder_chg*100:.1f}%"
                    )
                elif holder_chg < -0.05:
                    chip_score -= 0.20
                    chip_reasons.append(
                        f"股东+{abs(holder_chg)*100:.1f}%(分散)"
                    )
        except Exception:
            pass

    # 涨停基因已移至短线博弈维度，基本面不重复加分
    # (limit_step API 调用移除，减少重复信号)

    factors["chip"] = max(0.0, min(1.0, chip_score))
    reasons.extend(chip_reasons)

    # ── 3.4 题材广度 (15%) ──
    concept_score = 0.3  # 基准
    concept_reasons: list[str] = []

    if concept_count >= 10:
        concept_score += 0.50
        concept_reasons.append(f"概念丰富({concept_count}个)")
    elif concept_count >= 7:
        concept_score += 0.35
        concept_reasons.append(f"概念较多({concept_count}个)")
    elif concept_count >= 4:
        concept_score += 0.20
        concept_reasons.append(f"{concept_count}个概念")
    elif concept_count >= 2:
        concept_score += 0.08
    elif concept_count == 1:
        pass  # 仅有行业分类，中性不加不减
    else:
        concept_score -= 0.10
        concept_reasons.append("概念缺失")

    factors["concept"] = max(0.0, min(1.0, concept_score))
    reasons.extend(concept_reasons)

    # ═══════════════════════════════════════════════════════════
    # 4. 综合评分
    # ═══════════════════════════════════════════════════════════
    weights = {
        "cap": 0.35,
        "earnings": 0.30,
        "chip": 0.20,
        "concept": 0.15,
    }

    base_score = sum(factors[k] * weights[k] for k in factors) * 100

    # ═══════════════════════════════════════════════════════════
    # 5. 非线性共振加分
    # ═══════════════════════════════════════════════════════════
    bonus = 0.0

    # 共振A: 小盘 + 业绩突变 → 最强催化剂组合
    if factors.get("cap", 0) >= 0.8 and factors.get("earnings", 0) >= 0.7:
        bonus += 12
        reasons.append("共振A:小盘+业绩爆发+12")

    # 共振B: 小盘 + 筹码集中 → 吸筹信号 (降权: 股东数据季度滞后, 与实时资金流矛盾时不应压倒)
    elif factors.get("cap", 0) >= 0.7 and factors.get("chip", 0) >= 0.7:
        bonus += 4
        reasons.append("共振B:小盘+吸筹+4")

    # 共振C: 业绩突变 + 概念丰富 → 事件驱动
    elif factors.get("earnings", 0) >= 0.7 and factors.get("concept", 0) >= 0.6:
        bonus += 6
        reasons.append("共振C:业绩+题材+6")

    # 困境反转独立加分 (已计入 earnings 子维度，此处不重复)

    final_score = min(100.0, base_score + bonus)

    # ═══════════════════════════════════════════════════════════
    # 6. 等级 & 理由整理
    # ═══════════════════════════════════════════════════════════
    if final_score >= 75:
        level = "高"
    elif final_score >= 55:
        level = "中"
    elif final_score >= 35:
        level = "低"
    else:
        level = "无"

    # 去重 (保留顺序，最多6条)
    seen: set[str] = set()
    unique_reasons: list[str] = []
    for r in reasons:
        if r not in seen:
            seen.add(r)
            unique_reasons.append(r)

    reason_str = (
        f"[{level}] " + "; ".join(unique_reasons[:6])
        if unique_reasons
        else f"[{level}] 数据不足"
    )

    return round(final_score, 1), reason_str
