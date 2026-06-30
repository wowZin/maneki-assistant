#!/usr/bin/env python3
"""
增强版因子回测 — 20天数据 + jvQuant资金流 + 日内形态

从 daily OHLCV 提取日内形态因子（无需jvQuant分钟数据）：
  - 收盘位置: (close-low)/(high-low) — 高位收盘=强势
  - 实体占比: abs(close-open)/(high-low) — 实体大=趋势明确
  - 上下影线比
  - T-1/T-2 滞后特征
  - 5日量价关系: 缩量上涨/放量滞涨

jvQuant 资金流:
  - 主力/大单/中单/小单净额
  - 主力占比 = 主力净额/成交额

用法:
  python plays/limit_up/backtest_v2.py --days 18  # 前18天训练，最后2天验证
  python plays/limit_up/backtest_v2.py --days 18 --skip-jvquant  # 快速模式
"""

import argparse, json, sys, time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_DIR))
sys.path.insert(0, str(PROJECT_DIR / "scripts"))

PLAY_DIR = Path(__file__).resolve().parent
BACKTEST_DIR = PLAY_DIR / "data" / "backtest"


def _sf(val, default=0.0):
    if val is None: return default
    try: return float(str(val).replace(",", "").replace("%", ""))
    except: return default


def _short(code: str) -> str:
    return code.replace(".SH", "").replace(".SZ", "")


# ═══════════════════════════════════════════════════════
# 数据加载
# ═══════════════════════════════════════════════════════

def load_cache():
    f = BACKTEST_DIR / "bulk_cache.json"
    if not f.exists():
        print(f"缓存不存在: {f}，先运行: fetch_data.py"); sys.exit(1)
    with open(f) as fh:
        raw = json.load(fh)
    out = {}
    for api, data in raw.items():
        if api == "_dates": continue
        if api in ("trade_cal", "stock_basic"):
            out[api] = data
        else:
            out[api] = {}
            for k, v in data.items():
                parts = k.split("|", 2)
                if len(parts) == 2: out[api][(parts[0], parts[1])] = v
                elif len(parts) == 3: out[api][(parts[0], parts[1], parts[2])] = v
    return out


def build_lookups(cache):
    sb = cache.get("stock_basic", {})
    items = sb.get("data", {}).get("items", [])
    fields = sb.get("data", {}).get("fields", [])
    stock_basic = {}
    if fields and items:
        for item in items:
            d = dict(zip(fields, item))
            c = d.get("ts_code", "")
            if c: stock_basic[_short(c)] = d

    tc = cache.get("trade_cal", {})
    tc_items = tc.get("data", {}).get("items", [])
    trade_dates = sorted([it[0] for it in tc_items if len(it) >= 2 and it[1] == 1])
    return stock_basic, trade_dates


# ═══════════════════════════════════════════════════════
# 候选池 + 特征
# ═══════════════════════════════════════════════════════

def build_dataset(cache, stock_basic, all_dates, train_dates, top_n=120):
    """构建候选池 + 计算所有因子 + 次日涨停标签"""
    daily = cache.get("daily", {})
    db = cache.get("daily_basic", {})
    mf = cache.get("moneyflow", {})
    limlist = cache.get("limit_list_d", {})
    limstep = cache.get("limit_step", {})

    # 次日涨停标签 map: (T_date, code) → 1 if T+1 涨停
    date_sorted = sorted(all_dates)
    next_lu = set()
    for i, d in enumerate(date_sorted):
        nd = date_sorted[i + 1] if i + 1 < len(date_sorted) else None
        if nd is None: continue
        for (ld, code, lt), _ in limlist.items():
            if ld == nd and lt == "U":
                next_lu.add((d, code))

    rows = []
    for d in train_dates:
        # 构建当日候选池
        candidates = []
        for (dd, code), row in daily.items():
            if dd != d: continue
            pct = _sf(row.get("pct_chg", 0))
            if pct < 0 or pct >= 9.5: continue  # exclude untradable
            short = _short(code)
            if not short: continue
            if short.startswith(("30", "688", "8", "4")): continue
            info = stock_basic.get(short, {})
            name = str(info.get("name", ""))
            if "ST" in name.upper(): continue
            ld = str(info.get("list_date", ""))
            if ld:
                try:
                    if (datetime.strptime(d, "%Y%m%d") - datetime.strptime(ld, "%Y%m%d")).days < 60:
                        continue
                except: pass
            db_row = db.get((d, code), {})
            cmv = _sf(db_row.get("circ_mv", 0))
            if cmv and cmv < 50000: continue
            to = _sf(db_row.get("turnover_rate_f", 0)) or _sf(db_row.get("turnover_rate", 0))
            if to and to < 2: continue
            candidates.append((short, code, name, pct, row, db_row))

        candidates.sort(key=lambda x: x[3], reverse=True)
        candidates = candidates[:top_n]

        # 获取前一日日期
        d_idx = date_sorted.index(d) if d in date_sorted else -1
        prev_d = date_sorted[d_idx - 1] if d_idx > 0 else d

        for short, code, name, pct, row, db_row in candidates:
            close = _sf(row.get("close", 0))
            open_p = _sf(row.get("open", 0))
            high = _sf(row.get("high", 0))
            low = _sf(row.get("low", 0))
            pre_close = _sf(row.get("pre_close", 0))
            vol = _sf(row.get("vol", 0))
            amount = _sf(row.get("amount", 0))

            # === 日内形态因子（从OHLCV计算） ===
            hl_range = high - low
            body = abs(close - open_p)
            upper_shadow = high - max(close, open_p)
            lower_shadow = min(close, open_p) - low

            # 收盘位置: 0=最低, 1=最高
            close_pos = (close - low) / hl_range if hl_range > 0 else 0.5
            # 实体占比
            body_ratio = body / hl_range if hl_range > 0 else 0
            # 上影线比
            upper_ratio = upper_shadow / body if body > 0 else 0
            # 下影线比
            lower_ratio = lower_shadow / body if body > 0 else 0
            # 振幅
            amplitude = (high - low) / pre_close * 100 if pre_close > 0 else 0

            # === T-1 因子 ===
            prev_row = daily.get((prev_d, code), {})
            prev_close = _sf(prev_row.get("close", 0))
            prev_pct = _sf(prev_row.get("pct_chg", 0))
            prev_vol = _sf(prev_row.get("vol", 0))
            prev_db = db.get((prev_d, code), {})
            prev_turnover = _sf(prev_db.get("turnover_rate", 0))
            prev_vol_ratio = _sf(prev_db.get("volume_ratio", 0))

            # 量比变化：当日量比 / T-1量比（>1=加速放量）
            vol_ratio = _sf(db_row.get("volume_ratio", 0))
            vol_accel = vol_ratio / prev_vol_ratio if prev_vol_ratio > 0 else 1.0

            # === Tushare 资金流 ===
            mf_row = mf.get((d, code), {})
            mf_net = _sf(mf_row.get("net_mf_amount", 0))  # 万元
            mf_buy_elg = _sf(mf_row.get("buy_elg_amount", 0))
            mf_sell_elg = _sf(mf_row.get("sell_elg_amount", 0))

            # 主力净占比（%）
            mf_pct = (mf_net * 10000 / amount) * 100 if amount > 0 else 0

            # T-1 资金流
            prev_mf = mf.get((prev_d, code), {})
            prev_mf_net = _sf(prev_mf.get("net_mf_amount", 0))
            # 资金流加速
            mf_accel = mf_net - prev_mf_net

            # === 涨停基因 ===
            max_step = 0
            for (sd, sc), sr in limstep.items():
                if sc == code:
                    max_step = max(max_step, _sf(sr.get("nums", 0)))

            # T-1 是否涨停
            was_limit = 0
            for (ld, lc, lt), _ in limlist.items():
                if lc == code and ld == prev_d and lt == "U":
                    was_limit = 1; break

            # 近5日涨幅
            recent_pcts = []
            for i2 in range(max(0, d_idx - 4), d_idx + 1):
                rd = date_sorted[i2]
                rr = daily.get((rd, code), {})
                recent_pcts.append(_sf(rr.get("pct_chg", 0)))
            pct_5d = sum(recent_pcts[-5:])
            positive_5d = sum(1 for p in recent_pcts[-5:] if p > 0)

            # === 市值因子 ===
            cmv_log = np.log10(cmv + 1) if cmv > 0 else 0
            cmv_yi = cmv / 10000  # 万元→亿元

            # === 标签 ===
            is_hit = 1 if (d, code) in next_lu else 0

            rows.append({
                "date": d, "code": code, "name": name, "short": short,
                "is_hit": is_hit, "pct_chg": pct,
                # 日内形态
                "close_pos": round(close_pos, 4),
                "body_ratio": round(body_ratio, 4),
                "upper_ratio": round(upper_ratio, 2),
                "lower_ratio": round(lower_ratio, 2),
                "amplitude": round(amplitude, 2),
                # 量价
                "vol_ratio": round(vol_ratio, 2),
                "turnover": round(to, 2),
                "vol_accel": round(vol_accel, 2),
                # T-1
                "prev_pct": round(prev_pct, 2),
                "prev_vol_ratio": round(prev_vol_ratio, 2),
                "prev_turnover": round(prev_turnover, 2),
                # 资金流
                "mf_net": round(mf_net, 2),
                "mf_pct": round(mf_pct, 4),
                "mf_buy_elg": round(mf_buy_elg, 2),
                "mf_sell_elg": round(mf_sell_elg, 2),
                "mf_accel": round(mf_accel, 2),  # 资金加速
                # 涨停基因
                "max_step": int(max_step),
                "was_limit": was_limit,
                # 趋势
                "pct_5d": round(pct_5d, 2),
                "positive_5d": positive_5d,
                # 市值
                "cmv_log": round(cmv_log, 2),
                "cmv_yi": round(cmv_yi, 2),
                # 原始值（供后续分析）
                "close": close, "amount": amount,
            })

    return rows


# ═══════════════════════════════════════════════════════
# jvQuant 资金流补充
# ═══════════════════════════════════════════════════════

def enrich_jvquant(rows, days_sample=10):
    """对样本中的 unique stocks 查询 jvQuant 资金流"""
    from scripts.jvquant_client import JvQuantClient
    client = JvQuantClient()

    unique = list(set((r["date"], r["short"]) for r in rows))
    print(f"  jvQuant: {len(unique)} 对 → 采样{days_sample}天前{days_sample*50}对")

    # 只采样最近 N 天
    dates_sorted = sorted(set(r["date"] for r in rows))
    sample_dates = set(dates_sorted[-days_sample:])
    pairs = [(d, s) for d, s in unique if d in sample_dates]
    pairs = pairs[:days_sample * 50]  # 最多500对

    fundflow = {}
    success = 0
    t0 = time.time()
    for i, (d, short) in enumerate(pairs):
        try:
            ds = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
            ff = client.get_fundflow_single(short, ds)
            if ff.get("code"):
                fundflow[(d, short)] = ff
                success += 1
        except: pass
        if (i + 1) % 30 == 0:
            print(f"    [{i+1}/{len(pairs)}] ok={success}", flush=True)
        time.sleep(0.15)

    elapsed = time.time() - t0
    print(f"  jvQuant: {success}/{len(pairs)} 成功 ({elapsed:.0f}s)")

    # 合并
    for r in rows:
        key = (r["date"], r["short"])
        ff = fundflow.get(key, {})
        r["jv_main_net"] = _sf(ff.get("main_net", 0))
        r["jv_big_net"] = _sf(ff.get("big_net", 0))
        r["jv_mid_net"] = _sf(ff.get("mid_net", 0))
        r["jv_small_net"] = _sf(ff.get("small_net", 0))
        r["jv_turnover"] = _sf(ff.get("turnover", 0))
        r["jv_vol_ratio"] = _sf(ff.get("vol_ratio", 0))
        r["jv_has_data"] = 1 if ff else 0

    return rows


# ═══════════════════════════════════════════════════════
# 因子分析
# ═══════════════════════════════════════════════════════

def analyze(rows):
    factor_cols = [k for k in rows[0] if k not in
                   ("date", "code", "name", "short", "is_hit", "close", "amount")]
    y = np.array([r["is_hit"] for r in rows])
    results = []

    for fn in factor_cols:
        x = np.array([_sf(r.get(fn, 0)) for r in rows])
        if np.std(x) < 1e-8: continue

        corr = np.corrcoef(x, y)[0, 1] if len(x) > 1 else 0
        hit = x[y == 1]; miss = x[y == 0]
        hm = hit.mean() if len(hit) else 0
        mm = miss.mean() if len(miss) else 0
        hs, ms = np.std(hit) if len(hit) else 0, np.std(miss) if len(miss) else 0
        pooled = np.sqrt((hs**2 + ms**2) / 2) if (hs + ms) > 0 else 1
        d = (hm - mm) / pooled if pooled > 0 else 0

        results.append({
            "factor": fn, "cohens_d": round(float(d), 4),
            "correlation": round(float(corr), 4),
            "hit_mean": round(float(hm), 2), "miss_mean": round(float(mm), 2),
            "missing": round(sum(1 for v in x if v == 0) / len(x), 3),
        })

    results.sort(key=lambda r: abs(r["cohens_d"]), reverse=True)
    return {"n": len(rows), "hits": int(y.sum()), "factors": results}


# ═══════════════════════════════════════════════════════
# 策略模拟 + 推送优化
# ═══════════════════════════════════════════════════════

def simulate_strategy(rows, top_k=10):
    """用因子加权模拟策略评分，评估命中率"""
    # 基于 top 绝对值因子的简单加权
    weights = {
        "close_pos": 1.0, "was_limit": 2.0, "cmv_log": -0.5,
        "vol_ratio": -0.3, "mf_pct": 0.8, "jv_mid_net": 0.6,
        "pct_5d": 0.4, "amplitude": 0.2, "upper_ratio": -0.3,
        "vol_accel": 0.3, "mf_accel": 0.3, "prev_pct": 0.2,
        "positive_5d": 0.2, "max_step": 0.5, "body_ratio": 0.2,
    }

    scores = []
    for r in rows:
        s = sum(weights.get(k, 0) * _sf(r.get(k, 0)) for k in weights)
        scores.append(s)

    # 按分排序
    idx = np.argsort(-np.array(scores))
    top_idx = idx[:top_k]

    hits_in_top = sum(1 for i in top_idx if rows[i]["is_hit"])
    precision = hits_in_top / top_k

    # 胜率（次日涨幅>0）
    wins = 0
    for i in top_idx:
        pct = rows[i]["pct_chg"]
        wins += 1 if pct > 0 else 0
    win_rate = wins / top_k

    return {"precision@K": round(precision, 4), "win_rate": round(win_rate, 4)}


# ═══════════════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=18, help="训练天数(默认18)")
    ap.add_argument("--top", type=int, default=120, help="每日候选数")
    ap.add_argument("--skip-jvquant", action="store_true")
    ap.add_argument("--validate", type=int, default=2, help="验证天数(默认2)")
    args = ap.parse_args()

    print(f"增强因子回测: {args.days}天训练 + {args.validate}天验证, Top{args.top}/天")

    # 加载
    cache = load_cache()
    stock_basic, all_dates = build_lookups(cache)
    all_dates = [d for d in all_dates if d < "20260630"]  # 排除今天
    print(f"数据: {len(all_dates)}交易日 [{all_dates[0]}→{all_dates[-1]}]")

    # 划分训练/验证
    train_dates = all_dates[-args.days - args.validate:-args.validate] if args.validate else all_dates[-args.days:]
    val_dates = all_dates[-args.validate:] if args.validate else []
    print(f"训练: {len(train_dates)}天 [{train_dates[0]}→{train_dates[-1]}]")
    if val_dates:
        print(f"验证: {len(val_dates)}天 [{val_dates[0]}→{val_dates[-1]}]")

    # 构建数据集
    rows = build_dataset(cache, stock_basic, all_dates, train_dates, args.top)
    print(f"样本: {len(rows)}条, 涨停: {sum(1 for r in rows if r['is_hit'])}条")

    # jvQuant 补充
    if not args.skip_jvquant:
        rows = enrich_jvquant(rows, days_sample=min(10, len(train_dates)))

    # 因子分析
    analysis = analyze(rows)
    print(f"\n{'='*80}")
    print(f"因子分析 — {analysis['n']}样本 {analysis['hits']}涨停 ({analysis['hits']/analysis['n']:.1%})")
    print(f"{'='*80}")
    print(f"{'因子':<22} {'|d|':>8} {'Corr':>8} {'Hit均值':>10} {'Miss均值':>10} {'缺失率':>7}")
    print("-" * 75)
    for f in analysis["factors"][:30]:
        d = abs(f["cohens_d"])
        bar = "█" * min(15, int(d * 20))
        print(f"{f['factor']:<22} {d:8.4f}{bar} {f['correlation']:>+8.4f} "
              f"{f['hit_mean']:>10.2f} {f['miss_mean']:>10.2f} {f['missing']:>6.1%}")

    # 策略模拟
    sim = simulate_strategy(rows, top_k=15)
    print(f"\n策略模拟(Top15): precision={sim['precision@K']:.1%} win_rate={sim['win_rate']:.1%}")

    # 保存
    out = {"meta": {"train_days": len(train_dates), "samples": analysis["n"],
                    "hits": analysis["hits"]},
           "factors": analysis["factors"], "simulation": sim}
    f1 = BACKTEST_DIR / "factor_analysis_v2.json"
    with open(f1, "w") as f: json.dump(out, f, ensure_ascii=False, indent=2)
    f2 = BACKTEST_DIR / "factor_data_v2.json"
    with open(f2, "w") as f: json.dump(rows, f, ensure_ascii=False, indent=2)
    print(f"\n结果: {f1} + {f2}")


if __name__ == "__main__":
    main()
