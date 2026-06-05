"""
l2api 沪深Level2实时行情客户端
=================================
TCP长连接, 3通道推送: Market(行情快照+十档盘口), Order(逐笔委托), Tran(逐笔成交)
本地聚合分钟K线, 线程安全缓存, 动态订阅管理。

用法:
  from scripts.l2_client import L2Client

  client = L2Client(account="xxx", password="xxx")
  client.start()                              # 启动3通道连接
  client.subscribe(["000001.SZ", "600519.SH"]) # 订阅候选股
  # ... 等待数据累积 (3-5秒)
  market = client.get_market("000001.SZ")      # 获取最新行情快照
  kline = client.get_minute_kline("000001.SZ") # 获取本地聚合的分钟K线
  client.unsubscribe(["000001.SZ"])            # 取消订阅
  client.stop()                                # 断开连接
"""

import json
import os
import queue
import socket
import threading
import time
import re
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# ============================================================
# 配置
# ============================================================

SERVER_HOST = "dy1.l2api.cn"
PORT_TYPE_MAP = {
    18100: "Market",
    18103: "Order",
    18105: "Tran",
}
NO_DATA_TIMEOUT = 10  # 无数据超时重连(秒)
RECONNECT_DELAY = 5    # 重连间隔(秒)
PKG_PATTERN = re.compile(r"<([^>]+)>")

# 股票代码标准化: 600519 → 600519.SH, 000001 → 000001.SZ
def normalize_code(code: str) -> str:
    if "." in code:
        return code
    if code.startswith("6"):
        return f"{code}.SH"
    return f"{code}.SZ"


def to_price(raw: str) -> float:
    """将l2api价格字段(×10000整数)转为float元"""
    try:
        return float(raw) / 10000.0
    except (ValueError, TypeError):
        return 0.0


def to_volume(raw: str) -> int:
    """将l2api成交量字段转为int股"""
    try:
        return int(raw)
    except (ValueError, TypeError):
        return 0


# ============================================================
# 数据解析
# ============================================================

def parse_market_record(fields):
    def safe(i):
        return fields[i] if len(fields) > i else ""

    return {
        "pack_no": safe(0), "market_code": safe(1), "symbol": safe(2),
        "trade_date": safe(3), "time": safe(4), "status": safe(5),
        "prev_close": safe(6), "open": safe(7), "high": safe(8),
        "low": safe(9), "last": safe(10),
        "ask_price": [safe(i) for i in range(11, 21)],
        "ask_qty":   [safe(i) for i in range(21, 31)],
        "bid_price": [safe(i) for i in range(31, 41)],
        "bid_qty":   [safe(i) for i in range(41, 51)],
        "trade_count": safe(51), "trade_volume": safe(52),
        "trade_amount": safe(53), "total_bid_volume": safe(54),
        "total_ask_volume": safe(55), "avg_bid_price": safe(56),
        "avg_ask_price": safe(57), "limit_up": safe(58),
        "limit_down": safe(59), "total_buy_orders": safe(60),
        "total_sell_orders": safe(61), "buy_cancel_orders": safe(62),
        "buy_cancel_volume": safe(63), "sell_cancel_orders": safe(64),
        "sell_cancel_volume": safe(65),
    }


def parse_order_record(fields):
    return {
        "pack_no": fields[0] if len(fields) > 0 else "",
        "market_code": fields[1] if len(fields) > 1 else "",
        "symbol": fields[2] if len(fields) > 2 else "",
        "trade_date": fields[3] if len(fields) > 3 else "",
        "time": fields[4] if len(fields) > 4 else "",
        "order_no": fields[5] if len(fields) > 5 else "",
        "order_price": fields[6] if len(fields) > 6 else "",
        "order_qty": fields[7] if len(fields) > 7 else "",
        "order_type": fields[8] if len(fields) > 8 else "",
        "order_bs": fields[9] if len(fields) > 9 else "",
        "orig_order_no": fields[10] if len(fields) > 10 else "",
        "seq_no": fields[11] if len(fields) > 11 else "",
        "channel_no": fields[12] if len(fields) > 12 else "",
    }


def parse_tran_record(fields):
    return {
        "pack_no": fields[0] if len(fields) > 0 else "",
        "market_code": fields[1] if len(fields) > 1 else "",
        "symbol": fields[2] if len(fields) > 2 else "",
        "trade_date": fields[3] if len(fields) > 3 else "",
        "time": fields[4] if len(fields) > 4 else "",
        "trade_no": fields[5] if len(fields) > 5 else "",
        "trade_price": fields[6] if len(fields) > 6 else "",
        "trade_qty": fields[7] if len(fields) > 7 else "",
        "trade_amount": fields[8] if len(fields) > 8 else "",
        "bs_flag": fields[9] if len(fields) > 9 else "",
        "trade_type": fields[10] if len(fields) > 10 else "",
        "orig_no": fields[11] if len(fields) > 11 else "",
        "ask_order_seq": fields[12] if len(fields) > 12 else "",
        "bid_order_seq": fields[13] if len(fields) > 13 else "",
    }


def parse_payload(data_type: str, payload: str) -> list[dict]:
    payload = payload.strip()
    if not payload:
        return []

    parts = [p for p in payload.split("#") if p]
    if not parts:
        return []

    parser = {"Market": parse_market_record, "Order": parse_order_record,
              "Tran": parse_tran_record}.get(data_type)
    if not parser:
        return []

    records = []
    pack_no = None
    for idx, p in enumerate(parts):
        fields = p.split(",")
        if idx == 0 and fields:
            pack_no = fields[0]
        elif pack_no is not None:
            fields = [pack_no] + fields
        records.append(parser(fields))
    return records


# ============================================================
# 协议命令
# ============================================================

def _cmd_login(account, password):
    return f"<DL,{account},{password}>".encode()

def _cmd_sub(account, password, symbol):
    return f"<DY2,{account},{password},{symbol}>".encode()

def _cmd_unsub(account, password, symbol):
    return f"<QXDY2,{account},{password},{symbol}>".encode()

def _cmd_query(account, password):
    return f"<CXDY2,{account},{password}>".encode()


# ============================================================
# 分钟K线聚合器
# ============================================================

class MinuteKlineAggregator:
    """从逐笔成交(Tran)本地聚合分钟K线"""

    def __init__(self, max_bars=240):
        self.max_bars = max_bars
        self._bars: dict[str, list[dict]] = {}  # {symbol: [{time, open, high, low, close, volume, amount}]}
        self._lock = threading.Lock()

    def feed(self, symbol: str, tran: dict):
        """喂入一笔Tran数据"""
        ts = tran.get("time", "")
        if len(ts) < 4:
            return
        minute_key = ts[:4]  # HHMM

        price = to_price(tran.get("trade_price", "0"))
        qty = to_volume(tran.get("trade_qty", "0"))
        amount = to_price(tran.get("trade_amount", "0"))  # amount也是×10000
        if price <= 0:
            return

        with self._lock:
            bars = self._bars.setdefault(symbol, [])
            if bars and bars[-1]["time"] == minute_key:
                bar = bars[-1]
                bar["high"] = max(bar["high"], price)
                bar["low"] = min(bar["low"], price)
                bar["close"] = price
                bar["volume"] += qty
                bar["amount"] += amount
            else:
                bars.append({
                    "time": minute_key,
                    "open": price, "high": price,
                    "low": price, "close": price,
                    "volume": qty, "amount": amount,
                })
                if len(bars) > self.max_bars:
                    bars.pop(0)

    def get_bars(self, symbol: str, n: int = 60) -> list[dict]:
        with self._lock:
            bars = self._bars.get(symbol, [])
            return bars[-n:] if n else list(bars)

    def get_vwap(self, symbol: str, n: int = 0) -> float:
        """计算VWAP (成交量加权均价)"""
        bars = self.get_bars(symbol, n)
        total_vol = sum(b["volume"] for b in bars)
        if total_vol == 0:
            return 0.0
        return sum(b["amount"] for b in bars) / total_vol

    def clear_symbol(self, symbol: str):
        with self._lock:
            self._bars.pop(symbol, None)


# ============================================================
# 数据缓存 (线程安全)
# ============================================================

class DataCache:
    """线程安全的最新数据缓存"""

    def __init__(self):
        self._lock = threading.RLock()
        self._market: dict[str, dict] = {}    # {symbol: latest_market_record}
        self._order_book: dict[str, dict] = {} # {symbol: latest_order_record}
        self._market_ts: dict[str, float] = {} # {symbol: last_update_timestamp}
        self._subscribed: set[str] = set()

    def update_market(self, rec: dict):
        symbol = rec.get("symbol", "")
        with self._lock:
            self._market[symbol] = rec
            self._market_ts[symbol] = time.time()

    def get_market(self, symbol: str, max_age: float = 5.0) -> Optional[dict]:
        with self._lock:
            ts = self._market_ts.get(symbol, 0)
            if time.time() - ts > max_age:
                return None
            return self._market.get(symbol)

    def set_subscribed(self, symbols: set[str]):
        with self._lock:
            self._subscribed = symbols

    def get_subscribed(self) -> set[str]:
        with self._lock:
            return set(self._subscribed)


# ============================================================
# 主客户端
# ============================================================

class L2Client:
    """l2api Level2 实时行情客户端"""

    def __init__(self, account: str, password: str, host: str = SERVER_HOST):
        self.account = account
        self.password = password
        self.host = host
        self._running = False
        self._threads: list[threading.Thread] = []
        self.cache = DataCache()
        self.kline = MinuteKlineAggregator()
        self._cmd_queues: dict[int, queue.Queue] = {}
        self.debug = False  # 调试模式: 打印原始数据包

        # ── 健康状态追踪 ──
        # {port: {"connected_at": float|None, "last_data_at": float|None,
        #         "reconnect_times": list[float], "connected": bool}}
        self._channel_state: dict[int, dict] = {}
        self._health_lock = threading.Lock()
        for port in PORT_TYPE_MAP:
            self._channel_state[port] = {
                "connected_at": None,
                "last_data_at": None,
                "reconnect_times": [],
                "connected": False,
            }

    # ---- 生命周期 ----

    def start(self):
        """启动3通道TCP连接和数据处理线程"""
        if self._running:
            return
        self._running = True
        ports = list(PORT_TYPE_MAP.keys())
        logger.info(f"l2api 启动 {len(ports)} 通道: {ports}")

        for port in ports:
            self._cmd_queues[port] = queue.Queue()
            t = threading.Thread(target=self._recv_loop, args=(port,), daemon=True)
            t.start()
            self._threads.append(t)
        logger.info("l2api 所有通道已启动")

    def stop(self):
        """停止所有连接"""
        self._running = False
        logger.info("l2api 正在停止...")
        for t in self._threads:
            t.join(timeout=5)
        self._threads.clear()
        logger.info("l2api 已停止")

    # ---- 健康检测 ----

    def _on_channel_connected(self, port: int):
        """记录通道连接成功（由 _recv_loop 调用）"""
        now = time.time()
        with self._health_lock:
            state = self._channel_state[port]
            state["connected_at"] = now
            state["connected"] = True
            # 清理超过 120s 的旧重连记录
            state["reconnect_times"] = [
                t for t in state["reconnect_times"] if now - t < 120
            ]

    def _on_channel_disconnected(self, port: int):
        """记录通道断开（由 _recv_loop 调用）"""
        now = time.time()
        with self._health_lock:
            state = self._channel_state[port]
            state["connected"] = False
            state["reconnect_times"].append(now)
            # 清理超过 120s 的旧重连记录
            state["reconnect_times"] = [
                t for t in state["reconnect_times"] if now - t < 120
            ]

    def _on_channel_data(self, port: int):
        """记录通道收到数据（由 _recv_loop 调用）"""
        now = time.time()
        with self._health_lock:
            self._channel_state[port]["last_data_at"] = now

    def is_healthy(self) -> bool:
        """检查 L2 连接是否健康。

        健康条件：至少有一个通道满足以下全部条件：
        - 已连接
        - 最近 60s 内重连次数 < 3（非震荡）
        - 最近 30s 内有数据到达（或处于初始启动窗口：连接 <20s 且重连 <2 次）

        所有通道均不健康时返回 False。
        """
        now = time.time()
        with self._health_lock:
            for port, state in self._channel_state.items():
                recent_reconnects = sum(
                    1 for t in state["reconnect_times"] if now - t < 60
                )
                last_data_age = (
                    now - state["last_data_at"] if state["last_data_at"] else 9999
                )
                connected_age = (
                    now - state["connected_at"] if state["connected_at"] else 9999
                )

                # 震荡检测：60s 内重连 >=3 次 → 不健康
                if recent_reconnects >= 3:
                    continue

                # 数据超时检测：30s 无数据
                if last_data_age > 30:
                    # 初始启动窗口：连接 <20s 且重连 <2 → 放行
                    if connected_age < 20 and recent_reconnects < 2:
                        return True
                    continue

                # 通道健康
                return True

        return False

    def health_summary(self) -> dict:
        """返回各通道健康状态摘要（供日志/调试）"""
        now = time.time()
        channels = {}
        with self._health_lock:
            for port, state in self._channel_state.items():
                type_name = PORT_TYPE_MAP.get(port, str(port))
                channels[type_name] = {
                    "connected": state["connected"],
                    "last_data_age_s": (
                        round(now - state["last_data_at"], 1)
                        if state["last_data_at"] else None
                    ),
                    "recent_reconnects": sum(
                        1 for t in state["reconnect_times"] if now - t < 60
                    ),
                }
        return {"healthy": self.is_healthy(), "channels": channels}

    # ---- 订阅管理 ----

    def subscribe(self, codes: list[str]):
        """订阅一批股票 (向所有3个端口投递订阅命令)"""
        codes = [normalize_code(c) for c in codes]
        current = self.cache.get_subscribed()
        new_codes = [c for c in codes if c not in current]
        if not new_codes:
            return

        logger.info(f"l2api 新增订阅 {len(new_codes)} 只: {new_codes[:5]}...")
        for port in PORT_TYPE_MAP:
            q = self._cmd_queues.get(port)
            if q:
                for c in new_codes:
                    q.put(("sub", c))

        self.cache.set_subscribed(current | set(new_codes))

    def unsubscribe(self, codes: list[str]):
        """取消订阅一批股票"""
        codes = [normalize_code(c) for c in codes]
        current = self.cache.get_subscribed()
        remove_codes = [c for c in codes if c in current]
        if not remove_codes:
            return

        logger.info(f"l2api 取消订阅 {len(remove_codes)} 只")
        for port in PORT_TYPE_MAP:
            q = self._cmd_queues.get(port)
            if q:
                for c in remove_codes:
                    q.put(("unsub", c))

        self.cache.set_subscribed(current - set(remove_codes))

    def sync_subscriptions(self, codes: list[str]):
        """同步订阅列表: 新增未订阅的, 取消已退出的"""
        codes = [normalize_code(c) for c in codes]
        target = set(codes)
        current = self.cache.get_subscribed()
        to_add = target - current
        to_remove = current - target

        if to_remove:
            self.unsubscribe(list(to_remove))
        if to_add:
            self.subscribe(list(to_add))

    # ---- 数据查询 ----

    def get_market(self, code: str, max_age: float = 5.0) -> Optional[dict]:
        """获取最新行情快照 (含十档盘口)"""
        return self.cache.get_market(normalize_code(code), max_age)

    def get_minute_kline(self, code: str, n: int = 60) -> list[dict]:
        """获取本地聚合的分钟K线"""
        return self.kline.get_bars(normalize_code(code), n)

    def get_vwap(self, code: str) -> float:
        """获取VWAP"""
        return self.kline.get_vwap(normalize_code(code))

    def is_ready(self, code: str) -> bool:
        """数据是否就绪 (订阅后有数据到达)"""
        return self.get_market(code) is not None

    # ---- 内部实现 ----

    def _recv_loop(self, port: int):
        """单端口接收循环 (带自动重连, 从队列取订阅命令)"""
        data_type = PORT_TYPE_MAP[port]
        cmd_q = self._cmd_queues.get(port)
        if cmd_q is None:
            logger.error(f"l2api [{data_type}] 未找到命令队列，线程退出")
            return

        consecutive_failures = 0
        while self._running:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1)
            try:
                sock.connect((self.host, port))
                logger.info(f"l2api [{data_type}] 已连接 {self.host}:{port}")
                self._on_channel_connected(port)

                # 登录
                sock.sendall(_cmd_login(self.account, self.password))
                time.sleep(0.3)
                try:
                    sock.recv(4096)
                except socket.timeout:
                    pass

                # 重订阅当前所有股票
                for symbol in self.cache.get_subscribed():
                    sock.sendall(_cmd_sub(self.account, self.password, symbol))
                    time.sleep(0.03)

                last_recv = time.time()

                while self._running:
                    # 处理待发送的订阅/取消订阅命令
                    try:
                        while True:
                            action, symbol = cmd_q.get_nowait()
                            if action == "sub":
                                sock.sendall(_cmd_sub(self.account, self.password, symbol))
                            elif action == "unsub":
                                sock.sendall(_cmd_unsub(self.account, self.password, symbol))
                    except queue.Empty:
                        pass

                    # 超时检测
                    if time.time() - last_recv > NO_DATA_TIMEOUT:
                        logger.warning(f"l2api [{data_type}] {NO_DATA_TIMEOUT}s 无数据，重连")
                        break

                    try:
                        data = sock.recv(65536)
                        if not data:
                            logger.warning(f"l2api [{data_type}] 服务器断开")
                            break
                        last_recv = time.time()
                        self._process_data(data_type, data)
                        self._on_channel_data(port)
                    except socket.timeout:
                        continue
                    except Exception as e:
                        logger.error(f"l2api [{data_type}] 接收异常: {e}")
                        break

            except Exception as e:
                consecutive_failures += 1
                backoff = min(RECONNECT_DELAY * (2 ** (consecutive_failures - 1)), 300)
                logger.error(f"l2api [{data_type}] 连接失败: {e} (第{consecutive_failures}次, {backoff}s后重连)")
            else:
                consecutive_failures = 0
            finally:
                self._on_channel_disconnected(port)
                try:
                    sock.close()
                except Exception:
                    pass

            if self._running:
                sleep_for = min(RECONNECT_DELAY * (2 ** (consecutive_failures - 1)), 300) if consecutive_failures > 0 else RECONNECT_DELAY
                time.sleep(sleep_for)

        logger.info(f"l2api [{data_type}] 线程退出")

    def _process_data(self, data_type: str, data: bytes):
        """解析并缓存收到的数据"""
        try:
            text = data.decode(errors="ignore")
        except Exception:
            return

        if self.debug:
            # 截断过长数据
            preview = text[:500] + "..." if len(text) > 500 else text
            print(f"[DEBUG {data_type}] {preview}")

        for m in PKG_PATTERN.finditer(text):
            payload = m.group(1)
            if payload in ("HeartBeat", "欢迎") or payload.startswith(("DL,", "KICK,")):
                if self.debug:
                    print(f"[DEBUG {data_type}] 控制消息: {payload[:80]}")
                continue

            records = parse_payload(data_type, payload)
            if self.debug and records:
                print(f"[DEBUG {data_type}] 解析 {len(records)} 条记录, 首条: {records[0]}")

            if data_type == "Market":
                for rec in records:
                    code = rec.get("market_code", rec.get("symbol", ""))
                    if code:
                        rec["symbol"] = normalize_code(code)
                        self.cache.update_market(rec)

            elif data_type == "Tran":
                for rec in records:
                    code = rec.get("market_code", rec.get("symbol", ""))
                    if code:
                        rec["symbol"] = normalize_code(code)
                        self.kline.feed(normalize_code(code), rec)


# ============================================================
# 全局单例
# ============================================================
# 本地代理客户端 — 当 l2_daemon 运行时自动使用, 避免多进程互踢
# ============================================================

L2_DAEMON_HOST = "127.0.0.1"
L2_DAEMON_PORT = 18999
L2_DAEMON_PID_FILE = "/root/maneki-agent/plays/limit_up/data/.l2_daemon.pid"


class L2ProxyClient:
    """L2 代理客户端 — 连接 l2_daemon 进程, 复用其 L2 连接。

    与 L2Client 接口兼容, pipeline/watchdog 无需修改即可透明使用。
    """

    def __init__(self, host=L2_DAEMON_HOST, port=L2_DAEMON_PORT):
        self._host = host
        self._port = port
        self._sock = None
        self._lock = threading.Lock()
        self._running = False
        self.cache = _ProxyCache()
        self.kline = _ProxyKline()
        self.debug = False

    def start(self):
        if self._running: return
        self._running = True

    def stop(self):
        self._running = False
        with self._lock:
            if self._sock:
                try: self._sock.close()
                except: pass
                self._sock = None

    def _send(self, cmd: str) -> str:
        with self._lock:
            if self._sock is None:
                self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self._sock.settimeout(3)
                self._sock.connect((self._host, self._port))
            try:
                self._sock.sendall((cmd + "\n").encode())
                buf = b""
                while b"\n" not in buf:
                    data = self._sock.recv(4096)
                    if not data: raise ConnectionError("daemon disconnected")
                    buf += data
                return buf.split(b"\n", 1)[0].decode()
            except Exception:
                try: self._sock.close()
                except: pass
                self._sock = None
                raise

    def subscribe(self, codes: list[str]):
        if not codes: return
        self._send(f"SUB {' '.join(codes)}")
        self.cache._subbed.update(normalize_code(c) for c in codes)

    def unsubscribe(self, codes: list[str]):
        if not codes: return
        self._send(f"UNSUB {' '.join(codes)}")
        self.cache._subbed.difference_update(normalize_code(c) for c in codes)

    def sync_subscriptions(self, codes: list[str]):
        codes = [normalize_code(c) for c in codes]
        target = set(codes)
        current = self.cache.get_subscribed()
        self.subscribe(list(target - current))
        self.unsubscribe(list(current - target))

    def get_market(self, code: str, max_age: float = 5.0):
        try:
            resp = self._send(f"MARKET {normalize_code(code)}")
            if resp == "NULL": return None
            return json.loads(resp)
        except: return None

    def get_vwap(self, code: str):
        try: return float(self._send(f"VWAP {normalize_code(code)}"))
        except: return None

    def get_minute_kline(self, code: str, n: int = 60):
        try: return json.loads(self._send(f"KLINE {normalize_code(code)} {n}"))
        except: return []

    def is_ready(self, code: str):
        try: return self._send(f"IS_READY {normalize_code(code)}") == "1"
        except: return False

    def is_healthy(self):
        try: return json.loads(self._send("HEALTH")).get("healthy", False)
        except: return False

    def health_summary(self):
        try: return json.loads(self._send("HEALTH"))
        except: return {"healthy": False, "channels": {}}


class _ProxyCache:
    def __init__(self): self._subbed: set[str] = set()
    def get_subscribed(self): return self._subbed
    def set_subscribed(self, s): self._subbed = s


class _ProxyKline:
    def get_bars(self, *args, **kwargs): return []
    def get_vwap(self, *args, **kwargs): return None


# ── 全局单例 ──
_client: Optional[L2Client | L2ProxyClient] = None
_daemon_checked: bool = False


def _check_daemon() -> bool:
    """检查 L2 daemon 是否运行"""
    global _daemon_checked
    if _daemon_checked: return isinstance(_client, L2ProxyClient)
    _daemon_checked = True
    pid_file = Path(L2_DAEMON_PID_FILE)
    if not pid_file.exists(): return False
    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, 0)
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1)
        s.connect((L2_DAEMON_HOST, L2_DAEMON_PORT))
        s.sendall(b"PING\n")
        resp = s.recv(1024)
        s.close()
        return b"PONG" in resp
    except: return False


def get_client(account: str = "", password: str = "") -> L2Client | L2ProxyClient:
    global _client
    if _client is None:
        if _check_daemon():
            logger.info("l2api 检测到 daemon, 使用代理连接")
            _client = L2ProxyClient()
            _client.start()
        else:
            if not account or not password:
                raise ValueError("首次初始化需要提供 account 和 password")
            _client = L2Client(account=account, password=password)
    return _client


def has_client() -> bool:
    return _client is not None