#!/usr/bin/env python3
"""转债打板模拟交易记录系统（验证阶段）。

玩法：模型高分 + 竞价强势 → 有转债 → 转债 T+0（开盘买、收盘卖）。
目的：用真实事前口径记录模拟盈亏，验证玩法能否赚钱，避免冲动充值开通权限。

过滤规则（宽松，积累样本）：
  主板(00/60) + 有存续转债 + (model_score ≥ MS_MIN 或 auc_pct ≥ AUC_MIN)

模拟口径：
  买入 = 转债当日开盘价（open）
  卖出 = 转债当日收盘价（close），T+0 当日了结
  收益 = (close/open - 1) * 100

记录输出：wiki/raw/cb-play/sim/{date}.json
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, '/root/maneki-agent')
from scripts.tu_share import call_tushare

PANEL_DIR = '/root/maneki-agent/wiki/raw/limit-up/panel'
OUT_DIR = '/root/maneki-agent/wiki/raw/cb-play/sim'

MS_MIN = 40.0    # 模型分阈值（宽松）
AUC_MIN = 3.0    # 竞价涨幅阈值（宽松）


def all_trade_dates(start, end):
    resp = call_tushare('trade_cal', {'start_date': start, 'end_date': end, 'is_open': '1'}, 'cal_date')
    if resp and resp.get('data'):
        fi = {f: i for i, f in enumerate(resp['data']['fields'])}
        return sorted(r[fi['cal_date']] for r in resp['data']['items'])
    return []


def load_cb_map():
    resp = call_tushare('cb_basic', {}, 'ts_code,stk_code,delist_date')
    if not resp or not resp.get('data'):
        return {}
    fi = {f: i for i, f in enumerate(resp['data']['fields'])}
    out = {}
    for r in resp['data']['items']:
        d = dict(zip(fi, r))
        if not d.get('delist_date'):
            out[d['stk_code']] = d['ts_code']
    return out


def load_cb_daily(date):
    r = call_tushare('cb_daily', {'trade_date': date}, 'ts_code,open,close,pct_chg,pre_close')
    if not r or not r.get('data'):
        return {}
    fi = {f: i for i, f in enumerate(r['data']['fields'])}
    return {x[fi['ts_code']]: {f: x[fi[f]] for f in fi} for x in r['data']['items']}


def load_panel(date):
    f = f'{PANEL_DIR}/{date}.parquet'
    if not os.path.exists(f):
        return None
    import pandas as pd
    try:
        return pd.read_parquet(f, columns=['code', 'name', 'model_score', 'auc_pct'])
    except Exception:
        return None


def run_day(date, cb_map):
    """单日模拟，返回当日候选记录列表。"""
    panel = load_panel(date)
    if panel is None:
        return []
    cb_daily = load_cb_daily(date)
    records = []
    for _, r in panel.iterrows():
        code = r['code']
        if not code.startswith(('00', '60')):
            continue
        cb = cb_map.get(code)
        if not cb or cb not in cb_daily:
            continue
        ms = r['model_score']
        ap = r['auc_pct']
        if ms is None or (isinstance(ms, float) and ms != ms):
            ms = 0.0
        if ap is None or (isinstance(ap, float) and ap != ap):
            ap = 0.0
        ms, ap = float(ms), float(ap)
        # 过滤：模型分 或 竞价 任一达标
        if not (ms >= MS_MIN or ap >= AUC_MIN):
            continue
        row = cb_daily[cb]
        if not row.get('open') or not row.get('close') or not row.get('pre_close'):
            continue
        buy = row['open']
        sell = row['close']
        ret = (sell / buy - 1) * 100
        records.append({
            'date': date,
            'stock_code': code,
            'stock_name': r.get('name', ''),
            'cb_code': cb,
            'model_score': round(ms, 2),
            'auc_pct': round(ap, 2),
            'buy_price': buy,       # 转债开盘价
            'sell_price': sell,     # 转债收盘价
            'ret_pct': round(ret, 3),
            'signal': 'model' if ms >= MS_MIN else 'auction' if ap >= AUC_MIN else '',
        })
    return records


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--start', default='20260713')
    ap.add_argument('--end', default=None)
    ap.add_argument('--date', default=None)  # 单日
    args = ap.parse_args()

    cb_map = load_cb_map()
    print(f'存续转债映射 {len(cb_map)} 只')

    if args.date:
        dates = [args.date]
    else:
        dates = all_trade_dates(args.start, args.end or args.start)

    os.makedirs(OUT_DIR, exist_ok=True)
    total = 0
    for d in dates:
        records = run_day(d, cb_map)
        if records:
            out_file = f'{OUT_DIR}/{d}.json'
            with open(out_file, 'w') as f:
                json.dump(records, f, ensure_ascii=False, indent=2)
            rets = [x['ret_pct'] for x in records]
            win = sum(1 for x in rets if x > 0)
            print(f'{d}: {len(records)} 笔 | 收益 {sum(rets)/len(rets):+.2f}% | 胜率 {win/len(rets)*100:.0f}%')
            for x in records:
                print(f"    {x['stock_code']} {x['stock_name']} ms={x['model_score']} auc={x['auc_pct']}% → {x['ret_pct']:+.2f}%")
            total += len(records)
        else:
            # 记录空文件（当日无候选，保持日期连续性）
            out_file = f'{OUT_DIR}/{d}.json'
            with open(out_file, 'w') as f:
                json.dump([], f)
    print(f'\n累计 {total} 笔模拟交易，输出目录 {OUT_DIR}')


if __name__ == '__main__':
    main()
