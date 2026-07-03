"""带标签面板构建。

流程：
1. load_analysis_records() — 读 data/analysis/*.json → DataFrame[code,date,5维分,total,pct_chg]，按 (code,date) 去重
2. pull_daily_bars() — 按 trade_date 批量拉 tushare 日线，缓存到 cache/daily.parquet
3. build_panel() — 为每条评分记录 join 未来收益/追高/涨停标签 → out/panel.csv

tushare 仅在 pull_daily_bars 内懒加载，labels/metrics 与本模块的纯逻辑可在无 .env 时单测。
"""

from __future__ import annotations

import glob
import json
import os
from pathlib import Path

import pandas as pd

from . import labels as L

PLAY_DIR = Path(__file__).resolve().parent.parent
PROJECT_DIR = PLAY_DIR.parent.parent
# 数据源优先级：wiki/raw/limit-up/analysis（归档区）→ plays/limit_up/data/analysis（当日）
ANALYSIS_DIRS = [
    PROJECT_DIR / "wiki" / "raw" / "limit-up" / "analysis",
    PLAY_DIR / "data" / "analysis",
]
ANALYSIS_DIR = ANALYSIS_DIRS[0]  # 向后兼容：默认指向 wiki/raw/limit-up
CACHE_DIR = Path(__file__).resolve().parent / "cache"
OUT_DIR = Path(__file__).resolve().parent / "out"
CACHE_DIR.mkdir(exist_ok=True)
OUT_DIR.mkdir(exist_ok=True)

# 回测面板沉淀路径：wiki/raw/limit-up/panel/<api>/YYYYMMDD.parquet
# 按天存储，跨会话持久，git 跟踪，避免重复拉取
PANEL_DIR = PROJECT_DIR / "wiki" / "raw" / "limit-up" / "panel"

DIM_COLS = ["fundamental", "technical", "fundflow", "sentiment", "shortterm"]

# 唯一总分列（2026-07-02 重构后）
TOTAL_COLS = ["total_score"]

# ===== 1. 评分记录 =====
def load_analysis_records(
    dedup: str | None = None,
    analysis_dir: Path | str | None = None,
    dates: list[str] | None = None,
) -> pd.DataFrame:
    """读取所有 analysis JSON，展开为每行一条 (code, date, ts) 评分。

    默认保留**全部轮次**（一天多轮 pipeline 每轮都是独立信号），
    dedup 参数仅为回退兼容，不建议使用：
      - None（默认）: 全量保留，每轮一条记录
      - "last":     每 (code,date) 只保留最后一轮
      - "max_total": 每 (code,date) 保留 total 最高的一轮
      - "mean":     每 (code,date) 各维度取均值

    analysis_dir: 自定义 analysis 目录。默认合并
      wiki/raw/limit-up/analysis（历史归档）+ plays/limit_up/data/analysis（当日）。
    dates: 若指定，只加载这些交易日（YYYYMMDD）的文件。
    """
    if analysis_dir is not None:
        source_dirs = [Path(analysis_dir)]
    else:
        source_dirs = [d for d in ANALYSIS_DIRS if d.exists()]

    date_filter = set(dates) if dates else None

    rows = []
    seen_files: set[str] = set()  # 防止同名文件在多目录重复加载
    for base in source_dirs:
        for f in sorted(glob.glob(str(base / "*.json"))):
            fname = os.path.basename(f)
            if fname in seen_files:
                continue
            seen_files.add(fname)
            if fname.startswith("v2_"):
                continue
            date_prefix = fname.split("_")[0]
            if date_filter and date_prefix not in date_filter:
                continue
            _process_file(f, rows)
    df = pd.DataFrame(rows).dropna(subset=["code", "date"])
    if df.empty:
        return df

    if dedup is None:
        # 默认：全量保留，每轮独立
        return df.reset_index(drop=True)
    if dedup == "last":
        return df.sort_values("ts").groupby(["code", "date"], as_index=False).last() \
                 .drop(columns=["ts"], errors="ignore").reset_index(drop=True)
    if dedup == "max_total":
        return df.sort_values("total").groupby(["code", "date"], as_index=False).last() \
                 .drop(columns=["ts"], errors="ignore").reset_index(drop=True)
    if dedup == "mean":
        return df.groupby(["code", "date"], as_index=False)[
            DIM_COLS + TOTAL_COLS + ["total", "pct_chg_score_day", "resonance_count", "is_resonance"]
        ].mean().reset_index(drop=True)
    raise ValueError(f"unknown dedup: {dedup}")


def _process_file(filepath: str, rows: list):
    """把单个 analysis JSON 展开成行，追加到 rows。"""
    fname = os.path.basename(filepath)
    date = fname.split("_")[0]
    ts = fname.replace(".json", "")
    try:
        recs = json.load(open(filepath))
    except Exception:
        return
    for r in recs:
        if not isinstance(r, dict) or not r.get("code"):
            continue
        sc = r.get("scores", {}) or {}
        res = r.get("resonance", {}) or {}
        row = {
            "code": r.get("code"),
            "date": date,
            "ts": ts,
            "scan_time": ts[-4:] if len(ts) >= 4 else "",
            "fundamental": _f(sc.get("fundamental")),
            "technical": _f(sc.get("technical")),
            "fundflow": _f(sc.get("fundflow")),
            "sentiment": _f(sc.get("sentiment")),
            "shortterm": _f(sc.get("shortterm")),
            "total": _f(r.get("total")),
            "pct_chg_score_day": _f(r.get("pct_chg")),
            "resonance_count": _f(res.get("count")),
            "is_resonance": 1 if res.get("is_resonance") else 0,
        }
        for col in TOTAL_COLS:
            row[col] = _f(r.get(col))
        rows.append(row)


def _f(v):
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (ValueError, TypeError):
        return None


# ===== 2. 面板数据：按天 + 按列增量缓存 =====
#
# 数据沉淀路径：wiki/raw/limit-up/panel/<api>/<YYYYMMDD>.parquet
#
# 两个正交维度的增量：
#   - 行增量（新日期）：读某天时若 parquet 不存在，才拉 Tushare
#   - 列增量（新字段）：读某天时若 parquet 缺列，只拉该字段并合并到当天 parquet
#
# 因子变了 → 只补新字段那一列；日期扩了 → 只补新日期那几行。

# 每个 API 的字段规范（列名 -> 是否 numeric）
_API_FIELDS: dict[str, dict[str, bool]] = {
    "daily": {
        "ts_code": False, "trade_date": False,
        "open": True, "high": True, "low": True, "close": True, "pre_close": True,
        "pct_chg": True, "vol": True, "amount": True,
    },
    "daily_basic": {
        "ts_code": False, "trade_date": False,
        "pe": True, "pb": True, "circ_mv": True,
        "turnover_rate": True, "volume_ratio": True,
        "total_mv": True, "turnover_rate_f": True,
    },
    "moneyflow": {
        "ts_code": False, "trade_date": False,
        "buy_elg_amount": True, "sell_elg_amount": True,
        "buy_lg_amount": True, "sell_lg_amount": True,
        "net_mf_amount": True,
    },
}


def _trade_dates(start: str, end: str) -> list[str]:
    """从 Tushare 拉指定区间的交易日历。"""
    from scripts.tu_share import call_tushare
    cal = call_tushare(
        "trade_cal", {"exchange": "SSE", "start_date": start, "end_date": end}
    )
    trade_dates = []
    if cal.get("data"):
        fields = cal["data"]["fields"]
        for item in cal["data"]["items"]:
            row = dict(zip(fields, item))
            if int(row.get("is_open", 0)) == 1:
                trade_dates.append(str(row["cal_date"]))
    return trade_dates


def _fetch_day_partial(api: str, date: str, fields_needed: list[str]) -> pd.DataFrame:
    """拉某一天全市场的指定字段。fields_needed 不含 ts_code/trade_date 也会自动补上。"""
    from scripts.tu_share import call_tushare
    key_fields = ["ts_code", "trade_date"]
    fields_req = list(dict.fromkeys(key_fields + fields_needed))  # 去重保序
    fields_str = ",".join(fields_req)
    res = call_tushare(api, {"trade_date": date}, fields_str)
    if not res.get("data"):
        return pd.DataFrame()
    cols = res["data"]["fields"]
    items = res["data"]["items"]
    df = pd.DataFrame(items, columns=cols)
    fld_spec = _API_FIELDS.get(api, {})
    for c in df.columns:
        if fld_spec.get(c, False):
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df["trade_date"] = df["trade_date"].astype(str)
    return df


def _ensure_day_columns(api: str, date: str, cols_needed: list[str]) -> pd.DataFrame:
    """确保某天 parquet 中含有所需字段：缺则按列增量拉取并合并。"""
    day_dir = PANEL_DIR / api
    day_dir.mkdir(parents=True, exist_ok=True)
    day_file = day_dir / f"{date}.parquet"

    # 排除主键列（key 列总是存在）
    non_key = [c for c in cols_needed if c not in ("ts_code", "trade_date")]

    if day_file.exists():
        df = pd.read_parquet(day_file)
        missing = [c for c in non_key if c not in df.columns]
        if not missing:
            return df
        # 列增量拉取
        print(f"  [{api}] {date} 缺列 {missing}, 增量拉取")
        add = _fetch_day_partial(api, date, missing)
        if add.empty:
            return df
        df = df.merge(add[["ts_code", "trade_date"] + missing], on=["ts_code", "trade_date"], how="left")
        df.to_parquet(day_file)
        return df

    # 整天新拉
    df = _fetch_day_partial(api, date, non_key)
    if df.empty:
        return df
    df.to_parquet(day_file)
    return df


def _load_or_fetch_by_day(api: str, dates: list[str], cols_needed: list[str],
                            refresh: bool = False) -> pd.DataFrame:
    """为一组交易日装载 api 所需字段，行 + 列都做增量。"""
    frames = []
    for d in dates:
        if refresh:
            (PANEL_DIR / api / f"{d}.parquet").unlink(missing_ok=True)
        df = _ensure_day_columns(api, d, cols_needed)
        if not df.empty:
            frames.append(df)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True).sort_values(["ts_code", "trade_date"]).reset_index(drop=True)


def pull_daily_bars(codes: list[str], start: str, end: str, refresh: bool = False,
                    cols: list[str] | None = None) -> pd.DataFrame:
    """按天拉/读 daily，按列增量。

    cols: 需要的字段（默认全量 daily 字段）。
    """
    cols = cols or [c for c in _API_FIELDS["daily"] if c not in ("ts_code", "trade_date")]
    dates = _trade_dates(start, end)
    df = _load_or_fetch_by_day("daily", dates, cols, refresh=refresh)
    if df.empty:
        return df
    return df[df["ts_code"].isin(set(codes))].copy()


def pull_daily_basic_bars(codes: list[str], start: str, end: str, refresh: bool = False,
                           cols: list[str] | None = None) -> pd.DataFrame:
    """按天拉/读 daily_basic，按列增量。"""
    cols = cols or ["pe", "pb", "circ_mv", "turnover_rate", "volume_ratio"]
    dates = _trade_dates(start, end)
    df = _load_or_fetch_by_day("daily_basic", dates, cols, refresh=refresh)
    if df.empty:
        return df
    return df[df["ts_code"].isin(set(codes))].copy()


def pull_moneyflow_bars(codes: list[str], start: str, end: str, refresh: bool = False,
                        cols: list[str] | None = None) -> pd.DataFrame:
    """按天拉/读 moneyflow，按列增量。"""
    cols = cols or ["buy_elg_amount", "sell_elg_amount", "buy_lg_amount", "sell_lg_amount", "net_mf_amount"]
    dates = _trade_dates(start, end)
    df = _load_or_fetch_by_day("moneyflow", dates, cols, refresh=refresh)
    if df.empty:
        return df
    return df[df["ts_code"].isin(set(codes))].copy()


# ===== 3. 标签 join =====
def build_panel(
    dedup: str | None = None,
    pad_before: int = 12,
    pad_after: int = 6,
    analysis_dir: Path | str | None = None,
    dates: list[str] | None = None,
) -> pd.DataFrame:
    """构建带标签面板并落库 out/panel.csv。

    默认保留全部轮次（一天多轮扫描每轮独立记录），join 未来标签，
    并用 total_score() 重算总分（对齐 2026-07-02 重构后的唯一总分）。

    pad_before/after: 评分日窗口外多取的交易日（含 trailing_10 与 fwd_3 缓冲）。
    dates: 若指定，只回测这些交易日（YYYYMMDD）。
    """
    rec = load_analysis_records(dedup=dedup, analysis_dir=analysis_dir, dates=dates)
    if rec.empty:
        raise RuntimeError("无 analysis 记录")
    codes = sorted(rec["code"].unique().tolist())
    dmin, dmax = rec["date"].min(), rec["date"].max()
    start = _shift_calendar(dmin, -pad_before * 2)
    end = _shift_calendar(dmax, pad_after * 2)

    bars = pull_daily_bars(codes, start, end)
    if bars.empty:
        raise RuntimeError("日线拉取为空（检查 .env / tushare token）")

    # 拉 daily_basic 供 total_score 使用（circ_mv/turnover/vol_ratio/pe/pb）
    dbasic = pull_daily_basic_bars(codes, start, end)

    # 拉 moneyflow 供资金流特征
    mf = pull_moneyflow_bars(codes, start, end)

    # 按 code 分组
    label_rows = []
    grouped_daily = {c: g for c, g in bars.groupby("ts_code")}
    grouped_basic = {c: g for c, g in dbasic.groupby("ts_code")} if not dbasic.empty else {}
    grouped_moneyflow = {c: g for c, g in mf.groupby("ts_code")} if not mf.empty else {}

    for _, row in rec.iterrows():
        g = grouped_daily.get(row["code"])
        if g is None:
            label_rows.append({})
            continue
        d = g["trade_date"].tolist()
        c = g["close"].tolist()
        h = g["high"].tolist()
        p = g["pct_chg"].tolist()
        label_rows.append(L.compute_labels(d, c, h, p, row["date"]))

    lab = pd.DataFrame(label_rows)
    panel = pd.concat([rec.reset_index(drop=True), lab], axis=1)

    # ── 补面板特征 + 重算 total_score ──
    panel = _augment_and_score(panel, grouped_daily, grouped_basic, grouped_moneyflow)

    out_file = OUT_DIR / "panel.csv"
    panel.to_csv(out_file, index=False)
    return panel


def _augment_and_score(panel: pd.DataFrame, grouped_daily: dict, grouped_basic: dict,
                       grouped_moneyflow: dict | None = None) -> pd.DataFrame:
    """对面板每行补 total_score 所需的 PIT 特征列，然后调 total_score() 重算总分。"""
    import numpy as np
    from plays.limit_up.factors import REGISTRY, TOTAL_SCORE_COMPONENTS
    from plays.limit_up.total import total_score as _total_score

    total_scores = []
    comp_cols: dict[str, list] = {n: [] for n in TOTAL_SCORE_COMPONENTS}

    # 新增特征列缓存
    feat_cols = {
        "position_20d": [],
        "trailing_10": [],
        "trailing_5": [],
        "pct_chg_std_10d": [],
        "pct_chg_std_5d": [],
        "limit_up_count_20d": [],
        "limit_up_count_60d": [],
        "avg_amount_5d": [],
        "pct_chg_score_day": [],
        "turnover_rate": [],
        "volume_ratio": [],
        "circ_mv": [],
        "pe": [],
        "pb": [],
        "pullback_10d": [],
        "pullback_20d": [],
        "net_mf_amount": [],
        "net_mf_ratio": [],
        "buy_elg_ratio": [],
        "buy_lg_ratio": [],
    }

    for _, row in panel.iterrows():
        code = row["code"]
        date = row["date"]
        gd = grouped_daily.get(code)
        gb = grouped_basic.get(code) if grouped_basic else None
        if gd is None or gd.empty:
            total_scores.append(None)
            for n in TOTAL_SCORE_COMPONENTS:
                comp_cols[n].append(None)
            for c in feat_cols:
                feat_cols[c].append(None)
            continue

        dates = gd["trade_date"].tolist()
        closes = gd["close"].tolist()
        highs = gd["high"].tolist()
        lows = gd["low"].tolist() if "low" in gd.columns else [None] * len(closes)
        pcts = gd["pct_chg"].tolist()
        amounts = gd["amount"].tolist() if "amount" in gd.columns else [None] * len(closes)

        try:
            i = dates.index(date)
        except ValueError:
            total_scores.append(None)
            for n in TOTAL_SCORE_COMPONENTS:
                comp_cols[n].append(None)
            for c in feat_cols:
                feat_cols[c].append(None)
            continue

        # PIT 索引：用评分日前一个交易日（T-1）收盘，与生产 pipeline 保持一致
        pit_i = i - 1 if i >= 1 else i
        c0 = closes[pit_i] if pit_i >= 0 else closes[i]
        pit_date = dates[pit_i] if pit_i >= 0 else date

        # 20 日位置（基于 T-1 及之前 19 个交易日）
        lo20 = max(0, pit_i - 19)
        hs = [x for x in highs[lo20:pit_i+1] if x is not None]
        ls = [x for x in lows[lo20:pit_i+1] if x is not None]
        pos_20d = (c0 - min(ls)) / (max(hs) - min(ls)) if (c0 and hs and ls and max(hs) > min(ls)) else 0.5

        # trailing（T-1 相对 T-11/T-6）
        trailing_10 = (closes[pit_i] / closes[pit_i-10] - 1.0) if (pit_i >= 10 and closes[pit_i-10]) else 0.0
        trailing_5 = (closes[pit_i] / closes[pit_i-5] - 1.0) if (pit_i >= 5 and closes[pit_i-5]) else 0.0

        def _std(w):
            lo = max(0, pit_i - w + 1)
            seq = [p for p in pcts[lo:pit_i+1] if p is not None]
            return float(np.std(seq, ddof=0)) if len(seq) >= 2 else 0.0

        # 5 日均成交额（T-1 往前 5 日；daily.amount 单位千元 → 元）
        amt_seq = [a for a in amounts[max(0, pit_i-4):pit_i+1] if a is not None]
        avg_amount_5d = float(np.mean(amt_seq)) * 1000 if amt_seq else 0.0

        # 涨停基因（截至 T-1，不含评分日当天）
        def _limit_count(window):
            lo = max(0, pit_i - window + 1)
            return sum(1 for p in pcts[lo:pit_i+1] if p is not None and p >= 9.8)

        limit_up_count_20d = float(_limit_count(20))
        limit_up_count_60d = float(_limit_count(60))

        # 回撤（基于 T-1 窗口高点）
        def _pullback(window):
            whs = [h for h in highs[max(0, pit_i - window + 1):pit_i+1] if h is not None]
            if whs and c0:
                hmax = max(whs)
                return max(0.0, (hmax - c0) / hmax) if hmax > 0 else 0.0
            return 0.0

        pullback_10d = _pullback(10)
        pullback_20d = _pullback(20)

        # daily_basic 取 T-1（与生产 pipeline 一致）
        db_row = {}
        if gb is not None and not gb.empty:
            db_sub = gb[gb["trade_date"] == pit_date]
            if db_sub.empty:
                # fallback：取最近一个不大于 pit_date 的日期
                for d in sorted(gb["trade_date"].unique().tolist(), reverse=True):
                    if d <= pit_date:
                        db_sub = gb[gb["trade_date"] == d]
                        break
            if not db_sub.empty:
                db_row = db_sub.iloc[0].to_dict()

        def _db(key, default=0.0):
            v = db_row.get(key)
            try:
                return float(v) if v is not None else default
            except (ValueError, TypeError):
                return default

        # moneyflow 取 T-1
        mf_row = {}
        gm = grouped_moneyflow.get(code) if grouped_moneyflow else None
        if gm is not None and not gm.empty:
            mf_sub = gm[gm["trade_date"] == pit_date]
            if mf_sub.empty:
                for d in sorted(gm["trade_date"].unique().tolist(), reverse=True):
                    if d <= pit_date:
                        mf_sub = gm[gm["trade_date"] == d]
                        break
            if not mf_sub.empty:
                mf_row = mf_sub.iloc[0].to_dict()

        def _mf(key, default=0.0):
            v = mf_row.get(key)
            try:
                return float(v) if v is not None else default
            except (ValueError, TypeError):
                return default

        net_mf = _mf("net_mf_amount")
        buy_elg = _mf("buy_elg_amount")
        sell_elg = _mf("sell_elg_amount")
        buy_lg = _mf("buy_lg_amount")
        sell_lg = _mf("sell_lg_amount")
        t1_amount = amounts[pit_i] * 1000 if (pit_i >= 0 and amounts[pit_i]) else 0.0
        net_mf_ratio = net_mf / t1_amount if t1_amount > 0 else 0.0
        buy_elg_ratio = buy_elg / (buy_elg + sell_elg) if (buy_elg + sell_elg) > 0 else 0.5
        buy_lg_ratio = buy_lg / (buy_lg + sell_lg) if (buy_lg + sell_lg) > 0 else 0.5

        feat = {
            "sentiment": float(row.get("sentiment") or 0.0),
            "shortterm": float(row.get("shortterm") or 0.0),
            "technical": float(row.get("technical") or 0.0),
            "fundflow": float(row.get("fundflow") or 0.0),
            "fundamental": float(row.get("fundamental") or 0.0),
            "position_20d": pos_20d,
            "trailing_10": trailing_10,
            "trailing_5": trailing_5,
            "pct_chg_std_10d": _std(10),
            "pct_chg_std_5d": _std(5),
            "limit_up_count_20d": limit_up_count_20d,
            "limit_up_count_60d": limit_up_count_60d,
            "avg_amount_5d": avg_amount_5d,
            "pct_chg_score_day": float(pcts[i] or 0.0),
            "turnover_rate": _db("turnover_rate", 5.0),
            "volume_ratio": _db("volume_ratio", 1.0),
            "circ_mv": _db("circ_mv", 0.0),
            "pe": _db("pe", 999.0),
            "pb": _db("pb", 999.0),
            "pullback_10d": pullback_10d,
            "pullback_20d": pullback_20d,
            "net_mf_amount": net_mf,
            "net_mf_ratio": net_mf_ratio,
            "buy_elg_ratio": buy_elg_ratio,
            "buy_lg_ratio": buy_lg_ratio,
        }

        ts = _total_score(feat)
        total_scores.append(ts)
        for n in TOTAL_SCORE_COMPONENTS:
            comp_cols[n].append(round(REGISTRY[n](feat), 2))
        for c in feat_cols:
            feat_cols[c].append(feat[c])

    panel = panel.copy()
    panel["total_score"] = total_scores
    for n, vals in comp_cols.items():
        panel[f"comp_{n}"] = vals
    for c, vals in feat_cols.items():
        panel[c] = vals
    return panel


def _shift_calendar(yyyymmdd: str, days: int) -> str:
    from datetime import datetime, timedelta

    d = datetime.strptime(yyyymmdd, "%Y%m%d") + timedelta(days=days)
    return d.strftime("%Y%m%d")


if __name__ == "__main__":
    import sys

    sys.path.insert(0, str(PLAY_DIR.parent.parent))
    p = build_panel()
    print(f"panel: {len(p)} 行, 列: {list(p.columns)}")
    print(p[["code", "date"] + DIM_COLS + ["fwd_ret_3", "hit_limit_3", "trailing_5"]].head(10))
