#!/usr/bin/env python3
"""确认器参数敏感性回放（2026-08-17，转化率优化第二步）。

用 snapshot_log（surge 每 60s 全候选快照）回放买入确认器状态机，
扫参数组合，量化"触发数提升 vs 触发后收益/胜率"，只上触发率↑且
胜率不降的参数（用户定规矩：策略改动先回测对比再部署）。

用法:
  python3 plays/watchdog/backtest_confirm_params.py [20260814 20260817]

逻辑对齐 plays/watchdog/confirm.py：
  - 触发: 当前价≥前10轮高×0.995 + 拉升≥rise_min + 跨≥span轮 + 峰≥peaks + 窗口放量×vol_mult
  - 站稳: 触发后连续 stand 轮 last≥base×0.995 → 买入
  - 收益: 买入 ts +30min 的价格涨跌幅（无后续数据则跳过）
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path("/root/maneki-agent")
SNAP_DIR = ROOT / "plays/limit_up/data/snapshot_log"

HI_WINDOW = 10
HI_TOL = 0.995
STAND_TOL = 0.995

# (名称, dict(参数)) —— 基线 + 逐步放宽
COMBOS = {
    "基线(现生产)": dict(rise_min=2.0, span=4, peaks=2, vol_mult=1.5, stand=2),
    "C:拉升门槛1.5": dict(rise_min=1.5, span=4, peaks=2, vol_mult=1.5, stand=2),
    "D:1.5+触发时仍在涨": dict(rise_min=1.5, span=4, peaks=2, vol_mult=1.5, stand=2,
                              rising_only=True),
}


def trigger(px: list[float], vol: list[float], p: dict) -> bool:
    """对齐 confirm.trend_up_trigger（参数化）"""
    if len(px) < HI_WINDOW + 1 or len(vol) < HI_WINDOW + 1:
        return False
    last = px[-1]
    # 2026-08-17 变体D：触发时"仍在涨"——当前轮 ≥ 上一轮×容差
    #（拦"拉高后横盘"：603284 13:19 急拉45.95 → 13:20 横盘45.85 仍被
    #  0.5% 容差算"创新高"放行，触发轮本身就是滞涨位）
    if p.get("rising_only") and len(px) >= 2:
        if last < px[-2] * 0.998:
            return False
    hi = max(px[-HI_WINDOW - 1:-1])
    if last < hi * HI_TOL:
        return False
    _lo = min(px[-HI_WINDOW - 1:-1])
    _rise = (last / _lo - 1) * 100 if _lo > 0 else 0
    if _rise < p["rise_min"]:
        return False
    _seg = px[-HI_WINDOW - 1:-1]
    _lo_idx = min(range(len(_seg)), key=lambda i: _seg[i])
    _span = len(_seg) - 1 - _lo_idx
    if _span < p["span"]:
        return False
    _peaks = 0
    _h = _seg[0]
    for _p in _seg:
        if _p > _h:
            _peaks += 1
            _h = _p
    if _peaks < p["peaks"]:
        return False
    for j in range(len(vol) - HI_WINDOW, len(vol)):
        v5 = vol[max(0, j - 5):j]
        v5 = [v for v in v5 if v > 0]
        if len(v5) >= 3 and vol[j] > sum(v5) / len(v5) * p["vol_mult"]:
            return True
    return False


def replay(ts: list[str], px: list[float], vol: list[float], p: dict) -> list[tuple[int, float, str]]:
    """状态机回放：返回 [(买入轮idx, 买入价, 买入ts)]"""
    buys: list[tuple[int, float, str]] = []
    base = 0.0
    cnt = 0
    for i in range(HI_WINDOW + 1, len(px)):
        if base <= 0:
            if trigger(px[:i + 1], vol[:i + 1], p):
                base = px[i]
                cnt = 0
        else:
            if px[i] >= base * STAND_TOL:
                cnt += 1
                if cnt >= p["stand"]:
                    buys.append((i, px[i], ts[i]))
                    base = 0.0
                    cnt = 0
            else:
                base = 0.0
                cnt = 0
    return buys


def main() -> None:
    dates = sys.argv[1:] or ["20260814", "20260817"]
    for td in dates:
        f = SNAP_DIR / f"{td}.parquet"
        if not f.exists():
            print(f"[skip] {td} 无 snapshot_log")
            continue
        df = pd.read_parquet(f)
        print(f"\n===== {td} snapshot_log {len(df)} 行 =====")
        # 每只票：ts/price/分钟量(inner+outer 差分近似)
        rows = []
        for code, g in df.sort_values("ts").groupby("code"):
            ts = g["ts"].tolist()
            px = [float(x) for x in g["price"].tolist()]
            inner = g["inner_vol"].fillna(0).astype(float).tolist()
            outer = g["outer_vol"].fillna(0).astype(float).tolist()
            vol = [max(0.0, (inner[i] + outer[i]) - (inner[i - 1] + outer[i - 1]))
                   for i in range(len(px))]
            vol[0] = 0.0
            rows.append((code, ts, px, vol))
        print(f"股票数={len(rows)}")
        # 时间索引（用于 30min 收益）
        all_ts = sorted(set(t for _, ts, _, _ in rows for t in ts))
        for name, p in COMBOS.items():
            n_buy = 0
            pnl30 = []
            for code, ts, px, vol in rows:
                for i, price, bt in replay(ts, px, vol, p):
                    n_buy += 1
                    # 30min 后价格
                    base_min = _to_min(bt)
                    tgt = base_min + 30
                    fut = None
                    for t in ts[i + 1:]:
                        if _to_min(t) >= tgt:
                            fut = px[ts.index(t)]
                            break
                    if fut:
                        pnl30.append((fut / price - 1) * 100)
            if n_buy == 0:
                print(f"  {name}: 触发0")
                continue
            a = np.array(pnl30) if pnl30 else np.array([])
            if len(a) == 0:
                print(f"  {name}: 触发{n_buy} 无30min样本")
                continue
            print(f"  {name}: 触发{n_buy} | 30min均{a.mean():+.2f}% 胜率{(a>0).mean()*100:.0f}% "
                  f"(样本{len(a)})")


def _to_min(t: str) -> float:
    h, m, s = t.split(":")
    return int(h) * 60 + int(m) + int(s) / 60


if __name__ == "__main__":
    main()
