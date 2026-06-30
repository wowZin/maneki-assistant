#!/usr/bin/env python3
"""V3回测 — 竞价 + jvQuant日内 + 板块共振 + XGBoost"""

import json, sys, time
import numpy as np
from collections import defaultdict
from datetime import datetime
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_DIR))
sys.path.insert(0, str(PROJECT_DIR / "scripts"))

PLAY_DIR = Path(__file__).resolve().parent
BT_DIR = PLAY_DIR / "data" / "backtest"


def sf(v, d=0.0):
    if v is None: return d
    try: return float(str(v).replace(",","").replace("%",""))
    except: return d


def short(code): return code.replace(".SH","").replace(".SZ","")


# ═══════════════ 数据加载 ═══════════════

def load_all():
    with open(BT_DIR / "bulk_cache.json") as f:
        raw = json.load(f)
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


# ═══════════════ 特征工程 ═══════════════

def build_features(cache):
    daily = cache["daily"]
    db = cache["daily_basic"]
    mf = cache["moneyflow"]
    auction = cache.get("stk_auction", {})
    limlist = cache["limit_list_d"]
    limstep = cache["limit_step"]
    limcpt = cache.get("limit_cpt_list", {})

    # stock basic
    sb = cache["stock_basic"]
    items = sb.get("data",{}).get("items",[])
    fields = sb.get("data",{}).get("fields",[])
    stock_info = {}
    if fields and items:
        for item in items:
            d = dict(zip(fields, item))
            c = d.get("ts_code","")
            if c: stock_info[short(c)] = d

    dates = sorted(set(d[0] for d in daily))
    dates = [d for d in dates if d < "20260630"]

    # Build sector heat: per day, per industry, count limit-ups
    sector_heat = defaultdict(lambda: defaultdict(int))
    for (d, code, lt), v in limlist.items():
        if lt != "U": continue
        ind = stock_info.get(short(code), {}).get("industry", "")
        if ind: sector_heat[d][ind] += 1

    # Next-day labels
    date_sorted = sorted(dates)
    next_lu = set()
    for i, d in enumerate(date_sorted):
        nd = date_sorted[i+1] if i+1 < len(date_sorted) else None
        if nd is None: continue
        for (ld, code, lt), _ in limlist.items():
            if ld == nd and lt == "U":
                next_lu.add((d, code))

    rows = []
    for d in dates[:-2]:  # exclude last 2 for validation
        # Build candidate pool
        candidates = []
        for (dd, code), row in daily.items():
            if dd != d: continue
            pct = sf(row.get("pct_chg", 0))
            if pct < 0 or pct >= 9.5: continue  # tradable only
            s = short(code)
            if not s or s.startswith(("30","688","8","4")): continue
            info = stock_info.get(s, {})
            if "ST" in str(info.get("name","")).upper(): continue
            ld = str(info.get("list_date",""))
            if ld:
                try:
                    if (datetime.strptime(d,"%Y%m%d")-datetime.strptime(ld,"%Y%m%d")).days<60:
                        continue
                except: pass
            dbr = db.get((d, code), {})
            cmv = sf(dbr.get("circ_mv",0))
            if cmv and cmv<50000: continue
            to = sf(dbr.get("turnover_rate_f",0)) or sf(dbr.get("turnover_rate",0))
            if to and to<2: continue
            candidates.append((s, code, pct, row, dbr))
        candidates.sort(key=lambda x: x[2], reverse=True)
        candidates = candidates[:150]

        d_idx = date_sorted.index(d) if d in date_sorted else -1
        prev_d = date_sorted[d_idx-1] if d_idx>0 else d

        for s, code, pct, row, dbr in candidates:
            close = sf(row.get("close",0))
            open_p = sf(row.get("open",0))
            high = sf(row.get("high",0))
            low = sf(row.get("low",0))
            pre_close = sf(row.get("pre_close",0))
            amount = sf(row.get("amount",0))
            hl_range = high - low
            body = abs(close - open_p)

            # === 基础日内形态 ===
            f = {
                "date": d, "code": code, "short": s,
                "is_hit": 1 if (d, code) in next_lu else 0,
                "pct_chg": pct,
                "close_pos": round((close-low)/hl_range,4) if hl_range>0 else 0.5,
                "body_ratio": round(body/hl_range,4) if hl_range>0 else 0,
                "upper_ratio": round((high-max(close,open_p))/body,2) if body>0 else 0,
                "lower_ratio": round((min(close,open_p)-low)/body,2) if body>0 else 0,
                "amplitude": round((high-low)/pre_close*100,2) if pre_close>0 else 0,
                "vol_ratio": sf(dbr.get("volume_ratio",0)),
                "turnover": sf(dbr.get("turnover_rate",0)) or sf(dbr.get("turnover_rate_f",0)),
                "cmv_yi": sf(dbr.get("circ_mv",0))/10000,
                "amount": amount,
            }

            # === 竞价因子 ===
            auc = auction.get((d, code), {})
            auc_price = sf(auc.get("price",0))
            auc_pre = sf(auc.get("pre_close",0))
            auc_vol = sf(auc.get("vol",0))
            auc_amt = sf(auc.get("amount",0))
            auc_vr = sf(auc.get("volume_ratio",0))
            f["auc_gap"] = round((auc_price-auc_pre)/auc_pre*100,2) if auc_pre>0 else 0
            f["auc_amt_ratio"] = round(auc_amt/(amount*1000),6) if amount>0 else 0  # auction amount / daily amount
            f["auc_vol_ratio"] = auc_vr
            f["auc_has_data"] = 1 if auc else 0

            # === T-1 因子 ===
            prev_row = daily.get((prev_d, code), {})
            prev_db = db.get((prev_d, code), {})
            prev_pct = sf(prev_row.get("pct_chg",0))
            f["prev_pct"] = round(prev_pct, 2)
            f["prev_turnover"] = sf(prev_db.get("turnover_rate",0))
            f["prev_vol_ratio"] = sf(prev_db.get("volume_ratio",0))
            f["vol_accel"] = round(f["vol_ratio"]/f["prev_vol_ratio"],2) if f["prev_vol_ratio"]>0 else 1.0

            # === 资金流 ===
            mf_row = mf.get((d, code), {})
            mf_net = sf(mf_row.get("net_mf_amount",0))
            f["mf_net"] = mf_net
            f["mf_pct"] = round(mf_net/amount*1000,4) if amount>0 else 0  # fixed unit conversion
            prev_mf = mf.get((prev_d, code), {})
            f["mf_accel"] = round(mf_net - sf(prev_mf.get("net_mf_amount",0)), 2)

            # === 涨停基因 ===
            max_step = 0
            for (sd, sc), sr in limstep.items():
                if sc == code: max_step = max(max_step, sf(sr.get("nums",0)))
            f["max_step"] = int(max_step)
            was_limit = 0
            for (ld, lc, lt), _ in limlist.items():
                if lc == code and ld == prev_d and lt == "U": was_limit = 1; break
            f["was_limit"] = was_limit

            # === 5日趋势 ===
            recent = []
            for i2 in range(max(0,d_idx-4), d_idx+1):
                rd = date_sorted[i2]
                rr = daily.get((rd, code), {})
                recent.append(sf(rr.get("pct_chg",0)))
            f["pct_5d"] = round(sum(recent[-5:]), 2)
            f["positive_5d"] = sum(1 for p in recent[-5:] if p>0)

            # === 板块共振 ===
            ind = stock_info.get(s, {}).get("industry", "")
            f["sector_heat"] = sector_heat.get(d, {}).get(ind, 0)
            f["sector_rank"] = 0  # will be filled below

            rows.append(f)

    # Fill sector_rank: percentile of sector_heat within each day
    by_date = defaultdict(list)
    for r in rows: by_date[r["date"]].append(r)
    for d, day_rows in by_date.items():
        heats = [r["sector_heat"] for r in day_rows]
        if max(heats) > 0:
            for r in day_rows:
                r["sector_rank"] = round(sum(1 for h in heats if h <= r["sector_heat"]) / len(heats), 3)

    return rows


# ═══════════════ jvQuant 日内因子 ═══════════════

def enrich_jvquant(rows, sample_days=5):
    """用 jvQuant 分钟数据补充日内时序因子"""
    from scripts.jvquant_client import JvQuantClient
    client = JvQuantClient()

    # Sample: pick top 80 stocks from each of the last sample_days
    dates = sorted(set(r["date"] for r in rows))
    sample_dates_set = set(dates[-sample_days:])
    to_enrich = [r for r in rows if r["date"] in sample_dates_set]
    to_enrich.sort(key=lambda r: r["pct_chg"], reverse=True)
    to_enrich = to_enrich[:sample_days * 80]

    print(f"  jvQuant日内: {len(to_enrich)}条 → ", end="", flush=True)
    enriched = {}
    t0 = time.time()
    for i, r in enumerate(to_enrich):
        try:
            ds = f'{r["date"][:4]}-{r["date"][4:6]}-{r["date"][6:8]}'
            md = client.get_intraday_metrics(r["short"], ds)
            if md:
                enriched[(r["date"], r["code"])] = md
        except: pass
        if (i+1) % 30 == 0: print(f"{i+1}/{len(to_enrich)}", end="", flush=True)
    elapsed = time.time() - t0
    print(f" → {len(enriched)} ok ({elapsed:.0f}s)")

    # Merge
    for r in rows:
        key = (r["date"], r["code"])
        im = enriched.get(key, {})
        r["jv_vwap"] = im.get("vwap", 0)
        r["jv_morning_vol"] = im.get("morning_vol_ratio", 0)
        r["jv_afternoon_str"] = im.get("afternoon_strength", 0)
        r["jv_tail_vol"] = im.get("tail_vol_ratio", 0)
        r["jv_open"] = im.get("open", 0)
        r["jv_has_intraday"] = 1 if im else 0

    return rows


# ═══════════════ ML模型 ═══════════════

def train_xgboost(rows):
    """训练XGBoost并评估"""
    from xgboost import XGBClassifier
    from sklearn.model_selection import TimeSeriesSplit
    from sklearn.metrics import precision_score

    feature_cols = [k for k in rows[0] if k not in
                    ("date","code","short","is_hit","amount")]
    X = np.array([[sf(r.get(c,0)) for c in feature_cols] for r in rows])
    y = np.array([r["is_hit"] for r in rows])

    # Time-series cross-validation
    dates = sorted(set(r["date"] for r in rows))
    fold_breaks = [dates[len(dates)*i//5] for i in range(1,5)]

    results = []
    for fold_end in fold_breaks:
        train_idx = [i for i,r in enumerate(rows) if r["date"] < fold_end]
        val_idx = [i for i,r in enumerate(rows)
                   if fold_end <= r["date"] <
                   (dates[min(dates.index(fold_end)+3, len(dates)-1)]
                    if dates.index(fold_end)+3 < len(dates) else dates[-1])]
        if len(val_idx) < 10: continue

        Xt, yt = X[train_idx], y[train_idx]
        Xv, yv = X[val_idx], y[val_idx]

        # Handle class imbalance
        scale = (len(yt)-sum(yt)) / max(1, sum(yt))
        model = XGBClassifier(n_estimators=100, max_depth=4, learning_rate=0.05,
                              scale_pos_weight=scale, random_state=42, verbosity=0)
        model.fit(Xt, yt)

        # Evaluate
        probs = model.predict_proba(Xv)[:, 1]
        # Top-K precision
        top_idx = np.argsort(-probs)
        for k in [5, 10, 15]:
            top_k = top_idx[:k]
            hr = yv[top_k].sum() / k
            wr = sum(1 for i in top_k
                     if rows[val_idx[i]]['pct_chg'] > 2) / k
            results.append({"fold": fold_end, "k": k, "hit_rate": hr, "win_rate": wr,
                            "n_val": len(val_idx)})

        # Feature importance
        if fold_end == fold_breaks[-1]:
            imp = sorted(zip(feature_cols, model.feature_importances_),
                        key=lambda x: x[1], reverse=True)
            print(f"\n  Top 15 features:")
            for name, score in imp[:15]:
                print(f"    {name:<25} {score:.4f}")

    return results, model, feature_cols


# ═══════════════ 主流程 ═══════════════

def main():
    print("V3回测: 竞价 + jvQuant日内 + 板块 + XGBoost")
    cache = load_all()
    print(f"数据: {len(cache)} APIs")

    print("\n[1/4] 特征工程...")
    rows = build_features(cache)
    hits = sum(1 for r in rows if r["is_hit"])
    print(f"  {len(rows)}样本, {hits}涨停 ({hits/len(rows):.1%})")
    print(f"  特征数: {len([k for k in rows[0] if k not in ('date','code','short','is_hit','amount')])}")

    print("\n[2/4] jvQuant日内因子...")
    rows = enrich_jvquant(rows, sample_days=5)

    print("\n[3/4] XGBoost训练...")
    results, model, features = train_xgboost(rows)

    print(f"\n[4/4] 结果:")
    for k in [5, 10, 15]:
        hrs = [r["hit_rate"] for r in results if r["k"] == k]
        wrs = [r["win_rate"] for r in results if r["k"] == k]
        if hrs:
            print(f"  Top{k:>2}: hit={np.mean(hrs):.0%} win={np.mean(wrs):.0%} "
                  f"({len(hrs)} folds)")

    # Save model info
    with open(BT_DIR / "xgb_features.json", "w") as f:
        json.dump({"features": features,
                   "importance": sorted(zip(features, model.feature_importances_.tolist()),
                                       key=lambda x: x[1], reverse=True)},
                  f, ensure_ascii=False, indent=2)
    print(f"\n模型特征已保存: {BT_DIR}/xgb_features.json")


if __name__ == "__main__":
    main()
