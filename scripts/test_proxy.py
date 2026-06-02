#!/usr/bin/env python3
"""代理连通性测试脚本

用法:
  python scripts/test_proxy.py              # 基本测试
  python scripts/test_proxy.py -v           # 详细输出
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import requests
from proxy_utils import get_proxies_dict, is_proxy_enabled

verbose = "-v" in sys.argv

# 1. 检查代理是否启用
if not is_proxy_enabled():
    print("❌ PROXY_ENABLED=false")
    sys.exit(1)
print("✅ PROXY_ENABLED=true")

# 2. 获取代理配置
proxies = get_proxies_dict()
if not proxies:
    print("❌ 获取代理配置失败")
    sys.exit(1)
print(f"✅ 代理: {proxies.get('http', '无')[:60]}...")

# 3. 测试 stock/get（单只查询）
url1 = "https://push2.eastmoney.com/api/qt/stock/get?secid=1.600126&fields=f43,f170"
try:
    resp = requests.get(url1, proxies=proxies, timeout=10)
    d = resp.json().get("data", {})
    price = d.get("f43", 0)
    pct = d.get("f170", 0)
    if price:
        print(f"✅ stock/get: 600126 价格{price} 涨幅{pct}%")
    else:
        print(f"⚠️ stock/get: 返回空数据")
except Exception as e:
    print(f"❌ stock/get: {e}")
    sys.exit(1)

# 4. 测试 clist（批量查询）
url2 = "https://push2.eastmoney.com/api/qt/clist/get?np=1&fltt=2&invt=2&fs=m:0+t:6+f:!2,m:0+t:80+f:!2,m:1+t:2+f:!2,m:1+t:23+f:!2,m:0+t:81+s:262144+f:!2&fields=f12,f14,f2,f3&pn=1&pz=10&po=1&dect=1&ut=fa5fd1943c7b386f172d6893dbfba10b"
try:
    resp = requests.get(url2, proxies=proxies, timeout=10)
    items = resp.json().get("data", {}).get("diff", [])
    if items and len(items) > 0:
        print(f"✅ clist: {len(items)} 只")
        if verbose:
            for s in items[:5]:
                print(f"     {s.get('f12','')} {s.get('f14','')} {s.get('f3','?')}%")
    else:
        print(f"⚠️ clist: 返回空")
except Exception as e:
    print(f"❌ clist: {e}")
    sys.exit(1)

print("\n🎉 代理正常")
