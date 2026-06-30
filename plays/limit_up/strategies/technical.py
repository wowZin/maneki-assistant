#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
技术面涨停潜力预判  — 数据驱动重构

与 V1/V2 核心差异（基于 400 样本 16 涨停的回测发现）:
  1. 量比评分反转: 量比过高 (d>3.0) 预示次日涨停概率降低 (Cohen's d=-0.50)
     原策略奖励高量比 → 错误方向； 奖励适中量比 (1.0-2.5)
  2. 流通市值加成: 小市值弹性是涨停核心条件 (Cohen's d=+0.47)
  3. 一票否决精简: 从 5 条减为 2 条 (放量破位 + 高位滞涨)，减少误杀
  4. 上影线惩罚: 上影/实体 > 1.0 扣分 (卖压明确的负向信号, d=-0.19)
  5. 移除板块协同: 大量 API 调用但信号噪声，技术面评分维度移除
  6. 5 日累计涨幅作为正向信号 (d=+0.17)
  7. 简化均线: 仅保留 MA20 斜率 + 收盘 > MA20，MA 多头排列/MA60 移除

评分维度 (四维度，从六维度精简):
  1. 量能质量 (40pts) — 量比适中区间 + 换手健康 + 洗盘起爆 + 上影惩罚
  2. 趋势位置 (25pts) — MA20 斜率 + 5 日动量 + 5 日阳线
  3. 筹码与市值 (20pts) — 流通市值加成 + 换手衰减 + 布林收敛
  4. 形态确认 (15pts) — 平台突破 + 下影支撑 + 假突破惩罚

一票否决 (仅 2 条):
  1. 放量破位: 收盘 < MA20 且 量比 > 2.5
  2. 高位滞涨: 近 20 日涨幅 > 60% 且 换手 > 25% 且 上影/实体 > 1.5
"""

import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_DIR))
sys.path.insert(0, str(PROJECT_DIR / "scripts"))

from scripts.tu_share import call_tushare  # noqa: E402
from plays.limit_up.utils import safe_float_none, is_trading_time  # noqa: E402
from plays.limit_up.pipeline import _get_realtime_fund_cache  # noqa: E402


def score_technical(code: str) -> tuple[int | float, str]:
    """
    技术面 : 数据驱动的涨停潜力预判 (0-100)

    基于 400 样本 16 涨停的回测重构。
    核心原则:
      - 量比适中 (1.0-2.5) 是正向信号，过高是负向信号
      - 小市值是基础优势条件
      - 上影线是明确的卖压信号
      - 少量关键因子比大量启发式规则更有效
    """
    safe_float = safe_float_none

    # ── 1. 数据获取 ─────────────────────────────────────
    try:
        _stk_fields = (
            "trade_date,close,open,high,low,pre_close,change,pct_change,vol,amount,"
            "vol_ratio,turnover_rate,ma_bfq_20,ma_bfq_60,"
            "boll_upper_bfq,boll_mid_bfq,boll_lower_bfq,total_mv"
        )
        resp = call_tushare("stk_factor_pro", {"ts_code": code}, _stk_fields)
        factor_data = resp.get("data", {})
        factor_items = factor_data.get("items", [])
        factor_fields = factor_data.get("fields", [])
    except Exception:
        return 50, "技术数据获取失败"

    if not factor_items:
        return 50, "技术数据不足"

    factors = [dict(zip(factor_fields, item)) for item in factor_items]
    factors.sort(key=lambda x: x.get("trade_date", ""), reverse=True)

    if len(factors) < 5:
        return 50, "技术数据不足(需>=5日)"

    # ── 2. 当日核心数据提取 ─────────────────────────────
    today = factors[0]

    close = safe_float(today.get("close"))
    open_price = safe_float(today.get("open"))
    high = safe_float(today.get("high"))
    low = safe_float(today.get("low"))
    vol_ratio = safe_float(today.get("vol_ratio"))
    turnover = safe_float(today.get("turnover_rate"))
    ma20 = safe_float(today.get("ma_bfq_20"))
    boll_mid = safe_float(today.get("boll_mid_bfq"))
    total_mv = safe_float(today.get("total_mv"))  # 万元
    today_vol = safe_float(today.get("vol"))
    pre_close = safe_float(today.get("pre_close"))
    # 日内振幅 = (最高-最低)/前收*100
    amplitude = ((high - low) / pre_close * 100) if high and low and pre_close else None
    # T-1 量比 (用于量比加速检测)
    prev_vol_ratio = safe_float(factors[1].get("vol_ratio")) if len(factors) > 1 else None
    # 上/下影线比
    body = abs(close - open_price) if close and open_price else 0
    upper_shadow = (high - max(close, open_price)) if high and close and open_price else 0
    lower_shadow = (min(close, open_price) - low) if low and close and open_price else 0
    upper_ratio = (upper_shadow / body) if body > 0 else 0
    lower_ratio = (lower_shadow / body) if body > 0 else 0

    # 盘中优先使用实时量比（替代 T-1 vol_ratio）
    if is_trading_time():
        fund_cache = _get_realtime_fund_cache()
        code_short = code.split(".")[0]
        rt = fund_cache.get(code_short, {})
        if rt.get("vol_ratio", 0) > 0:
            vol_ratio = rt["vol_ratio"]

    # vol_ratio 可能为 None (stk_factor_pro 不返回此字段)
    # 优先从实时缓存取, 降级从 daily_basic 取
    if vol_ratio is None:
        try:
            resp_db = call_tushare("daily_basic", {"ts_code": code}, "volume_ratio,turnover_rate")
            db_items = resp_db.get("data", {}).get("items", [])
            if db_items:
                db_fields = resp_db.get("data", {}).get("fields", [])
                db_d = dict(zip(db_fields, db_items[0]))
                vol_ratio = safe_float(db_d.get("volume_ratio"))
                if turnover is None:
                    turnover = safe_float(db_d.get("turnover_rate"))
        except: pass
    # 核心字段校验 (放宽: close必须有, vol_ratio允许降级后仍为None)
    if close is None:
        return 50, "核心数据缺失(close)"
    # 量比最终降级: 仍为None时默认中性值1.0
    if vol_ratio is None:
        vol_ratio = 1.0

    # ── 3. 历史动态统计（用于百分位计算） ─────────────────
    vol_ratios_hist = []
    turnovers_hist = []
    for f in factors[:80]:
        vr = safe_float(f.get("vol_ratio"))
        tr = safe_float(f.get("turnover_rate"))
        if vr is not None:
            vol_ratios_hist.append(vr)
        if tr is not None:
            turnovers_hist.append(tr)

    def pctile(arr, p):
        """计算 arr 的第 p 百分位 (0-100)"""
        if not arr:
            return 0
        s = sorted(arr)
        idx = int(len(s) * p / 100)
        return s[min(idx, len(s) - 1)]

    # ═══════════════════════════════════════════════════════
    # 4. 一票否决规则（仅 2 条，从 5 条精简）
    # ═══════════════════════════════════════════════════════

    # 4.1 放量破位: 收盘 < MA20 且 量比 > 2.5
    # 阈值从 > Top20% 放宽至 > 2.5，减少误杀
    if close and ma20 and close < ma20:
        if vol_ratio and vol_ratio > 2.5:
            return 0, f"放量破位:收盘{close:.2f}<MA20={ma20:.2f},量比{vol_ratio:.2f}>2.5"

    # 4.2 高位滞涨: 阶段涨幅 > 60% + 换手 > 25% + 长上影
    if len(factors) >= 20 and turnover:
        lows_20d = [safe_float(factors[i].get("low")) for i in range(20)]
        stage_low = min((lv for lv in lows_20d if lv is not None), default=None)
        if stage_low and close and stage_low > 0:
            stage_gain = (close - stage_low) / stage_low * 100
            if stage_gain > 60 and turnover > 25:
                if high and open_price:
                    body = abs(close - open_price)
                    upper_shadow = high - max(close, open_price)
                    if body > 0 and upper_shadow / body > 1.5:
                        return 0, (
                            f"高位滞涨:涨幅{stage_gain:.0f}%,"
                            f"换手{turnover:.1f}%,长上影"
                        )

    # ═══════════════════════════════════════════════════════
    # 4.3 竞价跌停开盘预检: 提前拉取auc_gap供否决使用
    auc_gap = None
    try:
        resp_auc = call_tushare("stk_auction", {"ts_code": code}, "price,pre_close")
        auc_items = resp_auc.get("data", {}).get("items", [])
        if auc_items:
            auc_f = resp_auc.get("data", {}).get("fields", [])
            auc_d = dict(zip(auc_f, auc_items[0]))
            auc_p = safe_float(auc_d.get("price"))
            auc_pre = safe_float(auc_d.get("pre_close"))
            if auc_pre > 0: auc_gap = (auc_p - auc_pre) / auc_pre * 100
    except: pass
    if auc_gap is not None and auc_gap < -9:
        return 0, f"竞价跌停开盘:竞价{auc_gap:.1f}%"

    # 5. 四维度评分
    # ═══════════════════════════════════════════════════════
    score = 0
    reasons = []

    # ── 5.1 量能质量 (40pts) — 最高权重，数据区分力最强 ──
    vol_score = 0
    vol_reasons = []

    # 5.1a 量比适中区间评分 (0-15pts)
    # Cohen's d = -0.50: 量比过高强烈负向，最佳区间 1.0-2.5
    if vol_ratio:
        if 1.2 <= vol_ratio <= 2.0:
            vol_score += 15
            vol_reasons.append(f"量比适中({vol_ratio:.2f})+15")
        elif 2.0 < vol_ratio <= 2.5:
            vol_score += 12
            vol_reasons.append(f"量比略高({vol_ratio:.2f})+12")
        elif 1.0 <= vol_ratio < 1.2:
            vol_score += 10
            vol_reasons.append(f"量比正常({vol_ratio:.2f})+10")
        elif 2.5 < vol_ratio <= 3.0:
            vol_score += 7
            vol_reasons.append(f"量比偏高({vol_ratio:.2f})+7")
        elif 0.7 <= vol_ratio < 1.0:
            vol_score += 5
            vol_reasons.append(f"量比略低({vol_ratio:.2f})+5")
        elif 3.0 < vol_ratio <= 4.0:
            vol_score += 2
            vol_reasons.append(f"量比过高({vol_ratio:.2f})+2")
        else:  # > 4.0 或 < 0.7
            vol_score += 0
            vol_reasons.append(f"量比极端({vol_ratio:.2f})+0")

    # 5.1b 换手健康度 (0-10pts)
    # Cohen's d = +0.07 (微弱正向): 适度活跃换手正向，极端值有害
    if turnover:
        if 5 <= turnover <= 12:
            vol_score += 10
            vol_reasons.append(f"换手活跃({turnover:.1f}%)+10")
        elif 3 <= turnover < 5:
            vol_score += 7
            vol_reasons.append(f"换手温和({turnover:.1f}%)+7")
        elif 12 < turnover <= 15:
            vol_score += 5
            vol_reasons.append(f"换手偏高({turnover:.1f}%)+5")
        elif 1.5 <= turnover < 3:
            vol_score += 3
            vol_reasons.append(f"换手偏低({turnover:.1f}%)+3")
        elif turnover < 1.5:
            vol_score -= 5
            vol_reasons.append(f"换手极低({turnover:.1f}%)-5")
        # 过热惩罚 (>20%)
        if turnover > 20:
            vol_score -= 5
            vol_reasons.append(f"换手过热({turnover:.1f}%)-5")

    # 5.1c 竞价跳空因子 (0-5pts, d=+0.15~0.17)
    # auc_gap 已在否决预检阶段拉取, 此处复用; 若为None则降级为0
    if auc_gap is None:
        auc_gap = 0
    if 1 <= auc_gap <= 5:
        vol_score += 5; vol_reasons.append(f"竞价高开{auc_gap:.1f}%+5")
    elif 0 < auc_gap < 1:
        vol_score += 2; vol_reasons.append(f"竞价平开{auc_gap:.1f}%+2")
    elif -2 <= auc_gap < 0:
        vol_score -= 2; vol_reasons.append(f"竞价低开{auc_gap:.1f}%-2")
    elif -5 <= auc_gap < -2:
        vol_score -= 8; vol_reasons.append(f"竞价大幅低开{auc_gap:.1f}%-8")
    elif auc_gap < -5:
        vol_score -= 15; vol_reasons.append(f"竞价暴跌{auc_gap:.1f}%-15")

    # 5.1d 振幅因子 (0-6pts, d=-0.39: 低振幅蓄力>高振幅出货)
    if amplitude and amplitude < 8:
        vol_score += 6; vol_reasons.append(f"振幅温和{amplitude:.1f}%+6")
    elif amplitude and amplitude > 15:
        vol_score -= 5; vol_reasons.append(f"振幅过大{amplitude:.1f}%-5")

    # 5.1e 洗盘起爆检测 (0-10pts)
    # 5.1e 量比加速过滤 (假阳性杀手: vol_accel>2.5=出货)
    if vol_ratio and prev_vol_ratio and prev_vol_ratio > 0:
        vol_accel = vol_ratio / prev_vol_ratio
        if vol_accel > 2.5:
            vol_score -= 15; vol_reasons.append(f"量比暴涨{vol_accel:.1f}x-15")
        elif vol_accel > 1.8:
            vol_score -= 8; vol_reasons.append(f"量比急升{vol_accel:.1f}x-8")
        elif 0.7 <= vol_accel <= 1.3:
            vol_score += 5; vol_reasons.append(f"量比稳健{vol_accel:.1f}x+5")

    # 前 N 日缩量 + 当日量比 1.0-2.5 → 洗盘充分后起爆
    if vol_ratio and len(factors) >= 4:
        low_vol_days = 0
        for i in range(1, min(7, len(factors))):
            vr_i = safe_float(factors[i].get("vol_ratio"))
            if vr_i is not None and vr_i < 0.8:
                low_vol_days += 1
        if low_vol_days >= 2 and 1.0 <= vol_ratio <= 2.5:
            vol_score += 10
            vol_reasons.append(f"洗盘起爆(前{low_vol_days}日缩量)+10")
        elif low_vol_days >= 1 and 1.0 <= vol_ratio <= 3.0:
            vol_score += 5
            vol_reasons.append(f"轻度洗盘(前{low_vol_days}日缩量)+5")

    # 5.1d 量能放大确认 (0-5pts)
    # 今日量 > 前 3 日均量 1.2 倍 且 量比在合理区间
    if today_vol and len(factors) >= 4:
        vol_3d_sum = 0
        vol_3d_count = 0
        for i in range(1, 4):
            v = safe_float(factors[i].get("vol"))
            if v is not None and v > 0:
                vol_3d_sum += v
                vol_3d_count += 1
        if vol_3d_count == 3:
            vol_3d_avg = vol_3d_sum / 3
            if today_vol > vol_3d_avg * 1.2 and 1.0 <= vol_ratio <= 3.0:
                vol_score += 5
                vol_reasons.append("放量确认(量>3日均+20%)+5")

    # 5.1e 上影线惩罚 (负向信号, Cohen's d = -0.19)
    # 上影/实体 > 1.0 → 卖压明显 -5; 极端 (> 2.0) → -8
    if high and open_price and close and open_price > 0:
        body = abs(close - open_price)
        upper = high - max(close, open_price)
        if body > 0:
            us_ratio = upper / body
            if us_ratio > 2.0:
                vol_score -= 8
                vol_reasons.append(f"上影极长({us_ratio:.1f}:1)-8")
            elif us_ratio > 1.0:
                vol_score -= 5
                vol_reasons.append(f"上影过长({us_ratio:.1f}:1)-5")

    score += max(0, min(40, vol_score))
    reasons.extend(vol_reasons)

    # ── 5.2 趋势位置 (25pts) — 简化：仅 MA20 斜率 + 5 日动量 ──
    trend_score = 0
    trend_reasons = []

    # 5.2a 收盘 vs MA20 (0-8pts)
    # 硬标准：收盘 > MA20 是基础趋势确认
    if close and ma20:
        if close > ma20:
            pct_above = (close / ma20 - 1) * 100
            if pct_above >= 3:
                trend_score += 8
                trend_reasons.append(f"强势>MA20(+{pct_above:.1f}%)+8")
            else:
                trend_score += 5
                trend_reasons.append("站上MA20+5")
        else:
            trend_score -= 8
            trend_reasons.append("收盘<MA20-8")

    # 5.2b MA20 斜率 (0-7pts)
    # 仅检查 MA20 是否上倾（2 日对比），不检查 MA5/MA10 排列
    if ma20 and len(factors) >= 3:
        ma20_prev = safe_float(factors[2].get("ma_bfq_20"))
        if ma20_prev and ma20_prev > 0:
            slope_pct = (ma20 / ma20_prev - 1) * 100
            if slope_pct > 0.5:
                trend_score += 7
                trend_reasons.append(f"MA20上倾+7")
            elif slope_pct > 0:
                trend_score += 4
                trend_reasons.append(f"MA20微倾+4")

    # 5.2c 5 日累计涨幅 (0-5pts, Cohen's d = +0.17)
    # 正向动量是次日涨停的正向信号
    if len(factors) >= 5:
        close_5ago = safe_float(factors[4].get("close"))
        if close_5ago and close_5ago > 0:
            pct_5d = (close / close_5ago - 1) * 100
            if 5 <= pct_5d <= 15:
                trend_score += 5
                trend_reasons.append(f"5日累计+{pct_5d:.1f}%+5")
            elif 2 <= pct_5d < 5:
                trend_score += 3
                trend_reasons.append(f"5日累计+{pct_5d:.1f}%+3")
            elif 0 <= pct_5d < 2:
                trend_score += 1
                trend_reasons.append(f"5日微涨+1")
            elif pct_5d < -3:
                trend_score -= 3
                trend_reasons.append(f"5日累计{pct_5d:.1f}%-3")

    # 5.2d 5 日正向天数 (0-5pts, Cohen's d = +0.14)
    # 阳线多 = 趋势健康
    if len(factors) >= 5:
        positive_days = 0
        for i in range(5):
            pc = safe_float(factors[i].get("pct_change"))
            if pc is not None and pc > 0:
                positive_days += 1
        if positive_days >= 3:
            trend_score += positive_days
            trend_reasons.append(f"5日{positive_days}阳+{positive_days}")

    score += max(0, min(25, trend_score))
    reasons.extend(trend_reasons)

    # ── 5.3 筹码与市值 (20pts) — 市值加成 + 换手衰减 + 布林收敛 ──
    chip_score = 0
    chip_reasons = []

    # 5.3a 流通市值加成 (0-10pts, Cohen's d = +0.47)
    # 小市值弹性是涨停的核心条件之一
    # total_mv 单位: 万元 (Tushare stk_factor_pro)
    if total_mv and total_mv > 0:
        mv_yi = total_mv / 10000  # 万元 → 亿
        if mv_yi < 30:
            chip_score += 10
            chip_reasons.append(f"微型市值({mv_yi:.0f}亿)+10")
        elif mv_yi < 50:
            chip_score += 8
            chip_reasons.append(f"小市值({mv_yi:.0f}亿)+8")
        elif mv_yi < 100:
            chip_score += 5
            chip_reasons.append(f"中市值({mv_yi:.0f}亿)+5")
        elif mv_yi < 200:
            chip_score += 2
            chip_reasons.append(f"大市值({mv_yi:.0f}亿)+2")
        else:
            chip_reasons.append(f"超大市值({mv_yi:.0f}亿)+0")
    else:
        # total_mv 缺失时不扣分也不加分
        chip_reasons.append("市值数据缺失+0")

    # 5.3b 换手衰减检测 (0-5pts)
    # 换手逐日递减 = 筹码锁定良好
    if len(factors) >= 5:
        tr_5d = [safe_float(factors[i].get("turnover_rate")) for i in range(5)]
        valid_tr = [t for t in tr_5d if t is not None]
        if len(valid_tr) >= 4:
            latest_tr = valid_tr[0]
            avg_rest = sum(valid_tr[1:]) / (len(valid_tr) - 1) if len(valid_tr) > 1 else valid_tr[0]
            if avg_rest > 0 and latest_tr < avg_rest:
                chip_score += 5
                chip_reasons.append("换手递减锁定+5")

    # 5.3c 布林带宽收敛 (0-5pts)
    # 波动收敛 = 变盘前兆，筹码充分沉淀
    if len(factors) >= 20:
        bw_list = []
        for i in range(20):
            bu = safe_float(factors[i].get("boll_upper_bfq"))
            bl = safe_float(factors[i].get("boll_lower_bfq"))
            bm = safe_float(factors[i].get("boll_mid_bfq"))
            if bu is not None and bl is not None and bm is not None and bm > 0:
                bw_list.append((bu - bl) / bm * 100)
        if bw_list:
            bw_cur = bw_list[0]
            bw_p30 = pctile(bw_list, 30)
            if bw_cur > 0 and bw_p30 > 0 and bw_cur <= bw_p30:
                chip_score += 5
                chip_reasons.append(f"布林收敛(bw{bw_cur:.1f}%)+5")

    score += max(0, min(20, chip_score))
    reasons.extend(chip_reasons)

    # ── 5.4 形态确认 (15pts) — 平台突破 + 下影支撑 ──
    pattern_score = 0
    pattern_reasons = []

    # 5.4a 平台突破 (0-10pts)
    # 近 10 日窄幅盘整 (振幅 < 15%) + 收盘突破高点 + 量比适中确认
    if close and high and low and len(factors) >= 10:
        highs_10d = []
        lows_10d = []
        for i in range(10):
            h = safe_float(factors[i].get("high"))
            lv = safe_float(factors[i].get("low"))
            if h is not None and lv is not None:
                highs_10d.append(h)
                lows_10d.append(lv)
        if highs_10d and lows_10d:
            max_h = max(highs_10d)
            min_l = min(lows_10d)
            range_amp = (max_h - min_l) / max_h * 100 if max_h > 0 else 100

            if range_amp < 15 and close > max_h * 0.99:
                if 1.0 <= vol_ratio <= 3.0:
                    pattern_score += 10
                    pattern_reasons.append(
                        f"平台突破(振幅{range_amp:.1f}%,量比{vol_ratio:.1f})+10"
                    )
                else:
                    pattern_score += 5
                    pattern_reasons.append(f"平台盘整(振幅{range_amp:.1f}%)+5")

    # 5.4b 下影支撑 (0-5pts)
    # 下影线明显 (> 实体 30%) = 买方承接力强
    if close and open_price and low and open_price > 0:
        body = abs(close - open_price)
        lower_shadow = min(close, open_price) - low
        if body > 0 and lower_shadow / body > 0.3:
            pattern_score += 5
            pattern_reasons.append("下影支撑+5")

    # 5.4c 假突破惩罚 (0 to -5pts)
    # 近 3 日 >= 2 天长上影 = 反复试探失败，卖压持续
    if len(factors) >= 3:
        us_count = 0
        for i in range(3):
            h = safe_float(factors[i].get("high"))
            c = safe_float(factors[i].get("close"))
            o = safe_float(factors[i].get("open"))
            if h is not None and c is not None and o is not None:
                b = abs(c - o)
                u = h - max(c, o)
                if u > 0 and b > 0 and u / b > 1.0:
                    us_count += 1
        if us_count >= 2:
            pattern_score -= 5
            pattern_reasons.append(f"假突破({us_count}日长上影)-5")

    score += max(0, min(15, pattern_score))
    reasons.extend(pattern_reasons)

    # ═══════════════════════════════════════════════════════
    # 6. 综合评定
    # ═══════════════════════════════════════════════════════
    final_score = max(0, min(100, score))

    if final_score >= 75:
        level = "高"
    elif final_score >= 55:
        level = "中"
    elif final_score >= 35:
        level = "低"
    else:
        level = "无"

    reason_str = (
        f"[{level}] " + "; ".join(reasons[:8])
        if reasons
        else f"[{level}] 无明显信号"
    )

    return final_score, reason_str
