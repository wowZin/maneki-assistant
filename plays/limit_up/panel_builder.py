#!/usr/bin/env python3
"""夜间 T-1 数据面板构建器。

在交易日凌晨运行，全量预取 T-1 数据，产出 64 特征面板 CSV，
供开盘前模型预评和盘中实时评分复用。

用法:
    python3 plays/limit_up/panel_builder.py                          # 默认全量
    python3 plays/limit_up/panel_builder.py --codes XXXX.SH,YYYY.SZ  # 指定代码
    python3 plays/limit_up/panel_builder.py --quick                  # 快验(100只)

输出:
    wiki/raw/limit-up/panel/{date}.parquet  (全量特征面板)
    wiki/raw/limit-up/panel/{date}_qc.json  (质检报告)
"""

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from scripts.tu_share import call_tushare
from plays.limit_up.strategies import factor_ctx
from plays.limit_up.pit_features import build_pit_features

# ── 配置 ──
RAW_DIR = PROJECT_DIR / "wiki" / "raw" / "limit-up" / "panel"
STOCK_BATCH = 50          # Tushare 批量上限
MAX_WORKERS = 16          # 并行 workers


def _today_str() -> str:
    return datetime.now().strftime("%Y%m%d")


def _prev_trade_date(today: str) -> str:
    """找前一交易日。"""
    _dt = datetime.strptime(today, "%Y%m%d")
    for _ in range(10):
        _dt -= timedelta(days=1)
        _try = _dt.strftime("%Y%m%d")
        cal = call_tushare("trade_cal", {"exchange": "SSE", "start_date": _try, "end_date": _try},
                           "cal_date,is_open")
        items = cal.get("data", {}).get("items", [])
        if items and items[0] and len(items[0]) > 1 and str(items[0][1]) == "1":
            return _try
    return today


def load_stock_list() -> list[str]:
    """加载全量主板非ST股票列表。"""
    r = call_tushare("stock_basic", {}, "ts_code,name,market,list_status,list_date")
    codes = []
    now = _today_str()
    for item in r.get("data", {}).get("items", []):
        code = item[0]
        market = item[2] if len(item) > 2 else ""
        list_date = item[4] if len(item) > 4 else ""
        # 过滤: 主板(SH/SZ) + 非ST(代码) + 上市>60天
        if code.split(".")[1] not in ("SH", "SZ"):
            continue
        if code.startswith("8") or code.startswith("4"):
            continue
        if "ST" in code.upper() or "*ST" in code.upper() or "退" in code:
            continue
        if list_date and len(list_date) == 8 and list_date > str(int(now) - 60):
            continue
        codes.append(code)
    print(f"  [panel] 筛选后 {len(codes)} 只")
    return codes


def batch_prefetch(codes: list[str], today: str, prev_date: str,
                   basic_cache: dict[str, dict]) -> tuple:
    """批量预取全量 T-1 数据（全市场 API 一次拉）。"""
    mf_cache, mf_prev_cache = {}, {}
    tl_cache, ti_cache = {}, {}
    daily_cache: dict[str, list[dict]] = {}

    # daily: 逐日拉全市场日线（不含 ts_code 时只返回当日）
    start70 = (datetime.strptime(prev_date, "%Y%m%d") - timedelta(days=70)).strftime("%Y%m%d")
    print(f"  [panel] ① 预取 daily ({start70}~{prev_date}, 逐日拉)...")
    t0 = time.time()
    cur = datetime.strptime(prev_date, "%Y%m%d")
    daily_count = 0
    for _ in range(70):
        td_str = cur.strftime("%Y%m%d")
        try:
            cal = call_tushare("trade_cal", {"exchange":"SSE","start_date":td_str,"end_date":td_str},"cal_date,is_open")
            if not (cal.get("data",{}).get("items",[[]])[0] or [])[1:] or str(cal["data"]["items"][0][1]) != "1":
                cur -= timedelta(days=1); continue
        except: pass
        try:
            r = call_tushare("daily", {"trade_date": td_str},
                             "ts_code,trade_date,open,pre_close,close,high,low,vol,amount,pct_chg")
            for it in r.get("data", {}).get("items", []):
                f = r["data"]["fields"]; d = dict(zip(f, it)); tc = d["ts_code"]
                if tc not in daily_cache: daily_cache[tc] = []
                daily_cache[tc].append(d)
            daily_count += 1
        except: pass
        cur -= timedelta(days=1)
    for tc, rows in daily_cache.items():
        rows.sort(key=lambda x: x.get("trade_date",""))
        factor_ctx.set_daily(tc, rows)
    print(f"    daily: {time.time()-t0:.1f}s ({len(daily_cache)}只, {daily_count}天)")

    # limit_list_d (全市场)
    print(f"  [panel] ①b 预取 limit_list_d...")
    t0 = time.time()
    limit_by_code: dict[str, list[str]] = {}
    try:
        r = call_tushare("limit_list_d", {"start_date": start70, "end_date": prev_date, "limit_type": "U"},
                         "ts_code,trade_date")
        limit_by_code: dict[str, list[str]] = {}
        for it in r.get("data", {}).get("items", []):
            tc, td = it[0], str(it[1])
            lim = limit_by_code.setdefault(tc, [])
            lim.append(td)
        cutoff20 = (datetime.strptime(prev_date, "%Y%m%d") - timedelta(days=20)).strftime("%Y%m%d")
        for tc, dates in limit_by_code.items():
            cnt20 = sum(1 for d in dates if d >= cutoff20)
            factor_ctx.set_limit_counts(tc, cnt20, len(dates))
    except Exception:
        pass
    print(f"    limit_list_d: {time.time()-t0:.1f}s ({len(limit_by_code)}只)")

    print(f"  [panel] ② 预取 daily_basic 复用...")
    t0 = time.time()
    print(f"    daily_basic: {time.time()-t0:.1f}s ({len(basic_cache)}只,已取)")

    print(f"  [panel] ③ 预取 moneyflow (全市场)...")
    t0 = time.time()
    try:
        r = call_tushare("moneyflow", {"trade_date": prev_date},
                         "ts_code,net_mf_amount,buy_elg_amount,sell_elg_amount,buy_lg_amount,sell_lg_amount")
        for it in r.get("data", {}).get("items", []):
            mf_cache[it[0]] = dict(zip(r["data"]["fields"], it))
        r2 = call_tushare("moneyflow", {"trade_date": (datetime.strptime(prev_date,"%Y%m%d")-timedelta(days=5)).strftime("%Y%m%d")},
                          "ts_code,net_mf_amount")
        prev2 = None
        # 找到前一交易日
        prev_dt = datetime.strptime(prev_date, "%Y%m%d") - timedelta(days=1)
        for _ in range(10):
            try_str = prev_dt.strftime("%Y%m%d")
            cal = call_tushare("trade_cal", {"exchange":"SSE","start_date":try_str,"end_date":try_str},"cal_date,is_open")
            items = cal.get("data",{}).get("items",[])
            if items and items[0] and len(items[0])>1 and str(items[0][1])=="1":
                prev2 = try_str
                break
            prev_dt -= timedelta(days=1)
        if prev2:
            r3 = call_tushare("moneyflow", {"trade_date": prev2}, "ts_code,net_mf_amount")
            for it in r3.get("data",{}).get("items",[]):
                mf_prev_cache[it[0]] = {"net_mf_amount": float(it[-1]) if len(it)>1 else 0}
    except Exception:
        pass
    print(f"    moneyflow: {time.time()-t0:.1f}s ({len(mf_cache)}只)")

    print(f"  [panel] ④ 预取 top_list/top_inst (全市场)...")
    t0 = time.time()
    try:
        r = call_tushare("top_list", {"trade_date": prev_date},
                         "ts_code,amount,net_amount,l_buy,l_amount,net_rate")
        for it in r.get("data", {}).get("items", []):
            tl_cache[it[0]] = dict(zip(r["data"]["fields"], it))
    except Exception:
        pass
    try:
        r = call_tushare("top_inst", {"trade_date": prev_date},
                         "ts_code,exalter,buy,sell,net_buy")
        for it in r.get("data", {}).get("items", []):
            ti_cache.setdefault(it[0], []).append(dict(zip(r["data"]["fields"], it)))
    except Exception:
        pass
    print(f"    top_list/top_inst: {time.time()-t0:.1f}s ({len(tl_cache)}只)")

    print(f"  [panel] ⑤ 预取 stk_auction (并行)...")
    auc_cache = {}
    t0 = time.time()
    try:
        from plays.limit_up.strategies.shortterm import _get_auction as auc_fn
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futs = {pool.submit(auc_fn, c): c for c in codes[:500]}
            for fut in as_completed(futs):
                try:
                    r = fut.result()
                    if r:
                        auc_cache[futs[fut]] = r
                except Exception:
                    pass
    except Exception:
        pass
    print(f"    auction: {time.time()-t0:.1f}s ({len(auc_cache)}只)")

    return mf_cache, mf_prev_cache, tl_cache, ti_cache, auc_cache


def build_features(codes: list[str], today: str, prev_date: str,
                   basic_cache: dict[str, dict],
                   mf_cache, mf_prev_cache, tl_cache, ti_cache, auc_cache) -> pd.DataFrame:
    """调用 build_pit_features 逐只构建64特征。"""
    rows = []
    total = len(codes)
    t0 = time.time()

    for i, code in enumerate(codes):
        if i > 0 and i % 100 == 0:
            print(f"    [{i}/{total}] {time.time()-t0:.0f}s")

        short = code.split(".")[0]

        # daily_rows
        raw = factor_ctx._DAILY_CACHE.get(code, [])
        daily_rows = sorted(raw, key=lambda x: x.get("trade_date", ""))
        if not daily_rows:
            continue
        pit_date = daily_rows[-1].get("trade_date", today)

        # basic_by_date (从 daily_basic 预取缓存)
        basic_ent = {}
        bd = basic_cache.get(code, {})
        if bd:
            basic_ent = {
                "turnover_rate": float(bd.get("turnover_rate", 0)),
                "volume_ratio": float(bd.get("volume_ratio", 0)),
                "circ_mv": float(bd.get("circ_mv", 0)),
                "pe": float(bd.get("pe") or 999.0),
                "pb": float(bd.get("pb") or 999.0),
            }

        # moneyflow
        mf = mf_cache.get(code, {})
        mf_ent = {"net_mf_amount": float(mf.get("net_mf_amount", 0)),
                  "buy_elg_amount": float(mf.get("buy_elg_amount", 0)),
                  "sell_elg_amount": float(mf.get("sell_elg_amount", 0)),
                  "buy_lg_amount": float(mf.get("buy_lg_amount", 0)),
                  "sell_lg_amount": float(mf.get("sell_lg_amount", 0))} if mf else {}
        mf_prev = mf_prev_cache.get(code, {})
        nm_prev = float(mf_prev.get("net_mf_amount", 0))
        mf_bd = {pit_date: mf_ent} if mf_ent else None
        if nm_prev and len(daily_rows) >= 2:
            p2 = daily_rows[-2].get("trade_date", "")
            if p2:
                mf_bd = mf_bd or {}
                mf_bd[p2] = {"net_mf_amount": nm_prev, "buy_elg_amount": 0,
                             "sell_elg_amount": 0, "buy_lg_amount": 0, "sell_lg_amount": 0}

        # concept
        cm = factor_ctx.get_concept_momentum(short)
        if not cm.get("n_concepts", 0):
            try:
                from plays.limit_up.strategies.concept_cache import get_concept_limit_ups as gclu
                clu = gclu(code)
                cn = [k for k in clu if not k.startswith("_")]
                best = max((clu.get(n, 0) for n in cn), default=0)
                cm["n_concepts"] = float(len(cn))
                cm["ret1_avg"] = float(best)
                cm["ret3_max"] = float(best * 2)
            except Exception:
                pass

        auc_ent = auc_cache.get(code, {})
        tl_ent = tl_cache.get(code, {})
        ti_list = ti_cache.get(code, [])

        try:
            feats = build_pit_features(
                code=code,
                score_date=today,
                daily_rows=daily_rows,
                basic_by_date={pit_date: basic_ent} if basic_ent else None,
                moneyflow_by_date=mf_bd,
                auction_by_date={pit_date: auc_ent} if auc_ent else None,
                concept_momentum=cm,
                top_list_by_date={pit_date: tl_ent} if tl_ent else None,
                top_inst_by_date={pit_date: ti_list} if ti_list else None,
                pit_mode=True,
            )
            feats["code"] = code
            feats["pit_date"] = pit_date
            rows.append(feats)
        except Exception as e:
            print(f"    [!] {code} build_pit_features 失败: {e}")

    print(f"  [panel] 特征构建完成: {len(rows)}/{total} 只, {time.time()-t0:.0f}s")
    return pd.DataFrame(rows)


def data_qc(df: pd.DataFrame) -> dict:
    """质检：检查空值率/分布/IC。"""
    report = {
        "total_stocks": len(df),
        "total_features": len([c for c in df.columns if c not in ("code", "pit_date")]),
        "missing_rate": {},
        "feature_stats": {},
    }

    for c in df.columns:
        if c in ("code", "pit_date"):
            continue
        null_pct = float(df[c].isna().sum() / len(df) * 100)
        if null_pct > 0:
            report["missing_rate"][c] = round(null_pct, 1)

        vals = df[c].dropna()
        if len(vals) == 0:
            continue
        p = [1, 5, 25, 50, 75, 95, 99]
        percs = {f"p{k}": round(float(vals.quantile(k / 100)), 2) for k in p}
        report["feature_stats"][c] = {
            "mean": round(float(vals.mean()), 2),
            "std": round(float(vals.std()), 2),
            "zeros%": round(float((vals == 0).sum() / len(vals) * 100), 1),
            **percs,
        }

    # 异常标记：某字段 0 占比 > 80%
    bad = [k for k, v in report["feature_stats"].items() if v.get("zeros%", 0) > 80]
    report["warnings"] = [f"{k}: 零值率 {v['zeros%']}%" for k, v in report["feature_stats"].items() if v.get("zeros%", 0) > 80]
    report["warning_count"] = len(bad)

    print(f"  [QC] 共{report['total_features']}特征, {len(report['missing_rate'])}有缺失, {len(bad)}高零值率")
    for w in report["warnings"][:5]:
        print(f"    ⚠ {w}")
    return report


def main():
    parser = argparse.ArgumentParser(description="T-1 数据面板构建器")
    parser.add_argument("--codes", type=str, help="逗号分隔的股票代码")
    parser.add_argument("--quick", action="store_true", help="快验(100只)")
    args = parser.parse_args()

    today = _today_str()
    prev_date = _prev_trade_date(today)
    print(f"  [panel] T-1: {prev_date}, 今日: {today}")

    RAW_DIR.mkdir(parents=True, exist_ok=True)

    # ── ① 先拉 daily_basic 确定股票池 ──
    print(f"  [panel] ① 预取 daily_basic (全市场,确定股票池)...")
    t0 = time.time()
    basic_cache: dict[str, dict] = {}
    try:
        r = call_tushare("daily_basic", {"trade_date": prev_date},
                         "ts_code,trade_date,turnover_rate,volume_ratio,circ_mv,pe,pb,amount")
        for it in r.get("data", {}).get("items", []):
            tc = it[0]
            # 过滤：主板非ST
            if tc.split(".")[1] not in ("SH", "SZ"): continue
            if "ST" in tc.upper() or tc.startswith("8") or tc.startswith("4"): continue
            basic_cache[tc] = dict(zip(r["data"]["fields"], it))
    except Exception:
        pass
    codes = list(basic_cache.keys())
    print(f"    daily_basic: {time.time()-t0:.1f}s ({len(codes)}只)")

    if args.quick:
        codes = codes[:100]
        print(f"  [panel] 快验模式: 取前100只")

    print(f"  [panel] 开始处理 {len(codes)} 只...")

    # ── ② 批量预取 IC 数据 ──
    mf_cache, mf_prev_cache, tl_cache, ti_cache, auc_cache = batch_prefetch(
        codes, today, prev_date, basic_cache)

    # 构建特征
    df = build_features(codes, today, prev_date,
                        basic_cache, mf_cache, mf_prev_cache, tl_cache, ti_cache, auc_cache)

    # QC
    qc_report = data_qc(df)

    # 保存
    out_path = RAW_DIR / f"{today}.parquet"
    df.to_parquet(out_path, index=False)
    print(f"  [panel] 面板已保存: {out_path} ({len(df)}只, {df.shape[1]}列)")

    qc_path = RAW_DIR / f"{today}_qc.json"
    with open(qc_path, "w") as f:
        json.dump(qc_report, f, ensure_ascii=False, indent=2)
    print(f"  [QC] 报告已保存: {qc_path}")

    return df, qc_report


if __name__ == "__main__":
    main()
