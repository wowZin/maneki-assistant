#!/usr/bin/env python3
"""Collect closing review data: surge count + concept board top/bottom."""
import json, requests

# 1. Parse surge file (handle leading "1|")
with open("/root/maneki-agent/plays/limit_up/data/pushed/20260729_surge.json") as f:
    raw = f.read()
if raw.startswith("1|"):
    raw = raw[2:]
surge = json.loads(raw)
print(f"SURGE_COUNT|{len(surge)}")
scores = [i['total_score'] for i in surge]
print(f"SURGE_MAX|{max(scores):.2f}")
print(f"SURGE_MIN|{min(scores):.2f}")

# 2. Concept boards - descending order
url = 'https://push2.eastmoney.com/api/qt/clist/get'
params = {
    'cb': '', 'pn': 1, 'pz': 500, 'po': 1,
    'np': 1, 'ut': 'bd1d9ddb04089700cf9c27f6f7426281',
    'fltt': 2, 'invt': 2, 'fid': 'f3',
    'fs': 'm:90+t:2', 'fields': 'f3,f14'
}
r = requests.get(url, params=params, timeout=10)
data = r.json()
items = data['data']['diff']
print(f"CONCEPT_COUNT|{len(items)}")
print("TOP3|" + "|".join(f"{i['f14']}:+{i['f3']}%" for i in items[:3]))
# Ascending order for worst performers
params2 = dict(params, po=0)
r2 = requests.get(url, params=params2, timeout=10)
data2 = r2.json()
items2 = data2['data']['diff']
print("BOT3|" + "|".join(f"{i['f14']}:{i['f3']}%" for i in items2[:3]))
