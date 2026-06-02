#!/usr/bin/env python3
"""东方财富代理IP工具模块

动态代理(zdtps.com): 先调API获取代理IP，再用该IP转发请求。
代理IP有效期约130秒，过期自动刷新。

东方财富 push2 API 必须走代理，无需开关。

使用方式:
  from proxy_utils import get_proxy_ip, get_proxies_dict
  from proxy_utils import get_requests_session_with_proxy

  session = get_requests_session_with_proxy()
  resp = session.get(API_URL)
"""

import json
import os
import time
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

SCRIPT_PATH = Path(__file__).resolve().parent.parent
PROJECT_DIR = SCRIPT_PATH if str(SCRIPT_PATH).endswith("maneki-agent") else Path.cwd()
load_dotenv(PROJECT_DIR / ".env")

# === 代理IP服务配置 (从.env读取) ===
PROXY_API_URL = os.getenv("PROXY_API_URL", "http://s189.zdtps.com:8080/GetIP/")
PROXY_INST_ID = os.getenv("PROXY_INST_ID", "")
PROXY_AKEY = os.getenv("PROXY_AKEY", "")

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)

EASTMONEY_HOME = "https://www.eastmoney.com/"

# 代理IP缓存
_cached_proxy = None


def get_proxy_ip(force_refresh=False):
    """获取代理IP地址，缓存未过期则复用，过期自动刷新。

    Returns: str "ip:port"，失败返回None
    """
    global _cached_proxy

    if not force_refresh and _cached_proxy:
        if time.time() < _cached_proxy["expires_at"]:
            addr = f"{_cached_proxy['ip']}:{_cached_proxy['port']}"
            print(f"  [代理] 复用缓存代理: {addr} (剩余{int(_cached_proxy['expires_at'] - time.time())}秒)")
            return addr

    print("  [代理] 从API获取新代理IP...")
    params = {
        "inst_id": PROXY_INST_ID,
        "akey": PROXY_AKEY,
        "count": "1",
        "dedup": "1",
        "timespan": "2",
        "type": "2",
    }
    query = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{PROXY_API_URL}?{query}"

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Python/proxy_utils"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            text = resp.read().decode("utf-8").strip()

        if not text:
            print("  [代理] API返回空内容")
            return None

        data = json.loads(text)
        if data.get("data") and data["data"].get("proxy_list"):
            p = data["data"]["proxy_list"][0]
            ip = p["ip"]
            port = p["port"]
            expired_seconds = p.get("expired_seconds", 130)

            _cached_proxy = {
                "ip": ip,
                "port": port,
                "expires_at": time.time() + expired_seconds - 10,
            }
            addr = f"{ip}:{port}"
            print(f"  [代理] 获取新代理: {addr} (有效期{expired_seconds}秒)")
            return addr

        print("  [代理] API返回无可用代理")
        return None

    except Exception as e:
        print(f"  [代理] 获取代理IP失败: {e}")
        return None


def get_proxies_dict(proxy_addr=None):
    """返回requests可用的代理dict。

    直接调get_proxy_ip获取代理，不设开关。

    Returns: {"http": "http://ip:port", "https": "http://ip:port"} 或 None(获取失败)
    """
    if proxy_addr is None:
        proxy_addr = get_proxy_ip()
    if proxy_addr is None:
        return None

    proxy_url = f"http://{proxy_addr}"
    return {"http": proxy_url, "https": proxy_url}


def get_requests_session_with_proxy(proxy_addr=None):
    """创建带代理+浏览器UA的requests.Session。

    先访问东方财富首页拿cookies，再返回session供后续API请求使用。
    """
    import requests

    proxy_addr = proxy_addr or get_proxy_ip()
    if proxy_addr is None:
        print("  [代理] 获取代理IP失败，返回无代理session")
        sess = requests.Session()
        sess.headers.update({
            "User-Agent": BROWSER_UA,
            "Referer": "https://quote.eastmoney.com/",
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Connection": "keep-alive",
        })
        return sess

    proxy_url = f"http://{proxy_addr}"
    sess = requests.Session()
    sess.proxies = {"http": proxy_url, "https": proxy_url}
    sess.headers.update({
        "User-Agent": BROWSER_UA,
        "Referer": "https://quote.eastmoney.com/",
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Connection": "keep-alive",
    })

    print("  [代理] 访问东方财富首页拿cookies...")
    try:
        home_resp = sess.get(EASTMONEY_HOME, timeout=15, verify=True)
        print(f"  [代理] 首页状态码: {home_resp.status_code}, cookies: {list(sess.cookies.keys())}")
    except Exception as e:
        print(f"  [代理] 首页访问异常(继续): {e}")

    return sess


def get_urllib_opener_with_proxy(proxy_addr=None):
    """创建带代理的urllib OpenerDirector"""
    proxy_addr = proxy_addr or get_proxy_ip()
    if proxy_addr is None:
        return None

    proxy_handler = urllib.request.ProxyHandler({
        "http": f"http://{proxy_addr}",
        "https": f"http://{proxy_addr}",
    })
    opener = urllib.request.build_opener(proxy_handler)
    opener.addheaders = [
        ("User-Agent", BROWSER_UA),
        ("Referer", "https://quote.eastmoney.com/"),
        ("Accept", "*/*"),
    ]
    return opener


# ═══════════════════════════════════════════════════════════
# 代理重试机制：失败时自动换IP重试
# ═══════════════════════════════════════════════════════════

def clear_proxy_cache():
    """清除缓存的代理IP（请求失败时调用，强制下次换新IP）"""
    global _cached_proxy
    _cached_proxy = None


def request_with_proxy_retry(url, max_retries=3, timeout=10, **kwargs):
    """通过代理请求URL，失败时自动清除缓存IP + 换新IP重试。

    Args:
        url: 请求URL
        max_retries: 最大重试次数（不含首次，共 1+max_retries 次尝试）
        timeout: 每次请求超时秒数
        **kwargs: 传给 requests.get 的其他参数(如 headers)

    Returns:
        requests.Response 对象，或 None（全部重试失败）

    每次重试前会 clear_proxy_cache() 强制获取新代理IP。
    每日800次提取额度下，max_retries=3 每次调用最多消耗4个IP。
    """
    import requests as _requests

    last_error = None
    for attempt in range(1 + max_retries):
        if attempt > 0:
            # 重试前：清除缓存IP，强制换新
            clear_proxy_cache()
            print(f"  [代理重试] 第{attempt}次重试 (已换新IP)...")

        proxy_addr = get_proxy_ip()
        if proxy_addr is None:
            last_error = "无法获取代理IP"
            continue

        proxies = {"http": f"http://{proxy_addr}", "https": f"http://{proxy_addr}"}
        try:
            resp = _requests.get(url, proxies=proxies, timeout=timeout, **kwargs)
            # 检查是否被东财拦截（返回空或异常状态码）
            if resp.status_code == 200:
                return resp
            if resp.status_code in (403, 429, 502, 503):
                print(f"  [代理重试] 状态码{resp.status_code}, 换IP重试")
                continue
            return resp  # 其他状态码直接返回
        except Exception as e:
            last_error = str(e)[:100]
            print(f"  [代理重试] 请求失败: {last_error}")

    print(f"  [代理重试] {1+max_retries}次尝试全部失败: {last_error}")
    return None