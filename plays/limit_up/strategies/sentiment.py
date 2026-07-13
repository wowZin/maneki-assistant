#!/usr/bin/env python3
"""
情绪面涨停潜力预判  — 精简否决 + 昨日涨停溢价 + 人气简化 + 动量信号

 相比 v1 的变更:
  - 否决规则从 6+1 精简到 3 个（市场退潮 / 主线崩塌 / 情绪熔断）
  - 新增 was_limit_yesterday +5 溢价奖励
  - 个股人气维度简化：移除龙虎榜分析，换手分档升级
  - 熊市态集合竞价乘数保底 0.7x（原 0.5x）
  - 纯跟风弱势从一票否决降级为评分扣分 + 优雅降级
  - 新增次日动量信号：昨日涨>5% + 概念升温 → +3
  - 保留: 市场状态感知竞价评分 / 概念共振逻辑 / 五维度框架

签名: score_sentiment(code: str) -> tuple[int | float, str]
"""

import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_DIR))
sys.path.insert(0, str(PROJECT_DIR / "scripts"))

# 共享基础设施 + 玩法工具
from scripts.tu_share import call_tushare  # noqa: E402
from plays.limit_up.utils import safe_float_none, safe_int_none, list_to_dict  # noqa: E402

# 实时概念热点缓存（替代 THS 热门榜）
from plays.limit_up.strategies.concept_cache import (  # noqa: E402
    get_concept_limit_ups,
    get_best_concept,
    get_total_limit_up_count,
)

# 移除 _get_popularity_rank 依赖（热榜仅 100 只，无意义）


def score_sentiment(code: str, trade_date: str | None = None) -> tuple[int | float, str]:
    """
    情绪面  评分（0-100）

    五维度框架不变:
      大盘情绪 30 + 主线题材 30 + 板块梯队 20 + 个股人气 10 + 集合竞价 15
    精简否决: 仅保留市场退潮 / 主线崩塌 / 情绪熔断 三项
    """
    from datetime import datetime, timedelta

    # ─── 类型转换快捷方式 ───
    safe_float = safe_float_none
    safe_int = safe_int_none

    # ═══════════════════════════════════════════════════════════
    # 0. 基础参数
    # ═══════════════════════════════════════════════════════════
    today_str = trade_date or datetime.now().strftime('%Y%m%d')
    yesterday_str = (datetime.strptime(today_str, '%Y%m%d') - timedelta(days=1)).strftime('%Y%m%d') if trade_date else (datetime.now() - timedelta(days=1)).strftime('%Y%m%d')
    code_short = code.split('.')[0]

    # ═══════════════════════════════════════════════════════════
    # 1. 数据获取
    # ═══════════════════════════════════════════════════════════

    # 1.1 概念热点（基于实时涨停 + 全量概念映射）
    clu = get_concept_limit_ups(code)
    concept_names = [k for k in clu if not k.startswith("_")]
    concept_ul_cnt = {k: v for k, v in clu.items() if not k.startswith("_")}
    total_limit_ups = clu.get("_total_limit_ups", 0)
    total_limit_concepts = clu.get("_total_concepts", 0)

    # 1.2 全市场涨跌停数据
    limit_fields = ["trade_date", "ts_code", "name", "close", "pct_chg", "limit",
                    "limit_times", "up_stat"]
    limit_data = []
    try:
        resp = call_tushare("limit_list_d",
                            {"trade_date": today_str}, ",".join(limit_fields))
        data = resp.get("data", {})
        limit_data = list_to_dict(data.get("items", []), limit_fields)
    except Exception:
        pass

    # 1.3 连板天梯
    step_fields = ["trade_date", "ts_code", "name", "nums"]
    step_data = []
    try:
        resp = call_tushare("limit_step",
                            {"trade_date": today_str}, ",".join(step_fields))
        data = resp.get("data", {})
        step_data = list_to_dict(data.get("items", []), step_fields)
    except Exception:
        pass

    # 1.4 概念热点（基于实时涨停 + 全量概念映射）
    clu = get_concept_limit_ups(code)
    concept_names = [k for k in clu if not k.startswith("_")]
    concept_ul_cnt = {k: v for k, v in clu.items() if not k.startswith("_")}
    total_limit_ups = clu.get("_total_limit_ups", 0)
    total_limit_concepts = clu.get("_total_concepts", 0)

    def _get_ul_cnt(concept_name):
        return concept_ul_cnt.get(concept_name, 0)

    def _has_concept_data(concept_name):
        return concept_name in concept_ul_cnt

    # 1.5 个股昨日涨幅（用于 was_limit_yesterday + 动量信号）
    yesterday_pct = None
    yesterday_was_limit = False
    try:
        resp = call_tushare("daily", {
            "ts_code": code,
            "start_date": (datetime.now() - timedelta(days=5)).strftime('%Y%m%d'),
            "end_date": today_str,
        }, "trade_date,pct_chg,vol")
        daily_items = resp.get("data", {}).get("items", [])
        d_fields = resp.get("data", {}).get("fields", [])
        if daily_items and d_fields:
            d_dicts = [dict(zip(d_fields, x)) for x in daily_items]
            # 找最近一个非今日交易日的日线（作为 yesterday 数据）
            for d in d_dicts:
                dt = d.get("trade_date", "")
                if dt and dt != today_str:
                    pct = safe_float(d.get("pct_chg", 0))
                    if pct is not None and yesterday_pct is None:
                        yesterday_pct = pct
                        # 判断涨停：涨幅 >= 9.5% 视为涨停
                        yesterday_was_limit = (pct >= 9.5)
                        break
    except Exception:
        pass

    # 1.6 个股昨日成交量（集合竞价的 CallVolRatio 量纲分母）
    yesterday_vol = 0
    try:
        end_dt = datetime.now() - timedelta(days=5)
        resp = call_tushare("daily", {
            "ts_code": code,
            "start_date": end_dt.strftime('%Y%m%d'),
            "end_date": today_str,
        }, "trade_date,vol")
        vol_items = resp.get("data", {}).get("items", [])
        vol_fields = resp.get("data", {}).get("fields", [])
        if vol_items and vol_fields:
            vol_dicts = [dict(zip(vol_fields, x)) for x in vol_items]
            for d in vol_dicts:
                if d.get("trade_date", "") != today_str:
                    yesterday_vol = safe_float(d.get("vol", 0)) or 0
                    break
            if yesterday_vol == 0 and len(vol_items) >= 2:
                yesterday_vol = safe_float(vol_items[1][1]) if len(vol_items[1]) > 1 else 0
    except Exception:
        pass

    # 1.7 市场状态判定（牛/熊/震荡 + 乘数）
    market_state = "震荡"
    market_state_multiplier = 1.0
    mkt_amount_20d_avg = 0
    try:
        resp_sh = call_tushare("daily_info",
                               {"trade_date": today_str, "ts_code": "SSE"},
                               "trade_date,ts_code,com_count,amount")
        sh_data = resp_sh.get("data", {}).get("items", [])
        resp_sz = call_tushare("daily_info",
                               {"trade_date": today_str, "ts_code": "SZSE"},
                               "trade_date,ts_code,com_count,amount")
        sz_data = resp_sz.get("data", {}).get("items", [])

        # 20日均成交额
        resp_info = call_tushare("daily_info", {
            "start_date": (datetime.now() - timedelta(days=30)).strftime('%Y%m%d'),
            "end_date": today_str,
        }, "trade_date,amount")
        info_items = resp_info.get("data", {}).get("items", [])
        if info_items:
            amounts = []
            info_fields = resp_info.get("data", {}).get("fields", [])
            for item in info_items:
                d = dict(zip(info_fields, item)) if info_fields else {}
                amt = safe_float(d.get("amount", 0))
                if amt and amt > 0:
                    amounts.append(amt)
            if len(amounts) >= 5:
                mkt_amount_20d_avg = sum(amounts[-20:]) / len(amounts[-20:])

        # 涨跌比估算
        mkt_advance_decline_ratio = 1.0
        if limit_data:
            limit_up_cnt_est = len(
                [x for x in limit_data if str(x.get("limit", "")).upper() == "U"])
            limit_down_cnt_est = len(
                [x for x in limit_data if str(x.get("limit", "")).upper() == "D"])

            if limit_up_cnt_est > 0 and limit_down_cnt_est > 0:
                mkt_advance_decline_ratio = min(
                    limit_up_cnt_est / limit_down_cnt_est, 2.5)

            # 成交额比
            today_amount = 0
            if sh_data:
                sh_f = resp_sh.get("data", {}).get("fields", [])
                sh_d = dict(zip(sh_f, sh_data[0])) if sh_data and sh_f else {}
                today_amount += safe_float(sh_d.get("amount", 0)) or 0
            if sz_data:
                sz_f = resp_sz.get("data", {}).get("fields", [])
                sz_d = dict(zip(sz_f, sz_data[0])) if sz_data and sz_f else {}
                today_amount += safe_float(sz_d.get("amount", 0)) or 0

            amount_ratio = (today_amount / mkt_amount_20d_avg
                            if mkt_amount_20d_avg > 0 else 1.0)

            if mkt_advance_decline_ratio > 2.5 and amount_ratio > 1.2:
                market_state = "牛市"
                market_state_multiplier = 1.3
            elif mkt_advance_decline_ratio < 0.8 or amount_ratio < 0.7:
                market_state = "熊市"
                market_state_multiplier = 0.9  # 熊市保底 0.9x（原 0.7，放宽）
            else:
                market_state = "震荡"
                market_state_multiplier = 1.0

            # : 乘数范围 [0.7, 1.5]（保底 0.7，原 0.5）
            market_state_multiplier = max(0.7, min(1.5, market_state_multiplier))
    except Exception:
        pass

    # ═══════════════════════════════════════════════════════════
    # 2. 一票否决检查（ 精简为 3 项）
    # ═══════════════════════════════════════════════════════════

    # --- 否决 1: 市场退潮 —— 炸板率 > 45% ---
    if limit_data:
        up_cnt = len([x for x in limit_data if str(x.get("limit", "")).upper() == "U"])
        z_cnt = len([x for x in limit_data if str(x.get("limit", "")).upper() == "Z"])
        total_board = up_cnt + z_cnt
        if total_board > 0:
            break_rate_v = z_cnt / total_board * 100
            if break_rate_v > 45:
                return 0, f"市场退潮:炸板率{break_rate_v:.1f}%>45%"

    # --- 否决 2: 主线崩塌 —— 所属概念无涨停 ---
    if concept_names and concept_ul_cnt:
        tracked_concepts = [n for n in concept_names if _has_concept_data(n)]
        if tracked_concepts:
            max_ul = max([_get_ul_cnt(n) for n in tracked_concepts], default=0)
            if max_ul == 0:
                return 0, "主线崩塌:所属概念无涨停"

    # --- 否决 3: 情绪熔断 —— 炸板率 > 40% + 跌停 > 15 ---
    if limit_data:
        down_cnt = len([x for x in limit_data if str(x.get("limit", "")).upper() == "D"])
        if total_board > 0 and break_rate_v > 40 and down_cnt > 15:
            # 冰点试探：最高连板 <= 2 时标记
            max_height_ice = 0
            if step_data:
                max_height_ice = max(
                    [safe_int(x.get("nums", 0)) or 0 for x in step_data], default=0)
            if max_height_ice <= 2:
                return 0, (f"情绪熔断冰点:炸板率{break_rate_v:.1f}%>40%"
                           f"+跌停{down_cnt}家+连板{max_height_ice}板")
            else:
                return 0, (f"情绪熔断:炸板率{break_rate_v:.1f}%>40%"
                           f"+跌停{down_cnt}家")

    # ═══════════════════════════════════════════════════════════
    # 3. 五维度评分
    # ═══════════════════════════════════════════════════════════
    score = 0
    reasons = []

    # ─── 3.1 大盘整体情绪 (30分) ───
    market_score = 0
    market_reasons = []

    if limit_data:
        up_cnt_31 = len(
            [x for x in limit_data if str(x.get("limit", "")).upper() == "U"])
        down_cnt_31 = len(
            [x for x in limit_data if str(x.get("limit", "")).upper() == "D"])
        z_cnt_31 = len(
            [x for x in limit_data if str(x.get("limit", "")).upper() == "Z"])

        # 涨停溢价：涨停股平均涨幅
        up_items = [x for x in limit_data
                    if str(x.get("limit", "")).upper() == "U"]
        avg_premium = 0
        if up_items:
            premiums = [safe_float(x.get("pct_chg", 0)) or 0 for x in up_items]
            avg_premium = sum(premiums) / len(premiums)

        if avg_premium >= 1.5:
            market_score += 10
            market_reasons.append(f"涨停溢价{avg_premium:.1f}%+10")
        elif avg_premium < 0:
            market_score -= 10
            market_reasons.append(f"涨停溢价{avg_premium:.1f}%-10")

        # 涨跌结构
        if up_cnt_31 >= 35 and down_cnt_31 < 5:
            market_score += 8
            market_reasons.append("结构健康+8")
        elif up_cnt_31 > 0 and down_cnt_31 > 0:
            ratio = up_cnt_31 / max(down_cnt_31, 1)
            if ratio < 0.8:
                market_score -= 8
                market_reasons.append(f"涨跌比{ratio:.1f}-8")

        # 炸板控制
        total_b = up_cnt_31 + z_cnt_31
        if total_b > 0:
            br = z_cnt_31 / total_b * 100
            if br < 30:
                market_score += 7
                market_reasons.append(f"炸板率{br:.1f}%+7")
            elif br > 40:
                market_score -= 7
                market_reasons.append(f"炸板率{br:.1f}%-7")

    # 连板高度
    if step_data:
        max_height = max(
            [safe_int(x.get("nums", 0)) or 0 for x in step_data], default=0)
        if max_height >= 4:
            market_score += 5
            market_reasons.append(f"最高{max_height}板+5")
        elif max_height < 3:
            market_score -= 5
            market_reasons.append(f"最高仅{max_height}板-5")

    score += max(0, min(30, market_score))
    reasons.extend(market_reasons)

    # ─── 3.2 主线题材情绪 (30分) ───
    theme_score = 0
    theme_reasons = []

    # 找最佳概念
    best_concept = None
    best_ul_cnt = 0
    if concept_names and concept_ul_cnt:
        for name in concept_names:
            cnt = concept_ul_cnt.get(name, 0)
            if cnt > best_ul_cnt:
                best_ul_cnt = cnt
                best_concept = name

    if concept_names and concept_ul_cnt and best_concept:
        # [题材地位] 排名加分
        ranked = sorted(concept_ul_cnt.items(), key=lambda x: x[1], reverse=True)
        theme_rank = 99
        for ri, (cn, _) in enumerate(ranked, 1):
            if cn == best_concept:
                theme_rank = ri
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

        # [发酵强度] 相比前日涨停数
        prev_ul_cnt = 0  # 初始化为0, 防止 UnboundLocalError
        if best_ul_cnt >= 3:
            try:
                resp_prev = call_tushare("limit_cpt_list",
                                         {"trade_date": yesterday_str},
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
                theme_reasons.append(
                    f"{best_concept}涨停{best_ul_cnt}只"
                    f"(↑{best_ul_cnt - prev_ul_cnt})+8")
            elif prev_ul_cnt > 0 and best_ul_cnt == prev_ul_cnt:
                theme_score += 5
                theme_reasons.append(f"{best_concept}涨停{best_ul_cnt}只(持平)+5")
            elif prev_ul_cnt > 0 and best_ul_cnt < prev_ul_cnt:
                theme_score += 3
                theme_reasons.append(
                    f"{best_concept}涨停{best_ul_cnt}只"
                    f"(↓{prev_ul_cnt - best_ul_cnt})+3")
            else:
                theme_score += 6
                theme_reasons.append(f"{best_concept}涨停{best_ul_cnt}只+6")

        # [资金共识] 涨停数代理
        if best_ul_cnt >= 5:
            theme_score += 7
            theme_reasons.append("资金共识+7")
        elif best_ul_cnt >= 3:
            theme_score += 3
            theme_reasons.append("资金共识+3")

        # [周期标签] 三态
        is_retreat = False
        is_divergence = False
        if best_ul_cnt < 3:
            max_h = (max([safe_int(x.get("nums", 0)) or 0
                          for x in step_data], default=0)
                     if step_data else 0)
            if max_h < 3:
                is_retreat = True
        if limit_data:
            up_d2 = len([x for x in limit_data
                         if str(x.get("limit", "")).upper() == "U"])
            z_d2 = len([x for x in limit_data
                        if str(x.get("limit", "")).upper() == "Z"])
            tb = up_d2 + z_d2
            if tb > 0 and (z_d2 / tb * 100) > 40:
                is_divergence = True

        if is_retreat:
            theme_score -= 10
            theme_reasons.append("退潮-10")
        elif is_divergence:
            pass  # 分歧 0 分
        else:
            theme_score += 5
            theme_reasons.append("发酵+5")

        # ──  新增: 次日动量信号 ──
        # 条件: 昨日涨 > 5% 且 概念在升温（涨停数递增 或 >= 3 只）
        if yesterday_pct is not None and yesterday_pct > 5:
            concept_heating = (
                (prev_ul_cnt > 0 and best_ul_cnt > prev_ul_cnt)
                or (best_ul_cnt >= 3)
            )
            if concept_heating:
                theme_score += 3
                theme_reasons.append(
                    f"动量接力:昨+{yesterday_pct:.1f}%+{best_concept}升温+3"
                )

    
    # THS 精准概念共振：使用 factor_ctx 真实概念动量（替代已损坏的 ths_concept_map.json）
    ths_niche_bonus = 0
    try:
        from plays.limit_up.strategies import factor_ctx
        cm = factor_ctx.get_concept_momentum(code.split('.')[0])
        if cm.get("ret3_max", 0) > 5:
            ths_niche_bonus = 15
        elif cm.get("ret3_max", 0) > 3:
            ths_niche_bonus = 10
        elif cm.get("ret3_max", 0) > 1.5 and cm.get("up_ratio", 0) > 0.5:
            ths_niche_bonus = 5
    except Exception:
        pass
    if ths_niche_bonus > 0:
        theme_score += ths_niche_bonus
        theme_reasons.append(f"概念共振(+{ths_niche_bonus})")

    score += max(0, min(30, theme_score))
    reasons.extend(theme_reasons)

    # ─── 3.3 板块梯队情绪 (20分) ───
    sector_score = 0
    sector_reasons = []

    if concept_names and concept_ul_cnt:
        max_sector_ul = max([concept_ul_cnt.get(n, 0) for n in concept_names],
                            default=0)
        if max_sector_ul >= 5:
            sector_score += 10
            sector_reasons.append(f"板块涨停{max_sector_ul}只+10")
        elif max_sector_ul >= 3:
            sector_score += 6
            sector_reasons.append(f"板块涨停{max_sector_ul}只+6")

    # 连板梯队完整度
    if step_data:
        high_boards = [x for x in step_data if safe_int(x.get("nums", 0)) >= 3]
        if len(high_boards) >= 2:
            sector_score += 4
            sector_reasons.append("梯队完整+4")

    score += max(0, min(20, sector_score))
    reasons.extend(sector_reasons)

    # ─── 3.4 个股人气情绪 (10分)  简化 ───
    # 移除龙虎榜游资分析，换手分档升级，保留涨停记忆 + 连板基因
    popular_score = 0
    popular_reasons = []

    # A. 换手活跃度（分档升级，占 0-5 分）
    turnover = None
    try:
        rt_fund = _get_realtime_fund_cache()
        rt_turnover = rt_fund.get(code_short, {}).get("turnover", 0)
        if rt_turnover and rt_turnover > 0:
            turnover = rt_turnover
        else:
            # 优先从 pipeline 预取缓存读取，避免重复调 daily_basic
            from plays.limit_up.strategies import factor_ctx
            basic = factor_ctx.get_daily_basic(code)
            if basic:
                turnover = safe_float(basic.get("turnover_rate", 0))
            else:
                resp = call_tushare("daily_basic", {"ts_code": code},
                                    "turnover_rate,volume_ratio")
                daily_basic = resp.get("data", {}).get("items", [])
                if daily_basic and daily_basic[0]:
                    turnover = safe_float(daily_basic[0][0])
    except Exception:
        pass

    if turnover is not None:
        if turnover >= 30:
            popular_score += 1
            popular_reasons.append(f"换手{turnover:.1f}%过热+1")
        elif turnover >= 20:
            popular_score += 3
            popular_reasons.append(f"换手{turnover:.1f}%高活+3")
        elif turnover >= 10:
            popular_score += 5
            popular_reasons.append(f"换手{turnover:.1f}%活跃+5")
        elif turnover >= 3:
            popular_score += 3
            popular_reasons.append(f"换手{turnover:.1f}%温和+3")
        else:
            popular_reasons.append(f"换手{turnover:.1f}%清淡")

    # B. 涨停记忆：近 20 日涨停次数（0-3 分）
    try:
        end_dt = datetime.now()
        start_dt = end_dt - timedelta(days=25)
        resp = call_tushare("limit_list_d", {
            "ts_code": code,
            "start_date": start_dt.strftime('%Y%m%d'),
            "end_date": end_dt.strftime('%Y%m%d'),
        }, "trade_date,ts_code,limit")
        hist_ul = resp.get("data", {}).get("items", [])
        ul_cnt_20d = len(
            [x for x in hist_ul if str(x[2]).upper() == "U"]) if hist_ul else 0
        if ul_cnt_20d >= 2:
            popular_score += 3
            popular_reasons.append(f"20日涨停{ul_cnt_20d}次+3")
        elif ul_cnt_20d >= 1:
            popular_score += 1
            popular_reasons.append("20日涨停+1")
    except Exception:
        pass

    # C. 连板基因：近 60 日最高连板 >= 2（0-2 分）
    try:
        end_dt = datetime.now()
        start_dt = end_dt - timedelta(days=65)
        resp = call_tushare("limit_step", {
            "ts_code": code,
            "start_date": start_dt.strftime('%Y%m%d'),
            "end_date": end_dt.strftime('%Y%m%d'),
        }, "trade_date,ts_code,nums")
        step_hist = resp.get("data", {}).get("items", [])
        if step_hist:
            max_nums_60d = max(
                [safe_int(x[2]) or 0 for x in step_hist], default=0)
            if max_nums_60d >= 2:
                popular_score += 2
                popular_reasons.append(f"连板基因{max_nums_60d}连板+2")
    except Exception:
        pass

    score += max(0, min(10, popular_score))
    reasons.extend(popular_reasons)

    # ─── 3.5 集合竞价情绪动能 (15分) 含市场状态乘数 ───
    auction_score = 0
    auction_reasons = []

    try:
        resp = call_tushare("stk_auction", {
            "trade_date": today_str,
            "ts_code": code,
        }, (
            "ts_code,trade_date,vol,price,amount,pre_close,"
            "turnover_rate,volume_ratio,float_share"
        ))
        auction_data = resp.get("data", {})
        auction_items = auction_data.get("items", [])
        if auction_items and len(auction_items) > 0:
            item = auction_items[0]
            a_fields = auction_data.get("fields", [])
            a_dict = dict(zip(a_fields, item)) if a_fields else {}

            pre_close = safe_float(a_dict.get("pre_close")) or 0
            price = safe_float(a_dict.get("price")) or 0
            vol = safe_float(a_dict.get("vol")) or 0
            amount = safe_float(a_dict.get("amount")) or 0
            volume_ratio = safe_float(a_dict.get("volume_ratio")) or 0

            open_gap = (price - pre_close) / pre_close * 100 if pre_close > 0 else 0
            call_vol_ratio = vol / yesterday_vol if yesterday_vol > 0 else 0

            # 1. OpenGap 评分 (5分) × 市场状态乘数
            open_gap_base = 0
            if 5 <= open_gap < 8:
                open_gap_base = 5
            elif 3 <= open_gap < 5:
                open_gap_base = 3
            elif 1 <= open_gap < 3:
                open_gap_base = 1
            elif -1 <= open_gap < 1:
                open_gap_base = 0
            elif -3 <= open_gap < -1:
                open_gap_base = -2
            elif open_gap < -3:
                open_gap_base = -4
            elif open_gap >= 8:
                open_gap_base = 2
                # 秒板修正
                if volume_ratio > 5 and call_vol_ratio > 2.0:
                    open_gap_base = 5

            # 市场状态敏感：牛市放大加分，熊市保底（ floor = 0.7）
            if open_gap_base > 0:
                open_gap_score = round(open_gap_base * market_state_multiplier)
            elif open_gap_base < 0:
                # : bear_penalty = 1/multiplier, cap 1.5; 熊市 floor 0.7
                # → max penalty = 1/0.7 ≈ 1.43 < 1.5
                bear_penalty = (1.0 / market_state_multiplier
                                if market_state_multiplier < 1.0 else 1.0)
                bear_penalty = min(bear_penalty, 1.5)
                open_gap_score = round(open_gap_base * bear_penalty)
            else:
                open_gap_score = 0

            open_gap_score = max(0, min(5, open_gap_score))
            if open_gap_base != 0:
                gap_tag = f"[{market_state}态×{market_state_multiplier}]"
                auction_reasons.append(
                    f"竞价跳空{open_gap:.1f}%{gap_tag}→{open_gap_score}分")
            auction_score += open_gap_score

            # 2. CallVolRatio (5分) — 竞价关注度量纲修正
            if call_vol_ratio >= 3.0:
                auction_score += 5
                auction_reasons.append(f"竞价关注度极高(量比{call_vol_ratio:.1f})+5")
            elif call_vol_ratio >= 1.5:
                auction_score += 3
                auction_reasons.append(f"竞价关注度高(量比{call_vol_ratio:.1f})+3")
            elif call_vol_ratio >= 0.5:
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
            if amount >= 5000000:
                auction_score += 2
                auction_reasons.append(f"竞价成交{amount / 10000:.0f}万+2")
            elif amount >= 1000000:
                auction_score += 1
                auction_reasons.append(f"竞价成交{amount / 10000:.0f}万+1")
    except Exception:
        pass

    score += max(0, min(15, auction_score))
    reasons.extend(auction_reasons[:3])

    # ═══════════════════════════════════════════════════════════
    # 4.  新增奖金
    # ═══════════════════════════════════════════════════════════

    # --- 4.1 昨日涨停溢价 +5 ---
    if yesterday_was_limit:
        score += 5
        reasons.append("昨日涨停溢价+5")

    # ═══════════════════════════════════════════════════════════
    # 5. 高位情绪折扣（保留）
    # ═══════════════════════════════════════════════════════════
    stock_continuity = 0
    try:
        for item in step_data:
            if item.get("ts_code") == code:
                stock_continuity = safe_int(item.get("nums", 0)) or 0
                break
    except Exception:
        pass

    if stock_continuity >= 4:
        discount = 0.90 if stock_continuity == 4 else (0.85 if stock_continuity <= 6 else 0.7)
        score_before = score
        score = round(score * discount)
        reasons.append(
            f"[折扣]连板{stock_continuity}板×{discount} {score_before}→{score}")

    # ═══════════════════════════════════════════════════════════
    # 6. 最终汇总
    # ═══════════════════════════════════════════════════════════
    final_score = max(0, min(100, score))

    if final_score >= 75:
        level = "高"
    elif final_score >= 55:
        level = "中"
    elif final_score >= 35:
        level = "低"
    else:
        level = "无"

    reason_str = ("[-" + level + "] "
                  + "; ".join(reasons[:8])
                  if reasons else f"[-{level}] 无明显信号")

    return final_score, reason_str
