"""Temporary fallback scanner for cron job - uses HTTP direct with retries."""
import requests, json, re, sys, time
from pathlib import Path

BASE = Path.cwd()
headers = {'User-Agent': 'Mozilla/5.0'}
url = ('http://push2.eastmoney.com/api/qt/clist/get'
       '?np=1&fltt=2&invt=2'
       '&fs=m:0+t:6+f:!2,m:0+t:80+f:!2,m:1+t:2+f:!2,m:1+t:23+f:!2,m:0+t:81+s:262144+f:!2'
       '&fields=f12,f14,f2,f3,f11&pn=1&pz=200&po=1&dect=1'
       '&ut=fa5fd1943c7b386f172d6893dbfba10b&fid=f11')

for attempt in range(5):
    try:
        resp = requests.get(url, timeout=15, headers=headers)
        data = resp.json()
        items = data.get('data', {}).get('diff', [])
        candidates = []
        for s in items:
            code, name = s.get('f12',''), s.get('f14','')
            if re.search(r'ST|\*ST|退|N', name or ''):
                continue
            if re.match(r'^(300|301|688|8|4|920)', code):
                continue
            pct = float(s.get('f3',0) or 0)
            if pct < 2 or pct > 9.5:
                continue
            if '.' not in code:
                code = f'{code}.SH' if code.startswith('6') else f'{code}.SZ'
            candidates.append({'code': code, 'name': name, 'pct_chg': pct})
        print(f'OK: {len(candidates)} candidates (attempt {attempt+1})')
        if candidates:
            ts = time.strftime('%Y%m%d_%H%M', time.localtime(time.time()))
            out = BASE / 'data/signals' / f'surge_{ts}.json'
            with open(out, 'w') as f:
                json.dump(candidates, f)
            print(f'Saved: {out}')
            for c in candidates[:5]:
                print(f"  {c['code']} {c['name']} {c['pct_chg']}%")
        else:
            print('No candidates found (market may be closed or no stocks meeting threshold)')
        break
    except Exception as e:
        delay = 2 ** attempt
        print(f'Retry {attempt+1}: {str(e)[:100]}')
        time.sleep(delay)
else:
    print('All 5 retries exhausted')
    sys.exit(1)
