#!/usr/bin/env python3
"""细分段回测：model_score 每5分一档，开盘买入→次日开盘卖，找最赚钱区间"""
import json
import requests
from pathlib import Path

env = {}
for line in Path(".env").read_text().splitlines():
    if line.startswith("TUSHARE_TOKEN="):
        env["TUSHARE_TOKEN"] = line.split("=", 1)[1].strip()
tok = env["TUSHARE_TOKEN"]

days = ["20260814", "20260817", "20260818", "20260819", "20260820"]
ohlc = {}
for d in days + ["20260821"]:
    r = requests.post("http://api.tushare.pro", json={
        "api_name": "daily", "token": tok, "params": {"trade_date": d},
        "fields": "ts_code,open,close,pct_chg,pre_close", "limit": 6000}).json()
    ohlc[d] = {i[0]: {"o": i[1], "c": i[2], "pct": i[3], "pre": i[4]}
               for i in r["data"]["items"]}

bins = [(15, 20), (20, 25), (25, 30), (30, 35), (35, 40), (40, 45), (45, 50),
        (50, 55), (55, 60), (60, 70), (70, 200)]
agg = {b: {"n": 0, "pnl": [], "lim": 0, "bad3": 0, "gap": []} for b in bins}

for i, d in enumerate(days):
    nd = days[i + 1] if i + 1 < len(days) else "20260821"
    t = json.load(open(f"wiki/raw/limit-up/analysis/{d}.json"))
    d_o, n_o = ohlc.get(d, {}), ohlc.get(nd, {})
    for r in t:
        ms = r.get("model_score") or 0
        c = r["code"]
        if c not in d_o or c not in n_o:
            continue
        pre = d_o[c]["pre"] or d_o[c]["o"]
        gap = d_o[c]["o"] / pre - 1 if pre else 0
        if gap >= 0.095:
            continue
        pnl = (n_o[c]["o"] / d_o[c]["o"] - 1) * 100
        for lo, hi in bins:
            if lo <= ms < hi:
                a = agg[(lo, hi)]
                a["n"] += 1
                a["pnl"].append(pnl)
                a["gap"].append(gap * 100)
                if pnl >= 9.5:
                    a["lim"] += 1
                if pnl <= -3:
                    a["bad3"] += 1
                break

print("=== model_score 分档：开盘买→次日开盘卖（5天全量）===")
print(f"{'区间':8s} {'笔数':5s} {'均收益':8s} {'胜率':6s} {'吃到涨停':7s} {'亏>3%':6s} {'开盘高开':8s}")
best = None
for (lo, hi), a in sorted(agg.items()):
    if a["n"] == 0:
        continue
    avg = sum(a["pnl"]) / a["n"]
    wins = sum(1 for x in a["pnl"] if x > 0) / a["n"] * 100
    gavg = sum(a["gap"]) / a["n"]
    print(f"{lo}-{hi:<5d} {a['n']:<5d} {avg:+7.2f}% {wins:5.0f}% "
          f"{a['lim']:<7d} {a['bad3']:<6d} {gavg:+6.1f}%")
    if best is None or avg > best[1]:
        best = ((lo, hi), avg, a["n"], wins)
print(f"\n最佳区间: model {best[0][0]}-{best[0][1]} 均{best[1]:+.2f}% 胜率{best[3]:.0f}% ({best[2]}笔)")
