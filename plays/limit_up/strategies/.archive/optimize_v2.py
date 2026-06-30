
#!/usr/bin/env python3
"""
信号阈值优化引擎 V2 — 网格搜索最优信号触发阈值和组合规则

与 optimize.py 的核心差异:
  - 不再枚举维度权重 (float 0.2~2.5)
  - 改为网格搜索信号置信度阈值 + 组合规则参数
  - 评估指标从「涨停股平均排名」改为「Recall / Precision / F1」
  - 输出 Pareto 最优配置而非单一最优权重

原理:
  1. 加载 data/analysis/v2_*.json 历史 V2 分析记录
  2. 从 Tushare 拉取实际涨停数据
  3. 网格搜索每个信号的置信度触发阈值
  4. 重新应用组合规则，计算命中率
  5. 输出多目标 Pareto 前沿

用法:
  python plays/limit_up/optimize_v2.py [--days 14]
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
from plays.limit_up.signals import (SIGNAL_PRIORITY, SIGNAL_LABELS,  # noqa: E402
                                    signal_combination_judge)

TUSHARE_TOKEN = CONFIG.get("TUSHARE_TOKEN", "")


# =========================================
# 工具函数
# =========================================

def _tushare_dicts(api_name: str, params: dict, fields: str = "",
                   timeout: int = 30, delay: float = 0.15) -> list:
    time.sleep(delay)
    try:
        resp = call_tushare(api_name, params, fields, timeout)
        data = resp.get("data", {})
        flds = data.get("fields", [])
        items = data.get("items", [])
        if not flds or not items:
            return []
        return [dict(zip(flds, item)) for item in items if item]
    except Exception as e:
        print("  Tushare %s: %s" % (api_name, e))
        return []


# =========================================
# 1. 加载 V2 分析数据
# =========================================

def load_v2_analysis(days: int = 14) -> list:
    """加载 V2 分析记录，按日期+代码去重"""
    all_files = sorted(ANALYSIS_DIR.glob("v2_*.json"))
    if not all_files:
        print("无 V2 分析文件")
        return []

    date_to_files = {}
    for f in all_files:
        trade_date = f.stem.split("_")[1]  # v2_YYYYMMDD_HHMM.json
        if trade_date not in date_to_files:
            date_to_files[trade_date] = []
        date_to_files[trade_date].append(f)

    all_dates = sorted(date_to_files.keys())
    recent_dates = all_dates[-days:] if days > 0 and len(all_dates) > days else all_dates
    recent_files = [f for d in recent_dates for f in date_to_files[d]]
    print("  V2 交易日: %s ~ %s (%d天), 文件: %d/%d" % (
        recent_dates[0], recent_dates[-1], len(recent_dates),
        len(recent_files), len(all_files)))

    records = []
    for f in recent_files:
        trade_date = f.stem.split("_")[1]
        try:
            with open(f) as fh:
                data = json.load(fh)
            if not isinstance(data, list):
                continue
            for item in data:
                code = item.get("code", "")
                if not code:
                    continue
                code_short = code.split(".")[0]
                if code_short.startswith(("300", "301", "688", "8", "4")):
                    continue
                records.append({
                    "trade_date": trade_date,
                    "code": code,
                    "name": item.get("name", ""),
                    "signals": item.get("signals", {}),
                    "triggered_signals": item.get("triggered_signals", []),
                    "push_decision": item.get("push_decision", {}),
                })
        except Exception as e:
            print("  读取失败 %s: %s" % (f, e))

    # 去重
    seen = set()
    unique = []
    for r in reversed(records):
        key = (r["trade_date"], r["code"])
        if key not in seen:
            seen.add(key)
            unique.append(r)
    unique.reverse()

    print("  有效记录: %d 条" % len(unique))
    return unique


# =========================================
# 2. 拉取实际涨停
# =========================================

def fetch_limit_ups(dates: list) -> set:
    """拉取涨停数据，过滤与 pipeline 一致。

    优先用 daily 接口（同日可用），降级用 limit_list_d（T+1）。
    """
    name_map = {}
    try:
        for r in _tushare_dicts("stock_basic", {"list_status": "L"},
                                "ts_code,name", delay=0):
            name_map[r.get("ts_code", "")] = r.get("name", "")
    except Exception:
        pass

    limit_ups = set()
    for date in dates:
        added = 0
        # 优先 daily（同日数据可用）
        try:
            for r in _tushare_dicts("daily", {"trade_date": date},
                                    "ts_code,pct_chg"):
                code_full = r.get("ts_code", "")
                code = code_full.split(".")[0]
                pct = safe_float(r.get("pct_chg"))
                if not code: continue
                if code.startswith(("300", "301", "688", "8", "4")): continue
                name = name_map.get(code_full, "")
                if "ST" in name or name.startswith("N"): continue
                if pct >= 9.9:
                    limit_ups.add((date, code))
                    added += 1
        except Exception:
            pass

        # 降级 limit_list_d
        if added == 0:
            try:
                for r in _tushare_dicts("limit_list_d",
                                        {"trade_date": date, "limit_type": "U"},
                                        "ts_code,name"):
                    code_full = r.get("ts_code", "")
                    code = code_full.split(".")[0]
                    name = r.get("name", "") or name_map.get(code_full, "")
                    if code.startswith(("300", "301", "688", "8", "4")): continue
                    if "ST" in name or name.startswith("N"): continue
                    if (date, code) not in limit_ups:
                        limit_ups.add((date, code))
                        added += 1
            except Exception:
                pass

        print("  %s: +%d 只涨停" % (date, added))

    return limit_ups


# =========================================
# 3. 网格搜索
# =========================================

# 每个信号的置信度阈值候选集
THRESHOLD_GRID = {
    "concept_resonance": [0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0],
    "auction_rush": [0.3, 0.4, 0.5, 0.6, 0.7],
    "volume_breakout": [0.15, 0.25, 0.35, 0.5, 0.7],
    "limit_dna": [0.3, 0.5, 0.6, 0.7, 0.85],
    "smallcap": [0.3, 0.5, 0.6, 0.8],
    "morning_power": [0.3, 0.4, 0.5, 0.7, 0.8],
    "whale_hunt": [0.3, 0.4, 0.5, 0.7],
}

# 组合规则置信度阈值
COMBO_THRESHOLD_GRID = [0.5, 0.55, 0.6, 0.65, 0.7, 0.75]


def apply_thresholds(record: dict, thresholds: dict) -> dict:
    """用新的置信度阈值重新判断信号触发状态"""
    new_signals = {}
    for sname in SIGNAL_PRIORITY:
        sig = record.get("signals", {}).get(sname, {})
        conf = sig.get("confidence", 0.0)
        threshold = thresholds.get(sname, 0.0)
        new_signals[sname] = {
            "triggered": conf >= threshold,
            "confidence": conf,
            "detail": sig.get("detail", ""),
        }
    return new_signals


def evaluate_thresholds(records: list, limit_ups: set,
                        thresholds: dict, combo_min_conf: float) -> dict:
    """评估一组阈值配置"""
    pushed_count = 0
    hit_count = 0
    total_hit = 0  # 数据中的实际涨停总数

    for r in records:
        is_zt = (r["trade_date"], r["code"].split(".")[0]) in limit_ups
        if is_zt:
            total_hit += 1

        # 重新判断信号
        new_signals = apply_thresholds(r, thresholds)

        # 重新应用组合规则
        should_push, combo, conf = signal_combination_judge(new_signals)

        # 组合确信度也要满足最低门槛
        if should_push and conf >= combo_min_conf:
            pushed_count += 1
            if is_zt:
                hit_count += 1

    recall = hit_count / total_hit if total_hit > 0 else 0
    precision = hit_count / pushed_count if pushed_count > 0 else 0
    f1 = 2 * recall * precision / (recall + precision) if (recall + precision) > 0 else 0

    return {
        "thresholds": thresholds.copy(),
        "combo_min_conf": combo_min_conf,
        "pushed": pushed_count,
        "hit": hit_count,
        "total_zt": total_hit,
        "recall": round(recall, 4),
        "precision": round(precision, 4),
        "f1": round(f1, 4),
        "coverage": round(hit_count / total_hit * 100, 1) if total_hit > 0 else 0,
        "push_rate": round(pushed_count / len(records) * 100, 1) if records else 0,
    }


def grid_search(records: list, limit_ups: set,
                top_n: int = 20) -> list:
    """网格搜索最优阈值组合

    策略: 由于全量网格搜索组合爆炸 (7^len(THRESHOLD_GRID) * 6),
    采用分步策略:
    1. 先固定所有信号阈值为默认值, 单独调每个信号的阈值
    2. 找到每个信号的最优阈值
    3. 用这些最优阈值做联合调优
    """
    print("\n  阶段1: 单信号阈值调优...")
    default_thresholds = {s: 0.0 for s in SIGNAL_PRIORITY}  # 全触发
    default_combo = 0.5

    # 单信号调优
    best_per_signal = {}
    for sname in SIGNAL_PRIORITY:
        grid = THRESHOLD_GRID.get(sname, [0.0])
        best_f1 = -1
        best_t = grid[0]
        for t in grid:
            thresholds = dict(default_thresholds)
            thresholds[sname] = t
            ev = evaluate_thresholds(records, limit_ups, thresholds, default_combo)
            if ev["f1"] > best_f1:
                best_f1 = ev["f1"]
                best_t = t
        best_per_signal[sname] = {"threshold": best_t, "f1": best_f1}
        print("    %s: 最优阈值=%.2f (F1=%.3f)" % (
            SIGNAL_LABELS.get(sname, sname), best_t, best_f1))

    # 联合调优
    print("\n  阶段2: 联合阈值 + 组合确信度调优...")
    results = []
    total = 1
    for grid_list in THRESHOLD_GRID.values():
        total *= len(grid_list)
    total *= len(COMBO_THRESHOLD_GRID)
    print("    全量搜索 %d 种组合..." % total)

    # 采样搜索 (每个信号取最优附近的2-3个值)
    sampled_grid = {}
    for sname in SIGNAL_PRIORITY:
        best_t = best_per_signal[sname]["threshold"]
        all_vals = THRESHOLD_GRID.get(sname, [0.0])
        # 取最优值附近的值
        idx = all_vals.index(best_t) if best_t in all_vals else 0
        nearby = []
        for di in [-1, 0, 1]:
            ni = idx + di
            if 0 <= ni < len(all_vals):
                nearby.append(all_vals[ni])
        sampled_grid[sname] = sorted(set(nearby))
        print("    %s: 采样 %s" % (SIGNAL_LABELS.get(sname, sname),
                                   [str(x) for x in sampled_grid[sname]]))

    # 枚举采样组合
    from itertools import product
    sampled_count = 1
    for vals in sampled_grid.values():
        sampled_count *= len(vals)
    sampled_count *= len(COMBO_THRESHOLD_GRID)
    print("    采样搜索 %d 种组合..." % sampled_count)

    signal_names = list(sampled_grid.keys())
    for i, combo_vals in enumerate(product(*sampled_grid.values())):
        thresholds = dict(zip(signal_names, combo_vals))
        for combo_conf in COMBO_THRESHOLD_GRID:
            ev = evaluate_thresholds(records, limit_ups, thresholds, combo_conf)
            results.append(ev)
        if (i + 1) % 200 == 0:
            print("      进度: %d" % (i + 1))

    # 按 F1 排序
    results.sort(key=lambda x: (-x["f1"], -x["recall"], x["push_rate"]))

    # 取 Pareto 前沿 (非支配解: 没有其他配置在 recall 和 precision 上都优于它)
    pareto = []
    for r in results:
        dominated = False
        for p in pareto:
            if (p["recall"] >= r["recall"] and p["precision"] >= r["precision"]
                    and (p["recall"] > r["recall"] or p["precision"] > r["precision"])):
                dominated = True
                break
        if not dominated:
            pareto.append(r)
            if len(pareto) >= top_n:
                break

    return results, pareto


# =========================================
# 4. 报告输出
# =========================================

def print_report(baseline: dict, all_results: list, pareto: list,
                 records: list, limit_ups: set):
    """打印优化报告"""
    total_zt = sum(1 for r in records
                   if (r["trade_date"], r["code"].split(".")[0]) in limit_ups)

    print("\n" + "=" * 70)
    print("信号阈值优化报告 V2")
    print("=" * 70)
    print("数据: %d 条记录, 实际涨停 %d 只" % (len(records), total_zt))

    # 基准
    print("\n基准 (当前默认阈值):")
    print("  推送: %d, 命中: %d, 命中率: %.1f%%" % (
        baseline["pushed"], baseline["hit"],
        baseline["precision"] * 100))
    print("  Recall: %.1f%%, Precision: %.1f%%, F1: %.3f" % (
        baseline["recall"] * 100, baseline["precision"] * 100, baseline["f1"]))

    # Pareto 前沿
    print("\nPareto 前沿 (Top %d):" % min(len(pareto), 10))
    print("%4s %8s %6s %6s %6s %8s %8s" % (
        "Rank", "F1", "Recall", "Prec", "推送", "覆盖%%", "推送率%%"))
    print("%s" % ("-" * 55))
    for i, r in enumerate(pareto[:10]):
        print("%3d. %7.3f %5.1f%% %5.1f%% %4d  %5.1f%% %5.1f%%" % (
            i + 1, r["f1"], r["recall"] * 100, r["precision"] * 100,
            r["pushed"], r["coverage"], r["push_rate"]))

    # 推荐配置
    if pareto:
        best = pareto[0]
        print("\n推荐配置 (最高 F1=%.3f):" % best["f1"])
        print("  信号阈值:")
        for sname in SIGNAL_PRIORITY:
            t = best["thresholds"].get(sname, 0)
            label = SIGNAL_LABELS.get(sname, sname)
            print("    %s: conf >= %.2f" % (label, t))
        print("  组合最低确信度: %.2f" % best["combo_min_conf"])
        print("  预期: Recall %.1f%%, Precision %.1f%%, 推送 %d 只/天" % (
            best["recall"] * 100, best["precision"] * 100,
            best["pushed"] // max(len(set(r["trade_date"] for r in records)), 1)))

    # 信号贡献分析
    print("\n信号阈值灵敏度 (F1 变化):")
    default_thresholds = {s: 0.0 for s in SIGNAL_PRIORITY}
    base_ev = evaluate_thresholds(records, limit_ups, default_thresholds, 0.5)
    for sname in SIGNAL_PRIORITY:
        grid = THRESHOLD_GRID.get(sname, [0.0])
        max_f1_diff = 0
        best_t_for_signal = grid[0]
        for t in grid:
            thresh = dict(default_thresholds)
            thresh[sname] = t
            ev = evaluate_thresholds(records, limit_ups, thresh, 0.5)
            diff = ev["f1"] - base_ev["f1"]
            if diff > max_f1_diff:
                max_f1_diff = diff
                best_t_for_signal = t
        label = SIGNAL_LABELS.get(sname, sname)
        impact = "高" if max_f1_diff > 0.05 else ("中" if max_f1_diff > 0.01 else "低")
        print("  %s: 最优阈值=%.2f F1改善=%+.3f 影响=%s" % (
            label, best_t_for_signal, max_f1_diff, impact))

    print("\n" + "=" * 70)


def save_results(baseline: dict, all_results: list, pareto: list,
                 records: list, limit_ups: set):
    """保存优化结果"""
    output = {
        "optimized_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total_records": len(records),
        "total_zt": sum(1 for r in records
                        if (r["trade_date"], r["code"].split(".")[0]) in limit_ups),
        "dates": sorted(set(r["trade_date"] for r in records)),
        "baseline": baseline,
        "pareto_frontier": pareto[:15],
        "top_10": [{
            "rank": i + 1,
            "f1": r["f1"],
            "recall": r["recall"],
            "precision": r["precision"],
            "pushed": r["pushed"],
            "hit": r["hit"],
            "thresholds": r["thresholds"],
            "combo_min_conf": r["combo_min_conf"],
        } for i, r in enumerate(all_results[:10])],
    }

    WEIGHTS_DIR.mkdir(parents=True, exist_ok=True)
    out_file = WEIGHTS_DIR / "signal_optimized.json"
    with open(out_file, "w") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print("\n结果已保存: %s" % out_file)


# =========================================
# Main
# =========================================

def main(days: int = 14):
    print("=" * 70)
    print("信号阈值优化引擎 V2 — %s" % datetime.now())
    print("=" * 70)

    clear_tushare_cache()

    # 1. 加载 V2 数据
    print("\n[1/4] 加载 V2 分析数据...")
    records = load_v2_analysis(days)
    if not records:
        print("无 V2 数据。请先运行 pipeline_v2.py 积累数据。")
        return
    dates = sorted(set(r["trade_date"] for r in records))

    # 2. 拉涨停
    print("\n[2/4] 拉取实际涨停数据 (%d 天)..." % len(dates))
    limit_ups = fetch_limit_ups(dates)
    total_zt = sum(1 for r in records
                   if (r["trade_date"], r["code"].split(".")[0]) in limit_ups)
    print("  实际涨停: %d 只 (在记录中)" % total_zt)

    if total_zt < 5:
        print("  涨停数据太少 (<5), 优化结果仅供参考")

    # 3. 基准评估
    print("\n[3/4] 基准评估 + 网格搜索...")
    default_thresholds = {s: 0.0 for s in SIGNAL_PRIORITY}
    baseline = evaluate_thresholds(records, limit_ups, default_thresholds, 0.5)

    all_results, pareto = grid_search(records, limit_ups, top_n=20)

    # 4. 输出
    print("\n[4/4] 输出报告...")
    print_report(baseline, all_results, pareto, records, limit_ups)
    save_results(baseline, all_results, pareto, records, limit_ups)

    print("\n完成!")


if __name__ == "__main__":
    days = 14
    if len(sys.argv) > 1:
        for arg in sys.argv[1:]:
            if arg.startswith("--days="):
                days = int(arg.split("=")[1])
    main(days)
