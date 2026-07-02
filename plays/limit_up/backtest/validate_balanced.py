#!/usr/bin/env python3
"""
真实扫描信号验证：用 plays/limit_up/data/analysis/ 里的实际评分记录，
按 pipeline 中最新 balanced_total_pit 公式重新打分，Top-3 推送后计算命中率/胜率。

不调用实时行情 API；daily/daily_basic 从本地 parquet 缓存读取，缺失时从 Tushare 补全。
"""
from __future__ import annotations

import glob
import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd


PROJECT_DIR = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_DIR))

PLAY_DIR = Path(__file__).resolve().parent.parent
ANALYSIS_DIR = PLAY_DIR / "data" / "analysis"
CACHE_DIR = Path(__file__).resolve().parent / "cache"
LIMIT_PCT = 9.8


def load_daily_bars() -> pd.DataFrame:
    """加载本地缓存的日线数据。"""
    parquets = sorted([p for p in CACHE_DIR.glob("daily_*.parquet") if "basic" not in p.name])
    if not parquets:
        raise FileNotFoundError("无 daily parquet 缓存，先运行 dataset.build_panel()")
    df = pd.read_parquet(parquets[-1])
    df["trade_date"] = df["trade_date"].astype(str)
    numeric_cols = ["open", "high", "low", "close", "pre_close", "pct_chg", "vol", "amount"]
    for c in numeric_cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)


def load_daily_basic_bars() -> pd.DataFrame:
    """加载本地缓存的 daily_basic 数据，缺失则尝试从 Tushare 补全。"""
    parquets = sorted(CACHE_DIR.glob("dbasic_*.parquet"))
    if parquets:
        df = pd.read_parquet(parquets[-1])
    else:
        # 尝试按最新 daily parquet 的日期范围拉取
        from plays.limit_up.backtest.dataset import pull_daily_basic_bars
        daily_parquets = sorted(CACHE_DIR.glob("daily_*.parquet"))
        if not daily_parquets:
            raise FileNotFoundError("无 daily parquet，无法确定 daily_basic 拉取范围")
        name = daily_parquets[-1].stem  # daily_YYYYMMDD_YYYYMMDD
        parts = name.split("_")
        start, end = parts[1], parts[2]
        all_codes = load_daily_bars()["ts_code"].unique().tolist()
        df = pull_daily_basic_bars(all_codes, start, end)
    df["trade_date"] = df["trade_date"].astype(str)
    for c in ["pe", "pb", "circ_mv", "turnover_rate", "volume_ratio"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)


def load_analysis_files(analysis_dir: Path | None = None) -> list[tuple[str, str, list[dict]]]:
    """读取 analysis 文件，返回 (date, timestamp, records) 列表。"""
    analysis_dir = analysis_dir or ANALYSIS_DIR
    out = []
    for f in sorted(glob.glob(str(analysis_dir / "*.json"))):
        fname = os.path.basename(f)
        if fname.startswith("v2_"):
            continue
        parts = fname.replace(".json", "").split("_")
        if len(parts) != 2:
            continue
        date, ts = parts
        try:
            recs = json.load(open(f))
        except Exception:
            continue
        if not isinstance(recs, list) or not recs or recs[0].get("_empty"):
            continue
        out.append((date, ts, recs))
    return out


def _limit_up_count(code: str, score_date: str, bars: pd.DataFrame, days: int) -> int:
    """近 N 个交易日（含今日）涨停次数。"""
    g = bars[bars["ts_code"] == code]
    if g.empty:
        return 0
    past = g[g["trade_date"] <= score_date].tail(days)
    return int((past["pct_chg"] >= LIMIT_PCT).sum())


def _extract_pit_features(
    code: str,
    score_date: str,
    bars: pd.DataFrame,
    basic_bars: pd.DataFrame,
) -> dict:
    """从本地缓存提取 PIT 特征，与 pipeline._extract_pit_features 对齐。"""
    g = bars[bars["ts_code"] == code]
    g = g[g["trade_date"] <= score_date].sort_values("trade_date", ascending=False)
    daily_rows = g.to_dict("records")

    feats = {
        "trailing_10": 0.0,
        "trailing_5": 0.0,
        "position_20d": 0.5,
        "pullback_10d": 0.1,
        "pullback_20d": 0.1,
        "pct_chg_std_10d": 0.0,
        "pct_chg_std_5d": 0.0,
        "max_pct_chg_5d": 0.0,
        "limit_up_count_20d": 0.0,
        "limit_up_count_60d": 0.0,
        "circ_mv": 0.0,
        "pe": 999.0,
        "pb": 999.0,
        "turnover_rate": 5.0,
        "volume_ratio": 1.0,
    }

    if not daily_rows or len(daily_rows) < 2:
        return feats

    def _safe_float(val, default=0.0):
        if val is None:
            return default
        try:
            return float(val)
        except (ValueError, TypeError):
            return default

    def trailing(days: int) -> float:
        if len(daily_rows) < days:
            return 0.0
        try:
            return float(daily_rows[0]["close"]) / float(daily_rows[days - 1]["close"]) - 1.0
        except Exception:
            return 0.0

    feats["trailing_10"] = trailing(10)
    feats["trailing_5"] = trailing(5)

    # position_20d
    try:
        highs = [float(r["high"]) for r in daily_rows[:20]]
        lows = [float(r["low"]) for r in daily_rows[:20]]
        closes = [float(r["close"]) for r in daily_rows[:20]]
        h20, l20, c0 = max(highs), min(lows), closes[0]
        if h20 > l20:
            feats["position_20d"] = (c0 - l20) / (h20 - l20)
    except Exception:
        pass

    # pullback
    def pullback(days: int) -> float:
        if len(daily_rows) < 2:
            return 0.1
        try:
            highs = [float(r["high"]) for r in daily_rows[:days]]
            c0 = float(daily_rows[0]["close"])
            h = max(highs)
            return max(0.0, (h - c0) / h) if h > 0 else 0.1
        except Exception:
            return 0.1

    feats["pullback_10d"] = pullback(10)
    feats["pullback_20d"] = pullback(20)

    # pct_chg std / max
    try:
        pcts = [_safe_float(r.get("pct_chg"), 0.0) for r in daily_rows[:10]]
        if len(pcts) >= 5:
            feats["pct_chg_std_10d"] = float(np.std(pcts, ddof=0)) if len(pcts) >= 2 else 0.0
            feats["max_pct_chg_5d"] = max(pcts[:5])
        pcts5 = [_safe_float(r.get("pct_chg"), 0.0) for r in daily_rows[:5]]
        if len(pcts5) >= 2:
            feats["pct_chg_std_5d"] = float(np.std(pcts5, ddof=0))
    except Exception:
        pass

    # daily_basic for score_date
    if not basic_bars.empty:
        bg = basic_bars[(basic_bars["ts_code"] == code) & (basic_bars["trade_date"] <= score_date)]
        bg = bg.sort_values("trade_date", ascending=False)
        if not bg.empty:
            basic = bg.iloc[0]
            feats["circ_mv"] = _safe_float(basic.get("circ_mv"), 0.0)
            feats["pe"] = _safe_float(basic.get("pe"), 999.0)
            feats["pb"] = _safe_float(basic.get("pb"), 999.0)
            feats["turnover_rate"] = _safe_float(basic.get("turnover_rate"), 5.0)
            feats["volume_ratio"] = _safe_float(basic.get("volume_ratio"), 1.0)

    feats["limit_up_count_20d"] = float(_limit_up_count(code, score_date, bars, 20))
    feats["limit_up_count_60d"] = float(_limit_up_count(code, score_date, bars, 60))

    return feats


def _factor_large_cap_limit_gene_pit(feats: dict, tech: float) -> float:
    circ_mv = feats["circ_mv"]
    gene20 = feats["limit_up_count_20d"]
    gene60 = feats["limit_up_count_60d"]

    score = 0.0
    if circ_mv >= 500_0000:
        score += 12.0
    elif circ_mv >= 200_0000:
        score += 9.0
    elif circ_mv >= 100_0000:
        score += 6.0
    elif circ_mv >= 50_0000:
        score += 3.0

    if gene20 >= 2:
        score += 10.0
    elif gene20 >= 1:
        score += 5.0
    if gene60 >= 3:
        score += 6.0
    elif gene60 >= 1:
        score += 3.0

    if tech >= 40:
        score += 6.0
    elif tech >= 25:
        score += 3.0

    return score


def _factor_volatility_activation_pit(feats: dict) -> float:
    std10 = feats["pct_chg_std_10d"]
    std5 = feats["pct_chg_std_5d"]
    position = feats["position_20d"]
    max5 = feats["max_pct_chg_5d"]

    score = 0.0
    if std10 > 4.5 and 0.30 <= position <= 0.70 and max5 > 5.0:
        score += 20.0
    elif std10 > 3.5 and 0.25 <= position <= 0.75 and max5 > 3.5:
        score += 12.0
    elif std5 > 3.0 and position > 0.20:
        score += 6.0

    if std10 < 2.0:
        score -= 4.0
    return score


def _factor_turnover_momentum_pit(feats: dict) -> float:
    turnover = feats["turnover_rate"]
    vol_ratio = feats["volume_ratio"]
    std5 = feats["pct_chg_std_5d"]
    position = feats["position_20d"]

    score = 0.0
    if turnover >= 15 and vol_ratio >= 1.5 and std5 >= 3.0 and 0.30 <= position <= 0.80:
        score += 18.0
    elif turnover >= 10 and vol_ratio >= 1.2 and std5 >= 2.5 and position >= 0.25:
        score += 12.0
    elif turnover >= 5 and vol_ratio >= 1.0 and std5 >= 2.0:
        score += 5.0

    if turnover < 2:
        score -= 5.0
    return score


def _factor_limit_gene_momentum_pit(feats: dict, tech: float) -> float:
    gene20 = feats["limit_up_count_20d"]
    gene60 = feats["limit_up_count_60d"]
    t10 = feats["trailing_10"]
    position = feats["position_20d"]

    score = 0.0
    if gene20 >= 3:
        score += 15.0
    elif gene20 >= 2:
        score += 10.0
    elif gene20 >= 1:
        score += 5.0

    if gene60 >= 4:
        score += 8.0
    elif gene60 >= 2:
        score += 4.0

    if tech >= 40:
        score += 8.0
    elif tech >= 25:
        score += 4.0

    if t10 > 0.35:
        score *= 0.60
    elif t10 > 0.25:
        score *= 0.80
    if position > 0.85:
        score *= 0.70

    return round(score, 2)


def _factor_growth_momentum_pit(feats: dict, st: float, tech: float) -> float:
    pe = feats["pe"]
    pb = feats["pb"]
    std10 = feats["pct_chg_std_10d"]

    score = 0.0
    if pe > 50 or pe <= 0:
        score += 6.0
    elif pe > 30:
        score += 3.0

    if pb > 5:
        score += 4.0
    elif pb > 3:
        score += 2.0

    if st >= 45 and tech >= 35 and std10 >= 3.5:
        score += 12.0
    elif st >= 35 and tech >= 25 and std10 >= 2.5:
        score += 6.0

    return score


def _factor_concept_momentum(cm: dict) -> float:
    ret1 = cm.get("ret1_max", 0.0)
    ret3 = cm.get("ret3_max", 0.0)
    ret5 = cm.get("ret5_max", 0.0)
    ret1_avg = cm.get("ret1_avg", 0.0)
    ret3_avg = cm.get("ret3_avg", 0.0)
    n_cpt = cm.get("n_concepts", 0.0)
    up_ratio = cm.get("up_ratio", 0.5)

    score = ret3 * 2.5
    if ret1 > 2.0:
        score += ret1 * 0.8
    elif ret1 < -2.0:
        if ret3 > 3.0:
            score += ret3 * 0.3
    if ret5 > 5.0:
        score += 5.0
    elif ret5 < -5.0:
        score -= 8.0
    if ret3_avg > 2.0 and ret1_avg > 0:
        score += ret3_avg * 1.0
    if n_cpt >= 5:
        if up_ratio > 0.6:
            score += 8.0
        elif up_ratio > 0.4:
            score += 4.0
    elif n_cpt >= 3:
        if up_ratio > 0.6:
            score += 5.0
    return round(score, 2)


def _factor_concept_up_streak(cm: dict) -> float:
    streak = cm.get("up_streak_max", 0.0)
    ret1 = cm.get("ret1_max", 0.0)
    if streak >= 3 and ret1 > 0:
        return 12.0
    elif streak >= 2 and ret1 > 0:
        return 7.0
    elif streak >= 2:
        return 3.0
    return 0.0


def _factor_concept_turnover(cm: dict) -> float:
    turn = cm.get("turn_5d_max", 0.0)
    ret3 = cm.get("ret3_max", 0.0)
    if turn > 15 and ret3 > 2:
        return 10.0
    elif turn > 10 and ret3 > 0:
        return 5.0
    elif turn > 20:
        return -5.0
    return 0.0


def _get_concept_momentum(code_short: str, concept_daily: pd.DataFrame, concept_members: pd.DataFrame) -> dict:
    """从缓存的 concept 数据获取股票概念动量。"""
    result = {
        "ret3_max": 0.0, "ret1_max": 0.0, "ret5_max": 0.0,
        "ret3_avg": 0.0, "ret1_avg": 0.0, "up_ratio": 0.5,
        "up_streak_max": 0, "turn_5d_max": 0.0, "turn_5d_avg": 0.0,
        "n_concepts": 0,
    }
    if concept_daily.empty or concept_members.empty:
        return result
    members = concept_members[concept_members["stock_code"] == code_short]
    if members.empty:
        return result
    cpt_codes = members["cpt_code"].unique().tolist()
    cd = concept_daily[concept_daily["cpt_code"].isin(cpt_codes)].copy()
    if cd.empty:
        return result
    cd["trade_date"] = cd["trade_date"].astype(str)
    latest_date = cd["trade_date"].max()
    latest = cd[cd["trade_date"] == latest_date]
    if latest.empty:
        return result

    pcts = latest["pct_change"].apply(lambda x: _safe_float(x, 0.0))
    turns = latest["turnover_rate"].apply(lambda x: _safe_float(x, 0.0))
    result["ret1_max"] = pcts.max()
    result["ret1_avg"] = pcts.mean()
    result["n_concepts"] = len(pcts)
    result["up_ratio"] = (pcts > 0).mean() if len(pcts) > 0 else 0.5
    result["turn_5d_max"] = turns.max()
    result["turn_5d_avg"] = turns.mean()

    streaks, ret3s, ret5s, turn5s = [], [], [], []
    for cpt in cpt_codes:
        g = cd[cd["cpt_code"] == cpt].sort_values("trade_date")
        if len(g) >= 1:
            p = g["pct_change"].apply(lambda x: _safe_float(x, 0.0))
            t = g["turnover_rate"].apply(lambda x: _safe_float(x, 0.0))
            ret3s.append(p.tail(3).sum())
            ret5s.append(p.tail(5).sum())
            turn5s.append(t.tail(5).mean())
            up = (p > 0).astype(int)
            streak = 0
            for v in up.tolist()[::-1]:
                if v:
                    streak += 1
                else:
                    break
            streaks.append(streak)

    if ret3s:
        result["ret3_max"] = max(ret3s)
        result["ret3_avg"] = sum(ret3s) / len(ret3s)
        result["ret5_max"] = max(ret5s)
        result["up_streak_max"] = max(streaks)
        result["turn_5d_max"] = max(turn5s) if turn5s else result["turn_5d_max"]
        result["turn_5d_avg"] = sum(turn5s) / len(turn5s) if turn5s else result["turn_5d_avg"]
    return result


def _compute_balanced(record: dict, code: str, score_date: str, bars: pd.DataFrame, basic_bars: pd.DataFrame,
                     concept_daily: pd.DataFrame, concept_members: pd.DataFrame) -> float:
    """复刻 pipeline._compute_balanced_total_batch 的五维度聚合 + 反追高惩罚。"""
    s = record.get("scores", {})
    st = float(s.get("shortterm", 0))
    tech = float(s.get("technical", 0))
    sent = float(s.get("sentiment", 0))
    fund = float(s.get("fundflow", 0))
    funda = float(s.get("fundamental", 0))

    # 仅保留追高惩罚所需的 PIT 特征
    feats = _extract_pit_features(code, score_date, bars, basic_bars)
    t10 = feats["trailing_10"]
    t5 = feats["trailing_5"]
    position_20d = feats["position_20d"]
    pullback_10d = feats["pullback_10d"]

    score = sent * 0.40
    score += st * 0.30
    score += tech * 0.20
    score += fund * 0.05
    score += funda * 0.05

    penalty = 1.0
    if t10 > 0.30:
        penalty *= 0.75
    elif t10 > 0.20:
        penalty *= 0.85
    elif t10 > 0.10:
        penalty *= 0.93
    if t5 > 0.15:
        penalty *= 0.90
    if position_20d > 0.85 and pullback_10d < 0.03:
        penalty *= 0.80
    if sent > 60 and t10 > 0.15:
        penalty *= 0.85

    return max(0.0, score * penalty)


def _next_dates(dates: list[str], n: int = 3) -> dict[str, list[str]]:
    """为每个日期返回之后 n 个交易日的列表。"""
    nxt: dict[str, list[str]] = {}
    for i, d in enumerate(dates):
        nxt[d] = dates[i + 1: i + 1 + n]
    return nxt


def validate(k: int = 3, analysis_dir: Path | str | None = None):
    if analysis_dir:
        analysis_dir = Path(analysis_dir)
    else:
        analysis_dir = ANALYSIS_DIR

    bars = load_daily_bars()
    basic_bars = load_daily_basic_bars()
    cache_dir = Path(__file__).resolve().parent / "cache"
    concept_daily = pd.read_parquet(cache_dir / "concept_daily.parquet") if (cache_dir / "concept_daily.parquet").exists() else pd.DataFrame()
    concept_members = pd.read_parquet(cache_dir / "concept_members.parquet") if (cache_dir / "concept_members.parquet").exists() else pd.DataFrame()
    all_dates = sorted(bars["trade_date"].unique().tolist())
    next_dates = _next_dates(all_dates, 3)

    files = load_analysis_files(analysis_dir)
    print(f"读取 {len(files)} 个分析文件 (from {analysis_dir})")

    # 每个交易日合并所有扫描记录，同代码取时间戳最大的一次
    best_by_code: dict[str, dict[str, tuple[str, dict]]] = {}
    for date, ts, recs in files:
        if date not in best_by_code:
            best_by_code[date] = {}
        for r in recs:
            code = r.get("code", "")
            if not code:
                continue
            if code not in best_by_code[date] or ts > best_by_code[date][code][0]:
                best_by_code[date][code] = (ts, r)

    total_pushed = 0
    total_hit = 0
    total_win = 0
    records = []

    for date, code_map in sorted(best_by_code.items()):
        if date < "20260601" or date > "20260630":
            continue
        recs = [r for _, r in code_map.values()]
        # 为每条记录计算 balanced_total
        scored = []
        for r in recs:
            code = r.get("code", "")
            bt = _compute_balanced(r, code, date, bars, basic_bars, concept_daily, concept_members)
            scored.append((bt, r))

        if not scored:
            continue

        scored.sort(key=lambda x: x[0], reverse=True)
        pushed = scored[:k]

        for bt, r in pushed:
            code = r.get("code", "")
            name = r.get("name", "")

            # 命中：未来 3 个交易日出现涨停
            fut_dates = next_dates.get(date, [])
            fut = bars[(bars["ts_code"] == code) & (bars["trade_date"].isin(fut_dates))]
            hit = int((fut["pct_chg"] >= LIMIT_PCT).any()) if not fut.empty else 0

            # 胜率：下一个交易日收盘 > 评分日收盘 * 1.001
            score_day = bars[(bars["ts_code"] == code) & (bars["trade_date"] == date)]
            next_day = bars[(bars["ts_code"] == code) & (bars["trade_date"].isin(fut_dates[:1]))]
            win = 0
            if not score_day.empty and not next_day.empty:
                buy_close = float(score_day.iloc[0]["close"])
                sell_close = float(next_day.iloc[0]["close"])
                if buy_close > 0 and sell_close > buy_close * 1.001:
                    win = 1

            total_pushed += 1
            total_hit += hit
            total_win += win
            records.append({
                "date": date, "code": code, "name": name,
                "balanced_total": bt, "hit": hit, "win": win,
            })

    if total_pushed == 0:
        print("无推送记录")
        return

    hr = total_hit / total_pushed
    wr = total_win / total_pushed
    print(f"\n{'指标':<10} {'值':>8}")
    print("-" * 20)
    print(f"{'推送总数':<10} {total_pushed:>8}")
    print(f"{'命中数':<10} {total_hit:>8}")
    print(f"{'命中率':<10} {hr:>7.1%}")
    print(f"{'胜局数':<10} {total_win:>8}")
    print(f"{'胜率':<10} {wr:>7.1%}")

    # 保存明细
    out_dir = Path(__file__).resolve().parent / "data"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "validate_balanced_result.json"
    with open(out_file, "w") as f:
        json.dump({
            "k": k,
            "total_pushed": total_pushed,
            "hit_rate": round(hr, 4),
            "win_rate": round(wr, 4),
            "records": records,
        }, f, ensure_ascii=False, indent=2)
    print(f"\n已保存: {out_file}")


if __name__ == "__main__":
    import argparse
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))
    parser = argparse.ArgumentParser(description="balanced_total 真实扫描信号验证")
    parser.add_argument("--k", type=int, default=3, help="每日推送数量")
    parser.add_argument("--analysis-dir", default=str(ANALYSIS_DIR), help="analysis 目录路径")
    args = parser.parse_args()
    validate(k=args.k, analysis_dir=args.analysis_dir)
