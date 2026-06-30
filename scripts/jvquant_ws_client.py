#!/usr/bin/env python3
"""
jvQuant WebSocket 实时行情客户端 — 替代 l2_daemon

分层订阅策略（价值最大化）:
  L1 (5档盘口) → Top 10-15 候选股初筛: 价格/量比/买卖比
  L10 (10档盘口) → Top 5 高分股深度: 卖盘压单检测/盘口深度
  L2 (逐笔成交) → Top 1-2 推送确认: VWAP/净流向/尾盘检测

成本跟踪:
  - 每只股票订阅 ~0.3元/天（首次订阅计费，同日重复订阅不计）
  - 日消费超50元时飞书报警（不降级）
  - 每轮扫描结束后自动退订不再需要的股票

接口兼容:
  提供与 l2_daemon_client 相同的函数签名，现有代码无需修改
"""

import json
import logging
import threading
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import jvQuant

PROJECT_DIR = Path(__file__).resolve().parent.parent


def _load_env() -> dict:
    env = {}
    env_file = PROJECT_DIR / ".env"
    if env_file.exists():
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    env[key] = value
    return env


_ENV = _load_env()
TOKEN = _ENV.get("JVQUANT_TOKEN", "")
BUDGET_ALERT = float(_ENV.get("JVQUANT_BUDGET_ALERT", "50"))  # 报警阈值（元）
COST_PER_STOCK = float(_ENV.get("JVQUANT_COST_PER_STOCK", "0.3"))  # 单只订阅成本


# ═══════════════════════════════════════════════════════════════
# 数据缓存（线程安全）
# ═══════════════════════════════════════════════════════════════

class StockDataCache:
    """单只股票的实时数据缓存"""

    def __init__(self):
        self.l1: dict = {}       # AbLv1 最新快照
        self.l10: dict = {}      # AbLv10 最新快照
        self.l2_ticks: list = []  # AbLv2 逐笔成交（最近200条）
        self.l2_amount: float = 0
        self.l2_volume: float = 0
        self.vwap: float = 0
        self.last_update: float = 0
        # 分钟K线聚合
        self.kline_bars: list[dict] = []  # [{time, open, high, low, close, volume}]
        self._current_bar: dict | None = None
        self._bar_minute: str = ""

    def update_l1(self, lv1) -> None:
        self.l1 = lv1.get_map() if hasattr(lv1, 'get_map') else {}
        self.last_update = time.time()

    def update_l10(self, lv10) -> None:
        self.l10 = lv10.get_map() if hasattr(lv10, 'get_map') else {}
        self.last_update = time.time()

    def add_tick(self, deal) -> None:
        """添加逐笔成交，同时更新分钟K线"""
        d = deal.get_map() if hasattr(deal, 'get_map') else {}
        price = float(d.get("price", 0))
        volume = float(d.get("volume", 0))
        tick_time = str(d.get("time", ""))

        self.l2_ticks.append(d)
        if len(self.l2_ticks) > 200:
            self.l2_ticks = self.l2_ticks[-200:]

        self.l2_amount += price * volume
        self.l2_volume += volume
        if self.l2_volume > 0:
            self.vwap = self.l2_amount / self.l2_volume

        # 分钟K线聚合
        if tick_time and len(tick_time) >= 5:
            minute = tick_time[:5]  # "13:05"
            if minute != self._bar_minute:
                self._finish_bar()
                self._bar_minute = minute
                self._current_bar = {
                    "time": minute,
                    "open": price, "high": price,
                    "low": price, "close": price,
                    "volume": volume,
                }
            elif self._current_bar:
                self._current_bar["high"] = max(self._current_bar["high"], price)
                self._current_bar["low"] = min(self._current_bar["low"], price)
                self._current_bar["close"] = price
                self._current_bar["volume"] += volume

    def _finish_bar(self) -> None:
        if self._current_bar:
            self.kline_bars.append(self._current_bar)
            if len(self.kline_bars) > 240:
                self.kline_bars = self.kline_bars[-240:]

    def get_market_snapshot(self) -> dict:
        """返回与 l2api market 兼容的数据结构"""
        l1 = self.l1
        l10 = self.l10
        # 优先用 L10（10档深度），降级 L1（5档）
        src = l10 if l10 else l1

        bid_prices = [src.get(f"b{i}p", "") for i in range(1, 11)]
        bid_qtys = [str(src.get(f"b{i}", 0)) for i in range(1, 11)]
        ask_prices = [src.get(f"s{i}p", "") for i in range(1, 11)]
        ask_qtys = [str(src.get(f"s{i}", 0)) for i in range(1, 11)]

        total_bid = sum(float(q) for q in bid_qtys if q)
        total_ask = sum(float(q) for q in ask_qtys if q)

        return {
            "last": str(src.get("price", 0)),
            "open": str(src.get("open", src.get("last_close", 0))),
            "high": str(src.get("high", src.get("price", 0))),
            "low": str(src.get("low", src.get("price", 0))),
            "pre_close": str(src.get("last_close", 0)),
            "bid_price": bid_prices,
            "bid_qty": [str(int(float(q))) for q in bid_qtys],
            "ask_price": ask_prices,
            "ask_qty": [str(int(float(q))) for q in ask_qtys],
            "total_bid_volume": str(int(total_bid)),
            "total_ask_volume": str(int(total_ask)),
            "trade_volume": str(src.get("volume", 0)),
            "trade_amount": str(src.get("amount", 0)),
            "time": src.get("time", ""),
        }

    def is_ready(self) -> bool:
        """数据是否就绪（5秒内有更新）"""
        return (time.time() - self.last_update) < 5


# ═══════════════════════════════════════════════════════════════
# WebSocket 客户端
# ═══════════════════════════════════════════════════════════════

class JvQuantWSClient:
    """jvQuant WebSocket 实时行情客户端（单例）"""

    def __init__(self):
        if not TOKEN:
            raise ValueError("JVQUANT_TOKEN not set in .env")

        self._ws: Any = None
        self._cache: dict[str, StockDataCache] = defaultdict(StockDataCache)
        self._l1_codes: set[str] = set()   # 当前 L1 订阅
        self._l10_codes: set[str] = set()  # 当前 L10 订阅
        self._l2_codes: set[str] = set()   # 当前 L2 订阅
        self._all_subscribed_today: set[str] = set()  # 今日已订阅（计费）
        self._today: str = datetime.now().strftime("%Y%m%d")
        self._alert_sent: bool = False
        self._lock = threading.Lock()
        self._running = False

    # ── 连接管理 ──

    def connect(self) -> bool:
        """建立 WebSocket 连接"""
        if self._running:
            return True

        self._check_day_reset()

        ws = jvQuant.websocket_client
        try:
            self._ws = ws.Construct(
                market="ab",
                token=TOKEN,
                log_level=logging.WARNING,
                ab_lv1_handle=self._on_l1,
                ab_lv2_handle=self._on_l2,
                ab_lv10_handle=self._on_l10,
            )
            self._running = True
            print(f"[jvQuant WS] 已连接")
            return True
        except Exception as e:
            print(f"[jvQuant WS] 连接失败: {e}")
            return False

    def disconnect(self) -> None:
        if self._ws and self._running:
            try:
                self._ws.disconnect()
            except Exception:
                pass
            self._running = False

    def is_connected(self) -> bool:
        return self._running

    # ── 订阅管理 ──

    def subscribe_l1(self, codes: list[str]) -> int:
        """订阅 L1（5档盘口）。返回新增订阅数"""
        return self._subscribe("l1", codes)

    def subscribe_l10(self, codes: list[str]) -> int:
        """订阅 L10（10档盘口）。返回新增订阅数"""
        return self._subscribe("l10", codes)

    def subscribe_l2(self, codes: list[str]) -> int:
        """订阅 L2（逐笔成交）。返回新增订阅数"""
        return self._subscribe("l2", codes)

    def unsubscribe_l1(self, codes: list[str]) -> None:
        self._unsubscribe("l1", codes)

    def unsubscribe_l10(self, codes: list[str]) -> None:
        self._unsubscribe("l10", codes)

    def unsubscribe_l2(self, codes: list[str]) -> None:
        self._unsubscribe("l2", codes)

    def _subscribe(self, level: str, codes: list[str]) -> int:
        """内部订阅方法"""
        if not self._running:
            self.connect()
        if not self._running:
            return 0

        attr_map = {"l1": "_l1_codes", "l10": "_l10_codes", "l2": "_l2_codes"}
        method_map = {"l1": self._ws.add_lv1, "l10": self._ws.add_lv10,
                      "l2": self._ws.add_lv2}
        code_set = getattr(self, attr_map[level])

        new_codes = [c for c in codes if c not in code_set and len(c) == 6]
        if not new_codes:
            return 0

        with self._lock:
            method_map[level](new_codes)
            code_set.update(new_codes)
            self._all_subscribed_today.update(new_codes)

        self._check_budget()
        return len(new_codes)

    def _unsubscribe(self, level: str, codes: list[str]) -> None:
        attr_map = {"l1": "_l1_codes", "l10": "_l10_codes", "l2": "_l2_codes"}
        method_map = {"l1": self._ws.del_lv1, "l10": self._ws.del_lv10,
                      "l2": self._ws.del_lv2}
        code_set = getattr(self, attr_map[level])

        remove = [c for c in codes if c in code_set]
        if not remove:
            return

        with self._lock:
            try:
                method_map[level](remove)
            except Exception:
                pass
            code_set.difference_update(remove)

    # ── 数据回调 ──

    def _on_l1(self, lv1) -> None:
        code = lv1.code if hasattr(lv1, 'code') else ""
        if code:
            self._cache[code].update_l1(lv1)

    def _on_l10(self, lv10) -> None:
        code = lv10.code if hasattr(lv10, 'code') else ""
        if code:
            self._cache[code].update_l10(lv10)

    def _on_l2(self, lv2) -> None:
        code = lv2.code if hasattr(lv2, 'code') else ""
        if not code:
            return
        deals = lv2.deal_list if hasattr(lv2, 'deal_list') else []
        for deal in deals:
            self._cache[code].add_tick(deal)

    # ── 数据查询（兼容 l2_daemon_client API） ──

    def get_market(self, code: str) -> dict | None:
        """获取盘口快照（兼容 daemon_get_market）"""
        short = code.replace(".SH", "").replace(".SZ", "")
        c = self._cache.get(short)
        if c and c.is_ready():
            return c.get_market_snapshot()
        return None

    def get_vwap(self, code: str) -> float | None:
        """获取 VWAP（兼容 daemon_get_vwap）"""
        short = code.replace(".SH", "").replace(".SZ", "")
        c = self._cache.get(short)
        if c and c.vwap > 0:
            return c.vwap
        return None

    def get_kline(self, code: str, n: int = 5) -> list[dict]:
        """获取分钟K线（兼容 daemon_get_kline）"""
        short = code.replace(".SH", "").replace(".SZ", "")
        c = self._cache.get(short)
        if c and c.kline_bars:
            return c.kline_bars[-n:]
        return []

    def is_ready(self, code: str) -> bool:
        """数据是否就绪（兼容 daemon_is_ready）"""
        short = code.replace(".SH", "").replace(".SZ", "")
        c = self._cache.get(short)
        return c.is_ready() if c else False

    def get_bid_ask_ratio(self, code: str) -> float:
        """买卖盘口比（新增：L1数据即可计算）"""
        short = code.replace(".SH", "").replace(".SZ", "")
        c = self._cache.get(short)
        if c:
            l1 = c.l1
            b1 = float(l1.get("b1", 0))
            s1 = float(l1.get("s1", 0))
            return b1 / s1 if s1 > 0 else 1.0
        return 1.0

    # ── 成本跟踪 ──

    @property
    def daily_cost(self) -> float:
        return len(self._all_subscribed_today) * COST_PER_STOCK

    @property
    def subscribed_count(self) -> int:
        return len(self._all_subscribed_today)

    def _check_day_reset(self) -> None:
        today = datetime.now().strftime("%Y%m%d")
        if today != self._today:
            self._all_subscribed_today.clear()
            self._today = today
            self._alert_sent = False

    def _check_budget(self) -> None:
        if self.daily_cost >= BUDGET_ALERT and not self._alert_sent:
            self._alert_sent = True
            self._send_feishu_alert()

    def _send_feishu_alert(self) -> None:
        """飞书报警：日消费超阈值"""
        try:
            from scripts.tu_share import CONFIG
            import requests

            msg = (f"⚠️ jvQuant 日消费预警\n"
                   f"已订阅: {self.subscribed_count} 只\n"
                   f"预估费用: {self.daily_cost:.1f} 元\n"
                   f"阈值: {BUDGET_ALERT} 元\n"
                   f"时间: {datetime.now().strftime('%H:%M')}\n"
                   f"状态: 继续服务（未降级）")

            token_resp = requests.post(
                "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": CONFIG["FEISHU_APP_ID"],
                      "app_secret": CONFIG["FEISHU_APP_SECRET"]},
                timeout=10)
            token = token_resp.json().get("tenant_access_token", "")

            if token:
                requests.post(
                    "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "receive_id": CONFIG.get("FEISHU_CHAT_ID_SIGNAL", ""),
                        "msg_type": "text",
                        "content": json.dumps({"text": msg})
                    },
                    timeout=10)
                print(f"[jvQuant WS] 飞书报警已发送: {self.daily_cost:.1f}元")
        except Exception as e:
            print(f"[jvQuant WS] 飞书报警失败: {e}")

    # ── 统计 ──

    def stats(self) -> dict:
        return {
            "l1_count": len(self._l1_codes),
            "l10_count": len(self._l10_codes),
            "l2_count": len(self._l2_codes),
            "total_subscribed_today": self.subscribed_count,
            "daily_cost": round(self.daily_cost, 1),
            "budget_alert_sent": self._alert_sent,
        }


# ═══════════════════════════════════════════════════════════════
# 单例 + 兼容接口（与 l2_daemon_client 相同签名）
# ═══════════════════════════════════════════════════════════════

_ws_client: JvQuantWSClient | None = None


def _get_ws() -> JvQuantWSClient:
    global _ws_client
    if _ws_client is None:
        _ws_client = JvQuantWSClient()
        _ws_client.connect()
    return _ws_client


# ── 兼容 l2_daemon_client 接口 ──

def daemon_alive() -> bool:
    """检查 WebSocket 是否连接（兼容旧接口）"""
    try:
        ws = _get_ws()
        return ws.is_connected()
    except Exception:
        return False


def daemon_subscribe(codes: list[str]) -> None:
    """订阅 L1（兼容旧接口，成本最优）"""
    ws = _get_ws()
    shorts = [c.replace(".SH", "").replace(".SZ", "") for c in codes]
    n = ws.subscribe_l1(shorts)
    if n > 0:
        print(f"[jvQuant] L1订阅 +{n}只 (累计{ws.subscribed_count}只/{ws.daily_cost:.1f}元)")


def daemon_subscribe_l10(codes: list[str]) -> None:
    """订阅 L10（深度盘口，高分股专用）"""
    ws = _get_ws()
    shorts = [c.replace(".SH", "").replace(".SZ", "") for c in codes]
    n = ws.subscribe_l10(shorts)
    if n > 0:
        print(f"[jvQuant] L10订阅 +{n}只")


def daemon_subscribe_l2(codes: list[str]) -> None:
    """订阅 L2（逐笔，推送确认专用）"""
    ws = _get_ws()
    shorts = [c.replace(".SH", "").replace(".SZ", "") for c in codes]
    n = ws.subscribe_l2(shorts)
    if n > 0:
        print(f"[jvQuant] L2订阅 +{n}只")


def daemon_unsubscribe(codes: list[str]) -> None:
    """退订所有级别"""
    ws = _get_ws()
    shorts = [c.replace(".SH", "").replace(".SZ", "") for c in codes]
    ws.unsubscribe_l1(shorts)
    ws.unsubscribe_l10(shorts)
    ws.unsubscribe_l2(shorts)


def daemon_get_market(code: str) -> dict | None:
    """获取盘口快照"""
    return _get_ws().get_market(code)


def daemon_get_vwap(code: str) -> float | None:
    """获取 VWAP"""
    return _get_ws().get_vwap(code)


def daemon_get_kline(code: str, n: int = 5) -> list[dict]:
    """获取分钟K线"""
    return _get_ws().get_kline(code, n)


def daemon_is_ready(code: str) -> bool:
    """数据是否就绪"""
    return _get_ws().is_ready(code)


def daemon_stats() -> dict:
    """获取统计信息"""
    return _get_ws().stats()


def daemon_health() -> str:
    """健康检查（兼容旧接口）"""
    ws = _get_ws()
    if ws.is_connected():
        s = ws.stats()
        return (f"OK|jvQuant WS|L1={s['l1_count']} L10={s['l10_count']} "
                f"L2={s['l2_count']}|今日{s['total_subscribed_today']}只"
                f"={s['daily_cost']}元")
    return "DOWN|jvQuant WS disconnected"


def daemon_is_healthy() -> bool:
    """健康状态（兼容旧接口）"""
    return daemon_alive()


# ── 扩展接口（非兼容，新增） ──

def daemon_cmd(cmd: str) -> str:
    """兼容旧 l2_daemon 命令接口

    支持的命令:
      SUB <codes>    → subscribe_l1
      UNSUB <codes>  → unsubscribe all levels
      MARKET <code>  → get_market (返回 JSON)
      VWAP <code>    → get_vwap (返回浮点数)
      HEALTH         → daemon_health()
      PING           → "PONG"
    """
    ws = _get_ws()
    parts = cmd.strip().split()
    if not parts:
        return "ERR empty command"

    op = parts[0].upper()
    try:
        if op == "SUB":
            codes = parts[1:]
            ws.subscribe_l1(codes)
            return f"OK subscribed {len(codes)}"

        elif op == "UNSUB":
            codes = parts[1:]
            ws.unsubscribe_l1(codes)
            ws.unsubscribe_l10(codes)
            ws.unsubscribe_l2(codes)
            return f"OK unsubscribed {len(codes)}"

        elif op == "MARKET":
            code = parts[1]
            mkt = ws.get_market(code)
            if mkt:
                return json.dumps(mkt, ensure_ascii=False)
            return "NULL"

        elif op == "VWAP":
            code = parts[1]
            vwap = ws.get_vwap(code)
            return str(vwap) if vwap else "NULL"

        elif op == "HEALTH":
            return daemon_health()

        elif op == "PING":
            return "PONG"

        elif op == "NETFLOW":
            # NETFLOW 基于 L2 逐笔数据计算大单净流向
            code = parts[1]
            short = code.replace(".SH", "").replace(".SZ", "")
            c = ws._cache.get(short)
            if c and c.l2_volume > 0:
                # 简化：用 L2 金额 / L2 量 ≈ 均价，乘以 (买量-卖量) 估计净流向
                buy_vol = sum(float(d.get("volume", 0))
                              for d in c.l2_ticks if d.get("type", "") == "B")
                sell_vol = sum(float(d.get("volume", 0))
                               for d in c.l2_ticks if d.get("type", "") == "S")
                net = (buy_vol - sell_vol) * c.vwap
                return str(net)
            return "NULL"

        else:
            return f"ERR unknown command: {op}"

    except Exception as e:
        return f"ERR {e}"


# ── 分层订阅接口 ──

def subscribe_tiered(candidates: list[dict], top_n_l1: int = 12,
                     top_n_l10: int = 5, top_n_l2: int = 2) -> dict:
    """分层订阅：按评分/涨幅排序后，分批订阅不同级别

    Args:
        candidates: [{code, pct_chg, ...}] 候选股列表
        top_n_l1: L1 订阅前 N 只
        top_n_l10: L10 订阅前 N 只
        top_n_l2: L2 订阅前 N 只

    Returns:
        {l1: [...], l10: [...], l2: [...]}
    """
    ws = _get_ws()
    # 按涨幅排序
    sorted_candidates = sorted(candidates,
                               key=lambda x: x.get("pct_chg", 0), reverse=True)

    shorts = []
    for c in sorted_candidates:
        code = c.get("code", "")
        short = code.replace(".SH", "").replace(".SZ", "")
        if len(short) == 6:
            shorts.append(short)

    result = {"l1": [], "l10": [], "l2": []}

    l1_codes = shorts[:top_n_l1]
    n1 = ws.subscribe_l1(l1_codes)
    result["l1"] = l1_codes[:n1]

    l10_codes = shorts[:top_n_l10]
    n10 = ws.subscribe_l10(l10_codes)
    result["l10"] = l10_codes[:n10]

    l2_codes = shorts[:top_n_l2]
    n2 = ws.subscribe_l2(l2_codes)
    result["l2"] = l2_codes[:n2]

    print(f"[jvQuant] 分层订阅: L1={len(result['l1'])} L10={len(result['l10'])} "
          f"L2={len(result['l2'])} | 今日累计{ws.subscribed_count}只/{ws.daily_cost:.1f}元")
    return result


if __name__ == "__main__":
    print("jvQuant WebSocket 客户端测试")
    print(f"Token: {'已配置' if TOKEN else '未配置!'}")
    print(f"报警阈值: {BUDGET_ALERT}元")

    ws = _get_ws()
    print(f"连接状态: {ws.is_connected()}")

    # 测试订阅
    test_codes = ["600519", "000001"]
    n = ws.subscribe_l1(test_codes)
    print(f"L1订阅: +{n}只")

    time.sleep(3)
    for code in test_codes:
        mkt = ws.get_market(code)
        ready = ws.is_ready(code)
        print(f"  {code}: ready={ready}, price={mkt.get('last') if mkt else 'N/A'}")

    print(f"统计: {ws.stats()}")
    ws.disconnect()
