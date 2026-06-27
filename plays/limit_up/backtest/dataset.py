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
ANALYSIS_DIR = PLAY_DIR / "data" / "analysis"
CACHE_DIR = Path(__file__).resolve().parent / "cache"
OUT_DIR = Path(__file__).resolve().parent / "out"
CACHE_DIR.mkdir(exist_ok=True)
OUT_DIR.mkdir(exist_ok=True)

DIM_COLS = ["fundamental", "technical", "fundflow", "sentiment", "shortterm"]


# ===== 1. 评分记录 =====
def load_analysis_records(dedup: str = "last") -> pd.DataFrame:
    """读取所有 analysis JSON，展开为每行一条 (code,date) 评分。

    dedup: 同一 (code,date) 多次扫描的去重方式
      - "last": 取当日最后一次扫描（文件名时间戳最大），最接近收盘决策
      - "max_total": 取 total 最高的一次
      - "mean": 各维度取均值
    """
    rows = []
    for f in sorted(glob.glob(str(ANALYSIS_DIR / "*.json"))):
        fname = os.path.basename(f)
        # skip v2 signal-based files — different format, no dimension scores
        if fname.startswith("v2_"):
            continue
        date = fname.split("_")[0]  # YYYYMMDD
        ts = fname.replace(".json", "")  # YYYYMMDD_HHMM 用于 last 排序
        try:
            recs = json.load(open(f))
        except Exception:
            continue
        for r in recs:
            sc = r.get("scores", {})
            res = r.get("resonance", {}) or {}
            rows.append(
                {
                    "code": r.get("code"),
                    "date": date,
                    "ts": ts,
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
            )
    df = pd.DataFrame(rows).dropna(subset=["code", "date"])
    if df.empty:
        return df
    if dedup == "last":
        df = df.sort_values("ts").groupby(["code", "date"], as_index=False).last()
    elif dedup == "max_total":
        df = df.sort_values("total").groupby(["code", "date"], as_index=False).last()
    elif dedup == "mean":
        df = df.groupby(["code", "date"], as_index=False)[
            DIM_COLS + ["total", "pct_chg_score_day", "resonance_count", "is_resonance"]
        ].mean()
    return df.drop(columns=["ts"], errors="ignore").reset_index(drop=True)


def _f(v):
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (ValueError, TypeError):
        return None


# ===== 2. 日线拉取（懒加载 tushare）=====
def pull_daily_bars(codes: list[str], start: str, end: str, refresh: bool = False) -> pd.DataFrame:
    """按 trade_date 批量拉日线，缓存到 cache/daily.parquet。

    tushare daily 单次返回某交易日全市场，再过滤 codes，调用次数 = 交易日数。
    """
    cache_f = CACHE_DIR / f"daily_{start}_{end}.parquet"
    if cache_f.exists() and not refresh:
        df = pd.read_parquet(cache_f)
        return df[df["ts_code"].isin(codes)].copy()

    from scripts.tu_share import call_tushare  # 懒加载，避免无 .env 时 import 崩溃

    # 交易日历
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

    frames = []
    code_set = set(codes)
    for d in trade_dates:
        res = call_tushare(
            "daily",
            {"trade_date": d},
            "ts_code,trade_date,open,high,low,close,pre_close,pct_chg,vol,amount",
        )
        if not res.get("data"):
            continue
        fields = res["data"]["fields"]
        items = res["data"]["items"]
        sub = pd.DataFrame(items, columns=fields)
        sub = sub[sub["ts_code"].isin(code_set)]
        frames.append(sub)
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    for c in ["open", "high", "low", "close", "pre_close", "pct_chg", "vol", "amount"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["trade_date"] = df["trade_date"].astype(str)
    df = df.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
    df.to_parquet(cache_f)
    return df


# ===== 3. 标签 join =====
def build_panel(dedup: str = "last", pad_before: int = 12, pad_after: int = 6) -> pd.DataFrame:
    """构建带标签面板并落库 out/panel.csv。

    pad_before/after: 评分日窗口外多取的交易日（含 trailing_10 与 fwd_3 缓冲）。
    """
    rec = load_analysis_records(dedup=dedup)
    if rec.empty:
        raise RuntimeError("无 analysis 记录")
    codes = sorted(rec["code"].unique().tolist())
    dmin, dmax = rec["date"].min(), rec["date"].max()
    start = _shift_calendar(dmin, -pad_before * 2)  # 自然日近似，多拿一些再按交易日对齐
    end = _shift_calendar(dmax, pad_after * 2)

    bars = pull_daily_bars(codes, start, end)
    if bars.empty:
        raise RuntimeError("日线拉取为空（检查 .env / tushare token）")

    # 按 code 分组建序列
    label_rows = []
    grouped = {c: g for c, g in bars.groupby("ts_code")}
    for _, row in rec.iterrows():
        g = grouped.get(row["code"])
        if g is None:
            label_rows.append({})
            continue
        dates = g["trade_date"].tolist()
        closes = g["close"].tolist()
        highs = g["high"].tolist()
        pcts = g["pct_chg"].tolist()
        label_rows.append(L.compute_labels(dates, closes, highs, pcts, row["date"]))

    lab = pd.DataFrame(label_rows)
    panel = pd.concat([rec.reset_index(drop=True), lab], axis=1)
    panel.to_csv(OUT_DIR / "panel.csv", index=False)
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
