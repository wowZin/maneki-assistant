#!/usr/bin/env python3
"""Quick check for trading time and proxy"""
import sys
sys.path.insert(0, '/root/maneki-agent')
from plays.limit_up.utils import is_trading_time, is_market_closed
print('is_trading_time:', is_trading_time())
print('is_market_closed:', is_market_closed())
