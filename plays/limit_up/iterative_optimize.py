#!/usr/bin/env python3
"""迭代优化 — 滑动窗口验证，持续改进直到命中率/胜率收敛"""

import json, sys, time, itertools
from collections import defaultdict
import numpy as np
sys.path.insert(0, '.')
sys.path.insert(0, 'scripts')


def load_data():
    with open('plays/limit_up/data/backtest/factor_data_v2.json') as f:
        return json.load(f)


def score_v1(r):
    """V1: 基准 — 简单线性证据加权"""
    s = 0
    s += r['max_step'] * 8
    if r['was_limit']: s += 12
    vr = r['vol_ratio']
    if 1.0 <= vr <= 2.5: s += 10
    elif 2.5 < vr <= 3.5: s += 3
    elif vr > 4.0: s -= 8
    s += min(r['prev_turnover'] * 0.8, 10)
    if r['mf_pct'] > 5: s -= 5
    elif r['mf_pct'] < -2: s += 4
    if r['cmv_yi'] < 50: s += 10
    elif r['cmv_yi'] < 100: s += 6
    if r['positive_5d'] >= 3: s += 5
    if r['vol_accel'] < 0.8: s += 4
    elif r['vol_accel'] > 2.0: s -= 4
    if 0.3 <= r['close_pos'] <= 0.8: s += 4
    if r.get('jv_turnover', 0) > 5: s += 6
    if r['amplitude'] > 8: s += 4
    return s


def score_v2(r):
    """V2: 加入主力流出+中单流入=吸筹逻辑"""
    s = score_v1(r)  # base
    # 主力流出 + 散户/中单流入 = 隐蔽吸筹
    if r['mf_pct'] < -3 and r.get('jv_small_net', 0) > 1000:
        s += 8
    if r.get('jv_mid_net', 0) > 2000:
        s += 6
    # 涨停基因强化
    if r['max_step'] >= 3:
        s += 10
    # 低量比（未爆发）+ 高换手（活跃）= 即将爆发
    if r['vol_ratio'] < 1.5 and r['turnover'] > 10:
        s += 6
    return s


def score_v3(r):
    """V3: 加入开盘形态 + 量价背离检测"""
    s = score_v2(r)
    # 下影线长 = 买方支撑 (日内形态)
    if r['lower_ratio'] > 0.5:
        s += 5
    # 上影线长 = 卖方压力 (负向)
    if r['upper_ratio'] > 1.5:
        s -= 6
    # 缩量上涨(量加速<1) + 涨幅>5% = 惜售
    if r['vol_accel'] < 0.7 and r['pct_chg'] > 5:
        s += 8
    # 放量滞涨(量加速>2 + 涨幅<3%) = 出货
    if r['vol_accel'] > 2.5 and r['pct_chg'] < 3:
        s -= 8
    # 振幅大(>10%) + 实体大(>0.6) = 趋势明确
    if r['amplitude'] > 10 and r['body_ratio'] > 0.6:
        s += 5
    return s


def evaluate(scorer, rows, top_k=10):
    """计算 Top-K 命中率和胜率"""
    for r in rows: r['_score'] = scorer(r)
    rows.sort(key=lambda x: x['_score'], reverse=True)
    top = rows[:top_k]
    hits = sum(1 for r in top if r['is_hit'])
    wins = sum(1 for r in top if r['pct_chg'] > 2)
    return hits / top_k, wins / top_k


def sliding_window_eval(scorer, all_rows, window=16, val_size=2, top_k=10):
    """滑动窗口验证：每次用 window 天训练，val_size 天验证"""
    dates = sorted(set(r['date'] for r in all_rows))
    results = []
    for i in range(window, len(dates) - val_size + 1, val_size):
        train_dates = set(dates[i-window:i])
        val_dates = set(dates[i:i+val_size])
        val = [r for r in all_rows if r['date'] in val_dates]
        if len(val) < top_k: continue
        hr, wr = evaluate(scorer, val, top_k)
        results.append({'window_end': dates[i-1], 'val': list(val_dates),
                        'hit_rate': hr, 'win_rate': wr, 'n_val': len(val)})
    return results


def main():
    rows = load_data()
    print(f"数据: {len(rows)}样本, {len(set(r['date'] for r in rows))}天")

    versions = [
        ("V1-基准线性", score_v1),
        ("V2-吸筹+涨停基因", score_v2),
        ("V3-形态+量价背离", score_v3),
    ]

    print(f"\n{'='*70}")
    print(f"{'版本':<20} {'命中率均值':>10} {'胜率均值':>10} {'命中率范围':>18} {'胜率范围':>18}")
    print("-"*70)

    for name, scorer in versions:
        results = sliding_window_eval(scorer, rows, window=14, val_size=2, top_k=10)
        hrs = [r['hit_rate'] for r in results]
        wrs = [r['win_rate'] for r in results]
        print(f"{name:<20} {np.mean(hrs):>9.1%} {np.mean(wrs):>9.1%} "
              f"{min(hrs):>7.0%}-{max(hrs):>7.0%}   {min(wrs):>7.0%}-{max(wrs):>7.0%}")

        if name == "V3-形态+量价背离":
            print(f"\n  详细: {len(results)}个验证窗口")
            for r in results:
                print(f"    {r['val']}: hit={r['hit_rate']:.0%} win={r['win_rate']:.0%}")


if __name__ == "__main__":
    main()
