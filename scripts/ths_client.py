#!/usr/bin/env python3
"""
同花顺 Web 实时行情客户端
=========================

通过 Cookie 认证直连同花顺实时行情接口，替代东方财富 push2 API。

数据来源: d.10jqka.com.cn 实时行情接口 (需登录态 Cookie)
缓存策略: 交易日当日缓存，每次调用批量刷新全市场数据

用法:
    from scripts.ths_client import get_ths_client

    client = get_ths_client()
    quote = client.get_quote("000001")  # → dict with OHLCV, pct_chg, turnover, etc.
    batch = client.get_batch_quotes(["000001", "600519"])  # → {code: {...}}

字段映射 (d.10jqka.com.cn → 标准名):
    199112 → pct_chg    涨跌幅%
    1968584 → turnover  换手率%
    1771976 → vol_ratio 量比
    10     → price      现价
    7      → open       今开
    8      → high       最高
    9      → low        最低
    6      → pre_close  昨收
    13     → volume     成交量(手)
    19     → amount     成交额(元)
    2942   → amplitude  振幅%
    134152 → pe         市盈率
    69     → limit_up   涨停价
    70     → limit_down 跌停价
"""

import logging
import re
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)

PROJECT_DIR = Path(__file__).resolve().parent.parent


def _load_ths_cookie() -> str:
    """从 .env 加载同花顺 Cookie"""
    env_file = PROJECT_DIR / ".env"
    if not env_file.exists():
        return ""
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if line.startswith("THS_COOKIE="):
                return line.split("=", 1)[1]
    return ""


# 同花顺实时行情 → 标准字段映射
_FIELD_MAP = {
    "199112": "pct_chg",       # 涨跌幅%
    "1968584": "turnover",     # 换手率%
    "1771976": "vol_ratio",    # 量比
    "10": "price",             # 现价
    "7": "open",               # 今开
    "8": "high",               # 最高
    "9": "low",                # 最低
    "6": "pre_close",          # 昨收
    "13": "volume",            # 成交量(手)
    "19": "amount",            # 成交额(元)
    "2942": "amplitude",       # 振幅%
    "264648": "change",        # 涨跌额
    "134152": "pe",            # 市盈率
    "69": "limit_up",          # 涨停价
    "70": "limit_down",        # 跌停价
    "24": "bid1",              # 买一价
    "25": "bid1_vol",          # 买一量
    "30": "ask1",              # 卖一价
    "31": "ask1_vol",          # 卖一量
    "14": "inner_vol",         # 内盘
    "15": "outer_vol",         # 外盘
}


class THSClient:
    """同花顺实时行情客户端"""

    def __init__(self, cookie: str = None):
        self._cookie = cookie or _load_ths_cookie()
        self._cache: dict[str, dict] = {}
        self._cache_date: str = ""
        self._cache_ts: float = 0.0
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Cookie": self._cookie,
            "Referer": "https://www.10jqka.com.cn/",
        })

    @property
    def has_cookie(self) -> bool:
        return bool(self._cookie)

    def _code_to_prefix(self, code: str) -> str:
        """代码 → 同花顺前缀 (sh/sz)"""
        code = code.replace(".SH", "").replace(".SZ", "")
        return "sh" if code.startswith("6") else "sz"

    def _normalize_code(self, code: str) -> str:
        """统一代码格式: 000001.SZ → 000001"""
        return code.replace(".SH", "").replace(".SZ", "")

    def _parse_raw(self, text: str) -> Optional[dict]:
        """解析 JSONP 响应为原始 item dict"""
        m = re.search(r'\(\{.*\}\)', text, re.DOTALL)
        if not m:
            return None
        try:
            return json.loads(m.group()[1:-1]).get("items", {})
        except (json.JSONDecodeError, KeyError):
            return None

    def _map_fields(self, raw: dict) -> dict:
        """将同花顺原始字段映射为标准字段名"""
        result = {}
        for raw_key, std_key in _FIELD_MAP.items():
            val = raw.get(raw_key)
            if val is not None:
                try:
                    result[std_key] = float(val)
                except (ValueError, TypeError):
                    result[std_key] = val
        # 保留所有原始字段 (以 f_ 前缀)
        for k, v in raw.items():
            try:
                result[f"f_{k}"] = float(v) if v and v != "-" else 0.0
            except (ValueError, TypeError):
                result[f"f_{k}"] = v
        return result

    def get_quote(self, code: str) -> Optional[dict]:
        """获取单只股票实时行情"""
        if not self._cookie:
            logger.warning("同花顺 Cookie 未配置，无法获取行情")
            return None

        code = self._normalize_code(code)
        prefix = self._code_to_prefix(code)
        url = f"https://d.10jqka.com.cn/v2/realhead/{prefix}_{code}/last.js"

        t0 = time.time()
        ok, items = False, []
        try:
            resp = self._session.get(url, timeout=5)
            if not resp.ok:
                logger.debug(f"同花顺行情失败: {code} HTTP {resp.status_code}")
                return None
            raw = self._parse_raw(resp.text)
            if not raw:
                return None
            ok, items = True, 1
            return self._map_fields(raw)
        except Exception as e:
            logger.debug(f"同花顺行情异常: {code} {e}")
            return None
        finally:
            from scripts.audit import record
            record("ths", "quote", ok=ok, items=items, latency_ms=(time.time()-t0)*1000)

    def get_batch_quotes(self, codes: list[str]) -> dict[str, dict]:
        """批量获取实时行情（逐只请求，自动缓存，缓存 TTL 过期自动刷新）

        Args:
            codes: 股票代码列表

        Returns:
            {code: {price, pct_chg, turnover, ...}}
        """
        now = time.time()

        # 缓存 TTL 判断（盘内每轮重新拉取）
        if not self._cache_date:
            self._cache_date = datetime.now().strftime("%Y%m%d")
        # 每天首次或缓存超过 30 秒，刷新
        if self._cache_date != datetime.now().strftime("%Y%m%d") or now - self._cache_ts > 30:
            self._cache = {}
            self._cache_date = datetime.now().strftime("%Y%m%d")
            self._cache_ts = now

        results = {}
        new_codes = []

        for code in codes:
            short = self._normalize_code(code)
            if short in self._cache:
                results[short] = self._cache[short]
            else:
                new_codes.append(short)

        if new_codes:
            t0, success = time.time(), 0
            for code in new_codes:
                quote = self.get_quote(code)
                if quote:
                    self._cache[code] = quote
                    results[code] = quote
                    success += 1
                else:
                    results[code] = None
            from scripts.audit import record
            record("ths", "batch_quote", ok=success>0,
                   items=success, latency_ms=(time.time()-t0)*1000,
                   extra=f"{success}/{len(new_codes)}")

        return results

    def get_realtime_pct_cache(self) -> dict[str, float]:
        """获取全市场涨跌幅缓存（兼容原 _batch_fetch_realtime_pct 接口）

        注意: 同花顺没有全市场批量接口，这里返回一个空壳。
        实际使用时通过 get_batch_quotes 逐只获取会更准确。
        此方法仅供兼容，建议逐步迁移到 get_batch_quotes。
        """
        logger.warning("get_realtime_pct_cache 已废弃，请使用 get_batch_quotes")
        return {}

    def get_fund_cache(self) -> dict[str, dict]:
        """获取全市场资金流缓存（兼容原 _get_realtime_fund_cache 接口）

        同花顺不直接提供资金流接口。换手率/量比通过 get_quote 获取，
        主力净流入通过 L2 或 Tushare 获取。
        此方法仅供兼容，返回空字典。
        """
        logger.warning("get_fund_cache 已废弃，换手率/量比用 get_quote, 主力净流入用 L2")
        return {}

    def get_hot_list(self, stock_type: str = "a", list_type: str = "value") -> list[dict]:
        """获取同花顺热门搜索榜单（人气排名）

        Args:
            stock_type: a=全部A股
            list_type: value=按热度值排序

        Returns:
            [{code, name, rate(热度值), rise_and_fall(涨跌幅%), hot_rank_chg(排名变化),
              search_cnt(搜索次数), tag: {concept_tag: [...]}}]
        """
        if not self._cookie:
            logger.warning("同花顺 Cookie 未配置")
            return []

        url = (
            "https://dq.10jqka.com.cn/fuyao/hot_list_data/out/hot_list/v1/stock"
            f"?stock_type={stock_type}&type=day&list_type={list_type}"
        )
        t0 = time.time()
        ok, items = False, []
        try:
            resp = self._session.get(url, timeout=10)
            if resp.status_code == 401:
                logger.warning("同花顺 hot_list 接口反爬(401)，Cookie 可能已过期")
                return []
            if not resp.ok:
                return []
            data = resp.json()
            if data.get("status_code") != 0:
                return []
            items = data.get("data", {}).get("stock_list", [])
            ok = True
            # 标准化字段名
            for item in items:
                rf = item.pop("rise_and_fall", 0)
                item["pct_chg"] = float(rf) if rf is not None else 0.0
                item["hot_rank"] = item.get("display_order", 0)
            return items
        except Exception as e:
            logger.warning(f"hot_list 获取失败: {e}")
            return []
        finally:
            from scripts.audit import record
            record("ths", "hot_list", ok=ok, items=len(items) if ok else 0,
                   latency_ms=(time.time()-t0)*1000)

    def get_hot_rank_map(self) -> dict[str, int]:
        """获取热门榜排名映射 {code_short: rank(1-based)}

        用于替代 f62 人气排名。
        """
        items = self.get_hot_list()
        return {item["code"]: item.get("hot_rank", 0) for item in items if item.get("code")}

    def get_concept_tags(self) -> dict[str, list[str]]:
        """获取热门榜股票的概念标签映射 {code_short: [concept_name, ...]}"""
        items = self.get_hot_list()
        return {
            item["code"]: item.get("tag", {}).get("concept_tag", [])
            for item in items if item.get("code")
        }


# 全局单例
_client: Optional[THSClient] = None


def get_ths_client() -> THSClient:
    """获取同花顺客户端单例"""
    global _client
    if _client is None:
        _client = THSClient()
    return _client


def reset_ths_client():
    """重置客户端（Cookie 更新后调用）"""
    global _client
    _client = None
