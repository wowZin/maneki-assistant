#!/usr/bin/env python3
"""基本面分析 - score_fundamental"""
import sys
from pathlib import Path
PROJECT_DIR = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_DIR))
sys.path.insert(0, str(PROJECT_DIR / "scripts"))

from scripts.tu_share import call_tushare  # noqa: E402
from plays.limit_up.utils import safe_float, safe_int_none  # noqa: E402


safe_int = safe_int_none
def score_fundamental(code):
    """
    基本面涨停潜力量化评分
    五维度：业绩40% + 事件30% + 筹码15% + 财务10% + 估值5%
    含财务避雷一票否决（含行业豁免）+ 见光死惩罚 + 非线性共振加分
    """
    from datetime import datetime, timedelta

    # ===== 1. 数据获取 =====
    # 获取daily_basic (估值)
    try:
        resp = call_tushare("daily_basic", {"ts_code": code}, "pe,pb,total_mv,circ_mv")
        basic_data = resp.get("data", {}).get("items", [])
        pe, pb, total_mv, circ_mv = (basic_data[0][0], basic_data[0][1], basic_data[0][2], basic_data[0][3]) if basic_data else (None, None, None, None)
    except Exception:
        pe, pb, _total_mv, _circ_mv = None, None, None, None
    
    # 获取fina_indicator (盈利+财务) - 取最近2期以覆盖年报
    try:
        resp = call_tushare("fina_indicator", {"ts_code": code}, "ann_date,end_date,roe,roe_dt,dt_netprofit_yoy,n_income,dt_netprofit,or_yoy,op_yoy,debt_to_assets,current_ratio,ocfps,bps")
        fina_data = resp.get("data", {})
        _fina_fields = fina_data.get("fields", [])
        _fina_items = fina_data.get("items", [])
        fina_latest = dict(zip(_fina_fields, _fina_items[0])) if _fina_items else {}
        # 如果最新是Q1/Q3(非年报/半年报)，取第二期作为补充
        fina_annual = {}
        if fina_latest.get('end_date'):
            _ed = str(fina_latest['end_date'])
            if _ed.endswith('0331') or _ed.endswith('0930'):
                fina_annual = dict(zip(_fina_fields, _fina_items[1])) if len(_fina_items) > 1 else {}
    except Exception:
        fina_latest, fina_annual = {}, {}
    
    # 获取balancesheet (商誉)
    try:
        resp = call_tushare("balancesheet", {"ts_code": code}, "ann_date,end_date,goodwill,total_hldr_eqy_exc_min_int")
        _bs = resp.get("data", {})
        _bs_fields = _bs.get("fields", [])
        _bs_items = _bs.get("items", [])
        bs_latest = dict(zip(_bs_fields, _bs_items[0])) if _bs_items else {}
    except Exception:
        bs_latest = {}
    
    # 获取income (营收+非经常性损益)
    try:
        resp = call_tushare("income", {"ts_code": code}, "ann_date,end_date,total_revenue,revenue,n_income,non_oper_income,non_oper_exp")
        _inc = resp.get("data", {})
        _inc_fields = _inc.get("fields", [])
        _inc_items = _inc.get("items", [])
        dict(zip(_inc_fields, _inc_items[0])) if _inc_items else {}
        dict(zip(_inc_fields, _inc_items[1])) if len(_inc_items) > 1 else {}
    except Exception:
        _inc_latest, _inc_prev = {}, {}
    
    # 获取股东户数
    try:
        resp = call_tushare("stk_holdernumber", {"ts_code": code}, "ann_date,end_date,holder_num")
        _hld = resp.get("data", {})
        _hld_fields = _hld.get("fields", [])
        _hld_items = _hld.get("items", [])
        holder_latest = dict(zip(_hld_fields, _hld_items[0])) if _hld_items else {}
        holder_prev = dict(zip(_hld_fields, _hld_items[1])) if len(_hld_items) > 1 else {}
    except Exception:
        holder_latest, holder_prev = {}, {}
    
    # 获取概念板块数量
    try:
        resp = call_tushare("concept_detail", {"ts_code": code}, "id,concept_name")
        concept_items = resp.get("data", {}).get("items", [])
        concept_count = len(concept_items) if concept_items else 0
    except Exception:
        concept_count = 0
    
    # 获取近期公告 (用于事件窗口判断)
    recent_ann_count = 0
    recent_ann_window = 10  # 事件窗口: 近10个自然日
    try:
        today_str = datetime.now().strftime("%Y%m%d")
        start_str = (datetime.now() - timedelta(days=recent_ann_window)).strftime("%Y%m%d")
        resp = call_tushare("anns_d", {"ts_code": code, "start_date": start_str, "end_date": today_str}, "ann_date,title")
        ann_items = resp.get("data", {}).get("items", [])
        recent_ann_count = len(ann_items) if ann_items else 0
    except Exception:
        recent_ann_count = 0
    
    # 获取行业分类（用于行业豁免判断）
    industry_name = ""
    try:
        resp = call_tushare("stock_basic", {"ts_code": code}, "ts_code,industry")
        _sb = resp.get("data", {}).get("items", [])
        if _sb:
            _sb_fields = resp.get("data", {}).get("fields", [])
            _sb_dict = dict(zip(_sb_fields, _sb[0])) if _sb_fields else {}
            industry_name = str(_sb_dict.get('industry', ''))
    except Exception:
        pass
    
    # ===== 2. 一票否决检查（含行业豁免） =====
    risk_flags = []
    is_vetoed = False
    
    # 获取ROE用于行业豁免判断
    _roe_source = fina_annual if fina_annual.get('end_date', '').endswith('1231') else fina_latest
    roe_source_val = safe_float(_roe_source.get('roe')) or 0
    
    # 商誉/净资产 > 30% 且 ROE < 10%（医药/电子行业ROE>10%暂免）
    if bs_latest.get('goodwill') and bs_latest.get('total_hldr_eqy_exc_min_int'):
        goodwill_ratio = safe_float(bs_latest['goodwill']) / safe_float(bs_latest['total_hldr_eqy_exc_min_int']) * 100
        if goodwill_ratio > 30:
            # 行业豁免：医药/电子行业ROE>10%免于否决
            pharma_and_elec = any(kw in industry_name for kw in ["医药", "医疗", "电子", "半导体", "元器件"])
            if pharma_and_elec and roe_source_val > 10:
                risk_flags.append(f"商誉占比{goodwill_ratio:.0f}%(行业豁免)")
            else:
                is_vetoed = True
                risk_flags.append(f"商誉占比{goodwill_ratio:.0f}%>30%")
    
    # 资产负债率 > 70% 且 经营现金流连续2季度为负
    # 行业豁免：房地产/建筑/非银金融/公用事业仅看现金流
    debt_exempt_industries = ["房地产", "建筑", "非银金融", "公用事业", "银行", "综合"]
    is_debt_exempt = any(kw in industry_name for kw in debt_exempt_industries)
    if fina_latest.get('debt_to_assets'):
        debt_ratio = safe_float(fina_latest.get('debt_to_assets'))
        if debt_ratio > 70:
            ocfps_latest = safe_float(fina_latest.get('ocfps'))
            ocfps_annual = safe_float(fina_annual.get('ocfps')) if fina_annual else None
            if is_debt_exempt:
                # 行业豁免：仅检查现金流，负债率高不否决
                if ocfps_latest is not None and ocfps_latest < 0:
                    if ocfps_annual is None or ocfps_annual < 0:
                        is_vetoed = True
                        risk_flags.append("经营现金流连续为负(行业豁免负债率)")
                else:
                    risk_flags.append(f"负债率{debt_ratio:.0f}%>70%(行业豁免)")
            else:
                if ocfps_latest is not None and ocfps_latest < 0:
                    if ocfps_annual is None or ocfps_annual < 0:
                        is_vetoed = True
                        risk_flags.append(f"负债率{debt_ratio:.0f}%>70%且经营现金流为负")
    
    # 扣非净利润连续3年亏损 + 困境反转豁免（用扣非净利润判断）
    is_consecutive_loss = False
    if fina_latest.get('dt_netprofit_yoy'):
        profit_yoy = safe_float(fina_latest.get('dt_netprofit_yoy'))
        if profit_yoy is not None and profit_yoy < -50:
            is_consecutive_loss = True
    
    # 困境反转检查：最新扣非>0且去年同期扣非<0 → 豁免（防假反转）
    is_turnaround = False
    if fina_latest.get('n_income') and fina_latest.get('dt_netprofit'):
        latest_ni = safe_float(fina_latest['n_income'])
        latest_dt = safe_float(fina_latest['dt_netprofit'])
        if latest_ni is not None and latest_dt is not None and latest_ni > 0 and latest_dt > 0:
            # 需要至少两期数据判断"去年亏损"
            if fina_annual.get('dt_netprofit'):
                prev_dt = safe_float(fina_annual['dt_netprofit'])
                if prev_dt is not None and prev_dt < 0:
                    is_turnaround = True
    
    if is_consecutive_loss and not is_turnaround:
        is_vetoed = True
        risk_flags.append("扣非净利润连续亏损(无困境反转)")
    
    # 非经常性损益占比 > 50%（用净利润-扣非净利润计算，非营业外收支）
    if fina_latest.get('n_income') and fina_latest.get('dt_netprofit'):
        n_income = safe_float(fina_latest['n_income'])
        dt_netprofit = safe_float(fina_latest['dt_netprofit'])
        if n_income is not None and dt_netprofit is not None and n_income != 0:
            non_recurring = n_income - dt_netprofit
            non_op_ratio = abs(non_recurring / n_income) * 100
            if non_op_ratio > 50:
                is_vetoed = True
                risk_flags.append(f"非经常性损益占比{non_op_ratio:.0f}%>50%")
    
    # 主业营收占比：废弃（Tushare income表revenue≈total_revenue, 恒≈100%）
    # 经营活动现金流健康度由负债率否决一并覆盖
    
    # 近6个月内存在 > 流通盘10% 的大额解禁
    try:
        from datetime import datetime, timedelta
        end_dt = datetime.now()
        start_dt = end_dt - timedelta(days=180)
        resp = call_tushare("share_float", {
                    "ts_code": code,
                    "start_date": start_dt.strftime('%Y%m%d'),
                    "end_date": end_dt.strftime('%Y%m%d')
                }, "float_date,float_share,float_ratio")
        float_items = resp.get("data", {}).get("items", [])
        if float_items:
            for item in float_items:
                float_ratio = safe_float(item[2]) if len(item) > 2 else 0
                if float_ratio and float_ratio > 10:
                    is_vetoed = True
                    risk_flags.append(f"大额解禁{item[0]}占比{float_ratio:.1f}%")
                    break
    except Exception:
        pass
    
    # 获取近20日涨幅（见光死惩罚用）
    pre_return_20d = 0
    try:
        end_dt = datetime.now()
        start_dt = end_dt - timedelta(days=25)
        resp_pr = call_tushare("daily", {
            "ts_code": code,
            "start_date": start_dt.strftime('%Y%m%d'),
            "end_date": end_dt.strftime('%Y%m%d')
        }, "trade_date,close")
        pr_items = resp_pr.get("data", {}).get("items", [])
        if pr_items and len(pr_items) >= 2:
            _pr_fields = resp_pr.get("data", {}).get("fields", [])
            _pr_dicts = [dict(zip(_pr_fields, x)) for x in pr_items if _pr_fields]
            close_20d_ago = safe_float(_pr_dicts[-1].get('close', 0)) or 0
            close_latest = safe_float(_pr_dicts[0].get('close', 0)) or 0
            if close_20d_ago > 0:
                pre_return_20d = (close_latest - close_20d_ago) / close_20d_ago * 100
    except Exception:
        pass
    
    # 获取分析师一致预期（用于见光死惩罚）
    eps_revision_1m = 0
    try:
        resp_fc = call_tushare("fina_forecast", {"ts_code": code}, "ts_code,ann_date,end_date,type,report_date,eps_change_ratio")
        fc_items = resp_fc.get("data", {}).get("items", [])
        if fc_items:
            fc_fields = resp_fc.get("data", {}).get("fields", [])
            fc_dicts = [dict(zip(fc_fields, x)) for x in fc_items if fc_fields]
            for fc in fc_dicts:
                change_ratio = safe_float(fc.get('eps_change_ratio', 0)) or 0
                if change_ratio != 0:
                    eps_revision_1m = change_ratio
                    break
    except Exception:
        pass
    
    # 触发否决直接返回0分
    if is_vetoed:
        return 0, f"财务避雷否决: {'; '.join(risk_flags)}"
    
    # ===== 3. 五维度评分 =====
    factors = {}  # 各维度标准化得分 [0,1]
    reasons = []
    
    # --- 3.1 盈利业绩维度 (40%) ---
    profit_score = 0.5  # 基准
    profit_reasons = []
    
    # ROE（复用已计算的_roe_source和roe_source_val）
    if roe_source_val > 0:
        if roe_source_val > 15:
            profit_score += 0.20
            profit_reasons.append(f"ROE={roe_source_val:.1f}%")
        elif roe_source_val > 10:
            profit_score += 0.10
            profit_reasons.append(f"ROE={roe_source_val:.1f}%")
        elif roe_source_val < 5:
            profit_score -= 0.15
            profit_reasons.append(f"ROE={roe_source_val:.1f}%偏低")
    
    # 扣非净利润同比
    if fina_latest.get('dt_netprofit_yoy'):
        profit_yoy = safe_float(fina_latest.get('dt_netprofit_yoy'))
        if profit_yoy > 50:
            profit_score += 0.25
            profit_reasons.append(f"扣非净利+{profit_yoy:.0f}%")
        elif profit_yoy > 20:
            profit_score += 0.15
            profit_reasons.append(f"扣非净利+{profit_yoy:.0f}%")
        elif profit_yoy < 0:
            profit_score -= 0.20
            profit_reasons.append(f"扣非净利{profit_yoy:.0f}%")
    
    # 营收增速
    if fina_latest.get('or_yoy'):
        rev_yoy = safe_float(fina_latest.get('or_yoy'))
        if rev_yoy > 30:
            profit_score += 0.15
            profit_reasons.append(f"营收+{rev_yoy:.0f}%")
        elif rev_yoy < 0:
            profit_score -= 0.10
    
    factors['profit'] = max(0, min(1, profit_score))
    reasons.extend(profit_reasons)
    
    # --- 3.2 题材事件维度 (30%) ---
    event_score = 0.5
    event_reasons = []
    
    # 概念数量（代理事件热度）
    if concept_count >= 8:
        event_score += 0.20
        event_reasons.append(f"{concept_count}个概念")
    elif concept_count >= 4:
        event_score += 0.10
        event_reasons.append(f"{concept_count}个概念")
    elif concept_count == 0:
        event_score -= 0.10
    
    # 公告事件强度（有近期公告加分）
    if recent_ann_count >= 3:
        event_score += 0.20
        event_reasons.append(f"近期{recent_ann_count}条公告")
    elif recent_ann_count >= 1:
        event_score += 0.10
        event_reasons.append(f"近期{recent_ann_count}条公告")
    
    # 见光死惩罚 — 公告前涨幅过大导致事件因子衰减
    if pre_return_20d > 0 and eps_revision_1m != 0:
        price_in_ratio = pre_return_20d / (abs(eps_revision_1m) + 5)
        if price_in_ratio > 2.5 and (factors.get('profit', 0) > 0.6 or event_score > 0.7):
            event_score *= 0.5  # 事件贡献腰斩
            reasons.append(f"见光死PIR={price_in_ratio:.1f}>2.5事件减半")
        if price_in_ratio > 3.5:
            factors['profit'] = max(0, factors.get('profit', 0) * 0.7)  # 业绩也衰减
            reasons.append(f"见光死PIR={price_in_ratio:.1f}>3.5业绩减30%")
    
    factors['event'] = max(0, min(1, event_score))
    reasons.extend(event_reasons)
    
    # --- 3.3 股东筹码维度 (15%) ---
    chip_score = 0.5
    chip_reasons = []
    
    # 股东户数环比下降（仅做底仓过滤，不直接加分）
    if holder_latest.get('holder_num') and holder_prev.get('holder_num'):
        try:
            holder_now = safe_float(holder_latest.get('holder_num'))
            holder_before = safe_float(holder_prev.get('holder_num'))
            holder_chg = (holder_before - holder_now) / holder_before
            if holder_chg >= 0.05:
                chip_score += 0.20
                chip_reasons.append(f"股东户数-{holder_chg*100:.1f}%")
            elif holder_chg < -0.05:
                chip_score -= 0.20
                chip_reasons.append(f"股东户数+{abs(holder_chg)*100:.1f}%")
        except Exception:
            pass
    
    # 连板基因（近60日曾连板 → 有资金记忆）
    try:
        end_dt = datetime.now()
        start_dt = end_dt - timedelta(days=65)
        resp_gene = call_tushare("limit_step", {
            "ts_code": code,
            "start_date": start_dt.strftime('%Y%m%d'),
            "end_date": end_dt.strftime('%Y%m%d')
        }, "trade_date,ts_code,nums")
        gene_items = resp_gene.get("data", {}).get("items", [])
        if gene_items:
            max_nums = max([safe_int(x[2]) or 0 for x in gene_items], default=0)
            if max_nums >= 2:
                chip_score += 0.15
                chip_reasons.append(f"连板基因{max_nums}连板")
    except Exception:
        pass
    
    # 解禁冲击（未来30日解禁市值/流通市值 <2% → +2分）
    try:
        start_dt = datetime.now()
        end_dt = datetime.now() + timedelta(days=30)
        resp_unlock = call_tushare("share_float", {
            "ts_code": code,
            "start_date": start_dt.strftime('%Y%m%d'),
            "end_date": end_dt.strftime('%Y%m%d')
        }, "float_date,float_share,float_ratio")
        unlock_items = resp_unlock.get("data", {}).get("items", [])
        has_large_unlock = False
        if unlock_items:
            for item in unlock_items:
                float_ratio = safe_float(item[2]) if len(item) > 2 else 0
                if float_ratio and float_ratio > 2:
                    has_large_unlock = True
                    break
        if not has_large_unlock:
            chip_score += 0.10
            chip_reasons.append("解禁冲击低+0.1")
    except Exception:
        pass
    
    factors['chip'] = max(0, min(1, chip_score))
    reasons.extend(chip_reasons)
    
    # --- 3.4 财务健康维度 (10%) ---
    finance_score = 1.0
    finance_reasons = []
    
    # 负债率风险扣分（未触发否决则在此扣分）
    if fina_latest.get('debt_to_assets'):
        debt_ratio = safe_float(fina_latest.get('debt_to_assets'))
        if debt_ratio > 60:
            finance_score -= 0.20
            finance_reasons.append(f"负债率{debt_ratio:.0f}%")
        elif debt_ratio > 50:
            finance_score -= 0.10
    
    # 流动比率
    if fina_latest.get('current_ratio'):
        current_ratio = safe_float(fina_latest.get('current_ratio'))
        if current_ratio and current_ratio < 1:
            finance_score -= 0.20
            finance_reasons.append("流动性风险")
    
    factors['finance'] = max(0, min(1, finance_score))
    if finance_reasons:
        reasons.extend(finance_reasons)
    
    # --- 3.5 估值性价比维度 (5%) ---
    value_score = 0.0  # 废除基准分，仅作为加分项
    value_reasons = []
    
    # PE相对估值（废除低PE加分，改为上调幅度检测）
    if pe and pe > 0 and pb and pb > 0:
        # 远期PEG < 1.5视为有估值重塑空间
        if pe / (roe_source_val if roe_source_val > 0 else 15) < 1.5:
            value_score += 0.30
            value_reasons.append("PEG合理")
        # 困境反转特殊加分
        if is_turnaround:
            value_score += 0.30
            value_reasons.append("困境反转+0.3")
    
    factors['value'] = max(0, min(1, value_score))
    reasons.extend(value_reasons)
    
    # ===== 4. 综合评分计算 =====
    weights = {
        'profit': 0.40,
        'event': 0.30,
        'chip': 0.15,
        'finance': 0.10,
        'value': 0.05
    }
    
    base_score = sum(factors[k] * weights[k] for k in factors) * 100
    
    # ===== 5. 非线性共振加分（取最高，不叠加） =====
    bonus = 0
    # 条件A: 业绩≥0.8 且 事件≥0.7 且 事件窗口t≤10
    if factors.get('profit', 0) >= 0.8 and factors.get('event', 0) >= 0.7 and recent_ann_count > 0:
        bonus = 15
        reasons.append("共振A:业绩+事件+15")
    # 条件B: 筹码≥0.8 且 估值≥0.6
    elif factors.get('chip', 0) >= 0.8 and factors.get('value', 0) >= 0.6:
        bonus = 10
        reasons.append("共振B:筹码+估值+10")
    
    # 困境反转独立加分（最新单季扣非>0且去年同期<0 → +5分）
    turnaround_bonus = 0
    if is_turnaround:
        turnaround_bonus = 5
        reasons.append("困境反转+5")
    
    final_score = min(100, base_score + bonus + turnaround_bonus)
    
    # ===== 6. 涨停潜力等级 =====
    if final_score >= 75:
        level = "高"
    elif final_score >= 55:
        level = "中"
    elif final_score >= 35:
        level = "低"
    else:
        level = "无"
    
    reason_str = f"[{level}] " + "; ".join(reasons[:5]) if reasons else f"[{level}] 数据不足"
    
    return round(final_score, 1), reason_str
