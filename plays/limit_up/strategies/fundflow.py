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
    """Tushare daily_basic 降级。"""
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

    # 0.2 Tushare 降级数据
    mf_rows = _get_tushare_moneyflow(code, trade_date)
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
    if not jv_data:
        inst_rows = _get_top_inst(code, trade_date)
        if inst_rows:
            net_sell = 0
            for inst in inst_rows:
                nb = safe_float(inst.get("net_buy", 0))
                if nb < 0:
                    net_sell += abs(nb)
            # 获取流通市值用于比例判断
            circ_mv_yuan = 0
            try:
                from scripts.tu_share import call_tushare
                resp_d = call_tushare("daily_basic", {"ts_code": code}, "circ_mv")
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
    # 2. 维度评分
    # ═══════════════════════════════════════════════════════════
    score = 0.0
    reasons: list[str] = []

    # ─── 维度1: 中单净流入 (35分) ───
    # f_mid_net Cohen's d = +0.26 → 最强正向预测因子
    # 使用 sigmoid 平滑评分，中心 ≈ 成交额的0.15%
    dim1 = _score_bidirectional(
        mid_pct,
        center=0.40,       # 中单净占比 0.40% 为中枢（典型活跃股中单流入水平）
        scale=0.35,        # 0.35% 标准差 → 0.05%~0.75% 区间覆盖过渡区
        pos_max=35.0,
        neg_max=-12.0,     # 负向最多扣 12 分（中单流出弱于流入的预测力）
    )
    dim1 = round(dim1, 1)
    dim1 = max(-12.0, min(35.0, dim1))

    # 构造中单净额描述（有成交额则附百分比，否则仅绝对值）
    if amount_yuan > 0:
        mid_desc = f"中单净{mid_net:+.0f}万({mid_pct:+.2f}%)"
    else:
        mid_desc = f"中单净{mid_net:+.0f}万"

    if dim1 >= 20:
        reasons.append(f"[中单{dim1:.0f}]{mid_desc}→强力吸筹")
    elif dim1 >= 10:
        reasons.append(f"[中单{dim1:.0f}]{mid_desc}→偏积极")
    elif dim1 >= 0:
        reasons.append(f"[中单{dim1:.0f}]{mid_desc}→中性")
    else:
        reasons.append(f"[中单{dim1:.0f}]{mid_desc}→偏弱")

    score += dim1

    # ─── 维度2: 中小单共振 (25分) ───
    # 核心模式：中单+小单流入但主力流出 → 隐形吸筹（对倒洗盘）
    # 主力砸盘制造恐慌 → 中小资金悄悄接筹 → 后续拉升概率高
    dim2 = 0.0

    if jv_data:
        # jvQuant 有完整四维数据，可做精确共振判断
        mid_pos = mid_net > 0
        small_pos = small_net > 0
        main_neg = main_net < 0
        big_neg = big_net < 0
        price_rising = pct_chg > 2

        # 加分档位（取最高匹配）
        if mid_pos and small_pos and main_neg and price_rising:
            # 经典隐形吸筹：中单+小单流入，主力流出，股价上涨
            dim2 += 25
            reasons.append(f"[共振{dim2:.0f}]主力{main_net:+.0f}万但中单{mid_net:+.0f}万+小单{small_net:+.0f}万→隐形吸筹")
        elif mid_pos and small_pos and main_neg:
            # 中单+小单共振流入，主力在卖（无需涨幅条件）
            dim2 += 18
            reasons.append(f"[共振{dim2:.0f}]中单{mid_net:+.0f}万+小单{small_net:+.0f}万 vs 主力{main_net:+.0f}万→吸筹")
        elif mid_pos and small_pos:
            # 中单+小单同步流入（主力不一定在卖）
            dim2 += 12
            reasons.append(f"[共振{dim2:.0f}]中小单同步流入{mid_net+small_net:+.0f}万")
        elif mid_pos and main_neg and big_neg:
            # 中单独立承接，大单+主力都在卖（中单对抗抛压）
            dim2 += 10
            reasons.append(f"[共振{dim2:.0f}]中单独抗抛压{mid_net:+.0f}万(主力{main_net:+.0f})")
        elif mid_pos:
            # 仅中单流入
            dim2 += 5
            reasons.append(f"[共振{dim2:.0f}]中单净{mid_net:+.0f}万→单边支撑")

        # 扣分档位
        if mid_net < 0 and small_net < 0 and main_net < 0:
            # 全资金流出 → 真正弱势
            dim2 -= 15
            reasons.append(f"[共振{dim2:.0f}]全员流出→真弱势")
        elif mid_net < 0 and small_net < 0:
            dim2 -= 8
            reasons.append(f"[共振{dim2:.0f}]中小单同步流出{mid_net+small_net:+.0f}万")
    else:
        # Tushare 降级：中单 = 总净额 - 主力净额（粗粒度代理）
        if mid_net > 0 and main_net < 0:
            # 总净流入正但主力净流出 → 散户/中单承接（粗粒度共振）
            dim2 += 12
            reasons.append(f"[共振{dim2:.0f}]中单代理{mid_net:+.0f}万 vs 主力{main_net:+.0f}万→疑似吸筹")
        elif mid_net > 0 and main_net > 0:
            dim2 += 5
            reasons.append(f"[共振{dim2:.0f}]主力+中单代理同步流入[Tushare粗粒]")
        elif mid_net < 0 and main_net < 0:
            dim2 -= 10
            reasons.append(f"[共振{dim2:.0f}]主力+中单代理双流出→弱势")

    dim2 = max(-20.0, min(25.0, dim2))
    score += dim2

    # ─── 维度3: 主力真实意图 (15分) ───
    # 反转逻辑：主力净占比过高 → 可能对倒出货（负向信号）
    # f_main_pct d = -0.25, f_main_net d = -0.21
    dim3 = 0.0

    if jv_data:
        # 有完整四维数据 → 精确判断主力与中单关系
        if main_net > 0 and mid_net > 0:
            # 主力+中单共振上扬（最健康）
            main_mid_ratio = main_net / mid_net if mid_net > 0 else 999
            if main_mid_ratio < 2.0:
                # 主力不过度主导，中单配合良好
                dim3 += 12
                reasons.append(f"[主力{dim3:.0f}]主力{main_net:+.0f}万+中单{mid_net:+.0f}万→共振上扬")
            elif main_mid_ratio < 3.0:
                dim3 += 6
                reasons.append(f"[主力{dim3:.0f}]主力{main_net:+.0f}万→适度主导")
            else:
                # 主力过分主导 → 可能对倒
                dim3 -= 8
                reasons.append(f"[主力{dim3:.0f}]主力{main_net:+.0f}万远超中单{main_mid_ratio:.1f}x→疑似对倒")
        elif main_net > 0 and mid_net <= 0:
            # 主力独大，中单不跟 → 谨慎偏负
            dim3 -= 5
            reasons.append(f"[主力{dim3:.0f}]主力{main_net:+.0f}万独大无中单跟→偏虚")
        elif main_net < 0 and mid_net > 0:
            # 主力流出但中单承接（已在dim2覆盖，dim3轻度加分）
            dim3 += 8
            reasons.append(f"[主力{dim3:.0f}]主力{main_net:+.0f}万但中单承接→逆势信号")
        elif main_net < 0 and mid_net < 0:
            # 主力+中单双流出
            dim3 -= 12
            reasons.append(f"[主力{dim3:.0f}]主力{main_net:+.0f}万+中单{mid_net:+.0f}万→双流出")
    else:
        # Tushare 降级：仅有主力净额
        # 使用主力净额/流通市值作为判断依据
        if main_vs_circ > 0.3:
            # 主力净流入 > 0.3‰流通市值，但无法验证中单是否跟随
            dim3 += 5
            reasons.append(f"[主力{dim3:.0f}]主力流入{main_vs_circ:.2f}‰流通市值[Tushare粗粒]")
        elif main_vs_circ < -0.3:
            dim3 -= 10
            reasons.append(f"[主力{dim3:.0f}]主力流出{main_vs_circ:.2f}‰流通市值")
        elif main_net > 0:
            dim3 += 3
            reasons.append(f"[主力{dim3:.0f}]主力{main_net:+.0f}万→微弱流入")
        elif main_net < 0:
            dim3 -= 3
            reasons.append(f"[主力{dim3:.0f}]主力{main_net:+.0f}万→微弱流出")

    # 主力净占比过大时的额外惩罚（f_main_pct d = -0.25）
    if main_pct > 5 and amount_yuan > 0:
        # 主力净占比 > 5% → 过度集中信号
        dim3 -= 5
        reasons.append(f"[主力{dim3:.0f}]主力占比{main_pct:.1f}%过高→疑似对倒")

    dim3 = max(-20.0, min(15.0, dim3))
    score += dim3

    # ─── 维度4: 龙虎榜机构游资 (15分) ───
    dim4 = 0.0

    # 4.1 龙虎榜机构交易明细
    inst_rows = _get_top_inst(code, trade_date)
    if inst_rows:
        inst_net_buy = 0.0
        hot_net_buy = 0.0
        seat_net_values: list[float] = []

        for inst in inst_rows:
            nb = safe_float(inst.get("net_buy", 0))
            exalter = inst.get("exalter", "")
            if "机构" in exalter or "专用" in exalter:
                inst_net_buy += nb
            else:
                hot_net_buy += nb
            seat_net_values.append(abs(nb))

        total_net = inst_net_buy + hot_net_buy

        # 席位合力：机构+游资净买入
        if total_net > 50000000:  # 5000万元
            dim4 += 8
            reasons.append(f"[龙虎{dim4:.0f}]机构+游资净买{total_net/1e8:.1f}亿")
        elif total_net > 10000000:  # 1000万元
            dim4 += 4
            reasons.append(f"[龙虎{dim4:.0f}]机构+游资净买{total_net/10000:.0f}万")

        # 席位主导：最大席位净买入占比 > 30%
        if total_net > 0 and seat_net_values:
            max_seat = max(seat_net_values)
            if max_seat / total_net > 0.3:
                dim4 += 5
                reasons.append(f"[龙虎{dim4:.0f}]席位主导{max_seat/10000:.0f}万")

        # 机构>游资：机构主导优于游资主导
        if total_net > 0 and inst_net_buy > hot_net_buy * 2:
            dim4 += 2
            reasons.append(f"[龙虎{dim4:.0f}]机构主导")

    # 4.2 龙虎榜上榜净买率
    top_rows = _get_top_list(code, trade_date)
    if top_rows:
        top = top_rows[0]
        net_rate = safe_float(top.get("net_rate", 0))
        if net_rate > 0:
            dim4 += 3
            reasons.append(f"[龙虎{dim4:.0f}]净买率{net_rate:.1f}%")
        elif net_rate < -5:
            dim4 -= 5
            reasons.append(f"[龙虎{dim4:.0f}]净卖率{abs(net_rate):.1f}%→撤离")

    # 4.3 涨停封板质量（limit_list）
    limit_rows = _get_limit_list(code, trade_date)
    if limit_rows:
        ll = limit_rows[0]
        limit_type = str(ll.get("limit", "")).upper()
        open_times = safe_float(ll.get("open_times", 1))
        fd_amount = safe_float(ll.get("fd_amount", 0))  # 万
        first_time = ll.get("first_time", "")

        if limit_type == "U":
            # 开板次数
            if open_times == 0:
                dim4 += 4
                reasons.append(f"[龙虎{dim4:.0f}]秒封/一字板")
            elif open_times == 1:
                dim4 += 2
                reasons.append(f"[龙虎{dim4:.0f}]开板1次")
            elif open_times >= 4:
                dim4 -= 4
                reasons.append(f"[龙虎{dim4:.0f}]炸板{int(open_times)}次→分歧")

            # 封单规模
            if fd_amount > 0 and circ_mv_yuan > 0:
                fd_ratio = fd_amount / (circ_mv_yuan / 10000) * 100  # 封单/流通市值(%)
                if fd_ratio > 1.0:
                    dim4 += 3
                    reasons.append(f"[龙虎{dim4:.0f}]封单{fd_ratio:.1f}%流通")
                elif fd_ratio < 0.2:
                    dim4 -= 3
                    reasons.append(f"[龙虎{dim4:.0f}]封单弱{fd_ratio:.2f}%")

            # 首封时间
            if first_time:
                try:
                    hhmm = first_time.replace(":", "")
                    if hhmm < "103000":
                        dim4 += 3
                        reasons.append(f"[龙虎{dim4:.0f}]早封{first_time}")
                    elif hhmm > "140000":
                        dim4 -= 3
                        reasons.append(f"[龙虎{dim4:.0f}]尾板{first_time}")
                except Exception:
                    pass

    dim4 = max(-20.0, min(15.0, dim4))
    score += dim4

    # ─── 维度5: 融资与北向资金 (10分) ───
    dim5 = 0.0

    # 5.1 融资融券
    margin_rows = _get_margin_detail(code, trade_date)
    if margin_rows and len(margin_rows) >= 3:
        rzye_list = sorted(
            [(safe_float(r.get("rzye", 0)), r.get("trade_date", ""))
             for r in margin_rows],
            key=lambda x: x[1], reverse=True
        )
        rzye_vals = [v for v, _ in rzye_list]

        if len(rzye_vals) >= 3:
            # 融资连续3日增长
            if rzye_vals[0] > rzye_vals[1] > rzye_vals[2]:
                dim5 += 5
                reasons.append(f"[融资{dim5:.0f}]融资3日连增")
            elif rzye_vals[0] < rzye_vals[1] < rzye_vals[2]:
                dim5 -= 5
                reasons.append(f"[融资{dim5:.0f}]融资3日连降→杠杆撤离")

            # 融资活跃度：融资买入额/成交额
            rzmre = safe_float(margin_rows[0].get("rzmre", 0))
            if rzmre > 0 and amount_yuan > 0:
                rz_ratio = rzmre / amount_yuan * 100
                if rz_ratio > 8:
                    dim5 += 3
                    reasons.append(f"[融资{dim5:.0f}]买入占比{rz_ratio:.1f}%→活跃")
    elif margin_rows:
        reasons.append(f"[融资{dim5:.0f}]数据不足")

    # 5.2 北向资金（hk_hold）
    hk_rows = _get_hk_hold(code)
    if hk_rows:
        hk = hk_rows[0]
        hk_vol = safe_float(hk.get("vol", 0))
        hk_ratio = safe_float(hk.get("ratio", 0))

        if len(hk_rows) >= 5:
            vol_5d = safe_float(hk_rows[4].get("vol", 0))
            if vol_5d > 0 and hk_vol > vol_5d:
                chg_pct = (hk_vol - vol_5d) / vol_5d * 100
                dim5 += 2
                reasons.append(f"[融资{dim5:.0f}]北向5日+{chg_pct:.1f}%")
        elif hk_vol > 0:
            dim5 += 1
            reasons.append(f"[融资{dim5:.0f}]北向持仓{hk_vol/10000:.0f}万股")

    dim5 = max(-10.0, min(10.0, dim5))
    score += dim5

    # ═══════════════════════════════════════════════════════════
    # 3. 汇总输出
    # ═══════════════════════════════════════════════════════════
    final_score = max(0.0, min(100.0, score))
    final_score = round(final_score, 1)

    # 潜力等级
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

    reason_str = f"[{level}][{data_src}] {'; '.join(reasons)}"
    return final_score, reason_str
