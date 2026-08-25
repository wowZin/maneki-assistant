#!/usr/bin/env python3
"""构造「真拉升/假拉升」带标签样本 + 文本化描述，供 LLM 盲测。

样本定义：
  拉升事件 = 某时刻价格从最近 20 分钟低点涨 ≥ 3%（潜在追涨买点）
  真拉升(标签 buy) = 该时点买入后 30 分钟涨 > +1.0%
  假拉升(标签 skip) = 该时点买入后 30 分钟跌 < -1.0%
  （涨跌在 ±1% 之间的模糊样本丢弃）

文本描述 = 模拟盘中决策视角，给出：当前价/涨幅/距涨停、今日走势轨迹、
拉升形态、量比变化、内外盘，不含标签。

输出：wiki/raw/cb-play/vision_samples/llm_test_samples.json
"""
import pandas as pd
import numpy as np
import json, os, sys, random
sys.path.insert(0, '/root/maneki-agent')

SNAP_DIR = '/root/maneki-agent/plays/limit_up/data/snapshot_log'
OUT = '/root/maneki-agent/wiki/raw/cb-play/llm_test_samples.json'

DAYS = ['20260811','20260812','20260813','20260814','20260817',
        '20260818','20260819','20260820','20260821','20260824']

RISE_MIN = 3.0    # 拉升阈值
RET_UP = 1.0      # 真拉升：买后30min涨超1%
RET_DOWN = -1.0   # 假拉升：买后30min跌超1%


def extract_event(df_code):
    """扫描单票分钟序列，找拉升事件 + 标签。"""
    g = df_code.sort_values('ts').reset_index(drop=True)
    prices = g['price'].astype(float).tolist()
    pcts = g['pct_chg'].astype(float).tolist()
    vrs = g['vol_ratio'].astype(float).tolist()
    outers = g['outer_vol'].astype(float).tolist()
    inners = g['inner_vol'].astype(float).tolist()
    ts = g['ts'].tolist()
    n = len(prices)
    if n < 50:
        return []
    prev_close = prices[0] / (1 + pcts[0] / 100) if pcts[0] > -99 else prices[0]
    limit_price = round(prev_close * 1.1, 2)
    events = []
    last_event_idx = -10  # 避免同一波重复
    for i in range(20, n - 30):
        low20 = min(prices[i-20:i])
        rise = (prices[i] / low20 - 1) * 100 if low20 > 0 else 0
        if rise < RISE_MIN:
            continue
        if i - last_event_idx < 10:
            continue
        # 买后 30min 收益
        ret_30 = (prices[i+30] / prices[i] - 1) * 100
        if RET_UP <= ret_30:
            label = 'buy'
        elif ret_30 <= RET_DOWN:
            label = 'skip'
        else:
            continue
        last_event_idx = i
        events.append({
            'idx': i, 'label': label,
            'price': prices[i], 'pct': pcts[i],
            'limit_price': limit_price, 'prev_close': prev_close,
            'ts': ts[i],
        })
    return events


def build_text(g, ev):
    """生成盘中决策文本描述（不含标签）。"""
    prices = g['price'].astype(float).tolist()
    pcts = g['pct_chg'].astype(float).tolist()
    vrs = g['vol_ratio'].astype(float).tolist()
    outers = g['outer_vol'].astype(float).tolist()
    inners = g['inner_vol'].astype(float).tolist()
    ts = g['ts'].tolist()
    i = ev['idx']
    # 今日关键轨迹
    day_high_idx = max(range(i+1), key=lambda k: prices[k])
    day_high = prices[day_high_idx]
    open_pct = pcts[0]
    cur_pct = pcts[i]
    # 距涨停
    gap = (ev['limit_price'] / ev['price'] - 1) * 100
    # 从当日高点回落
    off_high = (prices[i] / day_high - 1) * 100 if day_high > 0 else 0
    # 拉升斜率：近20分钟低点到当前
    low20 = min(prices[i-20:i])
    low20_idx = i - 20 + prices[i-20:i].index(low20)
    rise_pct = (prices[i] / low20 - 1) * 100
    rise_mins = i - low20_idx
    # 量比变化
    vr_early = np.mean(vrs[max(0,i-20):max(0,i-15)])
    vr_now = vrs[i]
    vr_trend = '放量' if vr_now > vr_early * 1.2 else ('缩量' if vr_now < vr_early * 0.8 else '平量')
    # 内外盘比
    total_flow = (outers[i] - outers[max(0,i-10)]) + (inners[i] - inners[max(0,i-10)])
    if total_flow > 0:
        buy_ratio = (outers[i] - outers[max(0,i-10)]) / total_flow * 100
    else:
        buy_ratio = 50
    # 波形：20分钟内创新高次数
    seg = prices[i-20:i+1]
    peaks = 0
    h = seg[0]
    for p in seg:
        if p > h:
            peaks += 1
            h = p
    shape = '单波冲高' if peaks <= 1 else f'{peaks}波推进'

    text = (
        f"股票 {ev.get('code','')}，盘中 {ts[i]}。\n"
        f"当前价 {prices[i]:.2f}，涨幅 {cur_pct:+.1f}%（开盘 {open_pct:+.1f}%），距涨停价还有 {gap:.1f}%。\n"
        f"近20分钟从低点拉升 {rise_pct:.1f}%（用时约 {rise_mins} 分钟），形态{shape}。\n"
        f"今日最高 {day_high:.2f}（+{(day_high/ev['prev_close']-1)*100:.1f}%），当前从高点回落 {off_high:.1f}%。\n"
        f"量比 {vr_now:.1f}（近20分钟趋势：{vr_trend}）。\n"
        f"近10分钟主动买盘占比 {buy_ratio:.0f}%（外盘/内外盘合计）。\n"
        f"问题：当前时点值不值得追涨买入？只回答「买」或「不买」两个词之一，不要解释理由。"
    )
    return text


def main():
    samples = []
    for d in DAYS:
        f = f'{SNAP_DIR}/{d}.parquet'
        if not os.path.exists(f):
            continue
        df = pd.read_parquet(f)
        for code, g in df.groupby('code'):
            g = g.sort_values('ts').reset_index(drop=True)
            events = extract_event(g)
            for ev in events:
                ev['code'] = code
                ev['day'] = d
                ev['text'] = build_text(g, ev)
                samples.append(ev)

    n_buy = sum(1 for s in samples if s['label'] == 'buy')
    n_skip = sum(1 for s in samples if s['label'] == 'skip')
    print(f'构造样本：真拉升(buy) {n_buy} 个，假拉升(skip) {n_skip} 个，共 {len(samples)} 个')

    # 平衡采样（各取 min，最多各 40）
    buys = [s for s in samples if s['label'] == 'buy']
    skips = [s for s in samples if s['label'] == 'skip']
    random.seed(42)
    k = min(40, len(buys), len(skips))
    balanced = random.sample(buys, k) + random.sample(skips, k)
    random.shuffle(balanced)

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, 'w') as fp:
        json.dump(balanced, fp, ensure_ascii=False, indent=2)
    print(f'平衡采样 {len(balanced)} 个（各 {k}），已写入 {OUT}')

    # 打印 2 个样本示例
    print('\n样例文本（1个buy + 1个skip）:')
    for s in balanced:
        if s['label'] == 'buy':
            print(f'\n--- [标签:buy] {s["code"]} {s["day"]} ---')
            print(s['text'])
            break
    for s in balanced:
        if s['label'] == 'skip':
            print(f'\n--- [标签:skip] {s["code"]} {s["day"]} ---')
            print(s['text'])
            break


if __name__ == '__main__':
    main()
