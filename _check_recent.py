#!/usr/bin/env python3
"""检查最近几轮 pipeline 候选股中哪些符合宽松推送条件"""
import json
from pathlib import Path

analysis_dir = Path("plays/limit_up/data/analysis")

# 取最近6轮
files = sorted(analysis_dir.glob("20260615_*.json"), reverse=True)[:8]

print(f"最近 {len(files)} 轮检查")
print()

for fp in files:
    data = json.loads(fp.read_text())
    label = fp.stem  # 20260615_1434
    
    # 当前高确信规则
    hc_pass = []
    # 宽松规则: 情绪>=25 + 总分>=35
    loose_pass = []
    
    for s in data:
        total = s.get("total", 0)
        if isinstance(total, (int, float)):
            total = float(total)
        else:
            total = 0
        
        # 午盘宽松: 情绪>=25 + 总分>=35
        if total >= 35:
            scores = s.get("scores", {})
            sentiment = float(scores.get("sentiment", 0))
            fundflow = float(scores.get("fundflow", 0))
            
            # 当前规则
            if sentiment >= 35 and fundflow >= 35 and total >= 40:
                hc_pass.append(s)
            
            # 宽松规则
            if sentiment >= 25:
                loose_pass.append(s)
    
    time_str = label[-4:] + ":" + label[-2:]
    print(f"  {label[-4:]}  {len(data):2d}只  高确信{len(hc_pass):2d}只  宽松{len(loose_pass):2d}只", end="")
    if loose_pass:
        top = sorted(loose_pass, key=lambda x: x.get("top3_score",0), reverse=True)[:3]
        names = []
        for s in top:
            n = s["name"] + "(" + str(int(s.get("top3_score",0))) + ")"
            names.append(n)
        print(f"  [{', '.join(names)}]", end="")
    print()

# 最新一轮详细
latest = files[0]
data = json.loads(latest.read_text())
print(f"\n===== 最新一轮 ({latest.stem[-4:]}) 详细 =====")
print(f"\n{'名称':8s} {'综合':>5s} {'基本面':>5s} {'技术面':>5s} {'资金面':>5s} {'情绪面':>5s} {'短线':>5s} {'涨幅':>5s}")
print("-" * 55)
for s in sorted(data, key=lambda x: x.get("top3_score", 0), reverse=True):
    sc = s.get("scores", {})
    total = s.get("top3_score", 0)
    if total >= 35:
        print(f'{s["name"]:8s} {total:5.1f} {sc.get("fundamental",0):5.1f} {sc.get("technical",0):5.1f} {sc.get("fundflow",0):5.1f} {sc.get("sentiment",0):5.1f} {sc.get("shortterm",0):5.1f} {s.get("pct_chg",0):5.1f}%')

# 再看宽松能推哪几只
print(f"\n===== 宽松推送候选 (情绪>=25 + 总分>=35) =====")
candidates = [s for s in sorted(data, key=lambda x: x.get("top3_score",0), reverse=True)
              if s.get("top3_score",0) >= 35 and float(s.get("scores",{}).get("sentiment",0)) >= 25]
for s in candidates:
    sc = s.get("scores", {})
    print(f'  {s["name"]:8s} {s.get("top3_score",0):5.1f} 涨幅{s.get("pct_chg",0):5.1f}% 情绪{sc.get("sentiment",0):3.0f} 资金{sc.get("fundflow",0):3.0f} 短线{sc.get("shortterm",0):4.1f}')
print(f'\n共 {len(candidates)} 只符合条件')
