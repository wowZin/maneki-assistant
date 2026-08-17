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
import websocket

from scripts.audit import record as _audit_record, format_error as _audit_format_error

PROJECT_DIR = Path(__file__).resolve().parent.parent

# 消息接收抽样计数器（每 100 条 record 一次，避免刷屏）
_msg_counters = defaultdict(int)
_MSG_SAMPLE_INTERVAL = 100


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

    def _ensure_connection(self) -> bool:
        """Verify connection is alive; reconnect if dropped.

        The jvQuant WS can silently disconnect during long idle periods
        (e.g. Tushare data fetch).  _running stays True even after drop.
        We test with a lightweight "list" command to detect dead sockets.
        """
        if not self._running:
            return self.connect()
        try:
            if self._ws:
                # Probe: "list" is a noop if connected, raises if dead
                self._ws.cmd("list")
            return True
        except (websocket.WebSocketConnectionClosedException,
                websocket.WebSocketTimeoutException,
                ConnectionError, OSError, TimeoutError, Exception):
            print(f"[jvQuant WS] 连接已断开，重新连接...")
            self._running = False
            self._l1_codes.clear()
            self._l10_codes.clear()
            self._l2_codes.clear()
            return self.connect()

    def connect(self) -> bool:
        """建立 WebSocket 连接"""
        if self._running:
            return True

        self._check_day_reset()

        ws = jvQuant.websocket_client
        t0 = time.perf_counter()
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
            latency = (time.perf_counter() - t0) * 1000
            _audit_record("jvquant_ws", "connect", ok=True, items=1, latency_ms=latency)
            print(f"[jvQuant WS] 已连接")
            return True
        except Exception as e:
            latency = (time.perf_counter() - t0) * 1000
            _audit_record("jvquant_ws", "connect", ok=False, items=0,
                          latency_ms=latency, extra=_audit_format_error(e))
            print(f"[jvQuant WS] 连接失败: {e}")
            return False

    def disconnect(self) -> None:
        if self._ws and self._running:
            try:
                self._ws.disconnect()
                _audit_record("jvquant_ws", "disconnect", ok=True, items=1)
            except Exception as e:
                _audit_record("jvquant_ws", "disconnect", ok=False,
                              extra=_audit_format_error(e))
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
        """内部订阅方法（自动重连 WAL-RETRY: 1次）"""
        return self._subscribe_with_retry(level, codes, retry_count=0)

    def _subscribe_with_retry(self, level: str, codes: list[str],
                               retry_count: int) -> int:
        max_retries = 1
        if not self._running:
            self.connect()
        if not self._running:
            _audit_record("jvquant_ws", "subscribe", ok=False, items=0,
                          extra=f"ERR:NotConnected|level={level}")
            return 0

        attr_map = {"l1": "_l1_codes", "l10": "_l10_codes", "l2": "_l2_codes"}
        method_map = {"l1": self._ws.add_lv1, "l10": self._ws.add_lv10,
                      "l2": self._ws.add_lv2}
        code_set = getattr(self, attr_map[level])

        new_codes = [c for c in codes if c not in code_set and len(c) == 6]
        if not new_codes:
            _audit_record("jvquant_ws", "subscribe", ok=True, items=0,
                          extra=f"level={level} skip=dup")
            return 0

        t0 = time.perf_counter()
        try:
            with self._lock:
                method_map[level](new_codes)
                code_set.update(new_codes)
                self._all_subscribed_today.update(new_codes)
            latency = (time.perf_counter() - t0) * 1000
            _audit_record("jvquant_ws", "subscribe", ok=True,
                          items=len(new_codes), latency_ms=latency,
                          extra=f"level={level}")
        except (websocket.WebSocketConnectionClosedException, OSError) as e:
            latency = (time.perf_counter() - t0) * 1000
            _audit_record("jvquant_ws", "subscribe", ok=False, items=0,
                          latency_ms=latency,
                          extra=_audit_format_error(e, {"level": level}))
            if retry_count < max_retries:
                print(f"[jvQuant WS] 订阅失败({type(e).__name__}), 重连重试...")
                self._running = False
                self._l1_codes.clear()
                self._l10_codes.clear()
                self._l2_codes.clear()
                self.connect()
                return self._subscribe_with_retry(level, codes,
                                                  retry_count + 1)
            raise
        except Exception as e:
            latency = (time.perf_counter() - t0) * 1000
            _audit_record("jvquant_ws", "subscribe", ok=False, items=0,
                          latency_ms=latency,
                          extra=_audit_format_error(e, {"level": level}))
            raise

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

        t0 = time.perf_counter()
        with self._lock:
            try:
                method_map[level](remove)
                latency = (time.perf_counter() - t0) * 1000
                _audit_record("jvquant_ws", "unsubscribe", ok=True,
                              items=len(remove), latency_ms=latency,
                              extra=f"level={level}")
            except Exception as e:
                latency = (time.perf_counter() - t0) * 1000
                _audit_record("jvquant_ws", "unsubscribe", ok=False,
                              items=0, latency_ms=latency,
                              extra=_audit_format_error(e, {"level": level}))
            code_set.difference_update(remove)

    # ── 数据回调 ──

    def _on_l1(self, lv1) -> None:
        code = lv1.code if hasattr(lv1, 'code') else ""
        if code:
            self._cache[code].update_l1(lv1)
            _msg_counters["l1"] += 1
            if _msg_counters["l1"] % _MSG_SAMPLE_INTERVAL == 0:
                _audit_record("jvquant_ws", "msg_l1", ok=True,
                              items=_MSG_SAMPLE_INTERVAL,
                              extra=f"total={_msg_counters['l1']}")

    def _on_l10(self, lv10) -> None:
        code = lv10.code if hasattr(lv10, 'code') else ""
        if code:
            self._cache[code].update_l10(lv10)
            _msg_counters["l10"] += 1
            if _msg_counters["l10"] % _MSG_SAMPLE_INTERVAL == 0:
                _audit_record("jvquant_ws", "msg_l10", ok=True,
                              items=_MSG_SAMPLE_INTERVAL,
                              extra=f"total={_msg_counters['l10']}")

    def _on_l2(self, lv2) -> None:
        code = lv2.code if hasattr(lv2, 'code') else ""
        if not code:
            return
        deals = lv2.deal_list if hasattr(lv2, 'deal_list') else []
        for deal in deals:
            self._cache[code].add_tick(deal)
        _msg_counters["l2"] += 1
        if _msg_counters["l2"] % _MSG_SAMPLE_INTERVAL == 0:
            _audit_record("jvquant_ws", "msg_l2", ok=True,
                          items=_MSG_SAMPLE_INTERVAL,
                          extra=f"total={_msg_counters['l2']} deals={len(deals)}")

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
#
# ★ 2026-08-17 重构：daemon_* 系列全部改读共享内存 /dev/shm/ws_snap.json，
# 独立进程调用**不再建 WS 连接**——jvQuant 同 token 单连接，任何新连接都会
# 踢掉 ws_daemon 主连接（互踢根因：pipeline_feishu/stock_analyzer/pan_analyzer
# 调 _get_ws() 在各自进程建第二个连接 → 行情断 30s-1min+，watchdog 盲区）。
# 订阅类（daemon_subscribe*）改写 ws_sub.json，由 ws_daemon 每 2s 增量消费。
# _get_ws() 仅保留给 ws_daemon 进程内部/主动分析工具（无主连接时）使用。
# ═══════════════════════════════════════════════════════════════

import os as _os  # noqa: E402

_SHM_DIR = Path("/dev/shm")
WS_SNAP_FILE = _SHM_DIR / "ws_snap.json"
WS_SUB_FILE = _SHM_DIR / "ws_sub.json"
_SNAP_FRESH_SEC = 30.0  # 快照 mtime 超 30s 视为 ws_daemon 不在/断链


def _short6(code: str) -> str:
    return code.replace(".SH", "").replace(".SZ", "")


def _snap_data() -> dict:
    """读 ws_daemon 共享内存快照（仅新鲜时返回，否则 {}）。"""
    try:
        if WS_SNAP_FILE.exists() \
                and (time.time() - WS_SNAP_FILE.stat().st_mtime) < _SNAP_FRESH_SEC:
            return json.loads(WS_SNAP_FILE.read_text())
    except Exception:
        pass
    return {}


def _snap_alive() -> bool:
    try:
        return WS_SNAP_FILE.exists() \
            and (time.time() - WS_SNAP_FILE.stat().st_mtime) < _SNAP_FRESH_SEC
    except Exception:
        return False


def _load_sub() -> tuple[list[str], list[str]]:
    try:
        d = json.loads(WS_SUB_FILE.read_text())
        return d.get("shorts", []), d.get("l2_shorts", [])
    except Exception:
        return [], []


def _write_sub(shorts: list[str], l2_shorts: list[str]) -> None:
    """原子写 ws_sub.json（多进程共写，tmp 带 pid 防并发覆盖——state.json 同款教训）。"""
    try:
        WS_SUB_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = WS_SUB_FILE.with_name(f"ws_sub.json.tmp.{_os.getpid()}")
        tmp.write_text(json.dumps({"shorts": shorts, "l2_shorts": l2_shorts},
                                  ensure_ascii=False))
        tmp.rename(WS_SUB_FILE)
    except Exception:
        pass


def _update_sub(add_l1: list[str] | None = None, add_l2: list[str] | None = None,
                remove: list[str] | None = None) -> None:
    """订阅变更（写 ws_sub.json，ws_daemon 增量消费）。"""
    shorts, l2s = _load_sub()
    rm = {_short6(c) for c in (remove or [])}
    shorts = [s for s in shorts if s not in rm]
    l2s = [s for s in l2s if s not in rm]
    for c in (add_l1 or []):
        s = _short6(c)
        if s and s not in shorts:
            shorts.append(s)
    for c in (add_l2 or []):
        s = _short6(c)
        if s and s not in l2s:
            l2s.append(s)
    _write_sub(shorts, l2s)


_ws_client: JvQuantWSClient | None = None


def _get_ws() -> JvQuantWSClient:
    """进程内 WS 连接（⚠️ 仅 ws_daemon 进程内部使用）。

    独立进程（pipeline_feishu/stock_analyzer/pan_analyzer/脚本）调用会新建
    第二个 WS 连接 → 踢掉 ws_daemon 主连接（同 token 单连接）。2026-08-17
    起 daemon_* 系列已改读共享内存，禁止独立进程再走 _get_ws()。
    """
    global _ws_client
    if _ws_client is None:
        _ws_client = JvQuantWSClient()
        _ws_client.connect()
    else:
        # Reconnect if the underlying WebSocket is dead but _running is still True
        _ws_client._ensure_connection()
    return _ws_client


# ── 兼容 l2_daemon_client 接口 ──

def daemon_alive() -> bool:
    """检查 ws_daemon 是否在跑（快照新鲜度，不再建 WS 连接）。"""
    return _snap_alive()


def daemon_subscribe(codes: list[str]) -> None:
    """订阅 L1（写 ws_sub.json，由 ws_daemon 增量订阅）。"""
    _update_sub(add_l1=codes)


def daemon_subscribe_l10(codes: list[str]) -> None:
    """订阅 L10（ws_daemon 仅 l1/l2 通道，L10 并入 L1——L1 快照含 10 档盘口）。"""
    _update_sub(add_l1=codes)


def daemon_subscribe_l2(codes: list[str]) -> None:
    """订阅 L2（写 ws_sub.json l2_shorts，ws_daemon 增量订阅）。"""
    _update_sub(add_l2=codes)


def daemon_unsubscribe(codes: list[str]) -> None:
    """退订所有级别（写 ws_sub.json 移除）。"""
    _update_sub(remove=codes)


def daemon_get_market(code: str) -> dict | None:
    """获取盘口快照（读共享内存，不建连）。"""
    return _snap_data().get(_short6(code))


def daemon_get_vwap(code: str) -> float | None:
    """获取 VWAP（读共享内存）。"""
    v = _snap_data().get(f"{_short6(code)}_vwap")
    return float(v) if v is not None else None


def daemon_get_kline(code: str, n: int = 5) -> list[dict]:
    """获取分钟K线——共享内存无 K 线（L2 逐笔在 ws_daemon 进程内存），返回空。"""
    return []


def daemon_is_ready(code: str) -> bool:
    """数据是否就绪（快照含该 code 且新鲜）。"""
    return _short6(code) in _snap_data()


def daemon_stats() -> dict:
    """统计（读共享内存 + ws_sub.json，无计费数据返回 0 元）。"""
    snap = _snap_data()
    l1_count = len([k for k in snap if len(k) == 6 and not k.endswith("_vwap")])
    shorts, l2s = _load_sub()
    return {"l1_count": l1_count, "l10_count": 0, "l2_count": len(l2s),
            "total_subscribed_today": len(shorts), "daily_cost": 0.0}


def daemon_health() -> str:
    """健康检查（兼容旧接口）。"""
    if daemon_alive():
        s = daemon_stats()
        return (f"OK|jvQuant WS|L1={s['l1_count']} L10={s['l10_count']} "
                f"L2={s['l2_count']}|今日{s['total_subscribed_today']}只"
                f"={s['daily_cost']}元")
    return "DOWN|jvQuant WS disconnected"


def daemon_is_healthy() -> bool:
    """健康状态（兼容旧接口）"""
    return daemon_alive()


# ── 扩展接口（非兼容，新增） ──

def daemon_cmd(cmd: str) -> str:
    """兼容旧 l2_daemon 命令接口（全部走共享内存，不建连）

    支持的命令:
      SUB <codes>    → 写 ws_sub.json（L1 订阅）
      UNSUB <codes>  → 写 ws_sub.json（退订所有级别）
      MARKET <code>  → daemon_get_market (返回 JSON)
      VWAP <code>    → daemon_get_vwap (返回浮点数)
      HEALTH         → daemon_health()
      PING           → "PONG"
    """
    parts = cmd.strip().split()
    if not parts:
        return "ERR empty command"

    op = parts[0].upper()
    try:
        if op == "SUB":
            daemon_subscribe(parts[1:])
            return f"OK subscribed {len(parts[1:])}"

        elif op == "UNSUB":
            daemon_unsubscribe(parts[1:])
            return f"OK unsubscribed {len(parts[1:])}"

        elif op == "MARKET":
            code = parts[1]
            mkt = daemon_get_market(code)
            if mkt:
                return json.dumps(mkt, ensure_ascii=False)
            return "NULL"

        elif op == "VWAP":
            code = parts[1]
            vwap = daemon_get_vwap(code)
            return str(vwap) if vwap else "NULL"

        elif op == "HEALTH":
            return daemon_health()

        elif op == "PING":
            return "PONG"

        elif op == "NETFLOW":
            # L2 逐笔在 ws_daemon 进程内存，共享内存无此数据
            return "NULL"

        else:
            return f"ERR unknown command: {op}"

    except Exception as e:
        return f"ERR {e}"


# ── 分层订阅接口 ──

def subscribe_tiered(candidates: list[dict], top_n_l1: int = 12,
                     top_n_l10: int = 5, top_n_l2: int = 2) -> dict:
    """分层订阅：按评分/涨幅排序后，分批写 ws_sub.json（ws_daemon 增量消费）。

    Args:
        candidates: [{code, pct_chg, ...}] 候选股列表
        top_n_l1: L1 订阅前 N 只
        top_n_l10: L10 订阅前 N 只（并入 L1——快照含 10 档盘口）
        top_n_l2: L2 订阅前 N 只

    Returns:
        {l1: [...], l10: [...], l2: [...]}
    """
    sorted_candidates = sorted(candidates,
                               key=lambda x: x.get("pct_chg", 0), reverse=True)

    shorts = []
    for c in sorted_candidates:
        code = c.get("code", "")
        short = code.replace(".SH", "").replace(".SZ", "")
        if len(short) == 6:
            shorts.append(short)

    l1_codes = shorts[:top_n_l1]
    l10_codes = shorts[:top_n_l10]
    l2_codes = shorts[:top_n_l2]
    _update_sub(add_l1=l1_codes + l10_codes, add_l2=l2_codes)

    result = {"l1": l1_codes, "l10": l10_codes, "l2": l2_codes}
    print(f"[jvQuant] 分层订阅(写ws_sub): L1={len(result['l1'])} "
          f"L10={len(result['l10'])} L2={len(result['l2'])}")
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
