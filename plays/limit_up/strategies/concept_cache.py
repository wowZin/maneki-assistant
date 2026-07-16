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

import threading
from pathlib import Path

_CONCEPT_DIR = Path(__file__).resolve().parent.parent.parent.parent / "wiki" / "raw" / "limit-up" / "panel" / "concept"
_MEMBERS_FILE = _CONCEPT_DIR / "concept_members.parquet"
_NAMES_FILE = _CONCEPT_DIR / "concept_names.json"

_LOCK = threading.RLock()
_STOCK_CONCEPTS: dict[str, list[str]] = {}       # {short_code: [concept_name, ...]}
_CONCEPT_LIMIT_UPS: dict[str, int] = {}          # {concept_name: limit_up_count}
_LIMIT_CODES: set[str] = set()                   # 本轮的涨停股短码
_LOADED = False
_REFRESHED = False


def _fetch_limit_up_codes() -> set[str]:
    """用 THS SDK batch_quotes 扫描全市场,返回涨停股短码集合。"""
    try:
        from scripts.ths_client import get_ths_client as _ths
        import json
        from pathlib import Path

        # 读取候选池(1416只,开盘前已建好)
        pool_file = Path(__file__).resolve().parent.parent.parent.parent / "plays" / "limit_up" / "data" / "pool"
        pools = sorted(pool_file.glob(f"pool_*.json"))
        if not pools:
            print("[concept_cache] 无候选池")
            return set()
        pool = json.loads(pools[-1].read_text())
        codes = [s["code"] for s in pool if s.get("code")]

        ths = _ths()
        limit_codes: set[str] = set()
        for i in range(0, len(codes), 50):
            batch = codes[i:i+50]
            quotes = ths.get_batch_quotes(batch)
            for code, q in quotes.items():
                if q is None:
                    continue
                pct = float(q.get("pct_chg", 0) or 0)
                if pct >= 9.5:
                    limit_codes.add(code)
        return limit_codes
    except Exception as e:
        print(f"[concept_cache] 扫描失败: {e}")
        return set()


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
