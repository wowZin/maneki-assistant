#!/usr/bin/env python3
"""回测：开盘买入 model_score 高分（预测涨停）→ 次日开盘卖（用户 0821 要求）"""
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

for seg in (40, 50, 60, 70):
    tr = []
    for i, d in enumerate(days):
        nd = days[i + 1] if i + 1 < len(days) else "20260821"
        t = json.load(open(f"wiki/raw/limit-up/analysis/{d}.json"))
        d_o, n_o = ohlc.get(d, {}), ohlc.get(nd, {})
        for r in t:
            ms = r.get("model_score") or 0
            if ms < seg:
                continue
            c = r["code"]
            if c not in d_o or c not in n_o:
                continue
            pre = d_o[c]["pre"] or d_o[c]["o"]
            gap = d_o[c]["o"] / pre - 1 if pre else 0
            if gap >= 0.095:
                continue  # 开盘已涨停/接近涨停，买不进
            pnl = (n_o[c]["o"] / d_o[c]["o"] - 1) * 100
            tr.append({"day": d, "code": c, "panel": round(ms, 1), "pnl": pnl})
    if not tr:
        continue
    n = len(tr)
    avg = sum(x["pnl"] for x in tr) / n
    wins = sum(1 for x in tr if x["pnl"] > 0)
    lim = sum(1 for x in tr if x["pnl"] >= 9.5)
    bad3 = sum(1 for x in tr if x["pnl"] <= -3)
    print(f"model≥{seg}: {n}笔 均{avg:+.2f}% 胜率{wins/n*100:.0f}% "
          f"吃到涨停{lim} 亏>3%:{bad3}笔")
    for d in days:
        sub = [x for x in tr if x["day"] == d]
        if sub:
            print(f"    {d}: {len(sub)}笔 均{sum(x['pnl'] for x in sub)/len(sub):+.2f}% "
                  f"胜率{sum(1 for x in sub if x['pnl']>0)/len(sub)*100:.0f}%")
