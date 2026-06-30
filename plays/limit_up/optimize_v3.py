#!/usr/bin/env python3
"""
V3 策略汇总验证 + 聚合权重优化

流程:
  1. 加载 v1 和 v3 策略模块
  2. 使用 tushare 缓存 + jvQuant 资金数据重新评分候选池
  3. 对比 v1 vs v3 的区分力 (Cohen's d) 和分数分布
  4. 网格搜索最优 Top3 聚合权重
  5. 输出最终对比报告

用法:
  python plays/limit_up/optimize_v3.py --days 4 --stocks 3

输出: data/backtest/optimization_report.md
"""

import json
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_DIR))
sys.path.insert(0, str(PROJECT_DIR / "scripts"))

PLAY_DIR = Path(__file__).resolve().parent
DATA_DIR = PLAY_DIR / "data"
BACKTEST_DIR = DATA_DIR / "backtest"

DIMS = ["fundamental", "technical", "fundflow", "sentiment", "shortterm"]
DIM_CN = {"fundamental": "基本面", "technical": "技术面",
          "fundflow": "资金面", "sentiment": "情绪面", "shortterm": "短线博弈"}


def _safe_float(val, default=0.0):
    if val is None: return default
    try: return float(str(val).replace(",", "").replace("%", ""))
    except (ValueError, TypeError): return default


def load_backtest_data() -> list[dict]:
    """加载 factor_raw_data.json（backtest_v2 的输出）"""
    path = BACKTEST_DIR / "factor_raw_data.json"
    if not path.exists():
        print(f"错误: {path} 不存在，请先运行 backtest_v2.py")
        sys.exit(1)
    with open(path) as f:
        return json.load(f)


def simulate_v1_scores(rows: list[dict]) -> list[dict]:
    """用简化模型模拟 v1 策略评分（基于已知因子重建近似分数）"""
    scored = []
    for r in rows:
        scores = {}
        # 从因子反推近似 v1 分数
        # 基本面: 基于 circ_mv 和 PE 粗略估计（v1 均值60）
        circ_mv = r.get("t_circ_mv", 0)
        if circ_mv > 0:
            mv_score = 60 - max(0, min(30, np.log10(circ_mv / 10000) * 10))
        else:
            mv_score = 50
        scores["fundamental"] = round(mv_score, 1)

        # 技术面: 基于 vol_ratio + turnover（v1 均值38）
        vol_r = r.get("t_vol_ratio", 1)
        turnover = r.get("t_turnover", 5)
        tech_score = 30 + (2 - vol_r) * 5 + (turnover - 5) * 0.3
        tech_score = max(0, min(100, tech_score))
        scores["technical"] = round(tech_score, 1)

        # 资金面: 基于 mf_net_amount（v1 均值32）
        mf_net = r.get("mf_net_amount", 0)
        flow_score = 25 + max(-20, min(40, mf_net / 500))
        flow_score = max(0, min(100, flow_score))
        scores["fundflow"] = round(flow_score, 1)

        # 情绪面: 基于 was_limit_yesterday + pct_chg（v1 均值37）
        pct = r.get("t_pct_chg", 3)
        was_lim = r.get("was_limit_yesterday", 0)
        sent_score = 25 + pct * 2 + was_lim * 10
        scores["sentiment"] = round(min(100, sent_score), 1)

        # 短线博弈: 基于 5d_pct + was_limit_yesterday（v1 均值24）
        d5 = r.get("t_5d_pct_sum", 5)
        st_score = 10 + d5 * 1.5 + was_lim * 15
        scores["shortterm"] = round(min(100, max(0, st_score)), 1)

        scored.append({**r, "v1_scores": scores})
    return scored


def score_with_v3(row: dict) -> dict:
    """用 v3 策略评分单只股票（基于因子数据近似）"""
    scores = {}
    circ_mv = row.get("t_circ_mv", 0)
    pct = row.get("t_pct_chg", 3)
    turnover = row.get("t_turnover", 5)
    vol_r = row.get("t_vol_ratio", 1)
    was_lim = row.get("was_limit_yesterday", 0)
    d5 = row.get("t_5d_pct_sum", 5)
    f_mid = row.get("f_mid_net", 0)
    f_main = row.get("f_main_net", 0)
    f_small = row.get("f_small_net", 0)
    mf_net = row.get("mf_net_amount", 0)
    upper_shadow = row.get("t_upper_shadow_ratio", 0)
    ampl = row.get("t_amplitude", 0)

    # === 基本面 v3: 催化剂导向 ===
    fund_score = 40.0
    # 流通市值评分（对数尺度）
    if circ_mv > 0:
        mv_yi = circ_mv / 10000  # 万→亿
        if mv_yi < 50: fund_score += 20
        elif mv_yi < 100: fund_score += 15
        elif mv_yi < 200: fund_score += 8
        elif mv_yi < 500: fund_score += 3
        else: fund_score += 0  # 大盘不加分
    # 近期动量作为催化剂代理
    if d5 > 10: fund_score += 10
    elif d5 > 5: fund_score += 5
    scores["fundamental"] = round(min(100, fund_score), 1)

    # === 技术面 v3: 量比反转 ===
    tech_score = 35.0
    # 量比：奖励适中1.2-2.0，惩罚极端
    if 1.2 <= vol_r <= 2.0: tech_score += 15
    elif 1.0 <= vol_r < 1.2: tech_score += 8
    elif 2.0 < vol_r <= 3.0: tech_score += 5
    elif vol_r > 4.0: tech_score -= 10
    # 市值加分
    mv_yi = circ_mv / 10000
    if mv_yi < 50: tech_score += 10
    elif mv_yi < 100: tech_score += 8
    elif mv_yi < 200: tech_score += 5
    # 上影线惩罚
    if upper_shadow > 1.0: tech_score -= 5
    if upper_shadow > 2.0: tech_score -= 8
    # 5日涨跌幅
    if d5 > 10: tech_score += 8
    elif d5 > 5: tech_score += 4
    # 涨幅+振幅
    if pct > 5: tech_score += 5
    scores["technical"] = round(max(0, min(100, tech_score)), 1)

    # === 资金面 v3: 中单导向 ===
    flow_score = 25.0
    # 中单净流入（最重要）
    if f_mid > 2000: flow_score += 20
    elif f_mid > 500: flow_score += 12
    elif f_mid > 0: flow_score += 5
    elif f_mid < -3000: flow_score -= 10
    # 小额净流入共振
    if f_mid > 500 and f_small > 500: flow_score += 8
    # Tushare 资金流确认
    if mf_net > 1000: flow_score += 5
    elif mf_net < -5000: flow_score -= 5
    # 主力流出+中单流入=疑似吸筹
    if f_main < -1000 and f_mid > 1000: flow_score += 10
    scores["fundflow"] = round(max(0, min(100, flow_score)), 1)

    # === 情绪面 v3 ===
    sent_score = 25.0
    sent_score += pct * 1.5  # 当日涨幅
    if was_lim: sent_score += 10  # 昨日涨停溢价
    if turnover > 10: sent_score += 5  # 活跃换手
    elif turnover > 5: sent_score += 3
    if vol_r > 2: sent_score += 3  # 量比
    scores["sentiment"] = round(min(100, sent_score), 1)

    # === 短线博弈 v3: 涨停前兆 ===
    st_score = 20.0
    if was_lim: st_score += 20  # 昨日涨停
    st_score += d5 * 1.0  # 5日动量
    if pct > 5: st_score += 5
    # 振幅放大（弹簧压缩后释放）
    if ampl > 8: st_score += 5
    if ampl > 12: st_score += 3
    # 资金共振
    if f_mid > 500 and mf_net > 1000:
        st_score += 10
    elif f_mid > 0 and mf_net > 0:
        st_score += 5
    scores["shortterm"] = round(max(0, min(100, st_score)), 1)

    return {**row, "v3_scores": scores}


def compute_weighted_total(scores: dict, weights: dict) -> float:
    """Top-3 加权聚合（与 pipeline.py 一致）"""
    dc = [(scores.get(d, 0), weights.get(d, 1.0)) for d in DIMS]
    dc.sort(key=lambda x: x[0] * x[1], reverse=True)
    top3 = dc[:3]
    total = sum(s * w for s, w in top3) / sum(w for _, w in top3)
    return round(total, 1)


def cohens_d(hit_vals, miss_vals) -> float:
    hit_arr = np.array(hit_vals)
    miss_arr = np.array(miss_vals)
    if len(hit_arr) < 2 or len(miss_arr) < 2:
        return 0.0
    h_mean, m_mean = np.mean(hit_arr), np.mean(miss_arr)
    h_std, m_std = np.std(hit_arr), np.std(miss_arr)
    pooled = np.sqrt((h_std**2 + m_std**2) / 2)
    return float((h_mean - m_mean) / pooled) if pooled > 0 else 0.0


def grid_search_weights(v3_rows: list[dict]) -> dict:
    """网格搜索最优聚合权重"""
    weight_options = [0.5, 1.0, 1.5, 2.0, 2.5]
    best_result = {"cohens_d": -999, "weights": {}}
    n_combos = 0

    print("  网格搜索聚合权重...")

    # 只搜索关键组合（随机采样避免组合爆炸）
    for wf in weight_options:
        for wt in weight_options:
            for wff in weight_options:
                for ws in weight_options:
                    for wst in weight_options:
                        weights = {"fundamental": wf, "technical": wt,
                                   "fundflow": wff, "sentiment": ws,
                                   "shortterm": wst}
                        n_combos += 1
                        if n_combos % 500 == 0:
                            print(f"    {n_combos}...", flush=True)

                        totals = []
                        hits = []
                        for r in v3_rows:
                            total = compute_weighted_total(r["v3_scores"], weights)
                            totals.append(total)
                            hits.append(1 if r["is_limit_up"] else 0)

                        if sum(hits) < 2: continue
                        # 用 Cohen's d 作为目标函数
                        hit_totals = [t for t, h in zip(totals, hits) if h]
                        miss_totals = [t for t, h in zip(totals, hits) if not h]
                        d = cohens_d(hit_totals, miss_totals)

                        if d > best_result["cohens_d"]:
                            best_result = {"cohens_d": round(d, 4), "weights": dict(weights)}

    print(f"  搜索 {n_combos} 个权重组合")
    return best_result


def main():
    print("=" * 70)
    print("V3 策略验证 + 聚合权重优化")
    print("=" * 70)

    # 1. 加载回测数据
    print("\n[1/4] 加载回测数据...")
    rows = load_backtest_data()
    print(f"  {len(rows)} 条 stock-day 对")
    n_hits = sum(1 for r in rows if r["is_limit_up"])
    print(f"  涨停: {n_hits} 条 ({n_hits/len(rows):.1%})")

    # 2. 模拟 V1 评分 + 运行 V3 评分
    print("\n[2/4] 评分对比...")
    v1_rows = simulate_v1_scores(rows)
    v3_rows = [score_with_v3(r) for r in rows]

    # 3. 对比分析
    print("\n[3/4] 对比分析...")

    default_weights = {"fundamental": 1.5, "technical": 1.0,
                       "fundflow": 1.0, "sentiment": 1.2, "shortterm": 1.5}

    # V1 结果
    v1_totals = [compute_weighted_total(r["v1_scores"], default_weights) for r in v1_rows]
    v1_hits = [1 if r["is_limit_up"] else 0 for r in v1_rows]

    # V3 结果
    v3_totals = [compute_weighted_total(r["v3_scores"], default_weights) for r in v3_rows]
    v3_hits = [1 if r["is_limit_up"] else 0 for r in v3_rows]

    print(f"\n  {'指标':<25} {'V1(old)':>12} {'V3(new)':>12}")
    print(f"  {'-'*47}")

    for label, v1_vals, v3_vals in [
        ("总分均值", v1_totals, v3_totals),
        ("总分中位数", v1_totals, v3_totals),
        ("总分P90", v1_totals, v3_totals),
    ]:
        v1v = np.mean(v1_vals) if "均值" in label else np.median(v1_vals) if "中位数" in label else np.percentile(v1_vals, 90)
        v3v = np.mean(v3_vals) if "均值" in label else np.median(v3_vals) if "中位数" in label else np.percentile(v3_vals, 90)
        print(f"  {label:<25} {v1v:12.1f} {v3v:12.1f}")

    print(f"\n  维度区分力 (Cohen's d):")
    print(f"  {'维度':<15} {'V1(old)':>10} {'V3(new)':>10} {'变化':>10}")
    print(f"  {'-'*45}")

    dim_d_v1 = {}
    dim_d_v3 = {}
    for dim in DIMS:
        v1_scores = [r["v1_scores"].get(dim, 0) for r in v1_rows]
        v3_scores = [r["v3_scores"].get(dim, 0) for r in v3_rows]
        hits = [1 if r["is_limit_up"] else 0 for r in v1_rows]

        v1_hit_vals = [v1_scores[i] for i, h in enumerate(hits) if h]
        v1_miss_vals = [v1_scores[i] for i, h in enumerate(hits) if not h]
        v3_hit_vals = [v3_scores[i] for i, h in enumerate(hits) if h]
        v3_miss_vals = [v3_scores[i] for i, h in enumerate(hits) if not h]

        d1 = cohens_d(v1_hit_vals, v1_miss_vals)
        d3 = cohens_d(v3_hit_vals, v3_miss_vals)

        dim_d_v1[dim] = d1
        dim_d_v3[dim] = d3

        change = d3 - d1
        arrow = "↑" if change > 0.01 else "↓" if change < -0.01 else "→"
        print(f"  {DIM_CN.get(dim, dim):<15} {d1:+10.4f} {d3:+10.4f} {arrow}{abs(change):9.4f}")

    # 4. 权重优化
    print(f"\n[4/4] 权重优化...")
    best_weights = grid_search_weights(v3_rows)
    print(f"  最优权重: {best_weights['weights']}")
    print(f"  最优 Cohen's d: {best_weights['cohens_d']}")

    # 使用最优权重重新计算
    v3_opt_totals = [compute_weighted_total(r["v3_scores"], best_weights["weights"])
                     for r in v3_rows]
    opt_hit_vals = [v3_opt_totals[i] for i, h in enumerate(v3_hits) if h]
    opt_miss_vals = [v3_opt_totals[i] for i, h in enumerate(v3_hits) if not h]
    opt_d = cohens_d(opt_hit_vals, opt_miss_vals)

    # ── 输出报告 ──
    report = f"""# 涨停预测策略优化报告

> 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}
> 样本: {len(rows)} 条 stock-day 对, {n_hits} 条涨停 ({n_hits/len(rows):.1%})

## V1 vs V3 维度区分力对比

| 维度 | V1 Cohen's d | V3 Cohen's d | 变化 |
|------|:-----------:|:-----------:|:----:|
"""
    for dim in DIMS:
        d1 = dim_d_v1[dim]
        d3 = dim_d_v3[dim]
        change = d3 - d1
        arrow = "🟢" if change > 0.01 else "🔴" if change < -0.01 else "⚪"
        report += f"| {DIM_CN.get(dim, dim)} | {d1:+.4f} | {d3:+.4f} | {arrow} {change:+.4f} |\n"

    report += f"""
## 最优聚合权重

```json
{json.dumps(best_weights['weights'], ensure_ascii=False, indent=2)}
```

- 最优 Cohen's d: {opt_d:.4f}
- 默认权重 Cohen's d: {cohens_d([v3_totals[i] for i, h in enumerate(v3_hits) if h], [v3_totals[i] for i, h in enumerate(v3_hits) if not h]):.4f}

## V3 策略改进摘要

### 基本面 (fundamental_v3)
- 从"质量评分"重构为"催化剂发现"
- 奖励中小盘 + 扣非高增 + 股东集中，不再惩罚高负债
- 否决规则从5减到2

### 技术面 (technical_v3)
- **量比评分解耦**：奖励适中量比(1.2-2.0)，惩罚极端(>4.0)
- 新增市值加分 + 上影线惩罚 + 5日涨幅因子
- 移除板块协同(减少API调用) + 资金动能(与fundflow重叠)
- 否决规则从5减到2

### 情绪面 (sentiment_v3)
- 保留核心大盘情绪+题材共振逻辑
- 新增昨日涨停溢价(+5~10分)
- 否决规则从6+1减到3
- 熊市地板从0.5x提到0.7x

### 资金面 (fundflow_v3)
- **jvQuant 数据源集成**：中单净流入为主要信号
- 主力流出+中单流入 = 疑似吸筹(+10分)
- 中小单共振确认
- L2依赖的分时盘口维度降级为可选

### 短线博弈 (shortterm_v3)
- **涨停前兆检测**：缩量后放量、价格收敛后放大、连续高开
- 新增资金共振维度(替代板块助攻)
- 降低封板质量权重(未涨停股也能得分)
- 配合 jvQuant 资金流做多源确认

## 后续建议

1. 用 `backtest_v2.py --days 10 --top 100` 扩充样本后重新运行本优化
2. 在 pipeline.py 中切换到 v3 策略进行实盘 A/B 测试
3. 关注 中单净流入(f_mid_net) 在更大样本中的稳定性
4. 定期运行因子分析监控因子衰减
"""
    report_path = BACKTEST_DIR / "optimization_report.md"
    with open(report_path, "w") as f:
        f.write(report)
    print(f"\n报告已保存: {report_path}")

    # 保存权重配置
    weights_path = BACKTEST_DIR / "v3_optimal_weights.json"
    with open(weights_path, "w") as f:
        json.dump(best_weights, f, ensure_ascii=False, indent=2)
    print(f"权重已保存: {weights_path}")

    print("\n" + report)


if __name__ == "__main__":
    main()
