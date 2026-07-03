#!/usr/bin/env python3
"""
回测数据层：批量拉取 + 本地缓存

用法:
    cache = fetch_data("20260601", "20260620")
    # cache["daily"] -> list of dicts
    # cache["limit_list_d"] -> list of dicts
"""
import json
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from scripts.tu_share import call_tushare

CACHE_DIR = Path(__file__).resolve().parent / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _cache_key(start: str, end: str) -> str:
    return f"bulk_{start}_{end}.json"


def _load_cache(start: str, end: str) -> dict | None:
    path = CACHE_DIR / _cache_key(start, end)
    if path.exists():
        print(f"  [缓存] 命中 {path.name}")
        with open(path) as f:
            return json.load(f)
    return None


def _save_cache(start: str, end: str, data: dict):
    path = CACHE_DIR / _cache_key(start, end)
    with open(path, "w") as f:
        json.dump(data, f, ensure_ascii=False, default=str)
    print(f"  [缓存] 已保存 {path.name} ({len(json.dumps(data))//1024}KB)")


def _ts(code: str, params: dict, fields: str) -> list[dict]:
    """调用 Tushare 并转为 list[dict]"""
    resp = call_tushare(code, params, fields)
    items = resp.get("data", {}).get("items", [])
    cols = resp.get("data", {}).get("fields", [])
    return [dict(zip(cols, row)) for row in items]


def _fmt(v):
    if v is None:
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return str(v)


def fetch_data(start: str, end: str, use_jvquant: bool = False, force: bool = False) -> dict:
    """
    批量拉取 start~end 日期范围内的回测数据。

    Args:
        start: YYYYMMDD
        end: YYYYMMDD
        use_jvquant: 是否尝试 jvQuant SQL 增强
        force: 强制重新拉取

    Returns:
        dict: {
            "trade_cal": [...],
            "stock_basic": [...],
            "daily": [...],
            "daily_basic": [...],
            "moneyflow": [...],
            "limit_list_d": [...],
            "limit_step": [...],
            "stk_auction": [...],
            "stk_limit": [...],
            # jvQuant 增强（可选）
            "jv_kline": {...},
            "jv_minute": {...},
        }
    """
    if not force:
        cached = _load_cache(start, end)
        if cached:
            return cached

    print(f"\n[数据] 拉取 {start} ~ {end}")
    t0 = time.time()
    data = {}

    # 交易日历（全量）
    print("  trade_cal...", end=" ", flush=True)
    data["trade_cal"] = _ts("trade_cal", {}, "exchange,cal_date,is_open,pretrade_date")
    print(f"{len(data['trade_cal'])}条")

    # 股票基本信息（全量）
    print("  stock_basic...", end=" ", flush=True)
    data["stock_basic"] = _ts("stock_basic", {}, "ts_code,name,area,industry,list_date,market,exchange,is_hs")
    print(f"{len(data['stock_basic'])}条")

    # 日线（按日期范围）
    print("  daily...", end=" ", flush=True)
    daily_rows = _ts("daily", {"start_date": start, "end_date": end},
                     "ts_code,trade_date,open,high,low,close,pre_close,pct_chg,vol,amount")
    print(f"{len(daily_rows)}条")

    # 日线基础指标
    print("  daily_basic...", end=" ", flush=True)
    db_rows = _ts("daily_basic", {"start_date": start, "end_date": end},
                  "ts_code,trade_date,close,turnover_rate,turnover_rate_f,volume_ratio,circ_mv,total_mv,amount,pe,pb")
    print(f"{len(db_rows)}条")

    # 资金流向
    print("  moneyflow...", end=" ", flush=True)
    mf_rows = _ts("moneyflow", {"start_date": start, "end_date": end},
                  "ts_code,trade_date,buy_elg_amount,sell_elg_amount,buy_lg_amount,sell_lg_amount,net_mf_amount")
    print(f"{len(mf_rows)}条")

    # 涨停列表
    print("  limit_list_d...", end=" ", flush=True)
    ll_rows = _ts("limit_list_d", {"start_date": start, "end_date": end},
                  "ts_code,trade_date,limit,open_times,fd_amount,first_time,last_time,up_times,down_times,status")
    print(f"{len(ll_rows)}条")

    # 连板信息
    print("  limit_step...", end=" ", flush=True)
    ls_rows = _ts("limit_step", {"start_date": start, "end_date": end},
                  "ts_code,trade_date,nums")
    print(f"{len(ls_rows)}条")

    # 集合竞价
    print("  stk_auction...", end=" ", flush=True)
    try:
        auc_rows = _ts("stk_auction", {"start_date": start, "end_date": end},
                       "ts_code,trade_date,price,pre_close,volume,amount,turnover_rate")
        print(f"{len(auc_rows)}条")
    except Exception as e:
        print(f"失败({e})，跳过")
        auc_rows = []

    # 涨跌停价格
    print("  stk_limit...", end=" ", flush=True)
    try:
        sl_rows = _ts("stk_limit", {"start_date": start, "end_date": end},
                      "ts_code,trade_date,up_limit,down_limit")
        print(f"{len(sl_rows)}条")
    except Exception as e:
        print(f"失败({e})，跳过")
        sl_rows = []

    # 按 trade_date 索引各表，方便按天查询
    data["trade_cal"] = _index_trade_cal(data["trade_cal"])
    data["stock_basic_map"] = _index_stock_basic(data["stock_basic"])
    data["daily"] = _index_by_date(daily_rows)
    data["daily_basic"] = _index_by_date(db_rows)
    data["moneyflow"] = _index_by_date(mf_rows)
    data["limit_list_d"] = _index_by_date(ll_rows)
    data["limit_step"] = _index_by_date(ls_rows)
    data["stk_auction"] = _index_by_date(auc_rows)
    data["stk_limit"] = _index_by_date(sl_rows)

    # 可选：jvQuant SQL 增强
    if use_jvquant:
        _fetch_jvquant(data, start, end)

    elapsed = time.time() - t0
    print(f"  [数据] 完成 ({elapsed:.1f}s)")
    _save_cache(start, end, data)
    return data


def _index_by_date(rows: list[dict]) -> dict:
    """按 trade_date 分组索引: {trade_date: [row, ...]}"""
    idx: dict[str, list[dict]] = {}
    for r in rows:
        d = r.get("trade_date", "")
        if d not in idx:
            idx[d] = []
        # 转数值类型
        row = {k: _fmt(v) for k, v in r.items()}
        idx[d].append(row)
    return idx


def _index_trade_cal(rows: list[dict]) -> dict:
    """交易日历: {cal_date: {exchange, is_open, ...}}"""
    idx = {}
    for r in rows:
        idx[r["cal_date"]] = {k: _fmt(v) for k, v in r.items()}
    return idx


def _index_stock_basic(rows: list[dict]) -> dict:
    """股票基本信息: {ts_code: {name, market, ...}}"""
    idx = {}
    for r in rows:
        idx[r["ts_code"]] = {k: _fmt(v) for k, v in r.items()}
    return idx


def _fetch_jvquant(data: dict, start: str, end: str):
    """通过 jvQuant SQL 客户端获取增强数据"""
    print("  jvQuant SQL...", end=" ", flush=True)
    try:
        import jvQuant
        from scripts.jvquant_ws_client import _load_env

        env = _load_env()
        token = env.get("JVQUANT_TOKEN", "")
        if not token:
            print("无 token，跳过")
            return

        sql = jvQuant.sql_client
        client = sql.Construct(token)

        # K 线数据（按日/周，用于短线 VWAP 计算）
        # 回放指定日期范围内所有主板股票的日K线
        kline_data = {}
        sample_stocks = _get_sample_stocks(data)
        for ts_code in sample_stocks[:50]:  # 限制数量避免成本过高
            code_short = ts_code.replace(".SH", "").replace(".SZ", "")
            try:
                resp = client.kline(code_short, "stock", "前复权", "day", 60)
                kline_data[ts_code] = resp
            except Exception:
                pass
        data["jv_kline"] = kline_data
        print(f"K线({len(kline_data)}只)", end=" ")

        print("OK")
    except ImportError:
        print("jvQuant 未安装，跳过")
    except Exception as e:
        print(f"失败({e})，跳过")


def _get_sample_stocks(data: dict) -> list[str]:
    """从 daily 数据中取有交易记录的主板股票"""
    codes = set()
    for date_rows in data.get("daily", {}).values():
        for r in date_rows:
            code = r.get("ts_code", "")
            pure = code.split(".")[0]
            if pure.startswith(("6", "0", "2")):  # 主板+中小板
                codes.add(code)
    return sorted(codes)
