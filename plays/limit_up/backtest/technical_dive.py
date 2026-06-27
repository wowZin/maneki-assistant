#!/usr/bin/env python3
"""
Technical dimension deep-dive analysis script.
Reads backtest panel.csv and produces diagnostic statistics for the technical_proposal.md.
"""
import csv
from collections import defaultdict
import math


def spearman_rank(x, y):
    """Manual Spearman rank correlation (no scipy)."""
    n = len(x)
    if n < 2:
        return 0.0
    # rank x
    sorted_x = sorted((v, i) for i, v in enumerate(x))
    rank_x = [0] * n
    for r, (_, i) in enumerate(sorted_x):
        rank_x[i] = r + 1
    # rank y
    sorted_y = sorted((v, i) for i, v in enumerate(y))
    rank_y = [0] * n
    for r, (_, i) in enumerate(sorted_y):
        rank_y[i] = r + 1
    # Pearson on ranks
    mean_rx = sum(rank_x) / n
    mean_ry = sum(rank_y) / n
    num = sum((rank_x[i] - mean_rx) * (rank_y[i] - mean_ry) for i in range(n))
    den_x = math.sqrt(sum((rank_x[i] - mean_rx) ** 2 for i in range(n)))
    den_y = math.sqrt(sum((rank_y[i] - mean_ry) ** 2 for i in range(n)))
    if den_x == 0 or den_y == 0:
        return 0.0
    return num / (den_x * den_y)


def load_panel(path):
    rows = []
    with open(path, newline='') as f:
        reader = csv.DictReader(f)
        for r in reader:
            rows.append(r)
    return rows


def parse_float(v):
    try:
        return float(v)
    except Exception:
        return None


def main():
    panel = load_panel('/Users/zhangying/projects/maneki-assistant/plays/limit_up/backtest/out/panel.csv')
    # Filter valid rows
    data = []
    for r in panel:
        tech = parse_float(r.get('technical'))
        fwd3 = parse_float(r.get('fwd_ret_3'))
        fwd1 = parse_float(r.get('fwd_ret_1'))
        fwd_max3 = parse_float(r.get('fwd_max_3'))
        hit = parse_float(r.get('hit_limit_3'))
        trail5 = parse_float(r.get('trailing_5'))
        trail10 = parse_float(r.get('trailing_10'))
        pct_chg = parse_float(r.get('pct_chg_score_day'))
        total = parse_float(r.get('total'))
        if tech is None or fwd3 is None or trail5 is None or trail10 is None:
            continue
        data.append({
            'technical': tech,
            'fwd_ret_3': fwd3,
            'fwd_ret_1': fwd1,
            'fwd_max_3': fwd_max3,
            'hit_limit_3': hit,
            'trailing_5': trail5,
            'trailing_10': trail10,
            'pct_chg_score_day': pct_chg,
            'total': total,
        })

    n = len(data)
    print(f"Valid rows: {n}")

    # 1. RankIC
    tech_vals = [d['technical'] for d in data]
    fwd3_vals = [d['fwd_ret_3'] for d in data]
    fwd1_vals = [d['fwd_ret_1'] for d in data]
    fwd_max3_vals = [d['fwd_max_3'] for d in data]
    hit_vals = [d['hit_limit_3'] for d in data]
    trail5_vals = [d['trailing_5'] for d in data]
    trail10_vals = [d['trailing_10'] for d in data]
    pct_chg_vals = [d['pct_chg_score_day'] for d in data]

    print(f"RankIC technical vs fwd_ret_3: {spearman_rank(tech_vals, fwd3_vals):.4f}")
    print(f"RankIC technical vs fwd_ret_1: {spearman_rank(tech_vals, fwd1_vals):.4f}")
    print(f"RankIC technical vs fwd_max_3: {spearman_rank(tech_vals, fwd_max3_vals):.4f}")
    print(f"RankIC technical vs hit_limit_3: {spearman_rank(tech_vals, hit_vals):.4f}")
    print(f"RankIC technical vs trailing_5: {spearman_rank(tech_vals, trail5_vals):.4f}")
    print(f"RankIC technical vs trailing_10: {spearman_rank(tech_vals, trail10_vals):.4f}")
    print(f"RankIC technical vs pct_chg_score_day: {spearman_rank(tech_vals, pct_chg_vals):.4f}")

    # 2. Bucket analysis (5 buckets by technical score)
    sorted_by_tech = sorted(data, key=lambda x: x['technical'])
    bucket_size = n // 5
    buckets = []
    for b in range(5):
        start = b * bucket_size
        end = start + bucket_size if b < 4 else n
        bucket = sorted_by_tech[start:end]
        avg_tech = sum(d['technical'] for d in bucket) / len(bucket)
        avg_fwd3 = sum(d['fwd_ret_3'] for d in bucket) / len(bucket)
        avg_fwd1 = sum(d['fwd_ret_1'] for d in bucket) / len(bucket)
        avg_hit = sum(d['hit_limit_3'] for d in bucket) / len(bucket)
        avg_trail5 = sum(d['trailing_5'] for d in bucket) / len(bucket)
        avg_trail10 = sum(d['trailing_10'] for d in bucket) / len(bucket)
        avg_pct = sum((d['pct_chg_score_day'] or 0) for d in bucket) / len(bucket)
        win_rate = sum(1 for d in bucket if d['fwd_ret_3'] > 0) / len(bucket)
        buckets.append({
            'bucket': b,
            'n': len(bucket),
            'avg_tech': avg_tech,
            'avg_fwd3': avg_fwd3,
            'avg_fwd1': avg_fwd1,
            'avg_hit': avg_hit,
            'avg_trail5': avg_trail5,
            'avg_trail10': avg_trail10,
            'avg_pct_chg': avg_pct,
            'win_rate': win_rate,
        })

    print("\n--- Bucket Analysis (by technical score) ---")
    for b in buckets:
        print(f"Bucket {b['bucket']}: n={b['n']}, tech_mean={b['avg_tech']:.2f}, "
              f"fwd_ret_3={b['avg_fwd3']:.4f}, fwd_ret_1={b['avg_fwd1']:.4f}, "
              f"hit_limit_3={b['avg_hit']:.4f}, trail_5={b['avg_trail5']:.4f}, "
              f"trail_10={b['avg_trail10']:.4f}, pct_chg_day={b['avg_pct_chg']:.4f}, "
              f"win_rate={b['win_rate']:.4f}")

    # 3. High-score concentration: top 20% technical vs trailing/pct_chg
    top20_pct = int(n * 0.2)
    top20 = sorted_by_tech[-top20_pct:]
    bottom80 = sorted_by_tech[:-top20_pct]
    print("\n--- Top 20% vs Bottom 80% ---")
    print(f"Top20 avg trailing_5: {sum(d['trailing_5'] for d in top20)/len(top20):.4f}")
    print(f"Bot80 avg trailing_5: {sum(d['trailing_5'] for d in bottom80)/len(bottom80):.4f}")
    print(f"Top20 avg trailing_10: {sum(d['trailing_10'] for d in top20)/len(top20):.4f}")
    print(f"Bot80 avg trailing_10: {sum(d['trailing_10'] for d in bottom80)/len(bottom80):.4f}")
    print(f"Top20 avg pct_chg_day: {sum((d['pct_chg_score_day'] or 0) for d in top20)/len(top20):.4f}")
    print(f"Bot80 avg pct_chg_day: {sum((d['pct_chg_score_day'] or 0) for d in bottom80)/len(bottom80):.4f}")
    print(f"Top20 avg fwd_ret_3: {sum(d['fwd_ret_3'] for d in top20)/len(top20):.4f}")
    print(f"Bot80 avg fwd_ret_3: {sum(d['fwd_ret_3'] for d in bottom80)/len(bottom80):.4f}")
    print(f"Top20 hit_limit_3: {sum(d['hit_limit_3'] for d in top20)/len(top20):.4f}")
    print(f"Bot80 hit_limit_3: {sum(d['hit_limit_3'] for d in bottom80)/len(bottom80):.4f}")

    # 4. Trailing-return bins: how does technical score vary by trailing_10?
    sorted_by_trail10 = sorted(data, key=lambda x: x['trailing_10'])
    t_bucket_size = n // 5
    t_buckets = []
    for b in range(5):
        start = b * t_bucket_size
        end = start + t_bucket_size if b < 4 else n
        bucket = sorted_by_trail10[start:end]
        avg_tech = sum(d['technical'] for d in bucket) / len(bucket)
        avg_trail10 = sum(d['trailing_10'] for d in bucket) / len(bucket)
        avg_fwd3 = sum(d['fwd_ret_3'] for d in bucket) / len(bucket)
        avg_hit = sum(d['hit_limit_3'] for d in bucket) / len(bucket)
        t_buckets.append({
            'bucket': b,
            'avg_trail10': avg_trail10,
            'avg_tech': avg_tech,
            'avg_fwd3': avg_fwd3,
            'avg_hit': avg_hit,
        })
    print("\n--- Trailing_10 Quintiles (technical score inside each) ---")
    for b in t_buckets:
        print(f"Trail10 Bucket {b['bucket']}: trail10_mean={b['avg_trail10']:.4f}, "
              f"tech_mean={b['avg_tech']:.2f}, fwd_ret_3={b['avg_fwd3']:.4f}, hit_limit_3={b['avg_hit']:.4f}")

    # 5. pct_chg_score_day bins
    sorted_by_pct = sorted(data, key=lambda x: x['pct_chg_score_day'] or 0)
    p_bucket_size = n // 5
    p_buckets = []
    for b in range(5):
        start = b * p_bucket_size
        end = start + p_bucket_size if b < 4 else n
        bucket = sorted_by_pct[start:end]
        avg_pct = sum((d['pct_chg_score_day'] or 0) for d in bucket) / len(bucket)
        avg_tech = sum(d['technical'] for d in bucket) / len(bucket)
        avg_fwd3 = sum(d['fwd_ret_3'] for d in bucket) / len(bucket)
        avg_hit = sum(d['hit_limit_3'] for d in bucket) / len(bucket)
        p_buckets.append({
            'bucket': b,
            'avg_pct': avg_pct,
            'avg_tech': avg_tech,
            'avg_fwd3': avg_fwd3,
            'avg_hit': avg_hit,
        })
    print("\n--- pct_chg_score_day Quintiles (technical score inside each) ---")
    for b in p_buckets:
        print(f"PctChg Bucket {b['bucket']}: pct_mean={b['avg_pct']:.4f}, "
              f"tech_mean={b['avg_tech']:.2f}, fwd_ret_3={b['avg_fwd3']:.4f}, hit_limit_3={b['avg_hit']:.4f}")

    # 6. Conditional: high trailing but low technical vs low trailing high technical
    trail10_high = [d for d in data if d['trailing_10'] > 0.15]  # >15%
    trail10_low = [d for d in data if d['trailing_10'] < 0.05]  # <5%
    print("\n--- Conditional: trailing_10 > 15% vs < 5% ---")
    if trail10_high:
        print(f">15% trail10: n={len(trail10_high)}, avg_tech={sum(d['technical'] for d in trail10_high)/len(trail10_high):.2f}, "
              f"avg_fwd3={sum(d['fwd_ret_3'] for d in trail10_high)/len(trail10_high):.4f}, "
              f"hit={sum(d['hit_limit_3'] for d in trail10_high)/len(trail10_high):.4f}")
    if trail10_low:
        print(f"<5% trail10: n={len(trail10_low)}, avg_tech={sum(d['technical'] for d in trail10_low)/len(trail10_low):.2f}, "
              f"avg_fwd3={sum(d['fwd_ret_3'] for d in trail10_low)/len(trail10_low):.4f}, "
              f"hit={sum(d['hit_limit_3'] for d in trail10_low)/len(trail10_low):.4f}")

    # 7. High technical (>75) vs low technical (<40) deep stats
    high_tech = [d for d in data if d['technical'] >= 75]
    low_tech = [d for d in data if d['technical'] < 40]
    print("\n--- High tech (>=75) vs Low tech (<40) ---")
    if high_tech:
        print(f"High tech n={len(high_tech)}, avg_trail5={sum(d['trailing_5'] for d in high_tech)/len(high_tech):.4f}, "
              f"avg_trail10={sum(d['trailing_10'] for d in high_tech)/len(high_tech):.4f}, "
              f"avg_pct_chg={sum((d['pct_chg_score_day'] or 0) for d in high_tech)/len(high_tech):.4f}, "
              f"avg_fwd3={sum(d['fwd_ret_3'] for d in high_tech)/len(high_tech):.4f}, "
              f"hit={sum(d['hit_limit_3'] for d in high_tech)/len(high_tech):.4f}")
    if low_tech:
        print(f"Low tech n={len(low_tech)}, avg_trail5={sum(d['trailing_5'] for d in low_tech)/len(low_tech):.4f}, "
              f"avg_trail10={sum(d['trailing_10'] for d in low_tech)/len(low_tech):.4f}, "
              f"avg_pct_chg={sum((d['pct_chg_score_day'] or 0) for d in low_tech)/len(low_tech):.4f}, "
              f"avg_fwd3={sum(d['fwd_ret_3'] for d in low_tech)/len(low_tech):.4f}, "
              f"hit={sum(d['hit_limit_3'] for d in low_tech)/len(low_tech):.4f}")

    # 8. Position penalty simulation: what if we subtract trailing_10 * 50 from technical?
    penalized = []
    for d in data:
        # trailing_10 is decimal (e.g. 0.10 = 10%), scale to 0-50 penalty
        penalty = min(50, d['trailing_10'] * 200)  # 25% trailing -> 50 penalty
        adj = max(0, d['technical'] - penalty)
        penalized.append({**d, 'adj_tech': adj})
    adj_vals = [d['adj_tech'] for d in penalized]
    print(f"\n--- Position Penalty Simulation (penalty = min(50, trailing_10*200)) ---")
    print(f"RankIC adj_tech vs fwd_ret_3: {spearman_rank(adj_vals, fwd3_vals):.4f}")
    print(f"RankIC adj_tech vs hit_limit_3: {spearman_rank(adj_vals, hit_vals):.4f}")
    print(f"RankIC adj_tech vs trailing_10: {spearman_rank(adj_vals, trail10_vals):.4f}")

    # 9. What if we only keep stocks with trailing_10 < 10% ?
    low_mo = [d for d in data if d['trailing_10'] < 0.10]
    if low_mo:
        lt_tech = [d['technical'] for d in low_mo]
        lt_fwd3 = [d['fwd_ret_3'] for d in low_mo]
        lt_hit = [d['hit_limit_3'] for d in low_mo]
        print(f"\n--- Subset: trailing_10 < 10% (n={len(low_mo)}) ---")
        print(f"RankIC tech vs fwd_ret_3: {spearman_rank(lt_tech, lt_fwd3):.4f}")
        print(f"RankIC tech vs hit_limit_3: {spearman_rank(lt_tech, lt_hit):.4f}")
        print(f"Avg tech={sum(lt_tech)/len(lt_tech):.2f}, avg fwd3={sum(lt_fwd3)/len(lt_fwd3):.4f}, hit={sum(lt_hit)/len(lt_hit):.4f}")


if __name__ == '__main__':
    main()
