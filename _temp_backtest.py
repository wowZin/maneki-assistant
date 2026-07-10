#!/usr/bin/env python3
import sys; sys.path.insert(0, 'scripts')
from tu_share import call_tushare
from collections import defaultdict

pushed = ['002354.SZ','603757.SH','002057.SZ','600172.SH','002167.SZ']
pushed_names = {'002354.SZ':'天娱数科','603757.SH':'大元泵业','002057.SZ':'中钢天源','600172.SH':'黄河旋风','002167.SZ':'东方锆业'}

print("="*70)
print("今日5只推送股 历史走势回测分析")
print("="*70)

df = call_tushare('daily', {'ts_code':','.join(pushed), 'start_date':'20260615', 'end_date':'20260710'})
data = df['data']
fields = data['fields']
items = data['items']

by_code = defaultdict(list)
for row in items:
    d = dict(zip(fields, row))
    by_code[d['ts_code']].append(d)

for code in pushed:
    rows = by_code.get(code, [])
    rows.sort(key=lambda x: x['trade_date'])
    nm = pushed_names[code]
    
    print(f"\n--- {nm} ({code}) ---")
    recent = rows[-6:]
    dates = [r['trade_date'] for r in recent]
    pcts = [float(r['pct_chg']) for r in recent]
    closes = [float(r['close']) for r in recent]
    
    print(f"  近6日:  {' → '.join(dates)}")
    print(f"  涨跌%:  {' → '.join(f'{p:+.2f}' for p in pcts)}")
    print(f"  收盘价: {' → '.join(f'{c:.2f}' for c in closes)}")
    
    today_chg = pcts[-1]
    if today_chg < -3:
        print(f"  ⚠️ 今日大跌 {today_chg:.2f}%")
        if len(pcts) >= 4:
            pre_3 = sum(pcts[-4:-1])
            print(f"  推前3日累计: {pre_3:+.2f}%")
    elif today_chg > 0:
        print(f"  ✅ 今日上涨 {today_chg:.2f}%")
    else:
        print(f"  ⚪ 今日平盘 {today_chg:.2f}%")
