#!/usr/bin/env python3
"""
涨停预测回测框架 — 历史数据回放，模拟每日评分 vs 实际涨停

核心思路:
  对每个历史交易日 T:
    1. 从当日涨幅≥2%的主板股中构建候选池（模拟 pipeline 扫描）
    2. 用 T-1 及以前的数据评分（防未来数据泄露）
    3. 与 T 日实际涨停标签对比

用法:
  python plays/limit_up/backtest.py --days 10 --top 150
  python plays/limit_up/backtest.py --days 10 --top 150 --skip-scoring  # 仅预取数据

输出: data/backtest/backtest_results.json + data/backtest/backtest_metrics.json
"""

import argparse
import json
import sys
import time
import traceback
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_DIR))
sys.path.insert(0, str(PROJECT_DIR / "scripts"))

PLAY_DIR = Path(__file__).resolve().parent
DATA_DIR = PLAY_DIR / "data"
BACKTEST_DIR = DATA_DIR / "backtest"
BACKTEST_DIR.mkdir(parents=True, exist_ok=True)

# ===== 全局状态 =====
_SIMULATION_DATE: str | None = None  # 当前模拟的交易日期 T
_PREFETCHED: dict = {}  # 预取原始数据缓存
_STOCK_BASIC_CACHE: dict[str, dict] = {}  # code_short → {name, industry, list_date}
_PER_STOCK_CACHE: dict[str, dict] = {}  # code_short → {api_name: {fields: [...], items: [...]}}
_SCORE_CACHE: dict[tuple[str, str], dict] = {}  # (code, trade_date) → {dim: (score, reason)}
_API_CALL_COUNT: int = 0

DIMS = ["fundamental", "technical", "fundflow", "sentiment", "shortterm"]


# ═══════════════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════════════

def _safe_float(val, default=0.0):
    if val is None: return default
    try: return float(val)
    except (ValueError, TypeError): return default


def _make_cache_key(api_name: str, params: dict) -> str:
    items = sorted(params.items())
    return f"{api_name}:{json.dumps(items, ensure_ascii=False)}"


def _code_short(code: str) -> str:
    return code.replace(".SH", "").replace(".SZ", "").replace(".BJ", "")


def _to_dicts(items: list, fields: list) -> list[dict]:
    if not fields or not items: return []
    return [dict(zip(fields, row)) for row in items]


# ═══════════════════════════════════════════════════════════════════
# BacktestDataProvider — Tushare 拦截层
# ═══════════════════════════════════════════════════════════════════

class BacktestDataProvider:
    """回测数据提供者：拦截 call_tushare，从预取缓存返回历史数据。

    核心安全规则:
    - 所有日期参数必须 ≤ simulation_date（T-1），防止未来数据泄露
    - 当参数中 trade_date = simulation_date 时，自动退到上一交易日
    - 对于无日期参数的接口（如 stk_factor_pro），直接返回缓存数据
    """

    def __init__(self, simulation_date: str, prefecthed: dict,
                 stock_basic: dict, prev_trade_date: str,
                 real_call_tushare=None, per_stock_data: dict | None = None):
        self.sim_date = simulation_date
        self.prev_date = prev_trade_date  # T-1 交易日
        self.prefetched = prefecthed
        self.stock_basic = stock_basic
        self._per_stock = {}  # 本次回测中的逐股缓存
        self._real_call = real_call_tushare  # 真实的 call_tushare（未打补丁）
        self._per_stock_data = per_stock_data or {}  # 预取的逐股数据
        self._date_index = {}  # {api_name: {date: {code: row}}}
        self._build_date_index()

    def _build_date_index(self):
        """为 bulk 数据构建日期索引，加速按日查询"""
        for api_name, cache in self.prefetched.items():
            if api_name in ("trade_cal", "stock_basic"):
                continue  # 原始响应格式，不需要索引
            idx = defaultdict(dict)  # {date: {code_or_key: row}}
            for k, v in cache.items():
                if len(k) == 2:
                    d, code = k
                    idx[d][code] = v
                elif len(k) == 3:
                    d, code, ltype = k
                    idx[d][(code, ltype)] = v
            self._date_index[api_name] = dict(idx)

    def call(self, api_name: str, params: dict, fields: str = "",
             timeout: int = 10) -> dict:
        """拦截 call_tushare，从预取缓存返回数据"""
        global _API_CALL_COUNT
        _API_CALL_COUNT += 1

        # 1. 调整日期参数
        params = self._adjust_dates(params)

        # 2. 按接口类型分发
        if api_name == "trade_cal":
            return self._handle_trade_cal(params, fields)
        if api_name == "stock_basic":
            return self._handle_stock_basic(params, fields)
        if api_name in ("daily", "daily_basic", "moneyflow", "limit_list_d",
                        "limit_step", "stk_auction", "daily_info", "limit_cpt_list"):
            return self._handle_bulk_api(api_name, params, fields)
        if api_name == "stk_factor_pro":
            return self._handle_stk_factor(params, fields)
        # 逐股接口: fina_indicator, balancesheet, income, concept_detail,
        #           stk_holdernumber, hk_hold, margin_detail, top_list,
        #           top_inst, share_float, anns_d, fina_forecast
        return self._handle_per_stock_api(api_name, params, fields)

    def _adjust_dates(self, params: dict) -> dict:
        """将所有日期参数 clamp 到 T-1"""
        adjusted = dict(params)
        for key in ("trade_date", "start_date", "end_date"):
            if key in adjusted:
                val = str(adjusted[key])
                if val >= self.sim_date:
                    adjusted[key] = self.prev_date
        return adjusted

    def _handle_trade_cal(self, params: dict, fields: str) -> dict:
        return self.prefetched.get("trade_cal", {"data": {"fields": [], "items": []}})

    def _handle_stock_basic(self, params: dict, fields: str) -> dict:
        ts_code = params.get("ts_code", "")
        if ts_code:
            short = _code_short(ts_code)
            info = self.stock_basic.get(short, {})
            if info:
                items = [[info.get("ts_code", ts_code),
                          info.get("name", ""),
                          info.get("industry", ""),
                          info.get("list_date", "")]]
            else:
                items = [[ts_code, "", "", ""]]
            return {"data": {"fields": ["ts_code", "name", "industry", "list_date"],
                             "items": items}}
        # 全量返回
        items = [[v.get("ts_code", ""), v.get("name", ""),
                  v.get("industry", ""), v.get("list_date", "")]
                 for v in self.stock_basic.values()]
        return {"data": {"fields": ["ts_code", "name", "industry", "list_date"],
                         "items": items}}

    def _handle_bulk_api(self, api_name: str, params: dict, fields: str) -> dict:
        """处理按日期批量查询的接口，使用日期索引加速"""
        cache = self.prefetched.get(api_name, {})
        date_idx = self._date_index.get(api_name, {})
        ts_code = params.get("ts_code", "")
        trade_date = params.get("trade_date", "")
        start_date = params.get("start_date", "")
        end_date = params.get("end_date", "")
        flds = fields.split(",") if fields else []

        # 对于 limit_list_d，查询时可能不指定 limit_type，需要特殊处理
        if api_name == "limit_list_d":
            return self._handle_limit_list_d(params, fields, flds, date_idx)

        # 单日单股查询 — 最快路径
        if ts_code and trade_date:
            day_data = date_idx.get(trade_date, {})
            row = day_data.get(ts_code, None)
            return {"data": {"fields": flds, "items": [list(row.values())] if row else []}}

        # 日期范围+单股
        if ts_code and start_date and end_date:
            items = []
            for d in sorted(date_idx.keys()):
                if start_date <= d <= end_date:
                    row = date_idx[d].get(ts_code)
                    if row:
                        items.append(list(row.values()) if isinstance(row, dict) else row)
            return {"data": {"fields": flds, "items": items}}

        # 单日全量
        if trade_date and not ts_code:
            day_data = date_idx.get(trade_date, {})
            items = []
            for code, row in day_data.items():
                if isinstance(code, str):  # 跳过复合 key
                    items.append(list(row.values()) if isinstance(row, dict) else row)
            return {"data": {"fields": flds, "items": items}}

        # 日期范围全量
        if start_date and end_date and not ts_code:
            items = []
            for d in sorted(date_idx.keys()):
                if start_date <= d <= end_date:
                    for code, row in date_idx[d].items():
                        if isinstance(code, str):
                            items.append(list(row.values()) if isinstance(row, dict) else row)
            return {"data": {"fields": flds, "items": items}}

        return {"data": {"fields": [], "items": []}}

    def _handle_limit_list_d(self, params: dict, fields: str, flds: list,
                              date_idx: dict) -> dict:
        """limit_list_d 特殊处理：支持 limit_type 过滤和 ts_code 过滤"""
        ts_code = params.get("ts_code", "")
        trade_date = params.get("trade_date", "")
        start_date = params.get("start_date", "")
        end_date = params.get("end_date", "")
        limit_type = params.get("limit_type", "").upper()

        day_data = date_idx.get(trade_date, {}) if trade_date else {}

        if ts_code and trade_date:
            # 查找该股的所有 limit_type（U/D/Z）
            items = []
            for lt in ("U", "D", "Z"):
                if limit_type and lt != limit_type:
                    continue
                row = day_data.get((ts_code, lt))
                if row:
                    items.append(list(row.values()) if isinstance(row, dict) else row)
            return {"data": {"fields": flds, "items": items}}

        if trade_date and not ts_code:
            items = []
            for key, row in day_data.items():
                if isinstance(key, tuple) and len(key) == 2:
                    k_code, k_lt = key
                    if limit_type and k_lt != limit_type:
                        continue
                    items.append(list(row.values()) if isinstance(row, dict) else [k_code])
            return {"data": {"fields": flds, "items": items}}

        if ts_code and start_date and end_date:
            items = []
            for d in sorted(date_idx.keys()):
                if start_date <= d <= end_date:
                    for lt in ("U", "D", "Z"):
                        if limit_type and lt != limit_type:
                            continue
                        row = date_idx[d].get((ts_code, lt))
                        if row:
                            items.append(list(row.values()) if isinstance(row, dict) else row)
            return {"data": {"fields": flds, "items": items}}

        return {"data": {"fields": [], "items": []}}

    def _handle_stk_factor(self, params: dict, fields: str) -> dict:
        """stk_factor_pro: 返回预取的因子数据，按日期过滤"""
        ts_code = params.get("ts_code", "")
        short = _code_short(ts_code)

        # 优先从预取 per_stock_data 获取
        ps_data = self._per_stock_data.get(short, {}).get("stk_factor_pro")
        if ps_data:
            all_items = ps_data.get("items", [])
            all_fields = ps_data.get("fields", [])
            # 过滤：只返回 simulation_date 之前的数据
            filtered = []
            for item in all_items:
                row = dict(zip(all_fields, item)) if all_fields else {}
                d = str(row.get("trade_date", ""))
                if d <= self.prev_date:
                    filtered.append(item)
            return {"data": {"fields": all_fields, "items": filtered}}

        # 降级：从 daily 缓存模拟（无 MA/MACD 等技术指标）
        cache = self.prefetched.get("daily", {})
        items = []
        for (d, c), row in sorted(cache.items(), reverse=True):
            if c == ts_code and d <= self.prev_date:
                items.append(list(row.values()) if isinstance(row, dict) else row)
        flds = fields.split(",") if fields else []
        return {"data": {"fields": flds, "items": items}}

    def _handle_per_stock_api(self, api_name: str, params: dict,
                               fields: str) -> dict:
        """逐股接口：从 per_stock_data 缓存返回（预取或降级为空）"""
        ts_code = params.get("ts_code", "")
        short = _code_short(ts_code)

        # 从预取 per_stock_data 获取
        ps_data = self._per_stock_data.get(short, {}).get(api_name)
        if ps_data:
            return {"data": {"fields": ps_data.get("fields", []),
                             "items": ps_data.get("items", [])}}

        # 从运行时缓存获取
        cache_key = f"{api_name}:{short}"
        if cache_key in self._per_stock:
            return self._per_stock[cache_key]

        # daily_basic 从 bulk 缓存走
        if api_name == "daily_basic":
            return self._handle_bulk_api(api_name, params, fields)

        # 其他逐股接口：返回空（策略层有异常处理/降级逻辑）
        return {"data": {"fields": fields.split(",") if fields else [],
                         "items": []}}


# ═══════════════════════════════════════════════════════════════════
# 数据预取
# ═══════════════════════════════════════════════════════════════════

def pre_fetch_bulk_data(trade_dates: list[str], cache_dir: Path | None = None,
                         use_cache: bool = True) -> dict:
    """批量预取：按日期拉取全量数据，缓存到磁盘加速后续回测。

    Args:
        trade_dates: 交易日列表（YYYYMMDD，升序）
        cache_dir: 缓存目录，用于磁盘持久化
        use_cache: 是否使用磁盘缓存

    Returns:
        prefetched: {api_name: {(date, code): row_dict}}
    """
    from scripts.tu_share import call_tushare, clear_tushare_cache

    if not trade_dates:
        return {}

    all_dates = sorted(trade_dates)
    min_date = all_dates[0]
    max_date = all_dates[-1]
    print(f"预取数据: {len(all_dates)} 个交易日 [{min_date} → {max_date}]")

    prefetched = {}
    cache_file = cache_dir / "bulk_cache.json" if cache_dir else None

    # 尝试从磁盘加载缓存
    if use_cache and cache_file and cache_file.exists():
        try:
            with open(cache_file) as f:
                cached = json.load(f)
            cached_dates = set(cached.get("_dates", []))
            if cached_dates >= set(all_dates):
                print(f"  从磁盘缓存加载 (跳过 API 调用)")
                result = {}
                for api, data in cached.items():
                    if api == "_dates": continue
                    if api in ("trade_cal", "stock_basic"):
                        # 这些是原始API响应，直接使用
                        result[api] = data
                    else:
                        # 转换回 (date, code) → row 的字典
                        result[api] = {}
                        for k, v in data.items():
                            parts = k.split("|", 2)
                            if len(parts) == 2:
                                result[api][(parts[0], parts[1])] = v
                            elif len(parts) == 3:
                                result[api][(parts[0], parts[1], parts[2])] = v
                return result
        except Exception:
            pass

    # ── 1. trade_cal ──
    print("  [1/8] trade_cal ...", end="", flush=True)
    clear_tushare_cache()
    resp = call_tushare("trade_cal", {
        "exchange": "SSE",
        "start_date": min_date,
        "end_date": max_date
    }, "cal_date,is_open,pretrade_date")
    prefetched["trade_cal"] = resp
    print(f" OK")

    # ── 2. stock_basic ──
    print("  [2/8] stock_basic ...", end="", flush=True)
    clear_tushare_cache()
    resp = call_tushare("stock_basic", {"list_status": "L"},
                        "ts_code,name,industry,list_date")
    prefetched["stock_basic"] = resp
    items = resp.get("data", {}).get("items", [])
    print(f" {len(items)} stocks OK")

    # ── 3. daily (逐日拉取) ──
    print(f"  [3/8] daily ({len(all_dates)}天) ...", end="", flush=True)
    daily_cache = {}
    for i, d in enumerate(all_dates):
        clear_tushare_cache()
        resp = call_tushare("daily", {"trade_date": d},
                            "ts_code,open,high,low,close,pre_close,pct_chg,vol,amount")
        items = resp.get("data", {}).get("items", [])
        fields = resp.get("data", {}).get("fields", [])
        if fields and items:
            for item in items:
                row = dict(zip(fields, item))
                code = row.get("ts_code", "")
                if code:
                    daily_cache[(d, code)] = row
        if (i + 1) % 5 == 0: print(f" {i+1}/{len(all_dates)}", end="", flush=True)
    prefetched["daily"] = daily_cache
    print(f" {len(daily_cache)} rows OK")

    # ── 4. daily_basic (逐日拉取) ──
    print(f"  [4/8] daily_basic ({len(all_dates)}天) ...", end="", flush=True)
    db_cache = {}
    for i, d in enumerate(all_dates):
        clear_tushare_cache()
        resp = call_tushare("daily_basic", {"trade_date": d},
                            "ts_code,turnover_rate,turnover_rate_f,volume_ratio,"
                            "circ_mv,total_mv,pe,pb,amount")
        items = resp.get("data", {}).get("items", [])
        fields = resp.get("data", {}).get("fields", [])
        if fields and items:
            for item in items:
                row = dict(zip(fields, item))
                code = row.get("ts_code", "")
                if code:
                    db_cache[(d, code)] = row
        if (i + 1) % 5 == 0: print(f" {i+1}/{len(all_dates)}", end="", flush=True)
    prefetched["daily_basic"] = db_cache
    print(f" {len(db_cache)} rows OK")

    # ── 5. moneyflow (逐日拉取) ──
    print(f"  [5/8] moneyflow ({len(all_dates)}天) ...", end="", flush=True)
    mf_cache = {}
    for i, d in enumerate(all_dates):
        clear_tushare_cache()
        resp = call_tushare("moneyflow", {"trade_date": d},
                            "ts_code,net_mf_amount,net_mf_vol,"
                            "buy_elg_amount,sell_elg_amount,"
                            "buy_lg_amount,sell_lg_amount")
        items = resp.get("data", {}).get("items", [])
        fields = resp.get("data", {}).get("fields", [])
        if fields and items:
            for item in items:
                row = dict(zip(fields, item))
                code = row.get("ts_code", "")
                if code:
                    mf_cache[(d, code)] = row
        if (i + 1) % 5 == 0: print(f" {i+1}/{len(all_dates)}", end="", flush=True)
    prefetched["moneyflow"] = mf_cache
    print(f" {len(mf_cache)} rows OK")

    # ── 6. limit_list_d (逐日拉取) ──
    print(f"  [6/8] limit_list_d ({len(all_dates)}天) ...", end="", flush=True)
    ll_cache = {}
    for i, d in enumerate(all_dates):
        clear_tushare_cache()
        for ltype in ["U", "D", "Z"]:
            resp = call_tushare("limit_list_d", {"trade_date": d, "limit_type": ltype},
                                "ts_code,name,close,pct_chg,limit_times,limit,"
                                "open_times,fd_amount,first_time,last_time,up_stat")
            items = resp.get("data", {}).get("items", [])
            fields = resp.get("data", {}).get("fields", [])
            if fields and items:
                for item in items:
                    row = dict(zip(fields, item))
                    code = row.get("ts_code", "")
                    if code:
                        ll_cache[(d, code, ltype)] = row
        if (i + 1) % 5 == 0: print(f" {i+1}/{len(all_dates)}", end="", flush=True)
    prefetched["limit_list_d"] = ll_cache
    print(f" {len(ll_cache)} rows OK")

    # ── 7. limit_step (逐日拉取) ──
    print(f"  [7/8] limit_step ({len(all_dates)}天) ...", end="", flush=True)
    ls_cache = {}
    for i, d in enumerate(all_dates):
        clear_tushare_cache()
        resp = call_tushare("limit_step", {"trade_date": d},
                            "trade_date,ts_code,name,nums")
        items = resp.get("data", {}).get("items", [])
        fields = resp.get("data", {}).get("fields", [])
        if fields and items:
            for item in items:
                row = dict(zip(fields, item))
                code = row.get("ts_code", "")
                if code:
                    ls_cache[(d, code)] = row
        if (i + 1) % 5 == 0: print(f" {i+1}/{len(all_dates)}", end="", flush=True)
    prefetched["limit_step"] = ls_cache
    print(f" {len(ls_cache)} rows OK")

    # ── 8. stk_auction (逐日拉取，可能部分日期无数据) ──
    print(f"  [8/8] stk_auction ({len(all_dates)}天) ...", end="", flush=True)
    sa_cache = {}
    for i, d in enumerate(all_dates):
        clear_tushare_cache()
        resp = call_tushare("stk_auction", {"trade_date": d},
                            "ts_code,vol,price,amount,pre_close,"
                            "turnover_rate,volume_ratio,float_share")
        items = resp.get("data", {}).get("items", [])
        fields = resp.get("data", {}).get("fields", [])
        if fields and items:
            for item in items:
                row = dict(zip(fields, item))
                code = row.get("ts_code", "")
                if code:
                    sa_cache[(d, code)] = row
        if (i + 1) % 5 == 0: print(f" {i+1}/{len(all_dates)}", end="", flush=True)
    prefetched["stk_auction"] = sa_cache
    print(f" {len(sa_cache)} rows OK")

    # 保存磁盘缓存
    if cache_file:
        try:
            serializable = {"_dates": list(all_dates)}
            for api, data in prefetched.items():
                if api in ("trade_cal", "stock_basic"):
                    # 原始 API 响应，直接保存
                    serializable[api] = data
                else:
                    # (date, code) → row 的字典，将 tuple key 序列化为字符串
                    serializable[api] = {}
                    for k, v in data.items():
                        serializable[api]["|".join(str(x) for x in k)] = v
            with open(cache_file, "w") as f:
                json.dump(serializable, f, ensure_ascii=False)
            print(f"  磁盘缓存已保存: {cache_file}")
        except Exception as e:
            print(f"  磁盘缓存保存失败: {e}")

    return prefetched


def build_stock_basic_lookup(prefetched: dict) -> dict[str, dict]:
    """从预取数据构建 code_short → stock_basic 的查找表"""
    resp = prefetched.get("stock_basic", {})
    items = resp.get("data", {}).get("items", [])
    fields = resp.get("data", {}).get("fields", [])
    lookup = {}
    if fields and items:
        for item in items:
            row = dict(zip(fields, item))
            code = row.get("ts_code", "")
            if code:
                short = _code_short(code)
                lookup[short] = row
    return lookup


# ═══════════════════════════════════════════════════════════════════
# 候选池构建
# ═══════════════════════════════════════════════════════════════════

def build_candidate_pool(trade_date: str, prefetched: dict,
                         stock_basic: dict, top_n: int = 150) -> list[dict]:
    """构建某日的候选股列表：涨幅≥2%的主板股，按涨幅排序取 top_n。

    过滤规则（与 filter.py 一致）:
    - 排除创业板(30xxxx.SZ)、科创板(688xxx.SH)、北交所(8xxxx/4xxxx)
    - 排除 ST/*ST
    - 排除上市不满60日新股
    - 排除流通市值<5亿
    - 排除换手率<2%
    - 排除连续一字板（无法买入）
    """
    daily = prefetched.get("daily", {})
    db_cache = prefetched.get("daily_basic", {})

    candidates = []
    for (d, code), row in daily.items():
        if d != trade_date:
            continue

        pct = _safe_float(row.get("pct_chg", 0))
        if pct < 2 or pct > 9.5:
            continue

        short = _code_short(code)
        if not short:
            continue

        # 过滤创业板/科创板/北交所
        if (short.startswith("30") or short.startswith("688") or
                short.startswith("8") or short.startswith("4")):
            continue

        # 过滤 ST
        info = stock_basic.get(short, {})
        name = info.get("name", "")
        if "ST" in str(name).upper() or "*ST" in str(name):
            continue

        # 过滤上市不满60日
        list_date_str = str(info.get("list_date", ""))
        if list_date_str:
            try:
                list_dt = datetime.strptime(list_date_str, "%Y%m%d")
                trade_dt = datetime.strptime(trade_date, "%Y%m%d")
                if (trade_dt - list_dt).days < 60:
                    continue
            except ValueError:
                pass

        # 过滤流通市值<5亿 或 换手率<2%
        db_row = db_cache.get((trade_date, code), {})
        circ_mv = _safe_float(db_row.get("circ_mv", 0))
        if circ_mv and circ_mv < 50000:
            continue
        turnover = (_safe_float(db_row.get("turnover_rate_f", 0)) or
                    _safe_float(db_row.get("turnover_rate", 0)))
        if turnover and turnover < 2:
            continue

        # 过滤一字板
        if pct >= 9.9 and turnover < 0.5:
            continue
        if pct <= -9.9 and turnover < 0.5:
            continue

        candidates.append({
            "code": code,
            "name": name,
            "pct_chg": pct,
            "short": short,
        })

    # 按涨幅降序，取 top_n
    candidates.sort(key=lambda x: x["pct_chg"], reverse=True)
    candidates = candidates[:top_n]

    return candidates


# ═══════════════════════════════════════════════════════════════════
# 评分适配器
# ═══════════════════════════════════════════════════════════════════

def _setup_backtest_env(provider: BacktestDataProvider, trade_date: str):
    """设置回测环境：拦截 call_tushare + 模拟实时缓存"""
    import scripts.tu_share as tu_module
    import plays.limit_up.pipeline as pl_module

    # 保存原始函数
    orig_call = tu_module.call_tushare
    orig_fund_cache = pl_module._get_realtime_fund_cache
    orig_clear = tu_module.clear_tushare_cache

    # 替换 call_tushare
    tu_module.call_tushare = lambda api, params, fields="", timeout=10: \
        provider.call(api, params, fields, timeout)

    # 替换 clear_tushare_cache 为空操作
    tu_module.clear_tushare_cache = lambda: None

    # 模拟实时资金流缓存（从预取数据构建）
    def _mock_fund_cache():
        """用 pre-fetched daily_basic + moneyflow 模拟实时资金流"""
        cache = {}
        daily = provider.prefetched.get("daily", {})
        db = provider.prefetched.get("daily_basic", {})
        mf = provider.prefetched.get("moneyflow", {})
        prev = provider.prev_date

        # 收集上一个交易日有数据的所有股票
        seen = set()
        for (d, code), row in mf.items():
            if d == prev and code not in seen:
                seen.add(code)
                short = _code_short(code)
                db_row = db.get((prev, code), {})
                cache[short] = {
                    "net_flow": _safe_float(row.get("net_mf_amount", 0)) * 10000,
                    "vol_ratio": _safe_float(db_row.get("volume_ratio", 0)),
                    "turnover": _safe_float(db_row.get("turnover_rate", 0)),
                    "amount": _safe_float(db_row.get("amount", 0)) * 1000,
                }
        # 补充 daily 中有但 moneyflow 中无的
        for (d, code), row in daily.items():
            if d == prev:
                short = _code_short(code)
                if short not in cache:
                    db_row = db.get((prev, code), {})
                    cache[short] = {
                        "net_flow": 0,
                        "vol_ratio": _safe_float(db_row.get("volume_ratio", 0)),
                        "turnover": _safe_float(db_row.get("turnover_rate", 0)),
                        "amount": _safe_float(row.get("amount", 0)) * 1000,
                    }
        return cache

    pl_module._get_realtime_fund_cache = _mock_fund_cache

    # 模拟同花顺实时行情缓存
    def _mock_ths_quote_cache():
        cache = {}
        prev = provider.prev_date
        daily = provider.prefetched.get("daily", {})
        db = provider.prefetched.get("daily_basic", {})
        for (d, code), row in daily.items():
            if d == prev:
                short = _code_short(code)
                db_row = db.get((prev, code), {})
                cache[short] = {
                    "pct_chg": _safe_float(row.get("pct_chg", 0)),
                    "price": _safe_float(row.get("close", 0)),
                    "turnover": _safe_float(db_row.get("turnover_rate", 0)),
                    "vol_ratio": _safe_float(db_row.get("volume_ratio", 0)),
                    "amount": _safe_float(row.get("amount", 0)),
                }
        return cache

    pl_module._THS_QUOTE_CACHE = _mock_ths_quote_cache()
    pl_module._REALTIME_PCT_CACHE = {k: v.get("pct_chg", 0)
                                      for k, v in pl_module._THS_QUOTE_CACHE.items()}
    pl_module._REALTIME_PCT_TS = provider.prev_date

    # 清空其他实时缓存
    pl_module._POPULARITY_RANK_CACHE.clear()
    pl_module._HOT_CONCEPT_CACHE.clear()
    pl_module._HOT_LIST_ITEMS.clear()
    pl_module._REALTIME_FUND_CACHE.clear()
    pl_module._REALTIME_FUND_TS = ""

    # 拦截 _fetch_ths_hot_list 防止回测中发起 HTTP 请求
    pl_module._fetch_ths_hot_list = lambda: None

    # 拦截 is_trading_time 返回 False（回测场景始终视为盘后）
    import plays.limit_up.utils as utils_module
    utils_module.is_trading_time = lambda: False

    return orig_call, orig_fund_cache, orig_clear


def _restore_env(orig_call, orig_fund_cache, orig_clear):
    """恢复原始环境"""
    import scripts.tu_share as tu_module
    import plays.limit_up.pipeline as pl_module
    import plays.limit_up.utils as utils_module

    tu_module.call_tushare = orig_call
    tu_module.clear_tushare_cache = orig_clear
    pl_module._get_realtime_fund_cache = orig_fund_cache
    pl_module._REALTIME_PCT_CACHE.clear()
    pl_module._THS_QUOTE_CACHE.clear()
    pl_module._REALTIME_FUND_CACHE.clear()
    utils_module.is_trading_time = lambda: __import__('plays.limit_up.utils', fromlist=['is_trading_time']).is_trading_time


def score_stock(code: str, trade_date: str, provider: BacktestDataProvider,
                weights: dict[str, float] | None = None) -> dict | None:
    """对单只股票在回测日期 T 进行五维度评分。

    使用 T-1 数据（通过 provider 保证），模拟 pipeline 的评分逻辑。
    返回: {code, name, scores, reasons, total, ...} 或 None
    """
    cache_key = (code, str(trade_date))
    if cache_key in _SCORE_CACHE:
        return _SCORE_CACHE[cache_key]

    orig_call, orig_fund_cache, orig_clear = _setup_backtest_env(provider, trade_date)

    try:
        # 并行五维评分
        from concurrent.futures import ThreadPoolExecutor, as_completed

        scoring_funcs = {}
        try:
            from plays.limit_up.strategies.fundamental import score_fundamental
            scoring_funcs["fundamental"] = score_fundamental
        except ImportError:
            pass
        try:
            from plays.limit_up.strategies.technical import score_technical
            scoring_funcs["technical"] = score_technical
        except ImportError:
            pass
        try:
            from plays.limit_up.strategies.fundflow import score_fundflow
            scoring_funcs["fundflow"] = score_fundflow
        except ImportError:
            pass
        try:
            from plays.limit_up.strategies.sentiment import score_sentiment
            scoring_funcs["sentiment"] = score_sentiment
        except ImportError:
            pass
        try:
            from plays.limit_up.strategies.shortterm import score_shortterm
            scoring_funcs["shortterm"] = score_shortterm
        except ImportError:
            pass

        scores = {}
        reasons = {}
        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = {pool.submit(fn, code): name
                       for name, fn in scoring_funcs.items()}
            for future in as_completed(futures):
                dim = futures[future]
                try:
                    s, r = future.result(timeout=30)
                    scores[dim] = s
                    reasons[dim] = r
                except Exception as e:
                    scores[dim] = 0
                    reasons[dim] = f"异常: {e}"

        # 计算综合评分（top-3 weighted average）
        if weights is None:
            weights = {
                "fundamental": 1.5, "technical": 1.0,
                "fundflow": 1.0, "sentiment": 1.2, "shortterm": 1.5,
            }

        dc = [(scores.get(d, 0), weights.get(d, 1.0)) for d in DIMS]
        dc.sort(key=lambda x: x[0] * x[1], reverse=True)
        top3 = dc[:3]
        total = (sum(s * w for s, w in top3) /
                 sum(w for _, w in top3) if sum(w for _, w in top3) > 0 else 0)

        rc = sum(1 for d in DIMS if scores.get(d, 0) >= 75)

        result = {
            "code": code,
            "scores": {d: scores.get(d, 0) for d in DIMS},
            "reasons": {d: reasons.get(d, "") for d in DIMS},
            "total": round(total, 1),
            "resonance": {"count": rc, "threshold": 75, "is_resonance": rc >= 3},
        }

    except Exception as e:
        result = {
            "code": code,
            "scores": {d: 0 for d in DIMS},
            "reasons": {d: f"评分失败: {e}" for d in DIMS},
            "total": 0,
            "resonance": {"count": 0, "threshold": 75, "is_resonance": False},
        }
    finally:
        _restore_env(orig_call, orig_fund_cache, orig_clear)

    _SCORE_CACHE[cache_key] = result
    return result


# ═══════════════════════════════════════════════════════════════════
# 回测主循环
# ═══════════════════════════════════════════════════════════════════

class LimitUpBacktest:
    """涨停预测回测框架"""

    def __init__(self, days: int = 10, top_n: int = 150,
                 end_date: str | None = None):
        self.days = days
        self.top_n = top_n
        self.end_date = end_date or datetime.now().strftime("%Y%m%d")
        self.trade_dates: list[str] = []
        self.prev_trade_dates: dict[str, str] = {}  # T → T-1
        self.prefetched: dict = {}
        self.stock_basic: dict[str, dict] = {}
        self._per_stock_data: dict[str, dict] = {}  # code_short → {api: {fields, items}}
        self.candidates: dict[str, list[dict]] = {}
        self.scores: dict[str, list[dict]] = {}
        self.limit_up_set: set[tuple[str, str]] = set()  # {(date, code)}
        self.metrics: dict = {}

    def fetch_trade_dates(self):
        """获取最近 N 个交易日"""
        from scripts.tu_share import call_tushare

        start = (datetime.strptime(self.end_date, "%Y%m%d") -
                 timedelta(days=max(40, self.days * 3)))
        resp = call_tushare("trade_cal", {
            "exchange": "SSE",
            "start_date": start.strftime("%Y%m%d"),
            "end_date": self.end_date
        }, "cal_date,is_open,pretrade_date")
        items = resp.get("data", {}).get("items", [])

        all_dates = []
        for item in items:
            if len(item) >= 2 and item[1] == 1:
                all_dates.append(item[0])
        all_dates.sort()

        # 取最近 N 个（需要 N+1 来做 T-1 对照）
        self.trade_dates = all_dates[-(self.days + 1):]

        # 构建 T → T-1 映射
        for i in range(1, len(self.trade_dates)):
            self.prev_trade_dates[self.trade_dates[i]] = self.trade_dates[i - 1]

        # 去掉第一个（仅用于 T-1 对照，不作为回测日）
        self.trade_dates = self.trade_dates[1:]

        print(f"交易日: {len(self.trade_dates)} 天 [{self.trade_dates[0]} → {self.trade_dates[-1]}]")

    def pre_fetch(self, use_cache: bool = True):
        """预取全量数据"""
        all_dates = sorted(set(list(self.trade_dates) +
                               list(self.prev_trade_dates.values())))
        self.prefetched = pre_fetch_bulk_data(all_dates, BACKTEST_DIR, use_cache)
        self.stock_basic = build_stock_basic_lookup(self.prefetched)

    def build_pools(self):
        """为每个交易日构建候选池"""
        for d in self.trade_dates:
            pool = build_candidate_pool(d, self.prefetched, self.stock_basic,
                                        self.top_n)
            self.candidates[d] = pool
            # 记录涨停标签
            ll_cache = self.prefetched.get("limit_list_d", {})
            for (dd, code, ltype), row in ll_cache.items():
                if dd == d and ltype == "U":
                    self.limit_up_set.add((d, code))

            print(f"  {d}: {len(pool)}只候选, 涨停{sum(1 for c in pool if (d, c['code']) in self.limit_up_set)}只")

        # ── 收集所有候选股唯一代码 ──
        all_codes = set()
        for d in self.trade_dates:
            for c in self.candidates[d]:
                all_codes.add(c["code"])
        print(f"\n逐股预取: {len(all_codes)} 只唯一候选股...")

        # 预取 stk_factor_pro（技术面核心数据源）
        # 限制数量以控制积分消耗和超时风险
        n_unique = min(len(all_codes), 100)
        self._prefetch_per_stock_batch(all_codes, "stk_factor_pro",
            "trade_date,close,open,high,low,pre_close,change,pct_change,"
            "vol,amount,vol_ratio,turnover_rate,"
            "ma_bfq_5,ma_bfq_10,ma_bfq_20,ma_bfq_60,"
            "macd_dif_bfq,macd_dea_bfq,macd_bfq,"
            "kdj_k_bfq,kdj_d_bfq,"
            "rsi_bfq_6,boll_upper_bfq,boll_mid_bfq,boll_lower_bfq",
            max_stocks=n_unique)

        # concept_detail 接口需要更高权限（当前返回 40101），跳过
        # 策略层已有异常处理，取不到概念数据时降级为基础评分

    def _prefetch_per_stock_batch(self, all_codes: set, api_name: str,
                                   fields: str, max_stocks: int = 200):
        """批量预取逐股数据（限制数量以控制积分消耗）"""
        from scripts.tu_share import call_tushare, clear_tushare_cache

        codes = sorted(all_codes)[:max_stocks]
        success = 0
        batch_start = time.time()
        for i, code in enumerate(codes):
            try:
                clear_tushare_cache()
                tmo = 30 if api_name == "stk_factor_pro" else 15
                resp = call_tushare(api_name, {"ts_code": code}, fields, timeout=tmo)
                items = resp.get("data", {}).get("items", [])
                flds = resp.get("data", {}).get("fields", [])
                if items:
                    short = _code_short(code)
                    if short not in self._per_stock_data:
                        self._per_stock_data[short] = {}
                    self._per_stock_data[short][api_name] = {
                        "fields": flds, "items": items}
                    success += 1
            except Exception:
                pass
            if (i + 1) % 20 == 0:
                elapsed = time.time() - batch_start
                print(f"  {api_name}: {i+1}/{len(codes)} ({success} ok, {elapsed:.0f}s)",
                      flush=True)
        elapsed = time.time() - batch_start
        print(f"  {api_name}: {success}/{len(codes)} 成功 ({elapsed:.0f}s)")

    def run(self):
        """执行回测主循环"""
        global _SCORE_CACHE, _API_CALL_COUNT
        _SCORE_CACHE.clear()
        _API_CALL_COUNT = 0

        total_stocks = sum(len(v) for v in self.candidates.values())
        scored_count = 0
        start_time = time.time()

        for d in self.trade_dates:
            pool = self.candidates[d]
            prev = self.prev_trade_dates.get(d, d)
            provider = BacktestDataProvider(d, self.prefetched, self.stock_basic, prev,
                                            per_stock_data=self._per_stock_data)

            print(f"\n{d} ({len(pool)}只候选):", end="", flush=True)
            results = []
            for i, stock in enumerate(pool):
                code = stock["code"]
                try:
                    r = score_stock(code, d, provider)
                    if r:
                        r["name"] = stock.get("name", "")
                        r["pct_chg"] = stock.get("pct_chg", 0)
                        is_hit = (d, code) in self.limit_up_set
                        r["is_hit"] = is_hit
                        results.append(r)
                except Exception as e:
                    pass  # 单只失败不影响整体

                scored_count += 1
                if (i + 1) % 30 == 0:
                    elapsed = time.time() - start_time
                    rate = scored_count / elapsed if elapsed > 0 else 0
                    eta = (total_stocks - scored_count) / rate if rate > 0 else 0
                    print(f"\n  [{i+1}/{len(pool)}] {elapsed:.0f}s elapsed, "
                          f"ETA {eta:.0f}s", end="", flush=True)

            self.scores[d] = results
            hits_today = sum(1 for r in results if r.get("is_hit"))
            print(f" → {len(results)}只评分完成, 命中{hits_today}只")

        elapsed = time.time() - start_time
        print(f"\n总耗时: {elapsed:.0f}s, API调用: {_API_CALL_COUNT}, "
              f"评分: {scored_count} 次")

    def evaluate(self) -> dict:
        """计算回测指标"""
        all_pairs = []
        for d in self.trade_dates:
            for r in self.scores.get(d, []):
                all_pairs.append({
                    "date": d,
                    "code": r["code"],
                    "name": r.get("name", ""),
                    "total": r["total"],
                    "scores": r["scores"],
                    "is_hit": r.get("is_hit", False),
                    "pct_chg": r.get("pct_chg", 0),
                })

        if not all_pairs:
            return {"error": "no data"}

        totals = np.array([p["total"] for p in all_pairs])
        hits = np.array([1 if p["is_hit"] else 0 for p in all_pairs])
        n_hits = int(hits.sum())
        n_total = len(all_pairs)

        metrics = {
            "n_days": len(self.trade_dates),
            "n_stocks_scored": n_total,
            "n_actual_limit_ups": n_hits,
            "limit_up_rate": round(n_hits / n_total, 4) if n_total > 0 else 0,
            "score_distribution": {
                "mean": float(np.mean(totals)),
                "std": float(np.std(totals)),
                "min": float(np.min(totals)),
                "p25": float(np.percentile(totals, 25)),
                "p50": float(np.percentile(totals, 50)),
                "p75": float(np.percentile(totals, 75)),
                "p90": float(np.percentile(totals, 90)),
                "max": float(np.max(totals)),
            },
        }

        # Precision@K, Recall@K
        sorted_idx = np.argsort(-totals)
        for k in [5, 10, 20, 50, 100]:
            top_k_idx = sorted_idx[:min(k, n_total)]
            hits_in_top = int(hits[top_k_idx].sum())
            metrics[f"precision@{k}"] = round(hits_in_top / min(k, n_total), 4)
            metrics[f"recall@{k}"] = round(
                hits_in_top / n_hits, 4) if n_hits > 0 else 0

        # AUC
        try:
            from sklearn.metrics import roc_auc_score
            metrics["auc"] = round(float(roc_auc_score(hits, totals)), 4)
        except ImportError:
            # 手动计算简单 AUC（用 percentiles 近似）
            pass

        # 推送模拟（总分 ≥ 35）
        pushed = [p for p in all_pairs if p["total"] >= 35]
        pushed_hits = sum(1 for p in pushed if p["is_hit"])
        metrics["push_simulation"] = {
            "threshold": 35,
            "pushed_count": len(pushed),
            "pushed_hits": pushed_hits,
            "push_hit_rate": round(pushed_hits / len(pushed), 4) if pushed else 0,
            "push_win_rate": round(
                sum(1 for p in pushed if p["pct_chg"] > 0) / len(pushed), 4
            ) if pushed else 0,
        }

        # 推送模拟（总分 ≥ 45 — 高确信度）
        pushed_high = [p for p in all_pairs if p["total"] >= 45]
        ph_hits = sum(1 for p in pushed_high if p["is_hit"])
        metrics["push_simulation_high"] = {
            "threshold": 45,
            "pushed_count": len(pushed_high),
            "pushed_hits": ph_hits,
            "push_hit_rate": round(ph_hits / len(pushed_high), 4) if pushed_high else 0,
        }

        # 推送模拟（总分 ≥ 55 — 极高确信度）
        pushed_vhigh = [p for p in all_pairs if p["total"] >= 55]
        pvh_hits = sum(1 for p in pushed_vhigh if p["is_hit"])
        metrics["push_simulation_vhigh"] = {
            "threshold": 55,
            "pushed_count": len(pushed_vhigh),
            "pushed_hits": pvh_hits,
            "push_hit_rate": round(pvh_hits / len(pushed_vhigh), 4) if pushed_vhigh else 0,
        }

        # 每维度 Cohen's d
        for dim in DIMS:
            dim_scores = np.array([p["scores"].get(dim, 0) for p in all_pairs])
            hit_mask = hits == 1
            miss_mask = hits == 0
            hit_mean = float(dim_scores[hit_mask].mean()) if hit_mask.any() else 0
            miss_mean = float(dim_scores[miss_mask].mean()) if miss_mask.any() else 0
            hit_std = float(dim_scores[hit_mask].std()) if hit_mask.any() else 0
            miss_std = float(dim_scores[miss_mask].std()) if miss_mask.any() else 0
            pooled_std = np.sqrt((hit_std ** 2 + miss_std ** 2) / 2) if (hit_std + miss_std) > 0 else 1
            d = (hit_mean - miss_mean) / pooled_std if pooled_std > 0 else 0

            # Point-biserial correlation
            corr = float(np.corrcoef(dim_scores, hits)[0, 1]) if len(dim_scores) > 1 else 0

            metrics[f"{dim}_cohens_d"] = round(d, 4)
            metrics[f"{dim}_correlation"] = round(corr, 4)
            metrics[f"{dim}_hit_mean"] = round(hit_mean, 2)
            metrics[f"{dim}_miss_mean"] = round(miss_mean, 2)

        self.metrics = metrics
        return metrics

    def save_results(self):
        """保存回测结果"""
        # 完整结果
        results = {
            "meta": {
                "run_time": datetime.now().isoformat(),
                "trade_dates": self.trade_dates,
                "days": self.days,
                "top_n": self.top_n,
            },
            "metrics": self.metrics,
            "by_date": {d: sorted(self.scores.get(d, []),
                                  key=lambda x: x["total"], reverse=True)
                        for d in self.trade_dates},
        }
        path = BACKTEST_DIR / "backtest_results.json"
        with open(path, "w") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        print(f"\n回测结果已保存: {path}")

        # 指标摘要
        path2 = BACKTEST_DIR / "backtest_metrics.json"
        with open(path2, "w") as f:
            json.dump(self.metrics, f, ensure_ascii=False, indent=2)
        print(f"回测指标已保存: {path2}")

    def print_report(self):
        """打印回测报告"""
        m = self.metrics
        if not m:
            return
        print("\n" + "=" * 60)
        print("回测报告")
        print("=" * 60)
        print(f"交易日: {m['n_days']}天")
        print(f"评分股票: {m['n_stocks_scored']}只次")
        print(f"实际涨停: {m['n_actual_limit_ups']}只次 ({m['limit_up_rate']:.1%})")
        print(f"\n评分分布: mean={m['score_distribution']['mean']:.1f}, "
              f"median={m['score_distribution']['p50']:.1f}, "
              f"max={m['score_distribution']['max']:.1f}")
        print(f"\nPrecision@K:")
        for k in [5, 10, 20, 50]:
            print(f"  @{k:>3}: {m.get(f'precision@{k}', 0):.2%}", end="")
            if m.get(f"recall@{k}", 0) > 0:
                print(f"  (recall {m[f'recall@{k}']:.2%})")
            else:
                print()

        if "auc" in m:
            print(f"\nAUC: {m['auc']:.4f}")

        print(f"\n推送模拟 (threshold=35):")
        ps = m.get("push_simulation", {})
        print(f"  推送{ps.get('pushed_count',0)}只 → 命中{ps.get('pushed_hits',0)}只 "
              f"(命中率{ps.get('push_hit_rate',0):.1%})")

        print(f"\n维度区分力 (Cohen's d):")
        for dim in DIMS:
            d_val = m.get(f"{dim}_cohens_d", 0)
            corr = m.get(f"{dim}_correlation", 0)
            hit_m = m.get(f"{dim}_hit_mean", 0)
            miss_m = m.get(f"{dim}_miss_mean", 0)
            bar = "█" * max(0, int(d_val * 20)) if d_val > 0 else "░" * max(0, int(-d_val * 20))
            print(f"  {dim:>12}: d={d_val:+.4f} corr={corr:+.4f} "
                  f"hit={hit_m:.1f} miss={miss_m:.1f} {bar}")


# ═══════════════════════════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="涨停预测回测框架")
    parser.add_argument("--days", type=int, default=10,
                        help="回测交易日数（默认10）")
    parser.add_argument("--top", type=int, default=150,
                        help="每日候选股数上限（默认150）")
    parser.add_argument("--end-date", default=None,
                        help="回测截止日期（YYYYMMDD，默认今天）")
    parser.add_argument("--no-cache", action="store_true",
                        help="不使用磁盘缓存，强制重新拉取")
    parser.add_argument("--skip-scoring", action="store_true",
                        help="仅预取数据，跳过评分（调试用）")
    args = parser.parse_args()

    bt = LimitUpBacktest(days=args.days, top_n=args.top, end_date=args.end_date)

    print("=" * 60)
    print(f"涨停预测回测: {args.days}天 x Top{args.top}只/天")
    print("=" * 60)

    print("\n[1/4] 获取交易日列表...")
    bt.fetch_trade_dates()

    print("\n[2/4] 预取数据...")
    bt.pre_fetch(use_cache=not args.no_cache)

    print("\n[3/4] 构建候选池...")
    bt.build_pools()

    if args.skip_scoring:
        print("\n[跳过] --skip-scoring 模式，仅预取数据")
        return

    print(f"\n[4/4] 执行回测...")
    bt.run()

    print("\n[评估] 计算指标...")
    bt.evaluate()

    bt.print_report()
    bt.save_results()


if __name__ == "__main__":
    main()
