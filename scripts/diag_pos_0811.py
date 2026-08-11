import json, os, glob
from pathlib import Path

base = Path('/root/maneki-agent')

# 1. 交割单: 昨天(0810)的买入记录
print("=== 交割单 plays/trading/data/reports/ 最近文件 ===")
rpt_dir = base / 'plays/trading/data/reports'
for f in sorted(rpt_dir.glob('*.json')):
    print(' ', f.name, f.stat().st_mtime)
# 找昨天 20260810 和今天的
for date in ['20260810', '20260811']:
    f = rpt_dir / f'{date}.json'
    if f.exists():
        recs = json.loads(f.read_text())
        print(f"\n=== {date} 交割单 {len(recs)} 条 ===")
        for r in recs:
            print(f"  {r.get('direction')} {r.get('code')} {r.get('name')} "
                  f"price={r.get('price')} shares={r.get('shares')} time={r.get('time')} "
                  f"reason={r.get('reason')} pnl={r.get('pnl')}")

# 2. state.json 当前盯盘状态
print("\n=== state.json ===")
sf = base / 'plays/watchdog/data/state.json'
if sf.exists():
    s = json.loads(sf.read_text())
    print('states:', len(s))
    for code, st in s.items():
        print(f"  {code} {st.get('name','')} status={st.get('status')} "
              f"entry_at={st.get('entry_at')} entry_price={st.get('entry_price')} "
              f"highest={st.get('highest_since_entry')} t1={st.get('t1_blocked_date')} "
              f"pending_sell={st.get('pending_sell_order_id')}")
else:
    print('  NO state.json')
