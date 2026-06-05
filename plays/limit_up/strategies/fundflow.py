import sys
from pathlib import Path
PROJECT_DIR = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_DIR))
sys.path.insert(0, str(PROJECT_DIR / "scripts"))
from scripts.tu_share import call_tushare  # noqa: E402  # noqa: E402
from plays.limit_up.utils import is_trading_time, list_to_dict, safe_float, safe_int_none  # noqa: E402
import requests  # noqa: E402
from datetime import datetime  # noqa: E402
from plays.limit_up.pipeline import _get_realtime_fund_cache  # noqa: E402

# 工具函数别名
# safe_float = safe_float_none  -- 不别名：safe_float返回0.0（避免None比较崩溃），需要None的用safe_float_none
safe_int = safe_int_none

def score_fundflow(code):
    """
    资金面涨停潜力预判
    五维度量化评分：超大单主力35分 + 龙虎榜机构游资25分 + 分时盘口20分 + 融资聪明资金7分 + 筹码抛压13分
    含一票否决规则（含市场状态调节器+一字板豁免）
    """
    from datetime import datetime

    today = datetime.now().strftime("%Y%m%d")

    # 尝试获取l2api实时数据
    l2_market = None
    l2_vwap = None
    l2_kline = []
    try:
        from scripts.l2_client import has_client, get_client, to_price, to_volume
        if has_client():
            c = get_client()
            l2_market = c.get_market(code)
            if l2_market:
                l2_vwap = c.get_vwap(code)
                l2_kline = c.get_minute_kline(code, n=30)
                # 聚合最近5分钟Tran净流向
                l2_kline[-5:] if len(l2_kline) >= 5 else l2_kline
                # Tran数据通过K线间接反映(volume增量方向)
    except Exception:
        pass
    
    score = 0
    reason = []
    veto_flags = []
    
    # ===== 1. 获取资金流向数据 =====
    # 1.1 个股资金流向（moneyflow）
    moneyflow_data = []
    try:
        resp = call_tushare("moneyflow", {"ts_code": code}, "trade_date,buy_elg_vol,buy_elg_amount,sell_elg_vol,sell_elg_amount,buy_lg_vol,buy_lg_amount,sell_lg_vol,sell_lg_amount,net_mf_vol,net_mf_amount")
        items = resp.get("data", {}).get("items", [])
        fields = resp.get("data", {}).get("fields", [])
        moneyflow_data = list_to_dict(items, fields)
    except Exception:
        pass
    
    # 1.2 龙虎榜交易明细（top_list）
    top_list_data = []
    try:
        resp = call_tushare("top_list", {"ts_code": code, "trade_date": today}, "trade_date,ts_code,name,close,pct_change,turnover_rate,amount,l_sell,l_buy,l_amount,net_amount,net_rate")
        items = resp.get("data", {}).get("items", [])
        fields = resp.get("data", {}).get("fields", [])
        top_list_data = list_to_dict(items, fields)
    except Exception:
        pass
    
    # 1.3 龙虎榜机构交易（top_inst）
    top_inst_data = []
    try:
        resp = call_tushare("top_inst", {"ts_code": code, "trade_date": today}, "trade_date,ts_code,exalter,side,buy,buy_rate,sell,sell_rate,net_buy,reason")
        items = resp.get("data", {}).get("items", [])
        fields = resp.get("data", {}).get("fields", [])
        top_inst_data = list_to_dict(items, fields)
    except Exception:
        pass
    
    # 1.4 北向持股（hk_hold）
    hk_hold_data = []
    try:
        # hk_hold接口参数：ts_code必填，exchange为SH/SZ（港股通市场类型，非股票交易所）
        # 北向资金看的是沪股通/深股通，对应exchange=SH/SZ
        exchange = "SH" if code.endswith(".SH") else "SZ" if code.endswith(".SZ") else ""
        resp = call_tushare("hk_hold", {"ts_code": code, "exchange": exchange}, "trade_date,ts_code,name,vol,ratio,exchange")
        items = resp.get("data", {}).get("items", [])
        fields = resp.get("data", {}).get("fields", [])
        hk_hold_data = list_to_dict(items, fields)
    except Exception:
        pass
    
    # 注：hk_hold的exchange参数是港股通市场类型（SH=沪股通，SZ=深股通），
    # 与股票后缀一致，无需额外转换。
    
    # 1.5 每日基本面数据（获取流通市值）
    daily_basic_data = []
    try:
        resp = call_tushare("daily_basic", {"ts_code": code}, "trade_date,ts_code,close,pct_chg,turnover_rate,turnover_rate_f,volume_ratio,total_mv,circ_mv,amount")
        items = resp.get("data", {}).get("items", [])
        fields = resp.get("data", {}).get("fields", [])
        daily_basic_data = list_to_dict(items, fields)
    except Exception:
        pass

    # 1.6 每日行情数据（获取high/low/pre_close, daily_basic不含这些字段）
    daily_data = []
    try:
        resp = call_tushare("daily", {"ts_code": code}, "trade_date,ts_code,open,high,low,close,pre_close,pct_chg,vol,amount")
        items = resp.get("data", {}).get("items", [])
        fields = resp.get("data", {}).get("fields", [])
        daily_data = list_to_dict(items, fields)
    except Exception:
        pass

    # ===== 2. 一票否决检查 =====
    yiziban_exempt = False  # 一字板豁免标志
    # 2.1 主力持续流出：近3日累计净流出 > 0.5%流通市值
    if moneyflow_data:
        recent_3d = moneyflow_data[:3] if len(moneyflow_data) >= 3 else moneyflow_data
        net_3d = sum([safe_float(x.get("net_mf_amount", 0)) for x in recent_3d])
        if daily_basic_data:
            circ_mv = safe_float(daily_basic_data[0].get("circ_mv", 0)) * 10000  # 万转元
            if circ_mv > 0 and net_3d < -circ_mv * 0.005:  # 流出超0.5%流通市值
                veto_flags.append(f"主力3日净流出{net_3d/10000:.2f}万")
    
    # 2.2 纯散户博弈：主力净占比 < 10%（无大资金控盘）
    # 一字板豁免：一字板(pct_chg≈10%且开板次数=0)无大单成交属正常，主力已高度控盘
    is_yiziban = False  # 一字板标志
    if daily_basic_data:
        pct_chg = safe_float(daily_basic_data[0].get("pct_chg", 0))
        # 用涨跌停数据判断一字板（开板次数=0且涨停）
        try:
            resp_ll = call_tushare("limit_list_d", {"ts_code": code, "trade_date": today}, "trade_date,ts_code,close,pct_chg,open_times,limit")
            ll_items = resp_ll.get("data", {}).get("items", [])
            if ll_items:
                ll_fields = resp_ll.get("data", {}).get("fields", [])
                ll_data = list_to_dict(ll_items, ll_fields)
                if ll_data:
                    open_times = safe_float(ll_data[0].get("open_times", 1))
                    limit_type = str(ll_data[0].get("limit", "")).upper()
                    if limit_type == "U" and open_times == 0:
                        is_yiziban = True
        except Exception:
            pass
        # 简化判断：涨幅>=9.9%也视为一字板候选（limit_list可能无数据）
        if not is_yiziban and pct_chg >= 9.9:
            is_yiziban = True
    
    # 否决4 — 纯散户博弈（3日累计豁免）
    # <5% + 3日累计净流入≤0 → 否决
    # <5% + 3日累计净流入>0 → 不否决，维度1占比因子扣-5分
    main_ratio_for_dim1 = None  # 保存主力占比供维度1使用
    dim1_veto4_deduction = 0  # 否决4豁免后的维度1扣分标记
    
    # 计算3日累计净流入（用于否决4豁免判定）
    net_3d_for_veto4 = 0
    if moneyflow_data:
        recent_3d = moneyflow_data[:3] if len(moneyflow_data) >= 3 else moneyflow_data
        net_3d_for_veto4 = sum([safe_float(x.get("net_mf_amount", 0)) for x in recent_3d])
    
    if not is_yiziban:
        # 优先实时东财数据，降级 Tushare T+1
        fund_cache = _get_realtime_fund_cache()
        code_short = code.split('.')[0]
        rt = fund_cache.get(code_short, {})
        rt_net = rt.get("net_flow", 0)
        rt_amt = rt.get("amount", 0)

        if rt_amt > 0:
            # 实时数据可用：主力净流入 / 成交额
            main_ratio = rt_net / rt_amt * 100
            main_ratio_for_dim1 = main_ratio
            src_tag = ""
        elif moneyflow_data:
            # 降级：Tushare T+1
            latest = moneyflow_data[0]
            buy_elg = safe_float(latest.get("buy_elg_amount", 0))
            sell_elg = safe_float(latest.get("sell_elg_amount", 0))
            buy_lg = safe_float(latest.get("buy_lg_amount", 0))
            sell_lg = safe_float(latest.get("sell_lg_amount", 0))
            net_elg = buy_elg - sell_elg
            net_lg = buy_lg - sell_lg
            # 分母用全市场成交额(daily_basic.amount, 千元转元), 非仅超大单+大单
            total_amount = safe_float(daily_basic_data[0].get("amount", 0)) * 1000 if daily_basic_data else 0
            if total_amount > 0:
                main_ratio = (net_elg + net_lg) / total_amount * 100
                main_ratio_for_dim1 = main_ratio
                src_tag = "[T-1]"

        if main_ratio_for_dim1 is not None:
            if main_ratio_for_dim1 < 5:
                if net_3d_for_veto4 <= 0:
                    veto_flags.append(f"纯散户博弈{src_tag}:主力净占比{main_ratio_for_dim1:.1f}%<5%+3日累计净流入{net_3d_for_veto4:.0f}万≤0")
                else:
                    dim1_veto4_deduction = -5  # 豁免否决，转入维度1扣分
    elif is_yiziban:
        # 一字板豁免：不触发否决，但记录豁免信息到reason
        yiziban_exempt = True
    
    # 2.3 龙虎榜大额撤离：机构/游资净卖出 > 净买入2倍
    if top_inst_data:
        total_net_buy = 0
        total_net_sell = 0
        for inst in top_inst_data:
            nb = safe_float(inst.get("net_buy", 0))
            if nb > 0:
                total_net_buy += nb
            else:
                total_net_sell += abs(nb)
        if total_net_sell > total_net_buy * 2 and total_net_sell > 1000:  # 万元
            veto_flags.append(f"龙虎榜净卖出{total_net_sell:.0f}万")
    
    # 否决3 — 分时资金背离（增加尾盘抢筹豁免+换手率/累计流入双重验证）
    corr_threshold = -0.6  # 默认相关系数阈值
    if daily_basic_data:
        try:
            # 获取全市场成交额和跌停家数判断市场状态
            resp_info = call_tushare("daily_info", {"trade_date": today, "ts_code": "SSE"}, "trade_date,ts_code,amount")
            info_items = resp_info.get("data", {}).get("items", [])
            market_amount = 0
            if info_items:
                info_fields = resp_info.get("data", {}).get("fields", [])
                info_data = list_to_dict(info_items, info_fields)
                if info_data:
                    market_amount = safe_float(info_data[0].get("amount", 0))  # 万元
            # 获取跌停家数
            limit_down_cnt = 0
            try:
                resp_ld = call_tushare("limit_list_d", {"trade_date": today, "limit_type": "D"}, "trade_date,ts_code")
                ld_items = resp_ld.get("data", {}).get("items", [])
                limit_down_cnt = len(ld_items) if ld_items else 0
            except Exception:
                pass
            # 低迷市判定：全市场成交额<8000亿(80000000万) 或 跌停>20家
            if market_amount < 80000000 or limit_down_cnt > 20:
                corr_threshold = -0.75
        except Exception:
            pass
    
    # 查找 T-1日换手率和当日收盘位置（用于否决3豁免判定）
    t_turnover_rate = 0
    if daily_basic_data:
        t_turnover_rate = safe_float(daily_basic_data[0].get("turnover_rate", 0))
    
    if moneyflow_data and daily_basic_data:
    # Tushare T+1 散户接盘检查
        latest_basic = daily_basic_data[0]
        pct_change = safe_float(latest_basic.get("pct_chg", 0))
        if pct_change > 3:
            latest_mf = moneyflow_data[0]
            net_mf = safe_float(latest_mf.get("net_mf_amount", 0))
            if net_mf < 0:
                if corr_threshold < -0.6:
                    pass  # 低迷市放宽
                else:
                    # 尾盘抢筹豁免（同上，用Tushare数据）
                    if "high" in latest_basic:
                        close = safe_float(latest_basic.get("close", 0))
                        high = safe_float(latest_basic.get("high", 0))
                    else:
                        close = high = 0
                    close_high_ratio = close / high if high > 0 else 0
                    if close_high_ratio > 0.92 and (t_turnover_rate < 15 or net_3d_for_veto4 > 0):
                        pass  # 豁免
                    else:
                        veto_flags.append(f"资金背离[T+1]:涨{pct_change:.1f}%但净流出{abs(net_mf)/10000:.0f}万")
    
    # 否决5 — 观测窗口内走势恶化（l2api分钟K线实时检测）
    dim3_recent_penalty = 0
    if l2_kline and len(l2_kline) >= 5:
        recent = l2_kline[-5:]  # 最近5分钟
        # 检查价格趋势: 最近3根是否持续走低
        last_3 = l2_kline[-3:]
        if len(last_3) == 3:
            closes = [b.get("close", 0) for b in last_3]
            highs = [b.get("high", 0) for b in last_3]
            if closes[-1] < closes[-2] < closes[-3] and highs[-1] < highs[-3]:
                dim3_recent_penalty += 8
                reason.append("[近期走弱]最近3分钟价格+高点持续下行-8")
            elif closes[-1] < closes[-2] and highs[-1] < highs[-2]:
                dim3_recent_penalty += 4
                reason.append("[近期松动]最近2分钟走弱-4")
        # 检查量价背离: 价格横盘但成交量萎缩
        if len(recent) >= 5:
            recent_vols = [b.get("volume", 0) for b in recent]
            recent_closes = [b.get("close", 0) for b in recent]
            price_range = max(recent_closes) - min(recent_closes)
            avg_vol_first2 = (recent_vols[0] + recent_vols[1]) / 2 if len(recent_vols) >= 2 else 0
            avg_vol_last2 = (recent_vols[-2] + recent_vols[-1]) / 2 if len(recent_vols) >= 2 else 0
            # 价格窄幅(<1%) + 成交量萎缩>30% = 高位横盘出货
            close_avg = sum(recent_closes) / len(recent_closes)
            if close_avg > 0 and price_range / close_avg < 0.01 and avg_vol_first2 > 0 and avg_vol_last2 / avg_vol_first2 < 0.7:
                dim3_recent_penalty += 6
                reason.append("[高位滞涨]近5分钟价窄量缩-6")

    # 否决6 — 尾盘集中兑现（优先l2api分钟K线，降级日频代理）
    dim3_tail_penalty = 0
    if l2_kline and len(l2_kline) >= 5:
        # 有分钟K线: 精确检测尾盘走弱
        tail_bars = [b for b in l2_kline if b.get("time", "") >= "1430"]
        if tail_bars:
            # 14:30后成交量占比
            tail_vol = sum(b.get("volume", 0) for b in tail_bars)
            total_vol = sum(b.get("volume", 0) for b in l2_kline)
            tail_vol_ratio = tail_vol / total_vol if total_vol > 0 else 0
            # 尾盘均价 vs 全天VWAP
            if tail_vol > 0:
                tail_vwap = sum(b.get("amount", 0) for b in tail_bars) / tail_vol
                full_vwap = sum(b.get("amount", 0) for b in l2_kline) / total_vol if total_vol > 0 else 0
                tail_weak = tail_vwap < full_vwap and tail_vol_ratio > 0.25  # 尾盘放量下跌
                if tail_weak:
                    dim3_tail_penalty += 8
                    reason.append(f"[尾盘走弱L2]尾盘{tail_vol_ratio:.0%}量+均价下行-8")
                elif tail_vol_ratio > 0.35 and tail_vwap < full_vwap * 0.995:
                    dim3_tail_penalty += 5
                    reason.append(f"[尾盘松动L2]尾盘{tail_vol_ratio:.0%}量-5")
    else:
        # 降级日频代理 (原逻辑)
        if daily_data:
            close = safe_float(daily_data[0].get("close", 0))
            high = safe_float(daily_data[0].get("high", 0))
            low = safe_float(daily_data[0].get("low", 0))
            pct_chg = safe_float(daily_data[0].get("pct_chg", 0))
            avg_price_proxy = (high + low) / 2 if high > 0 and low > 0 else 0
            below_avg = close < avg_price_proxy if avg_price_proxy > 0 else False
            if moneyflow_data:
                latest_mf = moneyflow_data[0]
                net_mf = safe_float(latest_mf.get("net_mf_amount", 0))
                if net_mf < 0 and below_avg:
                    dim3_tail_penalty += 5
                    reason.append(f"[尾盘走弱]净流出{abs(net_mf)/10000:.0f}万+低于均价-5")
                if pct_chg < 0 and net_mf < 0 and below_avg:
                    dim3_tail_penalty += 5
                    reason.append("[尾盘走弱]收跌+净流出+低于均价-5")
    
    # 触发否决直接返回
    if veto_flags:
        return 0, f"否决: {'; '.join(veto_flags)}"
    
    # ===== 3. 维度1：超大单主力净流入（35分）=====
    dim1_score = 0
    dim1_reason = []
    
    # --- 规模阈值因子(15分)：主力净流入占成交额比例 ---
    # 优先实时(Eastmoney f62)，降级 Tushare T+1
    fund_cache = _get_realtime_fund_cache()
    code_short = code.split('.')[0]
    rt_data = fund_cache.get(code_short, {})
    rt_net_flow = rt_data.get("net_flow", 0)
    rt_amount = rt_data.get("amount", 0)
    
    if rt_amount > 0:
        rt_net_ratio = rt_net_flow / rt_amount * 100
        if rt_net_ratio >= 3:
            dim1_score += 25
            dim1_reason.append(f"主力净流入[实时]{rt_net_ratio:.1f}%+25")
        elif rt_net_ratio < 0.1:
            dim1_score -= 15
            dim1_reason.append(f"主力净流入[实时]{rt_net_ratio:.1f}%-15")
    elif moneyflow_data and daily_basic_data:
        # 降级 Tushare T+1
        latest = moneyflow_data[0]
        buy_elg = safe_float(latest.get("buy_elg_amount", 0))
        sell_elg = safe_float(latest.get("sell_elg_amount", 0))
        buy_lg = safe_float(latest.get("buy_lg_amount", 0))
        sell_lg = safe_float(latest.get("sell_lg_amount", 0))
        main_net = (buy_elg - sell_elg) + (buy_lg - sell_lg)
        circ_mv = safe_float(daily_basic_data[0].get("circ_mv", 0)) * 10000  # 万转元
        if circ_mv > 0:
            main_net_ratio = main_net / circ_mv * 100
            if main_net_ratio >= 0.3:
                dim1_score += 15
                dim1_reason.append(f"主力净流入[T-1]{main_net_ratio:.2f}%+15")
            elif main_net_ratio < 0.1:
                dim1_score -= 15  # 从-5修正为-15(与文档对齐)
                dim1_reason.append(f"主力净流入[T-1]{main_net_ratio:.2f}%-15")
    
    # --- 占比健康因子(10分)：主力净占比梯度评分 ---
    # >30%: +10分，15%-30%: 0分(中性)，5%-15%: -5分(偏弱)，<5%: 已在否决区拦截
    # main_ratio_for_dim1 已在否决2.2阶段计算（优先实时，降级T+1）
    _has_rt = False  # 初始化：后续代码块外引用需要
    if main_ratio_for_dim1 is not None:
        main_ratio = main_ratio_for_dim1
        _rt_fund = _get_realtime_fund_cache()
        _has_rt = bool(_rt_fund.get(code_short, {}).get("amount", 0))
        src_tag = "" if _has_rt else "[T-1]"
        if main_ratio > 30:
            dim1_score += 10
            dim1_reason.append(f"主力占比{src_tag}{main_ratio:.1f}%+10")
        elif main_ratio >= 5 and main_ratio < 15:  # 5%-15%偏弱扣分
            dim1_score -= 5
            dim1_reason.append(f"主力占比偏弱{src_tag}{main_ratio:.1f}%-5")

    # 否决4豁免 — 主力净占比<5%但3日累计净流入>0，转入维度1扣-5分
    if dim1_veto4_deduction != 0:
        dim1_score += dim1_veto4_deduction
        dim1_reason.append("否决4豁免(3日累计>0)-5")
    elif not _has_rt and moneyflow_data:
        # 降级：Tushare T+1（实时数据不可用时）
        latest = moneyflow_data[0]
        buy_elg = safe_float(latest.get("buy_elg_amount", 0))
        sell_elg = safe_float(latest.get("sell_elg_amount", 0))
        buy_lg = safe_float(latest.get("buy_lg_amount", 0))
        sell_lg = safe_float(latest.get("sell_lg_amount", 0))
        main_net = (buy_elg - sell_elg) + (buy_lg - sell_lg)
        total_amount = safe_float(daily_basic_data[0].get("amount", 0)) * 1000 if daily_basic_data else 0
        if total_amount > 0:
            main_ratio = main_net / total_amount * 100
            if main_ratio > 30:
                dim1_score += 10
                dim1_reason.append(f"主力占比[T-1]{main_ratio:.1f}%+10")
            elif main_ratio >= 5 and main_ratio < 15:
                dim1_score -= 5
                dim1_reason.append(f"主力占比偏弱[T-1]{main_ratio:.1f}%-5")
    
    # --- 持续抢筹因子(10分)：近3日连续净流入（仍用Tushare历史数据） ---
    if moneyflow_data and len(moneyflow_data) >= 3:
        net_3d = [safe_float(x.get("net_mf_amount", 0)) for x in moneyflow_data[:3]]
        if all(n > 0 for n in net_3d):
            dim1_score += 10
            dim1_reason.append("连续3日净流入+10")
    
    # --- 散户接盘因子 量化+豁免(-20分)：主力流出但散户接盘 ---
    # 触发条件（三者同时满足）：
    # ① 主力净额 < 0
    # ② (中单净额 + 小单净额) > 成交额的8%
    # ③ 当日股价涨幅 > 3%
    # 豁免条件（满足任一即不扣分）：
    # ① 当日涨停且换手率在5%-25%区间（换手板属健康博弈）
    # ② 近3日主力累计净流入 > 0（主力前期已进场）
    retail_retail_exempt = False
    # 取换手率（散户接盘豁免判定用）
    retail_turnover_rate = t_turnover_rate if 't_turnover_rate' in dir() else 0
    pct_change_for_retail = safe_float(daily_basic_data[0].get("pct_chg", 0)) if daily_basic_data else 0
    
    if moneyflow_data:
        latest = moneyflow_data[0]
        buy_elg = safe_float(latest.get("buy_elg_amount", 0))
        sell_elg = safe_float(latest.get("sell_elg_amount", 0))
        buy_lg = safe_float(latest.get("buy_lg_amount", 0))
        sell_lg = safe_float(latest.get("sell_lg_amount", 0))
        net_mf = safe_float(latest.get("net_mf_amount", 0))
        main_net = (buy_elg - sell_elg) + (buy_lg - sell_lg)
        # Tushare降级分支也需要做豁免判定
        if main_net < 0 and net_mf > 0:
            # 豁免①：涨停+换手5%-25%
            if pct_change_for_retail >= 9.5 and 5 <= retail_turnover_rate <= 25:
                retail_retail_exempt = True
            # 豁免②：3日累计净流入>0
            elif net_3d_for_veto4 > 0:
                retail_retail_exempt = True
            if not retail_retail_exempt:
                dim1_score -= 20
                dim1_reason.append("散户接盘[T-1]-20")
    
    dim1_score = max(0, min(35, dim1_score))
    score += dim1_score
    if dim1_reason:
        reason.append(f"[主力{dim1_score}分] {' '.join(dim1_reason)}")
    
    # ===== 4. 维度2：龙虎榜机构游资（25分）=====
    dim2_score = 0
    dim2_reason = []
    
    if is_trading_time():
        # 盘中方案：从大单代理改为封板质量因子(T-1日limit_list)
        # 消除与维度1(大单流入)的共线性，封板质量反映游资/机构合力
        limit_list_data = []
        try:
            resp = call_tushare("limit_list_d", {"ts_code": code}, "trade_date,ts_code,close,pct_chg,open_times,fd_amount,first_time,last_time,up_stat,limit")
            items = resp.get("data", {}).get("items", [])
            fields = resp.get("data", {}).get("fields", [])
            limit_list_data = list_to_dict(items, fields)
        except Exception:
            pass
        
        if limit_list_data:
            # 取T-1日数据(最新一条)
            latest_ll = limit_list_data[0]
            fd_amount = safe_float(latest_ll.get("fd_amount", 0))  # 封单金额(万)
            first_time = latest_ll.get("first_time", "")  # 首次涨停时间
            open_times = safe_float(latest_ll.get("open_times", 1))  # 开板次数
            limit_type = str(latest_ll.get("limit", "")).upper()
            
            # 封板强度：封单金额 > 流通市值1%
            if daily_basic_data and fd_amount > 0:
                circ_mv = safe_float(daily_basic_data[0].get("circ_mv", 0))  # 万
                if circ_mv > 0:
                    fd_ratio = fd_amount / circ_mv * 100
                    if fd_ratio >= 1:
                        dim2_score += 10
                        dim2_reason.append(f"[盘中]封单{fd_ratio:.1f}%流通市值+10")
                    elif fd_ratio < 0.3:
                        dim2_score -= 10
                        dim2_reason.append(f"[盘中]封单弱{fd_ratio:.1f}%-10")
            
            # 首封时间：<10:30(早封板=游资合力强)
            if first_time and limit_type == "U":
                try:
                    hhmm = first_time.replace(":", "")
                    if hhmm < "103000":
                        dim2_score += 8
                        dim2_reason.append(f"[盘中]早封板{first_time}+8")
                    elif hhmm > "140000":
                        dim2_score -= 8
                        dim2_reason.append(f"[盘中]尾板{first_time}-8")
                except Exception:
                    pass
            
            # 开板次数：0次(一字板/秒封)+7分，1次+3分，>=3次扣7分
            if limit_type == "U":
                if open_times == 0:
                    dim2_score += 7
                    dim2_reason.append("[盘中]秒封/一字板+7")
                elif open_times == 1:
                    dim2_score += 3
                    dim2_reason.append("[盘中]开板1次+3")
                elif open_times >= 3:
                    dim2_score -= 7
                    dim2_reason.append(f"[盘中]开板{int(open_times)}次-7")
            
            # 首板豁免：T-1日非涨停股但T日涨幅>7% → 不适用"未涨停扣15分"，按0分处理
            if limit_type != "U":
                # 检查T日是否正在拉升（涨幅>7%），若是则豁免
                t_day_pct = 0
                if daily_basic_data:
                    t_day_pct = safe_float(daily_basic_data[0].get("pct_chg", 0))
                if t_day_pct > 7:
                    pass  # 首板豁免：不扣分
                else:
                    dim2_score -= 15
                    dim2_reason.append("[盘中]T-1未涨停-15")
        else:
            # 无limit_list数据（非涨停股），给基础分0
            dim2_reason.append("[盘中]无封板数据")
    else:
        # 盘后方案：Tushare龙虎榜
        inst_net_buy = 0
        hot_money_net_buy = 0
        
        if top_inst_data:
            for inst in top_inst_data:
                nb = safe_float(inst.get("net_buy", 0))
                exalter = inst.get("exalter", "")
                # 机构席位
                if "机构" in exalter or "专用" in exalter:
                    inst_net_buy += nb
                else:
                    hot_money_net_buy += nb
            
            # 资金合力：机构+游资净买入 > 3000万
            total_net = inst_net_buy + hot_money_net_buy
            if total_net > 3000:
                dim2_score += 12
                dim2_reason.append(f"机构游资净买{total_net/10000:.2f}亿+12")
            
            # 席位主导：单一席位净买入占比 > 40%
            if total_net > 0:
                max_seat = max(abs(inst_net_buy), abs(hot_money_net_buy))
                if max_seat / total_net > 0.4:
                    dim2_score += 8
                    dim2_reason.append("席位主导+8")
        
        # 龙虎榜上榜且净流入
        if top_list_data:
            latest_top = top_list_data[0]
            net_rate = safe_float(latest_top.get("net_rate", 0))
            if net_rate > 0:
                dim2_score += 5
                dim2_reason.append(f"龙虎榜净买率{net_rate:.1f}%+5")
    
    dim2_score = max(0, min(25, dim2_score))
    score += dim2_score
    if dim2_reason:
        reason.append(f"[龙虎{dim2_score}分] {' '.join(dim2_reason)}")
    
    # ===== 5. 维度3：分时盘口资金抢筹（20分）=====
    dim3_score = 0
    dim3_reason = []

    # --- l2api实时因子 (优先) ---
    if l2_market:
        last = to_price(l2_market.get("last", "0"))
        total_bid_vol = to_volume(l2_market.get("total_bid_volume", "0"))
        total_ask_vol = to_volume(l2_market.get("total_ask_volume", "0"))

        # 盘口买卖比: 买盘总量/卖盘总量 > 1.2 → 买方强势
        if total_bid_vol > 0 and total_ask_vol > 0:
            ob_ratio = total_bid_vol / total_ask_vol
            if ob_ratio > 1.5:
                dim3_score += 8
                dim3_reason.append(f"盘口买压{ob_ratio:.1f}x+8")
            elif ob_ratio > 1.2:
                dim3_score += 5
                dim3_reason.append(f"盘口偏买{ob_ratio:.1f}x+5")
            elif ob_ratio < 0.7:
                dim3_score -= 5
                dim3_reason.append(f"盘口偏卖{ob_ratio:.1f}x-5")

        # VWAP乖离率: 价格在VWAP上方<1% → 多头控盘
        if l2_vwap and l2_vwap > 0:
            vwap_dev = (last - l2_vwap) / l2_vwap
            if 0 < vwap_dev < 0.01:
                dim3_score += 6
                dim3_reason.append(f"VWAP上方{vwap_dev:.2%}+6")
            elif -0.005 < vwap_dev <= 0:
                dim3_score += 4
                dim3_reason.append(f"VWAP附近{vwap_dev:.2%}+4")
            elif vwap_dev < -0.02:
                dim3_score -= 5
                dim3_reason.append(f"VWAP下方{vwap_dev:.2%}-5")

        # 卖盘压单检测: 卖一档堆量远大于买一档 → 拉高出货
        ask_qtys_raw = [to_volume(q) for q in l2_market.get("ask_qty", [])]
        bid_qtys_raw = [to_volume(q) for q in l2_market.get("bid_qty", [])]
        ask1_qty = ask_qtys_raw[0] if len(ask_qtys_raw) > 0 else 0
        bid1_qty = bid_qtys_raw[0] if len(bid_qtys_raw) > 0 else 0
        ask_top3 = sum(ask_qtys_raw[:3])
        bid_top3 = sum(bid_qtys_raw[:3])
        if ask1_qty > 0 and bid1_qty > 0:
            if ask1_qty > bid1_qty * 5:
                dim3_score -= 10
                dim3_reason.append(f"卖一压单{ask1_qty/10000:.0f}万手(买一{bid1_qty/10000:.0f}万手×5)-10")
            elif ask1_qty > bid1_qty * 3:
                dim3_score -= 6
                dim3_reason.append(f"卖盘堆量{ask1_qty/10000:.0f}万手(买一{bid1_qty/10000:.0f}万手×3)-6")
            elif ask1_qty > bid1_qty * 2:
                dim3_score -= 3
                dim3_reason.append(f"卖盘偏重{ask1_qty/10000:.0f}万手(买一{bid1_qty/10000:.0f}万手×2)-3")
        # 前三档卖盘总量 > 买盘总量2倍
        if ask_top3 > 0 and bid_top3 > 0 and ask_top3 > bid_top3 * 2:
            dim3_score -= 4
            dim3_reason.append(f"卖盘深度压倒买盘(卖{ask_top3/10000:.0f}vs买{bid_top3/10000:.0f}万手)-4")

    # --- 日频代理因子 (优先实时东财，降级 T+1) ---
    # 无 L2 时，用东财实时缓存替代 T+1 moneyflow 的 net_mf / turnover_rate
    _rt_fund3 = _get_realtime_fund_cache()
    _rt3 = _rt_fund3.get(code.split('.')[0], {})
    _rt_has_data = bool(_rt3.get("amount", 0))

    if _rt_has_data:
        _net_mf = _rt3.get("net_flow", 0)       # 实时主力净流入(元)
        _turnover = _rt3.get("turnover", 0)      # 实时换手率(%)
        _use_rt = True
    elif moneyflow_data:
        _net_mf = safe_float(moneyflow_data[0].get("net_mf_amount", 0)) * 10000 if moneyflow_data else 0  # 万→元
        _turnover = safe_float(daily_basic_data[0].get("turnover_rate", 0)) if daily_basic_data else 0
        _use_rt = False
    else:
        _net_mf = 0
        _turnover = 0
        _use_rt = False

    if (_use_rt and _rt3) or (moneyflow_data and daily_data):
        net_mf = _net_mf
        turnover_rate = _turnover
        src_tag = "" if _use_rt else "[T-1]"

        if net_mf > 0:
            if not _use_rt and daily_data:
                # T+1: 有完整 OHLC 做形态判断
                latest_daily = daily_data[0]
                close = safe_float(latest_daily.get("close", 0))
                low = safe_float(latest_daily.get("low", 0))
                high = safe_float(latest_daily.get("high", 0))
                pre_close = safe_float(latest_daily.get("pre_close", 0))
            else:
                # 实时: 跳过日频形态判断(需要OHLC)，仅凭净流入加分
                close = low = high = pre_close = 0

            if close > 0 and pre_close > 0:
                low_ok = low >= pre_close * 0.99 if pre_close > 0 else True
                close_high_ok = close / high > 0.95 if high > 0 else True
                if low_ok and close_high_ok:
                    dim3_score += 10
                    dim3_reason.append(f"持续净流入{net_mf/100000000:.2f}亿+10")

                amplitude = (high - low) / pre_close * 100 if pre_close > 0 else 0
                if turnover_rate > 3 and amplitude > 0 and (turnover_rate / amplitude) > 2.0:
                    dim3_score += 6
                    dim3_reason.append(f"强承接换手/振幅{max(0,turnover_rate/amplitude if amplitude>0 else 0):.1f}+6")
            elif net_mf > 0:
                # 无 OHLC 数据，简化加分
                dim3_score += 8
                dim3_reason.append(f"实时净流入{net_mf/100000000:.2f}亿+8")

            if not _use_rt and moneyflow_data:
                # 超大单主导判定(仅 T+1 有此数据粒度)
                latest = moneyflow_data[0]
                buy_elg = safe_float(latest.get("buy_elg_amount", 0))
                sell_elg = safe_float(latest.get("sell_elg_amount", 0))
                buy_lg = safe_float(latest.get("buy_lg_amount", 0))
                sell_lg = safe_float(latest.get("sell_lg_amount", 0))
                elg_net = buy_elg - sell_elg
                if elg_net > 0 and net_mf > 0:
                    dim3_score += 4
                    dim3_reason.append("超大单主导+4")
        else:
            if not l2_market:  # 仅当无l2api时清空
                dim3_score = 0
                dim3_reason.append(f"净流出{abs(net_mf)/100000000:.2f}亿")

        # 负向过滤（优先实时涨跌幅，降级 T+1）
        if _use_rt:
            from plays.limit_up.pipeline import _batch_fetch_realtime_pct as _rt_pct
            _pct_cache = _rt_pct()
            _pct_val = _pct_cache.get(code.split('.')[0])
            try:
                pct_chg = float(_pct_val) if _pct_val is not None else 0
            except (ValueError, TypeError):
                pct_chg = 0
        else:
            pct_chg = safe_float(daily_basic_data[0].get("pct_chg", 0))
        is_zt = (pct_chg >= 9.5)
        is_negative_triggered = False

        if pct_chg > 5 and turnover_rate < 2 and not is_zt:
            dim3_score -= 12
            dim3_reason.append(f"拉升无量{pct_chg:.1f}%换手{turnover_rate:.1f}%-12")
            is_negative_triggered = True
        if not is_negative_triggered and pct_chg < -3 and turnover_rate > 8:
            dim3_score -= 12
            dim3_reason.append(f"放量出逃{pct_chg:.1f}%换手{turnover_rate:.1f}%-12")
        if not is_negative_triggered and net_mf < 0 and turnover_rate > 10:
            dim3_score -= 12
            dim3_reason.append(f"对倒嫌疑换手{turnover_rate:.1f}%-12")

    # 尾盘走弱扣分（l2api精确或日频代理）
    dim3_score -= dim3_recent_penalty
    dim3_score -= dim3_tail_penalty

    dim3_score = max(0, min(20, dim3_score))  # l2api上线,上限恢复20
    score += dim3_score
    if dim3_reason:
        reason.append(f"[盘口{dim3_score}分] {' '.join(dim3_reason)}")
    
    # ===== 6. 维度4：融资与聪明资金（7分）=====
    dim4_score = 0
    dim4_reason = []
    
    if is_trading_time():
        # 盘中方案：从大单代理改为融资余额增速(T-1日margin_detail)
        # 消除与维度1(大单流入)的共线性，融资增速反映杠杆聪明钱
        margin_data = []
        try:
            resp = call_tushare("margin_detail", {"ts_code": code}, "trade_date,ts_code,rzye,rqye,rzmre,rqmcl,rzrqye")
            items = resp.get("data", {}).get("items", [])
            fields = resp.get("data", {}).get("fields", [])
            margin_data = list_to_dict(items, fields)
        except Exception:
            pass
        
        if margin_data and len(margin_data) >= 5:
            # 融资持续增长：近5日融资余额连续增长(按时间降序, [0]最新)
            # 连续增长 => [0] >= [1] >= [2] >= [3] >= [4]
            rzye_list = [safe_float(x.get("rzye", 0)) for x in margin_data[:5]]
            if all(rzye_list[i] >= rzye_list[i+1] for i in range(len(rzye_list)-1)) and rzye_list[0] > rzye_list[-1]:
                dim4_score += 3
                dim4_reason.append("[盘中]融资5日连增+3")
            
            # 融资活跃度：当日融资买入额占成交额比例>8%
            rzmre = safe_float(margin_data[0].get("rzmre", 0))  # 融资买入额(元)
            if daily_basic_data and rzmre > 0:
                # 成交额从moneyflow获取
                if moneyflow_data:
                    total_amount = sum([
                        safe_float(margin_data[0].get("rzmre", 0)),
                        abs(safe_float(moneyflow_data[0].get("net_mf_amount", 0)))
                    ])
                else:
                    total_amount = rzmre * 10  # 粗估
                if total_amount > 0:
                    rz_ratio = rzmre / total_amount * 100
                    if rz_ratio > 8:
                        dim4_score += 2
                        dim4_reason.append(f"[盘中]融资买入占比{rz_ratio:.1f}%+2")
            
            # 融资共振：融资余额增速与超大单净流入同向
            rz_chg = safe_float(margin_data[0].get("rzye", 0)) - safe_float(margin_data[1].get("rzye", 0)) if len(margin_data) > 1 else 0
            if moneyflow_data:
                main_net = safe_float(moneyflow_data[0].get("net_mf_amount", 0))
                if (rz_chg > 0 and main_net > 0) or (rz_chg < 0 and main_net < 0):
                    dim4_score += 2
                    dim4_reason.append("[盘中]融资主力同向+2")
            
            # 负向：融资余额连续3日下降
            if len(margin_data) >= 3:
                rz_3d = [safe_float(x.get("rzye", 0)) for x in margin_data[:3]]
                if rz_3d[0] < rz_3d[1] < rz_3d[2]:  # 按时间降序，连续下降
                    dim4_score -= 5
                    dim4_reason.append("[盘中]融资3日连降-5")
        elif margin_data:
            # 数据不足5日
            dim4_reason.append("[盘中]融资数据不足5日")
        else:
            dim4_reason.append("[盘中]无融资数据")
    else:
        # 盘后方案：Tushare hk_hold
        if hk_hold_data:
            # 当日北向持股变动
            latest_hold = hk_hold_data[0]
            latest_vol = safe_float(latest_hold.get("vol", 0))
            latest_ratio = safe_float(latest_hold.get("ratio", 0))
            
            # 近5日持仓变动
            if len(hk_hold_data) >= 5:
                vol_5d_ago = safe_float(hk_hold_data[4].get("vol", 0))
                if vol_5d_ago > 0:
                    vol_chg = (latest_vol - vol_5d_ago) / vol_5d_ago * 100
                    # 持续增持
                    if latest_vol > vol_5d_ago:
                        dim4_score += 6
                        dim4_reason.append(f"北向5日增持{vol_chg:.1f}%+6")
                    # 筹码锁定
                    if len(hk_hold_data) >= 2:
                        ratio_prev = safe_float(hk_hold_data[1].get("ratio", 0))
                        ratio_delta = latest_ratio - ratio_prev
                        if ratio_delta > 0.05:
                            dim4_score += 4
                            dim4_reason.append(f"持股占比+{ratio_delta:.2f}%+4")
            else:
                # 数据不足，当日北向持股 > 0 即可
                if latest_vol > 0:
                    dim4_score += 4
                    dim4_reason.append(f"北向持股{latest_vol/10000:.2f}万股+4")
    
    dim4_score = max(0, min(7, dim4_score))
    score += dim4_score
    if dim4_reason:
        reason.append(f"[融资{dim4_score}分] {' '.join(dim4_reason)}")
    
    # ===== 7. 维度5：筹码抛压与锁仓（13分）=====
    dim5_score = 0
    dim5_reason = []
    
    if moneyflow_data and len(moneyflow_data) >= 3:
        # 近3日净流入数据（按时间降序：[0]今天、[1]昨天、[2]前天）
        net_flows = [safe_float(x.get("net_mf_amount", 0)) for x in moneyflow_data[:3]]
        # 净流入递增 = 锁仓度高（前天<=昨天<=今天）
        if net_flows[2] <= net_flows[1] <= net_flows[0] and net_flows[0] > 0:
            dim5_score += 7
            dim5_reason.append("流入递增锁仓+7")
        
        # 无大幅流出 = 抛压可控
        if all(n >= 0 for n in net_flows):
            dim5_score += 3
            dim5_reason.append("无抛压+3")
        
        # 净流入加速（今日>前2日均值1.5倍）
        if net_flows[0] > 0 and len(net_flows) >= 3:
            avg_2d = (abs(net_flows[1]) + abs(net_flows[2])) / 2
            if avg_2d > 0 and net_flows[0] > avg_2d * 1.5:
                dim5_score += 3
                dim5_reason.append("流入加速+3")
    
    dim5_score = max(0, min(13, dim5_score))
    score += dim5_score
    if dim5_reason:
        reason.append(f"[锁仓{dim5_score}分] {' '.join(dim5_reason)}")
    
    # ===== 8. 返回结果 =====
    if yiziban_exempt:
        reason.append("[豁免]一字板跳过散户博弈否决")
    if not reason:
        reason.append("[无] 无明显资金信号")
    
    final_score = min(100, score)
    
    # 53-56分边缘区间二次确认
    if 53 <= final_score <= 56:
        # 二次确认：维度1≥21(60%) AND 维度5≥8(60%) → 升级为高潜力
        if dim1_score >= 21 and dim5_score >= 8:
            level = "高"
            reason.append("[边缘升级]维1≥21+维5≥8→高潜力")
        else:
            level = "中"
            reason.append("[边缘确认]维持中等潜力")
    elif final_score >= 75:
        level = "高"
    elif final_score >= 55:
        level = "中"
    elif final_score >= 35:
        level = "低"
    else:
        level = "无"
    
    return final_score, f"[{level}] " + "; ".join(reason)

