#!/usr/bin/env python3
"""
资金面评分  — 中单核心 + 中小单共振 + 主力逆向

核心变化（vs v2）:
  1. 主信号从「超大单主力净流入」转为「中单净流入」(Cohen's d +0.26)
  2. 新增「中小单共振」维度：中单+小单流入但主力流出 = 隐形吸筹
  3. 主力净额反转解读：主力净占比过高 → 可能对倒出货，扣分而非加分
  4. 移除 L2 依赖的「分时盘口资金抢筹」维度（无法回测）
  5. 否决规则大幅精简（仅保留极端情况，其余转入扣分）
  6. Sigmoid 平滑评分替代硬阈值分段，避免分数扎堆

数据源优先级: jvQuant (主力/大单/中单/小单净额) > Tushare moneyflow 降级

用法:
  # 实时评分
  score_fundflow("000001.SZ")

  # 回测评分（指定日期 + jvQuant 客户端）
  from scripts.jvquant_client import get_jvquant_client
  client = get_jvquant_client()
  score_fundflow("000001.SZ", trade_date="20260615", jv_client=client)

返回: (score: float 0-100, reason: str)
"""

import math
import sys
from datetime import datetime
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_DIR))
sys.path.insert(0, str(PROJECT_DIR / "scripts"))

from scripts.tu_share import call_tushare  # noqa: E402
from plays.limit_up.utils import safe_float, safe_float_none, list_to_dict  # noqa: E402

# 模块级缓存（由 pipeline 的 stage1_rough 预填充）
_FUNDFLOW_MF_CACHE: dict[str, dict] = {}  # {code: {net_mf_amount, ...}}
_FUNDFLOW_TI_CACHE: dict[str, list] = {}  # {code: [{exalter, net_buy, ...}]}


def set_fundflow_cache(mf_cache: dict, ti_cache: dict):
    """由 pipeline 调用，预填充当日资金流和龙虎榜数据。"""
    global _FUNDFLOW_MF_CACHE, _FUNDFLOW_TI_CACHE
    _FUNDFLOW_MF_CACHE = mf_cache
    _FUNDFLOW_TI_CACHE = ti_cache


# ═══════════════════════════════════════════════════════════════════
# Sigmoid 评分工具 — 平滑映射避免分数扎堆
# ═══════════════════════════════════════════════════════════════════

def _sigmoid(value: float, center: float, scale: float) -> float:
    """Logistic sigmoid: maps (-inf, +inf) → (0, 1) smoothly."""
    if scale <= 0:
        return 1.0 if value >= center else 0.0
    try:
        return 1.0 / (1.0 + math.exp(-(value - center) / scale))
    except OverflowError:
        return 1.0 if value > center else 0.0


def _score_sigmoid(value: float, center: float, scale: float,
                   max_score: float, min_score: float = 0.0) -> float:
    """Sigmoid scoring: maps value → [min_score, max_score] with smooth gradient.

    center: the inflection point (50th percentile-ish)
    scale:  spread of the sigmoid (smaller = steeper)
    """
    raw = _sigmoid(value, center, scale)
    return min_score + raw * (max_score - min_score)


def _score_bidirectional(value: float, center: float, scale: float,
                          pos_max: float, neg_max: float = 0.0) -> float:
    """Bidirectional sigmoid: positive side → [0, pos_max], negative side → [neg_max, 0].

    Uses tanh-like behavior: maps large positive → +pos_max, large negative → neg_max.
    """
    raw = _sigmoid(value, center, scale)
    if neg_max < 0:
        # raw ∈ [0, 1]: 0 → neg_max, 1 → pos_max
        return neg_max + raw * (pos_max - neg_max)
    return raw * pos_max


# ═══════════════════════════════════════════════════════════════════
# 数据获取层
# ═══════════════════════════════════════════════════════════════════

def _get_jv_fundflow(code_short: str, trade_date: str, jv_client) -> dict | None:
    """通过 jvQuant 获取个股单日资金流向。

    Returns:
        {main_net, big_net, mid_net, small_net, turnover, vol_ratio, pct_chg}
        或 None (获取失败)
    """
    try:
        data = jv_client.get_fundflow_single(code_short, trade_date)
        if data and any(data.get(k, 0) != 0 for k in
                        ["main_net", "big_net", "mid_net", "small_net"]):
            return data
    except Exception:
        pass
    return None


def _get_tushare_moneyflow(code: str, trade_date: str) -> list[dict]:
    """Tushare moneyflow 降级。返回 list[dict]（通常1条）。"""
    try:
        resp = call_tushare("moneyflow",
                            {"ts_code": code, "trade_date": trade_date},
                            "trade_date,ts_code,buy_elg_amount,sell_elg_amount,"
                            "buy_lg_amount,sell_lg_amount,net_mf_amount")
        items = resp.get("data", {}).get("items", [])
        fields = resp.get("data", {}).get("fields", [])
        return list_to_dict(items, fields)
    except Exception:
        return []


def _get_tushare_daily_basic(code: str, trade_date: str) -> list[dict]:
    """Tushare daily_basic 降级。优先从 pipeline 预取缓存读取。"""
    try:
        from plays.limit_up.strategies import factor_ctx
        basic = factor_ctx.get_daily_basic(code, trade_date)
        if basic:
            return [basic]
    except Exception:
        pass

    try:
        resp = call_tushare("daily_basic",
                            {"ts_code": code, "trade_date": trade_date},
                            "trade_date,ts_code,close,turnover_rate,"
                            "turnover_rate_f,volume_ratio,circ_mv,amount")
        items = resp.get("data", {}).get("items", [])
        fields = resp.get("data", {}).get("fields", [])
        return list_to_dict(items, fields)
    except Exception:
        return []


def _get_tushare_daily(code: str, trade_date: str) -> list[dict]:
    """Tushare daily 行情数据。"""
    try:
        resp = call_tushare("daily",
                            {"ts_code": code, "trade_date": trade_date},
                            "trade_date,ts_code,open,high,low,close,"
                            "pre_close,pct_chg,vol,amount")
        items = resp.get("data", {}).get("items", [])
        fields = resp.get("data", {}).get("fields", [])
        return list_to_dict(items, fields)
    except Exception:
        return []


def _get_top_inst(code: str, trade_date: str) -> list[dict]:
    """Tushare top_inst 龙虎榜机构交易。"""
    try:
        resp = call_tushare("top_inst",
                            {"ts_code": code, "trade_date": trade_date},
                            "trade_date,ts_code,exalter,side,buy,buy_rate,"
                            "sell,sell_rate,net_buy,reason")
        items = resp.get("data", {}).get("items", [])
        fields = resp.get("data", {}).get("fields", [])
        return list_to_dict(items, fields)
    except Exception:
        return []


def _get_top_list(code: str, trade_date: str) -> list[dict]:
    """Tushare top_list 龙虎榜上榜明细。"""
    try:
        resp = call_tushare("top_list",
                            {"ts_code": code, "trade_date": trade_date},
                            "trade_date,ts_code,name,close,pct_change,"
                            "turnover_rate,amount,l_sell,l_buy,l_amount,"
                            "net_amount,net_rate")
        items = resp.get("data", {}).get("items", [])
        fields = resp.get("data", {}).get("fields", [])
        return list_to_dict(items, fields)
    except Exception:
        return []


def _get_limit_list(code: str, trade_date: str) -> list[dict]:
    """Tushare limit_list_d 涨跌停列表。"""
    try:
        resp = call_tushare("limit_list_d",
                            {"ts_code": code, "trade_date": trade_date},
                            "trade_date,ts_code,close,pct_chg,open_times,"
                            "fd_amount,first_time,last_time,up_stat,limit")
        items = resp.get("data", {}).get("items", [])
        fields = resp.get("data", {}).get("fields", [])
        return list_to_dict(items, fields)
    except Exception:
        return []


def _get_margin_detail(code: str, trade_date: str) -> list[dict]:
    """Tushare margin_detail 融资融券明细。"""
    try:
        resp = call_tushare("margin_detail",
                            {"ts_code": code, "trade_date": trade_date},
                            "trade_date,ts_code,rzye,rqye,rzmre,rqmcl,rzrqye")
        items = resp.get("data", {}).get("items", [])
        fields = resp.get("data", {}).get("fields", [])
        return list_to_dict(items, fields)
    except Exception:
        return []


def _get_hk_hold(code: str) -> list[dict]:
    """Tushare hk_hold 北向持股。"""
    try:
        exchange = "SH" if code.endswith(".SH") else "SZ" if code.endswith(".SZ") else ""
        resp = call_tushare("hk_hold",
                            {"ts_code": code, "exchange": exchange},
                            "trade_date,ts_code,name,vol,ratio,exchange")
        items = resp.get("data", {}).get("items", [])
        fields = resp.get("data", {}).get("fields", [])
        return list_to_dict(items, fields)
    except Exception:
        return []


# ═══════════════════════════════════════════════════════════════════
# 主评分函数
# ═══════════════════════════════════════════════════════════════════

def score_fundflow(code: str, trade_date: str = None,
                       jv_client=None) -> tuple[float, str]:
    """资金面  评分。

    Args:
        code: 带后缀股票代码，如 "000001.SZ"
        trade_date: 交易日 YYYYMMDD，默认今天
        jv_client: JvQuantClient 实例，None 则降级 Tushare

    Returns:
        (score: 0-100, reason: str)
    """
    if trade_date is None:
        trade_date = datetime.now().strftime("%Y%m%d")

    code_short = code.split(".")[0]

    # ═══════════════════════════════════════════════════════════
    # 0. 获取数据
    # ═══════════════════════════════════════════════════════════

    # 0.1 优先 jvQuant（提供 main_net/big_net/mid_net/small_net 四维拆解）
    jv_data = None
    if jv_client is not None:
        jv_data = _get_jv_fundflow(code_short, trade_date, jv_client)

    # 0.2 数据源：优先模块缓存（pipeline 预填充），降级 Tushare
    _cached_mf = _FUNDFLOW_MF_CACHE.get(code, {})
    _cached_ti = _FUNDFLOW_TI_CACHE.get(code, [])
    mf_rows = [_cached_mf] if _cached_mf else _get_tushare_moneyflow(code, trade_date)
    ti_rows = _cached_ti or _get_top_inst(code, trade_date)
    db_rows = _get_tushare_daily_basic(code, trade_date)
    daily_rows = _get_tushare_daily(code, trade_date)

    mf = mf_rows[0] if mf_rows else {}
    db = db_rows[0] if db_rows else {}
    dl = daily_rows[0] if daily_rows else {}

    # 0.3 提取核心字段
    amount_yuan = 0  # 成交额(元)
    circ_mv_yuan = 0  # 流通市值(元)
    turnover = 0
    pct_chg = 0
    vol_ratio = 0

    # --- jvQuant 分支 ---
    if jv_data:
        main_net = jv_data.get("main_net", 0) or 0       # 万元
        big_net = jv_data.get("big_net", 0) or 0         # 万元
        mid_net = jv_data.get("mid_net", 0) or 0         # 万元
        small_net = jv_data.get("small_net", 0) or 0     # 万元
        turnover = jv_data.get("turnover", 0) or 0
        vol_ratio = jv_data.get("vol_ratio", 0) or 0
        pct_chg = jv_data.get("pct_chg", 0) or 0
        data_src = "jvQuant"

        # 补充成交额 / 流通市值（优先级: Tushare daily > Tushare daily_basic > jvQuant K线）
        if db:
            circ_mv_yuan = safe_float(db.get("circ_mv", 0)) * 10000  # 万元→元
            amount_yuan = safe_float(db.get("amount", 0)) * 1000     # 千元→元
        if dl and amount_yuan == 0:
            amount_yuan = safe_float(dl.get("amount", 0)) * 1000
        # jvQuant K线作为 amount 兜底（单股 fundflow 接口不返回成交额）
        if amount_yuan == 0 and jv_client is not None:
            try:
                kl = jv_client.get_kline(code_short, count=1)
                if kl and kl[0].get("amount", 0) > 0:
                    amount_yuan = safe_float(kl[0].get("amount", 0))
            except Exception:
                pass

    # --- Tushare 降级分支 ---
    else:
        data_src = "Tushare"
        if mf:
            buy_elg = safe_float(mf.get("buy_elg_amount", 0))
            sell_elg = safe_float(mf.get("sell_elg_amount", 0))
            buy_lg = safe_float(mf.get("buy_lg_amount", 0))
            sell_lg = safe_float(mf.get("sell_lg_amount", 0))
            main_net = (buy_elg - sell_elg) + (buy_lg - sell_lg)  # 万元
            # Tushare 不拆分中单/小单，用总净额 - 主力净额作为中单代理
            net_mf_total = safe_float(mf.get("net_mf_amount", 0))
            mid_net = net_mf_total - main_net     # 万元，≈中单+小单（以降级代理）
            big_net = 0
            small_net = 0                          # Tushare 无法拆分
        else:
            main_net = 0
            big_net = 0
            mid_net = 0
            small_net = 0

        if db:
            turnover = safe_float(db.get("turnover_rate", 0))
            vol_ratio = safe_float(db.get("volume_ratio", 0))
            amount_yuan = safe_float(db.get("amount", 0)) * 1000  # 千元→元
            circ_mv_yuan = safe_float(db.get("circ_mv", 0)) * 10000  # 万元→元
            pct_chg = safe_float(db.get("close_pct_chg", 0))  # daily_basic 无 pct_chg
        if dl:
            pct_chg = safe_float(dl.get("pct_chg", 0)) or pct_chg
            if amount_yuan == 0:
                amount_yuan = safe_float(dl.get("amount", 0)) * 1000

    # 实时数据覆盖（从 realtime_ctx 获取盘中换手/量比，覆盖 T-1 Tushare）
    try:
        from plays.limit_up.strategies.realtime_ctx import get_turnover as _rt_tr, get_vol_ratio as _rt_vr
        _rt_tr_val = _rt_tr(code)
        _rt_vr_val = _rt_vr(code)
        if _rt_tr_val is not None:
            turnover = _rt_tr_val
        if _rt_vr_val is not None:
            vol_ratio = _rt_vr_val
    except Exception:
        pass

    # 0.4 归一化指标：净额 / 成交额 (%)
    if amount_yuan > 0:
        main_pct = (main_net * 10000) / amount_yuan * 100  # 万元→元→%
        mid_pct = (mid_net * 10000) / amount_yuan * 100
        small_pct = (small_net * 10000) / amount_yuan * 100
        big_pct = (big_net * 10000) / amount_yuan * 100
    else:
        # 无成交额数据时使用绝对值（万元），不做百分比归一化
        main_pct = main_net
        mid_pct = mid_net
        small_pct = small_net
        big_pct = big_net

    # 0.5 主力净额/流通市值 (‰)，用于规模判断
    main_vs_circ = (main_net * 10000 / circ_mv_yuan * 1000) if circ_mv_yuan > 0 else 0

    # ═══════════════════════════════════════════════════════════
    # 1. 否决检查（仅保留极端情况，其余转入扣分）
    # ═══════════════════════════════════════════════════════════
    veto_reason = None

    # 否决1: 龙虎榜机构净卖出 > 流通市值0.3% 或 > 3亿（极端撤离信号）
    # 修复: 大盘股绝对金额高但占比小，改为相对+绝对双阈值
    if ti_rows:
        net_sell = 0
        for inst in ti_rows:
            nb = safe_float(inst.get("net_buy", 0))
            if nb < 0:
                net_sell += abs(nb)
            # 获取流通市值用于比例判断
            circ_mv_yuan = 0
            try:
                from plays.limit_up.strategies import factor_ctx
                basic = factor_ctx.get_daily_basic(code, trade_date)
                if basic:
                    circ_mv_yuan = safe_float(basic.get("circ_mv", 0)) * 10000  # 万元→元
                else:
                    from scripts.tu_share import call_tushare
                    resp_d = call_tushare("daily_basic", {"ts_code": code, "trade_date": trade_date}, "circ_mv")
                    db_items = resp_d.get("data",{}).get("items",[])
                    if db_items:
                        db_f = resp_d.get("data",{}).get("fields",[])
                        d_d = dict(zip(db_f, db_items[0]))
                        circ_mv_yuan = safe_float(d_d.get("circ_mv", 0)) * 10000  # 万元→元
            except: pass
            sell_pct = (net_sell / circ_mv_yuan * 100) if circ_mv_yuan > 0 else 999
            # 双阈值: 占比>1% 且 绝对额>1亿 → 否决（大盘股容错）
            if sell_pct > 1.0 and net_sell > 100000000:
                veto_reason = f"龙虎榜净卖出{net_sell/10000:.0f}万({sell_pct:.2f}%流通市值)（极端撤离）"

    if veto_reason:
        return 0.0, f"否决: {veto_reason}"

    # ═══════════════════════════════════════════════════════════
    # 2. 维度评分 v2.1：以换手率+成交额为核心，弱化资金流向方向
    #
    # 子因子 IC 发现：turnover_rate(0.23)、turnover_rate_f(0.23)、
    # avg_amount_5d(0.20) 是资金端最强信号；买卖各档金额均正相关，
    # 说明"大资金参与"本身才是核心，方向不重要。
    # ═══════════════════════════════════════════════════════════
    score = 0.0
    reasons: list[str] = []

    # ─── 维度1: 换手率活跃度 (35分) ───
    dim1 = 0.0
    if turnover >= 20:
        dim1 = 35
        reasons.append(f"换手极度活跃{turnover:.1f}%+35")
    elif turnover >= 15:
        dim1 = 28
        reasons.append(f"换手非常活跃{turnover:.1f}%+28")
    elif turnover >= 10:
        dim1 = 21
        reasons.append(f"换手活跃{turnover:.1f}%+21")
    elif turnover >= 6:
        dim1 = 14
        reasons.append(f"换手中度活跃{turnover:.1f}%+14")
    elif turnover >= 3:
        dim1 = 7
        reasons.append(f"换手温和{turnover:.1f}%+7")
    elif turnover >= 1:
        dim1 = 2
        reasons.append(f"换手偏低{turnover:.1f}%+2")
    else:
        reasons.append(f"换手极低{turnover:.1f}%+0")
    score += dim1

    # ─── 维度2: 成交额规模 (25分) ───
    dim2 = 0.0
    amount_wan = amount_yuan / 10000  # 元→万元
    if amount_wan >= 500_0000:  # 5000万
        dim2 = 25
        reasons.append(f"成交巨额{amount_wan/10000:.1f}亿+25")
    elif amount_wan >= 200_0000:
        dim2 = 20
        reasons.append(f"成交充沛{amount_wan/10000:.1f}亿+20")
    elif amount_wan >= 100_0000:
        dim2 = 15
        reasons.append(f"成交充足{amount_wan/10000:.1f}亿+15")
    elif amount_wan >= 50_0000:
        dim2 = 10
        reasons.append(f"成交尚可{amount_wan/10000:.1f}亿+10")
    elif amount_wan >= 20_0000:
        dim2 = 5
        reasons.append(f"成交一般{amount_wan/10000:.1f}亿+5")
    else:
        reasons.append(f"成交不足{amount_wan/10000:.1f}亿+0")
    score += dim2

    # ─── 维度3: 大市值+高换手共振 (10分) ───
    dim3 = 0.0
    circ_mv_yi = circ_mv_yuan / 10000 / 10000  # 元 → 亿元
    if circ_mv_yi >= 100 and turnover >= 8:
        dim3 = 10
        reasons.append(f"大盘活跃(流通{circ_mv_yi:.0f}亿换手{turnover:.1f}%)+10")
    elif circ_mv_yi >= 50 and turnover >= 6:
        dim3 = 6
        reasons.append(f"中大盘活跃(流通{circ_mv_yi:.0f}亿换手{turnover:.1f}%)+6")
    elif circ_mv_yi >= 20 and turnover >= 5:
        dim3 = 3
        reasons.append(f"中盘活跃(流通{circ_mv_yi:.0f}亿换手{turnover:.1f}%)+3")
    score += dim3

    # ─── 维度4: 资金流向健康度 (10分) ───
    # 数据证明绝对金额流全正，方向只给轻权重
    dim4 = 0.0
    total_net = main_net + mid_net + small_net  # 万元

    if amount_yuan > 0:
        total_net_pct = total_net * 10000 / amount_yuan * 100
        if total_net_pct >= 1.0:
            dim4 += 5
            reasons.append(f"资金净流入{total_net_pct:.2f}%+5")
        elif total_net_pct >= 0.3:
            dim4 += 3
            reasons.append(f"资金净流入{total_net_pct:.2f}%+3")
        elif total_net_pct <= -1.0:
            dim4 -= 3
            reasons.append(f"资金净流出{total_net_pct:.2f}%-3")

    if main_vs_circ >= 0.5:
        dim4 += 5
        reasons.append(f"主力流入{main_vs_circ:.2f}‰+5")
    elif main_vs_circ >= 0.2:
        dim4 += 2
        reasons.append(f"主力流入{main_vs_circ:.2f}‰+2")
    elif main_vs_circ <= -0.3:
        dim4 -= 3
        reasons.append(f"主力流出{main_vs_circ:.2f}‰-3")

    dim4 = max(-5.0, min(10.0, dim4))
    score += dim4

    # ─── 维度5: 龙虎榜与封板质量 (15分) ───
    dim5 = 0.0

    inst_rows = _get_top_inst(code, trade_date)
    if inst_rows:
        inst_net_buy = 0.0
        hot_net_buy = 0.0
        for inst in inst_rows:
            nb = safe_float(inst.get("net_buy", 0))
            exalter = inst.get("exalter", "")
            if "机构" in exalter or "专用" in exalter:
                inst_net_buy += nb
            else:
                hot_net_buy += nb
        total_net_lhb = inst_net_buy + hot_net_buy
        if total_net_lhb > 50000000:
            dim5 += 6
            reasons.append(f"龙虎榜净买{total_net_lhb/1e8:.1f}亿+6")
        elif total_net_lhb > 10000000:
            dim5 += 3
            reasons.append(f"龙虎榜净买{total_net_lhb/10000:.0f}万+3")
        elif total_net_lhb < -50000000:
            dim5 -= 5
            reasons.append(f"龙虎榜净卖{abs(total_net_lhb)/1e8:.1f}亿-5")

    top_rows = _get_top_list(code, trade_date)
    if top_rows:
        net_rate = safe_float(top_rows[0].get("net_rate", 0))
        if net_rate > 0:
            dim5 += 3
            reasons.append(f"上榜净买率{net_rate:.1f}%+3")
        elif net_rate < -5:
            dim5 -= 3
            reasons.append(f"上榜净卖率{abs(net_rate):.1f}%-3")

    limit_rows = _get_limit_list(code, trade_date)
    if limit_rows:
        ll = limit_rows[0]
        limit_type = str(ll.get("limit", "")).upper()
        open_times = safe_float(ll.get("open_times", 1))
        fd_amount = safe_float(ll.get("fd_amount", 0))
        first_time = ll.get("first_time", "")

        if limit_type == "U":
            if open_times == 0:
                dim5 += 3
                reasons.append("秒封/一字板+3")
            elif open_times >= 4:
                dim5 -= 3
                reasons.append(f"炸板{int(open_times)}次-3")

            if fd_amount > 0 and circ_mv_yuan > 0:
                fd_ratio = fd_amount / (circ_mv_yuan / 10000) * 100
                if fd_ratio > 1.0:
                    dim5 += 3
                    reasons.append(f"封单{fd_ratio:.1f}%流通+3")

            if first_time:
                try:
                    hhmm = first_time.replace(":", "")
                    if hhmm < "103000":
                        dim5 += 2
                        reasons.append(f"早封{first_time}+2")
                except Exception:
                    pass

    dim5 = max(-10.0, min(15.0, dim5))
    score += dim5

    # ─── 维度6: 追涨惩罚 ───
    # 近 10 日涨幅过大时，资金面的放量可能是出货而非接力
    try:
        from plays.limit_up.strategies import factor_ctx
        pf = factor_ctx.get_price_features(code)
        t10 = pf.get("trailing_10", 0.0)
        if t10 > 0.30:
            score *= 0.85
        elif t10 > 0.20:
            score *= 0.92
    except Exception:
        pass

    # ═══════════════════════════════════════════════════════════
    # 3. 汇总输出
    # ═══════════════════════════════════════════════════════════
    final_score = max(0.0, min(100.0, score))
    final_score = round(final_score, 1)

    if final_score >= 75:
        level = "高"
    elif final_score >= 55:
        level = "中"
    elif final_score >= 35:
        level = "低"
    else:
        level = "无"

    if not reasons:
        reasons.append("[无]无明显资金信号")

    reason_str = f"[{level}][{data_src}] {'; '.join(reasons[:6])}"
    return final_score, reason_str
