#!/usr/bin/env python3
"""HTTP fallback scanner for push2.eastmoney.com when HTTPS proxy fails."""
import requests, json, re, time, os

headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Referer': 'https://quote.eastmoney.com/',
}
url = ('http://push2.eastmoney.com/api/qt/clist/get'
       '?np=1&fltt=2&invt=2'
       '&fs=m:0+t:6+f:!2,m:0+t:80+f:!2,m:1+t:2+f:!2,m:1+t:23+f:!2,m:0+t:81+s:262144+f:!2'
       '&fields=f12,f14,f2,f3,f11&pn=1&pz=200&po=1'
       '&dect=1&ut=fa5fd1943c7b386f172d6893dbfba10b&fid=f3')

for attempt in range(5):
    try:
        resp = requests.get(url, timeout=15, headers=headers)
        data = resp.json()
        items = data.get('data', {}).get('diff', [])
        if not items:
            print(f'Attempt {attempt+1}: empty response, retrying...')
            time.sleep(2**attempt)
            continue
        candidates = []
        for s in items:
            code = str(s.get('f12', ''))
            name = str(s.get('f14', ''))
            if re.search(r'ST|\*ST|退|N', name or ''):
                continue
            if re.match(r'^(300|301|688|8|4|920)', code):
                continue
            pct = float(s.get('f3', 0) or 0)
            if pct < 2 or pct > 9.5:
                continue
            if '.' not in code:
                code = f'{code}.SH' if code.startswith('6') else f'{code}.SZ'
            candidates.append({'code': code, 'name': name, 'pct_chg': pct})
        ts = time.strftime('%Y%m%d_%H%M')
        fpath = f'plays/limit_up/data/signals/surge_{ts}.json'
        os.makedirs(os.path.dirname(fpath), exist_ok=True)
        with open(fpath, 'w') as f:
            json.dump(candidates, f, ensure_ascii=False)
        print(f'OK:{len(candidates)} candidates -> {fpath}')
        break
    except Exception as e:
        print(f'Retry {attempt+1}: {type(e).__name__}: {str(e)[:80]}')
        time.sleep(2 ** attempt)
else:
    print('All 5 attempts failed')
