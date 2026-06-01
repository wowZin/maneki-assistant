#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_DIR))
sys.path.insert(0, str(PROJECT_DIR / "scripts"))

from scripts.tu_share import call_tushare, get_industry, get_industry_peers  # noqa: E402
from plays.limit_up.utils import safe_float_none, is_trading_time  # noqa: E402
from plays.limit_up.pipeline import _get_realtime_fund_cache  # noqa: E402

# 模块级缓存：行业 → {code: {close, ma_bfq_20}}
# 单次扫描中多个候选股同行业时避重复API调用
_INDUSTRY_MA20_CACHE = {}


def score_technical(code):
    """
    技术面涨停潜力预判 V2.0（最终实盘定稿版）
    六维度量化评分：量能35分 + 趋势均线25分 + 筹码结构15分 + 关键位置12分 + 资金动能8分 + 板块协同5分
    含一票否决规则（动态阈值）+ 板块弱势天花板 + 时间因子

    V2.0变更（基于V1.0）：
    - 量能维度权重40→35，新增板块协同维度5分
    - 否决规则全面重构（5条动态阈值）：放量破位/高位滞涨/高位筹码发散/缩量阴跌/资金出逃
    - 所有阈值改为"滚动80日分位数+绝对值下限"双重判定
    - 筹码维度废除CYQ估算，改用换手衰减+布林带收敛替代
    - 删除MACD/KDJ/RSI因子（因子纯化）
    - 新增板块协同过滤维度（弱势板块硬性天花板总分≤74）
    - 新增时间因子（早盘冲动抑制/尾盘确认加分/信号衰减）
    - 新增洗盘-起爆节奏因子（前N日缩量+当日放量）
    """


    # 使用模块级 safe_float_none（区分None和0）
    safe_float = safe_float_none
    try:
        _stk_fields = "trade_date,close,open,high,low,pre_close,change,pct_change,vol,amount," \
                      "vol_ratio,turnover_rate,ma_bfq_5,ma_bfq_10,ma_bfq_20,ma_bfq_60," \
                      "macd_dif_bfq,macd_dea_bfq,macd_bfq,kdj_k_bfq,kdj_d_bfq," \
                      "rsi_bfq_6,boll_upper_bfq,boll_mid_bfq,boll_lower_bfq"
        resp = call_tushare("stk_factor_pro", {"ts_code": code}, _stk_fields)
        factor_data = resp.get("data", {})
        factor_items = factor_data.get("items", [])
        factor_fields = factor_data.get("fields", [])
    except Exception:
        return 50, "技术数据获取失败"

    if not factor_items:
        return 50, "技术数据不足"

    # 构建因子字典列表（按日期降序）
    factors = [dict(zip(factor_fields, item)) for item in factor_items]
    factors.sort(key=lambda x: x.get('trade_date', ''), reverse=True)

    # 盘中场景：Tushare数据为T-1日，无当日实时数据
    # 量比/换手等实时指标应从CDP获取，当前V1.0暂用T-1日数据
    if is_trading_time() and factors:
        latest_date = factors[0].get('trade_date', '')
        from datetime import datetime as _dt
        today_str = _dt.now().strftime("%Y%m%d")
        if latest_date < today_str:
            pass  # T-1数据，盘中可用但非实时

    if len(factors) < 3:
        return 50, "技术数据不足"

    today = factors[0]
    factors[1] if len(factors) > 1 else {}

    # 获取资金流向
    try:
        resp = call_tushare("moneyflow", {"ts_code": code}, "trade_date,net_mf_amount,buy_lg_amount,sell_lg_amount")
        mf_data = resp.get("data", {}).get("items", [])
    except Exception:
        mf_data = []

    # ===== 动态分位数计算（V2.0） =====
    # 近80日数据（从factors中取），用于动态阈值判定
    vol_ratios_80d = []
    turnovers_80d = []
    pct_chgs = []
    for f in factors[:80]:
        vr = safe_float(f.get('vol_ratio'))
        tr = safe_float(f.get('turnover_rate'))
        pc = safe_float(f.get('pct_change'))
        if vr: vol_ratios_80d.append(vr)  # noqa: E701
        if tr: turnovers_80d.append(tr)  # noqa: E701
        if pc is not None: pct_chgs.append(pc)  # noqa: E701

    def pctile(arr, p):
        if not arr: return 0  # noqa: E701
        s = sorted(arr)
        idx = int(len(s) * p / 100)
        return s[min(idx, len(s)-1)]

    vr_top20 = pctile(vol_ratios_80d, 80)
    vr_bot10 = pctile(vol_ratios_80d, 10)
    vr_bot30 = pctile(vol_ratios_80d, 30)
    vr_median = pctile(vol_ratios_80d, 50)
    tr_top5 = pctile(turnovers_80d, 95)
    pctile(turnovers_80d, 80)
    tr_bot10 = pctile(turnovers_80d, 10)
    tr_20pct = pctile(turnovers_80d, 20)
    tr_80pct = pctile(turnovers_80d, 80)

    close = safe_float(today.get('close'))
    ma20 = safe_float(today.get('ma_bfq_20'))
    vol_ratio = safe_float(today.get('vol_ratio'))
    # V2.4: 盘中优先使用东财实时量比(替代T-1)
    if is_trading_time():
        fund_cache = _get_realtime_fund_cache()
        code_short = code.split('.')[0]
        rt = fund_cache.get(code_short, {})
        if rt.get('vol_ratio', 0) > 0:
            vol_ratio = rt['vol_ratio']
    boll_mid = safe_float(today.get('boll_mid_bfq'))

    # ===== 2. V2.0 一票否决检查 =====

    # 2.1 放量破位：收盘<MA20 且 量比>近80日Top20%（且绝对值>1.5）
    if close and ma20 and close < ma20:
        if vol_ratio and (vol_ratio > vr_top20 or vol_ratio > 1.5):
            return 0, f"放量破位:收盘{close:.2f}<MA20={ma20:.2f},量比{vol_ratio:.2f}>Top20%"

    # 2.2 高位滞涨：阶段涨幅>60% 且 换手>近80日Top5% 且 长上影
    if len(factors) >= 20:
        lows_20d = [safe_float(factors[i].get('low')) for i in range(20)]
        stage_low = min((low_val for low_val in lows_20d if low_val), default=None)
        if stage_low and close:
            stage_gain = (close - stage_low) / stage_low * 100
            turnover = safe_float(today.get('turnover_rate'))
            high = safe_float(today.get('high'))
            open_price = safe_float(today.get('open'))
            if stage_gain > 60 and turnover and (turnover > tr_top5 or turnover > 25):
                if high and close and open_price:
                    body = abs(close - open_price)
                    upper_shadow = high - max(close, open_price)
                    if body > 0 and upper_shadow / body > 1.5:
                        return 0, f"高位滞涨:涨幅{stage_gain:.0f}%,换手{turnover:.1f}%,长上影"

    # 2.3 高位筹码发散：近3日换手逐日递增且股价滞涨 或 获利盘<15%
    if len(factors) >= 3:
        tr_3d = [safe_float(factors[i].get('turnover_rate')) for i in range(3)]
        pc_3d = [safe_float(factors[i].get('pct_change')) for i in range(3)]
        valid_tr = [t for t in tr_3d if t]
        valid_pc = [p for p in pc_3d if p is not None]
        # 连续递增且累计涨幅<2%→筹码发散
        if len(valid_tr) == 3 and valid_tr[0] > valid_tr[1] > valid_tr[2]:
            if len(valid_pc) == 3 and sum(valid_pc) < 2:
                return 0, f"高位筹码发散:3日换手递增+累计涨{sum(valid_pc):.1f}%<2%"

    # 2.4 持续缩量阴跌：连续3日量比<近80日Bottom10%（且绝对值<0.6）且累计跌幅>3%
    if len(factors) >= 3:
        vr_list = [safe_float(factors[i].get('vol_ratio')) for i in range(3)]
        pc_list = [safe_float(factors[i].get('pct_change')) for i in range(3)]
        valid_vr = [vr for vr in vr_list if vr]
        valid_pc = [pc for pc in pc_list if pc is not None]
        thr = min(vr_bot10, 0.6)
        if len(valid_vr) == 3 and all(vr < thr for vr in valid_vr):
            if len(valid_pc) == 3 and sum(valid_pc) < -3:
                return 0, f"持续缩量阴跌:3日量比<{thr:.2f},累计跌{sum(valid_pc):.1f}%"

    # 2.5 资金持续出逃：近2日资金动能为负 且 分时收盘/VWAP<0.99
    if mf_data and len(mf_data) >= 2:
        net_mf_2d = sum(safe_float(mf_data[i][1]) if len(mf_data[i]) > 1 else 0 for i in range(2))
        if net_mf_2d < 0:
            if close and boll_mid and close / boll_mid < 0.99:
                return 0, f"资金持续出逃:近2日净流出+收盘/中轨{close/boll_mid:.3f}<0.99"

    # ===== 3. 六维度评分（V2.0） =====
    score = 0
    reasons = []

    ma5 = safe_float(today.get('ma_bfq_5'))
    ma10 = safe_float(today.get('ma_bfq_10'))
    turnover = safe_float(today.get('turnover_rate'))
    high = safe_float(today.get('high'))
    low = safe_float(today.get('low'))
    open_price = safe_float(today.get('open'))

    # --- 3.1 量能结构维度 (35分) V2.0 ---
    vol_score = 0
    vol_reasons = []

    # 启动量能质量：量比>近80日Top20%且绝对值>1.5 且 当日量>3日均量1.3倍
    if vol_ratio:
        today_vol = safe_float(today.get('vol'))
        vol_3d = 0
        if len(factors) >= 4:
            vol_3d = sum(safe_float(factors[i].get('vol', 0)) for i in range(1, 4)) / 3

        quality_ok = (vol_ratio > vr_top20 or vol_ratio > 1.5)
        vol_ok = (today_vol and vol_3d > 0 and today_vol > vol_3d * 1.3) if today_vol and vol_3d > 0 else False

        if quality_ok and vol_ok:
            vol_score += 20
            vol_reasons.append(f"放量启动(量比{vol_ratio:.2f}>Top20%)+20")
        elif quality_ok or vol_ok:
            vol_score += 10
            vol_reasons.append(f"量能偏强(量比{vol_ratio:.2f})+10")

    # 洗盘-起爆节奏：前N日中至少2日量比<Bottom30% + 当日放量
    if vol_ratio and len(factors) >= 3:
        low_vol_days = 0
        for i in range(1, min(6, len(factors))):
            vr_i = safe_float(factors[i].get('vol_ratio'))
            if vr_i and (vr_i < vr_bot30 or vr_i < 0.8):
                low_vol_days += 1
        if low_vol_days >= 2 and (vol_ratio > vr_median or vol_ratio > 1.2):
            vol_score += 10
            vol_reasons.append(f"洗盘起爆(前{low_vol_days}日缩量)+10")

    # 换手健康：介于20%-80%分位 且 绝对值>3%且<15%
    if turnover:
        tr_low = min(tr_20pct, 3.0) if tr_20pct > 0 else 3.0
        tr_high = max(tr_80pct, 15.0)
        if tr_low <= turnover <= tr_high:
            vol_score += 5
            vol_reasons.append(f"换手{turnover:.1f}%健康+5")
        elif turnover < tr_bot10 or turnover < 1.5:
            vol_score -= 5
            vol_reasons.append(f"换手{turnover:.1f}%无量-5")
        if turnover > tr_top5 or turnover > 15:
            if high and close and open_price:
                body = abs(close - open_price)
                upper_shadow = high - max(close, open_price)
                if body > 0 and upper_shadow / body > 1.0:
                    vol_score -= 10
                    vol_reasons.append(f"暴量滞涨(换手{turnover:.1f}%)-10")

    score += max(0, min(35, vol_score))
    reasons.extend(vol_reasons)

    # --- 3.2 趋势与均线维度 (25分) V2.0 ---
    trend_score = 0
    trend_reasons = []
    ma60 = safe_float(today.get('ma_bfq_60'))

    # 多头排列：5>10>20日线且20日线斜率>0
    if ma5 and ma10 and ma20:
        if ma5 > ma10 > ma20:
            # 检查20日线斜率
            if len(factors) >= 2:
                ma20_prev = safe_float(factors[1].get('ma_bfq_20'))
                if ma20_prev and ma20 > ma20_prev:
                    trend_score += 15
                    trend_reasons.append("多头排列+15")
                else:
                    trend_score += 10
                    trend_reasons.append("均线多头(斜率平)+10")
            else:
                trend_score += 10
                trend_reasons.append("均线多头+10")
        elif ma5 < ma10 and close and ma20 and close < ma20:
            trend_score -= 10
            trend_reasons.append("空头排列-10")

    # MA60方向
    if ma60 and len(factors) >= 5:
        ma60_5d = safe_float(factors[4].get('ma_bfq_60'))
        if ma60_5d and ma60 < ma60_5d:
            trend_score -= 5
            trend_reasons.append("MA60下倾-5")

    # 回踩企稳：近5日最低触及10/20日线后收盘站回
    if close and ma10 and ma20:
        low_5d = min((safe_float(factors[i].get('low')) or float('inf')) for i in range(min(5, len(factors))))
        # 回踩10日线或20日线
        if low_5d <= ma10 * 1.01 and close > ma10:
            trend_score += 5
            trend_reasons.append("回踩MA10企稳+5")
        elif low_5d <= ma20 * 1.01 and close > ma20:
            trend_score += 3
            trend_reasons.append("回踩MA20企稳+3")
        # 缩量回踩确认
        if len(factors) >= 2 and vol_ratio:
            vr_yest = safe_float(factors[1].get('vol_ratio'))
            if vr_yest and vr_yest < vr_median and (low_5d <= ma10 * 1.01):
                trend_score += 5
                trend_reasons.append("缩量回踩+5")

    # 硬标准：收盘>MA20
    if close and ma20 and close <= ma20:
        trend_score -= 5
        trend_reasons.append("收盘<=MA20-5")

    score += max(0, min(25, trend_score))
    reasons.extend(trend_reasons)

    # --- 3.3 关键位置形态 (12分) V2.0 ---
    pos_score = 0
    pos_reasons = []

    # 平台突破：近N日振幅<15%且布林带宽<近40日Bottom30% + 收盘突破+量比>中位数
    if close and open_price and high and low and len(factors) >= 10:
        # 近10日振幅
        prices_10d = []
        for i in range(10):
            h = safe_float(factors[i].get('high'))
            low_val = safe_float(factors[i].get('low'))
            if h and low_val:
                prices_10d.append((h, low_val))
        if prices_10d:
            max_h = max(p[0] for p in prices_10d)
            min_l = min(p[1] for p in prices_10d)
            range_amplitude = (max_h - min_l) / max_h * 100 if max_h > 0 else 100

            # 布林带宽近40日Bottom30%（简化为带宽当前值）
            boll_upper = safe_float(today.get('boll_upper_bfq'))
            boll_lower = safe_float(today.get('boll_lower_bfq'))
            boll_width_cur = ((boll_upper - boll_lower) / boll_mid * 100) if boll_upper and boll_lower and boll_mid else 100

            if range_amplitude < 15 and boll_width_cur < 15:  # 平台盘整
                if close > max_h * 0.99:  # 收盘接近或突破平台高点
                    if vol_ratio and (vol_ratio > vr_median or vol_ratio > 1.0):
                        pos_score += 8
                        pos_reasons.append(f"平台突破(振幅{range_amplitude:.1f}%)+8")

    # 支撑确认：回踩前高后缩量企稳（下影线/实体<0.3）
    if close and open_price and low:
        body = abs(close - open_price)
        lower_shadow = min(close, open_price) - low
        if body > 0 and lower_shadow / body > 0.3:
            pos_score += 4
            pos_reasons.append("下影支撑+4")

    # 负向：上影线>60%
    if len(factors) >= 3:
        us_cnt = 0
        for i in range(3):
            h = safe_float(factors[i].get('high'))
            c = safe_float(factors[i].get('close'))
            o = safe_float(factors[i].get('open'))
            if h and c and o:
                b = abs(c - o)
                u = h - max(c, o)
                if u > 0 and b > 0 and u / b > 1.0:
                    us_cnt += 1
        if us_cnt >= 2:
            pos_score -= 4
            pos_reasons.append(f"假突破:3日{us_cnt}日上影-4")

    score += max(0, min(12, pos_score))
    reasons.extend(pos_reasons)

    # --- 3.4 筹码结构 (15分) V2.0 换手衰减替代CYQ ---
    chip_score = 0
    chip_reasons = []

    # 换手衰减良好：近5日换手逐日递减（斜率<0）
    if len(factors) >= 5 and turnover:
        tr_5d = [safe_float(factors[i].get('turnover_rate')) for i in range(5)]
        valid_tr5 = [t for t in tr_5d if t]
        if len(valid_tr5) == 5:
            tr_decreasing = all(valid_tr5[i] >= valid_tr5[i+1] for i in range(4))
            latest_tr = valid_tr5[0]
            avg_tr5 = sum(valid_tr5) / 5
            if tr_decreasing and latest_tr < avg_tr5:
                chip_score += 10
                chip_reasons.append("换手递减锁定+10")
        elif len(valid_tr5) >= 3:
            latest_tr = valid_tr5[0]
            avg_tr = sum(valid_tr5) / len(valid_tr5)
            if latest_tr < avg_tr:
                chip_score += 5
                chip_reasons.append("换手趋降+5")

    # 布林带宽收敛：处于近20日Bottom30%（波动收敛=筹码锁定）
    if len(factors) >= 20:
        bw_20d = []
        for i in range(20):
            bu = safe_float(factors[i].get('boll_upper_bfq'))
            bl = safe_float(factors[i].get('boll_lower_bfq'))
            bm = safe_float(factors[i].get('boll_mid_bfq'))
            if bu and bl and bm:
                bw_20d.append((bu - bl) / bm * 100)
        boll_width_cur = bw_20d[0] if bw_20d else 0
        bw_pct = pctile(bw_20d, 30) if bw_20d else 0
        if boll_width_cur > 0 and bw_pct > 0 and boll_width_cur <= bw_pct:
            chip_score += 5
            chip_reasons.append(f"波动收敛(带宽{boll_width_cur:.1f}%)+5")

    # 负向：高位换手递增+滞涨（已在否决2.3处理，这里再扣分）
    if len(factors) >= 3:
        tr_3d_chip = [safe_float(factors[i].get('turnover_rate')) for i in range(3)]
        pc_3d_chip = [safe_float(factors[i].get('pct_change')) for i in range(3)]
        valid_tr3c = [t for t in tr_3d_chip if t]
        valid_pc3c = [p for p in pc_3d_chip if p is not None]
        if len(valid_tr3c) == 3 and valid_tr3c[0] > valid_tr3c[1] > valid_tr3c[2]:
            if len(valid_pc3c) == 3 and sum(valid_pc3c) < 3:
                chip_score -= 10
                chip_reasons.append("换手递增滞涨-10")

    score += max(0, min(15, chip_score))
    reasons.extend(chip_reasons)

    # --- 3.5 日内资金动能 (8分) V2.0 ---
    capital_score = 0
    capital_reasons = []

    # 资金动能：外盘-内盘差用Level1代理（close/MA关系）
    # V2.0: 收盘>BOLL中轨+量比>中位数 → 资金正向
    if close and boll_mid:
        vw_ratio = close / boll_mid
        if vw_ratio > 1.01 and vol_ratio and vol_ratio > vr_median:
            capital_score += 5
            capital_reasons.append(f"资金正向(价>中轨+量比{vol_ratio:.2f})+5")
        elif vw_ratio > 1.01:
            capital_score += 2
            capital_reasons.append("价>中轨+2")

    # 分时强势：收盘/VWAP>1.01 且 日内最大回撤<VWAP下方1%
    if close and boll_mid:
        vw_ratio = close / boll_mid
        # 日内最大回撤代理：用(低开幅度)衡量
        intraday_drawdown = 0
        if open_price and low and open_price > 0:
            intraday_drawdown = (open_price - low) / open_price * 100
        if vw_ratio > 1.01 and intraday_drawdown < 1.5:
            capital_score += 3
            capital_reasons.append(f"分时强势(回撤{intraday_drawdown:.1f}%)+3")

    # 负向：分时走弱
    if close and boll_mid and close / boll_mid < 0.99:
        capital_score -= 8
        capital_reasons.append("分时走弱(价<中轨)-8")

    score += max(0, min(8, capital_score))
    reasons.extend(capital_reasons)

    # --- 3.6 板块协同过滤 (5分) V2.0新增 ---
    sector_score = 0
    sector_reasons = []
    sector_above_ma20_ratio = 0.5  # 中性的默认值

    try:
        # 获取所属行业（从缓存映射，避免逐只调stock_basic）
        industry = get_industry(code)
        if industry:
            # 获取同行业股票（客户端过滤，Tushare stock_basic不支持industry参数）
            ind_codes = get_industry_peers(industry, limit=20)
            if ind_codes and len(ind_codes) > 1:
                above_ma20_count = 0
                total_count = 0
                # 行业MA20数据缓存：单次扫描内同行业不重复请求
                if industry not in _INDUSTRY_MA20_CACHE:
                    _INDUSTRY_MA20_CACHE[industry] = {}
                    for ind_code in ind_codes:
                        if not ind_code:
                            continue
                        try:
                            resp_daily = call_tushare("stk_factor_pro", {"ts_code": ind_code}, "trade_date,close,ma_bfq_20")
                            d_items = resp_daily.get("data", {}).get("items", [])
                            if d_items:
                                d_fields = resp_daily.get("data", {}).get("fields", [])
                                d_dict = dict(zip(d_fields, d_items[0])) if d_fields else {}
                                _INDUSTRY_MA20_CACHE[industry][ind_code] = {
                                    "close": safe_float(d_dict.get('close')),
                                    "ma20": safe_float(d_dict.get('ma_bfq_20')),
                                }
                        except Exception:
                            pass
                # 从缓存读取
                for ind_code in ind_codes:
                    if not ind_code:
                        continue
                    total_count += 1
                    d = _INDUSTRY_MA20_CACHE[industry].get(ind_code)
                    if d and d.get("close") and d.get("ma20") and d["close"] > d["ma20"]:
                        above_ma20_count += 1
                if total_count > 0:
                    sector_above_ma20_ratio = above_ma20_count / total_count

                    if sector_above_ma20_ratio >= 0.4:
                        sector_score += 5
                        sector_reasons.append(f"板块共振({sector_above_ma20_ratio*100:.0f}%>MA20)+5")
                    elif sector_above_ma20_ratio >= 0.2:
                        sector_score += 2
                        sector_reasons.append(f"板块中性({sector_above_ma20_ratio*100:.0f}%>MA20)+2")
                    else:
                        # 板块弱势天花板：总分上限74
                        sector_reasons.append("板块弱势(<20%>MA20)天花板74分")
    except Exception:
        pass

    score += min(5, sector_score)
    reasons.extend(sector_reasons)

    # ===== 4. V2.0 板块弱势硬性天花板 =====
    if sector_above_ma20_ratio < 0.2 and score > 74:
        score = 74
        reasons.append("板块天花板→74")

    # ===== 5. V2.0 时间因子与信号衰减 =====
    try:
        from datetime import datetime as _dt
        now = _dt.now()
        now_minutes = now.hour * 60 + now.minute
        mkt_open = 9 * 60 + 30  # 09:30
        mkt_close = 15 * 60  # 15:00

        if mkt_open <= now_minutes <= mkt_close:
            # 早盘冲动抑制：10:30前触发，14:00后量比回落则-5分
            early_cutoff = 10 * 60 + 30  # 10:30
            if now_minutes < early_cutoff:
                # 预设标记，需14:00后验证
                pass
            # 尾盘确认加分：14:30后量比>Top20%则+3分
            tail_cutoff = 14 * 60 + 30  # 14:30
            if now_minutes >= tail_cutoff and vol_ratio:
                if vol_ratio > vr_top20 or vol_ratio > 1.5:
                    score += 3
                    reasons.append(f"尾盘确认量比{vol_ratio:.2f}+3")
    except Exception:
        pass

    # ===== 6. 综合评定 =====
    final_score = max(0, min(100, score))

    if final_score >= 75:
        level = "高"
    elif final_score >= 55:
        level = "中"
    elif final_score >= 35:
        level = "低"
    else:
        level = "无"

    reason_str = f"[{level}] " + "; ".join(reasons[:6]) if reasons else f"[{level}] 无明显信号"

    return final_score, reason_str
