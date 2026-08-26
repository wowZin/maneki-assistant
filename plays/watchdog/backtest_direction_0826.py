#!/usr/bin/env python3
"""路2 粗验证：内外盘方向（主动买卖）对次日/30min 收益的预测力。

用 snapshot_log（surge 候选，23 天，07-27~08-26）回放：
  信号 = 近 N 轮「外盘增量 > 内盘增量」的持续占优（主动买占优 = 主力方向的弱代理）
  前瞻 = 30min 后价格涨跌（事前信号 → 事后收益，无未来函数）

对比：信号触发样本 vs 全体候选样本 的 30min 收益/胜率。
"""
from __future__ import annotations
import numpy as np
import pandas as pd
from pathlib import Path

SNAP_DIR = Path("/root/maneki-agent/plays/limit_up/data/snapshot_log")

N = 5       # 近 N 轮窗口
K = 3       # 至少 K 轮外盘占优
MIN_PCT = 1.0  # 触发时当日涨幅下限（避免横盘零波动票）


def _to_min(t: str) -> float:
    h, m, s = t.split(":")
    return int(h) * 60 + int(m) + int(s) / 60


def load_all():
    frames = []
    for f in sorted(SNAP_DIR.glob("*.parquet")):
        try:
            df = pd.read_parquet(f)
            df["date"] = f.name[:8]
            frames.append(df)
        except Exception:
            continue
    return pd.concat(frames, ignore_index=True)


def main():
    df = load_all()
    print(f"snapshot_log 总行数 {len(df)}  日期数 {df['date'].nunique()}  股票数 {df['code'].nunique()}")

    sig_ret = []   # 信号触发样本 30min 收益
    base_ret = []  # 全体候选 30min 收益（基准）

    for code, g in df.sort_values("ts").groupby("code"):
        g = g.sort_values(["date", "ts"])
        ts = g["ts"].tolist()
        px = [float(x) for x in g["price"].tolist()]
        inner = g["inner_vol"].fillna(0).astype(float).tolist()
        outer = g["outer_vol"].fillna(0).astype(float).tolist()
        pct = g["pct_chg"].fillna(0).astype(float).tolist()
        if len(px) < N + 2:
            continue

        # 每分钟内外盘增量（累计值差分）
        d_outer = [max(0.0, outer[i] - outer[i - 1]) for i in range(len(outer))]
        d_inner = [max(0.0, inner[i] - inner[i - 1]) for i in range(len(inner))]
        d_outer[0] = d_inner[0] = 0.0

        for i in range(N, len(px)):
            # 事前信号：近 N 轮外盘占优
            win_outer = d_outer[i - N + 1:i + 1]
            win_inner = d_inner[i - N + 1:i + 1]
            dom = sum(1 for o, n in zip(win_outer, win_inner) if o > n)
            triggered = dom >= K and pct[i] >= MIN_PCT

            # 事后：30min 收益
            tgt = _to_min(ts[i]) + 30
            fut = None
            for j in range(i + 1, len(px)):
                if _to_min(ts[j]) >= tgt:
                    fut = px[j]
                    break
            if fut is None or px[i] <= 0:
                continue
            r = (fut / px[i] - 1) * 100
            if triggered:
                sig_ret.append(r)
            else:
                base_ret.append(r)

    sig = np.array(sig_ret)
    base = np.array(base_ret)
    print(f"\n信号(外盘{N}轮{K}轮占优)触发样本: {len(sig)}")
    print(f"  30min均收益 {sig.mean():+.3f}%  胜率 {(sig>0).mean()*100:.1f}%")
    print(f"基准(全体候选)样本: {len(base)}")
    print(f"  30min均收益 {base.mean():+.3f}%  胜率 {(base>0).mean()*100:.1f}%")
    print(f"\n信号 - 基准: 均收益差 {sig.mean()-base.mean():+.3f}%  胜率差 {(sig>0).mean()-(base>0).mean()*1:.1%}")


if __name__ == "__main__":
    main()
