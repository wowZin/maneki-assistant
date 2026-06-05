"""
盯盘助手
========
基于l2api Level2实时数据 + 双引擎动量-均值回归策略，持续监控标的。
通过飞书推送买卖信号，状态持久化到本地JSON。

指令:
  盯 000001.SZ    → 开始盯盘
  停 000001.SZ    → 停止盯盘
  盯盘列表        → 查看监控列表
  清盯盘          → 全部停止
"""

import json
import os
import sys
import time
import socket
import threading
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
import requests
from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from plays.watchdog.indicators import calc_all, check_trend, check_pullback, check_entry_score, check_exit_signal
from scripts.l2_client import to_price, to_volume, normalize_code  # noqa: E402
from scripts.tu_share import call_tushare  # noqa: E402

logger = logging.getLogger(__name__)

STATE_FILE = PROJECT_DIR / "plays" / "watchdog" / "data" / "state.json"
SCAN_INTERVAL = 30  # 每30秒检查一次信号
MAX_WATCH = 5       # 同时盯盘上限5只

# ── L2 守护进程客户端 ──

_DAEMON_HOST = "127.0.0.1"
_DAEMON_PORT = 18999


def _daemon_cmd(cmd: str, timeout: int = 5) -> str:
    """向 L2 守护进程发送命令, 返回响应"""
    s = socket.create_connection((_DAEMON_HOST, _DAEMON_PORT), timeout=timeout)
    s.sendall((cmd + "\n").encode())
    resp = s.recv(8192).decode().strip()
    s.close()
    return resp


def _daemon_alive() -> bool:
    try:
        return _daemon_cmd("PING", timeout=2) == "PONG"
    except Exception:
        return False

# ---- 飞书推送 ----

def _push_feishu(text: str):
    """推送盯盘信号到飞书"""
    try:
        env_file = PROJECT_DIR / ".env"
        if env_file.exists():
            load_dotenv(env_file)
        app_id = os.getenv("FEISHU_APP_ID", "")
        app_secret = os.getenv("FEISHU_APP_SECRET", "")
        chat_id = os.getenv("FEISHU_CHAT_ID_SIGNAL", os.getenv("FEISHU_BOT_CHAT_ID", ""))

        if not app_id or not app_secret:
            logger.warning("飞书未配置 app_id/app_secret，跳过推送")
            return
        if not chat_id:
            logger.warning("飞书未配置 chat_id (FEISHU_CHAT_ID_SIGNAL/FEISHU_BOT_CHAT_ID)，跳过推送")
            return

        # 获取tenant token
        resp = requests.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": app_id, "app_secret": app_secret}, timeout=10
        )
        token_data = resp.json()
        token = token_data.get("tenant_access_token", "")
        if not token:
            logger.error(f"飞书token获取失败: {token_data}")
            return

        send_resp = requests.post(
            f"https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={
                "receive_id": chat_id,
                "msg_type": "text",
                "content": json.dumps({"text": text}),
            }, timeout=10
        )
        result = send_resp.json()
        if result.get("code") == 0:
            logger.info(f"飞书推送成功: {text[:60]}...")
        else:
            logger.error(f"飞书推送失败: code={result.get('code')} msg={result.get('msg')} text={text[:60]}...")
    except Exception as e:
        logger.error(f"飞书推送异常: {e}")


# ---- 盯盘状态管理 ----

class WatchState:
    """单只股票的盯盘状态"""

    def __init__(self, code: str, name: str = ""):
        self.code = code
        self.name = name
        self.added_at = datetime.now().isoformat()
        self.status = "watching"  # watching | signal_pending | entered
        # 日线指标缓存
        self.indicators: dict = {}
        self.last_daily_update: str = ""  # YYYYMMDD
        # 入场相关
        self.entry_price: float = 0.0
        self.entry_at: str = ""
        self.highest_since_entry: float = 0.0
        self.bars_held: int = 0
        self.signal_low: float = 0.0   # Step2触发时的最低价(做多参考)
        self.signal_high: float = 0.0  # Step2触发时的最高价(做空参考)
        self.signal_at: str = ""        # 信号触发时间
        self.avg_vol_20: float = 0.0
        # 上次推送时间(防抖)
        self.last_alert_at: str = ""

    def to_dict(self) -> dict:
        return {
            "code": self.code, "name": self.name, "added_at": self.added_at, "status": self.status,
            "entry_price": self.entry_price, "entry_at": self.entry_at,
            "highest_since_entry": self.highest_since_entry, "bars_held": self.bars_held,
            "signal_low": self.signal_low, "signal_high": self.signal_high,
            "signal_at": self.signal_at, "avg_vol_20": self.avg_vol_20,
            "last_alert_at": self.last_alert_at, "last_daily_update": self.last_daily_update,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "WatchState":
        s = cls(d["code"], d.get("name", ""))
        s.added_at = d.get("added_at", "")
        s.status = d.get("status", "watching")
        s.entry_price = d.get("entry_price", 0.0)
        s.entry_at = d.get("entry_at", "")
        s.highest_since_entry = d.get("highest_since_entry", 0.0)
        s.bars_held = d.get("bars_held", 0)
        s.signal_low = d.get("signal_low", 0.0)
        s.signal_high = d.get("signal_high", 0.0)
        s.signal_at = d.get("signal_at", "")
        s.avg_vol_20 = d.get("avg_vol_20", 0.0)
        s.last_alert_at = d.get("last_alert_at", "")
        s.last_daily_update = d.get("last_daily_update", "")
        return s


class WatchdogEngine:
    """盯盘引擎 (单例)"""

    def __init__(self):
        self._lock = threading.Lock()
        self._states: dict[str, WatchState] = {}
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._scan_count = 0  # 扫描轮次计数
        self._load_state()

    # ---- 生命周期 ----

    def start(self):
        if self._running:
            return
        if not _daemon_alive():
            logger.warning("L2 守护进程未运行，盯盘引擎无法工作")
            return
        self._running = True
        # 同步订阅历史标的
        codes = list(self._states.keys())
        if codes and _daemon_alive():
            try:
                _daemon_cmd(f"SUB {' '.join(codes)}")
                logger.info(f"已同步订阅 {len(codes)} 只历史标的")
            except Exception as e:
                logger.warning(f"订阅历史标的失败: {e}")
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        logger.info("盯盘引擎已启动")

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        self._save_state()
        logger.info("盯盘引擎已停止")

    # ---- 指令处理 ----

    def _resolve_name(self, code: str) -> str:
        """查询股票名称"""
        try:
            resp = call_tushare("stock_basic", {"ts_code": code}, "ts_code,name")
            items = resp.get("data", {}).get("items", [])
            if items and len(items[0]) > 1:
                return items[0][1]
        except Exception:
            pass
        return code

    def add(self, codes: list[str]) -> str:
        codes = [normalize_code(c) for c in codes]
        msgs = []
        init_reasons: dict[str, str] = {}
        with self._lock:
            current = len(self._states)
            for code in codes:
                if code in self._states:
                    msgs.append(f"{code} 已在盯盘中")
                    continue
                if current >= MAX_WATCH:
                    msgs.append(f"盯盘已达上限({MAX_WATCH}只)，无法添加 {code}")
                    continue
                name = self._resolve_name(code)
                st = WatchState(code, name)
                # 立即获取日线数据
                ok, reason = self._update_daily(st)
                init_reasons[code] = reason
                self._states[code] = st
                current += 1
                msgs.append(f"开始盯盘 {name}({code})")
            self._save_state()

            # 同步 L2 守护进程订阅
            if _daemon_alive():
                try:
                    _daemon_cmd(f"SUB {' '.join(self._states.keys())}")
                except Exception as e:
                    logger.warning(f"L2守护进程订阅失败: {e}")

        # 为新增股票推送初始状态
        for code in codes:
            if code in self._states:
                st = self._states[code]
                default_reason = init_reasons.get(code, "数据加载中")
                trend_ok, trend_reason = check_trend(st.indicators) if st.indicators else (False, default_reason)
                _push_feishu(f"👁 盯盘 {st.name}({code})\n趋势: {trend_reason}")

        return "\n".join(msgs)

    def remove(self, codes: list[str]) -> str:
        codes = [normalize_code(c) for c in codes]
        msgs = []
        with self._lock:
            for code in codes:
                if code in self._states:
                    st = self._states.pop(code)
                    # 如果入场了，生成盯盘小结
                    if st.status == "entered":
                        pnl = "持仓中" if st.entry_price > 0 else ""
                        msgs.append(f"停止盯盘 {st.name}({code}) ({pnl})")
                    else:
                        msgs.append(f"停止盯盘 {st.name}({code})")
                else:
                    msgs.append(f"{code} 未在盯盘中")
            self._save_state()
            # 同步 L2 守护进程取消订阅
            if _daemon_alive() and codes:
                try:
                    _daemon_cmd(f"UNSUB {' '.join(codes)}")
                except Exception as e:
                    logger.warning(f"L2守护进程取消订阅失败: {e}")
        return "\n".join(msgs)

    def clear_all(self) -> str:
        with self._lock:
            count = len(self._states)
            codes = list(self._states.keys())
            self._states.clear()
            self._save_state()
            if _daemon_alive() and codes:
                try:
                    _daemon_cmd(f"UNSUB {' '.join(codes)}")
                except Exception as e:
                    logger.warning(f"L2守护进程取消订阅失败: {e}")
        return f"已清空{count}只盯盘标的"

    def list_all(self) -> str:
        with self._lock:
            if not self._states:
                return "当前无盯盘标的"
            lines = ["📋 盯盘列表:"]
            for code, st in self._states.items():
                status_icon = {"watching": "👁", "signal_pending": "⏳", "entered": "📈"}.get(st.status, "❓")
                lines.append(f"  {status_icon} {st.name}({st.code}) [{st.status}]")
            return "\n".join(lines)

    # ---- 内部循环 ----

    def _loop(self):
        logger.info("盯盘循环启动")
        trading_day_logged = False
        while self._running:
            try:
                if _is_trading_time():
                    if not trading_day_logged:
                        logger.info("进入交易时段，开始盯盘扫描")
                        trading_day_logged = True
                    with self._lock:
                        codes = list(self._states.keys())
                    if codes:
                        self._scan_round(codes)
                    time.sleep(SCAN_INTERVAL)
                else:
                    if trading_day_logged:
                        logger.info("交易时段结束，暂停盯盘扫描")
                        trading_day_logged = False
                    # 非交易时间：计算距离下一交易时段开始还有多久
                    wait = _next_trading_start()
                    if wait > 0:
                        next_ts = datetime.fromtimestamp(
                            time.time() + wait
                        ).strftime("%Y-%m-%d %H:%M")
                        logger.debug(
                            "非交易时间，%s后恢复 (%.0fs)",
                            next_ts, wait
                        )
                        # 最长等5分钟，以便响应引擎stop信号
                        sleep_for = min(wait, 300.0)
                        for _ in range(int(sleep_for)):
                            if not self._running:
                                break
                            time.sleep(1)
                    else:
                        time.sleep(30)
            except Exception as e:
                logger.error(f"盯盘循环异常: {e}")
                time.sleep(SCAN_INTERVAL)
        logger.info("盯盘循环退出")

    def _scan_round(self, codes: list[str]):
        today = datetime.now().strftime("%Y%m%d")
        now = datetime.now()

        # 周期性心跳日志（每20轮≈10分钟一次）
        self._scan_count += 1
        if self._scan_count % 20 == 1:
            status_summary = ", ".join(
                f"{st.name}({st.code})[{st.status}]"
                for st in self._states.values()
            )
            logger.info(f"盯盘心跳 #{self._scan_count}: {len(codes)}只 [{status_summary}]")

        for code in codes:
            with self._lock:
                st = self._states.get(code)
                if not st:
                    continue

            # 更新日线数据(每天一次，或重启后指标丢失时强制重载)
            if st.last_daily_update != today or not st.indicators:
                ok, reason = self._update_daily(st)
                if self._scan_count % 20 == 1:
                    logger.info(f"  {code} 日线: {'OK' if ok else reason}")

            if not st.indicators:
                logger.debug(f"  {code} 无指标数据，跳过")
                continue

            # 通过守护进程获取实时数据
            market_resp = _daemon_cmd(f"MARKET {code}")
            if market_resp == "NULL":
                if self._scan_count % 20 == 1:
                    logger.info(f"  {code} 无实时行情(L2未就绪)")
                continue

            try:
                market = json.loads(market_resp)
            except Exception:
                if self._scan_count % 20 == 1:
                    logger.info(f"  {code} 行情解析失败: {market_resp[:60]}")
                continue

            last = to_price(market.get("last", "0"))
            vwap_resp = _daemon_cmd(f"VWAP {code}")
            vwap_val = float(vwap_resp) if vwap_resp != "None" else 0.0
            # 用Market trade_volume作为日内成交量
            current_vol = to_volume(market.get("trade_volume", "0"))

            inds = st.indicators
            atr_val = inds["atr20"][-1] if not np.isnan(inds["atr20"][-1]) else 0

            if st.status == "watching":
                self._check_entry(st, inds, last, vwap_val, current_vol, atr_val, now)

            elif st.status == "signal_pending":
                self._check_entry_confirm(st, inds, last, vwap_val, current_vol, atr_val, market, now)

            elif st.status == "entered":
                self._check_exit(st, inds, last, atr_val, now)

    # ---- 入场检测 ----

    def _check_entry(self, st: WatchState, inds: dict, last: float, vwap: float,
                     current_vol: float, atr_val: float, now: datetime):
        # Step 1: 趋势过滤
        trend_ok, trend_reason = check_trend(inds)
        if not trend_ok:
            # 每20轮输出一次，避免刷屏
            if self._scan_count % 20 == 1:
                logger.info(f"  {st.code} 趋势未通过: {trend_reason}")
            return

        # Step 2: 回调待机
        pullback_ok, pb_reason = check_pullback(inds, last, -1)
        if not pullback_ok:
            if self._scan_count % 20 == 1:
                logger.info(f"  {st.code} 趋势OK但回调未触发: {pb_reason} (last={last:.2f})")
            return

        # Step 2 触发 → 标记观察信号
        st.status = "signal_pending"
        st.signal_low = last   # 做多参考: 触发时低点
        st.signal_high = last  # 做空参考: 触发时高点
        st.signal_at = now.strftime("%H:%M:%S")
        st.avg_vol_20 = float(np.nanmean(inds.get("volume_20", np.array([current_vol]))))
        st.last_alert_at = now.strftime("%H:%M")
        self._save_state()
        _push_feishu(
            f"⏳ {st.name}({st.code}) 回调待机信号\n"
            f"趋势: {trend_reason}\n"
            f"触发: {pb_reason}\n"
            f"参考低点: {last:.2f} | VWAP: {vwap:.2f}"
        )

    # ---- 入场确认 ----

    def _check_entry_confirm(self, st: WatchState, inds: dict, last: float, vwap: float,
                              current_vol: float, atr_val: float, market: dict, now: datetime):
        # Step 3: 入场计分
        open_price = to_price(market.get("open", "0"))
        score, score_reason = check_entry_score(
            inds, atr_val, vwap, open_price,
            st.signal_low, st.signal_high, last, current_vol, st.avg_vol_20
        )

        if score >= 2:
            st.status = "entered"
            st.entry_price = last
            st.entry_at = now.strftime("%Y-%m-%d %H:%M:%S")
            st.highest_since_entry = last
            st.bars_held = 0
            self._save_state()
            _push_feishu(
                f"📈 {st.name}({st.code}) 入场信号!\n"
                f"入场价: {last:.2f} | {score_reason}\n"
                f"ATR: {atr_val:.2f} | VWAP: {vwap:.2f}\n"
                f"止损位: {last - 2*atr_val:.2f} (2×ATR)"
            )
        else:
            # 计分不足 → 重置(信号过期)
            st.status = "watching"
            st.signal_low = 0.0
            st.signal_high = 0.0
            st.signal_at = ""
            self._save_state()

    # ---- 出场检测 ----

    def _check_exit(self, st: WatchState, inds: dict, last: float, atr_val: float, now: datetime):
        if st.entry_price <= 0:
            return

        # 更新最高价
        if last > st.highest_since_entry:
            st.highest_since_entry = last

        # 移动止损
        stop_price = st.highest_since_entry - 2 * atr_val
        if last <= stop_price:
            pnl_pct = (last / st.entry_price - 1) * 100
            _push_feishu(
                f"🛑 {st.name}({st.code}) 移动止损触发\n"
                f"入场: {st.entry_price:.2f} → 现价: {last:.2f}\n"
                f"最高: {st.highest_since_entry:.2f} | 止损: {stop_price:.2f}\n"
                f"盈亏: {pnl_pct:+.2f}%"
            )
            self.remove([st.code])
            return

        # 分批止盈 (3×ATR)
        profit_target = st.entry_price + 3 * atr_val
        if last >= profit_target and st.bars_held > 0:
            pnl_pct = (last / st.entry_price - 1) * 100
            _push_feishu(
                f"💰 {st.name}({st.code}) 止盈目标到达\n"
                f"入场: {st.entry_price:.2f} → 现价: {last:.2f}\n"
                f"盈亏: {pnl_pct:+.2f}% | 建议平50%"
            )

        # 趋势反转 / 时间止损
        exit_signal, exit_reason = check_exit_signal(inds, st.entry_price,
                                                      st.highest_since_entry, st.bars_held, atr_val, last,
                                                      max_profit_since_entry=st.highest_since_entry - st.entry_price)
        if exit_signal:
            pnl_pct = (last / st.entry_price - 1) * 100
            _push_feishu(
                f"🔻 {st.name}({st.code}) {exit_reason}\n"
                f"入场: {st.entry_price:.2f} → 现价: {last:.2f}\n"
                f"盈亏: {pnl_pct:+.2f}% | 持仓{st.bars_held}根K线"
            )
            self.remove([st.code])

    # ---- 日线数据更新 ----

    def _update_daily(self, st: WatchState) -> tuple[bool, str]:
        """从Tushare获取日线数据并计算指标"""
        try:
            resp = call_tushare("daily", {"ts_code": st.code, "limit": 120},
                               "trade_date,open,high,low,close,pre_close,vol,amount")
            items = resp.get("data", {}).get("items", [])
            if not items or len(items) < 30:
                logger.warning(f"{st.code} 日线数据不足({len(items)}条)")
                return False, f"日线数据不足({len(items)}条)"

            fields = resp["data"]["fields"]
            # 按日期升序
            rows = sorted(items, key=lambda x: x[0])
            df = {f: np.array([row[i] for row in rows], dtype=float) for i, f in enumerate(fields)}
            df["volume"] = df.get("vol", np.zeros(len(rows)))

            inds = calc_all(df)
            inds["close"] = df.get("close", np.array([]))
            inds["volume_20"] = np.convolve(df["volume"], np.ones(20)/20, mode="same")

            st.indicators = inds
            st.last_daily_update = datetime.now().strftime("%Y%m%d")
            self._save_state()
            logger.info(f"{st.code} 日线指标更新完成({len(rows)}条)")
            return True, "趋势指标已加载"

        except Exception as e:
            logger.error(f"{st.code} 日线更新失败: {e}")
            return False, f"日线更新失败: {e}"

    # ---- 状态持久化 ----

    def _save_state(self):
        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            data = {code: st.to_dict() for code, st in self._states.items()}
            with open(STATE_FILE, "w") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"状态保存失败: {e}")

    def _load_state(self):
        try:
            if STATE_FILE.exists():
                with open(STATE_FILE) as f:
                    data = json.load(f)
                for code, d in data.items():
                    self._states[code] = WatchState.from_dict(d)
                logger.info(f"加载盯盘状态: {len(self._states)} 只")
        except Exception as e:
            logger.error(f"状态加载失败: {e}")


# ---- 全局单例 ----

_engine: Optional[WatchdogEngine] = None


def get_engine() -> WatchdogEngine:
    global _engine
    if _engine is None:
        _engine = WatchdogEngine()
    return _engine


# ═══════════════════════════════════════════════════════════
# 交易时段工具
# ═══════════════════════════════════════════════════════════

def _is_trading_time() -> bool:
    """A股交易时段: 周一至五 9:30-11:30, 13:00-15:00"""
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    h, m = now.hour, now.minute
    if h < 9 or (h == 9 and m < 30):
        return False
    if h >= 15:
        return False
    if h == 11 and m >= 30:
        return False
    if h == 12:
        return False
    return True


def _next_trading_start() -> float:
    """计算距离下一个交易时段开始的秒数"""
    from datetime import timedelta
    now = datetime.now()
    wd = now.weekday()

    # 周五收盘后 → 下周一 9:30
    if wd == 4 and now.hour >= 15:
        next_day = now + timedelta(days=3)
    elif wd >= 5:
        # 周末 → 下周一 9:30
        next_day = now + timedelta(days=(7 - wd))
    elif now.hour < 9 or (now.hour == 9 and now.minute < 30):
        # 早盘前 → 今天 9:30
        next_day = now.replace(hour=9, minute=30, second=0, microsecond=0)
    elif (now.hour == 11 and now.minute >= 30) or now.hour == 12:
        # 午休 → 今天 13:00
        next_day = now.replace(hour=13, minute=0, second=0, microsecond=0)
    elif now.hour >= 15:
        # 收盘 → 次日 9:30
        next_day = now + timedelta(days=1)
        next_day = next_day.replace(hour=9, minute=30, second=0, microsecond=0)
    else:
        return 0.0  # 已经在交易时段

    delta = (next_day - now).total_seconds()
    return max(delta, 0.0)


# ═══════════════════════════════════════════════════════════
# 独立入口：常驻 daemon 进程
# ═══════════════════════════════════════════════════════════

def main():
    """盯盘引擎常驻入口

    启动 l2api → 启动盯盘引擎 → 保活主线程。
    类似 pipes/maneki/maneki_pipe.py 的常驻模式。
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )

    # 从 .env 加载凭证
    env_file = PROJECT_DIR / ".env"
    if env_file.exists():
        load_dotenv(env_file)

    account = os.getenv("L2API_ACCOUNT", "")
    password = os.getenv("L2API_PASSWORD", "")

    if not account or not password:
        logger.error("未配置 L2API_ACCOUNT / L2API_PASSWORD，无法启动盯盘")
        sys.exit(1)

    # 等待 L2 守护进程就绪
    logger.info("正在连接 L2 守护进程...")
    for _ in range(30):  # 最多等30秒
        if _daemon_alive():
            logger.info("L2 守护进程已就绪")
            break
        time.sleep(1)
    else:
        logger.error("L2 守护进程未运行，请先通过 health_check --full 启动")
        sys.exit(1)

    # 获取并启动盯盘引擎
    engine = get_engine()
    engine.start()

    logger.info("=" * 50)
    logger.info("盯盘引擎已启动！")
    logger.info("状态文件: %s", STATE_FILE)
    logger.info("扫描间隔: %ds", SCAN_INTERVAL)
    logger.info("盯盘上限: %d 只", MAX_WATCH)
    logger.info("=" * 50)
    logger.info("通过飞书发送指令: 盯/停/盯盘列表/清盯盘")

    try:
        while True:
            time.sleep(60)
            # 日志心跳，显示引擎状态
            with engine._lock:
                count = len(engine._states)
            status = "运行中" if engine._running else "已停止"
            logger.debug("心跳: 引擎%s, 盯盘%d只", status, count)
    except KeyboardInterrupt:
        logger.info("收到停止信号，正在关闭...")
        engine.stop()
        logger.info("盯盘引擎已关闭")


if __name__ == "__main__":
    main()