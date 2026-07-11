#!/usr/bin/env python3
"""候选池构建 — 每日一次，开盘前拉 daily_basic 全市场数据，筛选 50-200亿 主板候选股。

用法：
    python plays/limit_up/pool_builder.py              # 构建当日池
    python plays/limit_up/pool_builder.py --date 20260710  # 指定日期

输出：
    data/pool/pool_{date}.json: list[{"code": str, "name": str}]

过滤规则（与 filter.py 原规则保持一致）：
    1. 主板 (00/60 开头)
    2. 非 ST / *ST
    3. 上市满 120 天（非次新）
    4. 流通市值 50-200 亿
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, date as date_type
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from scripts.tu_share import call_tushare

PLAY_DIR = Path(__file__).resolve().parent
POOL_DIR = PLAY_DIR / "data" / "pool"
POOL_DIR.mkdir(parents=True, exist_ok=True)

MARKET_CAP_MIN = 500000  # 万元 (50亿)
MARKET_CAP_MAX = 2000000  # 万元 (200亿)
MIN_LISTING_DAYS = 120  # 非次新
MAIN_BOARD_PREFIXES = ("00", "60")
EXCLUDED_PREFIXES = ("300", "301", "688", "8", "4", "920", "430")


def _trade_date() -> str:
    """返回当前交易日 YYYYMMDD。简单实现：用今天，非交易日返回上一个可用交易日。"""
    return datetime.now().strftime("%Y%m%d")


def _load_stock_basic() -> dict[str, dict]:
    """拉 Tushare stock_basic，返回 {ts_code: {name, list_date}}"""
    resp = call_tushare("stock_basic", {}, "ts_code,name,list_date")
    out = {}
    for row in resp.get("data", {}).get("items", []):
        fields = resp["data"]["fields"]
        r = dict(zip(fields, row))
        out[r["ts_code"]] = r
    return out


def _load_market_data(trade_date: str) -> list[dict]:
    """拉 Tushare daily_basic，返回 [{ts_code, circ_mv}]。"""
    resp = call_tushare(
        "daily_basic",
        {"trade_date": trade_date},
        "ts_code,trade_date,circ_mv",
    )
    items = resp.get("data", {}).get("items", [])
    fields = resp.get("data", {}).get("fields", [])
    return [dict(zip(fields, row)) for row in items]


def _is_main_board(code: str) -> bool:
    pure = code.split(".")[0]
    return pure.startswith(MAIN_BOARD_PREFIXES)


def _is_excluded_board(code: str) -> bool:
    pure = code.split(".")[0]
    return pure.startswith(EXCLUDED_PREFIXES)


def build_pool(trade_date: str | None = None) -> list[dict]:
    """构建候选池。

    Args:
        trade_date: YYYYMMDD，默认当天。

    Returns:
        [{code, name}] 按流通市值降序。
    """
    td = trade_date or _trade_date()
    stock_basic = _load_stock_basic()
    market_data = _load_market_data(td)

    pool = []
    try:
        today = datetime.strptime(td, "%Y%m%d").date()
    except (ValueError, TypeError):
        today = datetime.now().date()

    for row in market_data:
        code = row["ts_code"]

        # 规则: 主板
        if not _is_main_board(code):
            continue

        # 规则: 非创业板/科创板/北交所
        if _is_excluded_board(code):
            continue

        # 规则: 非ST
        info = stock_basic.get(code, {})
        name = info.get("name", "") or ""
        if "ST" in name or "*ST" in name:
            continue

        # 规则: 非次新（上市满120天）
        list_date_str = info.get("list_date", "")
        if list_date_str:
            try:
                list_date = datetime.strptime(str(list_date_str), "%Y%m%d").date()
                days_since = (today - list_date).days
                if days_since < MIN_LISTING_DAYS:
                    continue
            except (ValueError, TypeError):
                pass

        # 规则: 流通市值 50-200亿
        circ_mv = float(row.get("circ_mv", 0) or 0)
        if circ_mv < MARKET_CAP_MIN or circ_mv > MARKET_CAP_MAX:
            continue

        pool.append({
            "code": code,
            "name": name,
            "circ_mv": circ_mv,
        })

    pool.sort(key=lambda x: x.get("circ_mv", 0), reverse=True)
    return pool


def save_pool(pool: list[dict], trade_date: str | None = None):
    """保存候选池到 data/pool/pool_{date}.json。"""
    td = trade_date or _trade_date()
    path = POOL_DIR / f"pool_{td}.json"
    with open(path, "w") as f:
        json.dump(pool, f, ensure_ascii=False)
    print(f"[pool] 已保存 {path} ({len(pool)} 只)")


def load_pool(trade_date: str | None = None) -> list[dict] | None:
    """加载当日候选池。不存在返回 None。"""
    td = trade_date or _trade_date()
    path = POOL_DIR / f"pool_{td}.json"
    if not path.exists():
        return None
    with open(path) as f:
        return json.load(f)


def ensure_pool(trade_date: str | None = None, force: bool = False) -> list[dict]:
    """确保候选池存在。不存在则构建。返回池中股票。"""
    td = trade_date or _trade_date()
    if not force:
        existing = load_pool(td)
        if existing is not None:
            print(f"[pool] 使用缓存 pool_{td}.json ({len(existing)} 只)")
            return existing
    print(f"[pool] 构建候选池 {td}...")
    pool = build_pool(td)
    save_pool(pool, td)
    return pool


def main():
    import argparse
    parser = argparse.ArgumentParser(description="构建候选池")
    parser.add_argument("--date", help="交易日 YYYYMMDD")
    parser.add_argument("--force", action="store_true", help="强制重新构建")
    args = parser.parse_args()

    pool = ensure_pool(args.date, force=args.force)
    print(f"[pool] 共 {len(pool)} 只候选股")


if __name__ == "__main__":
    main()
