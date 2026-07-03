#!/usr/bin/env python3
"""
涨停预测完整流程脚本
流程：异动扫描(同花顺直连) → 五维度评分 → 排序 → 飞书推送

用法:
  python plays/limit_up/pipeline.py                  # 完整流程(requests+代理)
  python plays/limit_up/pipeline.py --from-file=data/signals/xxx.json  # 从已有文件读取
"""

import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
import argparse
import requests
import numpy as np

# 项目根目录
PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
PLAY_DIR = Path(__file__).resolve().parent
DATA_DIR = PLAY_DIR / "data"
sys.path.insert(0, str(PROJECT_DIR))

from scripts.tu_share import CONFIG, clear_tushare_cache  # noqa: E402
from plays.limit_up.utils import is_trading_time, log_data_audit  # noqa: E402
from plays.limit_up.pit_features import build_pit_features  # noqa: E402

# ===== Feishu 测试模式 =====
FEISHU_TEST_MODE = CONFIG.get("FEISHU_TEST_MODE", "").lower() == "true"
def feishu_title_prefix():
    return "测试-" if FEISHU_TEST_MODE else ""

# ===== Agent 权重配置 =====
AGENT_WEIGHTS = {
    "fundamental": float(CONFIG.get("AGENT_WEIGHT_FUNDAMENTAL", "0.5")),
    "technical": float(CONFIG.get("AGENT_WEIGHT_TECHNICAL", "0.5")),
    "fundflow": float(CONFIG.get("AGENT_WEIGHT_FUND_FLOW", "1.5")),
    "sentiment": float(CONFIG.get("AGENT_WEIGHT_SENTIMENT", "1.0")),
    "shortterm": float(CONFIG.get("AGENT_WEIGHT_SHORTTERM", "0.5")),
}

# ===== 1. 扫描异动股 =====
def scan_surge():
    """通过同花顺热门榜获取候选股（Cookie直连，无代理依赖）

    数据源: dq.10jqka.com.cn 热门搜索榜 (100只)
    过滤: ST/新股/创业板/科创板，涨幅0%-9.5%(排除当日涨停)
    Returns: list[dict] - [{code, name, pct_chg}] 候选股列表，或None
    """
    if not is_trading_time():
        print(f"跳过扫描: 非交易时段 ({datetime.now().strftime('%H:%M')})")
        return None

    from scripts.ths_client import get_ths_client
    ths = get_ths_client()
    if not ths.has_cookie:
        print("扫描失败: 同花顺 Cookie 未配置")
        return None

    items = ths.get_hot_list()
    if not items:
        print("扫描失败: 热门榜无数据")
        return None

    candidates = []
    for s in items:
        code = s.get("code", "")
        name = s.get("name", "")
        pct = float(s.get("pct_chg", 0))

        # 过滤: ST/新股/创业板/科创板，涨幅0%-9.5%(排除当日涨停)
        if re.search(r"ST|\*ST|退|N", name or ""):
            continue
        if re.match(r"^(300|301|688|8|4|920)", code):
            continue
        if pct < 0 or pct >= 9.5:  # 放宽下限0%但排除当日已涨停(不可交易)
            continue
        if "." not in code:
            code = f"{code}.SH" if code.startswith("6") else f"{code}.SZ"
        candidates.append({"code": code, "name": name, "pct_chg": pct})

    if candidates:
        print(f"热门榜扫描: {len(items)}只 → {len(candidates)}只候选 (过滤ST/科创/创业板, 涨幅0-9.5%(排除当日涨停))")
    else:
        print(f"热门榜扫描: {len(items)}只 → 0只候选")
    return candidates

def load_from_file(filepath):
    """从已有文件加载信号"""
    path = Path(filepath)
    if not path.is_absolute():
        path = PROJECT_DIR / filepath

    with open(path) as f:
        raw = json.load(f)

    # 兼容两种格式：直接list 或 dict包含stocks
    if isinstance(raw, dict) and "stocks" in raw:
        stocks = raw["stocks"]
    elif isinstance(raw, list):
        stocks = raw
    else:
        print(f"无法解析信号文件格式: {type(raw)}")
        return None

    # 统一转换为标准格式 {code, name}
    candidates = []
    for s in stocks:
        code = s.get("代码") or s.get("code") or s.get("ts_code", "")
        name = s.get("名称") or s.get("name", "")
        # 补全ts_code格式 (002971 -> 002971.SZ, 603615 -> 603615.SH)
        if "." not in code:
            if code.startswith("6"):
                code = f"{code}.SH"
            else:
                code = f"{code}.SZ"
        candidates.append({"code": code, "name": name})

    print(f"从文件加载: {len(candidates)} 只候选股")
    return candidates

from plays.limit_up.filter import filter_candidates  # noqa: E402

# ===== 2. 基本面评分 =====
# Lazy import to avoid circular dependency
def score_fundamental(code):
    from plays.limit_up.strategies.fundamental import score_fundamental as _score_fundamental
    return _score_fundamental(code)

# ===== 3. 技术面评分 =====
def score_technical(code):
    from plays.limit_up.strategies.technical import score_technical as _score_technical
    return _score_technical(code)


# ===== 4. 资金面评分 =====
# 缓存当日资金流向数据（避免每次调用都重复请求）
_FUND_FLOW_CACHE = None
_FUND_FLOW_DATE = None

# jvQuant 数据客户端（用于盘后/回测资金流，避免 Tushare moneyflow 超限）
_JV_CLIENT = None

def _get_jv_client():
    """懒加载 jvQuant 数据客户端。"""
    global _JV_CLIENT
    if _JV_CLIENT is None:
        try:
            from scripts.jvquant_client import get_jvquant_client
            _JV_CLIENT = get_jvquant_client()
        except Exception as e:
            print(f"  [jvQuant] 数据客户端初始化失败: {e}")
            _JV_CLIENT = False
    return _JV_CLIENT if _JV_CLIENT is not False else None


def score_fundflow(code, trade_date: str | None = None):
    from plays.limit_up.strategies.fundflow import score_fundflow as _score_fundflow
    jv = _get_jv_client()
    return _score_fundflow(code, trade_date=trade_date, jv_client=jv)

# 实时行情缓存（同花顺 Cookie 直连）
_REALTIME_PCT_CACHE = {}   # {code_short: pct_chg}  兼容旧接口
_THS_QUOTE_CACHE = {}       # {code_short: {...}}    完整同花顺行情
_REALTIME_PCT_TS = ""
_POPULARITY_RANK_CACHE: dict[str, int] = {}  # {code_short: rank} 同花顺热门榜排名
_HOT_CONCEPT_CACHE: dict[str, list] = {}    # {code_short: [concept_name, ...]}
_HOT_LIST_ITEMS: list[dict] = []            # 热门榜原始数据（含 pct_chg, tag 等）


def _batch_fetch_ths_for_candidates(candidates: list[dict]) -> dict[str, dict]:
    """用同花顺批量获取候选股实时行情（替代原全市场扫描）

    Args:
        candidates: [{code, name, pct_chg, ...}]

    Returns:
        {code_short: {price, pct_chg, turnover, vol_ratio, amount, ...}}
    """
    global _REALTIME_PCT_CACHE, _THS_QUOTE_CACHE, _REALTIME_PCT_TS
    from datetime import datetime as _dt
    today = _dt.now().strftime("%Y%m%d")

    if _REALTIME_PCT_TS == today and _THS_QUOTE_CACHE:
        return _THS_QUOTE_CACHE

    # 盘后降级：Tushare
    from plays.limit_up.utils import is_market_closed, batch_get_pct_tushare
    if is_market_closed():
        cache = batch_get_pct_tushare(today)
        if cache:
            _REALTIME_PCT_CACHE = cache
            _REALTIME_PCT_TS = today
            _THS_QUOTE_CACHE = {k: {"pct_chg": v} for k, v in cache.items()}
            print(f"  [盘后] 涨幅降级Tushare: {len(cache)} 只")
            return _THS_QUOTE_CACHE
        return {}

    # 盘中：同花顺逐只获取
    from scripts.ths_client import get_ths_client
    ths = get_ths_client()
    if not ths.has_cookie:
        print("  [同花顺] Cookie 未配置，跳过实时行情预取")
        return {}

    codes = []
    for c in candidates:
        short = c["code"].replace(".SH", "").replace(".SZ", "")
        if short not in _THS_QUOTE_CACHE:
            codes.append(short)

    if not codes:
        return _THS_QUOTE_CACHE

    print(f"  [同花顺] 获取 {len(codes)} 只实时行情...", end="", flush=True)
    results = ths.get_batch_quotes(codes)
    success = sum(1 for v in results.values() if v is not None)

    for code_short, quote in results.items():
        if quote:
            _THS_QUOTE_CACHE[code_short] = quote
            _REALTIME_PCT_CACHE[code_short] = quote.get("pct_chg", 0)

    _REALTIME_PCT_TS = today
    print(f" {success}/{len(codes)} 成功")
    return _THS_QUOTE_CACHE


def _batch_fetch_realtime_pct():
    """获取全市场实时涨跌幅缓存（兼容旧接口）

    盘中优先同花顺（已有缓存直接返回），盘后降级 Tushare。
    注意: 同花顺无全市场批量接口，缓存在 _batch_fetch_ths_for_candidates 中预填充。
    """
    global _REALTIME_PCT_CACHE, _REALTIME_PCT_TS
    from datetime import datetime as _dt
    today = _dt.now().strftime("%Y%m%d")
    if _REALTIME_PCT_TS == today and _REALTIME_PCT_CACHE:
        return _REALTIME_PCT_CACHE

    # 盘后降级
    from plays.limit_up.utils import is_market_closed, batch_get_pct_tushare
    if is_market_closed():
        cache = batch_get_pct_tushare(today)
        if cache:
            _REALTIME_PCT_CACHE = cache
            _REALTIME_PCT_TS = today
            print(f"  [盘后] 涨幅降级Tushare: {len(cache)} 只")
            return cache
        return {}

    # 盘中: 如果缓存为空（未预填充），返回空，策略层会自主处理
    print("  [同花顺] 涨幅缓存为空，请在评分前调用 _batch_fetch_ths_for_candidates")
    return _REALTIME_PCT_CACHE


def _fetch_ths_hot_list():
    """获取同花顺热门榜数据（替代原人气排名方式）

    填充 _POPULARITY_RANK_CACHE 和 _HOT_CONCEPT_CACHE。
    """
    global _POPULARITY_RANK_CACHE, _HOT_CONCEPT_CACHE, _HOT_LIST_ITEMS
    today = datetime.now().strftime("%Y%m%d")
    if _POPULARITY_RANK_CACHE and getattr(_fetch_ths_hot_list, '_date', '') == today:
        return

    from scripts.ths_client import get_ths_client
    ths = get_ths_client()
    if not ths.has_cookie:
        return

    items = ths.get_hot_list()
    if not items:
        print("  [热门榜] 获取失败")
        return

    _POPULARITY_RANK_CACHE.clear()
    _POPULARITY_RANK_CACHE.update({item["code"]: item.get("hot_rank", 0) for item in items})
    _HOT_CONCEPT_CACHE.clear()
    _HOT_CONCEPT_CACHE.update({
        item["code"]: item.get("tag", {}).get("concept_tag", [])
        for item in items if item.get("code")
    })
    _HOT_LIST_ITEMS.clear()
    _HOT_LIST_ITEMS.extend(items)
    _fetch_ths_hot_list._date = today
    zt = sum(1 for i in items if i.get('pct_chg', 0) >= 9.5)
    print(f"  [同花顺] 热门榜: {len(items)} 只, 涨停{zt}")


def _get_popularity_rank(code: str) -> int | None:
    """获取个股人气排名（同花顺热门榜，兼容旧接口）

    返回: 排名(1-based) 或 None(不在榜单)
    """
    if not _POPULARITY_RANK_CACHE:
        _fetch_ths_hot_list()
    code_short = code.split('.')[0]
    rank = _POPULARITY_RANK_CACHE.get(code_short)
    return rank if rank and rank > 0 else None


def _get_hot_concept_tags(code: str) -> list[str]:
    """获取个股概念标签（来自同花顺热门榜）"""
    if not _HOT_CONCEPT_CACHE:
        _fetch_ths_hot_list()
    code_short = code.split('.')[0]
    return _HOT_CONCEPT_CACHE.get(code_short, [])

# 实时资金流缓存（同花顺 + L2 + Tushare 三级降级）
_REALTIME_FUND_CACHE = {}  # code_short → {net_flow, vol_ratio, turnover, amount}
_REALTIME_FUND_TS = ""


def _get_realtime_fund_cache():
    """获取实时资金流缓存（兼容旧接口）

    数据来源优先级:
      换手率/量比 → 同花顺直连 (ths_client)
      主力净流入   → L2 逐笔统计 (l2_daemon_client)
      盘后兜底     → Tushare moneyflow + daily_basic

    返回: {code_short: {net_flow(元), vol_ratio, turnover(%), amount(元)}}
    """
    global _REALTIME_FUND_CACHE, _REALTIME_FUND_TS
    today = datetime.now().strftime("%Y%m%d")
    if _REALTIME_FUND_CACHE and _REALTIME_FUND_TS == today:
        return _REALTIME_FUND_CACHE

    # 盘后降级：Tushare
    from plays.limit_up.utils import is_market_closed, batch_get_fundflow_tushare
    if is_market_closed():
        cache = batch_get_fundflow_tushare(today)
        if cache:
            _REALTIME_FUND_CACHE = cache
            _REALTIME_FUND_TS = today
            print(f"  [盘后] 资金流降级Tushare: {len(cache)} 只")
            return cache
        return {}

    # 盘中：优先从同花顺缓存 + L2 构建资金流
    global _THS_QUOTE_CACHE
    cache = {}

    # 从同花顺缓存提取 turnover/vol_ratio/amount
    for code_short, quote in _THS_QUOTE_CACHE.items():
        if quote:
            entry = {
                "turnover": quote.get("turnover", 0),
                "vol_ratio": quote.get("vol_ratio", 0),
                "amount": quote.get("amount", 0),
                "net_flow": _get_l2_net_flow(code_short),  # L2 大单净流向
            }
            cache[code_short] = entry

    if cache:
        _REALTIME_FUND_CACHE = cache
        _REALTIME_FUND_TS = today
        l2_hits = sum(1 for v in cache.values() if v.get("net_flow", 0) != 0)
        print(f"  [同花顺+L2] 资金流缓存: {len(cache)} 只 (L2命中{l2_hits})")

    return cache


def _get_l2_net_flow(code_short: str) -> float:
    """从 L2 守护进程获取个股大单净流向（特大单+大单主动性买卖差值）"""
    try:
        from scripts.jvquant_ws_client import daemon_alive, daemon_cmd
        if not daemon_alive():
            return 0.0
        code = f"{code_short}.SH" if code_short.startswith("6") else f"{code_short}.SZ"
        resp = daemon_cmd(f"NETFLOW {code}")
        if resp and resp != "NULL":
            return float(resp)
    except Exception:
        pass
    return 0.0


def score_sentiment(code):
    from plays.limit_up.strategies.sentiment import score_sentiment as _score_sentiment
    return _score_sentiment(code)


# ===== 面板数据预取（供 total_score 使用）=====
# 缓存当日 Tushare daily 和 limit_list 数据（避免重复请求）
_NV2_DAILY_CACHE: dict[str, list[dict]] = {}  # code → [{trade_date, close, high, low, ...}]
_NV2_DAILY_BASIC_CACHE: dict[str, dict[str, dict]] = {}  # code → {trade_date: {pe, pb, circ_mv, ...}}
_NV2_LIMIT_CACHE: dict[str, int] = {}         # code → 近20日涨停次数
_NV2_LIMIT_60D_CACHE: dict[str, int] = {}     # code → 近60日涨停次数
_NV2_MONEYFLOW_CACHE: dict[str, dict[str, dict]] = {}  # code → {trade_date: moneyflow_row}
_NV2_DATE = ""


def _fetch_nv2_data(codes: list[str]):
    """批量拉取 total_score 所需的 Tushare 面板数据

    数据源:
      - daily: 前收盘价(pre_close) → 计算 trailing_10/5, position_20d, std10, max_pct_chg_5d
      - daily_basic: PE/PB/市值/换手/量比 → PIT 面板列
      - limit_list_d: 近60日涨停次数 → 涨停基因(20d/60d)

    注意:
      - daily 支持多 ts_code 批量
      - daily_basic 不支持多 ts_code，必须逐只查询
      - limit_list_d 支持多 ts_code 批量
    """
    global _NV2_DAILY_CACHE, _NV2_DAILY_BASIC_CACHE, _NV2_LIMIT_CACHE, _NV2_LIMIT_60D_CACHE, _NV2_MONEYFLOW_CACHE, _NV2_DATE
    today = datetime.now().strftime("%Y%m%d")
    if _NV2_DATE == today and _NV2_DAILY_CACHE and _NV2_DAILY_BASIC_CACHE:
        return

    from scripts.tu_share import call_tushare
    from datetime import timedelta
    from concurrent.futures import ThreadPoolExecutor, as_completed

    start70 = (datetime.now() - timedelta(days=70)).strftime("%Y%m%d")
    start60 = (datetime.now() - timedelta(days=60)).strftime("%Y%m%d")

    def _tushare_code_msg(resp: dict) -> str:
        code = resp.get("code")
        msg = resp.get("msg", "")
        if code is None:
            return "无响应"
        if code != 0:
            return f"错误码{code}: {msg}"
        return "ok"

    # 1. 日线数据 (近70天，满足60日涨停基因 + 20日波动/位置计算)
    ts_codes = [c for c in codes if c not in _NV2_DAILY_CACHE]
    if ts_codes:
        try:
            resp = call_tushare("daily", {
                "ts_code": ",".join(ts_codes),
                "start_date": start70, "end_date": today,
            }, "ts_code,trade_date,open,pre_close,close,high,low,vol,amount,pct_chg")
            items = resp.get("data", {}).get("items", [])
            flds = resp.get("data", {}).get("fields", [])
            for row in items:
                d = dict(zip(flds, row))
                code = d.get("ts_code", "")
                if code:
                    if code not in _NV2_DAILY_CACHE:
                        _NV2_DAILY_CACHE[code] = []
                    _NV2_DAILY_CACHE[code].append(d)
            print(f"  [NV2] daily: {len(items)}条, {len(ts_codes)}只 ({_tushare_code_msg(resp)})")
        except Exception as e:
            print(f"  [NV2] daily拉取失败: {e}")

    # 2. daily_basic (近70天，PIT 综合评分需要 pe/pb/circ_mv/turnover/volume_ratio 历史)
    # 注意：Tushare daily_basic API 不支持逗号分隔的批量 ts_code，需逐只并行查询
    ts_codes_db = [c for c in codes if c not in _NV2_DAILY_BASIC_CACHE]
    if ts_codes_db:
        total_items = 0
        failed_codes = []

        def _fetch_one_daily_basic(code: str) -> tuple[str, list]:
            try:
                resp = call_tushare("daily_basic", {
                    "ts_code": code,
                    "start_date": start70, "end_date": today,
                }, "ts_code,trade_date,pe,pb,circ_mv,turnover_rate,volume_ratio")
                if resp.get("code", -1) != 0:
                    return code, []
                items = resp.get("data", {}).get("items", [])
                return code, items
            except Exception:
                return code, []

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(_fetch_one_daily_basic, code): code for code in ts_codes_db}
            for future in as_completed(futures):
                code, items = future.result()
                if not items:
                    failed_codes.append(code)
                    continue
                if code not in _NV2_DAILY_BASIC_CACHE:
                    _NV2_DAILY_BASIC_CACHE[code] = {}
                for row in items:
                    d = dict(zip(["ts_code", "trade_date", "pe", "pb", "circ_mv",
                                  "turnover_rate", "volume_ratio"], row))
                    trade_date = str(d.get("trade_date", ""))
                    if trade_date:
                        _NV2_DAILY_BASIC_CACHE[code][trade_date] = d
                total_items += len(items)

        fail_msg = f", 失败{len(failed_codes)}只" if failed_codes else ""
        print(f"  [NV2] daily_basic: {total_items}条, {len(ts_codes_db)}只 (逐只){fail_msg}")

        # 数据审计：主板 circ_mv 过小
        for code, by_date in _NV2_DAILY_BASIC_CACHE.items():
            if not by_date:
                continue
            latest = max(by_date.keys())
            row = by_date[latest]
            circ_mv = _safe_float(row.get("circ_mv"), 0.0)
            pure = code.split(".")[0]
            if 0 < circ_mv < 1000 and pure.startswith(("00", "60")):
                log_data_audit(
                    f"[pipeline] {code} circ_mv 异常小: {circ_mv}万元 (date={latest})"
                )

    # 3. moneyflow（近 25 个自然日，按日期全市场拉取，供模型资金流特征）
    if not _NV2_MONEYFLOW_CACHE:
        start20 = (datetime.now() - timedelta(days=25)).strftime("%Y%m%d")
        print(f"  [NV2] moneyflow: 拉取 {start20}~{today}")
        total_mf = 0
        for offset in range(26):
            d = (datetime.now() - timedelta(days=offset)).strftime("%Y%m%d")
            try:
                resp = call_tushare(
                    "moneyflow",
                    {"trade_date": d},
                    "ts_code,trade_date,net_mf_amount,buy_elg_amount,sell_elg_amount,"
                    "buy_lg_amount,sell_lg_amount",
                )
                if resp.get("code", -1) != 0:
                    continue
                items = resp.get("data", {}).get("items", [])
                fields = resp.get("data", {}).get("fields", [])
                for row in items:
                    drow = dict(zip(fields, row))
                    code = drow.get("ts_code", "")
                    td = str(drow.get("trade_date", ""))
                    if code and td:
                        if code not in _NV2_MONEYFLOW_CACHE:
                            _NV2_MONEYFLOW_CACHE[code] = {}
                        _NV2_MONEYFLOW_CACHE[code][td] = drow
                total_mf += len(items)
            except Exception as e:
                print(f"  [NV2] moneyflow {d} 拉取失败: {e}")
        print(f"  [NV2] moneyflow: 共 {total_mf} 条")

    # 4. 涨停基因 (近60日，同时产出 20d/60d 计数)
    ts_codes_l = [c for c in codes if c not in _NV2_LIMIT_CACHE]
    if ts_codes_l:
        try:
            resp = call_tushare("limit_list_d", {
                "ts_code": ",".join(ts_codes_l),
                "start_date": start60, "end_date": today,
                "limit_type": "U",
            }, "ts_code,trade_date")
            items = resp.get("data", {}).get("items", [])
            flds = resp.get("data", {}).get("fields", [])
            from collections import defaultdict
            dates_by_code: dict[str, list[str]] = defaultdict(list)
            for row in items:
                d = dict(zip(flds, row))
                code = d.get("ts_code", "")
                trade_date = str(d.get("trade_date", ""))
                if code and trade_date:
                    dates_by_code[code].append(trade_date)
            cutoff20 = (datetime.now() - timedelta(days=20)).strftime("%Y%m%d")
            for code in ts_codes_l:
                dates = sorted(dates_by_code.get(code, []))
                count20 = sum(1 for d in dates if d >= cutoff20)
                count60 = len(dates)
                _NV2_LIMIT_CACHE[code] = count20
                _NV2_LIMIT_60D_CACHE[code] = count60
            print(f"  [NV2] limit_list: {len(items)}条涨停记录 ({_tushare_code_msg(resp)})")
        except Exception as e:
            print(f"  [NV2] limit_list拉取失败: {e}")
            for code in ts_codes_l:
                _NV2_LIMIT_CACHE.setdefault(code, 0)
                _NV2_LIMIT_60D_CACHE.setdefault(code, 0)

    # 同步到 strategies/factor_ctx，供策略打分使用
    try:
        from plays.limit_up.strategies import factor_ctx
        for code in codes:
            factor_ctx.set_daily(code, _NV2_DAILY_CACHE.get(code, []))
            factor_ctx.set_daily_basic(code, _NV2_DAILY_BASIC_CACHE.get(code, {}))
            factor_ctx.set_limit_counts(
                code,
                _NV2_LIMIT_CACHE.get(code, 0),
                _NV2_LIMIT_60D_CACHE.get(code, 0),
            )
        # 概念数据按需加载一次
        if factor_ctx._CONCEPT_DAILY_CACHE is None:
            cache_dir = Path(__file__).resolve().parent / "backtest" / "cache"
            factor_ctx.load_concept_data_from_cache(cache_dir)
    except Exception as e:
        print(f"  [NV2] factor_ctx 同步失败: {e}")

    _NV2_DATE = today


def _send_jvquant_error(msg: str):
    """jvQuant 异常时发送飞书报警"""
    print(f"  [jvQuant] ⚠️ {msg}")
    try:
        import requests
        token_resp = requests.post(
            "https://open.feishu.cn/open-apis/v3/tenant_access_token/internal",
            json={"app_id": CONFIG["FEISHU_APP_ID"],
                  "app_secret": CONFIG["FEISHU_APP_SECRET"]},
            timeout=10)
        token = token_resp.json().get("tenant_access_token", "")
        if token:
            alert = (f"⚠️ jvQuant 行情异常\n"
                     f"错误: {msg}\n"
                     f"时间: {datetime.now().strftime('%H:%M:%S')}\n"
                     f"影响: 本轮无实时盘口数据，评分继续")
            requests.post(
                "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
                headers={"Authorization": f"Bearer {token}"},
                json={"receive_id": CONFIG.get("FEISHU_CHAT_ID_SIGNAL", ""),
                      "msg_type": "text",
                      "content": json.dumps({"text": alert})},
                timeout=10)
    except Exception:
        pass


def _safe_float(val, default: float = 0.0) -> float:
    if val is None:
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def _extract_pit_features(code: str, pit_mode: bool) -> dict:
    """从 _NV2_*_CACHE 提取 PIT 面板行，供 total_score 与 factors/ 使用。"""
    daily_rows = _NV2_DAILY_CACHE.get(code, [])
    basic_by_date = _NV2_DAILY_BASIC_CACHE.get(code, {})
    moneyflow_by_date = _NV2_MONEYFLOW_CACHE.get(code, {})
    code_short = code.split(".")[0]
    try:
        from plays.limit_up.strategies import factor_ctx
        concept_momentum = factor_ctx.get_concept_momentum(code_short)
    except Exception:
        concept_momentum = None
    # 复用统一 PIT 特征构建器，保持生产与回测特征口径一致
    feats = build_pit_features(
        code=code,
        score_date=_NV2_DATE,
        daily_rows=daily_rows,
        basic_by_date=basic_by_date,
        moneyflow_by_date=moneyflow_by_date,
        auction_by_date=None,
        concept_momentum=concept_momentum,
        pit_mode=pit_mode,
    )
    # 兼容旧逻辑：生产 pipeline 的 limit_up_count 来自 limit_list_d 缓存，精度更高
    feats["limit_up_count_20d"] = float(_NV2_LIMIT_CACHE.get(code, 0))
    feats["limit_up_count_60d"] = float(_NV2_LIMIT_60D_CACHE.get(code, 0))
    return feats


# ===== 6. 飞书推送 =====
def _get_feishu_token():
    """获取飞书 tenant_access_token"""
    import requests
    resp = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={
            "app_id": CONFIG["FEISHU_APP_ID"],
            "app_secret": CONFIG["FEISHU_APP_SECRET"]
        }
    )
    data = resp.json()
    return data.get("tenant_access_token")


def push_feishu(results):
    """飞书推送：按 total_score 降序取满足阈值的前 3 只。

    模型模式（LIMIT_UP_USE_MODEL=true）下默认阈值 70；
    quality_combo 模式下默认阈值 95。
    """
    import requests

    model_mode = os.environ.get("LIMIT_UP_USE_MODEL", "").lower() in ("true", "1", "yes")
    default_thr = "70" if model_mode else "95"
    threshold = float(CONFIG.get("ULTIMATE_PUSH_THRESHOLD", default_thr))

    def _stars(total):
        if total >= 90: return "⭐⭐⭐⭐⭐"
        if total >= 80: return "⭐⭐⭐⭐"
        if total >= 70: return "⭐⭐⭐"
        if total >= 60: return "⭐⭐"
        return ""

    sorted_results = sorted(results, key=lambda r: r.get("total_score", 0), reverse=True)
    if not sorted_results:
        print("  无可推送股票")
        return False

    max_ts = sorted_results[0].get("total_score", 0)
    if max_ts < threshold:
        print(f"  最高 total_score={max_ts:.1f} 低于阈值 {threshold}，不推送")
        return False

    push_list = [r for r in sorted_results if r.get("total_score", 0) >= threshold][:3]

    print(f"  阈值 {threshold}: max_ts={max_ts:.1f} → {len(push_list)}只推送")
    if not push_list:
        print("  无可推送股票")
        return False

    pushed_dir = DATA_DIR / "pushed"
    pushed_dir.mkdir(parents=True, exist_ok=True)
    pushed_file = pushed_dir / f"{datetime.now().strftime('%Y%m%d_%H%M')}.json"
    with open(pushed_file, "w") as f:
        json.dump(push_list, f, ensure_ascii=False, indent=2)
    print(f"  推送记录已保存: {pushed_file}")

    token = _get_feishu_token()
    if not token:
        print("飞书token获取失败")
        return False

    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"{feishu_title_prefix()}涨停预测 ({datetime.now().strftime('%H:%M')})"},
            "template": "blue",
        },
        "elements": [],
    }

    for r in push_list:
        s = r.get("scores", {})
        ts = r.get("total_score", 0)
        stars = _stars(ts)
        pct = r.get("pct_chg", 0)
        card["elements"].append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": (
                    f"**{r['code']} {r['name']}** {stars} 涨幅{pct:.1f}%\n"
                    f"总分:{ts:.0f} 情绪:{s.get('sentiment',0):.0f} 资金:{s.get('fundflow',0):.0f} 短线:{s.get('shortterm',0):.0f}"
                ),
            },
        })

    resp = requests.post(
        "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "receive_id": CONFIG["FEISHU_CHAT_ID_SIGNAL"],
            "msg_type": "interactive",
            "content": json.dumps(card),
        },
    )
    return True


def _write_empty_result(reason=""):
    """写入零结果分析文件（兜底：避免扫空静默失败）"""
    now = datetime.now()
    ts = now.strftime("%Y%m%d_%H%M")
    output_path = DATA_DIR / "analysis" / f"{ts}.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    empty = [{"_empty": True, "reason": reason, "time": now.isoformat()}]
    with open(output_path, "w") as f:
        json.dump(empty, f, ensure_ascii=False)
    print(f"零结果已记录: {output_path}")


def _score_one(stock: dict, l2_available: bool, weights: dict,
               scored_cache: dict | None = None, cache_hit: bool = False) -> dict:
    """单只股票评分"""
    code = stock["code"]
    name = stock["name"]

    if cache_hit and scored_cache and code in scored_cache:
        cached = scored_cache[code]
        # cached: {dim: (score, reason), ...} → reconstruct full result
        f_sc = cached.get("fundamental", (0, ""))[0]
        t_sc = cached.get("technical", (0, ""))[0]
        m_sc = cached.get("fundflow", (0, ""))[0]
        s_sc = cached.get("sentiment", (0, ""))[0]
        st_sc = cached.get("shortterm", (0, ""))[0]
        dc = [(f_sc, weights.get("fundamental", 1.0)), (t_sc, weights.get("technical", 1.0)),
              (m_sc, weights.get("fundflow", 1.0)), (s_sc, weights.get("sentiment", 1.0)),
              (st_sc, weights.get("shortterm", 1.5))]
        dc.sort(key=lambda x: x[0] * x[1], reverse=True)
        top3 = dc[:3]
        total = sum(s * w for s, w in top3) / sum(w for _, w in top3) if sum(w for _, w in top3) > 0 else 0
        rc = sum([f_sc >= 75, t_sc >= 75, m_sc >= 75, s_sc >= 75, st_sc >= 75])
        return {"code": code, "name": name,
                "scores": {"fundamental": f_sc, "technical": t_sc, "fundflow": m_sc,
                           "sentiment": s_sc, "shortterm": st_sc},
                "reasons": {"fundamental": cached.get("fundamental", ("", ""))[1],
                            "technical": cached.get("technical", ("", ""))[1],
                            "fundflow": cached.get("fundflow", ("", ""))[1],
                            "sentiment": cached.get("sentiment", ("", ""))[1],
                            "shortterm": cached.get("shortterm", ("", ""))[1]},
                "total": total, "top3_score": round(total, 1),
                "resonance": {"count": rc, "threshold": 75, "is_resonance": rc >= 3},
                "pct_chg": round(stock.get("pct_chg", 0), 1), "l2api": None}

    # 并行五维度评分
    funcs: dict[str, Callable] = {}
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from typing import Callable
    try:
        from plays.limit_up.strategies.fundamental import score_fundamental
        funcs["fundamental"] = score_fundamental
    except ImportError:
        pass
    try:
        from plays.limit_up.strategies.technical import score_technical
        funcs["technical"] = score_technical
    except ImportError:
        pass
    try:
        from plays.limit_up.strategies.fundflow import score_fundflow
        funcs["fundflow"] = score_fundflow
    except ImportError:
        pass
    try:
        from plays.limit_up.strategies.sentiment import score_sentiment
        funcs["sentiment"] = score_sentiment
    except ImportError:
        pass
    try:
        from plays.limit_up.strategies.shortterm import score_shortterm
        funcs["shortterm"] = score_shortterm
    except ImportError:
        pass

    scores: dict[str, int | float] = {}
    reasons: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(fn, code): name for name, fn in funcs.items()}
        for future in as_completed(futures):
            dim = futures[future]
            try:
                s, r = future.result()
                scores[dim] = s
                reasons[dim] = r
            except Exception as e:
                scores[dim] = 0
                reasons[dim] = f"评分异常: {e}"

    f_sc, t_sc = scores.get("fundamental", 0), scores.get("technical", 0)
    m_sc, s_sc = scores.get("fundflow", 0), scores.get("sentiment", 0)
    st_sc = scores.get("shortterm", 0)

    dc = [(f_sc, weights.get("fundamental", 1.0)), (t_sc, weights.get("technical", 1.0)),
          (m_sc, weights.get("fundflow", 1.0)), (s_sc, weights.get("sentiment", 1.0)),
          (st_sc, weights.get("shortterm", 1.5))]
    dc.sort(key=lambda x: x[0] * x[1], reverse=True)
    top3 = dc[:3]
    total = sum(s*w for s, w in top3) / sum(w for _, w in top3) if sum(w for _, w in top3) > 0 else 0
    rc = sum([f_sc >= 75, t_sc >= 75, m_sc >= 75, s_sc >= 75, st_sc >= 75])

    l2data = None
    if l2_available:
        try:
            from scripts.jvquant_ws_client import daemon_get_market, daemon_get_vwap, daemon_get_kline
            mkt = daemon_get_market(code)
            vwap = daemon_get_vwap(code)
            kb = daemon_get_kline(code, n=5)
            if mkt:
                last_val = float(mkt.get("last", 0))
                bid_prices = mkt.get("bid_price", [])
                ask_prices = mkt.get("ask_price", [])
                l2data = {"last": last_val,
                          "bid1_p": float(bid_prices[0]) if bid_prices and bid_prices[0] else 0,
                          "ask1_p": float(ask_prices[0]) if ask_prices and ask_prices[0] else 0,
                          "vwap": round(vwap, 2) if vwap else None,
                          "kline_bars": len(kb)}
        except Exception:
            pass

    return {"code": code, "name": stock["name"],
            "scores": {"fundamental": f_sc, "technical": t_sc, "fundflow": m_sc,
                       "sentiment": s_sc, "shortterm": st_sc},
            "reasons": {"fundamental": reasons.get("fundamental", ""), "technical": reasons.get("technical", ""),
                        "fundflow": reasons.get("fundflow", ""), "sentiment": reasons.get("sentiment", ""),
                        "shortterm": reasons.get("shortterm", "")},
            "weights": {k: f"{v:.1f}" for k, v in weights.items()},
            "total": total, "resonance": {"count": rc, "threshold": 75, "is_resonance": rc >= 3},
            "top3_score": round(total, 1), "pct_chg": round(stock.get("pct_chg", 0), 1),
            "l2api": l2data}


def _pre_rank(candidates, top_n=50):
    """涨停相关性预排：涨速 + 涨幅 + 人气排名"""
    pop_cache = _POPULARITY_RANK_CACHE
    scored = []
    for stock in candidates:
        surge = stock.get("surge", 0)
        pct = stock.get("pct_chg", 0)
        short = stock["code"].split(".")[0]
        score = 0
        if surge >= 5: score += 3  # noqa: E701
        elif surge >= 3: score += 2  # noqa: E701
        elif surge >= 2: score += 1  # noqa: E701
        if pct >= 7: score += 3  # noqa: E701
        elif pct >= 5: score += 2  # noqa: E701
        elif pct >= 3: score += 1  # noqa: E701
        rank = pop_cache.get(short)
        if rank is not None:
            if rank <= 100: score += 4  # noqa: E701
            elif rank <= 200: score += 3  # noqa: E701
            elif rank <= 300: score += 2  # noqa: E701
            elif rank <= 500: score += 1  # noqa: E701
        scored.append((score, stock))
    scored.sort(key=lambda x: x[0], reverse=True)
    ranked = [s for _, s in scored[:top_n]]
    print(f"[预排] {len(candidates)}只 -> Top{len(ranked)} (涨速+涨幅+人气)")
    for i, (sc, st) in enumerate(scored[:10]):
        print(f"  {i+1}. {st['code']} {st['name']} 分{sc} (涨速{st.get('surge',0):.1f} 涨幅{st.get('pct_chg',0):.1f})")
    return ranked


def main():
    parser = argparse.ArgumentParser(description="涨停预测流程")
    parser.add_argument("--from-file", help="从已有信号文件加载", default=None)
    parser.add_argument("--top", type=int, default=50, help="分析前N只股票（默认50）")
    parser.add_argument("--no-l2", action="store_true", help="跳过L2初始化（用于开盘早期无L2扫描）")
    args = parser.parse_args()

    # ── 进程锁：防止多个 pipeline 实例同时跑导致 L2 互踢 ──
    lock_file = DATA_DIR / "pipeline.lock"
    if lock_file.exists():
        try:
            old_pid = int(lock_file.read_text().strip())
            # 检查旧进程是否还在运行
            os.kill(old_pid, 0)
            print(f"跳过: 另一个 pipeline 实例正在运行 (PID={old_pid})")
            return
        except (OSError, ValueError):
            # 旧进程已死，清理过期锁
            lock_file.unlink(missing_ok=True)
    lock_file.write_text(str(os.getpid()))

    def _release_lock():
        try: lock_file.unlink(missing_ok=True)
        except: pass

    try:
        _run_pipeline(args)
    finally:
        _release_lock()


def _run_pipeline(args):
    clear_tushare_cache()
    from scripts.audit import reset; reset()

    from scripts.health_check import preflight_check, _send_alert_sync  # noqa: E402
    if not preflight_check():
        print("[预检] 关键数据源异常，阻塞执行。详情见 data/health_state.json")
        _write_empty_result("预检阻断: 关键数据源不可用")
        return

    print("=" * 50)
    print(f"涨停预测流程启动: {datetime.now()}")
    print("=" * 50)

    # 1. 获取候选股
    if args.from_file:
        candidates = load_from_file(args.from_file)
    else:
        print("\n[1/5] 异动扫描(同花顺直连)...")
        candidates = scan_surge()

    if not candidates:
        print("无候选股，退出")
        _write_empty_result("扫描无候选股")
        return

    # 1.5 过滤
    print("\n[1.5/5] 全系统过滤...")
    candidates = filter_candidates(candidates)
    if not candidates:
        print("过滤后无候选股，退出")
        _write_empty_result("过滤后无候选股")
        return

    weights = dict(AGENT_WEIGHTS)

    # 今日缓存：先 wiki/raw/limit-up/analysis，再 data/analysis
    today_str = datetime.now().strftime("%Y%m%d")
    scored_cache = {}
    for base in (
        PROJECT_DIR / "wiki" / "raw" / "limit-up" / "analysis",
        DATA_DIR / "analysis",
    ):
        if not base.exists():
            continue
        for f in sorted(base.glob(f"{today_str}*.json")):
            try:
                items = json.loads(f.read_text())
                if isinstance(items, list):
                    for item in items:
                        if "code" in item and "scores" in item:
                            scored_cache[item["code"]] = {
                                dim: (item["scores"][dim], item.get("reasons", {}).get(dim, ""))
                                for dim in item["scores"]
                            }
            except Exception:
                pass
    print(f"  scored_cache: {len(scored_cache)} 只缓存" if scored_cache else "  scored_cache: 无缓存")

    # 1.6 jvQuant WS 检查
    l2_available = False
    if not args.no_l2:
        try:
            from scripts.jvquant_ws_client import daemon_alive, daemon_stats
            if daemon_alive():
                l2_available = True
                s = daemon_stats()
                print(f"  [jvQuant WS] 已连接 | 今日{s['total_subscribed_today']}只={s['daily_cost']}元")
            else:
                _send_jvquant_error("jvQuant WebSocket 连接失败")
        except ImportError as e:
            _send_jvquant_error(f"jvQuant 模块未安装: {e}")
        except Exception as e:
            _send_jvquant_error(f"jvQuant 初始化异常: {e}")
    else:
        print("  [行情] 已跳过 (--no-l2 模式)")

    # 1.7 预排 + 概念
    print("\n[1.7/5] 涨停相关性预排...")
    _fetch_ths_hot_list()
    from scripts.tu_share import build_concept_map
    build_concept_map(_HOT_CONCEPT_CACHE)
    candidates = _pre_rank(candidates, top_n=args.top)

    # 1.8 THS 实时行情
    print("\n[1.8/5] 同花顺实时行情预取...")
    _batch_fetch_ths_for_candidates(candidates)

    # 1.9 预取 total_score 所需的历史面板数据
    print("\n[1.9/5] Tushare 面板数据预取（daily/daily_basic/limit_list_d）...")
    _fetch_nv2_data([c["code"] for c in candidates])

    # 2. 深度评分
    if l2_available:
        print(f"\n[2/5] 深度分析: {len(candidates)}只 (jvQuant 分层订阅, 无等待)")
        from scripts.jvquant_ws_client import subscribe_tiered
        subscribe_tiered(candidates, top_n_l1=min(15, len(candidates)),
                         top_n_l10=min(6, len(candidates)),
                         top_n_l2=min(2, len(candidates)))
        time.sleep(3)
    else:
        print(f"\n[2/5] 深度分析: {len(candidates)}只 (无实时行情)")

    all_results = []
    for stock in candidates:
        code = stock["code"]
        cache_hit = code in scored_cache
        tag = "[缓存]" if cache_hit else "评分中"
        print(f"  {code} {stock['name']} {tag}")
        try:
            r = _score_one(stock, l2_available, weights, scored_cache, cache_hit)
            all_results.append(r)
        except Exception as e:
            print(f"  {code} 评分失败: {e}")

    # Top-3 升 L2 深度
    all_results.sort(key=lambda x: x.get("total", 0), reverse=True)
    if l2_available:
        top_codes = [r["code"] for r in all_results[:3] if r.get("total", 0) >= 35]
        if top_codes:
            from scripts.jvquant_ws_client import daemon_subscribe_l2
            daemon_subscribe_l2(top_codes)
            time.sleep(2)
            for r in all_results[:3]:
                if r["code"] in top_codes:
                    try:
                        from scripts.jvquant_ws_client import daemon_get_market, daemon_get_vwap, daemon_get_kline
                        mkt = daemon_get_market(r["code"])
                        vwap = daemon_get_vwap(r["code"])
                        kb = daemon_get_kline(r["code"], n=5)
                        if mkt:
                            r["l2api"] = {"last": float(mkt.get("last", 0)),
                                          "vwap": round(vwap, 2) if vwap else None,
                                          "kline_bars": len(kb)}
                    except Exception:
                        pass
        from scripts.jvquant_ws_client import daemon_unsubscribe as jv_unsub
        all_codes = [c["code"] for c in candidates]
        jv_unsub(all_codes)

    # 3. 唯一总分：total_score(row) - PIT 语义（使用 T-1 数据）
    from plays.limit_up.total import total_score
    from plays.limit_up.factors import REGISTRY, TOTAL_SCORE_COMPONENTS
    for r in all_results:
        code = r["code"]
        feats = _extract_pit_features(code, pit_mode=True)
        feats["sentiment"] = r["scores"].get("sentiment", 0)
        feats["shortterm"] = r["scores"].get("shortterm", 0)
        feats["technical"] = r["scores"].get("technical", 0)
        feats["fundflow"] = r["scores"].get("fundflow", 0)
        feats["fundamental"] = r["scores"].get("fundamental", 0)
        # 记录 total_score 与三个组件因子（供 review/审计）
        r["factors"] = {
            name: round(REGISTRY[name](feats), 2)
            for name in TOTAL_SCORE_COMPONENTS
        }
        r["total_score"] = total_score(feats)

    # 4. 推送
    push_feishu(all_results)

    # 5. 排序 + 落盘
    all_results.sort(key=lambda x: x.get("total_score", 0), reverse=True)
    print("\n[排序结果]")
    for i, r in enumerate(all_results, 1):
        tag = " [共振]" if r.get("resonance", {}).get("is_resonance") else ""
        print(f"  {i}. {r['code']} {r['name']} - total_score:{r.get('total_score', 0):.1f}{tag}")

    output_dir = DATA_DIR / "analysis"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / f"{datetime.now().strftime('%Y%m%d_%H%M')}.json"
    with open(output_file, "w") as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2)
    print(f"\n结果已保存: {output_file}")

    from scripts.audit import summary, dump
    print("\n" + summary())
    logs_dir = DATA_DIR / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    audit_log = logs_dir / f"audit_{today_str}.log"
    try:
        dump(audit_log)
        print(f"审计日志: {audit_log}")
    except Exception as e:
        print(f"审计日志写入失败（audit.dump 可能未实现）: {e}")

    print("\n" + "=" * 50)
    print("流程完成!")
    print("=" * 50)
    reset()


if __name__ == "__main__":
    main()
