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

from plays.limit_up.pit_features import build_pit_features
from plays.limit_up.utils import log_data_audit
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
    "top_list": {
        "ts_code": False, "trade_date": False,
        "name": False, "close": True, "pct_change": True,
        "turnover_rate": True, "amount": True, "l_sell": True,
        "l_buy": True, "l_amount": True, "net_amount": True,
        "net_rate": True, "amount_rate": True, "float_values": True,
        "reason": False,
    },
    "top_inst": {
        "ts_code": False, "trade_date": False,
        "exalter": False, "buy": True, "buy_rate": True,
        "sell": True, "sell_rate": True, "net_buy": True,
        "side": False, "reason": False,
    },
    "auction": {
        "ts_code": False, "trade_date": False,
        "vol": True, "price": True, "amount": True,
        "pre_close": True, "turnover_rate": True, "volume_ratio": True, "float_share": True,
    },
}

# 面板内部名 -> Tushare API 名（多数相同，个别需要映射）
_API_TUSHARE_NAME: dict[str, str] = {
    # 2026-07-31 修复：原 stk_auction_o（盘后）只返回 vol/amount 无 price，
    # 导致训练集 auc_pct 算不出（pct_chg_score_day 回退 T-1 收盘涨幅），
    # 与生产 stk_auction（9:25 实时，有 price/pre_close）口径不一致。
    "auction": "stk_auction",
}


def _is_main_board(code: str) -> bool:
    """判断是否为主板（00/60 开头）。"""
    pure = code.split(".")[0] if code else ""
    return pure.startswith(("00", "60"))


def _validate_day_df(api: str, date: str, df: pd.DataFrame) -> pd.DataFrame:
    """校验某天面板数据的日期一致性、异常市值，并写审计日志。

    返回可能被修正（如 trade_date 强转字符串）的 DataFrame。
    """
    if df.empty:
        return df

    # 1. trade_date 与文件名一致
    if "trade_date" in df.columns:
        df["trade_date"] = df["trade_date"].astype(str)
        unique_dates = df["trade_date"].dropna().unique()
        if len(unique_dates) != 1 or str(unique_dates[0]) != str(date):
            log_data_audit(
                f"[panel] {api}/{date}.parquet trade_date 不匹配: "
                f"unique={list(unique_dates)}, expected={date}"
            )

    # 2. daily_basic circ_mv 异常（主板 < 1000 万元视为异常）
    if api == "daily_basic" and "circ_mv" in df.columns and "ts_code" in df.columns:
        bad = df[(df["circ_mv"] > 0) & (df["circ_mv"] < 1000) & df["ts_code"].apply(_is_main_board)]
        if not bad.empty:
            samples = bad[["ts_code", "circ_mv"]].head(5).to_dict("records")
            log_data_audit(
                f"[panel] {api}/{date}.parquet 发现主板异常 circ_mv(<1000万元): {samples}"
            )

    return df


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


def _fetch_day_partial(api: str, date: str, fields_needed: list[str], timeout: int = 10) -> pd.DataFrame:
    """拉某一天全市场的指定字段。fields_needed 不含 ts_code/trade_date 也会自动补上。"""
    from scripts.tu_share import call_tushare
    api_name = _API_TUSHARE_NAME.get(api, api)
    key_fields = ["ts_code", "trade_date"]
    fields_req = list(dict.fromkeys(key_fields + fields_needed))  # 去重保序
    fields_str = ",".join(fields_req)
    res = call_tushare(api_name, {"trade_date": date}, fields_str, timeout=timeout)
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


def _ensure_day_columns(api: str, date: str, cols_needed: list[str], timeout: int = 10) -> pd.DataFrame:
    """确保某天 parquet 中含有所需字段：缺则按列增量拉取并合并。"""
    day_dir = PANEL_DIR / api
    day_dir.mkdir(parents=True, exist_ok=True)
    day_file = day_dir / f"{date}.parquet"

    # 排除主键列（key 列总是存在）
    non_key = [c for c in cols_needed if c not in ("ts_code", "trade_date")]

    if day_file.exists():
        df = pd.read_parquet(day_file)
        df = _validate_day_df(api, date, df)
        missing = [c for c in non_key if c not in df.columns]
        if not missing:
            return df
        # 列增量拉取
        print(f"  [{api}] {date} 缺列 {missing}, 增量拉取")
        add = _fetch_day_partial(api, date, missing, timeout=timeout)
        add = _validate_day_df(api, date, add)
        if add.empty:
            return df
        # 接口可能不返回全部请求字段（如 stk_auction_o 只有 vol/amount）：
        # 实际返回的列做合并，未返回的列补 NaN，保证列契约不炸
        got = [c for c in missing if c in add.columns]
        not_got = [c for c in missing if c not in add.columns]
        if not_got:
            for c in not_got:
                df[c] = pd.NA
            log_data_audit(
                f"[panel] {api}/{date} 接口未返回字段 {not_got}，已补 NaN"
            )
        if got:
            df = df.merge(add[["ts_code", "trade_date"] + got], on=["ts_code", "trade_date"], how="left")
        df = _validate_day_df(api, date, df)
        df.to_parquet(day_file)
        return df

    # 整天新拉
    df = _fetch_day_partial(api, date, non_key, timeout=timeout)
    df = _validate_day_df(api, date, df)
    if df.empty:
        return df
    df.to_parquet(day_file)
    return df


def _load_or_fetch_by_day(api: str, dates: list[str], cols_needed: list[str],
                            refresh: bool = False, timeout: int = 10) -> pd.DataFrame:
    """为一组交易日装载 api 所需字段，行 + 列都做增量。"""
    frames = []
    for d in dates:
        if refresh:
            (PANEL_DIR / api / f"{d}.parquet").unlink(missing_ok=True)
        df = _ensure_day_columns(api, d, cols_needed, timeout=timeout)
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


def pull_top_list_bars(codes: list[str], start: str, end: str, refresh: bool = False,
                       cols: list[str] | None = None) -> pd.DataFrame:
    """按天拉/读龙虎榜上榜明细，按列增量。"""
    cols = cols or ["name", "close", "pct_change", "turnover_rate", "amount",
                    "l_sell", "l_buy", "l_amount", "net_amount", "net_rate",
                    "amount_rate", "float_values", "reason"]
    dates = _trade_dates(start, end)
    df = _load_or_fetch_by_day("top_list", dates, cols, refresh=refresh)
    if df.empty:
        return df
    return df[df["ts_code"].isin(set(codes))].copy()


def pull_top_inst_bars(codes: list[str], start: str, end: str, refresh: bool = False,
                        cols: list[str] | None = None) -> pd.DataFrame:
    """按天拉/读龙虎榜机构席位明细，按列增量。"""
    cols = cols or ["exalter", "buy", "buy_rate", "sell", "sell_rate", "net_buy", "side", "reason"]
    dates = _trade_dates(start, end)
    df = _load_or_fetch_by_day("top_inst", dates, cols, refresh=refresh)
    if df.empty:
        return df
    return df[df["ts_code"].isin(set(codes))].copy()


def pull_auction_bars(codes: list[str], start: str, end: str, refresh: bool = False,
                      cols: list[str] | None = None) -> pd.DataFrame:
    """按天拉/读集合竞价数据（Tushare stk_auction），按列增量。

    默认拉取 price/pre_close/vol/amount/turnover_rate/volume_ratio，
    用于构造 auc_* 特征 + auc_pct 竞价涨幅（覆盖 pct_chg_score_day）。
    stk_auction 接口响应较慢，默认 timeout 30s。
    """
    cols = cols or ["price", "pre_close", "vol", "amount", "turnover_rate", "volume_ratio"]
    dates = _trade_dates(start, end)
    df = _load_or_fetch_by_day("auction", dates, cols, refresh=refresh, timeout=30)
    if df.empty:
        return df
    return df[df["ts_code"].isin(set(codes))].copy()


def pull_intraday_metrics(codes: list[str], dates: list[str], refresh: bool = False,
                          max_workers: int = 8, per_call_timeout: float = 15.0) -> pd.DataFrame:
    """按天拉/读 jvQuant 历史分时数据聚合指标。

    返回字段：ts_code, trade_date, vwap, close, open, high, low, volume,
             amount_est, morning_vol_ratio, afternoon_strength, tail_vol_ratio。
    数据落盘到 wiki/raw/limit-up/panel/intraday/YYYYMMDD.parquet，按天增量。

    jvQuant 单股查询较慢（~2-4s），默认 8 线程并行，可把 8k 次查询压到 ~5min。
    """
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
    from scripts.jvquant_client import get_jvquant_client

    try:
        client = get_jvquant_client()
    except Exception as e:
        print(f"  [intraday] jvQuant 客户端初始化失败: {e}")
        return pd.DataFrame()

    day_dir = PANEL_DIR / "intraday"
    day_dir.mkdir(parents=True, exist_ok=True)
    target_codes = set(codes)
    frames: list[pd.DataFrame] = []

    def _fetch_one(code_full: str, date: str) -> dict | None:
        short = code_full.split(".")[0]
        try:
            metrics = client.get_intraday_metrics(short, date)
            if metrics:
                return {"ts_code": code_full, "trade_date": date, **metrics}
        except Exception as e:
            print(f"  [intraday] {code_full} {date} 拉取失败: {e}")
        return None

    for date in dates:
        day_file = day_dir / f"{date}.parquet"
        existing = pd.DataFrame()
        need = target_codes.copy()

        if day_file.exists() and not refresh:
            existing = pd.read_parquet(day_file)
            if "ts_code" in existing.columns:
                need = need - set(existing["ts_code"].unique())

        if need:
            rows: list[dict] = []
            tasks = [(c, date) for c in sorted(need)]
            with ThreadPoolExecutor(max_workers=max_workers) as ex:
                futures = {ex.submit(_fetch_one, c, date): c for c, _ in tasks}
                for fut in futures:
                    try:
                        row = fut.result(timeout=per_call_timeout)
                        if row:
                            rows.append(row)
                    except FutureTimeoutError:
                        print(f"  [intraday] {futures[fut]} {date} 超时")
                    except Exception as e:
                        print(f"  [intraday] {futures[fut]} {date} 异常: {e}")

            if rows:
                new_df = pd.DataFrame(rows)
                combined = pd.concat([existing, new_df], ignore_index=True) if not existing.empty else new_df
                combined = combined.drop_duplicates(subset=["ts_code", "trade_date"], keep="last")
                combined.to_parquet(day_file, index=False)
                frames.append(combined)
            elif not existing.empty:
                frames.append(existing)
        elif not existing.empty:
            frames.append(existing)

    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True).sort_values(["ts_code", "trade_date"]).reset_index(drop=True)


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

    # 加载概念缓存供 PIT 动量特征使用
    try:
        from plays.limit_up.strategies import factor_ctx
        if factor_ctx._CONCEPT_DAILY_CACHE is None or factor_ctx._CONCEPT_MEMBER_CACHE is None:
            factor_ctx.load_concept_data_from_cache()
    except Exception as e:
        print(f"  [warn] 概念缓存加载失败: {e}; sector_* 特征将使用默认值")

    bars = pull_daily_bars(codes, start, end)
    if bars.empty:
        raise RuntimeError("日线拉取为空（检查 .env / tushare token）")

    # 拉 daily_basic 供 total_score 使用（circ_mv/turnover/vol_ratio/pe/pb）
    dbasic = pull_daily_basic_bars(codes, start, end)

    # 拉 moneyflow 供资金流特征
    mf = pull_moneyflow_bars(codes, start, end)

    # 拉龙虎榜数据供 PIT 特征
    top_list = pull_top_list_bars(codes, start, end)
    top_inst = pull_top_inst_bars(codes, start, end)

    # 拉集合竞价数据供 auc_* 特征
    auction = pull_auction_bars(codes, start, end)

    # 拉日内分时指标供 id_* 特征
    intraday_dates = _trade_dates(start, end)
    intraday = pull_intraday_metrics(codes, intraday_dates)

    # 按 code 分组
    label_rows = []
    grouped_daily = {c: g for c, g in bars.groupby("ts_code")}
    grouped_basic = {c: g for c, g in dbasic.groupby("ts_code")} if not dbasic.empty else {}
    grouped_moneyflow = {c: g for c, g in mf.groupby("ts_code")} if not mf.empty else {}
    grouped_top_list = {c: g for c, g in top_list.groupby("ts_code")} if not top_list.empty else {}
    grouped_top_inst = {c: g for c, g in top_inst.groupby("ts_code")} if not top_inst.empty else {}
    grouped_auction = {c: g for c, g in auction.groupby("ts_code")} if not auction.empty else {}
    grouped_intraday = {c: g for c, g in intraday.groupby("ts_code")} if not intraday.empty else {}

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
    panel = _augment_and_score(panel, grouped_daily, grouped_basic, grouped_moneyflow,
                               grouped_top_list, grouped_top_inst, grouped_auction,
                               grouped_intraday)

    out_file = OUT_DIR / f"panel_{dmin}_{dmax}.csv"
    panel.to_csv(out_file, index=False)
    return panel


def _augment_and_score(panel: pd.DataFrame, grouped_daily: dict, grouped_basic: dict,
                       grouped_moneyflow: dict | None = None,
                       grouped_top_list: dict | None = None,
                       grouped_top_inst: dict | None = None,
                       grouped_auction: dict | None = None,
                       grouped_intraday: dict | None = None) -> pd.DataFrame:
    """对面板每行补 total_score 所需的 PIT 特征列，然后调 total_score() 重算总分。"""
    from plays.limit_up.factors import REGISTRY, TOTAL_SCORE_COMPONENTS
    from plays.limit_up.total import total_score as _total_score

    total_scores = []
    comp_cols: dict[str, list] = {n: [] for n in TOTAL_SCORE_COMPONENTS}

    # 预先把 grouped DataFrame 转成 dict/list，复用给 build_pit_features
    daily_rows_by_code: dict[str, list[dict]] = {}
    for code, gd in grouped_daily.items():
        daily_rows_by_code[code] = gd.sort_values("trade_date").to_dict("records")

    basic_by_code_date: dict[str, dict[str, dict]] = {}
    for code, gb in (grouped_basic or {}).items():
        basic_by_code_date[code] = {}
        for _, r in gb.iterrows():
            basic_by_code_date[code][str(r["trade_date"])] = r.to_dict()

    mf_by_code_date: dict[str, dict[str, dict]] = {}
    for code, gm in (grouped_moneyflow or {}).items():
        mf_by_code_date[code] = {}
        for _, r in gm.iterrows():
            mf_by_code_date[code][str(r["trade_date"])] = r.to_dict()

    top_list_by_code_date: dict[str, dict[str, dict]] = {}
    for code, gt in (grouped_top_list or {}).items():
        top_list_by_code_date[code] = {}
        for _, r in gt.iterrows():
            top_list_by_code_date[code][str(r["trade_date"])] = r.to_dict()

    top_inst_by_code_date: dict[str, dict[str, list[dict]]] = {}
    for code, gi in (grouped_top_inst or {}).items():
        top_inst_by_code_date[code] = {}
        for _, r in gi.iterrows():
            top_inst_by_code_date[code].setdefault(str(r["trade_date"]), []).append(r.to_dict())

    auction_by_code_date: dict[str, dict[str, dict]] = {}
    for code, ga in (grouped_auction or {}).items():
        auction_by_code_date[code] = {}
        for _, r in ga.iterrows():
            auction_by_code_date[code][str(r["trade_date"])] = r.to_dict()

    intraday_by_code_date: dict[str, dict[str, dict]] = {}
    for code, gi in (grouped_intraday or {}).items():
        intraday_by_code_date[code] = {}
        for _, r in gi.iterrows():
            intraday_by_code_date[code][str(r["trade_date"])] = r.to_dict()

    model_mode = "model_score" in TOTAL_SCORE_COMPONENTS
    if model_mode:
        from plays.limit_up.factors.optimized.model_score import factor_model_score_batch

    feat_rows: list[dict] = []
    non_model_components = [n for n in TOTAL_SCORE_COMPONENTS if n != "model_score"]
    for _, row in panel.iterrows():
        code = row["code"]
        date = row["date"]
        gd = grouped_daily.get(code)
        if gd is None or gd.empty:
            for n in TOTAL_SCORE_COMPONENTS:
                comp_cols[n].append(None)
            feat_rows.append({})
            continue

        feat = build_pit_features(
            code=code,
            score_date=str(date),
            daily_rows=daily_rows_by_code[code],
            basic_by_date=basic_by_code_date.get(code, {}),
            moneyflow_by_date=mf_by_code_date.get(code, {}),
            auction_by_date=auction_by_code_date.get(code, {}),
            intraday_by_date=intraday_by_code_date.get(code, {}),
            top_list_by_date=top_list_by_code_date.get(code, {}),
            top_inst_by_date=top_inst_by_code_date.get(code, {}),
            pit_mode=True,
        )
        # 把维度分接入 feat，供 quality_combo 等现有因子使用
        for dim in DIM_COLS:
            feat[dim] = float(row.get(dim) or 0.0)

        for n in non_model_components:
            comp_cols[n].append(round(REGISTRY[n](feat), 2))
        if model_mode:
            # model_score 走批量预测，先占位
            comp_cols["model_score"].append(None)
        feat_rows.append(feat)

    feat_df = pd.DataFrame(feat_rows)
    panel = panel.copy().reset_index(drop=True)

    if model_mode:
        # 批量模型预测：避免逐行 XGBoost 预测的巨大开销
        model_scores = factor_model_score_batch(feat_df)
        for i, score in enumerate(model_scores):
            if feat_rows[i]:
                comp_cols["model_score"][i] = round(float(score), 2)
                total_scores.append(comp_cols["model_score"][i])
            else:
                comp_cols["model_score"][i] = None
                total_scores.append(None)
    else:
        for feat in feat_rows:
            total_scores.append(_total_score(feat) if feat else None)

    panel["total_score"] = total_scores
    for n, vals in comp_cols.items():
        panel[f"comp_{n}"] = vals
    if not feat_df.empty:
        # 避免与已有列冲突：以 feat_df 为准
        for c in feat_df.columns:
            if c in panel.columns:
                panel = panel.drop(columns=[c])
        panel = pd.concat([panel, feat_df.reset_index(drop=True)], axis=1)
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
