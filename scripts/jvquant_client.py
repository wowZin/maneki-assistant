#!/usr/bin/env python3
"""
jvQuant 数据客户端 — 历史资金流向 + 分时数据 + K线查询

支持的查询:
  - 单日/多日资金流向：主力/大单/中单/小单净额 + 换手率 + 量比
  - 历史分钟数据（分时回放）
  - K线数据（日/周/月，前复权/后复权/不复权）
  - Level2 千档盘口 / 逐笔委托

Token 从 .env 的 JVQUANT_TOKEN 读取
"""

import logging
import time
from datetime import datetime, timedelta
from pathlib import Path

import jvQuant

from scripts.audit import record as _audit_record, format_error as _audit_format_error

PROJECT_DIR = Path(__file__).resolve().parent.parent


def _audit(api: str):
    """装饰器：为 JvQuantClient 方法自动记录审计。

    - 成功路径：ok=True，items 由返回值长度决定，latency 计时
    - 失败路径：ok=False，extra 结构化 ERR:...|params:...
    - 异常向外抛出（保持原语义）
    """
    def deco(fn):
        def wrapper(self, *args, **kwargs):
            t0 = time.perf_counter()
            key_param = args[0] if args else kwargs.get("code") or kwargs.get("date") or ""
            try:
                result = fn(self, *args, **kwargs)
                latency = (time.perf_counter() - t0) * 1000
                items = _count(result)
                _audit_record("jvquant", api, ok=True, items=items,
                              latency_ms=latency, extra=f"key={key_param}")
                return result
            except Exception as e:
                latency = (time.perf_counter() - t0) * 1000
                _audit_record("jvquant", api, ok=False, items=0,
                              latency_ms=latency,
                              extra=_audit_format_error(e, {"key": key_param}))
                raise
        wrapper.__name__ = fn.__name__
        wrapper.__doc__ = fn.__doc__
        return wrapper
    return deco


def _count(result) -> int:
    if isinstance(result, list):
        return len(result)
    if isinstance(result, dict):
        for k in ("series", "list", "bars", "items"):
            v = result.get(k)
            if isinstance(v, list):
                return len(v)
        return 1 if result else 0
    return 1 if result else 0


def _load_token() -> str:
    env_file = PROJECT_DIR / ".env"
    if env_file.exists():
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    if key == "JVQUANT_TOKEN":
                        return value
    return ""


class JvQuantClient:
    """jvQuant 数据查询客户端"""

    def __init__(self, token: str = ""):
        self.token = token or _load_token()
        if not self.token:
            raise ValueError("JVQUANT_TOKEN not set in .env")
        self._client = jvQuant.sql_client.Construct(self.token, logging.WARNING)
        self._fundflow_cache: dict[tuple, dict] = {}  # (date, code_short) → row

    # ═══════════════════════════════════════════════════════════
    # 资金流向查询
    # ═══════════════════════════════════════════════════════════

    @_audit("fundflow_single")
    def get_fundflow_single(self, code: str, date: str | None = None) -> dict:
        """获取单只股票单日资金流向

        Args:
            code: 纯数字代码，如 '600176'
            date: YYYY-MM-DD 或 YYYYMMDD，默认今天

        Returns:
            {main_net: 主力净额(万元), big_net: 大单净额, mid_net: 中单净额,
             small_net: 小单净额, turnover: 换手率(%), vol_ratio: 量比,
             pct_chg: 涨跌幅(%)}
        """
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")
        elif len(date) == 8:
            date = f"{date[:4]}-{date[4:6]}-{date[6:8]}"

        cache_key = (date, code)
        if cache_key in self._fundflow_cache:
            return self._fundflow_cache[cache_key]

        query = f"{code},主力净额,大单净额,中单净额,小单净额,换手率,量比,涨跌幅"
        resp = self._client.query(query, 1, 0, "QRR")
        result = self._parse_fundflow_row(resp)
        self._fundflow_cache[cache_key] = result
        return result

    @_audit("fundflow_batch")
    def get_fundflow_batch(self, date: str, filters: str = "主板,非ST") -> list[dict]:
        """获取某日全市场资金流向（最多100条）

        Args:
            date: YYYY-MM-DD 格式日期
            filters: 额外过滤条件，如 "主板,非ST,换手率大于3"

        Returns:
            [{code, name, main_net, big_net, mid_net, small_net,
              turnover, vol_ratio, pct_chg, pe}, ...]
        """
        if len(date) == 8:
            date = f"{date[:4]}-{date[4:6]}-{date[6:8]}"

        query = f"{filters},主力净额,大单净额,中单净额,小单净额,换手率,量比,涨跌幅,市盈率"
        resp = self._client.query(query, 1, 0, "QRR")

        data = resp.get("data", {}) if isinstance(resp, dict) else {}
        fields = data.get("fields", [])
        items = data.get("list", [])

        results = []
        for row in items:
            d = dict(zip(fields, row)) if len(fields) == len(row) else {}
            results.append(self._normalize_fundflow_row(d))
        return results

    @_audit("fundflow_multiday")
    def get_fundflow_multiday(self, code: str, days: int = 5,
                               end_date: str | None = None) -> dict:
        """获取单只股票近N日累计资金流向

        Args:
            code: 纯数字代码
            days: 天数（最多5）
            end_date: 截止日期 YYYYMMDD，默认今天

        Returns:
            {main_net_sum, big_net_sum, mid_net_sum, small_net_sum,
             main_net_daily: [...]}
        """
        if end_date is None:
            end_date = datetime.now().strftime("%Y%m%d")

        # 计算日期范围
        end_dt = datetime.strptime(end_date, "%Y%m%d")
        start_dt = end_dt - timedelta(days=days + 5)  # 多取几天覆盖周末
        start_str = start_dt.strftime("%Y-%m-%d")
        end_str = f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:8]}"

        query = (f"{code},近{days}日大单净额,近{days}日中单净额,"
                 f"近{days}日小单净额,近{days}日主力净额")
        resp = self._client.query(query, 1, 0, "QRR")
        data = resp.get("data", {}) if isinstance(resp, dict) else {}
        items = data.get("list", [])

        if items and len(items[0]) >= 4:
            return {
                "main_net_sum": self._parse_wan(items[0][3]) if len(items[0]) > 3 else 0,
                "big_net_sum": self._parse_wan(items[0][0]) if len(items[0]) > 0 else 0,
                "mid_net_sum": self._parse_wan(items[0][1]) if len(items[0]) > 1 else 0,
                "small_net_sum": self._parse_wan(items[0][2]) if len(items[0]) > 2 else 0,
            }
        return {}

    # ═══════════════════════════════════════════════════════════
    # 分时数据查询
    # ═══════════════════════════════════════════════════════════

    @_audit("minute")
    def get_minute_data(self, code: str, date: str, count: int = 1) -> dict:
        """获取历史分钟数据

        Args:
            code: 纯数字代码，如 '600176'
            date: YYYY-MM-DD 格式
            count: 获取天数

        Returns:
            {days: [date_str, ...], fields: [...],
             series: [{date, last_price, bars: [[time, price, avg_price, volume],...]}]}
        """
        if len(date) == 8:
            date = f"{date[:4]}-{date[4:6]}-{date[6:8]}"

        resp = self._client.minute(code, date, count)
        data = resp.get("data", {}) if isinstance(resp, dict) else {}

        result = {
            "code": data.get("code", code),
            "days": data.get("days", []),
            "fields": data.get("fields", ["时间", "最新价", "均价", "成交量"]),
            "series": [],
        }

        for day_data in data.get("list", []):
            series_item = {
                "date": day_data.get("date", ""),
                "last_price": day_data.get("last_price", ""),
                "bars": day_data.get("list", []),
            }
            result["series"].append(series_item)

        return result

    def get_intraday_metrics(self, code: str, date: str) -> dict:
        """从分钟数据计算日内资金指标

        Returns:
            {vwap, close, open_price, high, low, volume, amount_est,
             morning_vol_ratio, afternoon_strength, tail_vol_ratio}
        """
        md = self.get_minute_data(code, date, count=1)
        if not md["series"]:
            return {}

        bars = md["series"][0]["bars"]
        if not bars:
            return {}

        # 聚合计算
        prices = [b[1] for b in bars if len(b) >= 2]
        avg_prices = [b[2] for b in bars if len(b) >= 3]
        volumes = [b[3] for b in bars if len(b) >= 4]

        if not prices:
            return {}

        close = prices[-1]
        open_price = prices[0]
        high = max(prices)
        low = min(prices)
        total_vol = sum(volumes)
        total_amount = sum(p * v for p, v in zip(prices, volumes))

        # VWAP
        vwap = total_amount / total_vol if total_vol > 0 else close

        # 上午/下午/尾盘成交量
        morning_vol = 0
        afternoon_vol = 0
        tail_vol = 0
        for b in bars:
            t = b[0] if b[0] else ""
            v = b[3] if len(b) >= 4 else 0
            if t < "11:30":
                morning_vol += v
            elif t >= "13:00":
                afternoon_vol += v
            if t >= "14:30":
                tail_vol += v

        return {
            "vwap": round(vwap, 2),
            "close": close,
            "open": open_price,
            "high": high,
            "low": low,
            "volume": total_vol,
            "amount_est": round(total_amount, 0),
            "morning_vol_ratio": round(morning_vol / total_vol, 4) if total_vol > 0 else 0,
            "afternoon_strength": (round((afternoon_vol / (morning_vol if morning_vol > 0 else 1)), 4)),
            "tail_vol_ratio": round(tail_vol / total_vol, 4) if total_vol > 0 else 0,
        }

    # ═══════════════════════════════════════════════════════════
    # K线数据查询
    # ═══════════════════════════════════════════════════════════

    @_audit("kline")
    def get_kline(self, code: str, freq: str = "day", count: int = 5,
                  fq: str = "前复权") -> list[dict]:
        """获取K线数据

        Args:
            code: 纯数字代码
            freq: day/week/month
            count: 获取条数
            fq: 前复权/后复权/不复权

        Returns:
            [{date, open, close, high, low, volume, amount,
              amplitude, pct_chg, change, turnover_rate}, ...]
        """
        resp = self._client.kline(code, "stock", fq, freq, count)
        data = resp.get("data", {}) if isinstance(resp, dict) else {}
        fields = data.get("fields", ["日期", "开盘", "收盘", "最高", "最低",
                                      "成交量", "成交额", "振幅", "涨跌幅",
                                      "涨跌额", "换手率"])
        items = data.get("list", [])

        results = []
        for row in items:
            d = dict(zip(fields, row)) if len(fields) == len(row) else {}
            results.append({
                "date": d.get("日期", ""),
                "open": self._parse_float(d.get("开盘")),
                "close": self._parse_float(d.get("收盘")),
                "high": self._parse_float(d.get("最高")),
                "low": self._parse_float(d.get("最低")),
                "volume": self._parse_float(d.get("成交量")),
                "amount": self._parse_float(d.get("成交额")),
                "amplitude": self._parse_float(d.get("振幅")),
                "pct_chg": self._parse_float(d.get("涨跌幅")),
                "change": self._parse_float(d.get("涨跌额")),
                "turnover_rate": self._parse_float(d.get("换手率")),
            })
        return results

    # ═══════════════════════════════════════════════════════════
    # Level2 数据查询
    # ═══════════════════════════════════════════════════════════

    @_audit("order_book")
    def get_order_book(self, code: str, offset: int = 0) -> list[dict]:
        """获取Level2逐笔委托队列

        Returns:
            [{offset, price, volume, type(B/S), time}, ...]
        """
        resp = self._client.order_book(code, offset)
        data = resp.get("data", {}) if isinstance(resp, dict) else {}
        fields = data.get("fields", ["offset", "price", "volume", "type", "time"])
        items = data.get("list", [])

        results = []
        for row in items:
            d = dict(zip(fields, row)) if len(fields) == len(row) else {}
            results.append({
                "offset": d.get("offset", 0),
                "price": self._parse_float(d.get("price")),
                "volume": self._parse_float(d.get("volume")),
                "type": d.get("type", ""),
                "time": d.get("time", ""),
            })
        return results

    @_audit("level_queue")
    def get_level_queue(self, code: str) -> dict:
        """获取Level2千档盘口

        Returns:
            {code, count, queue: [{price, volume_count, queue_count, queue_slice}, ...]}
        """
        resp = self._client.level_queue(code)
        data = resp.get("data", {}) if isinstance(resp, dict) else {}
        items = data.get("list", [])

        queue = []
        for item in items:
            queue.append({
                "type": item.get("type", ""),
                "price": self._parse_float(item.get("price")),
                "volume_count": self._parse_float(item.get("volume_count")),
                "queue_count": self._parse_float(item.get("queue_count")),
                "queue_slice": item.get("queue_slice", ""),
            })
        return {"code": data.get("code", code), "count": data.get("count", 0),
                "queue": queue}

    # ═══════════════════════════════════════════════════════════
    # 解析工具
    # ═══════════════════════════════════════════════════════════

    def _parse_fundflow_row(self, resp: dict) -> dict:
        """从 query 响应解析单股资金流向"""
        data = resp.get("data", {}) if isinstance(resp, dict) else {}
        items = data.get("list", [])
        fields = data.get("fields", [])

        if not items or not fields:
            return {}

        d = dict(zip(fields, items[0])) if len(fields) == len(items[0]) else {}
        return self._normalize_fundflow_row(d)

    def _normalize_fundflow_row(self, d: dict) -> dict:
        """标准化资金流向字段名"""
        result = {}

        # 找到各字段（jvQuant 返回的字段名包含日期后缀）
        for key, val in d.items():
            v = str(val) if val is not None else ""
            if "代码" in key:
                result["code"] = v
            elif "名称" in key:
                result["name"] = v
            elif "主力净额" in key:
                result["main_net"] = self._parse_wan(v)
            elif "大单净额" in key:
                result["big_net"] = self._parse_wan(v)
            elif "中单净额" in key:
                result["mid_net"] = self._parse_wan(v)
            elif "小单净额" in key:
                result["small_net"] = self._parse_wan(v)
            elif "换手率" in key and "量比" not in key:
                result["turnover"] = self._parse_float(v)
            elif "量比" in key:
                result["vol_ratio"] = self._parse_float(v)
            elif "涨跌幅" in key:
                result["pct_chg"] = self._parse_float(v)
            elif "市盈率" in key or "PE" in key:
                result["pe"] = self._parse_float(v)
            elif "市值" in key:
                result["market_cap"] = v

        return result

    @staticmethod
    def _parse_wan(val) -> float:
        """解析 '1234.56万' 或 '-567.89万' 或 '1.23亿' 为万元数值"""
        if val is None:
            return 0.0
        s = str(val).strip()
        if not s or s == "--":
            return 0.0
        try:
            if "亿" in s:
                return float(s.replace("亿", "")) * 10000
            elif "万" in s:
                return float(s.replace("万", ""))
            else:
                return float(s)
        except (ValueError, TypeError):
            return 0.0

    @staticmethod
    def _parse_float(val) -> float:
        if val is None:
            return 0.0
        try:
            return float(str(val).replace("%", "").replace(",", "").strip())
        except (ValueError, TypeError):
            return 0.0


# ── 单例 ──
_instance: JvQuantClient | None = None


def get_jvquant_client() -> JvQuantClient:
    global _instance
    if _instance is None:
        _instance = JvQuantClient()
    return _instance


if __name__ == "__main__":
    client = JvQuantClient()

    # 测试：获取单股资金流向
    print("=== 600176 资金流向 ===")
    ff = client.get_fundflow_single("600176")
    for k, v in ff.items():
        print(f"  {k}: {v}")

    # 测试：获取分钟数据
    print("\n=== 600176 分时数据 ===")
    md = client.get_minute_data("600176", "2026-06-25", 1)
    for s in md["series"]:
        print(f"  {s['date']}: last_price={s['last_price']}, {len(s['bars'])} bars")

    # 测试：日内指标
    print("\n=== 600176 日内指标 ===")
    im = client.get_intraday_metrics("600176", "2026-06-25")
    for k, v in im.items():
        print(f"  {k}: {v}")

    # 测试：K线
    print("\n=== 600176 K线 ===")
    kl = client.get_kline("600176", count=3)
    for k in kl:
        print(f"  {k['date']}: close={k['close']} pct={k['pct_chg']}% turnover={k['turnover_rate']}%")
