#!/usr/bin/env python3
"""批量持仓复盘：读 watchdog state.json 的 entered 持仓，逐个调用 stock_analyzer.analyze()"""
import json
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_DIR = Path('/root/maneki-agent')
sys.path.insert(0, str(PROJECT_DIR))

state_file = PROJECT_DIR / 'plays' / 'watchdog' / 'data' / 'state.json'
with open(state_file) as fp:
    state = json.load(fp)

holdings = [(code, s) for code, s in state.items() if s.get('status') == 'entered']
print(f'持仓 entered: {len(holdings)} 只')

from plays.watchdog.stock_analyzer import analyze  # noqa: E402

results = []
for i, (code, s) in enumerate(holdings, 1):
    t0 = time.time()
    try:
        r = analyze(code)
        results.append({
            'code': code, 'name': r.get('name', ''),
            'entry_price': s.get('entry_price'),
            'model_score': r.get('model_score', 0),
            'scores': r.get('scores', {}),
            'pan': r.get('pan', {}),
        })
        pan = r.get('pan', {})
        sig = '; '.join(pan.get('signals', [])[:3])
        print(f'[{i}/{len(holdings)}] {code} {r.get("name","")} model={r.get("model_score",0):.1f} '
              f'pct={pan.get("pct_chg")} {pan.get("verdict","")} {sig} ({time.time()-t0:.0f}s)', flush=True)
    except Exception as e:
        print(f'[{i}/{len(holdings)}] {code} FAILED: {e}', flush=True)
        results.append({'code': code, 'error': str(e)})

out = PROJECT_DIR / 'scripts' / f'tmp_holdings_analysis_{datetime.now().strftime("%Y%m%d")}.json'
out.write_text(json.dumps(results, ensure_ascii=False, indent=1))
print(f'\n结果已写: {out}')
print('\n=== 摘要 ===')
for r in results:
    if 'error' in r:
        print(f"  {r['code']} ERROR {r['error']}")
        continue
    pan = r.get('pan', {})
    print(f"  {r['code']} {r['name']} model={r['model_score']:.1f} pct={pan.get('pct_chg')} "
          f"{pan.get('verdict','')} {'; '.join(pan.get('signals',[])[:2])}")
