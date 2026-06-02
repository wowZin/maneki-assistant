import sys
from pathlib import Path
PROJECT_DIR = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_DIR))
sys.path.insert(0, str(PROJECT_DIR / "scripts"))
from scripts.tu_share import call_tushare  # noqa: E402  # noqa: E402
from plays.limit_up.utils import safe_float_none, safe_int_none, list_to_dict  # noqa: E402
from plays.limit_up.pipeline import _get_popularity_rank, _batch_fetch_realtime_pct  # noqa: E402


def score_sentiment(code):
    """
    情绪面涨停潜力预判
    五维度量化评分：大盘情绪30分 + 主线题材30分 + 板块梯队20分 + 个股人气10分 + 集合竞价15分
    含一票否决规则
    """
    from datetime import datetime, timedelta


    # 使用模块级工具函数（区分None和0）
    safe_float = safe_float_none
    safe_int = safe_int_none
    # list_to_dict 使用模块级定义

    # ===== 0. 获取交易日期 =====
    today_str = datetime.now().strftime('%Y%m%d')

    # ===== 1. 数据获取 =====
    # 1.1 获取个股所属概念板块（离线数据）
    concept_names = []
    try:
        resp = call_tushare("concept_detail", {"ts_code": code}, "id,concept_name")
        data = resp.get("data", {})
        items = data.get("items", [])
        concept_names = [c[1] for c in items if len(c) > 1] if items else []
    except Exception:
        pass

    # 1.2 获取全市场涨停数据（T+1）
    limit_fields = ["trade_date", "ts_code", "name", "close", "pct_chg", "limit", "limit_times", "up_stat"]
    limit_data = []
    try:
        resp = call_tushare("limit_list_d", {"trade_date": today_str}, ",".join(limit_fields))
        data = resp.get("data", {})
        limit_data = list_to_dict(data.get("items", []), limit_fields)
    except Exception:
        pass

    # 1.3 获取连板天梯数据
    step_fields = ["trade_date", "ts_code", "name", "nums"]
    step_data = []
    try:
        resp = call_tushare("limit_step", {"trade_date": today_str}, ",".join(step_fields))
        data = resp.get("data", {})
        step_data = list_to_dict(data.get("items", []), step_fields)
    except Exception:
        pass

    # 1.4 获取龙虎榜数据
    top_list_fields = ["trade_date", "ts_code", "name", "close", "pct_change", "turnover_rate", "amount", "l_sell", "l_buy", "l_amount", "net_amount", "net_rate"]
    top_list_data = []
    try:
        resp = call_tushare("top_list", {"ts_code": code}, ",".join(top_list_fields))
        data = resp.get("data", {})
        top_list_data = list_to_dict(data.get("items", []), top_list_fields)
    except Exception:
        pass

    # 1.5 获取机构买卖明细
    try:
        resp = call_tushare("top_inst", {"trade_date": today_str}, "trade_date,ts_code,exalter,side,buy,buy_rate,sell,sell_rate,net_buy,reason")
        resp.get("data", {}).get("items", [])
    except Exception:
        pass

    # 1.6 获取涨停最强板块统计（概念涨停家数）
    cpt_fields = ["ts_code", "name", "trade_date", "days", "up_stat", "cons_nums", "up_nums", "pct_chg", "rank"]
    cpt_data = []
    try:
        resp = call_tushare("limit_cpt_list", {"trade_date": today_str}, ",".join(cpt_fields))
        data = resp.get("data", {})
        cpt_data = list_to_dict(data.get("items", []), cpt_fields)
    except Exception:
        pass

    # 构建概念→涨停家数映射（含模糊匹配：consum电子 vs consum电子概念）
    concept_ul_cnt = {}
    if cpt_data:
        for cpt in cpt_data:
            cpt_name = cpt.get('name', '')
            up_nums = safe_int(cpt.get('up_nums', 0)) or 0
            if cpt_name and up_nums > 0:
                concept_ul_cnt[cpt_name] = up_nums

    def _get_ul_cnt(concept_name):
        """模糊匹配概念涨停数：处理'消费电子'vs'消费电子概念'类差异"""
        if concept_name in concept_ul_cnt:
            return concept_ul_cnt[concept_name]
        for k, v in concept_ul_cnt.items():
            if concept_name in k or k in concept_name:
                return v
        return 0

    def _has_concept_data(concept_name):
        """检查概念是否在limit_cpt_list中（有数据），避免误杀未收录的概念"""
        if concept_name in concept_ul_cnt:
            return True
        for k in concept_ul_cnt:
            if concept_name in k or k in concept_name:
                return True
        return False
    # 1.7 获取个股昨日成交量（CallVolRatio量纲修正）
    yesterday_vol = 0
    try:
        end_dt = datetime.now() - timedelta(days=5)  # 往前多取几天确保覆盖周末
        resp = call_tushare("daily", {
            "ts_code": code,
            "start_date": end_dt.strftime('%Y%m%d'),
            "end_date": today_str
        }, "trade_date,vol")
        daily_items = resp.get("data", {}).get("items", [])
        if daily_items and len(daily_items) >= 2:
            # 取最近一个交易日的成交量作为yesterday_vol（排除当日）
            # daily接口按日期倒序返回，items[0]是最近的
            # 如果items[0]日期=today，取items[1]；否则取items[0]
            d_fields = resp.get("data", {}).get("fields", [])
            d_dict_list = [dict(zip(d_fields, x)) for x in daily_items if d_fields]
            for d in d_dict_list:
                if d.get('trade_date', '') != today_str:
                    yesterday_vol = safe_float(d.get('vol', 0)) or 0
                    break
            # 如果没找到非今日数据，取items[1]（第二近的日期）
            if yesterday_vol == 0 and len(daily_items) >= 2:
                vol_val = daily_items[1][1] if len(daily_items[1]) > 1 else 0
                yesterday_vol = safe_float(vol_val) or 0
    except Exception:
        pass

    # 1.8 市场状态判定（新增，用于OpenGap乘数）
    # 基于全市场涨跌家数+成交额判定牛市/震荡/熊市态
    market_state = "震荡"  # 默认震荡态
    market_state_multiplier = 1.0  # 默认乘数
    mkt_advance_decline_ratio = 0
    try:
        # 涨跌比：用limit_data中的涨停+跌停数估算（精确值需全市场数据）
        # 更精确方案：获取上证+深证每日交易统计
        resp_sh = call_tushare("daily_info", {"trade_date": today_str, "ts_code": "SSE"}, "trade_date,ts_code,com_count,amount")
        sh_data = resp_sh.get("data", {}).get("items", [])
        resp_sz = call_tushare("daily_info", {"trade_date": today_str, "ts_code": "SZSE"}, "trade_date,ts_code,com_count,amount")
        sz_data = resp_sz.get("data", {}).get("items", [])

        # 获取20日均成交额（取近20个交易日）
        mkt_amount_20d_avg = 0
        resp_info = call_tushare("daily_info", {
            "start_date": (datetime.now() - timedelta(days=30)).strftime('%Y%m%d'),
            "end_date": today_str
        }, "trade_date,amount")
        info_items = resp_info.get("data", {}).get("items", [])
        if info_items:
            amounts = []
            info_fields = resp_info.get("data", {}).get("fields", [])
            for item in info_items:
                d = dict(zip(info_fields, item)) if info_fields else {}
                amt = safe_float(d.get('amount', 0))
                if amt and amt > 0:
                    amounts.append(amt)
            if len(amounts) >= 5:
                mkt_amount_20d_avg = sum(amounts[-20:]) / len(amounts[-20:])

        # 计算涨跌比（用涨停跌停家数简化估算）
        if limit_data:
            limit_up_cnt_est = len([x for x in limit_data if str(x.get('limit', '')).upper() == 'U'])
            limit_down_cnt_est = len([x for x in limit_data if str(x.get('limit', '')).upper() == 'D'])
            # 精确涨跌比需要全市场数据，此处用涨停/跌停比率粗估
            # 更精确：用daily_info的com_count推算
            total_com = 0
            today_amount = 0
            if sh_data:
                sh_dict = dict(zip(resp_sh.get("data", {}).get("fields", []), sh_data[0])) if sh_data else {}
                total_com += safe_int(sh_dict.get('com_count', 0)) or 0
                today_amount += safe_float(sh_dict.get('amount', 0)) or 0
            if sz_data:
                sz_dict = dict(zip(resp_sz.get("data", {}).get("fields", []), sz_data[0])) if sz_data else {}
                total_com += safe_int(sz_dict.get('com_count', 0)) or 0
                today_amount += safe_float(sz_dict.get('amount', 0)) or 0

            # 用涨停跌停数近似涨跌比（早盘跌停极少时 ratio 会虚高）
            # 修复: 跌停=0时, ratio 不应直接触发"牛市"判定
            if limit_up_cnt_est > 0 and limit_down_cnt_est > 0:
                mkt_advance_decline_ratio = min(limit_up_cnt_est / limit_down_cnt_est, 2.5)  # cap 2.5
            elif limit_up_cnt_est > 0:
                mkt_advance_decline_ratio = 1.0  # 跌停=0时中性, 靠amount判定

            # 判定市场状态
            amount_ratio = today_amount / mkt_amount_20d_avg if mkt_amount_20d_avg > 0 else 1.0

            if mkt_advance_decline_ratio > 2.5 and amount_ratio > 1.2:
                market_state = "牛市"
                market_state_multiplier = 1.3
            elif mkt_advance_decline_ratio < 0.8 or amount_ratio < 0.7:
                market_state = "熊市"
                market_state_multiplier = 0.6
            else:
                market_state = "震荡"
                market_state_multiplier = 1.0

            # 限定乘数范围[0.5, 1.5]
            market_state_multiplier = max(0.5, min(1.5, market_state_multiplier))
    except Exception:
        pass

    # ===== 2. 一票否决检查 =====
    # 2.1 市场退潮：炸板率>45% 或 涨停数极少
    if limit_data:
        limit_up_cnt = len([x for x in limit_data if str(x.get('limit', '')).upper() == 'U'])
        limit_z_cnt = len([x for x in limit_data if str(x.get('limit', '')).upper() == 'Z'])  # 炸板
        total_board = limit_up_cnt + limit_z_cnt
        if total_board > 0:
            break_rate = limit_z_cnt / total_board * 100
            if break_rate > 45:
                return 0, f"市场退潮:炸板率{break_rate:.1f}%>45%"
            if limit_up_cnt < 15:
                return 0, f"市场退潮:涨停仅{limit_up_cnt}家"

    # veto6 — emotion circuit breaker
    if limit_data:
        up_cnt = len([x for x in limit_data if str(x.get('limit', '')).upper() == 'U'])
        down_cnt = len([x for x in limit_data if str(x.get('limit', '')).upper() == 'D'])
        z_cnt = len([x for x in limit_data if str(x.get('limit', '')).upper() == 'Z'])
        total_b = up_cnt + z_cnt
        break_rate = z_cnt / total_b * 100 if total_b > 0 else 0

        if break_rate > 40 and down_cnt > 15:
            # 检查是否可进入冰点拐点试探
            max_height = 0
            if step_data:
                max_height = max([safe_int(x.get('nums', 0)) or 0 for x in step_data], default=0)

            if max_height <= 2:
                # 冰点拐点：最高连板降至2板+跌停减少
                return 0, f"情绪熔断冰点:炸板率{break_rate:.1f}%>40%+跌停{down_cnt}家+连板{max_height}板"
            else:
                return 0, f"情绪熔断:炸板率{break_rate:.1f}%>40%+跌停{down_cnt}家"

    # 否决2(主线崩塌)：核心龙头断板 或 所属题材无涨停
    # T+1场景简化：概念涨停数=0视为主线崩塌（=1留给否决5纯跟风）
    # 注意：limit_cpt_list只返回当天前20概念，未收录的概念不能视为"无涨停"
    if concept_names and cpt_data:
        # 只检查在limit_cpt_list中有数据的概念
        tracked_concepts = [n for n in concept_names if _has_concept_data(n)]
        if tracked_concepts:
            max_ul = max([_get_ul_cnt(n) for n in tracked_concepts], default=0)
            if max_ul == 0:
                return 0, "主线崩塌:所属概念无涨停"

    # 2.3 高位杀跌：最高连板高度连续2日下降（需多日数据，T+1简化为高度<2）
    if step_data:
        max_height = max([safe_int(x.get('nums', 0)) or 0 for x in step_data], default=0)
        if max_height < 2 and len(step_data) > 0:
            return 0, f"高位杀跌:最高仅{max_height}板"

    # 否决4 — 个股情绪溃散（诱多A+一字闷杀B）
    # A. 诱多核按钮：
    #   ① 当日最高涨幅曾>3%但收盘跌停
    #   ② 该日换手率 > 近10日均换手率的1.5倍
    #   ③ 近5日内出现≥1次
    # B. 一字闷杀：
    #   ① 开盘即跌停且全天未开板
    #   ② 无需换手率确认
    #   ③ 近5日内出现≥1次
    # 豁免条件（A和B通用）：
    #   ① 曾出现过涨停（收盘涨停或触及涨停）
    #   OR
    #   ② 今日盘中最高价已触及涨停价（正在打反包板）
    try:
        end_dt = datetime.now()
        start_dt = end_dt - timedelta(days=10)
        resp = call_tushare("daily", {
            "ts_code": code,
            "start_date": start_dt.strftime('%Y%m%d'),
            "end_date": end_dt.strftime('%Y%m%d')
        }, "trade_date,close,pre_close,pct_chg,vol,high,low,open")
        daily_items = resp.get("data", {}).get("items", [])
        if daily_items and len(daily_items) >= 2:
            d_fields = resp.get("data", {}).get("fields", [])
            d_dicts = [dict(zip(d_fields, x)) for x in daily_items if d_fields]

            # 计算近10日均换手率（从daily_basic获取）
            try:
                resp_tr = call_tushare("daily_basic", {
                    "ts_code": code,
                    "start_date": start_dt.strftime('%Y%m%d'),
                    "end_date": end_dt.strftime('%Y%m%d')
                }, "trade_date,turnover_rate")
                tr_items = resp_tr.get("data", {}).get("items", [])
                tr_fields = resp_tr.get("data", {}).get("fields", [])
                tr_dicts = [dict(zip(tr_fields, x)) for x in tr_items if tr_fields]
                tr_values = [safe_float(x.get('turnover_rate', 0)) or 0 for x in tr_dicts if safe_float(x.get('turnover_rate')) is not None]
                tr_10d_avg = sum(tr_values[-10:]) / len(tr_values[-10:]) if len(tr_values) >= 10 else (sum(tr_values) / len(tr_values) if tr_values else 0)
            except Exception:
                tr_10d_avg = 0

            # 检查是否有豁免（曾经涨停/盘中触及涨停）
            has_exemption = False
            today_high_touched_zt = False

            for d in d_dicts:
                d_pct = safe_float(d.get('pct_chg', 0)) or 0
                if d_pct >= 9.5:
                    has_exemption = True
                    break
                # 今日盘中最高价是否触及涨停
                d_date = d.get('trade_date', '')
                if d_date == today_str:
                    d_high = safe_float(d.get('high', 0)) or 0
                    d_pre_close = safe_float(d.get('pre_close', 0)) or 0
                    if d_pre_close > 0 and d_high >= d_pre_close * 1.095:
                        today_high_touched_zt = True

            if today_high_touched_zt:
                has_exemption = True

            # 检查近5个交易日的核按钮（不含当日）
            recent_5d = [d for d in d_dicts if d.get('trade_date', '') != today_str][-5:]
            for d in recent_5d:
                d_pct = safe_float(d.get('pct_chg', 0)) or 0
                d_high = safe_float(d.get('high', 0)) or 0
                d_close = safe_float(d.get('close', 0)) or 0
                d_open = safe_float(d.get('open', 0)) or 0
                d_low = safe_float(d.get('low', 0)) or 0
                d_pre_close = safe_float(d.get('pre_close', 0)) or 0
                safe_float(d.get('vol', 0)) or 0

                # A. 诱多核按钮：最高曾>3%但收盘跌停
                is_a = (d_high > d_pre_close * 1.03) and (d_pct <= -9.9)
                # B. 一字闷杀：开盘即跌停且全天未开板
                is_b = (d_open == d_close) and (d_open == d_low) and (d_pct <= -9.9) and (d_high >= d_close * 0.99)

                if is_a or is_b:
                    if has_exemption:
                        continue  # 豁免

                    # A需要换手率确认
                    if is_a and tr_10d_avg > 0:
                        # 获取该日换手率
                        d_date = d.get('trade_date', '')
                        d_tr = 0
                        for tr_d in tr_dicts:
                            if tr_d.get('trade_date', '') == d_date:
                                d_tr = safe_float(tr_d.get('turnover_rate', 0)) or 0
                                break
                        if d_tr <= tr_10d_avg * 1.5:
                            continue  # 换手率不达标，不算核按钮

                    if is_a:
                        return 0, f"核按钮A诱多:{d['trade_date']}最高{d_high:.2f}跌停{d_close:.2f}"
                    elif is_b:
                        return 0, f"核按钮B一字闷杀:{d['trade_date']}一字跌停"
    except Exception:
        pass

    if top_list_data:
        for item in top_list_data[:3]:
            net_amount = safe_float(item.get('net_amount', 0))
            if net_amount and net_amount < -3000:
                return 0, f"游资出逃:净卖{abs(net_amount):.0f}万"

    # 否决5 — 纯跟风弱势
    # 数据源：东方财富个股人气榜（无法获取时，降级为仅检查涨幅）
    # Null处理：跳过人气排名检查
    popularity_rank = _get_popularity_rank(code)  # 真实人气排名

    # 获取个股当日涨幅（优先实时缓存 > 个股实时接口 > Tushare日线 > limit_data）
    stock_pct = 0
    # 盘中优先使用实时数据缓存
    code_short = code.split('.')[0]
    realtime_cache = _batch_fetch_realtime_pct()
    if code_short in realtime_cache:
        stock_pct = realtime_cache[code_short] or 0
        # 异常值防护(东财API偶发错误值如-192%)
        if abs(stock_pct) > 30:
            stock_pct = 0

    # 缓存未命中时，用个股行情接口单独查
    if stock_pct == 0:
        from plays.limit_up.utils import get_stock_pct
        ts_pct = get_stock_pct(code)
        if ts_pct is not None:
            stock_pct = ts_pct

    if stock_pct == 0:
        try:
            resp_stock = call_tushare("daily", {
                "ts_code": code,
                "start_date": today_str,
                "end_date": today_str
            }, "trade_date,pct_chg")
            stock_items = resp_stock.get("data", {}).get("items", [])
            if stock_items:
                stock_pct = safe_float(stock_items[0][1]) or 0
        except Exception:
            pass

    # 从limit_data也尝试找（盘中场景）
    if limit_data:
        for item in limit_data:
            if hasattr(item, 'get') and item.get('ts_code', '') == code:
                stock_pct = safe_float(item.get('pct_chg', 0)) or 0
                break

    # 判断是否主线题材（用概念涨停数代理：≥3只涨停=主线）
    is_main_theme = False
    if concept_names and cpt_data:
        max_ul = max([_get_ul_cnt(n) for n in concept_names], default=0)
        is_main_theme = max_ul >= 3

    if stock_pct < 3:
        return 0, f"纯跟风弱势:涨幅仅{stock_pct:.1f}%<3%"
    elif is_main_theme and popularity_rank is not None and popularity_rank > 200:
        return 0, f"纯跟风弱势:主线题材但人气仅{popularity_rank}名>200"
    elif not is_main_theme and popularity_rank is not None and popularity_rank > 200:
        return 0, f"纯跟风弱势:非主线且人气{popularity_rank}名>200"

    # ===== 3. 五维度评分 =====
    score = 0
    reasons = []

    # --- 3.1 大盘整体情绪维度 (30分) ---
    market_score = 0
    market_reasons = []

    if limit_data:
        limit_up_cnt = len([x for x in limit_data if str(x.get('limit', '')).upper() == 'U'])
        limit_down_cnt = len([x for x in limit_data if str(x.get('limit', '')).upper() == 'D'])
        limit_z_cnt = len([x for x in limit_data if str(x.get('limit', '')).upper() == 'Z'])

        # 赚钱效应：昨日涨停股今日平均溢价（T+1用当日涨停股平均涨幅代理）
        avg_premium = 0
        up_items = [x for x in limit_data if str(x.get('limit', '')).upper() == 'U']
        if up_items:
            premiums = [safe_float(x.get('pct_chg', 0)) for x in up_items]
            avg_premium = sum(premiums) / len(premiums) if premiums else 0

        if avg_premium >= 1.5:
            market_score += 10
            market_reasons.append(f"涨停溢价{avg_premium:.1f}%+10")
        elif avg_premium < 0:
            market_score -= 10
            market_reasons.append(f"涨停溢价{avg_premium:.1f}%-10")

        # 涨跌结构：涨停>35家且跌停<5家，涨跌比>1.5
        if limit_up_cnt >= 35 and limit_down_cnt < 5:
            market_score += 8
            market_reasons.append("结构健康+8")
        elif limit_up_cnt > 0 and limit_down_cnt > 0:
            ratio = limit_up_cnt / max(limit_down_cnt, 1)
            if ratio < 0.8:
                market_score -= 8
                market_reasons.append(f"涨跌比{ratio:.1f}-8")

        # 炸板控制：炸板率<30% +7分；>45%已在否决区处理
        total_board = limit_up_cnt + limit_z_cnt
        if total_board > 0:
            break_rate = limit_z_cnt / total_board * 100
            if break_rate < 30:
                market_score += 7
                market_reasons.append(f"炸板率{break_rate:.1f}%+7")
            elif break_rate > 40:
                market_score -= 7
                market_reasons.append(f"炸板率{break_rate:.1f}%-7")

    # 连板高度 ≥4板+5; <3板且较前两日下降→-5; ELSE:0
    if step_data:
        max_height = max([safe_int(x.get('nums', 0)) or 0 for x in step_data], default=0)
        if max_height >= 4:
            market_score += 5
            market_reasons.append(f"最高{max_height}板+5")
        elif max_height < 3:
            # 检查是否较前两日下降（需要多日step_data）
            market_score -= 5
            market_reasons.append(f"最高仅{max_height}板-5")

    score += max(0, min(30, market_score))
    reasons.extend(market_reasons)

    # --- 3.2 主线题材情绪维度 (30分) 三态+梯度 ---
    theme_score = 0
    theme_reasons = []

    if concept_names and cpt_data:
        max([concept_ul_cnt.get(n, 0) for n in concept_names], default=0)
        # 找当前股票所属的最佳概念
        best_concept = None
        best_ul_cnt = 0
        for name in concept_names:
            cnt = concept_ul_cnt.get(name, 0)
            if cnt > best_ul_cnt:
                best_ul_cnt = cnt
                best_concept = name

        # [题材地位] 梯度加分：1名+10, 2-3名+5, 4-5名+2 
        if cpt_data:
            # 从cpt_data找题材排名
            theme_rank = 99
            for cpt in cpt_data:
                cpt_name = cpt.get('name', '')
                if cpt_name == best_concept:
                    theme_rank = safe_int(cpt.get('rank', 99)) or 99
                    break
            if theme_rank == 1:
                theme_score += 10
                theme_reasons.append("题材第1+10")
            elif theme_rank <= 3:
                theme_score += 5
                theme_reasons.append(f"题材第{theme_rank}+5")
            elif theme_rank <= 5:
                theme_score += 2
                theme_reasons.append(f"题材第{theme_rank}+2")

        # [发酵强度] 增加持平条件（≥前日涨停数）
        # 修复: 移除 prev_ul_cnt = best_ul_cnt 自等恒真 Bug
        if best_ul_cnt >= 3:
            prev_ul_cnt = 0
            try:
                yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y%m%d')
                resp_prev = call_tushare("limit_cpt_list", {"trade_date": yesterday},
                                         "ts_code,name,up_nums")
                prev_items = resp_prev.get("data", {}).get("items", [])
                prev_fields = resp_prev.get("data", {}).get("fields", [])
                if prev_items and prev_fields:
                    for item in prev_items:
                        d = dict(zip(prev_fields, item))
                        if d.get("name") == best_concept:
                            prev_ul_cnt = safe_int(d.get("up_nums", 0)) or 0
                            break
            except Exception:
                pass
            if prev_ul_cnt > 0 and best_ul_cnt > prev_ul_cnt:
                theme_score += 8
                theme_reasons.append(f"{best_concept}涨停{best_ul_cnt}只(↑{best_ul_cnt-prev_ul_cnt})+8")
            elif prev_ul_cnt > 0 and best_ul_cnt == prev_ul_cnt:
                theme_score += 5
                theme_reasons.append(f"{best_concept}涨停{best_ul_cnt}只(持平)+5")
            elif prev_ul_cnt > 0 and best_ul_cnt < prev_ul_cnt:
                theme_score += 3
                theme_reasons.append(f"{best_concept}涨停{best_ul_cnt}只(↓{prev_ul_cnt-best_ul_cnt})+3")
            else:
                theme_score += 6  # 无前日对照, 给基础分
                theme_reasons.append(f"{best_concept}涨停{best_ul_cnt}只+6")

        # [资金共识] 题材主力净流入排名前5 → +7分
        # T+1场景无实时主力净流入数据，用涨停数代理
        if best_ul_cnt >= 5:
            theme_score += 7
            theme_reasons.append("资金共识+7")
        elif best_ul_cnt >= 3:
            theme_score += 3
            theme_reasons.append("资金共识+3")

        # [周期标签] 三态（退潮-10/分歧0/发酵+5）
        # 退潮：龙头断板跌停 AND 板块涨停<3 AND 资金流出
        # 分歧：炸板率>40% OR 涨停数未递增
        # 发酵/高潮：其他
        is_retreat = False
        is_divergence = False
        # 退潮判断：涨停数<3且无龙头
        if best_ul_cnt < 3:
            # 检查是否有龙头（最高连板>=3）
            max_height = max([safe_int(x.get('nums', 0)) or 0 for x in step_data], default=0) if step_data else 0
            if max_height < 3:
                is_retreat = True
        # 分歧判断：炸板率>40%
        if limit_data:
            up_cnt_d2 = len([x for x in limit_data if str(x.get('limit', '')).upper() == 'U'])
            z_cnt_d2 = len([x for x in limit_data if str(x.get('limit', '')).upper() == 'Z'])
            total_board_d2 = up_cnt_d2 + z_cnt_d2
            break_rate_d2 = z_cnt_d2 / total_board_d2 * 100 if total_board_d2 > 0 else 0
            if break_rate_d2 > 40:
                is_divergence = True

        if is_retreat:
            theme_score -= 10
            theme_reasons.append("退潮-10")
        elif is_divergence:
            pass  # 分歧0分
        else:
            theme_score += 5
            theme_reasons.append("发酵+5")

    score += max(0, min(30, theme_score))
    reasons.extend(theme_reasons)

    # --- 3.3 板块梯队情绪维度 (20分) ---
    sector_score = 0
    sector_reasons = []

    # 检查同板块涨停数（使用limit_cpt_list统计数据）
    if concept_names and concept_ul_cnt:
        max_sector_ul = max([concept_ul_cnt.get(n, 0) for n in concept_names], default=0)
        if max_sector_ul >= 5:
            sector_score += 10
            sector_reasons.append(f"板块涨停{max_sector_ul}只+10")
        elif max_sector_ul >= 3:
            sector_score += 6
            sector_reasons.append(f"板块涨停{max_sector_ul}只+6")

    # 连板梯队
    if step_data:
        high_boards = [x for x in step_data if safe_int(x.get('nums', 0)) >= 3]
        if len(high_boards) >= 2:
            sector_score += 4
            sector_reasons.append("梯队完整+4")

    score += max(0, min(20, sector_score))
    reasons.extend(sector_reasons)

    # --- 3.4 个股人气资金情绪维度 (10分) 修订 ---
    popular_score = 0
    popular_reasons = []

    # [换手活跃度 梯度计分]
    try:
        resp = call_tushare("daily_basic", {"ts_code": code}, "turnover_rate,volume_ratio")
        daily_basic = resp.get("data", {}).get("items", [])
        if daily_basic:
            turnover = safe_float(daily_basic[0][0]) if daily_basic[0] else None
            if turnover:
                if 5 <= turnover < 15:
                    popular_score += 1
                    popular_reasons.append(f"换手{turnover:.1f}%+1")
                elif 15 <= turnover < 25:
                    popular_score += 2
                    popular_reasons.append(f"换手{turnover:.1f}%活跃+2")
                elif 25 <= turnover < 30:
                    popular_score += 1
                    popular_reasons.append(f"换手{turnover:.1f}%高+1")
                elif turnover >= 30:
                    popular_score -= 2
                    popular_reasons.append(f"换手{turnover:.1f}%过热-2")
    except Exception:
        pass

    # [资金记忆 溢价校验：近20日涨停+次日溢价]
    try:
        end_dt = datetime.now()
        start_dt = end_dt - timedelta(days=25)
        resp = call_tushare("limit_list_d", {
            "ts_code": code,
            "start_date": start_dt.strftime('%Y%m%d'),
            "end_date": end_dt.strftime('%Y%m%d')
        }, "trade_date,ts_code,limit")
        hist_ul = resp.get("data", {}).get("items", [])
        ul_cnt_20d = len([x for x in hist_ul if str(x[2]).upper() == 'U']) if hist_ul else 0
        if ul_cnt_20d >= 2:
            # 检查最近一次涨停次日的最高溢价
            popular_score += 3
            popular_reasons.append(f"20日涨停{ul_cnt_20d}次+3")
        elif ul_cnt_20d >= 1:
            popular_score += 1
            popular_reasons.append("20日涨停+1")
    except Exception:
        pass

    # [龙虎榜偏好] 游资净买入>0
    if top_list_data:
        for item in top_list_data[:3]:
            net_rate = safe_float(item.get('net_rate'))
            if net_rate and net_rate > 0:
                popular_score += 2
                popular_reasons.append(f"龙虎榜净买{net_rate:.1f}%+2")
                break

    # [连板基因 新增] 近60日曾≥2连板
    try:
        end_dt = datetime.now()
        start_dt = end_dt - timedelta(days=65)
        resp = call_tushare("limit_step", {
            "ts_code": code,
            "start_date": start_dt.strftime('%Y%m%d'),
            "end_date": end_dt.strftime('%Y%m%d')
        }, "trade_date,ts_code,nums")
        step_hist = resp.get("data", {}).get("items", [])
        if step_hist:
            max_nums_60d = max([safe_int(x[2]) or 0 for x in step_hist], default=0)
            if max_nums_60d >= 2:
                popular_score += 3
                popular_reasons.append(f"连板基因{max_nums_60d}连板+3")
    except Exception:
        pass

    score += max(0, min(10, popular_score))
    reasons.extend(popular_reasons)

    # --- 3.5 集合竞价情绪动能维度 (15分) ---
    # 基于 stk_auction 接口的开盘竞价数据，衡量开盘前情绪强度
    auction_score = 0
    auction_reasons = []

    try:
        resp = call_tushare("stk_auction", {"trade_date": today_str, "ts_code": code}, "ts_code,trade_date,vol,price,amount,pre_close,turnover_rate,volume_ratio,float_share")
        auction_data = resp.get("data", {})
        auction_items = auction_data.get("items", [])
        if auction_items and len(auction_items) > 0:
            item = auction_items[0]
            a_fields = auction_data.get("fields", [])
            a_dict = dict(zip(a_fields, item)) if a_fields else {}

            pre_close = safe_float(a_dict.get('pre_close')) or 0
            price = safe_float(a_dict.get('price')) or 0
            vol = safe_float(a_dict.get('vol')) or 0
            amount = safe_float(a_dict.get('amount')) or 0
            safe_float(a_dict.get('float_share')) or 0
            volume_ratio = safe_float(a_dict.get('volume_ratio')) or 0

            open_gap = (price - pre_close) / pre_close * 100 if pre_close > 0 else 0
            # CallVolRatio量纲修正：竞价量/昨日成交量（旧版vol/float_share*100已废弃）
            call_vol_ratio = vol / yesterday_vol if yesterday_vol > 0 else 0

            # 1. OpenGap 评分 (5分) × 市场状态乘数
            # 基础跳空评分（震荡态参考值）
            open_gap_base = 0
            if 5 <= open_gap < 8:
                open_gap_base = 5
            elif 3 <= open_gap < 5:
                open_gap_base = 3
            elif 1 <= open_gap < 3:
                open_gap_base = 1
            elif -1 <= open_gap < 1:
                open_gap_base = 0  # 平开
            elif -3 <= open_gap < -1:
                open_gap_base = -2
            elif open_gap < -3:
                open_gap_base = -4
            elif open_gap >= 8:
                open_gap_base = 2  # 大幅高开有冲高回落风险
                # 秒板修正：IF 开盘后5分钟内封涨停 → 改为+5分（直接给满，不乘乘数）
                # 在stk_auction数据中无法精确获取5分钟内是否封板，用开盘价接近涨停+竞价量比>5代理
                if volume_ratio > 5 and call_vol_ratio > 2.0:
                    open_gap_base = 5  # 秒板信号：竞价量比高+高开=大概率秒板

            # 牛市态：加分放大，扣分不变；熊市态：加分缩小，扣分放大×1.5
            if open_gap_base > 0:
                open_gap_score = round(open_gap_base * market_state_multiplier)
            elif open_gap_base < 0:
                # 扣分：牛市不变(×1.0)，熊市放大(×1/multiplier，即÷0.6≈×1.67)
                bear_penalty_factor = 1.0 / market_state_multiplier if market_state_multiplier < 1.0 else 1.0
                bear_penalty_factor = min(bear_penalty_factor, 1.5)  # 最大放大1.5倍
                open_gap_score = round(open_gap_base * bear_penalty_factor)
            else:
                open_gap_score = 0

            # 截断规则
            open_gap_score = max(0, min(5, open_gap_score))
            if open_gap_base != 0:
                gap_state_tag = f"[{market_state}态×{market_state_multiplier}]"
                auction_reasons.append(f"竞价跳空{open_gap:.1f}%{gap_state_tag}→{open_gap_score}分")
            auction_score += open_gap_score

            # 2. CallVolRatio 评分 (5分) - 竞价关注度(量纲修正)
            # 新量纲：竞价量/昨日成交量，实际值范围0.3~5.0
            if call_vol_ratio >= 3.0:  # 竞价量达昨日全天3倍以上
                auction_score += 5
                auction_reasons.append(f"竞价关注度极高(量比{call_vol_ratio:.1f})+5")
            elif call_vol_ratio >= 1.5:  # 竞价量达昨日全天1.5倍
                auction_score += 3
                auction_reasons.append(f"竞价关注度高(量比{call_vol_ratio:.1f})+3")
            elif call_vol_ratio >= 0.5:  # 竞价量达昨日全天半量
                auction_score += 1
                auction_reasons.append(f"竞价关注度较高(量比{call_vol_ratio:.1f})+1")

            # 3. 量比验证 (3分)
            if volume_ratio > 5:
                auction_score += 3
                auction_reasons.append(f"竞价量比{volume_ratio:.1f}+3")
            elif volume_ratio > 3:
                auction_score += 1
                auction_reasons.append(f"竞价量比{volume_ratio:.1f}+1")

            # 4. 竞价成交额 (2分)
            if amount >= 5000000:  # 500万
                auction_score += 2
                auction_reasons.append(f"竞价成交{amount/10000:.0f}万+2")
            elif amount >= 1000000:  # 100万
                auction_score += 1
                auction_reasons.append(f"竞价成交{amount/10000:.0f}万+1")
    except Exception:
        pass

    score += max(0, min(15, auction_score))
    reasons.extend(auction_reasons[:3])

    # 高位情绪折扣 — 个股连板高度≥5板时对总分施加折扣系数
    stock_continuity = 0
    try:
        # 复用已获取的 step_data(line 67), 避免重复调 limit_step
        for item in step_data:
            if item.get("ts_code") == code:
                stock_continuity = safe_int(item.get("nums", 0)) or 0
                break
    except Exception:
        pass

    if stock_continuity >= 5:
        discount = 0.85 if stock_continuity <= 6 else 0.7
        score_before = score
        score = round(score * discount)
        reasons.append(f"[折扣]连板{stock_continuity}板×{discount} {score_before}→{score}")

    # ===== 4. 综合评定 =====
    final_score = max(0, min(100, score))

    if final_score >= 75:
        level = "高"
    elif final_score >= 55:
        level = "中"
    elif final_score >= 35:
        level = "低"
    else:
        level = "无"

    # 截断从[:5]改为[:8]，确保5个维度都能展示关键reason
    # 市场状态标签（牛市态/熊市态）在竞价维度reason中，[:5]截断会隐藏竞价维度
    reason_str = f"[{level}] " + "; ".join(reasons[:8]) if reasons else f"[{level}] 无明显信号"

    return final_score, reason_str
