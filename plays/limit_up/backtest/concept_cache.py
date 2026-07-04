#!/usr/bin/env python3
"""构建并维护同花顺概念数据缓存。

概念数据用于 `factor_ctx.get_concept_momentum()`，为 sentiment / shortterm 策略
以及模型 PIT 特征提供板块动量信号。

缓存路径（按 wiki/raw 归档规范）：
    wiki/raw/limit-up/panel/concept/concept_daily.parquet
    wiki/raw/limit-up/panel/concept/concept_members.parquet

用法：
    python plays/limit_up/backtest/concept_cache.py build --start 20260601 --end 20260702
    python plays/limit_up/backtest/concept_cache.py build-members
    python plays/limit_up/backtest/concept_cache.py check
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from scripts.tu_share import call_tushare

DEFAULT_CACHE_DIR = PROJECT_DIR / "wiki" / "raw" / "limit-up" / "panel" / "concept"
LEGACY_CACHE_DIR = PROJECT_DIR / "plays" / "limit_up" / "backtest" / "cache"

CONCEPT_DAILY_FILE = "concept_daily.parquet"
CONCEPT_MEMBERS_FILE = "concept_members.parquet"


def _trade_dates(start: str, end: str) -> list[str]:
    """从 Tushare 拉取交易日历。"""
    cal = call_tushare(
        "trade_cal",
        {"exchange": "SSE", "start_date": start, "end_date": end},
        "cal_date,is_open",
    )
    dates: list[str] = []
    if not cal.get("data"):
        return dates
    fields = cal["data"]["fields"]
    for item in cal["data"]["items"]:
        row = dict(zip(fields, item))
        if int(row.get("is_open", 0)) == 1:
            dates.append(str(row["cal_date"]))
    return dates


def _load_or_init_daily(cache_dir: Path) -> pd.DataFrame:
    """加载已有 concept_daily，若无则返回空 DataFrame。"""
    path = cache_dir / CONCEPT_DAILY_FILE
    if path.exists():
        df = pd.read_parquet(path)
        for col in ("trade_date", "ts_code"):
            if col in df.columns:
                df[col] = df[col].astype(str)
        return df
    return pd.DataFrame(columns=["ts_code", "trade_date", "open", "high", "low",
                                   "close", "pre_close", "avg_price", "change",
                                   "pct_change", "vol", "turnover_rate"])


def _load_or_init_members(cache_dir: Path) -> pd.DataFrame:
    """加载已有 concept_members，若无则返回空 DataFrame。"""
    path = cache_dir / CONCEPT_MEMBERS_FILE
    if path.exists():
        df = pd.read_parquet(path)
        for col in ("ts_code", "con_code"):
            if col in df.columns:
                df[col] = df[col].astype(str)
        return df
    return pd.DataFrame(columns=["ts_code", "con_code", "con_name"])


def _ensure_dir(cache_dir: Path) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def _fetch_ths_index() -> pd.DataFrame:
    """获取全部同花顺概念代码。"""
    resp = call_tushare("ths_index", {}, "ts_code,name")
    data = resp.get("data", {})
    items = data.get("items", [])
    fields = data.get("fields", [])
    rows = [dict(zip(fields, item)) for item in items]
    df = pd.DataFrame(rows, columns=["ts_code", "name"])
    df["ts_code"] = df["ts_code"].astype(str)
    return df


def _fetch_ths_daily(trade_date: str) -> pd.DataFrame:
    """获取某交易日全部概念行情。"""
    resp = call_tushare("ths_daily", {"trade_date": trade_date}, "")
    data = resp.get("data", {})
    items = data.get("items", [])
    fields = data.get("fields", [])
    if not items or not fields:
        return pd.DataFrame()
    df = pd.DataFrame(items, columns=fields)
    numeric_cols = ["open", "high", "low", "close", "pre_close", "avg_price",
                    "change", "pct_change", "vol", "turnover_rate"]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df["trade_date"] = df["trade_date"].astype(str)
    df["ts_code"] = df["ts_code"].astype(str)
    return df


def _fetch_ths_member(cpt_code: str, max_retries: int = 3) -> pd.DataFrame:
    """获取单个概念的成分股映射，带重试。"""
    for attempt in range(max_retries):
        try:
            resp = call_tushare("ths_member", {"ts_code": cpt_code}, "", timeout=30)
            data = resp.get("data", {})
            items = data.get("items", [])
            fields = data.get("fields", [])
            if not items and not fields and attempt + 1 < max_retries:
                time.sleep(1)
                continue
            if not items or not fields:
                return pd.DataFrame()
            df = pd.DataFrame(items, columns=fields)
            for col in df.columns:
                df[col] = df[col].astype(str)
            return df
        except Exception:
            if attempt + 1 >= max_retries:
                return pd.DataFrame()
            time.sleep(1)
    return pd.DataFrame()


def build_concept_daily(start: str, end: str, cache_dir: Path | None = None) -> pd.DataFrame:
    """增量构建概念日线缓存。

    Args:
        start: 起始日 YYYYMMDD
        end: 结束日 YYYYMMDD
        cache_dir: 缓存目录，默认 wiki/raw/limit-up/panel/concept

    Returns:
        合并后的 concept_daily DataFrame
    """
    cache_dir = _ensure_dir(cache_dir or DEFAULT_CACHE_DIR)
    existing = _load_or_init_daily(cache_dir)
    existing_dates = set(existing["trade_date"].unique()) if not existing.empty else set()

    dates = _trade_dates(start, end)
    to_fetch = [d for d in dates if d not in existing_dates]
    if not to_fetch:
        print(f"[concept_daily] {start}~{end} 已全部存在，无需拉取")
        return existing

    frames = [existing] if not existing.empty else []
    print(f"[concept_daily] 需拉取 {len(to_fetch)} 个交易日")
    for i, d in enumerate(to_fetch):
        df = _fetch_ths_daily(d)
        if df.empty:
            print(f"  [{d}] 无数据")
            continue
        frames.append(df)
        if (i + 1) % 10 == 0 or i == len(to_fetch) - 1:
            print(f"  已拉取 {i + 1}/{len(to_fetch)} 天")
        time.sleep(0.05)  # 轻微降速，避免触发频控

    if not frames:
        return existing

    combined = pd.concat(frames, ignore_index=True)
    combined = combined.drop_duplicates(subset=["ts_code", "trade_date"], keep="last")
    combined = combined.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
    # factor_ctx 期望概念代码列为 cpt_code
    if "ts_code" in combined.columns and "cpt_code" not in combined.columns:
        combined = combined.rename(columns={"ts_code": "cpt_code"})

    out_path = cache_dir / CONCEPT_DAILY_FILE
    combined.to_parquet(out_path, index=False)
    print(f"[concept_daily] 已保存 {len(combined)} 行 -> {out_path}")
    return combined


def build_concept_members(cache_dir: Path | None = None) -> pd.DataFrame:
    """构建概念成分股映射缓存。

    首次构建较慢（约 1700 个概念），建议每月刷新一次。
    每 100 个概念增量落盘，避免网络中断导致全部丢失。
    """
    cache_dir = _ensure_dir(cache_dir or DEFAULT_CACHE_DIR)
    existing = _load_or_init_members(cache_dir)
    existing_codes = set(existing["ts_code"].unique()) if not existing.empty else set()

    index_df = _fetch_ths_index()
    codes = [c for c in index_df["ts_code"].unique() if c not in existing_codes]
    if not codes:
        print("[concept_members] 已全部存在，无需拉取")
        return existing

    print(f"[concept_members] 需拉取 {len(codes)} 个概念（共 {len(index_df)} 个）")
    frames = [existing] if not existing.empty else []
    out_path = cache_dir / CONCEPT_MEMBERS_FILE

    for i, cpt in enumerate(codes):
        df = _fetch_ths_member(cpt)
        if not df.empty:
            # 统一字段名：ts_code(概念码), con_code(股票代码), con_name(股票名)
            if "ts_code" not in df.columns and len(df.columns) >= 2:
                df.columns = ["ts_code", "con_code", "con_name"][:len(df.columns)]
            # factor_ctx 期望概念代码列为 cpt_code，股票代码列为 stock_code（短代码）
            rename_map = {}
            if "ts_code" in df.columns:
                rename_map["ts_code"] = "cpt_code"
            if "con_code" in df.columns:
                rename_map["con_code"] = "stock_code"
            if rename_map:
                df = df.rename(columns=rename_map)
            if "stock_code" in df.columns:
                df["stock_code"] = df["stock_code"].str.replace(r"\.(SH|SZ|BJ)$", "", regex=True)
            frames.append(df)
        if (i + 1) % 100 == 0 or i == len(codes) - 1:
            print(f"  已拉取 {i + 1}/{len(codes)} 个概念")
            # 每 100 个概念或最后批量落盘，保留进度
            if frames:
                combined = pd.concat(frames, ignore_index=True)
                combined = combined.drop_duplicates(subset=["ts_code", "con_code"], keep="last")
                combined = combined.sort_values(["ts_code", "con_code"]).reset_index(drop=True)
                combined.to_parquet(out_path, index=False)
                print(f"  [checkpoint] 已保存 {len(combined)} 行")
                frames = [combined]
        time.sleep(0.5)  # ths_member 速率限制较严，保守间隔

    if not frames:
        return existing

    combined = pd.concat(frames, ignore_index=True)
    combined = combined.drop_duplicates(subset=["ts_code", "con_code"], keep="last")
    combined = combined.sort_values(["ts_code", "con_code"]).reset_index(drop=True)

    combined.to_parquet(out_path, index=False)
    print(f"[concept_members] 已保存 {len(combined)} 行 -> {out_path}")
    return combined


def check_cache(cache_dir: Path | None = None) -> dict:
    """检查缓存状态。"""
    cache_dir = cache_dir or DEFAULT_CACHE_DIR
    daily_path = cache_dir / CONCEPT_DAILY_FILE
    member_path = cache_dir / CONCEPT_MEMBERS_FILE
    out = {"cache_dir": str(cache_dir), "daily_exists": False, "members_exists": False}
    if daily_path.exists():
        df = pd.read_parquet(daily_path)
        out["daily_exists"] = True
        out["daily_rows"] = len(df)
        out["daily_dates"] = sorted(df["trade_date"].astype(str).unique().tolist())
        out["daily_concepts"] = df["ts_code"].nunique()
    if member_path.exists():
        df = pd.read_parquet(member_path)
        out["members_exists"] = True
        out["members_rows"] = len(df)
        out["members_concepts"] = df["ts_code"].nunique()
    return out


def _cmd_build(args):
    build_concept_daily(args.start, args.end, Path(args.cache_dir) if args.cache_dir else None)


def _cmd_build_members(args):
    build_concept_members(Path(args.cache_dir) if args.cache_dir else None)


def _cmd_check(args):
    info = check_cache(Path(args.cache_dir) if args.cache_dir else None)
    print(f"缓存目录: {info['cache_dir']}")
    if info["daily_exists"]:
        dates = info["daily_dates"]
        print(f"  concept_daily: {info['daily_rows']} 行, "
              f"{info['daily_concepts']} 个概念, "
              f"日期 {dates[0]} ~ {dates[-1]}")
    else:
        print("  concept_daily: 不存在")
    if info["members_exists"]:
        print(f"  concept_members: {info['members_rows']} 行, "
              f"{info['members_concepts']} 个概念")
    else:
        print("  concept_members: 不存在")


def main():
    parser = argparse.ArgumentParser(description="构建同花顺概念数据缓存")
    parser.add_argument("--cache-dir", help=f"缓存目录，默认 {DEFAULT_CACHE_DIR}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_build = sub.add_parser("build", help="构建概念日线行情缓存")
    p_build.add_argument("--start", required=True, help="起始日 YYYYMMDD")
    p_build.add_argument("--end", required=True, help="结束日 YYYYMMDD")
    p_build.set_defaults(func=_cmd_build)

    p_members = sub.add_parser("build-members", help="构建概念成分股映射缓存")
    p_members.set_defaults(func=_cmd_build_members)

    p_check = sub.add_parser("check", help="检查缓存状态")
    p_check.set_defaults(func=_cmd_check)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
