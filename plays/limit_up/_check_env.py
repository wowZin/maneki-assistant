#!/usr/bin/env python3
"""Quick env check"""
import sys
sys.path.insert(0, '/root/maneki-agent')
from scripts.tu_share import CONFIG
print('L2API_ENABLED:', CONFIG.get('L2API_ENABLED', 'not set'))
print('PROXY_ENABLED:', CONFIG.get('PROXY_ENABLED', 'not set'))
