#!/usr/bin/env python3
"""
快速因子回测 — jvQuant资金流向 + Tushare日线数据

核心流程:
  1. 从 tushare 磁盘缓存加载过去 N 个交易日的日线/日基本/涨停数据
  2. 从 jvQuant 批量拉取资金流向数据（主力/大单/中单/小单净额）
  3. 计算每个因子（技术面/基本面/情绪面/资金面/短线博弈）与次日涨停的相关性
  4. 输出因子排序 + 优化建议

用法:
  python plays/limit_up/backtest_v2.py --days 10 --top 100

输出:
  data/backtest/factor_correlation.json  — 因子排序
  data/backtest/factor_analysis.md      — 优化建议
"""

import argparse
import json
import sys
import time
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

# ═══════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════

def _safe_float(val, default=0.0):
    if val is None: return default
    try: return float(str(val).replace(",", "").replace("%", ""))
    except (ValueError, TypeError): return default


def _code_short(code: str) -> str:
    return code.replace(".SH", "").replace(".SZ", "").replace(".BJ", "")


# ═══════════════════════════════════════════════════════
# 数据加载
# ═══════════════════════════════════════════════════════

def load_tushare_cache() -> dict:
    """从磁盘缓存加载 tushare 预取数据"""
    cache_file = BACKTEST_DIR / "bulk_cache.json"
    if not cache_file.exists():
        print(f"错误: 缓存文件不存在 {cache_file}")
        print("请先运行: python plays/limit_up/backtest.py --days 10 --skip-scoring")
        sys.exit(1)

    print(f"加载 tushare 缓存: {cache_file}")
    with open(cache_file) as f:
        cached = json.load(f)

    print(f"  日期: {cached.get('_dates', [])}")

    result = {}
    for api, data in cached.items():
        if api == "_dates": continue
        if api in ("trade_cal", "stock_basic"):
            result[api] = data
        else:
            result[api] = {}
            for k, v in data.items():
                parts = k.split("|", 2)
                if len(parts) == 2:
                    result[api][(parts[0], parts[1])] = v
                elif len(parts) == 3:
                    result[api][(parts[0], parts[1], parts[2])] = v

    return result


def build_lookups(cache: dict) -> tuple:
    """构建 stock_basic 查找表 + 交易日列表"""
    # stock_basic
    sb = cache.get("stock_basic", {})
    sb_items = sb.get("data", {}).get("items", [])
    sb_fields = sb.get("data", {}).get("fields", [])
    stock_basic = {}
    if sb_fields and sb_items:
        for item in sb_items:
            d = dict(zip(sb_fields, item))
            code = d.get("ts_code", "")
            if code:
                stock_basic[_code_short(code)] = d

    # trade_cal
    tc = cache.get("trade_cal", {})
    tc_items = tc.get("data", {}).get("items", [])
    trade_dates = [item[0] for item in tc_items if len(item) >= 2 and item[1] == 1]
    trade_dates.sort()

    return stock_basic, trade_dates


# ═══════════════════════════════════════════════════════
# 候选池构建
# ═══════════════════════════════════════════════════════

def build_pools(cache: dict, stock_basic: dict, trade_dates: list[str],
                all_dates: list[str], top_n: int = 100) -> dict[str, list[dict]]:
    """构建每日候选池 + 次日涨停标签（NEXT day limit-up as target）

    核心改变：候选池基于 T 日涨幅≥2%的股票，但标签是 T+1 日是否涨停。
    这样才能评估"今天选股 → 明天涨停"的预测效果。
    """
    daily = cache.get("daily", {})
    db = cache.get("daily_basic", {})
    limlist = cache.get("limit_list_d", {})

    pools = {}
    # 构建 T+1 日涨停集合
    next_limit_up_set = set()
    date_sort = sorted(all_dates)
    for i, d in enumerate(date_sort):
        next_d = date_sort[i + 1] if i + 1 < len(date_sort) else None
        if next_d is None: continue
        for (ld, code, ltype), row in limlist.items():
            if ld == next_d and ltype == "U":
                next_limit_up_set.add((d, code))  # keyed by T, matched to T+1

    for d in trade_dates:
        candidates = []
        for (dd, code), row in daily.items():
            if dd != d: continue
            pct = _safe_float(row.get("pct_chg", 0))
            if pct < 2 or pct > 9.5: continue
            short = _code_short(code)
            if not short: continue
            if (short.startswith("30") or short.startswith("688") or
                    short.startswith("8") or short.startswith("4")):
                continue
            info = stock_basic.get(short, {})
            name = info.get("name", "")
            if "ST" in str(name).upper(): continue
            list_date = str(info.get("list_date", ""))
            if list_date:
                try:
                    ld = datetime.strptime(list_date, "%Y%m%d")
                    td = datetime.strptime(d, "%Y%m%d")
                    if (td - ld).days < 60: continue
                except: pass
            db_row = db.get((d, code), {})
            circ_mv = _safe_float(db_row.get("circ_mv", 0))
            if circ_mv and circ_mv < 50000: continue
            turnover = (_safe_float(db_row.get("turnover_rate_f", 0)) or
                        _safe_float(db_row.get("turnover_rate", 0)))
            if turnover and turnover < 2: continue
            # 标签：次日是否涨停
            next_hit = (d, code) in next_limit_up_set
            candidates.append({
                "code": code, "name": name, "pct_chg": pct,
                "short": short, "is_limit_up": next_hit,
                "close": _safe_float(row.get("close", 0)),
                "open": _safe_float(row.get("open", 0)),
                "high": _safe_float(row.get("high", 0)),
                "low": _safe_float(row.get("low", 0)),
                "pre_close": _safe_float(row.get("pre_close", 0)),
                "vol": _safe_float(row.get("vol", 0)),
                "amount": _safe_float(row.get("amount", 0)),
                "circ_mv": circ_mv,
                "turnover": turnover,
                "volume_ratio": _safe_float(db_row.get("volume_ratio", 0)),
                "pe": _safe_float(db_row.get("pe", 0)),
                "pb": _safe_float(db_row.get("pb", 0)),
            })
        candidates.sort(key=lambda x: x["pct_chg"], reverse=True)
        pools[d] = candidates[:top_n]
        hits = sum(1 for c in candidates[:top_n] if c["is_limit_up"])
        print(f"  {d}: {len(candidates[:top_n])}只候选, 次日涨停{hits}只")

    return pools


# ═══════════════════════════════════════════════════════
# jvQuant 资金流向批量拉取
# ═══════════════════════════════════════════════════════

def fetch_jvquant_fundflow(pools: dict[str, list[dict]],
                            trade_dates: list[str]) -> dict[tuple, dict]:
    """逐股拉取 jvQuant 资金流向（候选池中的每只股票）

    jvQuant batch query 只返回 top 100 只，候选池覆盖率低。
    改为对每个候选股的 short code 做单股查询，确保全覆盖。
    """
    from scripts.jvquant_client import JvQuantClient

    client = JvQuantClient()
    fundflow = {}

    # 收集所有唯一的 (date, short_code) 对
    all_pairs = set()
    for d in trade_dates:
        for stock in pools.get(d, []):
            all_pairs.add((d, stock["short"]))

    print(f"  共 {len(all_pairs)} 个 (date, code) 对需要查询")
    success = 0
    t0 = time.time()

    for i, (d, short) in enumerate(sorted(all_pairs)):
        try:
            date_str = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
            ff = client.get_fundflow_single(short, date_str)
            if ff.get("code"):
                fundflow[(d, short)] = ff
                success += 1
        except Exception:
            pass

        if (i + 1) % 50 == 0:
            elapsed = time.time() - t0
            rate = (i + 1) / elapsed if elapsed > 0 else 0
            eta = (len(all_pairs) - i - 1) / rate if rate > 0 else 0
            print(f"  [{i+1}/{len(all_pairs)}] {success} ok, "
                  f"{elapsed:.0f}s elapsed, ETA {eta:.0f}s", flush=True)

        time.sleep(0.15)  # 控制请求频率

    elapsed = time.time() - t0
    print(f"  jvQuant 完成: {success}/{len(all_pairs)} ({elapsed:.0f}s)")
    return fundflow


# ═══════════════════════════════════════════════════════
# 因子计算
# ═══════════════════════════════════════════════════════

def compute_factors(pools: dict[str, list[dict]],
                    cache: dict,
                    fundflow: dict[tuple, dict],
                    trade_dates: list[str],
                    prev_dates: dict[str, str]) -> list[dict]:
    """计算所有因子值 + 涨停标签，返回扁平化的 stock-day 对"""

    daily = cache.get("daily", {})
    db = cache.get("daily_basic", {})
    mf = cache.get("moneyflow", {})
    limstep = cache.get("limit_step", {})
    limlist = cache.get("limit_list_d", {})

    rows = []

    for d in trade_dates:
        prev = prev_dates.get(d, d)
        pool = pools.get(d, [])
        for stock in pool:
            code = stock["code"]
            short = stock["short"]

            # ── 获取 T-1 数据 ──
            prev_daily = daily.get((prev, code), {})
            prev_db = db.get((prev, code), {})
            prev_mf = mf.get((prev, code), {})
            # jvQuant 资金流向：先按完整 code 查，再按 short code 查
            ff = fundflow.get((d, code), fundflow.get((d, short), {}))

            close = stock["close"]
            prev_close = _safe_float(prev_daily.get("close", 0))
            prev_pct = _safe_float(prev_daily.get("pct_chg", 0))

            # ── 因子定义 ──
            factors = {
                "date": d,
                "code": code,
                "name": stock["name"],
                "is_limit_up": 1 if stock["is_limit_up"] else 0,
                "pct_chg": stock["pct_chg"],
            }

            # === 资金面因子（jvQuant 主力/大单/中单/小单） ===
            ff_main = _safe_float(ff.get("main_net", 0))  # 万元
            ff_big = _safe_float(ff.get("big_net", 0))
            ff_mid = _safe_float(ff.get("mid_net", 0))
            ff_small = _safe_float(ff.get("small_net", 0))
            ff_turnover = _safe_float(ff.get("turnover", 0))  # %
            ff_vol_ratio = _safe_float(ff.get("vol_ratio", 0))
            ff_pct = _safe_float(ff.get("pct_chg", 0))

            factors["f_main_net"] = ff_main
            factors["f_big_net"] = ff_big
            factors["f_mid_net"] = ff_mid
            factors["f_small_net"] = ff_small
            factors["f_main_big_ratio"] = (ff_main / (abs(ff_big) + 1))  # 主力/大单比
            factors["f_main_vs_small"] = ff_main - ff_small  # 主力-散户差
            # 主力净占比：主力净额 / 成交额（估计）
            factors["f_main_pct"] = (ff_main / (stock["amount"] / 10000 + 1)
                                     if stock["amount"] > 0 else 0)

            # === 技术面因子 ===
            factors["t_turnover"] = stock["turnover"]
            factors["t_vol_ratio"] = stock["volume_ratio"]
            factors["t_circ_mv"] = stock["circ_mv"]
            factors["t_pct_chg"] = stock["pct_chg"]
            factors["t_close_vs_pre"] = (close / stock["pre_close"] - 1
                                         if stock["pre_close"] > 0 else 0) * 100
            # 振幅
            factors["t_amplitude"] = ((stock["high"] - stock["low"]) /
                                      stock["pre_close"] * 100
                                      if stock["pre_close"] > 0 else 0)
            # 实体 vs 影线
            body = abs(close - stock["open"])
            upper_shadow = stock["high"] - max(close, stock["open"])
            lower_shadow = min(close, stock["open"]) - stock["low"]
            factors["t_upper_shadow_ratio"] = (upper_shadow / body if body > 0 else 0)
            factors["t_lower_shadow_ratio"] = (lower_shadow / body if body > 0 else 0)

            # === T-1 日技术面 ===
            factors["t1_pct_chg"] = prev_pct
            factors["t1_turnover"] = _safe_float(prev_db.get("turnover_rate", 0))
            factors["t1_vol_ratio"] = _safe_float(prev_db.get("volume_ratio", 0))

            # === Tushare 资金流 ===
            factors["mf_net_amount"] = _safe_float(prev_mf.get("net_mf_amount", 0))
            factors["mf_buy_elg"] = _safe_float(prev_mf.get("buy_elg_amount", 0))
            factors["mf_sell_elg"] = _safe_float(prev_mf.get("sell_elg_amount", 0))

            # === 连板基因 ===
            step_count = 0
            max_step = 0
            for (sd, sc), srow in limstep.items():
                if sc == code and sd == d:
                    step_count = _safe_float(srow.get("nums", 0))
                    max_step = max(max_step, step_count)
            factors["f_consecutive_boards"] = max_step
            factors["has_limit_dna"] = 1 if max_step >= 2 else 0

            # === 昨日涨停 ===
            is_yesterday_limit = False
            for (ld, lc, lt), lr in limlist.items():
                if lc == code and ld == prev and lt == "U":
                    is_yesterday_limit = True
                    break
            factors["was_limit_yesterday"] = 1 if is_yesterday_limit else 0

            # === 近5日涨幅 ===
            recent_pcts = []
            for (dd, dc), drow in sorted(daily.items()):
                if dc == code and dd <= d:
                    recent_pcts.append(_safe_float(drow.get("pct_chg", 0)))
            recent_5 = recent_pcts[-5:] if len(recent_pcts) >= 5 else recent_pcts
            factors["t_5d_pct_sum"] = sum(recent_5)
            factors["t_5d_positive_days"] = sum(1 for p in recent_5 if p > 0)

            # === 流通市值分位 ===
            factors["t_circ_mv_log"] = np.log10(stock["circ_mv"] + 1) if stock["circ_mv"] > 0 else 0

            rows.append(factors)

    return rows


# ═══════════════════════════════════════════════════════
# 相关性分析
# ═══════════════════════════════════════════════════════

def analyze_factors(rows: list[dict]) -> dict:
    """计算每个因子与次日涨停的相关性"""
    if not rows:
        return {}

    # 分离因子列
    factor_names = [k for k in rows[0].keys()
                    if k not in ("date", "code", "name", "is_limit_up", "pct_chg")]
    n = len(rows)
    y = np.array([r["is_limit_up"] for r in rows])

    results = []
    for fn in factor_names:
        x = np.array([_safe_float(r.get(fn, 0)) for r in rows])
        if np.std(x) < 1e-8:
            continue

        # Point-biserial correlation
        corr = np.corrcoef(x, y)[0, 1] if len(x) > 1 else 0

        # Cohen's d
        hit_mask = y == 1
        miss_mask = y == 0
        hit_mean = x[hit_mask].mean() if hit_mask.any() else 0
        miss_mean = x[miss_mask].mean() if miss_mask.any() else 0
        hit_std = x[hit_mask].std() if hit_mask.any() else 0
        miss_std = x[miss_mask].std() if miss_mask.any() else 0
        pooled = np.sqrt((hit_std**2 + miss_std**2) / 2) if (hit_std + miss_std) > 0 else 1
        d = (hit_mean - miss_mean) / pooled if pooled > 0 else 0

        # 缺失率
        missing_rate = sum(1 for v in x if v == 0) / len(x) if len(x) > 0 else 0

        results.append({
            "factor": fn,
            "dimension": _factor_dim(fn),
            "correlation": round(float(corr), 4),
            "cohens_d": round(float(d), 4),
            "hit_mean": round(float(hit_mean), 2),
            "miss_mean": round(float(miss_mean), 2),
            "missing_rate": round(missing_rate, 3),
            "n_hit": int(hit_mask.sum()),
            "n_miss": int(miss_mask.sum()),
        })

    results.sort(key=lambda x: abs(x["cohens_d"]), reverse=True)
    return {"n_samples": n, "n_hits": int(y.sum()),
            "factors": results}


def _factor_dim(fn: str) -> str:
    if fn.startswith("f_"): return "fundflow"
    if fn.startswith("t_") or fn.startswith("t1_"): return "technical"
    if fn.startswith("mf_"): return "moneyflow_tushare"
    if fn.startswith("s_"): return "sentiment"
    return "other"


# ═══════════════════════════════════════════════════════
# 报告
# ═══════════════════════════════════════════════════════

def print_report(analysis: dict):
    print("\n" + "=" * 70)
    print("因子相关性分析报告")
    print("=" * 70)
    print(f"样本: {analysis.get('n_samples', 0)}条, 涨停: {analysis.get('n_hits', 0)}条")
    print(f"\n{'因子':<28} {'维度':<16} {'Cohen d':>8} {'Corr':>8} {'Hit均值':>10} {'Miss均值':>10} {'缺失率':>8}")
    print("-" * 95)

    for f in analysis.get("factors", []):
        d = f["cohens_d"]
        bar = "█" * min(20, int(abs(d) * 20))
        sign = "+" if d >= 0 else "-"
        print(f"{f['factor']:<28} {f['dimension']:<16} "
              f"{sign}{abs(d):.4f}{bar:<22} "
              f"{f['correlation']:>+8.4f} "
              f"{f['hit_mean']:>10.2f} {f['miss_mean']:>10.2f} "
              f"{f['missing_rate']:>7.1%}")

    # 按维度汇总
    print(f"\n维度汇总:")
    dims = defaultdict(list)
    for f in analysis.get("factors", []):
        dims[f["dimension"]].append(abs(f["cohens_d"]))
    for dim, ds in sorted(dims.items()):
        avg_d = np.mean(ds)
        max_d = max(ds)
        print(f"  {dim:<16}: avg|d|={avg_d:.4f}, max|d|={max_d:.4f}, n={len(ds)}")


# ═══════════════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="因子回测分析")
    parser.add_argument("--days", type=int, default=5, help="回测交易日数")
    parser.add_argument("--top", type=int, default=100, help="每日候选股数")
    parser.add_argument("--skip-jvquant", action="store_true", help="跳过jvQuant资金流向")
    args = parser.parse_args()

    print("=" * 70)
    print(f"因子回测: {args.days}天 x Top{args.top}只/天")
    print("=" * 70)

    # 1. 加载 tushare 缓存
    print("\n[1/5] 加载 tushare 缓存...")
    cache = load_tushare_cache()
    stock_basic, all_dates = build_lookups(cache)

    # 2. 选取交易日
    all_dates.sort()
    trade_dates = all_dates[-args.days:] if len(all_dates) >= args.days else all_dates
    # 构建 T → T-1 映射
    prev_dates = {}
    date_idx = {d: i for i, d in enumerate(all_dates)}
    for d in trade_dates:
        idx = date_idx.get(d, 0)
        prev_dates[d] = all_dates[idx - 1] if idx > 0 else d

    print(f"交易日: {len(trade_dates)}天 [{trade_dates[0]} → {trade_dates[-1]}]")

    # 3. 构建候选池
    print("\n[2/5] 构建候选池...")
    pools = build_pools(cache, stock_basic, trade_dates, all_dates, args.top)

    # 4. jvQuant 资金流向
    fundflow = {}
    if not args.skip_jvquant:
        print("\n[3/5] jvQuant 资金流向...")
        fundflow = fetch_jvquant_fundflow(pools, trade_dates)
    else:
        print("\n[3/5] 跳过 jvQuant 资金流向")

    # 5. 计算因子
    print(f"\n[4/5] 计算因子...")
    rows = compute_factors(pools, cache, fundflow, trade_dates, prev_dates)
    print(f"  {len(rows)} 条 stock-day 对")
    hit_count = sum(1 for r in rows if r["is_limit_up"])
    print(f"  涨停: {hit_count} 条 ({hit_count/max(len(rows),1):.1%})")

    # 6. 因子分析
    print(f"\n[5/5] 因子相关性分析...")
    analysis = analyze_factors(rows)
    print_report(analysis)

    # 保存
    output_file = BACKTEST_DIR / "factor_correlation.json"
    with open(output_file, "w") as f:
        json.dump(analysis, f, ensure_ascii=False, indent=2)
    print(f"\n结果已保存: {output_file}")

    # 保存原始数据供 subagent 使用
    data_file = BACKTEST_DIR / "factor_raw_data.json"
    with open(data_file, "w") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    print(f"原始数据已保存: {data_file}")


if __name__ == "__main__":
    main()
