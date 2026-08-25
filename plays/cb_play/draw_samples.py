#!/usr/bin/env python3
"""画分钟图样本，供 LLM 视觉判断「真拉升 vs 假拉升」。

从 snapshot 数据里，挑「买后涨(真拉升)」和「买后跌(假拉升)」的典型样本，
画分时价格线 + 成交量柱 + 关键位置线（昨收/涨停价），保存 PNG。

图设计（贴合"决策要不要买"的视角）：
  - 上半：分时价格线（昨收基准线 + 涨停价线）
  - 下半：每分钟成交量柱（红涨绿跌）
  - 标题：代码+日期（不暴露"买后涨跌"标签，避免 LLM 作弊）
"""
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import os, sys
sys.path.insert(0, '/root/maneki-agent')

SNAP_DIR = '/root/maneki-agent/plays/limit_up/data/snapshot_log'
OUT_DIR = '/root/maneki-agent/plays/cb_play/vision_samples'
os.makedirs(OUT_DIR, exist_ok=True)

# 中文字体
plt.rcParams['font.sans-serif'] = ['DejaVu Sans', 'SimHei', 'WenQuanYi Micro Hei']
plt.rcParams['axes.unicode_minus'] = False


def load_snap(day):
    f = f'{SNAP_DIR}/{day}.parquet'
    if os.path.exists(f):
        return pd.read_parquet(f)
    return None


def draw(code, day, idx, label):
    """画单只票的分钟图，label 用于文件名（真/假）。"""
    df = load_snap(day)
    if df is None:
        return
    g = df[df['code'] == code].sort_values('ts')
    if g.empty or len(g) < 15:
        return
    prices = g['price'].astype(float).tolist()
    outers = g['outer_vol'].astype(float).tolist()
    inners = g['inner_vol'].astype(float).tolist()
    pcts = g['pct_chg'].astype(float).tolist()
    # 昨收
    prev_close = prices[0] / (1 + pcts[0] / 100) if pcts[0] > -99 else prices[0]
    limit_price = round(prev_close * 1.1, 2)

    # 每分钟成交量
    vol = []
    for i in range(len(prices)):
        tot = (outers[i] or 0) + (inners[i] or 0)
        prev = (outers[i-1] or 0) + (inners[i-1] or 0) if i > 0 else 0
        vol.append(max(tot - prev, 0))

    # 时间轴
    import datetime as dt
    times = []
    for t in g['ts']:
        try:
            times.append(dt.datetime.strptime(t, '%H:%M:%S'))
        except Exception:
            times.append(dt.datetime(2026, 8, 1, 9, 30))

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(8, 7), sharex=True,
                                   gridspec_kw={'height_ratios': [3, 1]})
    # 价格线
    ax1.plot(times, prices, color='#1f77b4', linewidth=1.5, label='price')
    ax1.axhline(prev_close, color='gray', linestyle='--', linewidth=0.8, label='prev_close')
    ax1.axhline(limit_price, color='red', linestyle='--', linewidth=0.8, label='limit_up')
    ax1.set_ylabel('Price')
    ax1.legend(loc='upper left', fontsize=7)
    ax1.set_title(f'{code} {day}', fontsize=11)
    ax1.grid(alpha=0.3)

    # 成交量柱（红涨绿跌）
    colors = ['#d62728' if prices[i] >= prices[i-1] else '#2ca02c' for i in range(len(prices))]
    ax2.bar(times, vol, color=colors, width=0.0012)
    ax2.set_ylabel('Vol')
    ax2.grid(alpha=0.3)

    ax1.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
    plt.tight_layout()
    out = f'{OUT_DIR}/{label}_{day}_{code.replace(".", "_")}.png'
    plt.savefig(out, dpi=110)
    plt.close()
    print(f'  保存 {out} (n={len(prices)} 帧)')
    return out


# 从之前的诊断结果，手挑已知「真拉升」和「假拉升」样本
# 真拉升（买后涨）：这些是事后确认买后 30min/收盘涨的票
# 假拉升（买后跌）：事后确认买后跌的票
# 样本来自之前 _diag_pullback.py 的输出和涨停明细

print('画「真拉升」样本（买后涨）:')
# 这些是涨停票里的盘中强势票（正股当天涨停，转债/股票买后涨）
samples_true = [
    ('20260812', '603466.SH'),  # 风语筑涨停，转债+3.7%
    ('20260819', '000723.SZ'),  # 美锦能源涨停
    ('20260817', '603916.SH'),  # 苏博特涨停
]
for day, code in samples_true:
    draw(code, day, 0, 'true')

print('画「假拉升」样本（买后跌）:')
samples_false = [
    ('20260731', '603290.SH'),  # 斯达半导 转债-6.7%（假拉升）
    ('20260731', '002475.SZ'),  # 立讯精密 转债-3.6%
    ('20260731', '603306.SH'),  # 华懋科技 -3.2%
]
for day, code in samples_false:
    draw(code, day, 0, 'false')

print('\n完成，输出目录', OUT_DIR)
