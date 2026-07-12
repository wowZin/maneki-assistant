#!/usr/bin/env python3
"""
涨停共性特征提取 — 从历史评分数据中找出涨停股的共性

流程:
  1. 加载 data/analysis/ 历史评分记录
  2. 从 Tushare limit_list_d 拉实际涨停数据
  3. 批量拉取原始特征（circ_mv, turnover_rate, volume_ratio, pct_chg, amount, net_mf_amount）
  4. 对比 HIT（涨停）vs MISS（未涨停）两组分布差异
  5. 输出 data/weights/zt_pattern.json + data/weights/zt_pattern.md

用法:
  python plays/limit_up/zt_pattern.py [--days 14]
"""

import json
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_DIR))
PLAY_DIR = Path(__file__).resolve().parent
DATA_DIR = PLAY_DIR / "data"
ANALYSIS_DIR = DATA_DIR / "analysis"
WEIGHTS_DIR = DATA_DIR / "weights"

from scripts.tu_share import call_tushare, CONFIG, clear_tushare_cache  # noqa: E402
from plays.limit_up.utils import safe_float  # noqa: E402

TUSHARE_TOKEN = CONFIG.get("TUSHARE_TOKEN", "")
DIMS = ["fundamental", "technical", "fundflow", "sentiment", "shortterm"]
DIM_CN = {
    "fundamental": "基本面", "technical": "技术面",
    "fundflow": "资金面", "sentiment": "情绪面", "shortterm": "短线博弈",
}


# ═══════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════

def _tushare_items_to_dicts(resp: dict) -> list[dict]:
    """将 Tushare API 响应转为 list[dict]"""
    data = resp.get("data", {})
    fields = data.get("fields", [])
    items = data.get("items", [])
    if not fields or not items:
        return []
    return [dict(zip(fields, item)) for item in items if item]


def _batch_call_tushare(api_name: str, params: dict, fields: str = "",
                        timeout: int = 30, delay: float = 0.15) -> list[dict]:
    """调用 Tushare API + 限速延迟，返回 list[dict]"""
    time.sleep(delay)
    try:
        resp = call_tushare(api_name, params, fields, timeout)
        return _tushare_items_to_dicts(resp)
    except Exception as e:
        print(f"  Tushare {api_name} 失败: {e}")
        return []


def _percentile(values: list[float], p: float) -> float:
    """计算百分位数"""
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    k = (len(sorted_vals) - 1) * p / 100.0
    f = int(k)
    c = k - f
    if f + 1 < len(sorted_vals):
        return sorted_vals[f] + c * (sorted_vals[f + 1] - sorted_vals[f])
    return sorted_vals[f]


# ═══════════════════════════════════════════
# 1. 加载历史评分数据
# ═══════════════════════════════════════════

def load_analysis_records(days: int = 14) -> list[dict]:
    """加载历史评分数据，按日期+代码去重"""
    all_files = sorted(ANALYSIS_DIR.glob("*.json"))
    if not all_files:
        print("无分析文件")
        return []

    # 提取交易日
    date_to_files = {}
    for f in all_files:
        trade_date = f.stem.split("_")[0]
        if trade_date not in date_to_files:
            date_to_files[trade_date] = []
        date_to_files[trade_date].append(f)

    all_dates = sorted(date_to_files.keys())
    recent_dates = all_dates[-days:] if days > 0 and len(all_dates) > days else all_dates
    recent_files = [f for d in recent_dates for f in date_to_files[d]]
    print(f"交易日范围: {recent_dates[0]} ~ {recent_dates[-1]} ({len(recent_dates)}天), "
          f"文件数: {len(recent_files)}/{len(all_files)}")

    # 读取 + 解析
    records = []
    for f in recent_files:
        trade_date = f.stem.split("_")[0]
        try:
            with open(f) as fh:
                data = json.load(fh)
            if not isinstance(data, list):
                continue
            for item in data:
                code_full = item.get("code", "")
                code = code_full.split(".")[0]
                if not code or code == "None":
                    continue
                # 排除创业板/科创板/北交所
                if code.startswith(("300", "301", "688", "8", "4")):
                    continue
                records.append({
                    "trade_date": trade_date,
                    "code": code,
                    "name": item.get("name", ""),
                    "scores": item.get("scores", {}),
                    "total": item.get("total", 0) or 0,
                    "pct_chg": item.get("pct_chg", 0) or 0,
                })
        except Exception as e:
            print(f"  读取失败 {f}: {e}")

    # 去重：同一日期代码保留最后一次（总分最高）
    seen = set()
    unique = []
    for r in reversed(records):
        key = (r["trade_date"], r["code"])
        if key not in seen:
            seen.add(key)
            unique.append(r)
    unique.reverse()

    print(f"  有效记录: {len(unique)} 条 (去重后)")
    return unique


# ═══════════════════════════════════════════
# 2. 拉取实际涨停数据
# ═══════════════════════════════════════════

def fetch_limit_ups(dates: list[str]) -> set[tuple[str, str]]:
    """拉取涨停数据，返回 {(date, code_short)}，过滤与 pipeline 一致"""
    # 拉 stock_basic 用于 ST/新股过滤
    name_map = {}
    try:
        resp = _batch_call_tushare("stock_basic", {"list_status": "L"},
                                   "ts_code,name", delay=0)
        for r in resp:
            name_map[r.get("ts_code", "")] = r.get("name", "")
        print(f"  stock_basic: {len(name_map)} 只")
    except Exception as e:
        print(f"  stock_basic 失败: {e}")

    limit_ups = set()
    for date in dates:
        try:
            rows = _batch_call_tushare("limit_list_d",
                                       {"trade_date": date, "limit_type": "U"},
                                       "ts_code,name")
            added = 0
            for r in rows:
                code_full = r.get("ts_code", "")
                code = code_full.split(".")[0]
                name = r.get("name", "") or name_map.get(code_full, "")
                # 过滤: 创业板/科创板/北交所
                if code.startswith(("300", "301", "688", "8", "4")):
                    continue
                # 过滤: ST/*ST/新股
                if "ST" in name or name.startswith("N"):
                    continue
                if (date, code) not in limit_ups:
                    limit_ups.add((date, code))
                    added += 1
            print(f"  {date}: +{added} 只涨停")
        except Exception as e:
            print(f"  {date}: 拉取失败 {e}")

    return limit_ups


# ═══════════════════════════════════════════
# 3. 批量拉取原始特征
# ═══════════════════════════════════════════

def batch_fetch_characteristics(records: list[dict]) -> dict[str, dict]:
    """批量拉取原始特征：按日期批量查询，然后匹配到每条记录

    Returns: {"code|date" -> {circ_mv, turnover_rate, volume_ratio,
                               pct_chg_raw, amount, net_mf_amount}}
    """
    dates = sorted(set(r["trade_date"] for r in records))
    print(f"\n拉取 {len(dates)} 个交易日原始特征...")

    # 按日期批量查询
    char_by_date = {}  # date -> {code_short -> characteristics}

    for i, date in enumerate(dates):
        print(f"  [{i+1}/{len(dates)}] {date}...", end="", flush=True)
        date_chars = defaultdict(dict)

        # daily_basic: circ_mv, turnover_rate, volume_ratio
        rows = _batch_call_tushare("daily_basic", {"trade_date": date},
                                   "ts_code,turnover_rate,volume_ratio,circ_mv,total_mv,pe,pb")
        for r in rows:
            code = r.get("ts_code", "").split(".")[0]
            date_chars[code].update({
                "turnover_rate": safe_float(r.get("turnover_rate")),
                "volume_ratio": safe_float(r.get("volume_ratio")),
                "circ_mv": safe_float(r.get("circ_mv")) * 10000,  # 万元→元
                "total_mv": safe_float(r.get("total_mv")) * 10000,
                "pe": safe_float(r.get("pe")),
                "pb": safe_float(r.get("pb")),
            })

        # daily: pct_chg, amount, close
        rows = _batch_call_tushare("daily", {"trade_date": date},
                                   "ts_code,close,pct_chg,amount")
        for r in rows:
            code = r.get("ts_code", "").split(".")[0]
            date_chars[code].update({
                "pct_chg_raw": safe_float(r.get("pct_chg")),
                "close": safe_float(r.get("close")),
                "amount": safe_float(r.get("amount")) * 10000,
            })

        # moneyflow: net_mf_amount
        rows = _batch_call_tushare("moneyflow", {"trade_date": date},
                                   "ts_code,net_mf_amount,net_mf_amount_lg")
        for r in rows:
            code = r.get("ts_code", "").split(".")[0]
            date_chars[code].update({
                "net_mf_amount": safe_float(r.get("net_mf_amount")) * 10000,
                "net_mf_amount_lg": safe_float(r.get("net_mf_amount_lg")) * 10000,
            })

        char_by_date[date] = dict(date_chars)
        total_stocks = len(date_chars)
        print(f" {total_stocks} 只")

    # 匹配到每条记录
    result = {}
    matched = 0
    for r in records:
        date = r["trade_date"]
        code = r["code"]
        chars = char_by_date.get(date, {}).get(code, {})
        key = f"{code}|{date}"
        if chars:
            result[key] = chars
            matched += 1
        else:
            result[key] = {}

    print(f"  特征匹配: {matched}/{len(records)} 条")
    return result


# ═══════════════════════════════════════════
# 4. 分组 + 分布计算
# ═══════════════════════════════════════════

def split_hit_miss(records: list[dict], limit_ups: set[tuple[str, str]],
                   characteristics: dict[str, dict]) -> tuple[list[dict], list[dict]]:
    """将记录分为 HIT 组和 MISS 组，并附上原始特征"""
    hit_group = []
    miss_group = []

    for r in records:
        key = f"{r['code']}|{r['trade_date']}"
        chars = characteristics.get(key, {})
        record = {**r, "chars": chars}
        if (r["trade_date"], r["code"]) in limit_ups:
            hit_group.append(record)
        else:
            miss_group.append(record)

    print(f"\n分组结果: HIT={len(hit_group)}只, MISS={len(miss_group)}只 "
          f"(命中率 {len(hit_group)/max(len(records),1)*100:.1f}%)")
    return hit_group, miss_group


def distribution_stats(values: list[float], label: str = "") -> dict:
    """计算分布统计"""
    if not values:
        return {"count": 0, "label": label}
    n = len(values)
    return {
        "count": n, "label": label,
        "mean": round(sum(values) / n, 2),
        "median": round(sorted(values)[n // 2], 2),
        "q1": round(_percentile(values, 25), 2),
        "q3": round(_percentile(values, 75), 2),
        "p10": round(_percentile(values, 10), 2),
        "p90": round(_percentile(values, 90), 2),
        "min": round(min(values), 2),
        "max": round(max(values), 2),
        "std": round((sum((v - sum(values)/n)**2 for v in values) / n) ** 0.5, 2),
    }


def effect_size(mean_hit: float, mean_miss: float, std_hit: float, std_miss: float) -> float:
    """Cohen's d 效应量"""
    pooled = ((std_hit**2 + std_miss**2) / 2) ** 0.5
    if pooled == 0:
        return 0.0
    return round((mean_hit - mean_miss) / pooled, 3)


def compare_characteristics(hit: list[dict], miss: list[dict]) -> dict:
    """对比 HIT vs MISS 在各特征上的分布"""
    comparisons = {}

    raw_features = {
        "circ_mv": "流通市值(元)",
        "turnover_rate": "换手率(%)",
        "volume_ratio": "量比",
        "pct_chg_raw": "当日涨幅(%)",
        "amount": "成交额(元)",
        "net_mf_amount": "主力净流入(元)",
        "net_mf_amount_lg": "大单净流入(元)",
        "pe": "市盈率",
        "pb": "市净率",
    }

    for feat_key, feat_label in raw_features.items():
        hit_vals = [safe_float(r.get("chars", {}).get(feat_key)) for r in hit
                    if safe_float(r.get("chars", {}).get(feat_key)) != 0]
        miss_vals = [safe_float(r.get("chars", {}).get(feat_key)) for r in miss
                     if safe_float(r.get("chars", {}).get(feat_key)) != 0]

        hit_stats = distribution_stats(hit_vals, "HIT")
        miss_stats = distribution_stats(miss_vals, "MISS")
        es = effect_size(hit_stats.get("mean", 0), miss_stats.get("mean", 0),
                         hit_stats.get("std", 1), miss_stats.get("std", 1))

        comparisons[feat_key] = {
            "label": feat_label,
            "hit": hit_stats,
            "miss": miss_stats,
            "effect_size": es,
            "separation": "强" if abs(es) > 0.5 else ("中" if abs(es) > 0.2 else "弱"),
        }

    # 维度分数
    for dim in DIMS:
        hit_vals = [safe_float(r.get("scores", {}).get(dim, 0)) for r in hit]
        miss_vals = [safe_float(r.get("scores", {}).get(dim, 0)) for r in miss]

        hit_stats = distribution_stats(hit_vals, "HIT")
        miss_stats = distribution_stats(miss_vals, "MISS")
        es = effect_size(hit_stats.get("mean", 0), miss_stats.get("mean", 0),
                         hit_stats.get("std", 1), miss_stats.get("std", 1))

        comparisons[f"score_{dim}"] = {
            "label": f"评分-{DIM_CN[dim]}",
            "hit": hit_stats,
            "miss": miss_stats,
            "effect_size": es,
            "separation": "强" if abs(es) > 0.5 else ("中" if abs(es) > 0.2 else "弱"),
        }

    # 总分
    hit_total = [r.get("total", 0) or 0 for r in hit]
    miss_total = [r.get("total", 0) or 0 for r in miss]
    hit_stats = distribution_stats(hit_total, "HIT")
    miss_stats = distribution_stats(miss_total, "MISS")
    es = effect_size(hit_stats.get("mean", 0), miss_stats.get("mean", 0),
                     hit_stats.get("std", 1), miss_stats.get("std", 1))
    comparisons["total"] = {
        "label": "综合总分",
        "hit": hit_stats,
        "miss": miss_stats,
        "effect_size": es,
        "separation": "强" if abs(es) > 0.5 else ("中" if abs(es) > 0.2 else "弱"),
    }

    return comparisons


def analyze_patterns(hit: list[dict], miss: list[dict], records: list[dict]) -> dict:
    """分析涨停共性模式"""
    total_records = len(records)
    total_hit = len(hit)

    if total_hit == 0:
        return {"error": "无涨停记录"}

    patterns = {}

    # ── 流通市值分布 ──
    circ_bins = [
        ("微型(<20亿)", 0, 2000000000),
        ("小型(20-50亿)", 2000000000, 5000000000),
        ("中型(50-100亿)", 5000000000, 10000000000),
        ("大型(100-200亿)", 10000000000, 20000000000),
        ("超大型(200-300亿)", 20000000000, 30000000000),
        ("巨盘(>300亿)", 30000000000, float("inf")),
    ]
    circ_mv_dist = {}
    for label, lo, hi in circ_bins:
        hit_cnt = sum(1 for r in hit if lo <= safe_float(r.get("chars", {}).get("circ_mv", 0)) < hi)
        miss_cnt = sum(1 for r in miss if lo <= safe_float(r.get("chars", {}).get("circ_mv", 0)) < hi)
        circ_mv_dist[label] = {
            "hit_count": hit_cnt, "hit_pct": round(hit_cnt / total_hit * 100, 1),
            "miss_count": miss_cnt,
            "lift": round((hit_cnt / total_hit) / max(miss_cnt / max(len(miss), 1), 0.001), 1),
        }
    patterns["circ_mv_distribution"] = circ_mv_dist

    # ── 换手率分布 ──
    turnover_bins = [
        ("冷清(<2%)", 0, 2), ("温和(2-5%)", 2, 5),
        ("活跃(5-15%)", 5, 15), ("高热(15-25%)", 15, 25),
        ("过热(>25%)", 25, 100),
    ]
    turnover_dist = {}
    for label, lo, hi in turnover_bins:
        hit_cnt = sum(1 for r in hit if lo <= safe_float(r.get("chars", {}).get("turnover_rate", 0)) < hi)
        turnover_dist[label] = {
            "hit_count": hit_cnt, "hit_pct": round(hit_cnt / total_hit * 100, 1),
        }
    patterns["turnover_distribution"] = turnover_dist

    # ── 涨幅分布（扫描时） ──
    pct_bins = [
        ("微涨(0-2%)", 0, 2), ("小涨(2-5%)", 2, 5),
        ("中涨(5-7%)", 5, 7), ("大涨(7-9.5%)", 7, 9.5),
    ]
    pct_dist = {}
    for label, lo, hi in pct_bins:
        hit_cnt = sum(1 for r in hit if lo <= safe_float(r.get("chars", {}).get("pct_chg_raw", 0)) < hi)
        pct_dist[label] = {
            "hit_count": hit_cnt, "hit_pct": round(hit_cnt / total_hit * 100, 1),
        }
    patterns["pct_chg_distribution"] = pct_dist

    # ── 维度共振 ──
    for threshold in [50, 60, 70, 75, 80]:
        hit_resonance = sum(1 for r in hit
                           if sum(1 for d in DIMS
                                  if safe_float(r.get("scores", {}).get(d, 0)) >= threshold) >= 3)
        patterns[f"resonance_{threshold}"] = {
            "hit_count": hit_resonance,
            "hit_pct": round(hit_resonance / total_hit * 100, 1),
        }

    # ── 单维度高分 ──
    for dim in DIMS:
        for threshold in [60, 70, 75, 80]:
            hit_cnt = sum(1 for r in hit
                         if safe_float(r.get("scores", {}).get(dim, 0)) >= threshold)
            key = f"{dim}_ge{threshold}"
            patterns[key] = {
                "hit_count": hit_cnt,
                "hit_pct": round(hit_cnt / total_hit * 100, 1),
            }

    # ── 量比分布 ──
    vol_bins = [
        ("缩量(<1)", 0, 1), ("正常(1-2)", 1, 2),
        ("放量(2-5)", 2, 5), ("巨量(>5)", 5, 999),
    ]
    vol_dist = {}
    for label, lo, hi in vol_bins:
        hit_cnt = sum(1 for r in hit if lo <= safe_float(r.get("chars", {}).get("volume_ratio", 0)) < hi)
        vol_dist[label] = {
            "hit_count": hit_cnt, "hit_pct": round(hit_cnt / total_hit * 100, 1),
        }
    patterns["volume_ratio_distribution"] = vol_dist

    return patterns


def compute_feature_importance(comparisons: dict) -> list[dict]:
    """按效应量排序特征重要性"""
    ranked = []
    for key, comp in comparisons.items():
        ranked.append({
            "feature": key,
            "label": comp["label"],
            "effect_size": comp["effect_size"],
            "separation": comp["separation"],
            "hit_mean": comp["hit"].get("mean", 0),
            "miss_mean": comp["miss"].get("mean", 0),
            "diff": round(comp["hit"].get("mean", 0) - comp["miss"].get("mean", 0), 2),
        })
    ranked.sort(key=lambda x: abs(x["effect_size"]), reverse=True)
    return ranked


# ═══════════════════════════════════════════
# 5. 报告输出
# ═══════════════════════════════════════════

def generate_report(records: list[dict], limit_ups: set, hit: list[dict], miss: list[dict],
                    comparisons: dict, patterns: dict, importance: list[dict]) -> dict:
    """生成结构化 JSON 报告"""
    dates = sorted(set(r["trade_date"] for r in records))
    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "data_summary": {
            "total_records": len(records),
            "hit_count": len(hit),
            "miss_count": len(miss),
            "hit_rate": round(len(hit) / max(len(records), 1) * 100, 1),
            "date_range": f"{dates[0]} ~ {dates[-1]}",
            "trading_days": len(dates),
        },
        "feature_importance": importance,
        "characteristic_comparison": {k: v for k, v in comparisons.items()
                                      if not k.startswith("score_") and k != "total"},
        "dimension_score_comparison": {k: v for k, v in comparisons.items()
                                       if k.startswith("score_") or k == "total"},
        "patterns": patterns,
        "key_findings": _generate_findings(comparisons, patterns, importance, len(hit)),
    }


def _generate_findings(comparisons: dict, patterns: dict, importance: list[dict],
                       hit_count: int) -> list[str]:
    """自动生成关键发现"""
    findings = []

    if importance:
        top = importance[:3]
        findings.append(f"区分力最强的3个特征: {', '.join(f'{i['label']}(效应量{i['effect_size']})' for i in top)}")

    # 流通市值
    circ_dist = patterns.get("circ_mv_distribution", {})
    small_hit = sum(d["hit_pct"] for k, d in circ_dist.items() if "20亿" in k or "50亿" in k)
    if small_hit > 50:
        findings.append(f"涨停股中 {small_hit:.0f}% 流通市值 < 50亿，小盘弹性是涨停的核心条件之一")

    # 换手率
    turnover_dist = patterns.get("turnover_distribution", {})
    active_hit = sum(d["hit_pct"] for k, d in turnover_dist.items()
                     if "5-15%" in k or "15-25%" in k or "2-5%" in k)
    if active_hit > 60:
        findings.append(f"涨停股中 {active_hit:.0f}% 换手率在 2-25% 区间，适度活跃的换手是涨停的必要条件")

    # 量比
    vol_dist = patterns.get("volume_ratio_distribution", {})
    high_vol = sum(d["hit_pct"] for k, d in vol_dist.items() if "放量" in k or "巨量" in k)
    if high_vol > 50:
        findings.append(f"涨停股中 {high_vol:.0f}% 量比 > 2，放量是涨停的重要信号")

    # 维度分数
    for dim in DIMS:
        key = f"score_{dim}"
        comp = comparisons.get(key, {})
        es = comp.get("effect_size", 0)
        if abs(es) > 0.3:
            direction = "高于" if es > 0 else "低于"
            findings.append(f"{DIM_CN[dim]}评分涨停组{direction}非涨停组 (效应量 {es:.2f})")

    # 共振
    r75 = patterns.get("resonance_75", {})
    if r75.get("hit_pct", 0) > 30:
        findings.append(f"{r75['hit_pct']:.0f}% 的涨停股有 >=3 个维度评分 >=75 (维度共振)")
    else:
        findings.append(f"仅 {r75.get('hit_pct', 0):.0f}% 的涨停股触发维度共振(>=3维>=75)，"
                        f"说明单一高分维度比多维度均衡更重要")

    # 涨幅分布
    pct_dist = patterns.get("pct_chg_distribution", {})
    mid_high = sum(d["hit_pct"] for k, d in pct_dist.items() if "5-7%" in k or "7-9.5" in k)
    if mid_high > 40:
        findings.append(f"涨停股在扫描时 {mid_high:.0f}% 涨幅已在 5-9.5%，扫描涨幅越高涨停概率越大")

    return findings


def generate_markdown(report: dict) -> str:
    """生成 Markdown 报告"""
    ds = report["data_summary"]
    lines = [
        f"# 涨停共性特征分析报告",
        f"",
        f"> 生成时间: {report['generated_at']}",
        f"",
        f"## 数据概览",
        f"",
        f"| 指标 | 数值 |",
        f"|------|------|",
        f"| 分析记录数 | {ds['total_records']} |",
        f"| 实际涨停(HIT) | {ds['hit_count']} |",
        f"| 未涨停(MISS) | {ds['miss_count']} |",
        f"| 分析命中率 | {ds['hit_rate']}% |",
        f"| 日期范围 | {ds['date_range']} ({ds['trading_days']}天) |",
        f"",
        f"## 关键发现",
        f"",
    ]
    for f in report.get("key_findings", []):
        lines.append(f"- {f}")
    lines.append("")

    # 特征重要性排名
    lines.append("## 特征区分力排名（效应量）")
    lines.append("")
    lines.append("| 排名 | 特征 | 效应量 | 区分力 | 涨停均值 | 非涨停均值 | 差值 |")
    lines.append("|------|------|--------|--------|----------|------------|------|")
    for i, imp in enumerate(report.get("feature_importance", [])[:15]):
        lines.append(f"| {i+1} | {imp['label']} | {imp['effect_size']:.3f} | "
                     f"{imp['separation']} | {imp['hit_mean']:.1f} | {imp['miss_mean']:.1f} | {imp['diff']:+.1f} |")
    lines.append("")

    # 流通市值分布
    lines.append("## 流通市值分布")
    lines.append("")
    circ = report.get("patterns", {}).get("circ_mv_distribution", {})
    if circ:
        lines.append("| 区间 | 涨停数 | 涨停占比 | Lift |")
        lines.append("|------|--------|----------|------|")
        for label, d in circ.items():
            lines.append(f"| {label} | {d['hit_count']} | {d['hit_pct']}% | {d['lift']}x |")
        lines.append("")

    # 换手率分布
    lines.append("## 换手率分布（涨停股）")
    lines.append("")
    turnover = report.get("patterns", {}).get("turnover_distribution", {})
    if turnover:
        lines.append("| 区间 | 涨停数 | 涨停占比 |")
        lines.append("|------|--------|----------|")
        for label, d in turnover.items():
            bar = "█" * max(1, int(d["hit_pct"] / 2))
            lines.append(f"| {label} | {d['hit_count']} | {d['hit_pct']}% {bar} |")
        lines.append("")

    # 量比分布
    lines.append("## 量比分布（涨停股）")
    lines.append("")
    vol = report.get("patterns", {}).get("volume_ratio_distribution", {})
    if vol:
        lines.append("| 区间 | 涨停数 | 涨停占比 |")
        lines.append("|------|--------|----------|")
        for label, d in vol.items():
            bar = "█" * max(1, int(d["hit_pct"] / 2))
            lines.append(f"| {label} | {d['hit_count']} | {d['hit_pct']}% {bar} |")
        lines.append("")

    # 维度评分对比
    lines.append("## 各维度评分 HIT vs MISS 对比")
    lines.append("")
    dim_comp = report.get("dimension_score_comparison", {})
    if dim_comp:
        lines.append("| 维度 | 涨停均值 | 涨停中位 | 非涨停均值 | 非涨停中位 | 效应量 |")
        lines.append("|------|----------|----------|------------|------------|--------|")
        for key, comp in dim_comp.items():
            label = comp.get("label", key)
            h = comp["hit"]
            m = comp["miss"]
            lines.append(f"| {label} | {h.get('mean', 0):.1f} | {h.get('median', 0):.1f} | "
                         f"{m.get('mean', 0):.1f} | {m.get('median', 0):.1f} | "
                         f"{comp.get('effect_size', 0):.3f} |")
        lines.append("")

    # 建议
    lines.append("## 对信号设计的启示")
    lines.append("")
    lines.append("基于以上分析，信号设计应侧重：")
    lines.append("")
    lines.append("1. **流通市值** — 如果涨停股集中在小市值区间，smallcap 信号阈值应据此校准")
    lines.append("2. **换手率** — 如果涨停股换手集中在某个区间，可在信号中设置最优区间而非简单阈值")
    lines.append("3. **量比** — 如果涨停股量比显著偏高，volume_breakout 信号应给量比更高权重")
    lines.append("4. **维度优先级** — 根据效应量排序，优先使用区分力最强的维度来构建信号")
    lines.append("5. **共振 vs 单项** — 如果共振触发率低，说明单项高分比多维度均衡更有预测力")
    lines.append("")
    lines.append("---")
    lines.append(f"*报告由 zt_pattern.py 自动生成*")

    return "\n".join(lines)


# ═══════════════════════════════════════════
# Main
# ═══════════════════════════════════════════

def main(days: int = 14):
    print("=" * 60)
    print(f"涨停共性特征提取 — {datetime.now()}")
    print("=" * 60)

    clear_tushare_cache()

    # 1. 加载历史评分
    print("\n[1/4] 加载历史评分数据...")
    records = load_analysis_records(days)
    if not records:
        print("无数据，退出")
        return
    dates = sorted(set(r["trade_date"] for r in records))

    # 2. 拉取实际涨停
    print(f"\n[2/4] 拉取 {len(dates)} 天涨停数据...")
    limit_ups = fetch_limit_ups(dates)
    total_hit_in_data = sum(1 for r in records if (r["trade_date"], r["code"]) in limit_ups)
    print(f"  记录中实际涨停: {total_hit_in_data} 只")

    # 3. 批量拉取原始特征
    print(f"\n[3/4] 拉取原始特征...")
    characteristics = batch_fetch_characteristics(records)

    # 4. 分组分析
    print(f"\n[4/4] 分组分析...")
    hit, miss = split_hit_miss(records, limit_ups, characteristics)

    if not hit:
        print("无涨停记录，无法分析。可能需要更多数据或检查日期范围。")
        return

    comparisons = compare_characteristics(hit, miss)
    patterns = analyze_patterns(hit, miss, records)
    importance = compute_feature_importance(comparisons)
    report = generate_report(records, limit_ups, hit, miss, comparisons, patterns, importance)

    # 5. 输出
    WEIGHTS_DIR.mkdir(parents=True, exist_ok=True)

    # JSON
    json_path = WEIGHTS_DIR / "zt_pattern.json"
    with open(json_path, "w") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\nJSON 报告已保存: {json_path}")

    # Markdown
    md_path = WEIGHTS_DIR / "zt_pattern.md"
    md_content = generate_markdown(report)
    with open(md_path, "w") as f:
        f.write(md_content)
    print(f"Markdown 报告已保存: {md_path}")

    # 简要输出
    print("\n" + "=" * 60)
    print("关键发现摘要:")
    print("=" * 60)
    for f in report.get("key_findings", []):
        print(f"  • {f}")

    print("\n特征重要性 Top 5:")
    for i, imp in enumerate(importance[:5]):
        print(f"  {i+1}. {imp['label']:20s} 效应量={imp['effect_size']:+.3f} "
              f"涨停均值={imp['hit_mean']:.1f} 非涨停均值={imp['miss_mean']:.1f}")

    print(f"\n完成!")


if __name__ == "__main__":
    days = 14
    if len(sys.argv) > 1:
        for arg in sys.argv[1:]:
            if arg.startswith("--days="):
                days = int(arg.split("=")[1])
    main(days)
