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

    # 按 code 分组
    label_rows = []
    grouped_daily = {c: g for c, g in bars.groupby("ts_code")}
    grouped_basic = {c: g for c, g in dbasic.groupby("ts_code")} if not dbasic.empty else {}

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
    panel = _augment_and_score(panel, grouped_daily, grouped_basic)

    out_file = OUT_DIR / "panel.csv"
    panel.to_csv(out_file, index=False)
    return panel


def _augment_and_score(panel: pd.DataFrame, grouped_daily: dict, grouped_basic: dict) -> pd.DataFrame:
    """对面板每行补 total_score 所需的特征列，然后调 total_score() 重算总分。"""
    import numpy as np
    from plays.limit_up.factors import REGISTRY, TOTAL_SCORE_COMPONENTS
    from plays.limit_up.total import total_score as _total_score

    total_scores = []
    comp_cols: dict[str, list] = {n: [] for n in TOTAL_SCORE_COMPONENTS}

    for _, row in panel.iterrows():
        code = row["code"]
        date = row["date"]
        gd = grouped_daily.get(code)
        gb = grouped_basic.get(code) if grouped_basic else None
        if gd is None or gd.empty:
            total_scores.append(None)
            for n in TOTAL_SCORE_COMPONENTS:
                comp_cols[n].append(None)
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
            continue

        # 特征
        lo20 = max(0, i - 19)
        hs = [x for x in highs[lo20:i+1] if x is not None]
        ls = [x for x in lows[lo20:i+1] if x is not None]
        c0 = closes[i]
        pos_20d = (c0 - min(ls)) / (max(hs) - min(ls)) if (c0 and hs and ls and max(hs) > min(ls)) else 0.5
        trailing_10 = (c0 / closes[i-10] - 1.0) if (i >= 10 and closes[i-10]) else 0.0

        def _std(w):
            lo = max(0, i - w + 1)
            seq = [p for p in pcts[lo:i+1] if p is not None]
            return float(np.std(seq, ddof=0)) if len(seq) >= 2 else 0.0

        # 5 日均成交额（daily.amount 单位千元 → 元）
        amt_seq = [a for a in amounts[max(0, i-4):i+1] if a is not None]
        avg_amount_5d = float(np.mean(amt_seq)) * 1000 if amt_seq else 0.0

        # daily_basic on that day
        db_row = {}
        if gb is not None and not gb.empty:
            db_sub = gb[gb["trade_date"] == date]
            if not db_sub.empty:
                db_row = db_sub.iloc[0].to_dict()

        feat = {
            "sentiment": float(row.get("sentiment") or 0.0),
            "shortterm": float(row.get("shortterm") or 0.0),
            "technical": float(row.get("technical") or 0.0),
            "fundflow": float(row.get("fundflow") or 0.0),
            "fundamental": float(row.get("fundamental") or 0.0),
            "position_20d": pos_20d,
            "trailing_10": trailing_10,
            "pct_chg_std_10d": _std(10),
            "limit_up_count_20d": 0.0,   # 面板无此列时，volatility_combo 会得 0 分
            "avg_amount_5d": avg_amount_5d,
        }

        ts = _total_score(feat)
        total_scores.append(ts)
        for n in TOTAL_SCORE_COMPONENTS:
            comp_cols[n].append(round(REGISTRY[n](feat), 2))

    panel = panel.copy()
    panel["total_score"] = total_scores
    for n, vals in comp_cols.items():
        panel[f"comp_{n}"] = vals
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
