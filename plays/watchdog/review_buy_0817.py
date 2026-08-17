#!/usr/bin/env python3
"""0817 买入策略+时机复盘（扫描池视角）。"""
import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, "/root/maneki-agent")
from scripts.ths_client import get_ths_client

ROOT = Path("/root/maneki-agent")
close_map = json.load(open("/tmp/close_20260817.json"))

trades = json.load(open(ROOT / "plays/trading/data/reports/20260817.json"))
buys = [x for x in trades if x.get("direction") == "买入"]

# THS 分时：每只买入票的当日 high/low/买入时刻位置
ths = get_ths_client()
print("=" * 70)
print("一、买入质量（买入价 vs 收盘 / 当日高低位置）")
print("=" * 70)
rows = []
for b in sorted(buys, key=lambda x: x["time"]):
    code = b["code"].split(".")[0]
    px = float(b["price"])
    close = close_map.get(b["code"])
    close_pct = close_map.get(b["code"], (None, None))[1] if close_map.get(b["code"]) else None
    # 分时
    r = ths.get_index_intraday(code)
    hi = lo = None
    pos_hi = None
    if r and r.get("points"):
        pts = [p for _, p in r["points"]]
        hi, lo = max(pts), min(pts)
        if hi > lo:
            pos_hi = (px - lo) / (hi - lo) * 100  # 0=最低 100=最高
    pnl_close = (close[0] / px - 1) * 100 if close else None
    rows.append((b["time"], b["code"], b["name"], px, pnl_close, close_pct, hi, lo, pos_hi))
    print(f"{b['time']} {b['name']:6s} 买{px:7.2f} "
          f"收盘{close[0]:7.2f} 买后{'+' if pnl_close and pnl_close>0 else ''}{pnl_close:+.2f}% "
          f"当日高{hi:.2f} 低{lo:.2f} 买在高低区间{pos_hi:.0f}%" if all(v is not None for v in (pnl_close, hi)) else f"{b['time']} {b['name']} 数据缺")

# 汇总
pnls = [r[4] for r in rows if r[4] is not None]
win = [p for p in pnls if p > 0]
print(f"\n买入后到收盘: 盈利 {len(win)}/{len(pnls)} 胜率 {len(win)/len(pnls)*100:.0f}% "
      f"平均 {sum(pnls)/len(pnls):+.2f}% 合计盈亏 {sum(pnl/100*px*200 for pnl,px in [(r[4],r[3]) for r in rows if r[4] is not None]):+.0f}元")
print(f"买入时机位置(高低区间%): 平均 {sum(r[8] for r in rows if r[8] is not None)/len([r for r in rows if r[8] is not None]):.0f}% "
      f"(>80%=买在高位区, <40%=买在低位区)")
print(f"买在高位区(>80%) {len([r for r in rows if r[8] is not None and r[8]>80])} 只, "
      f"买在低位区(<40%) {len([r for r in rows if r[8] is not None and r[8]<40])} 只")
