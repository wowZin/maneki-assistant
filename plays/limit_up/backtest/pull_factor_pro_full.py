#!/usr/bin/env python3
"""增量拉取 stk_factor_pro(261字段) + cyq_perf，覆盖训练集全部日期。

按 trade_date 全量拉取（铁律：禁止逐股调用）。已存在于 parquet 的日期跳过。
输出:
  wiki/raw/limit-up/panel/stk_factor_pro.parquet
  wiki/raw/limit-up/panel/cyq_perf.parquet
"""
import sys, time
sys.path.insert(0, '/root/maneki-agent')
import pandas as pd
from scripts.tu_share import call_tushare

TRAIN_CSV = '/root/maneki-agent/wiki/raw/limit-up/training/training_set.csv'
PANEL = '/root/maneki-agent/wiki/raw/limit-up/panel'


def pull_interface(api: str, out_path: str, dates: list[str], fields: str = ''):
    try:
        old = pd.read_parquet(out_path)
        old['trade_date'] = old['trade_date'].astype(str)
        have = set(old.trade_date.unique())
    except Exception:
        old = pd.DataFrame()
        have = set()
    todo = [d for d in dates if d not in have]
    print(f'{api}: {len(dates)} dates, have {len(have)}, todo {len(todo)}', flush=True)
    if not todo:
        return

    rows = []
    flds = None
    t0 = time.time()
    for i, date in enumerate(todo):
        for attempt in range(3):
            try:
                r = call_tushare(api, {'trade_date': date}, fields, timeout=120)
                f = r.get('data', {}).get('fields', [])
                items = r.get('data', {}).get('items', [])
                if flds is None:
                    flds = f
                for row in items:
                    rows.append(dict(zip(f, row)))
                eta = (time.time() - t0) / (i + 1) * (len(todo) - i - 1)
                print(f'  [{i+1}/{len(todo)}] {date}: {len(items)} rows, ETA {eta:.0f}s', flush=True)
                break
            except Exception as e:
                print(f'  {date} attempt {attempt+1} FAILED: {e}', flush=True)
                time.sleep(2 * (attempt + 1))
        time.sleep(0.5)

    new = pd.DataFrame(rows)
    new['trade_date'] = new['trade_date'].astype(str)
    for c in new.columns:
        if c not in ('ts_code', 'trade_date'):
            new[c] = pd.to_numeric(new[c], errors='coerce')
    df = pd.concat([old, new], ignore_index=True).drop_duplicates(
        subset=['ts_code', 'trade_date'], keep='last')
    df.to_parquet(out_path, index=False)
    print(f'{api} DONE: {df.shape} -> {out_path}', flush=True)


def main():
    t = pd.read_csv(TRAIN_CSV, dtype={'trade_date': str})
    dates = sorted(t.trade_date.unique())
    pull_interface('cyq_perf', f'{PANEL}/cyq_perf.parquet', dates)
    pull_interface('stk_factor_pro', f'{PANEL}/stk_factor_pro.parquet', dates)


if __name__ == '__main__':
    main()
