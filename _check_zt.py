#!/usr/bin/env python3
"""获取今日涨停个股并打分，看按规则能推送多少"""
import sys
sys.path.insert(0, '/root/maneki-agent')
import json
from datetime import datetime

from scripts.ths_client import get_ths_client

# 1. 获取热门榜涨停股
ths = get_ths_client()
items = ths.get_hot_list()
if not items:
    print("无法获取热门榜")
    sys.exit(1)

zt_stocks = [s for s in items if float(s.get("pct_chg", 0)) >= 9.5]
print(f"热门榜共 {len(items)} 只，涨停 {len(zt_stocks)} 只")
print()

# 2. 对每只涨停股评分
from plays.limit_up.strategies.fundamental import score_fundamental
from plays.limit_up.strategies.technical import score_technical
from plays.limit_up.strategies.fundflow import score_fundflow
from plays.limit_up.strategies.sentiment import score_sentiment
from plays.limit_up.strategies.shortterm import score_shortterm

results = []
for s in zt_stocks:
    code_short = s["code"]
    # 转成带后缀代码
    if "." in code_short:
        code_full = code_short
    else:
        code_full = f"{code_short}.SH" if code_short.startswith("6") else f"{code_short}.SZ"
    
    name = s.get("name", "")
    pct = float(s.get("pct_chg", 0))
    
    # 五维度评分
    try:
        f_score, f_reason = score_fundamental(code_full)
        t_score, t_reason = score_technical(code_full)
        ff_score, ff_reason = score_fundflow(code_full)
        st_score, st_reason = score_sentiment(code_full)
        stm_score, stm_reason = score_shortterm(code_full)
    except Exception as e:
        print(f"  {code_full} {name} 评分失败: {e}")
        continue
    
    scores = {
        "fundamental": f_score,
        "technical": t_score,
        "fundflow": ff_score,
        "sentiment": st_score,
        "shortterm": stm_score,
    }
    
    # 加权Top3择优
    sorted_scores = sorted(scores.values(), reverse=True)[:3]
    top3_score = sum(sorted_scores) / 3
    
    results.append({
        "code": code_full,
        "name": name,
        "pct_chg": pct,
        "scores": scores,
        "top3_score": round(top3_score, 1)
    })

# 3. 按总分排序
results.sort(key=lambda x: x["top3_score"], reverse=True)

# 4. 检查推送门槛
print(f"评分完成: {len(results)} 只")
print()
print(f"{'名称':8s} {'综合':>5s} {'基本面':>5s} {'技术面':>5s} {'资金面':>5s} {'情绪面':>5s} {'短线':>5s} {'涨幅':>5s}")
print("-" * 55)
for r in results:
    sc = r["scores"]
    print(f'{r["name"]:8s} {r["top3_score"]:5.1f} {sc["fundamental"]:5.1f} {sc["technical"]:5.1f} {sc["fundflow"]:5.1f} {sc["sentiment"]:5.1f} {sc["shortterm"]:5.1f} {r["pct_chg"]:5.1f}%')

# 5. 检查推送规则
print()
print("=" * 60)
print("高确信推送检查 (情绪>=35 + 资金>=35 + 总分>=40)")
print("=" * 60)
pass_count = 0
for r in results:
    sc = r["scores"]
    can_push = (
        sc["sentiment"] >= 35 and
        sc["fundflow"] >= 35 and
        r["top3_score"] >= 40
    )
    if can_push:
        pass_count += 1
        print(f'  ✅ {r["name"]:8s} {r["top3_score"]:5.1f} 情绪{sc["sentiment"]:3.0f} 资金{sc["fundflow"]:3.0f}')
    else:
        reason = []
        if sc["sentiment"] < 35: reason.append(f"情绪{sc['sentiment']:.0f}<35")
        if sc["fundflow"] < 35: reason.append(f"资金{sc['fundflow']:.0f}<35")
        if r["top3_score"] < 40: reason.append(f"总分{r['top3_score']:.1f}<40")
        print(f'  ❌ {r["name"]:8s} {r["top3_score"]:5.1f}   {" + ".join(reason)}')

print()
print(f"结论: {len(results)} 只涨停股中, {pass_count} 只能通过推送门槛")
