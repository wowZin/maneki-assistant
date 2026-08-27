#!/usr/bin/env python3
"""路1 离线回测：主力吸筹信号 fund_accumulate_confirm（影子模式数据积累后跑）。

输入：plays/limit_up/data/l2_bigorder/{date}.jsonl（ws_daemon 落盘，含 last/high/big_net/super_net）
信号（镜像 docs/entry-redesign.md，事前口径，无未来函数）：
  1. 主力持续净流入：近 N 轮 super_net+big_net 累计>0 且 ≥K 轮单轮为正
  2. 不追顶：现价 ≤ 当日最高×(1-TOP_TOL) 且 当日涨幅 ≤ MAX_PCT
  3. 主动买占优：big_buy > big_sell
前瞻：30min 后价格（同文件 last 序列）+ 次日 open/close（tushare daily）

用法：
  python3 plays/watchdog/backtest_fund_accumulate.py            # 全部日期
  python3 plays/watchdog/backtest_fund_accumulate.py 20260826  # 指定日期
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path("/root/maneki-agent")
BIGORDER_DIR = ROOT / "plays/limit_up/data/l2_bigorder"

# 信号参数（默认值，可被回测标定）
N = 5          # 近 N 轮窗口
K = 3          # 至少 K 轮单轮净额为正
TOP_TOL = 0.02   # 不追顶：现价距当日高点 ≥2%
MAX_PCT = 5.0  # 当日涨幅上限（%）

# 回测参数扫描（标定用）
SWEEP = {
    "基线": dict(N=5, K=3, top_tol=0.02, max_pct=5.0),
    "K收紧2": dict(N=5, K=2, top_tol=0.02, max_pct=5.0),
    "K收紧4": dict(N=5, K=4, top_tol=0.02, max_pct=5.0),
    "不追顶1%": dict(N=5, K=3, top_tol=0.01, max_pct=5.0),
    "不追顶3%": dict(N=5, K=3, top_tol=0.03, max_pct=5.0),
}


def _to_min(t: str) -> float:
    h, m, s = t.split(":")
    return int(h) * 60 + int(m) + int(s) / 60


def fund_accumulate_trigger(hist: list[dict], p: dict) -> bool:
    """hist: 最近若干轮记录（含当前，末尾最新），每轮有 last/high/big_net/super_net/big_buy/big_sell。

    返回是否触发。事前口径：只用 hist 内的数据。
    """
    if len(hist) < p["N"] + 1:
        return False
    win = hist[-(p["N"] + 1):]
    # 1. 主力持续净流入（差分：累计值相邻相减=本轮增量，2026-08-27 修正累计值直加 bug）
    total = 0.0
    pos_rounds = 0
    for i in range(1, len(win)):
        net_prev = float(win[i - 1].get("super_net_amount") or 0) + float(win[i - 1].get("big_net_amount") or 0)
        net_cur = float(win[i].get("super_net_amount") or 0) + float(win[i].get("big_net_amount") or 0)
        delta = net_cur - net_prev
        total += delta
        if delta > 0:
            pos_rounds += 1
    if total <= 0 or pos_rounds < p["K"]:
        return False
    # 2. 不追顶
    last = float(hist[-1].get("last") or 0)
    day_high = max(float(r.get("high") or 0) for r in hist)
    if last <= 0 or day_high <= 0:
        return False
    if last > day_high * (1 - p["top_tol"]):
        return False
    # 当日涨幅上限（pre_close 需外部注入，这里若缺 pre_close 则跳过此条件）
    pc = float(hist[-1].get("pre_close") or 0)
    if pc > 0:
        if (last / pc - 1) * 100 > p["max_pct"]:
            return False
    # 3. 主动买占优
    if float(hist[-1].get("big_buy_amount") or 0) <= float(hist[-1].get("big_sell_amount") or 0):
        return False
    return True


def load_day(date: str) -> dict[str, list[dict]]:
    f = BIGORDER_DIR / f"{date}.jsonl"
    if not f.exists():
        return {}
    by_code: dict[str, list[dict]] = {}
    with open(f) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            by_code.setdefault(r.get("code", ""), []).append(r)
    return by_code


def forward_30min(rows: list[dict], i: int) -> float | None:
    """返回 30min 后价格涨跌幅 %，无数据返回 None。"""
    if i >= len(rows) - 1:
        return None
    tgt = _to_min(rows[i]["ts"]) + 30
    cur = float(rows[i].get("last") or 0)
    if cur <= 0:
        return None
    for r in rows[i + 1:]:
        if _to_min(r["ts"]) >= tgt:
            fut = float(r.get("last") or 0)
            if fut > 0:
                return (fut / cur - 1) * 100
            return None
    return None


def main():
    dates = sys.argv[1:] or [f.name[:8] for f in sorted(BIGORDER_DIR.glob("*.jsonl"))]
    # 只处理有价格字段的日期（last/high 出现 = 影子模式已生效）
    agg = {name: {"n": 0, "ret30": []} for name in SWEEP}
    for d in dates:
        by_code = load_day(d)
        if not by_code:
            print(f"[skip] {d} 无数据")
            continue
        has_price = any(r.get("last") for rows in by_code.values() for r in rows)
        if not has_price:
            print(f"[skip] {d} 无价格字段（ws_daemon 未重启，影子字段未生效）")
            continue
        for code, rows in by_code.items():
            rows.sort(key=lambda r: r["ts"])
            for name, p in SWEEP.items():
                for i in range(p["N"], len(rows)):
                    if not fund_accumulate_trigger(rows[:i + 1], p):
                        continue
                    r30 = forward_30min(rows, i)
                    if r30 is not None:
                        agg[name]["n"] += 1
                        agg[name]["ret30"].append(r30)
    print(f"\n=== 主力吸筹信号回测（30min 前瞻）===")
    for name, a in agg.items():
        if a["n"] == 0:
            print(f"  {name}: 触发 0")
            continue
        arr = np.array(a["ret30"])
        print(f"  {name}: 触发 {a['n']} | 30min均 {arr.mean():+.3f}% 胜率 {(arr>0).mean()*100:.1f}%")
    print("\n注：次日收益需 tushare daily 次日数据，数据积累后单独算。")


if __name__ == "__main__":
    main()
