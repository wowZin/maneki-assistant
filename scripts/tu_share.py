#!/usr/bin/env python3
"""Tushare API 封装：配置加载、缓存查询、行业映射"""

import json
import sys
from pathlib import Path

import requests

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


def call_tushare(api_name, params, fields="", timeout=10):
    """带缓存的Tushare API调用，避免同一股票重复请求同一接口"""
    cache_key = (api_name, json.dumps(params, sort_keys=True), fields)
    if cache_key in _TUSHARE_CACHE:
        return _TUSHARE_CACHE[cache_key]
    try:
        payload = {"api_name": api_name, "token": TUSHARE_TOKEN, "params": params}
        if fields:
            payload["fields"] = fields
        resp = requests.post("https://api.tushare.pro", json=payload, timeout=timeout)
        result = resp.json()
        _TUSHARE_CACHE[cache_key] = result
        return result
    except Exception:
        _TUSHARE_CACHE[cache_key] = {}
        return {}


def clear_tushare_cache():
    global _TUSHARE_CACHE
    _TUSHARE_CACHE = {}


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