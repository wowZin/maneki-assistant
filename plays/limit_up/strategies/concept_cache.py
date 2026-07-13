#!/usr/bin/env python3
"""盘中实时概念热点缓存 — 替代 THS 热门榜概念标签。

数据流：
  概念映射: 从本地 parquet + JSON 加载（静态，零网络请求）
  实时涨停: 调用同花顺涨幅榜 API（分页，直到涨幅 < 9.9%），覆盖全市场
  刷新策略: refresh_concept_limit_ups() 每轮评分前调用一次

概念映射更新（概念很少变动）:
    python plays/limit_up/backtest/concept_cache.py build-members
"""

from __future__ import annotations

import os
import re
import threading
from pathlib import Path

import requests

_CONCEPT_DIR = Path(__file__).resolve().parent.parent.parent.parent / "wiki" / "raw" / "limit-up" / "panel" / "concept"
_MEMBERS_FILE = _CONCEPT_DIR / "concept_members.parquet"
_NAMES_FILE = _CONCEPT_DIR / "concept_names.json"
_GAINERS_URL = "https://data.10jqka.com.cn/market/zdfph/field/zdf/order/desc/ajax/{page}/"

_LOCK = threading.RLock()
_STOCK_CONCEPTS: dict[str, list[str]] = {}       # {short_code: [concept_name, ...]}
_CONCEPT_LIMIT_UPS: dict[str, int] = {}          # {concept_name: limit_up_count}
_LIMIT_CODES: set[str] = set()                   # 本轮的涨停股短码
_LOADED = False
_REFRESHED = False


def _load_cookie() -> str:
    """从 .env 加载同花顺 Cookie。"""
    env_file = Path(__file__).resolve().parent.parent.parent.parent / ".env"
    if not env_file.exists():
        return ""
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line.startswith("THS_COOKIE="):
            return line.split("=", 1)[1]
    return ""


def _fetch_limit_up_codes() -> set[str]:
    """调用同花顺涨幅榜 API（分页），返回全市场涨停股短码集合。"""
    cookie = _load_cookie()
    if not cookie:
        print("[concept_cache] 无 THS Cookie")
        return set()

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Cookie": cookie,
    }
    s = requests.Session()
    s.headers.update(headers)
    # 先访问主页面建立 session
    s.get("https://data.10jqka.com.cn/market/zdfph/", timeout=10)

    limit_codes: set[str] = set()
    page = 1

    while True:
        url = _GAINERS_URL.format(page=page)
        try:
            r = s.get(url, timeout=10)
            r.encoding = "gbk"
            if r.status_code != 200:
                break
            rows = re.findall(r"<tr[^>]*>(.*?)</tr>", r.text, re.DOTALL)
            page_codes: list[str] = []
            for row in rows:
                cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.DOTALL)
                if len(cells) < 5:
                    continue
                code = re.sub(r"<[^>]+>", "", cells[1]).strip()
                pct_str = re.sub(r"<[^>]+>", "", cells[4]).strip()
                try:
                    pct = float(pct_str)
                except ValueError:
                    continue
                if code and pct >= 9.5:
                    page_codes.append(code)
            if not page_codes:
                break
            limit_codes.update(page_codes)
            # 判断是否翻页：最后一只 > 9.9
            last_cells = re.findall(r"<td[^>]*>(.*?)</td>", rows[-1], re.DOTALL)
            if len(last_cells) >= 5:
                lp = re.sub(r"<[^>]+>", "", last_cells[4]).strip()
                try:
                    if float(lp) < 9.9:
                        break
                except ValueError:
                    break
            page += 1
        except Exception as e:
            print(f"[concept_cache] 第{page}页失败: {e}")
            break
    return limit_codes


def ensure_loaded():
    """从本地 parquet + JSON 加载概念映射（零网络请求）。"""
    global _LOADED, _STOCK_CONCEPTS
    if _LOADED:
        return
    with _LOCK:
        if _LOADED:
            return
        if not _MEMBERS_FILE.exists() or not _NAMES_FILE.exists():
            print("[concept_cache] 概念映射文件不完整，跳过")
            _LOADED = True
            return
        try:
            import json
            import pandas as pd
            cpt_names: dict[str, str] = json.loads(_NAMES_FILE.read_text())
            df = pd.read_parquet(_MEMBERS_FILE)
            for _, row in df.iterrows():
                code = str(row.get("stock_code", ""))
                cpt = str(row.get("cpt_code", ""))
                name = cpt_names.get(cpt)
                if code and name:
                    _STOCK_CONCEPTS.setdefault(code, []).append(name)
            _LOADED = True
            print(f"[concept_cache] 已加载 {len(_STOCK_CONCEPTS)} 只, "
                  f"{len(cpt_names)} 概念, {sum(len(v) for v in _STOCK_CONCEPTS.values())} 条")
        except Exception as e:
            print(f"[concept_cache] 加载失败: {e}")
            _LOADED = True


def refresh_concept_limit_ups(pool_codes: list[str] | None = None):
    """每轮评分前调用：调用涨幅榜 API，刷新概念涨停计数。"""
    global _CONCEPT_LIMIT_UPS, _REFRESHED
    ensure_loaded()
    limit_codes = _fetch_limit_up_codes()
    if not limit_codes:
        return
    with _LOCK:
        _CONCEPT_LIMIT_UPS = {}
        for short in limit_codes:
            for cname in _STOCK_CONCEPTS.get(short, []):
                _CONCEPT_LIMIT_UPS[cname] = _CONCEPT_LIMIT_UPS.get(cname, 0) + 1
        _LIMIT_CODES.clear()
        _LIMIT_CODES.update(limit_codes)
        _REFRESHED = True


def get_concept_limit_ups(code: str) -> dict:
    """获取某只股票所属概念的实时涨停数。"""
    ensure_loaded()
    if not _REFRESHED:
        refresh_concept_limit_ups()
    short = code.replace(".SH", "").replace(".SZ", "")
    result: dict[str, int] = {}
    for cname in _STOCK_CONCEPTS.get(short, []):
        cnt = _CONCEPT_LIMIT_UPS.get(cname, 0)
        if cnt > 0:
            result[cname] = cnt
    result["_total_limit_ups"] = len(_LIMIT_CODES)
    result["_total_concepts"] = len(_CONCEPT_LIMIT_UPS)
    return result


def get_total_limit_up_count() -> int:
    if not _REFRESHED:
        refresh_concept_limit_ups()
    return len(_LIMIT_CODES)


def get_best_concept(code: str) -> tuple[str | None, int]:
    cu = get_concept_limit_ups(code)
    best_name = None
    best_cnt = 0
    for name, cnt in cu.items():
        if name.startswith("_"):
            continue
        if cnt > best_cnt:
            best_name = name
            best_cnt = cnt
    return best_name, best_cnt


def clear():
    global _CONCEPT_LIMIT_UPS, _REFRESHED
    with _LOCK:
        _CONCEPT_LIMIT_UPS = {}
        _REFRESHED = False
