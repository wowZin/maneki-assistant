#!/usr/bin/env python3
"""今日涨停股评分检查"""
import sys
import re
import time
sys.path.insert(0, '/root/maneki-agent')
from scripts.ths_client import get_ths_client
from plays.limit_up.strategies.fundamental import score_fundamental
from plays.limit_up.strategies.technical import score_technical
from plays.limit_up.strategies.fundflow import score_fundflow
from plays.limit_up.strategies.sentiment import score_sentiment
from plays.limit_up.strategies.shortterm import score_shortterm

# 1. 获取涨停股
ths = get_ths_client()
items = ths.get_hot_list()
zt = [s for s in items if float(s.get("pct_chg", 0)) >= 9.5]
print(f"热门榜共{len(items)}只, 涨停{len(zt)}只")

# 过滤 创业板300/301/科创板688/北交8/4/920
eligible = []
for s in zt:
    code = s["code"]
    if re.match(r"^(300|301|688|8|4|920)", code):
        continue
    eligible.append(s)
print(f"过滤后(排除创/科/北交): {len(eligible)} 只")

results = []
for s in eligible:
    code_short = s["code"]
    code_full = f"{code_short}.SH" if code_short.startswith("6") else f"{code_short}.SZ"
    pct = float(s.get("pct_chg", 0))
    
    try:
        f, _ = score_fundamental(code_full)
        t, _ = score_technical(code_full)
        ff, _ = score_fundflow(code_full)
        st, _ = score_sentiment(code_full)
        tm, _ = score_shortterm(code_full)
    except Exception as e:
        print(f"  {s['name']} 评分异常: {e}")
        continue
    
    scores = {"f": f, "t": t, "ff": ff, "st": st, "tm": tm}
    top3 = sum(sorted(scores.values(), reverse=True)[:3]) / 3
    
    results.append({
        "name": s["name"], "code": code_full, "pct": pct,
        "scores": scores, "top3": round(top3, 1)
    })

results.sort(key=lambda x: x["top3"], reverse=True)

# 展示结果
print(f"\n评分完成: {len(results)} 只")
header = "{:8s} {:>5s} {:>5s} {:>5s} {:>5s} {:>5s} {:>5s} {:>5s}".format(
    "名称", "综合", "基本", "技术", "资金", "情绪", "短线", "涨幅")
print(header)
print("-" * 55)
for r in results:
    s = r["scores"]
    line = "{:8s} {:5.1f} {:5.1f} {:5.1f} {:5.1f} {:5.1f} {:5.1f} {:5.1f}%".format(
        r["name"], r["top3"], s["f"], s["t"], s["ff"], s["st"], s["tm"], r["pct"])
    print(line)

# 高确信过滤
sep = "=" * 60
print(f"\n{sep}")
print("高确信推送 (情绪>=35 + 资金>=35 + 总分>=40)")
print(sep)
pass_count = 0
for r in results:
    s = r["scores"]
    if s["st"] >= 35 and s["ff"] >= 35 and r["top3"] >= 40:
        print(f'  OK {r["name"]:8s} {r["top3"]:5.1f} 情绪{s["st"]:3.0f} 资金{s["ff"]:3.0f}')
        pass_count += 1
    else:
        reasons = []
        if s["st"] < 35:
            reasons.append("情绪" + str(int(s["st"])))
        if s["ff"] < 35:
            reasons.append("资金" + str(int(s["ff"])))
        if r["top3"] < 40:
            reasons.append("总分" + str(r["top3"]))
        print(f'  XX {r["name"]:8s} {r["top3"]:5.1f}  {" ".join(reasons)}')

print(f"\n结论: {len(results)} 只涨停股中, {pass_count} 只能推送")

# 宽松版
print(f"\n{sep}")
print("午盘宽松 (情绪>=25 + 总分>=35)")
print(sep)
loose_count = 0
for r in results:
    s = r["scores"]
    if s["st"] >= 25 and r["top3"] >= 35:
        print(f'  OK {r["name"]:8s} {r["top3"]:5.1f} 情绪{s["st"]:3.0f} 资金{s["ff"]:3.0f}')
        loose_count += 1
    else:
        reasons = []
        if s["st"] < 25:
            reasons.append("情绪" + str(int(s["st"])))
        if r["top3"] < 35:
            reasons.append("总分" + str(r["top3"]))
        print(f'  XX {r["name"]:8s} {r["top3"]:5.1f}  {" ".join(reasons)}')

print(f"\n结论: {loose_count} 只能推送")
