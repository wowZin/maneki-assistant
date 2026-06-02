#!/usr/bin/env python3
"""Tushare API 封装：配置加载、缓存查询、行业映射"""

import json
import logging
import sys
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))


def load_env():
    env_file = PROJECT_DIR / ".env"
    config = {}
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                config[key] = value
    return config


CONFIG = load_env()

TUSHARE_TOKEN = CONFIG.get("TUSHARE_TOKEN", "")

# ===== Tushare API 缓存层 =====
_TUSHARE_CACHE = {}

# ===== 交易日自动修正：盘前/非交易日自动使用上一个有数据的交易日 =====
_LAST_TRADE_DATE_CACHE = None

def get_last_trade_date_with_data() -> str:
    """获取最近一个有日线数据的交易日
    盘前(9:30前)、盘后数据未就绪时自动回退到上一个交易日
    """
    global _LAST_TRADE_DATE_CACHE
    if _LAST_TRADE_DATE_CACHE:
        return _LAST_TRADE_DATE_CACHE
    
    from datetime import datetime, timedelta
    now = datetime.now()
    today = now.strftime("%Y%m%d")
    
    # 找最近交易日（从今天往前，最多10天）
    candidates = []
    for offset in range(10):
        d = now - timedelta(days=offset) if offset > 0 else now
        ds = d.strftime("%Y%m%d")
        payload = {"api_name": "trade_cal", "token": TUSHARE_TOKEN,
                   "params": {"exchange": "SSE", "start_date": ds, "end_date": ds},
                   "fields": "cal_date,is_open"}
        try:
            resp = requests.post("https://api.tushare.pro", json=payload, timeout=5).json()
            items = resp.get("data", {}).get("items", [])
            for item in items:
                if item and len(item) >= 2 and item[1] == 1:
                    candidates.append(ds)
                    break
        except Exception:
            pass
    
    # 从最近的交易日开始，验证 daily 数据是否存在
    for ds in candidates:
        payload = {"api_name": "daily", "token": TUSHARE_TOKEN,
                   "params": {"trade_date": ds, "ts_code": "000001.SZ"},
                   "fields": "trade_date,pct_chg"}
        try:
            resp = requests.post("https://api.tushare.pro", json=payload, timeout=5).json()
            if resp.get("data", {}).get("items"):
                _LAST_TRADE_DATE_CACHE = ds
                return ds
        except Exception:
            pass
    
    result = candidates[0] if candidates else today
    _LAST_TRADE_DATE_CACHE = result
    return result


def call_tushare(api_name, params, fields="", timeout=10):
    """带缓存的Tushare API调用，自动修正交易日参数"""
    # 自动修正 trade_date/start_date/end_date 参数
    # 如果查询的是今天但数据未就绪，自动降级到上一个可用交易日
    from datetime import datetime
    today = datetime.now().strftime("%Y%m%d")
    # 将 params 中所有 =today 的日期参数统一修正为最近可用交易日
    resolved = get_last_trade_date_with_data()
    if resolved != today:
        needs_copy = any(key in params and params[key] == today
                         for key in ("trade_date", "start_date", "end_date"))
        if needs_copy:
            params = dict(params)  # 仅在有需要时 copy，避免无谓开销
        for key in ("trade_date", "start_date", "end_date"):
            if key in params and params[key] == today:
                params[key] = resolved
    cache_key = (api_name, json.dumps(params, sort_keys=True), fields)
    if cache_key in _TUSHARE_CACHE:
        return _TUSHARE_CACHE[cache_key]
    try:
        payload = {"api_name": api_name, "token": TUSHARE_TOKEN, "params": params}
        if fields:
            payload["fields"] = fields
        resp = requests.post("https://api.tushare.pro", json=payload, timeout=timeout)
        result = resp.json()
        if result is None:
            result = {}  # API 返回 null (如 top_list 无数据)
        _TUSHARE_CACHE[cache_key] = result
        return result
    except Exception as e:
        logger.warning("Tushare %s 失败 (不缓存): %s", api_name, e)
        return {}


def clear_tushare_cache():
    global _TUSHARE_CACHE, _LAST_TRADE_DATE_CACHE
    _TUSHARE_CACHE = {}
    _LAST_TRADE_DATE_CACHE = None


# ===== stock_basic 行业映射缓存 =====
_INDUSTRY_MAP = {}
_INDUSTRY_PEERS = {}


def _ensure_industry_map():
    global _INDUSTRY_MAP, _INDUSTRY_PEERS
    if _INDUSTRY_MAP:
        return
    try:
        resp = call_tushare("stock_basic", {"list_status": "L"}, "ts_code,industry")
        items = resp.get("data", {}).get("items", [])
        for item in items:
            if len(item) >= 2:
                code, ind = item[0], (item[1] or '')
                _INDUSTRY_MAP[code] = ind
                if ind:
                    _INDUSTRY_PEERS.setdefault(ind, []).append(code)
        print(f"  行业映射缓存: {len(_INDUSTRY_MAP)}只股票, {len(_INDUSTRY_PEERS)}个行业")
    except Exception as e:
        print(f"  行业映射加载失败: {e}")


def get_industry(code):
    _ensure_industry_map()
    return _INDUSTRY_MAP.get(code, '')


def get_industry_peers(industry, limit=20):
    _ensure_industry_map()
    peers = _INDUSTRY_PEERS.get(industry, [])
    return peers[:limit]


def clear_industry_cache():
    global _INDUSTRY_MAP, _INDUSTRY_PEERS
    _INDUSTRY_MAP = {}
    _INDUSTRY_PEERS = {}