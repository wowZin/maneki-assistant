#!/usr/bin/env python3
"""批量持仓复盘：对 watchdog state 中 entered 的股票逐个调用 stock_analyzer.analyze"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, '/root/maneki-agent')

# 加载持仓
with open('plays/watchdog/data/state.json') as fp:
    state = json.load(fp)

holdings = []
for code, s in state.items():
    if s.get('status') == 'entered':
        holdings.append((code, s))

print(f'持仓 entered: {len(holdings)} 只')

from plays.watchdog.stock_analyzer import analyze, format_result

results = []
for i, (code, s) in enumerate(holdings, 1):
    t0 = time.time()
    try:
        r = analyze(code)
        results.append({'code': code, 'name': r.get('name', ''), 'model_score': r.get('model_score', 0),
                        'scores': r.get('scores', {}), 'pan': r.get('pan', {})})
        print(f'[{i}/{len(holdings)}] {code} {r.get("name","")} model={r.get("model_score",0):.1f} '
              f'({time.time()-t0:.0f}s)', flush=True)
    except Exception as e:
        print(f'[{i}/{len(holdings)}] {code} FAILED: {e}', flush=True)
        results.append({'code': code, 'error': str(e)})

with open('scripts/tmp_holdings_analysis_0811.json', 'w') as fp:
    json.dump(results, fp, ensure_ascii=False, indent=1)

print('\n=== 批量结果摘要 ===')
for r in results:
    if 'error' in r:
        print(f"  {r['code']} ERROR {r['error']}")
        continue
    pan = r.get('pan', {})
    pct = pan.get('pct_chg', '?')
    verdict = pan.get('verdict', '')
    signals = pan.get('signals', [])
    sig_txt = '; '.join(signals[:3]) if signals else ''
    print(f"  {r['code']} {r['name']} model={r['model_score']:.1f} pct={pct} {verdict} {sig_txt}")
