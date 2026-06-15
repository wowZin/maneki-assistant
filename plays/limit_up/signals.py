#!/usr/bin/env python3
"""
涨停信号定义模块 — 离散信号检测器

基于 zt_pattern.py 实证分析结果设计：
- 情绪面(效应量+0.63) > 资金面(+0.35) > 短线博弈(+0.18)
- 基本面(-0.26) 和 技术面(-0.10) 无区分力甚至负相关，不作为信号
- 换手率 2-15% 是涨停股聚集区间
- 量比 1-5 是涨停股聚集区间

每个信号返回: (triggered: bool, confidence: float 0-1, detail: str)
"""

import logging
from datetime import datetime
from typing import Callable, Optional

from plays.limit_up.utils import safe_float  # noqa: E402

log = logging.getLogger(__name__)


# =========================================
# 类型定义
# =========================================

class SignalResult(dict):
    """信号结果: {triggered, confidence, detail}"""
    pass


class SignalContext(dict):
    """信号检测上下文，由 pipeline_v2 构建"""
    pass


# =========================================
# 信号 1: 概念共振 (concept_resonance)
# =========================================

def check_concept_resonance(code: str, ctx: dict) -> dict:
    """检测个股是否处于当日热门涨停概念中。

    逻辑: 个股所属概念中，当日涨停股数 >= 3 -> 触发

    zt_pattern 依据: 情绪面是区分力最强的维度评分(效应量+0.63)，
    而概念共振是情绪面的核心子维度。
    """
    tags = ctx.get("hot_concept_tags", [])
    concept_counts = ctx.get("concept_limit_counts", {})

    if not tags:
        return {"triggered": False, "confidence": 0.0,
                "detail": "无概念标签"}

    best_concept = ""
    best_count = 0
    for tag in tags:
        count = concept_counts.get(tag, 0)
        if count > best_count:
            best_count = count
            best_concept = tag

    if best_count >= 8:
        return {"triggered": True, "confidence": 1.0,
                "detail": "%s %d只涨停 超强概念" % (best_concept, best_count)}
    elif best_count >= 5:
        return {"triggered": True, "confidence": 0.9,
                "detail": "%s %d只涨停 最强概念" % (best_concept, best_count)}
    elif best_count >= 3:
        return {"triggered": True, "confidence": 0.7,
                "detail": "%s %d只涨停 概念共振" % (best_concept, best_count)}
    elif best_count >= 1:
        return {"triggered": False, "confidence": 0.0,
                "detail": "%s 仅%d只涨停 弱共振(不触发)" % (best_concept, best_count)}
    else:
        return {"triggered": False, "confidence": 0.0,
                "detail": "所属概念无涨停"}


# =========================================
# 信号 2: 竞价抢筹 (auction_rush)
# =========================================

def check_auction_rush(code: str, ctx: dict) -> dict:
    """检测集合竞价是否有抢筹迹象。

    逻辑: 竞价量/昨日成交量 >= 1.5 且 开盘涨幅 >= 2%
    """
    auction = ctx.get("auction_data")
    prev_vol = ctx.get("prev_day_vol")

    if not auction or not prev_vol or prev_vol <= 0:
        return {"triggered": False, "confidence": 0.0,
                "detail": "竞价数据不可用"}

    auction_vol = auction.get("vol", 0) or auction.get("volume", 0) or 0
    auction_price = auction.get("price", 0) or 0
    pre_close = auction.get("pre_close", 0) or 0

    if auction_vol <= 0:
        return {"triggered": False, "confidence": 0.0,
                "detail": "竞价量为0"}

    vol_ratio = auction_vol / prev_vol
    open_gap = (auction_price / pre_close - 1) * 100 if pre_close > 0 else 0

    if vol_ratio >= 3.0 and open_gap >= 5:
        return {"triggered": True, "confidence": 1.0,
                "detail": "竞价强抢: 量比%.1f 高开%.1f%%" % (vol_ratio, open_gap)}
    elif vol_ratio >= 1.5 and open_gap >= 2:
        return {"triggered": True, "confidence": 0.7,
                "detail": "竞价抢筹: 量比%.1f 高开%.1f%%" % (vol_ratio, open_gap)}
    elif vol_ratio >= 1.0 and open_gap >= 1:
        return {"triggered": True, "confidence": 0.4,
                "detail": "竞价关注: 量比%.1f 高开%.1f%%" % (vol_ratio, open_gap)}
    else:
        return {"triggered": False, "confidence": 0.0,
                "detail": "竞价平淡: 量比%.1f 开%+.1f%%" % (vol_ratio, open_gap)}


# =========================================
# 信号 3: 量价突破 (volume_breakout)
# =========================================

def check_volume_breakout(code: str, ctx: dict) -> dict:
    """检测放量突破信号。

    逻辑: 量比 >= 2 且 涨幅 >= 5%

    zt_pattern 依据: 量比区分力(效应量+0.44)，78.4% 涨停股量比在 1-5。
    """
    quote = ctx.get("ths_quote") or {}
    basic = ctx.get("basic_info", {})
    scan_pct = ctx.get("scan_pct", 0)

    # 量比: THS 实时 > daily_basic(含估算值) > 0
    vol_ratio = (safe_float(quote.get("vol_ratio", 0))
                 or safe_float(basic.get("volume_ratio", 0))
                 or 0)
    # 涨幅: THS 实时 > basic_info(已合并Tushare daily) > 扫描涨幅
    pct = (safe_float(quote.get("pct_chg", 0))
           or safe_float(basic.get("pct_chg", 0))
           or safe_float(scan_pct)
           or 0)

    if vol_ratio <= 0 and pct <= 0:
        return {"triggered": False, "confidence": 0.0,
                "detail": "无量价数据"}

    score = 0.0
    detail_parts = []

    if vol_ratio >= 3:
        score += 0.5
        detail_parts.append("量比%.1f" % vol_ratio)
    elif vol_ratio >= 2:
        score += 0.35
        detail_parts.append("量比%.1f" % vol_ratio)
    elif vol_ratio >= 1.5:
        score += 0.15
        detail_parts.append("量比%.1f" % vol_ratio)

    if pct >= 7:
        score += 0.5
        detail_parts.append("涨幅%.1f%%" % pct)
    elif pct >= 5:
        score += 0.35
        detail_parts.append("涨幅%.1f%%" % pct)
    elif pct >= 3:
        score += 0.15
        detail_parts.append("涨幅%.1f%%" % pct)

    if score >= 0.7:
        return {"triggered": True, "confidence": min(score, 1.0),
                "detail": "强势突破: %s" % ", ".join(detail_parts)}
    elif score >= 0.35:
        return {"triggered": True, "confidence": score,
                "detail": "放量启动: %s" % ", ".join(detail_parts)}
    elif score > 0:
        return {"triggered": True, "confidence": score,
                "detail": "温和放量: %s" % ", ".join(detail_parts)}
    else:
        return {"triggered": False, "confidence": 0.0,
                "detail": "量价平淡: 量比%.1f 涨幅%.1f%%" % (vol_ratio, pct)}


# =========================================
# 信号 4: 连板基因 (limit_dna)
# =========================================

def check_limit_dna(code: str, ctx: dict) -> dict:
    """检测连板基因 — 是否有近期涨停历史。

    A股特征: 涨停有惯性，近期涨停过的股票更容易再次涨停。
    """
    hist_20d = ctx.get("limit_history_20d", 0)
    hist_60d_max = ctx.get("limit_history_60d_max", 0)

    if hist_20d >= 3:
        return {"triggered": True, "confidence": 1.0,
                "detail": "强连板基因: 近20日涨停%d次" % hist_20d}
    elif hist_20d >= 2:
        return {"triggered": True, "confidence": 0.85,
                "detail": "连板基因: 近20日涨停%d次" % hist_20d}
    elif hist_20d >= 1:
        bonus = ""
        if hist_60d_max >= 3:
            bonus = " 60日最高%d连板" % hist_60d_max
        return {"triggered": True, "confidence": 0.6,
                "detail": "涨停记忆: 近20日涨停%d次%s" % (hist_20d, bonus)}
    elif hist_60d_max >= 2:
        return {"triggered": True, "confidence": 0.3,
                "detail": "远期连板: 60日最高%d连板" % hist_60d_max}
    else:
        return {"triggered": False, "confidence": 0.0,
                "detail": "无近期涨停记录"}


# =========================================
# 信号 5: 小盘弹性 (smallcap)
# =========================================

def check_smallcap(code: str, ctx: dict) -> dict:
    """检测小盘弹性 — 流通市值越小越容易涨停。

    zt_pattern 依据: 20-50亿区间 Lift=1.3x，最优区间。
    """
    basic = ctx.get("basic_info", {})
    circ_mv = basic.get("circ_mv", 0) or 0

    if circ_mv <= 0:
        return {"triggered": False, "confidence": 0.0,
                "detail": "无市值数据"}

    yi = circ_mv / 100000000  # 转为亿

    if yi < 20:
        return {"triggered": True, "confidence": 0.8,
                "detail": "微型盘: 流通市值%.1f亿" % yi}
    elif yi < 50:
        return {"triggered": True, "confidence": 0.6,
                "detail": "小盘弹性: 流通市值%.1f亿" % yi}
    elif yi < 100:
        return {"triggered": True, "confidence": 0.3,
                "detail": "中盘: 流通市值%.1f亿" % yi}
    else:
        return {"triggered": False, "confidence": 0.0,
                "detail": "大盘: 流通市值%.1f亿" % yi}


# =========================================
# 信号 6: 早盘强势 (morning_power)
# =========================================

def check_morning_power(code: str, ctx: dict) -> dict:
    """检测早盘强势 — 早盘涨幅已经较高的股票更可能封板。

    zt_pattern 依据: 当日涨幅区分力最强(效应量+3.31)。
    """
    quote = ctx.get("ths_quote") or {}
    scan_pct = ctx.get("scan_pct", 0)
    pct = quote.get("pct_chg", 0) or scan_pct or 0

    now = datetime.now()
    hour_min = now.hour * 100 + now.minute

    if hour_min < 1000:
        time_label = "早盘抢筹"
    elif hour_min < 1030:
        time_label = "早盘"
    elif hour_min < 1100:
        time_label = "午前"
    elif hour_min < 1300:
        time_label = "午间"
    elif hour_min < 1400:
        time_label = "午后"
    else:
        time_label = "尾盘"

    if hour_min < 1000 and pct >= 7:
        return {"triggered": True, "confidence": 1.0,
                "detail": "%s强攻: 涨幅%.1f%%" % (time_label, pct)}
    elif hour_min < 1030 and pct >= 5:
        return {"triggered": True, "confidence": 0.8,
                "detail": "%s强势: 涨幅%.1f%%" % (time_label, pct)}
    elif hour_min < 1100 and pct >= 5:
        return {"triggered": True, "confidence": 0.5,
                "detail": "%s走强: 涨幅%.1f%%" % (time_label, pct)}
    elif pct >= 7:
        return {"triggered": True, "confidence": 0.4,
                "detail": "%s高位: 涨幅%.1f%%" % (time_label, pct)}
    else:
        return {"triggered": False, "confidence": 0.0,
                "detail": "%s平淡: 涨幅%.1f%%" % (time_label, pct)}


# =========================================
# 信号 7: 主力抢筹 (whale_hunt)
# =========================================

def check_whale_hunt(code: str, ctx: dict) -> dict:
    """检测大资金抢筹。

    逻辑: 大单净流入 / 流通市值 > 0.2% -> 触发

    zt_pattern 依据: 资金面区分力第二(效应量+0.35)。
    注意: 涨停股 net_mf_amount 日末均值偏低(涨停封死后买不到)，
    所以盘中 L2 主动买盘比日末 Tushare 数据更有参考价值。
    """
    basic = ctx.get("basic_info", {})
    circ_mv = basic.get("circ_mv", 0) or 0
    l2_net = ctx.get("l2_net_flow")
    l2_available = ctx.get("l2_available", False)

    if circ_mv <= 0:
        return {"triggered": False, "confidence": 0.0,
                "detail": "无市值数据"}

    # 盘中: L2 实时数据
    if l2_available and l2_net is not None:
        net_ratio = l2_net / circ_mv
        if net_ratio > 0.005:
            return {"triggered": True, "confidence": 1.0,
                    "detail": "L2主力强抢: 净买%.2f%%流通市值" % (net_ratio * 100)}
        elif net_ratio > 0.002:
            return {"triggered": True, "confidence": 0.7,
                    "detail": "L2主力流入: 净买%.2f%%流通市值" % (net_ratio * 100)}
        elif net_ratio > 0.0005:
            return {"triggered": True, "confidence": 0.4,
                    "detail": "L2主力关注: 净买%.2f%%流通市值" % (net_ratio * 100)}
        elif net_ratio < -0.003:
            return {"triggered": False, "confidence": 0.0,
                    "detail": "L2主力出逃: 净卖%.2f%%流通市值" % (abs(net_ratio) * 100)}
        else:
            return {"triggered": False, "confidence": 0.0,
                    "detail": "L2主力平淡: 净%+.2f%%流通市值" % (net_ratio * 100)}

    # 盘后: Tushare moneyflow
    mf_amount = basic.get("net_mf_amount", 0) or 0
    if mf_amount != 0:
        mf_ratio = mf_amount / circ_mv
        if mf_ratio > 0.005:
            return {"triggered": True, "confidence": 0.7,
                    "detail": "主力大幅流入: %.2f%%流通市值" % (mf_ratio * 100)}
        elif mf_ratio > 0.002:
            return {"triggered": True, "confidence": 0.5,
                    "detail": "主力流入: %.2f%%流通市值" % (mf_ratio * 100)}
        elif mf_ratio < -0.005:
            return {"triggered": False, "confidence": 0.0,
                    "detail": "主力大幅流出: %.2f%%流通市值" % (abs(mf_ratio) * 100)}
        else:
            return {"triggered": False, "confidence": 0.0,
                    "detail": "主力平静: %+.2f%%流通市值" % (mf_ratio * 100)}

    return {"triggered": False, "confidence": 0.0,
            "detail": "无资金流数据"}


# =========================================
# 信号注册表
# =========================================

SIGNAL_REGISTRY = {
    "concept_resonance": check_concept_resonance,
    "auction_rush": check_auction_rush,
    "volume_breakout": check_volume_breakout,
    "limit_dna": check_limit_dna,
    "smallcap": check_smallcap,
    "morning_power": check_morning_power,
    "whale_hunt": check_whale_hunt,
}

SIGNAL_LABELS = {
    "concept_resonance": "概念共振",
    "auction_rush": "竞价抢筹",
    "volume_breakout": "量价突破",
    "limit_dna": "连板基因",
    "smallcap": "小盘弹性",
    "morning_power": "早盘强势",
    "whale_hunt": "主力抢筹",
}

SIGNAL_ICONS = {
    "concept_resonance": "FIRE",
    "auction_rush": "MONEY",
    "volume_breakout": "CHART",
    "limit_dna": "DNA",
    "smallcap": "HOME",
    "morning_power": "CLOCK",
    "whale_hunt": "WHALE",
}

# 信号优先级（区分力强的排前面）
SIGNAL_PRIORITY = [
    "concept_resonance",
    "whale_hunt",
    "volume_breakout",
    "auction_rush",
    "morning_power",
    "limit_dna",
    "smallcap",
]


# =========================================
# 组合判断
# =========================================

def signal_combination_judge(signals: dict) -> tuple:
    """基于信号组合判断是否推送。

    返回: (should_push, combination_name, push_confidence)
    """
    def triggered(name):
        return signals.get(name, {}).get("triggered", False)

    def conf(name):
        return signals.get(name, {}).get("confidence", 0.0)

    cr = triggered("concept_resonance")
    ar = triggered("auction_rush")
    vb = triggered("volume_breakout")
    ld = triggered("limit_dna")
    sc = triggered("smallcap")
    mp = triggered("morning_power")
    wh = triggered("whale_hunt")

    # A: 概念共振 + 竞价抢筹 + 小盘弹性
    if cr and ar and sc:
        c = conf("concept_resonance") * 0.4 + conf("auction_rush") * 0.35 + conf("smallcap") * 0.25
        return True, "A:概念+竞价+小盘", c

    # B: 概念共振 + 主力抢筹 + 量价突破
    if cr and wh and vb:
        c = conf("concept_resonance") * 0.4 + conf("whale_hunt") * 0.3 + conf("volume_breakout") * 0.3
        return True, "B:概念+主力+量价", c

    # C: 概念共振 + 早盘强势 + 连板基因
    if cr and mp and ld:
        c = conf("concept_resonance") * 0.4 + conf("morning_power") * 0.3 + conf("limit_dna") * 0.3
        return True, "C:概念+早盘+连板", c

    # D: 竞价抢筹 + 量价突破 + 小盘弹性
    if ar and vb and sc:
        c = conf("auction_rush") * 0.4 + conf("volume_breakout") * 0.35 + conf("smallcap") * 0.25
        return True, "D:竞价+量价+小盘", c

    # E: 概念共振 + (主力抢筹 OR 量价突破) → 基本确信
    if cr and (wh or vb):
        confirm_conf = max(conf("whale_hunt"), conf("volume_breakout"))
        c = conf("concept_resonance") * 0.55 + confirm_conf * 0.45
        if c >= 0.65:
            return True, "E:概念+资金/量价", c

    # F: 量价突破 + 连板基因 → 中确信（无概念共振但动能+记忆）
    if vb and ld:
        c = conf("volume_breakout") * 0.5 + conf("limit_dna") * 0.5
        if c >= 0.8:  # 无概念共振时需要更高确信度
            return True, "F:量价+连板", c

    # G: 早盘强势 + 量价突破 + 主力抢筹
    if mp and vb and wh:
        c = (conf("morning_power") * 0.35 + conf("volume_breakout") * 0.35
             + conf("whale_hunt") * 0.3)
        return True, "G:早盘+量价+主力", c

    # H: 竞价抢筹 + 早盘强势 + 小盘弹性
    if ar and mp and sc:
        c = (conf("auction_rush") * 0.35 + conf("morning_power") * 0.35
             + conf("smallcap") * 0.3)
        return True, "H:竞价+早盘+小盘", c

    # 多信号兜底（需 >=4 个独立信号）
    triggered_count = sum(1 for s in signals.values() if s.get("triggered"))
    if triggered_count >= 4:
        return True, "多信号:%d个" % triggered_count, 0.5

    return False, "无组合", 0.0


def check_all_signals(code: str, ctx: dict) -> dict:
    """运行所有信号检测器，返回结果字典。"""
    results = {}
    for name in SIGNAL_PRIORITY:
        fn = SIGNAL_REGISTRY.get(name)
        if fn:
            try:
                results[name] = fn(code, ctx)
            except Exception as e:
                log.warning("信号 %s 异常: %s", name, e)
                results[name] = {"triggered": False, "confidence": 0.0,
                                 "detail": "信号异常: %s" % e}
    return results


def triggered_signals(results: dict) -> list:
    """返回触发的信号名列表。"""
    return [name for name, r in results.items() if r.get("triggered")]


def signals_summary(results: dict) -> str:
    """返回信号摘要字符串。"""
    parts = []
    for name in SIGNAL_PRIORITY:
        r = results.get(name)
        if r and r.get("triggered"):
            icon = SIGNAL_ICONS.get(name, "")
            label = SIGNAL_LABELS.get(name, name)
            parts.append("[%s]%s" % (icon, label))
    return " ".join(parts) if parts else "无信号触发"
