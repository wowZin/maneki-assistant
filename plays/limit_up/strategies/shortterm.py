#!/usr/bin/env python3
"""短线博弈面评分 — 打板专用

因子权重:
  1. 封板质量 25% — 封板时间、封单流通比、撤单强度
  2. 连板动量 25% — 连板数、涨停基因、断板反包
  3. 开盘博弈 15% — 换手率、开盘形态、量比
  4. 板块助攻 15% — 概念精确匹配+别名映射、板块热度
  5. 攻击独特性 20% — 涨停高开率(>2%)、近10日涨幅、弱转强
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_DIR))

import requests  # noqa: E402 (used in _get_jj_data_eastmoney)

# ── 全局跨股票缓存（减少 Tushare 调用） ──────────────────────
_LIMIT_UP_CACHE = None        # 今日全市场涨停列表
_LIMIT_UP_CACHE_DATE = None
_CONCEPT_CACHE = None         # 今日概念涨停统计
_CONCEPT_CACHE_DATE = None
_CONCEPT_ALIAS_MAP = {        # 概念别名映射（Tushare → 同花顺/东财常见名）
    "锂电池":          ["锂电", "锂电池", "锂电概念"],
    "新能源汽车":      ["新能源车", "汽车", "新能源汽车", "新能源整车"],
    "芯片":            ["芯片", "半导体", "集成电路", "IC设计"],
    "光伏":            ["光伏", "太阳能", "光伏组件"],
    "人工智能":        ["AI", "人工智能", "智能", "AIGC"],
    "国企改革":        ["国企改革", "央企改革", "国资改革"],
    "军工":            ["军工", "国防", "航天"],
    "华为概念":        ["华为", "华为产业链", "华为概念"],
    "信创":            ["信创", "信息技术创新", "国产软件"],
    "数字经济":        ["数字经济", "数字中国", "数据要素"],
    "机器人":          ["机器人", "人形机器人", "工业机器人"],
    "低空经济":        ["低空经济", "飞行汽车", "eVTOL"],
    "CPO":             ["CPO", "光模块", "光通信"],
    "算力":            ["算力", "算力租赁", "数据中心"],
    "储能":            ["储能", "储能电池", "电力储能"],
    "消费电子":        ["消费电子", "电子", "AI手机"],
    "医药生物":        ["医药", "生物医药", "创新药"],
    "大消费":          ["消费", "大消费", "食品饮料"],
    "房地产":          ["地产", "房地产", "房地产开发"],
    "金融科技":        ["金融科技", "数字货币", "区块链"],
}


# ── 工具函数 ─────────────────────────────────────────────

def _today() -> str:
    """今日日历日期（始终返回真实今天），用于日期比较判断"""
    return datetime.now().strftime("%Y%m%d")


def _query_date() -> str:
    """Tushare 查询用日期：返回最近一个有数据的交易日，
    盘前/盘中自动退到 T-1，盘后退到今天（若数据已就绪）。
    仅用于构造 Tushare API 的 trade_date/start_date/end_date 参数。"""
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


# ── 统一数据获取（带缓存） ──────────────────────────────

def _to_df(api_name: str, params: dict, fields: str = ""):
    """调 call_tushare 返回 DataFrame（保持下游兼容）"""
    from scripts.tu_share import call_tushare
    result = call_tushare(api_name, params, fields)
    items = result.get("data", {}).get("items", [])
    cols = result.get("data", {}).get("fields", [])
    if not items or not cols:
        return None
    import pandas as pd
    return pd.DataFrame(items, columns=cols)


def _get_today_limit_ups():
    """全市场涨停列表（全局缓存，只调一次）
    注：盘中 limit_list_d 仅有 T-1 数据，当日涨停需结合东财实时行情判断。
    """
    global _LIMIT_UP_CACHE, _LIMIT_UP_CACHE_DATE
    today = _query_date()
    if _LIMIT_UP_CACHE is not None and _LIMIT_UP_CACHE_DATE == today:
        return _LIMIT_UP_CACHE
    try:
        df = _to_df("limit_list_d", {"trade_date": today, "limit_type": "U"})
        _LIMIT_UP_CACHE = df
        _LIMIT_UP_CACHE_DATE = today
    except Exception:
        _LIMIT_UP_CACHE = None
    return _LIMIT_UP_CACHE


def _get_today_limit_ups_set():
    """今日涨停股代码集合"""
    df = _get_today_limit_ups()
    if df is not None and not df.empty:
        return set(df["ts_code"].tolist())
    return set()


def _get_concept_limit_stats():
    """概念涨停统计（全局缓存，只调一次）
    注：盘中 limit_cpt_list 仅有 T-1 数据。
    """
    global _CONCEPT_CACHE, _CONCEPT_CACHE_DATE
    today = _query_date()
    if _CONCEPT_CACHE is not None and _CONCEPT_CACHE_DATE == today:
        return _CONCEPT_CACHE
    try:
        from scripts.tu_share import call_tushare
        result = call_tushare("limit_cpt_list", {"trade_date": today}, "ts_code,name,trade_date,up_nums")
        items = result.get("data", {}).get("items", [])
        fields = result.get("data", {}).get("fields", [])
        result_map = {}
        if items and fields:
            for item in items:
                d = dict(zip(fields, item))
                name = d.get("name", "")
                nums = _safe_int(d.get("up_nums", 0))
                if name and nums > 0:
                    result_map[name] = nums
        _CONCEPT_CACHE = result_map
        _CONCEPT_CACHE_DATE = today
    except Exception:
        _CONCEPT_CACHE = {}
    return _CONCEPT_CACHE


def _get_concept_names(code: str) -> list:
    """获取个股所属概念名称列表"""
    try:
        from scripts.tu_share import call_tushare
        result = call_tushare("concept_detail", {"ts_code": code}, "id,concept_name")
        items = result.get("data", {}).get("items", [])
        return [c[1] for c in items if len(c) > 1] if items else []
    except Exception:
        return []


def _get_limit_history(code: str, days=30) -> object:
    """获取个股历史涨停记录（最近 N 天）"""
    start = (datetime.now() - timedelta(days=days * 2)).strftime("%Y%m%d")
    end = _query_date()
    try:
        return _to_df("limit_list_d", {"ts_code": code, "start_date": start, "end_date": end, "limit_type": "U"})
    except Exception:
        return None


def _get_daily_data(code: str, days=30) -> object:
    """获取个股日线数据（最近 N 个自然日, 按 trade_date 降序）"""
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
    """获取个股基础面数据（流通股本、换手率、量比等）
    注：盘中 daily_basic 仅有 T-1 数据，实时换手/量比请用 _get_jj_data_eastmoney()。
    """
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


def _get_jj_data_eastmoney(code: str) -> dict:
    """通过东方财富实时行情获取集合竞价/盘口数据

    返回: {change_pct, open_pct, amount, vol_ratio, turnover_rate,
           circ_mv, now_price, jj_active}
    盘中优先用 clist 缓存(字段映射已验证正确)，降级 stock/get API。
    """
    from plays.limit_up.utils import is_market_closed, get_stock_quote

    # 盘后降级：使用 Tushare 数据
    if is_market_closed():
        q = get_stock_quote(code)
        if q.get("change_pct") is not None:
            t = q.get("turnover_rate", 0)
            if t > 1:
                q["turnover_rate"] = t / 100
            return q
        return {}

    result = {}
    short = code.split('.')[0]

    # ═══ 第一优先：clist 缓存（字段映射已验证正确） ═══
    try:
        from plays.limit_up.pipeline import _get_realtime_fund_cache, _batch_fetch_realtime_pct
        pct = _batch_fetch_realtime_pct().get(short)
        fund = _get_realtime_fund_cache().get(short, {})

        if pct is not None:
            try:
                pct_f = float(pct)
                if abs(pct_f) < 30:  # 异常值防护(东财偶发-192%)
                    result["change_pct"] = pct_f
            except (ValueError, TypeError):
                pass

        if fund:
            result["amount"] = fund.get("amount", 0)
            result["vol_ratio"] = fund.get("vol_ratio", 0)
            rt = fund.get("turnover", 0)  # 百分比(5.32=5.32%)
            if rt:
                result["turnover_rate"] = rt / 100  # → 小数(0.0532)
    except Exception:
        pass

    # ═══ 第二优先：stock/get API 补充缓存缺失字段 ═══
    need_stock = (
        "change_pct" not in result
        or "now_price" not in result
        or "circ_mv" not in result
    )

    if need_stock:
        try:
            from scripts.proxy_utils import request_with_proxy_retry

            if code.endswith(".SH"):
                em_code = f"1.{code.replace('.SH','')}"
            elif code.endswith(".SZ"):
                em_code = f"0.{code.replace('.SZ','')}"
            else:
                em_code = code

            url = (
                f"https://push2.eastmoney.com/api/qt/stock/get"
                f"?secid={em_code}&fields=f43,f44,f45,f46,f47,f48,f49,f50,f51,f52,f57,f58,f60,f116,f117,f162,f166,f167,f168,f169,f170,f171"
            )
            headers = {
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://quote.eastmoney.com/",
            }
            resp = request_with_proxy_retry(url, timeout=10, headers=headers)
            if resp is None:
                return result
            data = resp.json()
            d = data.get("data", {})
            if not d:
                return result

            # 已验证: f60=昨收×100, f170=涨跌幅×100
            # f166 含义已变(不再=换手率×100), 换手率改用成交量÷流通股本计算

            stock_now = _safe_float(d.get("f60", 0)) / 100
            if "now_price" not in result:
                result["now_price"] = stock_now

            if "change_pct" not in result:
                f170_raw = d.get("f170")
                if f170_raw is not None and f170_raw != "-":
                    try:
                        result["change_pct"] = float(f170_raw) / 100
                    except (ValueError, TypeError):
                        pass

            if "turnover_rate" not in result:
                # f166 字段已在EM API中变更含义(不再=换手率×100)
                # 改用成交量(f47,手) ÷ 流通股本(f116÷现价) 计算换手率
                vol_shou = _safe_float(d.get("f47", 0))
                circ_mv = _safe_float(d.get("f116", 0))
                price = _safe_float(d.get("f43", 0)) / 100
                if vol_shou > 0 and circ_mv > 0 and price > 0:
                    circ_shares = circ_mv / price
                    if circ_shares > 0:
                        result["turnover_rate"] = (vol_shou * 100) / circ_shares

            if "circ_mv" not in result:
                result["circ_mv"] = _safe_float(d.get("f116", 0))

            # open_pct / jj_active 仅 stock/get 可提供(字段不可靠, 作参考)
            if "open_pct" not in result:
                stock_open = _safe_float(d.get("f46"))
                if stock_now > 0 and stock_open > 0 and stock_open < stock_now * 1500:
                    result["open_pct"] = (stock_open / 100 - stock_now) / stock_now * 100

            result["jj_active"] = (
                result.get("vol_ratio", 0) > 1.5
                and abs(result.get("open_pct", 0)) > 2
            )
        except Exception:
            pass

    return result


# ── 1. 封板质量 (25%) ─────────────────────────────────

def _score_seal_quality(code: str, cache: dict) -> tuple:
    """封板质量评分

    优化:
      - 封单流通比 替代 绝对封单金额
      - 数据来源: limit_list_d + daily_basic
    """
    reasons = []
    score = 0
    today_ul = cache.get("today_ul_data")  # limit_list_d 今日该股数据
    daily_basic = cache.get("daily_basic", {})
    circ_mv = _safe_float(cache.get("em_data", {}).get("circ_mv", 0))  # 东财流通市值
    free_share = _safe_float(daily_basic.get("free_share", 0))  # 万股
    close_price = _safe_float(daily_basic.get("close", 0))

    if today_ul is not None:
        row = today_ul

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

        # 封单流通比 (核心: 封单额 / 流通市值)
        first_amount = _safe_float(row.get("first_limit_amount", 0))
        seal_mv_ratio = 0
        if first_amount > 0 and circ_mv > 0:
            seal_mv_ratio = first_amount / circ_mv
        elif first_amount > 0 and free_share > 0 and close_price > 0:
            seal_mv_ratio = first_amount / (free_share * 10000 * close_price)

        if seal_mv_ratio > 0.15:  # 封单>15%流通市值，极强
            score += 50
            reasons.append(f"封单流通比{seal_mv_ratio:.1%}+50")
        elif seal_mv_ratio > 0.10:  # >10%，强势
            score += 35
            reasons.append(f"封单流通比{seal_mv_ratio:.1%}+35")
        elif seal_mv_ratio > 0.05:
            score += 25
            reasons.append(f"封单流通比{seal_mv_ratio:.1%}+25")
        elif seal_mv_ratio > 0.02:
            score += 15
            reasons.append(f"封单流通比{seal_mv_ratio:.1%}+15")
        elif first_amount > 0:
            score += 10
            reasons.append(f"封单流通比{seal_mv_ratio:.1%}+10")

        # 撤单强度 (终封/首封)
        last_amount = _safe_float(row.get("last_limit_amount", 0))
        if first_amount > 0 and last_amount > 0:
            seal_ratio = last_amount / first_amount
            if seal_ratio < 0.3:
                score -= 20
                reasons.append("大幅撤单-20")
            elif seal_ratio < 0.6:
                score -= 10
                reasons.append("部分撤单-10")
    else:
        # 今日未涨停 / 无数据
        hist_ul = cache.get("limit_history")
        if hist_ul is not None and not hist_ul.empty:
            reasons.append("今日未涨停(参考历史)")
            score += 10
        else:
            reasons.append("无涨停记录")

    total = min(score, 100)
    reason_str = "; ".join(reasons) if reasons else "无数据"
    return max(total, 0), f"[封板] {reason_str}"


# ── 2. 连板动量 (25%) ─────────────────────────────────

def _score_momentum(code: str, cache: dict) -> tuple:
    """连板动量评分"""
    reasons = []
    score = 0
    hist_ul = cache.get("limit_history")
    daily_data = cache.get("daily_data")

    if hist_ul is not None and not hist_ul.empty:
        # 检查最近一次涨停是否是今天（判断当前是否在连板中）
        latest_ul_date = str(hist_ul.iloc[0].get("trade_date", ""))
        is_current_limit_up = (latest_ul_date == _today())

        # 连板数 — 只有今天还在涨停中才计当前连板
        limit_times = _safe_float(hist_ul.iloc[0].get("limit_times", 0))
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
            # 断板日：给一个基因分，但不像连板那么多
            reasons.append(f"断板(历史{int(limit_times)}连板)")
            score += 5

        # 涨停基因：近30天涨停次数
        recent_count = len(hist_ul)
        if recent_count >= 5:
            score += 20
            reasons.append(f"近月{recent_count}次涨停+20")
        elif recent_count >= 3:
            score += 10
            reasons.append(f"近月{recent_count}次涨停+10")

        # 断板反包
        if len(hist_ul) >= 2:
            dates = hist_ul["trade_date"].tolist()
            gaps = []
            for i in range(1, len(dates)):
                from datetime import datetime as _dt
                d1 = _dt.strptime(str(dates[i - 1]), "%Y%m%d")
                d2 = _dt.strptime(str(dates[i]), "%Y%m%d")
                gap = (d1 - d2).days
                if gap > 1 and gap < 10:
                    gaps.append(gap)
            if gaps:
                score += 15
                reasons.append("断板反包+15")
    else:
        reasons.append("近期无涨停")

    # 涨幅活跃度 (近5日日均涨幅)
    if daily_data is not None and not daily_data.empty:
        recent_days = daily_data.head(min(5, len(daily_data)))
        avg_pct = recent_days["pct_chg"].mean()
        if avg_pct > 3:
            score += 15
            reasons.append(f"近5日活跃(均涨{avg_pct:.1f}%)+15")
        elif avg_pct > 1:
            score += 5
            reasons.append(f"近5日温和(均涨{avg_pct:.1f}%)+5")

    total = min(score, 100)
    reason_str = "; ".join(reasons) if reasons else "无数据"
    return max(total, 0), f"[连板] {reason_str}"


# ── 3. 开盘博弈 (15%) ─────────────────────────────────

def _score_open_battle(code: str, cache: dict) -> tuple:
    """开盘博弈评分

    优化:
      - 用换手率/量比 替代 绝对成交额
      - 保留开盘形态评分
    """
    reasons = []
    score = 0
    daily_data = cache.get("daily_data")
    daily_basic = cache.get("daily_basic", {})
    em_data = cache.get("em_data", {})

    # 获取今日日线
    today_row = None
    if daily_data is not None and not daily_data.empty:
        # daily_data 按 trade_date 降序排列, iloc[0] 是最近交易日
        first_date = str(daily_data.iloc[0].get("trade_date", ""))
        if first_date == _today():
            today_row = daily_data.iloc[0]
        else:
            reasons.append(f"今日无数据(最近{first_date})")

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

            # 分歧转一致（开盘跌但最终涨停）
            if open_pct < 3 and high >= pre_close * 1.098:
                score += 20
                reasons.append("分歧转一致+20")

    # 换手率/量比评分（优先 em_data 实时数据，降级 daily_basic T+1）
    # 注意：em_data 换手率为小数(0.0532=5.32%)，daily_basic 为百分数(5.32=5.32%)
    em_turnover = _safe_float(em_data.get("turnover_rate", 0))
    em_vol_ratio = _safe_float(em_data.get("vol_ratio", 0))

    if em_data and (em_turnover > 0 or em_vol_ratio > 0):
        # 有实时数据：em_data 换手率小数→百分数统一
        turnover_rate = em_turnover * 100
        vol_ratio = em_vol_ratio
    else:
        # 降级 T+1 daily_basic（已为百分数格式）
        turnover_rate = _safe_float(daily_basic.get("turnover_rate", 0))
        vol_ratio = _safe_float(daily_basic.get("volume_ratio", 0))

    # 换手率: 打板接力通常 10%-30% 为佳
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

    # 量比评分
    if vol_ratio > 3:
        score += 15
        reasons.append(f"放量(量比{vol_ratio:.1f})+15")
    elif vol_ratio > 2:
        score += 10
        reasons.append(f"量比{vol_ratio:.1f}+10")
    elif vol_ratio > 1.5:
        score += 5
        reasons.append(f"温和放量(量比{vol_ratio:.1f})+5")

    if today_row is None and not reasons:
        reasons.append("暂无今日数据")

    total = min(score, 100)
    reason_str = "; ".join(reasons) if reasons else "无数据"
    return max(total, 0), f"[开盘] {reason_str}"


# ── 4. 板块助攻 (15%) ─────────────────────────────────

def _match_concept_ul_cnt(cname: str, concept_ul_cnt: dict) -> int:
    """精确匹配概念涨停数（支持别名映射，不再用模糊匹配）

    防止 "汽车" → "新能源汽车" 的误匹配
    """
    # 1. 精确匹配
    if cname in concept_ul_cnt:
        return concept_ul_cnt[cname]

    # 2. 别名映射: 如果 cname 是某个 key 的别名, 用该 key 查
    for main_name, aliases in _CONCEPT_ALIAS_MAP.items():
        if cname == main_name:
            # 检查别名是否在 concept_ul_cnt 中
            for alias in aliases:
                if alias in concept_ul_cnt:
                    return concept_ul_cnt[alias]
            break
        if cname in aliases:
            # cname 是别名, 用主名查
            if main_name in concept_ul_cnt:
                return concept_ul_cnt[main_name]
            for alias in aliases:
                if alias != cname and alias in concept_ul_cnt:
                    return concept_ul_cnt[alias]
            break

    # 3. 中文字符串包含匹配 (仅限 >=2 个字符的精确子串, 避免 "车"→"新能源汽车")
    for k, v in concept_ul_cnt.items():
        if len(cname) >= 3 and len(k) >= 3:
            if cname == k:
                return v
        # 仅当一方完整包含另一方且至少3个汉字
        if len(cname) >= 3 and len(k) >= 3:
            if cname in k or k in cname:
                return v

    return 0


def _score_sector(code: str, cache: dict) -> tuple:
    """板块助攻评分"""
    reasons = []
    score = 0

    concept_names = cache.get("concept_names", [])
    concept_ul_cnt = cache.get("concept_limit_stats", {})

    if not concept_names:
        reasons.append("无概念数据")
        return 10, "[板块] 无概念数据+10"

    if not concept_ul_cnt:
        reasons.append("无概念涨停统计")
        score += 10
        return max(min(score, 100), 0), f"[板块] {'; '.join(reasons)}"

    # 精确匹配所属概念中最大涨停数
    max_ul = max([_match_concept_ul_cnt(n, concept_ul_cnt) for n in concept_names], default=0)

    if max_ul >= 20:
        score += 60
        reasons.append(f"板块涨停潮(概念涨停{max_ul}只)+60")
    elif max_ul >= 10:
        score += 50
        reasons.append(f"板块爆发(概念涨停{max_ul}只)+50")
    elif max_ul >= 5:
        score += 35
        reasons.append(f"板块强联动({max_ul}只涨停)+35")
    elif max_ul >= 3:
        score += 25
        reasons.append(f"板块有热点({max_ul}只涨停)+25")
    elif max_ul >= 1:
        score += 15
        reasons.append(f"板块跟风({max_ul}只涨停)+15")
    else:
        reasons.append("所属概念无涨停")
        score += 5  # 有概念数据的基础分

    # 自身地位: 是否正在涨停（优先东财实时涨跌幅，降级 T+1 涨停列表）
    em_data = cache.get("em_data", {})
    if em_data:
        # 有行情数据：以涨跌幅为准（东财实时 or 盘后 Tushare）
        em_pct = _safe_float(em_data.get("change_pct", 0))
        is_limit_up_now = em_pct >= 9.9
    else:
        # 无行情数据：降级 T+1 涨停列表（API 全部异常时）
        today_limit_set = cache.get("today_limit_set", set())
        is_limit_up_now = code in today_limit_set

    if is_limit_up_now and max_ul > 0:
        score += 15
        reasons.append("自身涨停+15")
        # 板块龙头身位: 自身涨停 + 概念≥5只涨停 → 板块前排股
        if max_ul >= 5:
            score += 20
            reasons.append("板块前排+20")

    total = min(score, 100)
    reason_str = "; ".join(reasons) if reasons else "无数据"
    return max(total, 0), f"[板块] {reason_str}"


# ── 5. 攻击独特性 (20%) ───────────────────────────────

def _score_aggression(code: str, cache: dict) -> tuple:
    """攻击独特性评分

    优化:
      - 修复时间序列方向: 找涨停日后一个交易日应 pos-1 而非 pos+1
      - 高开阈值 0% → 2%
    """
    reasons = []
    score = 0
    daily_data = cache.get("daily_data")

    if daily_data is None or daily_data.empty:
        reasons.append("无近30日数据")
        return 0, "[攻击] 无近30日数据+0"

    # daily_data 按 trade_date 降序排列: [今天, 昨天, 前天, ...]
    recent = daily_data.head(20)

    # ① 近20日有过涨停 + 次日高开率>50%
    limit_up_dates = recent[recent["pct_chg"] >= 9.5]
    if not limit_up_dates.empty:
        high_open_count = 0
        for lu_idx in limit_up_dates.index:
            try:
                pos = recent.index.get_loc(lu_idx)
                if isinstance(pos, slice):
                    pos = pos.start
                # daily_data 降序排列: pos 小 = 日期新
                # 涨停日的下一个交易日是日期更小的行 = pos - 1
                if pos > 0:  # 有更早的行（即下一个交易日）
                    next_day = recent.iloc[pos - 1]
                    next_open = _safe_float(next_day.get("open", 0))
                    next_pre = _safe_float(next_day.get("pre_close", 0))
                    if next_pre > 0:
                        open_pct = (next_open / next_pre - 1) * 100
                        # 短线语境: 高开 > 2% 才算有效高开
                        if open_pct > 2:
                            high_open_count += 1
            except Exception:
                continue

        total_limit = len(limit_up_dates)
        if total_limit > 0 and high_open_count / total_limit > 0.5:
            score += 40
            reasons.append(f"涨停高开率{high_open_count}/{total_limit}>50%+40")

    # ② 近10日最大单日涨幅>7%
    max_pct = recent.head(10)["pct_chg"].max()
    if max_pct > 7:
        score += 35
        reasons.append(f"近10日最大涨幅{max_pct:.1f}%+35")

    # ③ 昨涨停 + 今弱转强
    if len(daily_data) >= 2:
        today_row = daily_data.iloc[0]
        yesterday_row = daily_data.iloc[1]
        today_date = str(today_row.get("trade_date", ""))
        yest_date = str(yesterday_row.get("trade_date", ""))
        # 必须确认 today_row 是今天的数据，防止周末/盘前把昨天当今天
        if today_date != _today():
            pass  # 无今日数据，跳过弱转强判断
        elif not yest_date or today_date <= yest_date:
            pass  # 日期顺序异常，跳过
        else:
            y_pct = _safe_float(yesterday_row.get("pct_chg", 0))
            t_open = _safe_float(today_row.get("open", 0))
            t_pre = _safe_float(today_row.get("pre_close", 0))
            t_pct = _safe_float(today_row.get("pct_chg", 0))

            if y_pct >= 9.5 and t_pre > 0:
                open_pct = (t_open / t_pre - 1) * 100
                if -2 <= open_pct <= 2 and t_pct > 4:
                    score += 25
                    reasons.append(f"弱转强(昨涨停今开{open_pct:.1f}%今{t_pct:.1f}%)+25")

    total = min(score, 100)
    reason_str = "; ".join(reasons) if reasons else "无数据"
    return max(total, 0), f"[攻击] {reason_str}"


# ── 6. 集合竞价 (新增, 融入攻击独特性) ───────────────

def _score_jj(code: str, cache: dict) -> tuple:
    """集合竞价因子评分

    通过东财实时API获取换手率/量比/开盘涨幅来估算竞价强度。
    理想竞价: 量比>2 + 开盘涨幅3-7% + 非一字板（有充分换手）

    加分合理范围: 0-15分
    """
    reasons = []
    score = 0
    em_data = cache.get("em_data", {})

    if not em_data:
        return 0, ""

    open_pct = _safe_float(em_data.get("open_pct", 0))
    vol_ratio = _safe_float(em_data.get("vol_ratio", 0))
    turnover_rate = _safe_float(em_data.get("turnover_rate", 0))

    # 检查是否有竞价数据 (open_pct > 0 说明有开盘数据)
    if open_pct == 0:
        return 0, ""

    # 竞价活跃度：量比+换手率复合
    if vol_ratio > 3 and 3 <= open_pct <= 8:
        score += 15
        reasons.append(f"竞价活跃(量比{vol_ratio:.1f}开{open_pct:.1f}%)+15")
    elif vol_ratio > 2 and 2 <= open_pct <= 8:
        score += 10
        reasons.append(f"竞价走强(量比{vol_ratio:.1f}开{open_pct:.1f}%)+10")
    elif vol_ratio > 1.5 or 2 <= open_pct <= 9.5:
        score += 5
        reasons.append(f"竞价正常(量比{vol_ratio:.1f}开{open_pct:.1f}%)+5")

    # 换手率竞价估算 (早盘换手 > 2% 说明竞价有量)
    if turnover_rate > 0.02:
        score += 5
        reasons.append(f"竞价换手{turnover_rate:.2%}+5")

    total = min(score, 20)
    reason_str = "; ".join(reasons) if reasons else ""
    return min(total, 15), f"[竞价] {reason_str}"


# ── 综合评分 ──────────────────────────────────────────

def score_shortterm(code: str) -> tuple:
    """短线博弈面综合评分（0-100）

    在入口函数统一获取所有数据，消除重复API调用。
    """
    # ── 1. 统一获取基础数据 ──
    cache = {}

    # 可全局缓存的（所有股票共享）
    cache["concept_limit_stats"] = _get_concept_limit_stats()
    cache["today_limit_set"] = _get_today_limit_ups_set()

    # 个股级数据（每只股票只调一次）
    cache["daily_data"] = _get_daily_data(code)
    cache["limit_history"] = _get_limit_history(code)
    cache["daily_basic"] = _get_daily_basic(code)
    cache["concept_names"] = _get_concept_names(code)
    cache["em_data"] = _get_jj_data_eastmoney(code)

    # 今日涨停数据 (从 limit_list_d 取个股数据)
    today_ul_df = _get_today_limit_ups()
    cache["today_ul_data"] = None
    if today_ul_df is not None and not today_ul_df.empty:
        matches = today_ul_df[today_ul_df["ts_code"] == code]
        if not matches.empty:
            cache["today_ul_data"] = matches.iloc[0]

    # ── 2. 子评分 ──
    seal_s, seal_r = _score_seal_quality(code, cache)
    momentum_s, momentum_r = _score_momentum(code, cache)
    open_s, open_r = _score_open_battle(code, cache)
    sector_s, sector_r = _score_sector(code, cache)
    agg_s, agg_r = _score_aggression(code, cache)
    jj_s, jj_r = _score_jj(code, cache)

    # ── 3. 加权汇总 ──
    weights = {
        "seal": 0.25,
        "momentum": 0.25,
        "open": 0.15,
        "sector": 0.15,
        "aggression": 0.20,
    }

    total = (
        seal_s * weights["seal"]
        + momentum_s * weights["momentum"]
        + open_s * weights["open"]
        + sector_s * weights["sector"]
        + agg_s * weights["aggression"]
    )

    # 集合竞价作为额外加分
    total += jj_s * 0.3  # 竞价因子, 最高加4.5分

    parts = [seal_r, momentum_r, open_r, sector_r, agg_r]
    if jj_r:
        parts.append(jj_r)
    reason = " | ".join(parts)

    return round(total, 1), reason


# ── 向后兼容接口（供旧测试/外部模块直接调用） ──────────

def score_aggression(code: str) -> tuple:
    """兼容旧接口: 单参数 → 内部构造 cache"""
    cache = {}
    cache["daily_data"] = _get_daily_data(code)
    cache["em_data"] = _get_jj_data_eastmoney(code)
    cache["limit_history"] = _get_limit_history(code)
    return _score_aggression(code, cache)


def score_seal_quality(code: str) -> tuple:
    cache = {}
    cache["today_ul_data"] = None
    today_ul_df = _get_today_limit_ups()
    if today_ul_df is not None and not today_ul_df.empty:
        matches = today_ul_df[today_ul_df["ts_code"] == code]
        if not matches.empty:
            cache["today_ul_data"] = matches.iloc[0]
    cache["limit_history"] = _get_limit_history(code)
    cache["daily_basic"] = _get_daily_basic(code)
    cache["em_data"] = _get_jj_data_eastmoney(code)
    return _score_seal_quality(code, cache)


def score_momentum(code: str) -> tuple:
    cache = {}
    cache["limit_history"] = _get_limit_history(code)
    cache["daily_data"] = _get_daily_data(code)
    return _score_momentum(code, cache)


def score_open_battle(code: str) -> tuple:
    cache = {}
    cache["daily_data"] = _get_daily_data(code)
    cache["daily_basic"] = _get_daily_basic(code)
    cache["em_data"] = _get_jj_data_eastmoney(code)
    return _score_open_battle(code, cache)


def score_sector(code: str) -> tuple:
    cache = {}
    cache["concept_names"] = _get_concept_names(code)
    cache["concept_limit_stats"] = _get_concept_limit_stats()
    cache["today_limit_set"] = _get_today_limit_ups_set()
    return _score_sector(code, cache)


# ── 自测 ──────────────────────────────────────────────

if __name__ == "__main__":
    codes = sys.argv[1:] if len(sys.argv) > 1 else ["603319.SH"]
    for code in codes:
        s, r = score_shortterm(code)
        print(f"\n{code}: {s}分")
        for part in r.split(" | "):
            print(f"  {part}")
