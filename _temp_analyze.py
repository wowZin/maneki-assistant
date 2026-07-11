#!/usr/bin/env python3
"""临时分析脚本：中京电子 002579.SZ"""

import json, sys, os, time
from pathlib import Path
from datetime import datetime

PROJECT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_DIR))

code = "002579.SZ"
name = "中京电子"
print(f"=== 中京电子 {code} 五维度分析 ===\n")

# ---- 1. 五维度评分 ----
from concurrent.futures import ThreadPoolExecutor, as_completed

funcs = {}
try:
    from plays.limit_up.strategies.fundamental import score_fundamental
    funcs["fundamental"] = score_fundamental
except ImportError as e:
    print(f"基本面模块加载失败: {e}")
try:
    from plays.limit_up.strategies.technical import score_technical
    funcs["technical"] = score_technical
except ImportError as e:
    print(f"技术面模块加载失败: {e}")
try:
    from plays.limit_up.strategies.fundflow import score_fundflow
    funcs["fundflow"] = score_fundflow
except ImportError as e:
    print(f"资金面模块加载失败: {e}")
try:
    from plays.limit_up.strategies.sentiment import score_sentiment
    funcs["sentiment"] = score_sentiment
except ImportError as e:
    print(f"情绪面模块加载失败: {e}")
try:
    from plays.limit_up.strategies.shortterm import score_shortterm
    funcs["shortterm"] = score_shortterm
except ImportError as e:
    print(f"短线博弈模块加载失败: {e}")

scores = {}
reasons = {}
errors = []

with ThreadPoolExecutor(max_workers=5) as pool:
    futures = {pool.submit(fn, code): name for name, fn in funcs.items()}
    for future in as_completed(futures):
        dim = futures[future]
        try:
            s, r = future.result()
            scores[dim] = s
            reasons[dim] = r
        except Exception as e:
            errors.append(f"{dim}: {e}")
            scores[dim] = 0
            reasons[dim] = f"评分异常: {e}"

dim_names = {
    "fundamental": "基本面",
    "technical": "技术面",
    "fundflow": "资金面",
    "sentiment": "情绪面",
    "shortterm": "短线博弈"
}

print("【五维度评分】")
for dim, label in dim_names.items():
    sc = scores.get(dim, 0)
    re = reasons.get(dim, "无")
    print(f"  {label}: {sc}分 — {re}")

if errors:
    print(f"\n评分错误: {errors}")

# ---- 2. 模型总分 (total_score) ----
print("\n--- 尝试计算模型总分 ---")
try:
    # 预取面板数据
    from scripts.tu_share import CONFIG, clear_tushare_cache
    clear_tushare_cache()

    from plays.limit_up.pit_features import build_pit_features
    from plays.limit_up.factors import REGISTRY, TOTAL_SCORE_COMPONENTS
    from plays.limit_up.total import total_score

    # 检查是否使用模型模式
    model_mode = "model_score" in TOTAL_SCORE_COMPONENTS
    print(f"  模型模式: {model_mode}")
    print(f"  评分组件: {TOTAL_SCORE_COMPONENTS}")

    if model_mode:
        # 需要完整的 NV2 数据
        from plays.limit_up.pipeline_feishu import _fetch_nv2_data, _extract_pit_features
        _fetch_nv2_data([code])
        feats = _extract_pit_features(code, pit_mode=True)
        feats["sentiment"] = scores.get("sentiment", 0)
        feats["shortterm"] = scores.get("shortterm", 0)
        feats["technical"] = scores.get("technical", 0)
        feats["fundflow"] = scores.get("fundflow", 0)
        feats["fundamental"] = scores.get("fundamental", 0)

        import pandas as pd
        from plays.limit_up.factors.optimized.model_score import factor_model_score_batch
        dfs = pd.DataFrame([feats])
        model_scores = factor_model_score_batch(dfs)
        ts = round(float(model_scores.iloc[0]), 2)
        print(f"\n  ★ 模型总分 (total_score): {ts}")
    else:
        # 传统因子模式
        feats = {}
        feats["sentiment"] = scores.get("sentiment", 0)
        feats["shortterm"] = scores.get("shortterm", 0)
        feats["technical"] = scores.get("technical", 0)
        feats["fundflow"] = scores.get("fundflow", 0)
        feats["fundamental"] = scores.get("fundamental", 0)
        ts = total_score(feats)
        print(f"\n  ★ 总分 (total_score): {ts:.1f}")

except Exception as e:
    print(f"  模型总分计算失败: {e}")
    import traceback
    traceback.print_exc()
    # fallback: 简单展示维度分
    print(f"\n  [回退] 五维度原始分: {scores}")
    ts = sum(scores.values()) / len(scores) if scores else 0
    print(f"  算术平均(仅供参考,非正式总分): {ts:.1f}")

# ---- 3. 展示总结 ----
print("\n" + "=" * 50)
print(f"中京电子 ({code}) 分析完成")
print(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
print("=" * 50)
