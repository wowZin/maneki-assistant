#!/usr/bin/env python3
import sys; sys.path.insert(0, 'scripts')
from tu_share import call_tushare

pushed = ['002354.SZ','603757.SH','002057.SZ','600172.SH','002167.SZ']
name_map = {'002354.SZ':'天娱数科','603757.SH':'大元泵业','002057.SZ':'中钢天源','600172.SH':'黄河旋风','002167.SZ':'东方锆业'}

# 今日数据
today = call_tushare('daily', {'ts_code':','.join(pushed), 'start_date':'20260710', 'end_date':'20260710'})
fields = today['data']['fields']
items = today['data']['items']

print('='*75)
print('今日推送股表现 (20260710)')
print('='*75)
print(f"{'代码':<12} {'名称':<8} {'开盘':>7} {'最高':>7} {'最低':>7} {'收盘':>7} {'涨跌%':>7} {'振幅%':>7}")
for row in items:
    d = dict(zip(fields, row))
    nm = name_map.get(d['ts_code'],'')
    amp = (float(d['high'])-float(d['low']))/float(d['pre_close'])*100 if float(d['pre_close'])!=0 else 0
    print(f"{d['ts_code']:<12} {nm:<8} {float(d['open']):>7.2f} {float(d['high']):>7.2f} {float(d['low']):>7.2f} {float(d['close']):>7.2f} {float(d['pct_chg']):>7.2f} {amp:>7.2f}")

print()

# 推前收盘
prev = call_tushare('daily', {'ts_code':','.join(pushed), 'start_date':'20260709', 'end_date':'20260709'})
pfields = prev['data']['fields']
pitems = prev['data']['items']
prev_close = {}
for row in pitems:
    d = dict(zip(pfields, row))
    prev_close[d['ts_code']] = float(d['close'])

print(f"{'代码':<12} {'名称':<8} {'推前收盘':>8} {'今日开盘':>8} {'今日最高':>8} {'开跌%':>8} {'收跌%':>8} {'高浮盈%':>8}")
print('-'*75)
for row in items:
    d = dict(zip(fields, row))
    nm = name_map.get(d['ts_code'],'')
    pc = prev_close.get(d['ts_code'], float(d['pre_close']))
    open_pct = (float(d['open']) - pc) / pc * 100
    high_pct = (float(d['high']) - pc) / pc * 100
    close_pct = (float(d['close']) - pc) / pc * 100
    print(f"{d['ts_code']:<12} {nm:<8} {pc:>8.2f} {float(d['open']):>8.2f} {float(d['high']):>8.2f} {open_pct:>8.2f} {close_pct:>8.2f} {high_pct:>8.2f}")
