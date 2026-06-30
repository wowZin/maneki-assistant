#!/usr/bin/env python3
"""短线博弈面评分  — 预突破检测 + 打板增强

与 V1/V2 的核心差异:
  - 不再仅依赖 limit_list_d 封板数据，大部分因子可用于未涨停股
  - 新增"预突破检测"三因子: 缩量后放量/价格收敛/连续高开
  - 移除板块助攻（已由 sentiment.py 覆盖），替换为资金共振
  - 集合竞价简化为活跃度检查
  - 封板质量降权为 15%，仅作为潜在加分项

因子权重:
  1. 连板动量 30% — 连板数、涨停基因、断板反包、涨幅活跃度
  2. 攻击独特性 25% — 涨停高开率、近10日涨幅、弱转强 + 预突破信号
  3. 封板质量 15% — 封板时间、封单流通比、撤单强度（仅涨停股有数据）
  4. 开盘博弈 15% — 换手率、量比、开盘形态 + 缩量后放量/价格收敛
  5. 资金共振 15% — 多源资金流确认（jvQuant + Tushare moneyflow）

集合竞价简化为活跃度 bonus（最高+3分）。
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_DIR))

# ── 工具函数 ─────────────────────────────────────────────


def _today() -> str:
    """今日日历日期"""
    return datetime.now().strftime("%Y%m%d")


def _query_date() -> str:
    """Tushare 查询日期"""
    from scripts.tu_share import get_last_trade_date_with_data
    return get_last_trade_date_with_data()


def _safe_float(val, default=0.0):
    if val is None:
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def _safe_int(val, default=0):
    if val is None:
        return default
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


# ── 数据获取（带缓存/降级） ──────────────────────────────


def _to_df(api_name: str, params: dict, fields: str = ""):
    """调 call_tushare 返回 DataFrame"""
    from scripts.tu_share import call_tushare
    result = call_tushare(api_name, params, fields)
    items = result.get("data", {}).get("items", [])
    cols = result.get("data", {}).get("fields", [])
    if not items or not cols:
        return None
    import pandas as pd
    return pd.DataFrame(items, columns=cols)


def _get_limit_history(code: str, days=30):
    """获取个股历史涨停记录（最近 N 天）"""
    start = (datetime.now() - timedelta(days=days * 2)).strftime("%Y%m%d")
    end = _query_date()
    try:
        return _to_df("limit_list_d", {"ts_code": code, "start_date": start,
                                       "end_date": end, "limit_type": "U"})
    except Exception:
        return None


def _get_daily_data(code: str, days=30):
    """获取个股日线数据（降序）"""
    start = (datetime.now() - timedelta(days=days * 2)).strftime("%Y%m%d")
    end = _query_date()
    try:
        df = _to_df("daily", {"ts_code": code, "start_date": start, "end_date": end})
        if df is not None and not df.empty:
            return df.sort_values("trade_date", ascending=False)
        return None
    except Exception:
        return None


def _get_daily_basic(code: str) -> dict:
    """获取个股基础面数据"""
    today = _query_date()
    try:
        from scripts.tu_share import call_tushare
        result = call_tushare("daily_basic", {"ts_code": code, "trade_date": today},
                              "ts_code,close,turnover_rate,volume_ratio,free_share,pe,pb")
        items = result.get("data", {}).get("items", [])
        fields = result.get("data", {}).get("fields", [])
        if items and fields:
            return dict(zip(fields, items[0]))
    except Exception:
        pass
    return {}


def _get_realtime_quote(code: str) -> dict:
    """获取实时行情数据（优先实时，盘后降级 Tushare）"""
    from plays.limit_up.utils import is_market_closed, get_stock_quote

    if is_market_closed():
        q = get_stock_quote(code)
        if q.get("change_pct") is not None:
            t = q.get("turnover_rate", 0)
            if t > 1:
                q["turnover_rate"] = t / 100
            return q
        return {}

    result = {}
    try:
        from scripts.ths_client import get_ths_client
        ths = get_ths_client()
        quote = ths.get_quote(code)
        if quote:
            result["change_pct"] = quote.get("pct_chg", 0)
            result["now_price"] = quote.get("price", 0)
            result["amount"] = quote.get("amount", 0)
            result["vol_ratio"] = quote.get("vol_ratio", 0)
            rt = quote.get("turnover", 0)
            if rt:
                result["turnover_rate"] = rt / 100
            if quote.get("f_407", 0) > 0:
                result["circ_mv"] = quote.get("f_407", 0)
            price = quote.get("price", 0)
            open_p = quote.get("open", 0)
            if price > 0 and open_p > 0:
                result["open_pct"] = (open_p - price) / price * 100
            result["jj_active"] = (
                result.get("vol_ratio", 0) > 1.5
                and abs(result.get("open_pct", 0)) > 2
            )
    except Exception:
        pass
    return result


def _get_today_limit_list_row(code: str):
    """获取今日个股的 limit_list_d 数据（若今日有涨停）"""
    today = _query_date()
    try:
        df = _to_df("limit_list_d", {"ts_code": code, "trade_date": today, "limit_type": "U"})
        if df is not None and not df.empty:
            return df.iloc[0]
    except Exception:
        pass
    return None


def _get_moneyflow(code: str, days=3) -> dict:
    """获取个股近期资金流向（中单净流入等）

    返回 dict 或 {}: {net_mf_amount, buy_md_amount/vol, sell_md_amount/vol, ...}
    数据按 trade_date 降序，[0] 为最近一个交易日
    """
    today = _query_date()
    try:
        from scripts.tu_share import call_tushare
        result = call_tushare("moneyflow", {"ts_code": code},
                              "trade_date,net_mf_amount,buy_md_vol,sell_md_vol,"
                              "buy_md_amount,sell_md_amount")
        items = result.get("data", {}).get("items", [])
        fields = result.get("data", {}).get("fields", [])
        if items and fields:
            records = [dict(zip(fields, item)) for item in items]
            return records[0] if records else {}
    except Exception:
        pass
    return {}


# ── 1. 连板动量 (30%) ─────────────────────────────────


def _score_momentum(code: str, daily_data, limit_history) -> tuple:
    """连板动量评分 — 保留 V1 核心逻辑，增加无涨停时的基础评估

    对未涨停股:
      - 有历史涨停记录的: 基于基因+断板反包评分
      - 无历史记录的: 仅基于涨幅活跃度评分
    """
    reasons = []
    score = 0
    has_limit_history = limit_history is not None and not limit_history.empty

    if has_limit_history:
        # 涨停日期列表（降序）
        limit_dates = limit_history["trade_date"].tolist()
        latest_ul_date = str(limit_dates[0])
        yesterday = _today()

        # 判断今日是否仍在连板中
        is_current_limit_up = (latest_ul_date == yesterday)

        # 连板数
        limit_times = _safe_int(limit_history.iloc[0].get("limit_times", 0))
        if is_current_limit_up and limit_times >= 4:
            score += 50
            reasons.append(f"{int(limit_times)}连板+50")
        elif is_current_limit_up and limit_times == 3:
            score += 40
            reasons.append("3连板+40")
        elif is_current_limit_up and limit_times == 2:
            score += 30
            reasons.append("2连板+30")
        elif is_current_limit_up and limit_times >= 1:
            score += 15
            reasons.append("首板+15")
        elif not is_current_limit_up and limit_times >= 1:
            # 断板日: 给基因分，且会触发下方断板反包检查
            reasons.append(f"断板(历史{int(limit_times)}连板)")
            score += 8

        # 涨停基因: 近30天涨停次数
        recent_count = len(limit_history)
        if recent_count >= 5:
            score += 20
            reasons.append(f"近月{recent_count}次涨停+20")
        elif recent_count >= 3:
            score += 12
            reasons.append(f"近月{recent_count}次涨停+12")
        elif recent_count >= 1:
            score += 5
            reasons.append(f"近月{recent_count}次涨停+5")

        # 断板反包: 涨停日间隔2-9日再次涨停
        if len(limit_dates) >= 2:
            gaps = []
            for i in range(1, min(len(limit_dates), 6)):
                try:
                    d1 = datetime.strptime(str(limit_dates[i - 1]), "%Y%m%d")
                    d2 = datetime.strptime(str(limit_dates[i]), "%Y%m%d")
                    gap = (d1 - d2).days
                    if 2 <= gap <= 9:
                        gaps.append(gap)
                except Exception:
                    continue
            if gaps:
                score += 15
                reasons.append("断板反包+15")
    else:
        reasons.append("近期无涨停")
        score += 2  # 微小基础分，避免完全零分

    # 涨幅活跃度（近5日日均涨幅，对所有股票生效）
    if daily_data is not None and not daily_data.empty:
        recent_days = daily_data.head(min(5, len(daily_data)))
        avg_pct = recent_days["pct_chg"].mean()
        if avg_pct > 3:
            score += 15
            reasons.append(f"近5日活跃(均涨{avg_pct:.1f}%)+15")
        elif avg_pct > 1:
            score += 8
            reasons.append(f"近5日温和(均涨{avg_pct:.1f}%)+8")
        elif avg_pct > 0:
            score += 3
            reasons.append(f"近5日微涨(均涨{avg_pct:.1f}%)+3")

    # 高位连板折扣: ≥4连板打9折, ≥5连板打85折, ≥7连板打7折
    if is_current_limit_up and limit_times >= 4:
        discount = 0.90 if limit_times == 4 else (0.85 if limit_times <= 6 else 0.7)
        score_before = score
        score = score * discount
        reasons.append(f"[折扣]{int(limit_times)}连板×{discount}")

    total = min(score, 100)
    reason_str = "; ".join(reasons) if reasons else "无数据"
    return max(total, 0), f"[连板] {reason_str}"


# ── 2. 攻击独特性 (25%) + 预突破信号 ──────────────────


def _score_aggression(code: str, daily_data) -> tuple:
    """攻击独特性评分 

    保留 V1 核心: 涨停高开率、近10日涨幅、弱转强
    新增预突破信号:
      - 连续高开: 近3日连续高开 → bonus
    """
    reasons = []
    score = 0

    if daily_data is None or daily_data.empty:
        reasons.append("无近30日数据")
        return 0, "[攻击] 无近30日数据+0"

    recent = daily_data.head(20)
    today_str = _today()

    # ① 涨停高开率（近20日有过涨停 + 次日高开>2% 比例>50%）
    limit_up_dates = recent[recent["pct_chg"] >= 9.5]
    if not limit_up_dates.empty:
        high_open_count = 0
        for lu_idx in limit_up_dates.index:
            try:
                pos = recent.index.get_loc(lu_idx)
                if isinstance(pos, slice):
                    pos = pos.start
                if pos > 0:  # 有下一个交易日
                    next_day = recent.iloc[pos - 1]
                    next_open = _safe_float(next_day.get("open", 0))
                    next_pre = _safe_float(next_day.get("pre_close", 0))
                    if next_pre > 0:
                        open_pct = (next_open / next_pre - 1) * 100
                        if open_pct > 2:
                            high_open_count += 1
            except Exception:
                continue

        total_limit = len(limit_up_dates)
        if total_limit > 0 and high_open_count / total_limit > 0.5:
            score += 40
            reasons.append(f"涨停高开率{high_open_count}/{total_limit}>50%+40")

    # ② 近10日最大单日涨幅 > 7%
    max_pct = recent.head(10)["pct_chg"].max()
    if max_pct > 7:
        score += 35
        reasons.append(f"近10日最大涨幅{max_pct:.1f}%+35")

    # ③ 弱转强（昨涨停 + 今开[-2%,2%] + 今涨幅>4%）
    if len(daily_data) >= 2:
        today_row = daily_data.iloc[0]
        yesterday_row = daily_data.iloc[1]
        today_date = str(today_row.get("trade_date", ""))
        yest_date = str(yesterday_row.get("trade_date", ""))

        if today_date == today_str and today_date > yest_date:
            y_pct = _safe_float(yesterday_row.get("pct_chg", 0))
            t_open = _safe_float(today_row.get("open", 0))
            t_pre = _safe_float(today_row.get("pre_close", 0))
            t_pct = _safe_float(today_row.get("pct_chg", 0))

            if y_pct >= 9.5 and t_pre > 0:
                open_pct = (t_open / t_pre - 1) * 100
                if -2 <= open_pct <= 2 and t_pct > 4:
                    score += 25
                    reasons.append(f"弱转强(昨涨停今开{open_pct:.1f}%今{t_pct:.1f}%)+25")

    # ④ [新增] 连续高开信号（consecutive higher opens）
    # 近3个交易日连续高开（每日 open > pre_close），说明资金持续抢筹
    if len(daily_data) >= 3:
        consec_high_open = 0
        for i in range(min(3, len(daily_data))):
            row = daily_data.iloc[i]
            o = _safe_float(row.get("open", 0))
            pc = _safe_float(row.get("pre_close", 0))
            if pc > 0 and o > pc:
                consec_high_open += 1
            else:
                break
        if consec_high_open >= 3:
            score += 15
            reasons.append("连续3日高开+15")
        elif consec_high_open >= 2:
            score += 8
            reasons.append("连续2日高开+8")

    total = min(score, 100)
    reason_str = "; ".join(reasons) if reasons else "无数据"
    return max(total, 0), f"[攻击] {reason_str}"


# ── 3. 封板质量 (15%) — 降权，仅作为潜在加分 ──────────


def _score_seal_quality(code: str, today_ul_row, em_data) -> tuple:
    """封板质量评分  — 仅在已有封板数据时生效

    降权到 15%，非涨停股此项记0分而非扣分。
    """
    reasons = []
    score = 0

    if today_ul_row is None:
        # 无封板数据，记为 0 分（不扣分）
        return 0, "[封板] 未涨停(此项0分)"

    row = today_ul_row
    circ_mv = _safe_float(em_data.get("circ_mv", 0))

    # 封板时间
    open_time = str(row.get("open_time", "")).strip()
    if open_time:
        try:
            hour = int(open_time[:2])
            minute = int(open_time[2:4])
            hm = hour * 100 + minute
            if hm <= 930:
                score += 50
                reasons.append("开盘秒板+50")
            elif hm <= 1000:
                score += 40
                reasons.append("30分内封板+40")
            elif hm <= 1030:
                score += 25
                reasons.append("早盘封板+25")
            elif hm <= 1130:
                score += 20
                reasons.append("午前封板+20")
            else:
                score += 10
                reasons.append("午后封板+10")
        except Exception:
            pass

    # 封单流通比
    first_amount = _safe_float(row.get("first_limit_amount", 0))
    seal_mv_ratio = 0
    if first_amount > 0:
        if circ_mv > 0:
            seal_mv_ratio = first_amount / circ_mv
        else:
            free_share = _safe_float(0)  # 无 daily_basic 时不计算
            if free_share > 0:
                close_price = _safe_float(0)
                seal_mv_ratio = first_amount / (free_share * 10000 * close_price)

        if seal_mv_ratio > 0.15:
            score += 50
            reasons.append(f"封单流通比{seal_mv_ratio:.1%}+50")
        elif seal_mv_ratio > 0.10:
            score += 35
            reasons.append(f"封单流通比{seal_mv_ratio:.1%}+35")
        elif seal_mv_ratio > 0.05:
            score += 25
            reasons.append(f"封单流通比{seal_mv_ratio:.1%}+25")
        elif seal_mv_ratio > 0.02:
            score += 15
            reasons.append(f"封单流通比{seal_mv_ratio:.1%}+15")
        elif seal_mv_ratio > 0:
            score += 10
            reasons.append(f"封单流通比{seal_mv_ratio:.1%}+10")

    # 撤单强度
    last_amount = _safe_float(row.get("last_limit_amount", 0))
    if first_amount > 0 and last_amount > 0:
        seal_ratio = last_amount / first_amount
        if seal_ratio < 0.3:
            score -= 20
            reasons.append("大幅撤单-20")
        elif seal_ratio < 0.6:
            score -= 10
            reasons.append("部分撤单-10")

    total = min(score, 100)
    reason_str = "; ".join(reasons) if reasons else ""
    return max(total, 0), f"[封板] {reason_str}"


# ── 4. 开盘博弈 (15%) + 缩量后放量 / 价格收敛 ──────────


def _score_open_battle(code: str, daily_data, daily_basic, em_data) -> tuple:
    """开盘博弈评分 

    保留: 开盘形态、换手率、量比
    新增预突破信号:
      - 缩量后放量: 昨量比<0.8 + 今量比>1.5 → bonus
      - 价格收敛: 5日振幅<15% + 今日振幅>5% → coiling spring bonus
    """
    reasons = []
    score = 0

    # ── 获取今日日线 ──
    today_row = None
    if daily_data is not None and not daily_data.empty:
        first_date = str(daily_data.iloc[0].get("trade_date", ""))
        if first_date == _today():
            today_row = daily_data.iloc[0]
        else:
            reasons.append(f"今日无数据(最近{first_date})")

    # ── 开盘形态 ──
    if today_row is not None:
        open_p = _safe_float(today_row.get("open", 0))
        pre_close = _safe_float(today_row.get("pre_close", 0))
        high = _safe_float(today_row.get("high", 0))

        if pre_close > 0 and open_p > 0:
            open_pct = (open_p - pre_close) / pre_close * 100
            if open_pct >= 9.5:
                score += 40
                reasons.append("一字/秒板开盘+40")
            elif open_pct >= 5:
                score += 30
                reasons.append(f"高开{open_pct:.1f}%+30")
            elif open_pct >= 3:
                score += 20
                reasons.append(f"小幅高开{open_pct:.1f}%+20")
            elif open_pct >= 0:
                score += 5
                reasons.append(f"平开{open_pct:.1f}%+5")
            else:
                score -= 10
                reasons.append(f"低开{open_pct:.1f}%-10")

            # 分歧转一致
            if open_pct < 3 and high >= pre_close * 1.098:
                score += 20
                reasons.append("分歧转一致+20")

    # ── 换手率 / 量比 ──
    em_turnover = _safe_float(em_data.get("turnover_rate", 0))
    em_vol_ratio = _safe_float(em_data.get("vol_ratio", 0))

    if em_data and (em_turnover > 0 or em_vol_ratio > 0):
        turnover_rate = em_turnover * 100  # 小数→百分数
        vol_ratio = em_vol_ratio
    else:
        turnover_rate = _safe_float(daily_basic.get("turnover_rate", 0))
        vol_ratio = _safe_float(daily_basic.get("volume_ratio", 0))

    # 换手率
    if 10 <= turnover_rate <= 30:
        score += 25
        reasons.append(f"换手适中({turnover_rate:.1f}%)+25")
    elif 30 < turnover_rate <= 50:
        score += 15
        reasons.append(f"换手偏高({turnover_rate:.1f}%)+15")
    elif turnover_rate > 50:
        score -= 5
        reasons.append(f"换手过高({turnover_rate:.1f}%)-5")
    elif 5 <= turnover_rate < 10:
        score += 10
        reasons.append(f"换手偏低({turnover_rate:.1f}%)+10")
    elif 0 < turnover_rate < 5:
        score += 5
        reasons.append(f"换手极低({turnover_rate:.1f}%)+5")

    # 量比
    if vol_ratio > 3:
        score += 15
        reasons.append(f"放量(量比{vol_ratio:.1f})+15")
    elif vol_ratio > 2:
        score += 10
        reasons.append(f"量比{vol_ratio:.1f}+10")
    elif vol_ratio > 1.5:
        score += 5
        reasons.append(f"温和放量(量比{vol_ratio:.1f})+5")

    # ── [新增] 缩量后放量 (volume contraction before expansion) ──
    # 昨量比<0.8（缩量）+ 今量比>1.5（放量）→ 资金回流转强信号
    if daily_data is not None and len(daily_data) >= 2:
        yesterday_row = daily_data.iloc[1]
        y_vol = _safe_float(yesterday_row.get("vol", 0))
        t_vol = _safe_float(today_row.get("vol", 0)) if today_row is not None else 0

        # 用量比代理: 昨量比 < 0.8 + 今量比 > 1.5
        if em_vol_ratio > 0:
            # 有实时量比，用日线volume辅助验证缩量后放量
            if y_vol > 0 and t_vol > 0 and t_vol > y_vol * 1.5:
                # 今日成交量 > 昨日 1.5 倍 → 放量
                if vol_ratio > 1.5:
                    score += 10
                    reasons.append(f"缩量后放量(昨缩今量比{vol_ratio:.1f})+10")
        elif vol_ratio > 1.5:
            # 降级: 仅凭今日量比>1.5 + 日线volume增大判断
            if y_vol > 0 and t_vol > 0 and t_vol > y_vol * 1.5:
                score += 8
                reasons.append(f"缩量后放量(成交量放大{t_vol/y_vol:.1f}倍)+8")

    # ── [新增] 价格收敛 (coiling spring) ──
    # 5日振幅<15% + 今日振幅>5% → 盘整后突破信号
    if daily_data is not None and len(daily_data) >= 5:
        recent_5d = daily_data.head(5)
        # 前4日振幅（不含今日）
        prev_4d = daily_data.iloc[1:5] if len(daily_data) >= 5 else daily_data.iloc[1:]
        amplitudes = []
        for _, row in prev_4d.iterrows():
            h = _safe_float(row.get("high", 0))
            l = _safe_float(row.get("low", 0))
            pc = _safe_float(row.get("pre_close", 0))
            if pc > 0:
                amplitudes.append((h - l) / pc * 100)

        if amplitudes and today_row is not None:
            avg_amp_4d = sum(amplitudes) / len(amplitudes)
            t_h = _safe_float(today_row.get("high", 0))
            t_l = _safe_float(today_row.get("low", 0))
            t_pc = _safe_float(today_row.get("pre_close", 0))
            if t_pc > 0:
                t_amp = (t_h - t_l) / t_pc * 100
                if avg_amp_4d < 15 and t_amp > 5:
                    score += 12
                    reasons.append(f"价格收敛突破(前4日均振幅{avg_amp_4d:.1f}%今{t_amp:.1f}%)+12")

    if today_row is None and not reasons:
        reasons.append("暂无今日数据")

    total = min(score, 100)
    reason_str = "; ".join(reasons) if reasons else "无数据"
    return max(total, 0), f"[开盘] {reason_str}"


# ── 5. 资金共振 (15%) — 取代板块助攻 ──────────────────


def _score_fund_resonance(code: str, moneyflow_tushare: dict,
                          fundflow_data: dict = None) -> tuple:
    """资金共振评分 — 多源资金流确认

    交叉验证 jvQuant 实时资金流 + Tushare moneyflow 中单流向:
      - 两源同向净流入 → 加分
      - 单源流入 → 基础分
      - 均流出 → 扣分

    fundflow_data: 可选传入的实时资金流数据（来自 pipeline 的 _get_realtime_fund_cache）
    """
    reasons = []
    score = 0

    # ── 源1: 实时主力净流入（优先 fundflow_data，降级实时查询） ──
    rt_net_flow = 0
    rt_amount = 0

    if fundflow_data and isinstance(fundflow_data, dict):
        code_short = code.split(".")[0]
        rt = fundflow_data.get(code_short, {})
        rt_net_flow = _safe_float(rt.get("net_flow", 0))
        rt_amount = _safe_float(rt.get("amount", 0))
    else:
        # 降级: 尝试从实时缓存获取
        try:
            from plays.limit_up.pipeline import _get_realtime_fund_cache
            cache = _get_realtime_fund_cache()
            code_short = code.split(".")[0]
            rt = cache.get(code_short, {})
            rt_net_flow = _safe_float(rt.get("net_flow", 0))
            rt_amount = _safe_float(rt.get("amount", 0))
        except Exception:
            pass

    # ── 源2: Tushare moneyflow 中单流向 ──
    mf_md_net = 0  # 中单净流入
    mf_total_net = 0  # 主力净流入
    if moneyflow_tushare:
        buy_md = _safe_float(moneyflow_tushare.get("buy_md_amount", 0))
        sell_md = _safe_float(moneyflow_tushare.get("sell_md_amount", 0))
        mf_md_net = buy_md - sell_md  # 中单净流入（万元）
        mf_total_net = _safe_float(moneyflow_tushare.get("net_mf_amount", 0))

    # ── 共振判断 ──
    rt_positive = rt_net_flow > 0
    mf_positive = mf_md_net > 0 or mf_total_net > 0

    if rt_positive and mf_positive:
        # 双源共振: 实时主力净流入 + Tushare 中单/主力净流入 > 0
        score += 60
        # 进一步: 如果占比健康 > 3%，加分
        if rt_amount > 0 and rt_net_flow / rt_amount > 0.03:
            score += 20
            reasons.append(f"双源共振(主力占比{rt_net_flow/rt_amount*100:.1f}%)+80")
        else:
            reasons.append(f"双源共振(实时流入{rt_net_flow/1e4:.0f}万+Tushare流入)+60")
    elif rt_positive:
        # 仅实时流入
        score += 35
        if rt_amount > 0 and rt_net_flow / rt_amount > 0.03:
            score += 15
            reasons.append(f"实时主力流入(占比{rt_net_flow/rt_amount*100:.1f}%)+50")
        else:
            reasons.append(f"实时主力流入({rt_net_flow/1e4:.0f}万)+35")
    elif mf_positive:
        # 仅 Tushare 中单/主力流入
        score += 25
        if mf_md_net > 0:
            reasons.append(f"中单净流入({mf_md_net:.0f}万)+25")
        else:
            reasons.append(f"主力净流入({mf_total_net:.0f}万)+25")
    elif rt_net_flow < 0 and mf_md_net < 0:
        # 双源流出 → 扣分
        score -= 15
        reasons.append(f"双源流出(实时{rt_net_flow/1e4:.0f}万+Tushare{mf_md_net:.0f}万)-15")
    elif rt_net_flow < 0:
        score -= 5
        reasons.append(f"实时主力流出({rt_net_flow/1e4:.0f}万)-5")
    elif mf_md_net < 0:
        score -= 5
        reasons.append(f"中单流出({mf_md_net:.0f}万)-5")
    else:
        reasons.append("无资金流数据")
        score += 10  # 无数据时给中性基础分

    total = max(0, min(score, 100))
    reason_str = "; ".join(reasons) if reasons else "无数据"
    return total, f"[共振] {reason_str}"


# ── 6. 集合竞价 (bonus, 最高+3分) ─────────────────────


def _score_auction(code: str, em_data: dict) -> tuple:
    """集合竞价  — 简化版

    仅检查竞价活跃度，不做复杂 gap 分析（已由 sentiment 覆盖）。
    基准: 量比 > 2 → 竞价有量 → 活跃确认
    """
    reasons = []
    score = 0

    if not em_data:
        return 0, ""

    vol_ratio = _safe_float(em_data.get("vol_ratio", 0))

    # 竞价活跃度简化检查
    if vol_ratio > 3:
        score += 3
        reasons.append(f"竞价极活跃(量比{vol_ratio:.1f})+3")
    elif vol_ratio > 2:
        score += 2
        reasons.append(f"竞价活跃(量比{vol_ratio:.1f})+2")
    elif vol_ratio > 1.5:
        score += 1
        reasons.append(f"竞价有量(量比{vol_ratio:.1f})+1")

    reason_str = "; ".join(reasons) if reasons else ""
    return min(score, 3), f"[竞价] {reason_str}"


# ── 综合评分入口 ──────────────────────────────────────


def score_shortterm(code: str, fundflow_data: dict = None) -> tuple:
    """短线博弈面综合评分  (0-100)

    Args:
        code: 股票代码，如 "000001.SZ"
        fundflow_data: 可选，实时资金流数据 dict {code_short: {net_flow, amount, ...}}
                       如果不传，内部自动获取

    Returns:
        (score: int|float, reason: str)
    """
    # ── 1. 统一获取基础数据 ──
    daily_data = _get_daily_data(code)
    limit_history = _get_limit_history(code)
    daily_basic = _get_daily_basic(code)
    em_data = _get_realtime_quote(code)
    today_ul_row = _get_today_limit_list_row(code)
    moneyflow = _get_moneyflow(code)

    # ── 2. 子评分 ──
    momentum_s, momentum_r = _score_momentum(code, daily_data, limit_history)
    agg_s, agg_r = _score_aggression(code, daily_data)
    seal_s, seal_r = _score_seal_quality(code, today_ul_row, em_data)
    open_s, open_r = _score_open_battle(code, daily_data, daily_basic, em_data)
    resonance_s, resonance_r = _score_fund_resonance(code, moneyflow, fundflow_data)
    auction_s, auction_r = _score_auction(code, em_data)

    # ── 3. 加权汇总 ──
    weights = {
        "momentum": 0.40,
        "aggression": 0.20,
        "seal": 0.05,
        "open": 0.15,
        "resonance": 0.15,
    }

    total = (
        momentum_s * weights["momentum"]
        + agg_s * weights["aggression"]
        + seal_s * weights["seal"]
        + open_s * weights["open"]
        + resonance_s * weights["resonance"]
    )

    # 集合竞价 bonus: 直接加原始分（最高+3）
    total += auction_s

    parts = [momentum_r, agg_r, seal_r, open_r, resonance_r]
    if auction_r:
        parts.append(auction_r)
    reason = " | ".join(parts)

    return round(total, 1), reason


# ── 自测 ──────────────────────────────────────────────

if __name__ == "__main__":
    codes = sys.argv[1:] if len(sys.argv) > 1 else ["603319.SH", "000001.SZ"]
    for code in codes:
        s, r = score_shortterm(code)
        print(f"\n{code}: {s}分")
        for part in r.split(" | "):
            print(f"  {part}")
