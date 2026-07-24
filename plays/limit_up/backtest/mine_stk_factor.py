#!/usr/bin/env python3
"""stk_factor_pro 技术因子挖掘：拉取→算IC→选最优特征→重训。"""
import sys; sys.path.insert(0,'.')
import pandas as pd, numpy as np, json, os, joblib, time
from scripts.tu_share import call_tushare
from plays.limit_up.backtest.metrics import rank_ic
from plays.limit_up.backtest.training import FEATURE_COLS, TRAINING_CSV

df = pd.read_csv(TRAINING_CSV, dtype={'trade_date':str})
df = df[df.trade_date <= '20260707']
dates = sorted(df.trade_date.unique())
codes = sorted(df.code.unique())
print(f'{len(dates)} dates, {len(codes)} codes')

# ── Pick best indicators (avoiding ones we already have via pit_features) ──
FIELDS = ('ts_code,trade_date,close_qfq,'
    'rsi_qfq_6,rsi_qfq_12,rsi_qfq_24,'
    'kdj_k_qfq,kdj_d_qfq,kdj_qfq,'
    'wr_qfq,wr1_qfq,cci_qfq,'
    'bias1_qfq,bias2_qfq,bias3_qfq,'
    'psy_qfq,roc_qfq,maroc_qfq,'
    'mfi_qfq,vr_qfq,'
    'dmi_pdi_qfq,dmi_mdi_qfq,dmi_adx_qfq,'
    'atr_qfq,'
    'ma_qfq_5,ma_qfq_10,ma_qfq_20,ma_qfq_60')

# ── Pull all dates ──
print('\nPulling stk_factor_pro...')
all_data = []
for i, date in enumerate(dates):
    try:
        r = call_tushare('stk_factor_pro', {'trade_date': date}, FIELDS, timeout=120)
        items = r.get('data',{}).get('items',[])
        flds = r.get('data',{}).get('fields',[])
        for row in items:
            all_data.append(dict(zip(flds, row)))
        print(f'  {date}: {len(items)} stocks')
    except Exception as e:
        print(f'  {date}: FAILED - {e}')
    time.sleep(0.5)  # rate limit

df_f = pd.DataFrame(all_data)
df_f['trade_date'] = df_f['trade_date'].astype(str)
for c in df_f.columns:
    if c not in ('ts_code','trade_date'):
        df_f[c] = pd.to_numeric(df_f[c], errors='coerce')
print(f'Total: {len(df_f)} rows')

# ── Merge with training set ──
m = df[['code','trade_date','hit_limit_3','avg_amount_5d']].copy()
m = m.merge(df_f, left_on=['code','trade_date'], right_on=['ts_code','trade_date'], how='left')

# ── Derived features ──
for ma_n in [5,10,20,60]:
    col = f'ma_qfq_{ma_n}'
    if col in m.columns:
        m[f'ma{ma_n}_dev'] = m['close_qfq'] / m[col] - 1.0

m['atr_pct'] = m['atr_qfq'] / m['close_qfq']  # ATR as % of price
m['kdj_diff'] = m['kdj_k_qfq'] - m['kdj_d_qfq']

# ── Compute IC ──
raw_feats = ['rsi_qfq_6','rsi_qfq_12','rsi_qfq_24','kdj_k_qfq','kdj_d_qfq','kdj_qfq',
    'wr_qfq','wr1_qfq','cci_qfq','bias1_qfq','bias2_qfq','bias3_qfq',
    'psy_qfq','roc_qfq','maroc_qfq','mfi_qfq','vr_qfq',
    'dmi_pdi_qfq','dmi_mdi_qfq','dmi_adx_qfq','atr_pct']
derived = ['ma5_dev','ma10_dev','ma20_dev','ma60_dev','kdj_diff']

print(f'\nIC Analysis:')
candidates = raw_feats + derived
results = []
for feat in candidates:
    if feat not in m.columns: continue
    valid = m[[feat,'hit_limit_3']].dropna()
    if len(valid) < 100: continue
    ic = rank_ic(valid[feat], valid.hit_limit_3)
    if ic is not None:
        results.append((feat, abs(ic), len(valid)))

results.sort(key=lambda x: -x[1])
header = f'{"Feature":<25s} {"|IC|":>8s} {"N":>6s}'
print(header)
for feat, ic, n in results:
    mark = ' ★' if ic > 0.10 else ''
    print(f'{feat:<25s} {ic:>8.4f} {n:>6d}{mark}')

# Select top features
KEEP = [f for f, ic, n in results if ic > 0.08]
print(f'\nKeeping {len(KEEP)} new features with |IC| > 0.08')
