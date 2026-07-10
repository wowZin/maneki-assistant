#!/usr/bin/env python3
"""Run backtest with intraday metrics disabled."""
import sys, os
sys.path.insert(0, '.')
sys.path.insert(0, 'plays/limit_up/backtest')

# Patch BEFORE any imports that pull in dataset
import plays.limit_up.backtest.dataset as ds_mod
import pandas as pd
ds_mod.pull_intraday_metrics = lambda *a, **kw: pd.DataFrame()

from plays.limit_up.backtest.backtest import resolve_dates, report, build_panel
from plays.limit_up.backtest.dataset import load_analysis_records

all_dates = sorted(set(load_analysis_records()['date'].unique().tolist()))
print(f"可用交易日: {len(all_dates)} 天: {all_dates[0]} ~ {all_dates[-1]}")

dates = all_dates[-20:]
print(f"\n回测区间: {dates[0]} ~ {dates[-1]} ({len(dates)} 天)")

import time
t0 = time.time()
panel = build_panel(dates=dates)
print(f"[面板] 完成 {len(panel)} 行 ({time.time()-t0:.0f}s)")
report(panel)
