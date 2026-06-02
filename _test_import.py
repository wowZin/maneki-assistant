import sys
sys.path.insert(0, '.')
from scripts.proxy_utils import request_with_proxy_retry, _looks_blocked
print('Import OK:', request_with_proxy_retry.__name__, _looks_blocked.__name__)
