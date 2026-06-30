#!/usr/bin/env python3
"""权重优化引擎 V2 — 排序质量优化（加权Top3择优版）

流程:
  1. 读取 data/analysis/ 历史评分数据
  2. 从 tushare 拉实际涨停数据
  3. 枚举权重组合 → 加权Top3择优排序 → 多指标评估
  4. 输出最优权重 + 维度贡献追踪 + 阈值校准曲线

用法:
  python scripts/optimize_ranking.py [--days 30]

输出:
  data/weights/ranking_optimized.json
"""

import json
import random  # noqa: E402
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_DIR))
sys.path.insert(0, str(PROJECT_DIR / "scripts"))

import requests  # noqa: E402

from scripts.tu_share import CONFIG  # noqa: E402

TUSHARE_TOKEN = CONFIG.get("TUSHARE_TOKEN", "")
CURRENT_WEIGHTS = {
    "fundamental": float(CONFIG.get("AGENT_WEIGHT_FUNDAMENTAL", "1.5")),
    "technical": float(CONFIG.get("AGENT_WEIGHT_TECHNICAL", "1.0")),
    "fundflow": float(CONFIG.get("AGENT_WEIGHT_FUND_FLOW", "1.0")),
    "sentiment": float(CONFIG.get("AGENT_WEIGHT_SENTIMENT", "1.2")),
    "shortterm": float(CONFIG.get("AGENT_WEIGHT_SHORTTERM", "1.5")),
}

PLAY_DIR = Path(__file__).resolve().parent
DATA_DIR = PLAY_DIR / "data"
ANALYSIS_DIR = DATA_DIR / "analysis"
WEIGHTS_DIR = DATA_DIR / "weights"
WEIGHTS_DIR.mkdir(parents=True, exist_ok=True)

DIMS = ["fundamental", "technical", "fundflow", "sentiment", "shortterm"]
DIM_CN = {
    "fundamental": "基本面",
    "technical": "技术面",
    "fundflow": "资金面",
    "sentiment": "情绪面",
    "shortterm": "短线博弈",
}


# ═══════════════════════════════════════════
# 评分函数
# ═══════════════════════════════════════════

def weighted_top3_score(scores: dict, weights: dict) -> float:
    """加权Top3择优：按加权贡献取前3维的加权均值"""
    contribs = [(scores.get(d, 0) or 0, weights.get(d, 1.0)) for d in DIMS]
    contribs.sort(key=lambda x: x[0] * x[1], reverse=True)
    top3 = contribs[:3]
    total_s = sum(s * w for s, w in top3)
    total_w = sum(w for _, w in top3)
    return total_s / total_w if total_w > 0 else 0


def top3_dims(scores: dict, weights: dict) -> list[str]:
    """返回哪3个维度进了Top3"""
    contribs = [(scores.get(d, 0) or 0, d, weights.get(d, 1.0)) for d in DIMS]
    contribs.sort(key=lambda x: x[0] * x[2], reverse=True)
    return [d for _, d, _ in contribs[:3]]


# ═══════════════════════════════════════════
# 1. 读取历史数据
# ═══════════════════════════════════════════

def load_analysis(days: int = 14) -> list[dict]:
    """读取历史评分数据，按日期+代码去重，滑动窗口只读最近 N 个交易日"""
    # 1. 先扫描文件名，确定最近 N 个交易日，避免加载全量文件
    all_files = sorted(ANALYSIS_DIR.glob("*.json"))
    if not all_files:
        return []

    # 提取所有文件中的唯一交易日，取最近 days 个
    date_to_files = {}
    for f in all_files:
        trade_date = f.stem.split("_")[0]
        if trade_date not in date_to_files:
            date_to_files[trade_date] = []
        date_to_files[trade_date].append(f)

    all_dates = sorted(date_to_files.keys())
    recent_dates = all_dates[-days:] if days > 0 and len(all_dates) > days else all_dates
    recent_files = [f for d in recent_dates for f in date_to_files[d]]
    print(f"  交易日范围: {recent_dates[0]} ~ {recent_dates[-1]} ({len(recent_dates)}天), "
          f"文件数: {len(recent_files)}/{len(all_files)}")

    # 2. 只读取最近 N 个交易日的文件
    records = []
    for f in recent_files:
        trade_date = f.stem.split("_")[0]
        with open(f) as fh:
            d = json.load(fh)
        if not isinstance(d, list):
            continue
        for item in d:
            code = item.get("code", "").split(".")[0]
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
                "total": item.get("total", 0),
            })

    # 3. 去重（同一日期代码只保留最后一次）
    seen = set()
    unique = []
    for r in reversed(records):
        key = (r["trade_date"], r["code"])
        if key not in seen:
            seen.add(key)
            unique.append(r)
    unique.reverse()

    return unique



# ═══════════════════════════════════════════
# 2. 拉取实际涨停数据
# ═══════════════════════════════════════════

def fetch_limit_ups(dates: list[str]) -> set[tuple[str, str]]:
    """从 tushare 拉取涨停数据，返回 {(date, code)}
    
    过滤规则与 pipeline 一致：
    - 排除创业板(300/301)/科创板(688)/北交所(8/4)
    - 排除 ST/*ST/新股(N开头)
    """
    # 先拉 stock_basic 获取名称（用于 ST/新股过滤）
    name_map = {}
    try:
        resp = requests.post("http://api.tushare.pro", json={
            "api_name": "stock_basic",
            "token": TUSHARE_TOKEN,
            "params": {"list_status": "L"},
            "fields": "ts_code,name"
        }, timeout=15)
        data = resp.json()
        if data.get("code") == 0 and data.get("data"):
            for item in data["data"]["items"]:
                name_map[item[0]] = item[1]
    except Exception:
        pass

    limit_ups = set()
    for date in dates:
        try:
            payload = {
                "api_name": "limit_list_d",
                "token": TUSHARE_TOKEN,
                "params": {"trade_date": date, "limit_type": "U"},
                "fields": "ts_code,name",
            }
            resp = requests.post("http://api.tushare.pro", json=payload, timeout=30)
            data = resp.json()
            if data.get("code") == 0 and data.get("data"):
                fields = data["data"]["fields"]
                idx = {f: i for i, f in enumerate(fields)}
                for item in data["data"]["items"]:
                    code_full = item[idx.get("ts_code", 0)]
                    code = code_full.split(".")[0]
                    name = item[idx.get("name", 0)] if "name" in idx else name_map.get(code_full, "")
                    # 过滤1: 创业板/科创板/北交所
                    if code.startswith(("300", "301", "688", "8", "4")):
                        continue
                    # 过滤2: ST/*ST/新股
                    if "ST" in name or name.startswith("N"):
                        continue
                    limit_ups.add((date, code))
                print(f"  {date}: +{len(limit_ups)} 只涨停（累计）")
        except Exception as e:
            print(f"  {date}: 拉取失败 {e}")
    return limit_ups


# ═══════════════════════════════════════════
# 3. 评估指标
# ═══════════════════════════════════════════

def evaluate_weights(
    records: list[dict],
    limit_ups: set[tuple[str, str]],
    weights: dict[str, float],
) -> dict:
    """评估一组权重的排序质量"""
    # 为每条记录计算加权Top3分
    scored = []
    for r in records:
        s = r["scores"]
        total = weighted_top3_score(s, weights)
        is_limit = (r["trade_date"], r["code"]) in limit_ups
        scored.append((total, is_limit, r["code"], r["trade_date"]))

    # 按总分降序排列
    scored.sort(key=lambda x: x[0], reverse=True)
    n = len(scored)
    limit_indices = [i for i, (_, is_limit, _, _) in enumerate(scored) if is_limit]
    limit_count = len(limit_indices)

    # 涨停股平均/中位排名
    avg_rank = sum(i + 1 for i in limit_indices) / limit_count if limit_count > 0 else n
    sorted_ranks = sorted(i + 1 for i in limit_indices)
    median_rank = sorted_ranks[len(sorted_ranks) // 2] if sorted_ranks else n

    # Top-K 覆盖率
    top_k = {}
    for k in [10, 20, 30, 50, 100]:
        top_k_count = sum(1 for i in range(min(k, n)) if scored[i][1])
        top_k[f"top{k}"] = top_k_count
        top_k[f"top{k}_rate"] = top_k_count / limit_count if limit_count > 0 else 0

    # 分差
    limit_scores = [scored[i][0] for i in limit_indices]
    non_limit_scores = [scored[i][0] for i in range(n) if not scored[i][1]]
    avg_limit = sum(limit_scores) / len(limit_scores) if limit_scores else 0
    avg_non = sum(non_limit_scores) / len(non_limit_scores) if non_limit_scores else 0
    sep = avg_limit - avg_non

    # AUC（简化版：随机采样比较，1000次）
    auc = 0.5  # default
    if limit_count > 0 and limit_count < n:
        cmp_count = 0
        cmp_win = 0
        for _ in range(1000):
            li = random.choice(limit_indices)
            ni = random.choice([i for i in range(n) if not scored[i][1]])
            cmp_count += 1
            if scored[li][0] > scored[ni][0]:
                cmp_win += 1
            elif scored[li][0] == scored[ni][0]:
                cmp_win += 0.5
        auc = cmp_win / cmp_count if cmp_count > 0 else 0.5

    # 综合分（0~1）
    rank_score = 1 - (avg_rank / n) if n > 0 else 0
    top20_coverage = top_k.get("top20_rate", 0)
    sep_norm = min(1, max(0, (sep - 5) / 50))  # 分差5以下0分，55以上满分
    auc_score = max(0, (auc - 0.5) * 2)  # 0.5→0, 1.0→1
    composite = 0.3 * rank_score + 0.4 * top20_coverage + 0.15 * sep_norm + 0.15 * auc_score

    return {
        "weights": weights.copy(),
        "composite": round(composite, 4),
        "avg_rank": round(avg_rank, 1),
        "median_rank": median_rank,
        "top10": top_k["top10"],
        "top20": top_k["top20"],
        "top30": top_k["top30"],
        "top50": top_k["top50"],
        "top10_rate": round(top_k["top10_rate"], 3),
        "top20_rate": round(top_k["top20_rate"], 3),
        "top30_rate": round(top_k["top30_rate"], 3),
        "top50_rate": round(top_k["top50_rate"], 3),
        "sep": round(sep, 2),
        "auc": round(auc, 4),
        "pushed_count": sum(1 for t, is_limit, _, _ in scored if t >= 35),
        "push_limit_count": sum(1 for t, is_limit, _, _ in scored if t >= 35 and is_limit),
        "total_limit": limit_count,
        "total_stocks": n,
    }


# ═══════════════════════════════════════════
# 4. 搜索算法
# ═══════════════════════════════════════════

def generate_combos(n: int = 5000, baseline_weights: dict = None) -> list[dict]:
    """生成随机权重组合 + 基准组合"""
    combos = []

    # 基准组合
    baselines = [
        baseline_weights or CURRENT_WEIGHTS,
        {d: 1.0 for d in DIMS},
        {"fundamental": 1.2, "technical": 1.0, "fundflow": 1.0, "sentiment": 1.0, "shortterm": 1.0},
        {"fundamental": 0.8, "technical": 1.2, "fundflow": 1.2, "sentiment": 1.2, "shortterm": 1.4},
        {"fundamental": 0.6, "technical": 1.0, "fundflow": 1.5, "sentiment": 1.3, "shortterm": 1.6},
    ]
    for b in baselines:
        if b and b not in combos:
            combos.append(b)

    # 随机组合
    for _ in range(n):
        w = {d: round(random.uniform(0.2, 2.5), 1) for d in DIMS}
        combos.append(w)

    return combos


def refine(results: list[dict], record_scores: list, limit_ups: set, top_n: int = 10, neighbors: int = 50) -> list[dict]:
    """从Top结果出发，邻域微调再搜索"""
    refined = []
    for r in results[:top_n]:
        base = r["weights"]
        for _ in range(neighbors):
            w = {d: max(0.1, round(base[d] + random.uniform(-0.3, 0.3), 1)) for d in DIMS}
            if w not in [res["weights"] for res in refined]:
                ev = evaluate_fast(record_scores, limit_ups, w)
                refined.append(ev)
    return refined


def evaluate_fast(scores_list, limit_ups_set, w):
    """快速评估一组权重（预计算后的高效版本）"""
    scored = []
    for trade_date, code, s in scores_list:
        total = weighted_top3_score(s, w)
        is_limit = (trade_date, code) in limit_ups_set
        scored.append((total, is_limit))

    scored.sort(key=lambda x: x[0], reverse=True)
    n = len(scored)
    limit_indices = [i for i, (_, is_limit) in enumerate(scored) if is_limit]
    limit_count = len(limit_indices)

    if limit_count == 0:
        return {"weights": w.copy(), "composite": 0, "avg_rank": n, "median_rank": n,
                "top10": 0, "top20": 0, "top30": 0, "top50": 0,
                "top10_rate": 0, "top20_rate": 0, "top30_rate": 0, "top50_rate": 0,
                "sep": 0, "auc": 0.5, "pushed_count": 0, "push_limit_count": 0,
                "total_limit": 0, "total_stocks": n}

    avg_rank = sum(i + 1 for i in limit_indices) / limit_count
    sorted_ranks = sorted(i + 1 for i in limit_indices)
    median_rank = sorted_ranks[len(sorted_ranks) // 2]

    top_k = {}
    for k in [10, 20, 30, 50]:
        top_k_count = sum(1 for i in range(min(k, n)) if scored[i][1])
        top_k[f"top{k}"] = top_k_count
        top_k[f"top{k}_rate"] = top_k_count / limit_count

    limit_scores_vals = [scored[i][0] for i in limit_indices]
    non_limit_scores_vals = [scored[i][0] for i in range(n) if not scored[i][1]]
    avg_limit = sum(limit_scores_vals) / len(limit_scores_vals)
    avg_non = sum(non_limit_scores_vals) / len(non_limit_scores_vals) if non_limit_scores_vals else 0
    sep = avg_limit - avg_non

    auc = 0.5
    if limit_count < n:
        cmp_win = 0
        non_limit_indices = [i for i in range(n) if not scored[i][1]]
        for _ in range(500):
            li = random.choice(limit_indices)
            ni = random.choice(non_limit_indices)
            if scored[li][0] > scored[ni][0]:
                cmp_win += 1
            elif scored[li][0] == scored[ni][0]:
                cmp_win += 0.5
        auc = cmp_win / 500

    rank_score = 1 - (avg_rank / n)
    top20_rate = top_k.get("top20_rate", 0)
    sep_norm = min(1, max(0, (sep - 5) / 50))
    auc_score = max(0, (auc - 0.5) * 2)
    composite = 0.3 * rank_score + 0.4 * top20_rate + 0.15 * sep_norm + 0.15 * auc_score

    push_count = sum(1 for t, _ in scored if t >= 35)
    push_limit = sum(1 for t, is_limit in scored if t >= 35 and is_limit)
    push_hit_rate = round(push_limit / push_count * 100, 1) if push_count > 0 else 0

    return {
        "weights": w.copy(),
        "composite": round(composite, 4),
        "avg_rank": round(avg_rank, 1),
        "median_rank": median_rank,
        "top10": top_k["top10"], "top20": top_k["top20"],
        "top30": top_k["top30"], "top50": top_k["top50"],
        "top10_rate": round(top_k["top10_rate"], 3),
        "top20_rate": round(top_k["top20_rate"], 3),
        "top30_rate": round(top_k["top30_rate"], 3),
        "top50_rate": round(top_k["top50_rate"], 3),
        "sep": round(sep, 2), "auc": round(auc, 4),
        "pushed_count": push_count,
        "push_limit_count": push_limit,
        "push_hit_rate": push_hit_rate,
        "total_limit": limit_count,
        "total_stocks": n,
    }


# ═══════════════════════════════════════════
# 5. 维度贡献追踪
# ═══════════════════════════════════════════

def compute_dim_contribution(records: list[dict], limit_ups: set, weights: dict) -> dict:
    """统计涨停股中各维度进入Top3的频次"""
    dim_counts = defaultdict(int)
    total_limit = 0

    # 每条记录去重
    seen = set()
    for r in records:
        key = (r["trade_date"], r["code"])
        if key in limit_ups and key not in seen:
            seen.add(key)
            total_limit += 1
            dims_in_top3 = top3_dims(r["scores"], weights)
            for d in dims_in_top3:
                dim_counts[d] += 1

    return {
        dim: {
            "count": dim_counts[dim],
            "rate": round(dim_counts[dim] / total_limit * 100, 1) if total_limit > 0 else 0,
        }
        for dim in DIMS
    }


# ═══════════════════════════════════════════
# 6. 阈值校准曲线
# ═══════════════════════════════════════════

def compute_threshold_curve(records: list[dict], limit_ups: set, weights: dict) -> list[dict]:
    """计算不同阈值下命中率"""
    scored = []
    for r in records:
        s = r["scores"]
        total = weighted_top3_score(s, weights)
        is_limit = (r["trade_date"], r["code"]) in limit_ups
        scored.append((total, is_limit))

    total_limit = sum(1 for _, is_limit in scored if is_limit)

    curve = []
    for t in range(10, 56, 5):
        above = [(sc, limit) for sc, limit in scored if sc >= t]
        limit_above = sum(1 for _, limit in above if limit)
        curve.append({
            "threshold": t,
            "pushed": len(above),
            "limit_above": limit_above,
            "coverage_rate": round(limit_above / total_limit * 100, 1) if total_limit > 0 else 0,
            "hit_rate": round(limit_above / len(above) * 100, 1) if above else 0,
        })

    return curve


# ═══════════════════════════════════════════
# 7. 报告输出
# ═══════════════════════════════════════════

def print_report(baseline: dict, top_results: list[dict], records: list[dict],
                 limit_ups: set, weights: dict, dim_contrib: dict, curve: list[dict]):
    """打印优化报告"""
    total_limit = sum(1 for r in records if (r["trade_date"], r["code"]) in limit_ups)

    print("\n" + "=" * 70)
    print("权重优化报告 V2 — 加权Top3版")
    print("=" * 70)
    print(f"数据: {len(records)} 条, 实际涨停 {total_limit} 只")
    dates = sorted(set(r["trade_date"] for r in records))
    print(f"覆盖交易日: {dates[0]} ~ {dates[-1]} ({len(dates)}天)")

    # 基准
    print("\n基准(当前权重):")
    w_str = "  ".join(f"{DIM_CN[d]}={weights[d]:.1f}" for d in DIMS)
    print(f"  {w_str}")
    print(f"  涨停均排: {baseline['avg_rank']:.0f}\t中位排: {baseline['median_rank']}")
    print(f"  Top10/20/30覆盖率: {baseline['top10_rate']*100:.0f}%/{baseline['top20_rate']*100:.0f}%/{baseline['top30_rate']*100:.0f}%")
    print(f"  AUC: {baseline['auc']:.2f}\t分差: {baseline['sep']:.1f}")
    pool_rate = round(baseline['push_limit_count'] / baseline['total_limit'] * 100, 1) if baseline['total_limit'] > 0 else 0
    print(f"  📦 信号池(≥35): {baseline['pushed_count']}只(含涨停{baseline['push_limit_count']}只, 覆盖率{pool_rate}%, 命中率{baseline['push_hit_rate']}%)")

    # Top 5
    print("\n🏆 Top 5 最优权重:")
    for i, r in enumerate(top_results[:5]):
        if r == baseline and i > 0:
            continue
        w = r["weights"]
        w_str = "  ".join(f"{DIM_CN[d]}={w[d]:.1f}" for d in DIMS)
        delta = r["composite"] - baseline["composite"]
        sign = "+" if delta >= 0 else ""
        pool_rate_r = round(r['push_limit_count'] / r['total_limit'] * 100, 1) if r['total_limit'] > 0 else 0
        print(f"\n  第{i+1}名 综合分{r['composite']:.2f} ({sign}{delta:.2f}):")
        print(f"    {w_str}")
        print(f"    涨停均排 {r['avg_rank']:.0f}  Top20覆盖率 {r['top20_rate']*100:.0f}%  Top50覆盖率 {r['top50_rate']*100:.0f}%")
        print(f"    AUC {r['auc']:.2f}  分差 {r['sep']:.1f}")
        print(f"    📦 信号池(≥35): {r['pushed_count']}只(含涨停{r['push_limit_count']}只, 覆盖率{pool_rate_r}%, 命中率{r['push_hit_rate']}%)")

    # 维度贡献
    print("\n📋 维度贡献率(当前权重):")
    for d in DIMS:
        c = dim_contrib[d]
        bar = "█" * max(1, int(c["rate"] / 5))
        status = ""
        if c["rate"] < 10:
            status = "← 待数据修复"
        elif c["rate"] < 50:
            status = "← 辅助维度"
        print(f"  {DIM_CN[d]}: {c['rate']:.0f}% ({c['count']}次) {bar} {status}")

    # 阈值曲线
    print("  📊 阈值校准曲线:")
    print(f"  {'阈值':>4} | {'信号池':>4} | {'涨停':>4} | {'覆盖率':>6} | {'命中率':>6}")
    print(f"  {'-'*4}-+-{'-'*5}-+-{'-'*4}-+-{'-'*6}-+-{'-'*6}")
    for c in curve:
        bar = "█" * max(1, c["limit_above"])
        print(f"  ≥{c['threshold']:>2} | {c['pushed']:>4} | {c['limit_above']:>4} | {c['coverage_rate']:>5.0f}% | {c['hit_rate']:>5.0f}% {bar}")
    print("\n  推荐阈值: 35~40（覆盖率30~50%，推送池10~25只）")

    print("\n" + "=" * 70)


def save_results(baseline: dict, top_results: list[dict], records: list[dict],
                 limit_ups: set, dim_contrib: dict, curve: list[dict]):
    """保存优化结果"""
    output = {
        "optimized_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "data_range": f"{records[0]['trade_date']} ~ {records[-1]['trade_date']}",
        "total_records": len(records),
        "total_limit_ups": sum(1 for r in records if (r["trade_date"], r["code"]) in limit_ups),
        "baseline": {
            "weights": baseline["weights"],
            "composite": baseline["composite"],
            "avg_rank": baseline["avg_rank"],
            "top20_rate": baseline["top20_rate"],
            "auc": baseline["auc"],
            "sep": baseline["sep"],
            "pushed_count": baseline["pushed_count"],
            "push_limit_count": baseline["push_limit_count"],
            "push_hit_rate": baseline["push_hit_rate"],
            "total_limit": baseline["total_limit"],
        },
        "recommended": top_results[0] if top_results else {},
        "top_10": [{
            "rank": i + 1,
            "weights": r["weights"],
            "composite": r["composite"],
            "avg_rank": r["avg_rank"],
            "top20_rate": r["top20_rate"],
            "auc": r["auc"],
            "sep": r["sep"],
            "pushed_count": r["pushed_count"],
            "push_limit_count": r["push_limit_count"],
            "push_hit_rate": r["push_hit_rate"],
            "total_limit": r["total_limit"],
        } for i, r in enumerate(top_results[:10])],
        "dim_contribution": dim_contrib,
        "threshold_curve": curve,
    }

    out_file = WEIGHTS_DIR / "ranking_optimized.json"
    with open(out_file, "w") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n结果已保存: {out_file}")


# ═══════════════════════════════════════════
# Main
# ═══════════════════════════════════════════

def main(days: int = 14):
    print("=" * 70)
    print(f"权重优化器 V2 启动 — {datetime.now()}")
    print("搜索模式: 加权Top3择优")
    print("=" * 70)

    # 1. 加载历史评分
    print("\n[1/4] 读取历史评分数据...")
    records = load_analysis(days)
    if not records:
        print("  ❌ 没有历史评分数据，退出")
        return
    print(f"  共 {len(records)} 条记录，{len(set(r['trade_date'] for r in records))} 个交易日")

    # 2. 拉涨停
    print("\n[2/4] 拉取实际涨停数据...")
    dates = sorted(set(r["trade_date"] for r in records))
    limit_ups = fetch_limit_ups(dates)
    total_limit = sum(1 for r in records if (r["trade_date"], r["code"]) in limit_ups)
    print(f"  数据中实际涨停 {total_limit} 只")

    if total_limit < 10:
        print("  ⚠️ 涨停数据太少，结果可能不准确")

    # 3. 评估当前权重
    print("\n[3/4] 搜索最优权重组合...")
    print(f"  当前权重: {' '.join(f'{k}={v}' for k,v in CURRENT_WEIGHTS.items())}")
    # 预计算记录数据
    record_scores = [(r["trade_date"], r["code"], r["scores"]) for r in records]
    baseline = evaluate_fast(record_scores, limit_ups, CURRENT_WEIGHTS)
    print(f"  基准综合分: {baseline['composite']:.4f}")

    # 4. 枚举搜索
    print("\n  生成权重组合...")
    combos = generate_combos(n=2000)
    print(f"  共 {len(combos)} 种组合")
    print("  开始搜索...")

    results = []
    for i, w in enumerate(combos):
        ev = evaluate_fast(record_scores, limit_ups, w)
        results.append(ev)
        if (i + 1) % 500 == 0:
            print(f"    进度: {i+1}/{len(combos)}")

    # 去重 + 排序
    seen_weights = set()
    unique_results = []
    for r in results:
        w_key = tuple(sorted(r["weights"].items()))
        if w_key not in seen_weights:
            seen_weights.add(w_key)
            unique_results.append(r)
    unique_results.sort(key=lambda x: (-x["composite"], -x["top20_rate"], -x["auc"]))

    # 局部精化
    print("  局部精化...")
    refined = refine(unique_results, record_scores, limit_ups, top_n=10, neighbors=30)
    unique_results.extend(refined)
    unique_results.sort(key=lambda x: (-x["composite"], -x["top20_rate"], -x["auc"]))

    top_results = unique_results[:10]

    # 5. 维度贡献追踪
    print("\n[4/4] 计算辅助数据...")
    dim_contrib = compute_dim_contribution(records, limit_ups, CURRENT_WEIGHTS)
    curve = compute_threshold_curve(records, limit_ups, CURRENT_WEIGHTS)

    # 6. 输出
    print_report(baseline, top_results, records, limit_ups, CURRENT_WEIGHTS, dim_contrib, curve)
    save_results(baseline, top_results, records, limit_ups, dim_contrib, curve)

    print("\n✅ 优化完成!")


if __name__ == "__main__":
    days = 14
    if len(sys.argv) > 1 and sys.argv[1].startswith("--days="):
        days = int(sys.argv[1].split("=")[1])
    main(days)
