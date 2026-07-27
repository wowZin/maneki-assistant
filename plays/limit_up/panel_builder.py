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
ANALYSIS_DIR = Path(__file__).resolve().parent / "data" / "analysis"
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
        # 过滤创业板/科创板：模型仅训练主板(60/00/002)，非主板评分无效
        if code.startswith(("300", "301", "688")):
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

    print(f"  [panel] ④b 预取 fina_indicator (批量)...")
    fi_cache: dict[str, dict] = {}
    t0 = time.time()
    for batch_start in range(0, len(codes), STOCK_BATCH):
        batch = codes[batch_start:batch_start + STOCK_BATCH]
        if not batch: continue
        try:
            r = call_tushare("fina_indicator", {"ts_code": ",".join(batch)},
                             "ts_code,end_date,dt_netprofit_yoy,n_income,dt_netprofit,or_yoy")
            seen: set[str] = set()
            for it in r.get("data", {}).get("items", []):
                tc = it[0]
                if tc not in seen:  # 只需要最新一期
                    fi_cache[tc] = dict(zip(r["data"]["fields"], it))
                    seen.add(tc)
        except Exception:
            pass
    print(f"    fina_indicator: {time.time()-t0:.1f}s ({len(fi_cache)}只)")

    print(f"  [panel] ⑤ 预取 stk_auction (并行, {prev_date})...")
    auc_cache = {}
    t0 = time.time()
    try:
        from scripts.tu_share import call_tushare as _ct
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            def _get_auc(c):
                try:
                    r = _ct("stk_auction", {"ts_code": c, "trade_date": prev_date},
                            "amount,vol,price,turnover_rate,pre_close")
                    items = r.get("data", {}).get("items", [])
                    flds = r.get("data", {}).get("fields", [])
                    return dict(zip(flds, items[0])) if items and flds else {}
                except Exception:
                    return {}
            futs = {pool.submit(_get_auc, c): c for c in codes}
            for fut in as_completed(futs):
                r = fut.result()
                if r:
                    auc_cache[futs[fut]] = r
    except Exception:
        pass
    print(f"    auction: {time.time()-t0:.1f}s ({len(auc_cache)}只)")

    return mf_cache, mf_prev_cache, tl_cache, ti_cache, auc_cache, fi_cache


def build_features(codes: list[str], today: str, prev_date: str,
                   basic_cache: dict[str, dict], basic_prev_cache: dict[str, dict],
                   mf_cache, mf_prev_cache, tl_cache, ti_cache, auc_cache,
                   fi_cache: dict[str, dict],
                   intraday_cache: dict[str, dict] | None = None) -> pd.DataFrame:
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

        # basic_by_date（从 daily_basic 预取缓存，含前日）
        basic_ent = {}
        prev_basic_ent = {}
        bd = basic_cache.get(code, {})
        if bd:
            basic_ent = {
                "turnover_rate": float(bd.get("turnover_rate", 0)),
                "volume_ratio": float(bd.get("volume_ratio", 0) or 0),
                "circ_mv": float(bd.get("circ_mv", 0) or 0),
                "pe": float(bd.get("pe", 0) or 999.0),
                "pb": float(bd.get("pb", 0) or 0),
            }
        bd_prev = basic_prev_cache.get(code, {})
        if bd_prev:
            prev_basic_ent = {
                "turnover_rate": float(bd_prev.get("turnover_rate", 0)),
                "volume_ratio": float(bd_prev.get("volume_ratio", 0) or 0),
            }
        basic_by_date = {}
        if basic_ent:
            basic_by_date[pit_date] = basic_ent
        if prev_basic_ent:
            prev_pit = (datetime.strptime(pit_date, "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")
            basic_by_date[prev_pit] = prev_basic_ent

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

        # concept（从 _STOCK_CONCEPTS 直接取，不依赖实时涨停数据）
        short = code.split(".")[0]
        try:
            from plays.limit_up.strategies.concept_cache import _STOCK_CONCEPTS, ensure_loaded
            ensure_loaded()
            cc_names = _STOCK_CONCEPTS.get(short, [])
            n_c = float(len(cc_names))
            cm = {"n_concepts": n_c,
                  "ret1_max": 0.0, "ret1_avg": 0.0,
                  "ret3_max": 0.0, "ret3_avg": 0.0,
                  "up_ratio": 0.5, "up_streak_max": 0,
                  "turn_5d_max": 0.0, "turn_5d_avg": 0.0}
        except Exception:
            cm = {"n_concepts": 0.0,
                  "ret1_max": 0.0, "ret1_avg": 0.0,
                  "ret3_max": 0.0, "ret3_avg": 0.0,
                  "up_ratio": 0.5, "up_streak_max": 0,
                  "turn_5d_max": 0.0, "turn_5d_avg": 0.0}

        auc_ent = auc_cache.get(code, {})
        tl_ent = tl_cache.get(code, {})
        ti_list = ti_cache.get(code, [])

        try:
            feats = build_pit_features(
                code=code,
                score_date=today,
                daily_rows=daily_rows,
                basic_by_date=basic_by_date if basic_by_date else None,
                moneyflow_by_date=mf_bd,
                auction_by_date={pit_date: auc_ent} if auc_ent else None,
                concept_momentum=cm,
                top_list_by_date={pit_date: tl_ent} if tl_ent else None,
                top_inst_by_date={pit_date: ti_list} if ti_list else None,
                intraday_by_date={pit_date: intraday_cache.get(code, {})} if intraday_cache else None,
                pit_mode=True,
            )
            feats["code"] = code
            feats["pit_date"] = pit_date
            # TODO(2026-07-25): 面板缺 name 列，后续单独补（stock_basic 已在 ST 过滤时加载，
            # 带上 name 进 feats 即可；pipeline/surge 目前从 analysis/pool 取名）
            rows.append(feats)
        except Exception as e:
            print(f"    [!] {code} build_pit_features 失败: {e}")

    print(f"  [panel] 特征构建完成: {len(rows)}/{total} 只, {time.time()-t0:.0f}s")
    return pd.DataFrame(rows)


def _add_strategy_scores(df: pd.DataFrame, fi_cache: dict[str, dict],
                         basic_cache: dict[str, dict], mf_cache: dict[str, dict],
                         tl_cache: dict[str, dict]) -> pd.DataFrame:
    """从已有缓存数据计算 5 策略分（不调 Tushare）。"""
    t0 = __import__("time").time()
    codes = df["code"].tolist()

    # 预计算（避免循环内 O(n²)）
    conc_dict = dict(zip(df["code"], df["n_concepts"]))
    row_dict = df.set_index("code")[["close_pos", "amplitude", "volume_ratio",
                                      "turnover_rate", "positive_5d", "auc_amt_ratio"]].to_dict("index")
    fund_scores, tech_scores, flow_scores, sent_scores, short_scores = [], [], [], [], []

    for code in codes:
        short = code.split(".")[0]
        row = row_dict.get(code, {})

        # ── fundamental: circ_mv(30%) + pe/pb(25%) + profit_yoy(25%) + concept(20%) ──
        bd = basic_cache.get(code, {})
        mv_yi = float(bd.get("circ_mv", 0) or 0) / 10000
        pe = float(bd.get("pe", 0) or 0)
        pb = float(bd.get("pb", 0) or 0)
        fi = fi_cache.get(code, {})
        profit = float(fi.get("dt_netprofit_yoy", 0) or 0)

        f = 0.0
        r = []
        if mv_yi >= 200: f += 20; r.append(f"大盘{mv_yi:.0f}亿+20")
        elif mv_yi >= 100: f += 17; r.append(f"中大盘{mv_yi:.0f}亿+17")
        elif mv_yi >= 50: f += 13; r.append(f"中盘{mv_yi:.0f}亿+13")
        elif mv_yi >= 20: f += 8; r.append(f"中小盘{mv_yi:.0f}亿+8")
        if pe > 50 or pe <= 0: f += 15; r.append(f"成长/亏损PE={pe:.0f}+15")
        elif pe > 30: f += 11; r.append(f"成长PE={pe:.0f}+11")
        if pb > 8: f += 10; r.append(f"高PB={pb:.1f}+10")
        elif pb > 5: f += 6; r.append(f"偏高PB={pb:.1f}+6")
        if profit > 50: f += 15; r.append(f"扣非高增{profit:.0f}%+15")
        elif profit > 20: f += 10; r.append(f"扣非增长{profit:.0f}%+10")
        # concept count from pit features
        cc = int(conc_dict.get(code, 0))
        if cc >= 10: f += 10; r.append(f"多概念{cc}个+10")
        elif cc >= 5: f += 5; r.append(f"概念{cc}个+5")
        fund_scores.append(min(100, round(f)))

        # ── technical: 从 PIT 特征中的昨收位置/振幅/量价计算 ──
        cp = float(row.get("close_pos", 0.5) or 0.5)
        amp = float(row.get("amplitude", 0) or 0) * 100
        vr = float(row.get("volume_ratio", 1) or 1)
        tr = float(row.get("turnover_rate", 5) or 5)
        nt = float(row.get("positive_5d", 0) or 0)
        t = 15.0
        if cp > 0.7: t += 16
        elif cp > 0.5: t += 10
        if amp > 8: t -= 5
        if vr > 1.5: t += 10
        elif vr > 1.0: t += 5
        if tr > 15: t += 8
        elif tr > 8: t += 3
        if nt >= 4: t += 12
        elif nt >= 3: t += 6
        tech_scores.append(min(100, round(t)))

        # ── fundflow: net_mf_amount 占比评分 ──
        mf_ent = mf_cache.get(code, {})
        nm = float(mf_ent.get("net_mf_amount", 0) or 0)
        mv = mv_yi * 10000 * 10000  # 亿→万元→元 ≈ 流通市值(元)
        if mv > 0 and nm != 0:
            ratio = nm * 10000 / mv * 100  # 净额占比%
            ff = min(40, max(0, ratio * 5))
        else:
            ff = 0
        flow_scores.append(min(100, round(ff + 20)))  # base 20

        # ── sentiment: 涨停概念热度 ──
        try:
            from plays.limit_up.strategies.concept_cache import get_concept_limit_ups, ensure_loaded
            ensure_loaded()
            clu = get_concept_limit_ups(code)
            cn = [k for k in clu if not k.startswith("_")]
            best = max((clu.get(n, 0) for n in cn), default=0)
        except Exception:
            best = 0
        sent = min(60, round(best * 10)) + 15  # base 15
        sent_scores.append(min(100, sent))

        # ── shortterm: 竞价数据评分 ──
        auc = float(row.get("auc_amt_ratio", 0) or 0)
        st = 10.0
        if auc > 1: st += 20
        elif auc > 0.5: st += 10
        elif auc > 0.1: st += 5
        short_scores.append(min(100, round(st)))

    df["fundamental"] = [float(v) for v in fund_scores]
    df["technical"] = [float(v) for v in tech_scores]
    df["fundflow"] = [float(v) for v in flow_scores]
    df["sentiment"] = [float(v) for v in sent_scores]
    df["shortterm"] = [float(v) for v in short_scores]
    print(f"  [panel] 策略分计算完成: {len(codes)}只, {__import__('time').time()-t0:.0f}s")
    return df


def data_qc(df: pd.DataFrame) -> dict:
    """质检：检查空值率/分布/IC。"""
    report = {
        "total_stocks": len(df),
        "total_features": len([c for c in df.columns if c not in ("code", "pit_date", "name")]),
        "missing_rate": {},
        "feature_stats": {},
    }

    for c in df.columns:
        if c in ("code", "pit_date", "name"):
            continue  # name 是字符串列，不做分位数统计
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

    today = os.environ.get("_PANEL_DATE") or _today_str()
    from plays.limit_up.utils import _is_trade_day
    if not _is_trade_day(today):
        print(f"[panel_builder] {today} 非交易日，跳过")
        return
    prev_date = _prev_trade_date(today)
    print(f"  [panel] T-1: {prev_date}, 今日: {today}")

    RAW_DIR.mkdir(parents=True, exist_ok=True)

    # ── ① 先拉 daily_basic 确定股票池 ──
    print(f"  [panel] ① 预取 daily_basic (全市场,确定股票池)...")
    t0 = time.time()
    basic_cache: dict[str, dict] = {}
    st_codes: set[str] = set()
    try:
        r = call_tushare("daily_basic", {"trade_date": prev_date},
                         "ts_code,trade_date,turnover_rate,volume_ratio,circ_mv,pe,pb,amount")
        for it in r.get("data", {}).get("items", []):
            tc = it[0]
            if tc.split(".")[1] not in ("SH", "SZ"): continue
            if tc.startswith("8") or tc.startswith("4"): continue
            basic_cache[tc] = dict(zip(r["data"]["fields"], it))
    except Exception:
        pass

    # 从 stock_basic 获取名称，过滤 ST；同时构建 name_map（panel 缺 name 列）
    name_map: dict[str, str] = {}
    try:
        r2 = call_tushare("stock_basic", {}, "ts_code,name,list_date")
        for it in r2.get("data", {}).get("items", []):
            tc = it[0]
            if tc in basic_cache:
                if "ST" in (it[1] or "").upper() or "退" in (it[1] or ""):
                    st_codes.add(tc)
                name_map[tc] = it[1] if it[1] else ""
    except Exception:
        pass
    for tc in st_codes:
        basic_cache.pop(tc, None)

    codes = list(basic_cache.keys())
    print(f"    daily_basic: {time.time()-t0:.1f}s ({len(codes)}只, 过滤ST={len(st_codes)})")

    # 补前日 daily_basic（供 prev_vol_ratio / prev_turnover）
    prev2 = (datetime.strptime(prev_date, "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")
    basic_prev_cache: dict[str, dict] = {}
    try:
        r = call_tushare("daily_basic", {"trade_date": prev2},
                         "ts_code,turnover_rate,volume_ratio")
        for it in r.get("data", {}).get("items", []):
            basic_prev_cache[it[0]] = dict(zip(r["data"]["fields"], it))
    except Exception:
        pass
    print(f"    daily_basic(前日{prev2}): {time.time()-t0:.1f}s ({len(basic_prev_cache)}只)")

    if args.quick:
        codes = codes[:100]
        print(f"  [panel] 快验模式: 取前100只")

    print(f"  [panel] 开始处理 {len(codes)} 只...")

    # ── ② 批量预取 IC 数据 ──
    mf_cache, mf_prev_cache, tl_cache, ti_cache, auc_cache, fi_cache = batch_prefetch(
        codes, today, prev_date, basic_cache)

    # 前日 daily_basic
    prev2 = (datetime.strptime(prev_date, "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")
    basic_prev_cache: dict[str, dict] = {}
    try:
        r = call_tushare("daily_basic", {"trade_date": prev2},
                         "ts_code,turnover_rate,volume_ratio")
        for it in r.get("data", {}).get("items", []):
            basic_prev_cache[it[0]] = dict(zip(r["data"]["fields"], it))
    except Exception:
        pass
    print(f"    daily_basic(前日{prev2}): {time.time()-t0:.1f}s ({len(basic_prev_cache)}只)")

    # ⑥ 加载 T-1 盘中数据（intraday）
    intraday_cache: dict[str, dict] = {}
    id_path = RAW_DIR / "intraday" / f"{prev_date}.parquet"
    if id_path.exists():
        try:
            import pandas as _pd
            idf = _pd.read_parquet(id_path)
            for _, r in idf.iterrows():
                intraday_cache[r["ts_code"]] = r.to_dict()
            print(f"    intraday({prev_date}): {len(intraday_cache)}只")
        except Exception as e:
            print(f"    intraday 加载失败: {e}")

    # 构建特征
    df = build_features(codes, today, prev_date,
                        basic_cache, basic_prev_cache,
                        mf_cache, mf_prev_cache, tl_cache, ti_cache, auc_cache, fi_cache,
                        intraday_cache=intraday_cache if intraday_cache else None)

    # 补策略分
    df = _add_strategy_scores(df, fi_cache, basic_cache, mf_cache, tl_cache)

    # 补名称列（stock_basic 已免费提供 name 字段，之前未落盘）
    df["name"] = df["code"].map(name_map)
    del name_map  # 释放

    # 删旧 TODO（已在此提交完成）
    # TODO(2026-07-25): 面板缺 name 列，后续单独补（stock_basic 已在 ST 过滤时加载，
    # 带上 name 进 feats 即可；pipeline/surge 目前从 analysis/pool 取名）

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

    # ── 模型预评分 ──
    try:
        from plays.limit_up.factors.optimized.model_score import factor_model_score_batch
        df["model_score"] = factor_model_score_batch(df)
        hot = df[df["model_score"] >= 35].sort_values("model_score", ascending=False)
        # 补齐中文名(从THS SDK批量取)
        try:
            from scripts.ths_client import get_ths_client as _ths
            _ths_inst = _ths()
            _shorts = [c.split(".")[0] for c in hot["code"]]
            _names = {}
            for _i in range(0, len(_shorts), 50):
                _q = _ths_inst.get_batch_quotes(_shorts[_i:_i+50])
                for _s, _d in _q.items():
                    if _d and _d.get("f_name"):
                        _names[_s] = _d["f_name"]
            hot = hot.copy()
            hot["name"] = hot["code"].apply(lambda c: _names.get(c.split(".")[0], ""))
        except Exception:
            hot["name"] = ""
        analysis_path = ANALYSIS_DIR / f"{today}.json"
        analysis_path.parent.mkdir(parents=True, exist_ok=True)
        keep = [c for c in df.columns if c not in ("pit_date",) and not c.startswith("_")]
        if "name" in hot.columns and "name" not in keep:
            keep = ["name"] + keep
        tmp = analysis_path.with_suffix(".tmp")
        hot[keep].to_json(tmp, orient="records", force_ascii=False)
        tmp.rename(analysis_path)  # 原子替换
        print(f"  [panel] 预评: {len(df)}只→≥35分={len(hot)}只已保存")
    except Exception as e:
        print(f"  [panel] 预评跳过: {e}")

    return df, qc_report


if __name__ == "__main__":
    main()
