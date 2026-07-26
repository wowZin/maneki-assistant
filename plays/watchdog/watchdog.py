"""
盯盘助手
========

基于 jvQuant WebSocket 实时数据 + limit_up 已验证因子，持续监控标的。
通过飞书推送买卖信号，状态持久化到本地 JSON。

指令:
  盯 000001.SZ    → 开始盯盘
  停 000001.SZ    → 停止盯盘
  盯盘列表        → 查看监控列表
  清盯盘          → 全部停止
"""

from __future__ import annotations

import glob
import json
import os
import sys
import threading
import time
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
import requests
from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from plays.watchdog.indicators import price_features, realtime_row, sma, atr
from plays.watchdog.signals import check_entry, check_exit, check_abnormal, compute_factor_scores, is_worth_watching
from scripts.tu_share import call_tushare  # noqa: E402

# WS 数据通过 ws_daemon 共享内存读取
SHM_DIR = Path("/dev/shm")
WS_SUB = SHM_DIR / "ws_sub.json"
WS_SNAP = SHM_DIR / "ws_snap.json"


def _read_ws_snap(code: str) -> dict:
    try:
        snap = json.loads(WS_SNAP.read_text())
        return snap.get(code, {})
    except Exception:
        return {}


def _write_ws_sub(shorts: list[str], l2_shorts: list[str] | None = None):
    try:
        WS_SUB.write_text(json.dumps({"shorts": shorts, "l2_shorts": l2_shorts or []}))
    except Exception:
        pass


def _norm(code: str) -> str:
    if "." in code:
        return code
    return f"{code}.SH" if code.startswith("6") else f"{code}.SZ"


def _short(code: str) -> str:
    return code.replace(".SH", "").replace(".SZ", "")


logger = logging.getLogger(__name__)
STATE_FILE = PROJECT_DIR / "plays" / "watchdog" / "data" / "state.json"
SCAN_INTERVAL = 30  # 每30秒检查一次信号
MAX_WATCH = 20      # 手动盯盘上限（surge 通道不设上限，2026-07-26 用户拍板）
ABNORMAL_COOLDOWN_SECONDS = 300  # 异常推送冷却：同一 level 5 分钟内不重复推送

# 候选池来源：limit_up pipeline 产出的 analysis
ANALYSIS_DIRS = [
    PROJECT_DIR / "wiki" / "raw" / "limit-up" / "analysis",
    PROJECT_DIR / "plays" / "limit_up" / "data" / "analysis",
]


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
            logger.warning("飞书未配置 chat_id，跳过推送")
            return

        resp = requests.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": app_id, "app_secret": app_secret}, timeout=10
        )
        token = resp.json().get("tenant_access_token", "")
        if not token:
            return

        send_resp = requests.post(
            "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
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
            logger.error(f"飞书推送失败: code={result.get('code')} msg={result.get('msg')}")
    except Exception as e:
        logger.error(f"飞书推送异常: {e}")


# ---- 盯盘状态 ----

class WatchState:
    """单只股票的盯盘状态"""

    def __init__(self, code: str, name: str = ""):
        self.code = code
        self.name = name
        self.added_at = datetime.now().isoformat()
        self.status = "watching"  # watching | alerted | entered | exited
        self.source = "manual"    # manual | surge —— surge 票只发入场信号，盘后零信号汰换
        self.entry_pushed_date = ""  # 入场信号推送日期 YYYYMMDD（汰换依据）
        self.entry_price: float = 0.0
        self.entry_at: str = ""
        self.highest_since_entry: float = 0.0
        self.bars_held: int = 0
        self.signal_type: str = ""
        self.signal_reason: str = ""
        self.signal_at: str = ""
        self.last_alert_at: str = ""
        # 异常状态推送去重
        self.last_abnormal_level: str = ""
        self.last_abnormal_pushed_at: float = 0.0
        # 资金流向历史（最近 N 轮扫描）
        self.netflow_history: list[float] = []
        # 日线数据缓存
        self.daily_rows: list[dict] = []
        self.daily_basic: dict = {}
        self.dim_scores: dict = {}
        self.last_daily_update: str = ""
        # 上一轮扫描快照（用于诱空检测）
        self.prev_last: float = 0.0
        self.prev_vwap: float = 0.0
        self.prev_vol_ratio: float = 0.0

    def to_dict(self) -> dict:
        return {
            "code": self.code, "name": self.name, "added_at": self.added_at,
            "status": self.status, "entry_price": self.entry_price,
            "source": self.source, "entry_pushed_date": self.entry_pushed_date,
            "entry_at": self.entry_at, "highest_since_entry": self.highest_since_entry,
            "bars_held": self.bars_held, "signal_type": self.signal_type,
            "signal_reason": self.signal_reason, "signal_at": self.signal_at,
            "last_alert_at": self.last_alert_at,
            "last_abnormal_level": self.last_abnormal_level,
            "last_abnormal_pushed_at": self.last_abnormal_pushed_at,
            "netflow_history": self.netflow_history,
            "daily_basic": self.daily_basic,
            "dim_scores": self.dim_scores,
            "last_daily_update": self.last_daily_update,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "WatchState":
        s = cls(d["code"], d.get("name", ""))
        s.added_at = d.get("added_at", "")
        s.status = d.get("status", "watching")
        s.source = d.get("source", "manual")
        s.entry_pushed_date = d.get("entry_pushed_date", "")
        s.entry_price = d.get("entry_price", 0.0)
        s.entry_at = d.get("entry_at", "")
        s.highest_since_entry = d.get("highest_since_entry", 0.0)
        s.bars_held = d.get("bars_held", 0)
        s.signal_type = d.get("signal_type", "")
        s.signal_reason = d.get("signal_reason", "")
        s.signal_at = d.get("signal_at", "")
        s.last_alert_at = d.get("last_alert_at", "")
        s.last_abnormal_level = d.get("last_abnormal_level", "")
        s.last_abnormal_pushed_at = d.get("last_abnormal_pushed_at", 0.0)
        s.netflow_history = d.get("netflow_history", [])
        s.daily_basic = d.get("daily_basic", {})
        s.dim_scores = d.get("dim_scores", {})
        s.last_daily_update = d.get("last_daily_update", "")
        return s


# ---- 引擎 ----

class WatchdogEngine:
    """盯盘引擎（单例）"""

    def __init__(self):
        self._lock = threading.Lock()
        self._states: dict[str, WatchState] = {}
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._scan_count = 0
        self._subscribed: set[str] = set()
        self._snap_fail_count = 0
        self._netflow_cache: dict[str, tuple[float, float]] = {}  # code -> (ts, netflow)
        self._netflow_hist_ts: dict[str, float] = {}  # code -> 上次 netflow 采样时间（连续轮询下按真实时间采样）
        self._alert_ts: dict[str, float] = {}         # code -> 入场信号触发时间（连续轮询下按真实时间确认）
        self._state_mtime: float = 0.0
        self._load_state()

    # ---- 生命周期 ----

    def start(self):
        if self._running:
            return
        self._running = True
        # 同步历史标的订阅到 ws_daemon
        with self._lock:
            codes = list(self._states.keys())
        if codes:
            self._subscribe(codes)
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        logger.info("盯盘引擎已启动")

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        self._save_state()
        logger.info("盯盘引擎已停止")

    def _subscribe(self, codes: list[str]):
        """订阅标的到 ws_daemon。"""
        shorts = [_short(c) for c in codes]
        self._subscribed.update(shorts)
        _write_ws_sub(list(self._subscribed), list(self._subscribed))

    def _unsubscribe(self, codes: list[str]):
        """从 ws_daemon 取消订阅。"""
        shorts = [_short(c) for c in codes]
        self._subscribed.difference_update(shorts)
        _write_ws_sub(list(self._subscribed), list(self._subscribed))

    # ---- 指令 ----

    def _resolve_name(self, code: str) -> str:
        try:
            resp = call_tushare("stock_basic", {"ts_code": code}, "ts_code,name")
            items = resp.get("data", {}).get("items", [])
            if items and len(items[0]) > 1:
                return items[0][1]
        except Exception:
            pass
        return code

    def add(self, codes: list[str]) -> str:
        codes = [_norm(c) for c in codes]
        msgs = []
        with self._lock:
            current = sum(1 for st in self._states.values() if st.source != "surge")
            for code in codes:
                if code in self._states:
                    msgs.append(f"{code} 已在盯盘中")
                    continue
                if current >= MAX_WATCH:
                    msgs.append(f"盯盘已达上限({MAX_WATCH}只)，无法添加 {code}")
                    continue
                name = self._resolve_name(code)
                st = WatchState(code, name)
                self._update_daily(st)
                self._states[code] = st
                current += 1
                msgs.append(f"开始盯盘 {name}({code})")
            self._save_state()

        self._subscribe(codes)

        # 添加成功不推送飞书（避免干扰）；指令响应由飞书 bot 直接回复
        for code in codes:
            if code in self._states:
                st = self._states[code]
                logger.info(f"开始盯盘 {st.name}({code}) 日线数据: {'OK' if st.daily_rows else '加载中'}")

        return "\n".join(msgs)

    def remove(self, codes: list[str]) -> str:
        codes = [_norm(c) for c in codes]
        msgs = []
        with self._lock:
            for code in codes:
                if code in self._states:
                    st = self._states.pop(code)
                    pnl = ""
                    if st.status == "entered" and st.entry_price > 0:
                        pnl_pct = (self._last_price(code) / st.entry_price - 1) * 100
                        pnl = f" 盈亏{pnl_pct:+.2f}%"
                    msgs.append(f"停止盯盘 {st.name}({code}){pnl}")
                else:
                    msgs.append(f"{code} 未在盯盘中")
            self._save_state()
        self._unsubscribe(codes)
        return "\n".join(msgs)

    def clear_all(self) -> str:
        with self._lock:
            count = len(self._states)
            codes = list(self._states.keys())
            self._states.clear()
            self._save_state()
        self._unsubscribe(codes)
        return f"已清空{count}只盯盘标的"

    def list_all(self) -> str:
        with self._lock:
            if not self._states:
                return "当前无盯盘标的"
            lines = ["📋 盯盘列表:"]
            for code, st in self._states.items():
                icon = {"watching": "👁", "alerted": "⏳", "entered": "📈", "exited": "🔚"}.get(st.status, "❓")
                lines.append(f"  {icon} {st.name}({st.code}) [{st.status}]")
            return "\n".join(lines)

    # ---- 指令 ----

    # ---- 循环 ----

    def _loop(self):
        logger.info("盯盘循环启动")
        trading_day_logged = False
        self._state_mtime = self._get_state_mtime()
        while self._running:
            try:
                if _is_trading_time():
                    if not trading_day_logged:
                        logger.info("进入交易时段，开始盯盘扫描")
                        trading_day_logged = True

                    # 日切检测：交易日变更时重新订阅
                    _today_str = datetime.now().strftime("%Y%m%d")
                    if getattr(self, "_trade_date", "") != _today_str:
                        self._trade_date = _today_str
                        with self._lock:
                            _codes = list(self._states.keys())
                        if _codes:
                            self._subscribe(_codes)
                            logger.info(f"新交易日 {_today_str}，重新订阅 {len(_codes)} 只")

                    # 共享内存数据检测
                    snap_ok = WS_SNAP.exists()
                    if not snap_ok:
                        logger.warning(f"ws_daemon 共享内存未就绪")
                        time.sleep(5)
                        continue
                    # 动态加载外部 state.json 变更（用户通过 client 添加/删除）
                    self._reload_state_if_changed()
                    with self._lock:
                        codes = list(self._states.keys())
                    if codes:
                        self._scan_round(codes)
                    time.sleep(1)  # 连续轮询：一轮跑完立刻下一轮（原 30s 固定间隔已废）
                else:
                    if trading_day_logged:
                        logger.info("交易时段结束，暂停盯盘扫描")
                        trading_day_logged = False
                        self._eod_purge()
                    wait = _next_trading_start()
                    sleep_for = min(wait, 300.0)
                    for _ in range(int(sleep_for)):
                        if not self._running:
                            break
                        time.sleep(1)
            except Exception as e:
                logger.error(f"盯盘循环异常: {e}")
                time.sleep(SCAN_INTERVAL)
        logger.info("盯盘循环退出")

    def _eod_purge(self):
        """盘后汰换：surge 票当天一次入场信号都没触发（仍 watching/alerted）→ 移出盯盘列表。

        entered 状态的票保留（可能有持仓，继续走出场管理）。
        """
        with self._lock:
            doomed = [c for c, st in self._states.items()
                      if st.source == "surge" and st.status != "entered"]
        if doomed:
            logger.info(f"盘后汰换 surge 零信号 {len(doomed)} 只: {doomed}")
            self.remove(doomed)

    def _get_state_mtime(self) -> float:
        try:
            return STATE_FILE.stat().st_mtime if STATE_FILE.exists() else 0.0
        except Exception:
            return 0.0

    def _reload_state_if_changed(self):
        """检测 state.json 是否被外部修改（watchdog_client 写入），有变化则重新加载。"""
        mtime = self._get_state_mtime()
        if mtime > self._state_mtime:
            logger.info("检测到 state.json 变更，重新加载")
            old_codes = set(self._states.keys())
            self._load_state()
            new_codes = set(self._states.keys())
            added = new_codes - old_codes
            removed = old_codes - new_codes
            if added:
                logger.info(f"新增盯盘: {added}")
                self._subscribe(list(added))
            if removed:
                logger.info(f"移除盯盘: {removed}")
                self._unsubscribe(list(removed))
            self._state_mtime = mtime

    def _scan_round(self, codes: list[str]):
        today = datetime.now().strftime("%Y%m%d")
        now = datetime.now()
        self._scan_count += 1

        if self._scan_count % 20 == 1:
            with self._lock:
                summary = ", ".join(f"{st.name}({st.code})[{st.status}]" for st in self._states.values())
            logger.info(f"盯盘心跳 #{self._scan_count}: {len(codes)}只 [{summary}]")

        for code in codes:
            with self._lock:
                st = self._states.get(code)
                if not st:
                    continue

            if st.last_daily_update != today or not st.daily_rows:
                self._update_daily(st)

            if not st.daily_rows:
                continue

            market = _read_ws_snap(_short(code))
            if not market or not market.get("last"):
                self._snap_fail_count += 1
                if self._scan_count % 10 == 0:
                    logger.warning(f"{code} 无行情数据（共享内存无数据{self._snap_fail_count}次）")
                continue
            self._snap_fail_count = 0  # 有数据了，重置

            vwap_data = _read_ws_snap(f"{_short(code)}_vwap")
            vwap = float(vwap_data) if isinstance(vwap_data, (int, float)) else 0.0
            bid_price = market.get("bid_price", [None] * 10)
            ask_price = market.get("ask_price", [None] * 10)
            bid1 = float(bid_price[0]) if bid_price and bid_price[0] else 0
            ask1 = float(ask_price[0]) if ask_price and ask_price[0] else 0
            ask_bid = bid1 / ask1 if ask1 > 0 else 1.0
            daily_features = price_features(st.daily_rows)
            row = realtime_row(code, market, vwap, daily_features, st.daily_basic, st.dim_scores, st.daily_rows)
            scores = compute_factor_scores(row)

            # 补充 L1/L2 实时信号到 row（供 check_entry 入场确认）
            row["bid1"] = bid1
            row["ask1"] = ask1
            row["vwap"] = vwap
            row["inner_vol"] = market.get("inner_vol", 0)
            row["outer_vol"] = market.get("outer_vol", 0)
            row["last"] = market.get("last", 0)

            last = float(market.get("last") or 0)

            # 资金流向历史：按真实时间采样（≥60s 一个点，10点≈10分钟窗口；
            # 连续轮询下若按轮追加，窗口会从5分钟塌缩到几十秒）
            _now_ts = time.time()
            if _now_ts - self._netflow_hist_ts.get(code, 0.0) >= 60:
                netflow = self._get_netflow(code)
                st.netflow_history.append(netflow)
                if len(st.netflow_history) > 10:
                    st.netflow_history = st.netflow_history[-10:]
                self._netflow_hist_ts[code] = _now_ts
            else:
                netflow = (st.netflow_history[-1] if st.netflow_history
                           else self._get_netflow(code))

            # ── 异常状态检测（资金离场 / 抛压 / 诱空）──
            abnormal, level, abnormal_reason = check_abnormal(
                row, scores, netflow, ask_bid,
                entry_price=st.entry_price if st.status == "entered" else 0.0,
                netflow_history=st.netflow_history,
                prev_last=st.prev_last,
                prev_vwap=st.prev_vwap,
                prev_vol_ratio=st.prev_vol_ratio,
            )

            # 保存本轮快照供下一轮诱空检测
            st.prev_last = last
            st.prev_vwap = vwap
            st.prev_vol_ratio = row.get("vol_ratio_proxy", 1.0)

            if abnormal:
                if level == "bear_trap":
                    # 诱空不移出盯盘,不推送
                    st.last_abnormal_level = level
                    st.last_abnormal_pushed_at = time.time()
                elif st.source == "surge":
                    # surge 票保持静默（只发入场信号）；critical 仍移除
                    st.last_abnormal_level = level
                    st.last_abnormal_pushed_at = time.time()
                    if level == "critical":
                        self.remove([code])
                        continue
                else:
                    icon = "🚨" if level == "critical" else "⚠️"
                    msg = (
                        f"{icon} {st.name}({code}) 异常状态 [{level}]\n"
                        f"{abnormal_reason}\n"
                        f"现价: {last:.2f} | VWAP: {vwap:.2f}"
                    )
                    # 冷却期内不重复推送
                    since_last = time.time() - st.last_abnormal_pushed_at
                    if level != st.last_abnormal_level or since_last >= ABNORMAL_COOLDOWN_SECONDS:
                        _push_feishu(msg)
                        st.last_abnormal_level = level
                        st.last_abnormal_pushed_at = time.time()
                    if level == "critical":
                        self.remove([code])
                        continue
            else:
                # 状态恢复正常，清空冷却记录（下次异常立即推送）
                # bear_trap 不移出盯盘，不清冷却，防止高频重复
                if st.last_abnormal_level != "bear_trap":
                    st.last_abnormal_level = ""

            if st.status == "watching":
                triggered, sig_type, reason = check_entry(row, scores)
                if triggered:
                    st.status = "alerted"
                    st.signal_type = sig_type
                    st.signal_reason = reason
                    st.signal_at = now.strftime("%H:%M:%S")
                    st.last_alert_at = now.strftime("%H:%M")
                    self._alert_ts[code] = time.time()
                    self._save_state()
                    if st.source != "surge":
                        # surge 票不发触发信号，只发入场信号
                        _push_feishu(
                            f"⏳ {st.name}({code}) 触发信号\n"
                            f"类型: {sig_type}\n"
                            f"原因: {reason}\n"
                            f"现价: {last:.2f} | VWAP: {vwap:.2f}"
                        )

            elif st.status == "alerted":
                # 信号确认：触发满 30s 后仍满足条件才入场
                # （连续轮询下按真实时间判断，保持原 30s 间隔的确认语义）
                if time.time() - self._alert_ts.get(code, 0.0) >= 30:
                    triggered, _, _ = check_entry(row, scores)
                    if triggered:
                        st.status = "entered"
                        st.entry_price = last
                        st.entry_at = now.strftime("%Y-%m-%d %H:%M:%S")
                        st.highest_since_entry = last
                        st.bars_held = 0
                        st.entry_pushed_date = now.strftime("%Y%m%d")
                        self._save_state()
                        _surge_tag = "【surge】" if st.source == "surge" else ""
                        _push_feishu(
                            f"📈 {st.name}({code}) 入场!{_surge_tag}\n"
                            f"入场价: {last:.2f}\n"
                            f"信号: {st.signal_reason}\n"
                            f"VWAP: {vwap:.2f}"
                        )
                    else:
                        st.status = "watching"
                        st.signal_type = ""
                        st.signal_reason = ""
                        st.signal_at = ""
                        self._save_state()

            elif st.status == "entered":
                if last > st.highest_since_entry:
                    st.highest_since_entry = last
                st.bars_held += 1

                # 时间止损按真实持仓分钟（bars_held 只是轮数计数，
                # 连续轮询下按轮换算会让 60 分钟止损在几分钟内误触发）
                try:
                    held_minutes = int((datetime.now() - datetime.strptime(
                        st.entry_at, "%Y-%m-%d %H:%M:%S")).total_seconds() // 60)
                except Exception:
                    held_minutes = st.bars_held
                exit_triggered, exit_reason = check_exit(
                    st.entry_price, st.highest_since_entry, last,
                    held_minutes, vwap, scores, row)
                if exit_triggered:
                    pnl_pct = (last / st.entry_price - 1) * 100
                    _surge_tag = "【surge】" if st.source == "surge" else ""
                    _push_feishu(
                        f"🛑 {st.name}({code}) 出场{_surge_tag}\n"
                        f"{exit_reason}\n"
                        f"入场: {st.entry_price:.2f} → 现价: {last:.2f}\n"
                        f"盈亏: {pnl_pct:+.2f}% | 持仓{st.bars_held}轮"
                    )
                    self.remove([code])

            self._save_state()

    def _last_price(self, code: str) -> float:
        market = _read_ws_snap(_short(code))
        return float(market.get("last") or 0) if market else 0.0

    def _get_netflow(self, code: str) -> float:
        """从 jvQuant 获取当日大单+中单净流向（元）。

        节流：每只股票 300s 缓存。资金流向是当日累计值，30s 粒度无意义，
        不节流时每股每轮一次 REST，30 只票一轮 30-60s 会拖垮 30s 节奏。
        """
        now = time.time()
        cached = self._netflow_cache.get(code)
        if cached and now - cached[0] < 300:
            return cached[1]
        val = 0.0
        try:
            from scripts.jvquant_client import get_jvquant_client
            client = get_jvquant_client()
            data = client.get_fundflow_single(_short(code))
            if data:
                # main_net/big_net/mid_net 单位万元，转元
                val = (
                    float(data.get("main_net", 0) or 0)
                    + float(data.get("big_net", 0) or 0)
                    + float(data.get("mid_net", 0) or 0)
                ) * 10000
        except Exception as e:
            logger.debug(f"获取资金流向失败 {code}: {e}")
        self._netflow_cache[code] = (now, val)
        return val

    # ---- 日线数据 ----

    def _update_daily(self, st: WatchState) -> bool:
        try:
            resp = call_tushare("daily", {"ts_code": st.code, "limit": 120},
                               "trade_date,open,high,low,close,pre_close,vol,amount,pct_chg")
            items = resp.get("data", {}).get("items", [])
            if not items or len(items) < 30:
                logger.warning(f"{st.code} 日线数据不足({len(items)}条)")
                return False

            fields = resp["data"]["fields"]
            rows = sorted(items, key=lambda x: x[0])
            st.daily_rows = [dict(zip(fields, row)) for row in rows]

            # daily_basic
            basic_resp = call_tushare("daily_basic", {"ts_code": st.code, "limit": 1},
                                      "ts_code,trade_date,pe,pb,circ_mv,turnover_rate,volume_ratio")
            basic_items = basic_resp.get("data", {}).get("items", [])
            if basic_items:
                basic_fields = basic_resp["data"]["fields"]
                st.daily_basic = dict(zip(basic_fields, basic_items[0]))

            # 五维度分：从最新 analysis 文件读取
            st.dim_scores = self._load_dim_scores(st.code)
            st.last_daily_update = datetime.now().strftime("%Y%m%d")
            logger.info(f"{st.code} 日线数据更新完成({len(rows)}条)")
            return True
        except Exception as e:
            logger.error(f"{st.code} 日线更新失败: {e}")
            return False

    def _load_dim_scores(self, code: str) -> dict:
        """从最新 analysis 文件读取五维度分"""
        files = []
        for base in ANALYSIS_DIRS:
            if base.exists():
                files.extend(base.glob("*.json"))
        if not files:
            return {}
        latest = sorted(files, key=lambda f: f.name, reverse=True)[0]
        try:
            data = json.loads(latest.read_text())
            if isinstance(data, list):
                for item in data:
                    if item.get("code") == code:
                        return item.get("scores", {})
        except Exception:
            pass
        return {}

    # ---- 候选池 ----

    def load_candidates(self) -> list[tuple[str, str]]:
        """从最新 limit_up analysis 加载候选股池。返回 [(code, name), ...]。"""
        files = []
        for base in ANALYSIS_DIRS:
            if base.exists():
                files.extend(base.glob("*.json"))
        if not files:
            return []
        latest = sorted(files, key=lambda f: f.name, reverse=True)[0]
        try:
            data = json.loads(latest.read_text())
        except Exception:
            return []

        candidates = []
        if isinstance(data, list):
            for item in data:
                code = item.get("code")
                name = item.get("name", "")
                total_score = float(item.get("total_score") or item.get("model_score", 0) or 0)
                scores = item.get("scores", {})
                if code and total_score >= 85:
                    candidates.append((code, name, total_score, scores))

        candidates.sort(key=lambda x: x[2], reverse=True)
        return [(c[0], c[1]) for c in candidates[:MAX_WATCH]]

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
                # 重建 _states：state.json 中没有的 → 删除
                new_states = {}
                for code, d in data.items():
                    new_states[code] = WatchState.from_dict(d)
                self._states.clear()
                self._states.update(new_states)
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


# ---- 交易时段 ----

def _is_trading_time() -> bool:
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
    from datetime import timedelta
    now = datetime.now()
    wd = now.weekday()

    if wd == 4 and now.hour >= 15:
        next_day = now + timedelta(days=3)
    elif wd >= 5:
        next_day = now + timedelta(days=(7 - wd))
    elif now.hour < 9 or (now.hour == 9 and now.minute < 30):
        next_day = now.replace(hour=9, minute=30, second=0, microsecond=0)
    elif (now.hour == 11 and now.minute >= 30) or now.hour == 12:
        next_day = now.replace(hour=13, minute=0, second=0, microsecond=0)
    elif now.hour >= 15:
        next_day = now + timedelta(days=1)
        next_day = next_day.replace(hour=9, minute=30, second=0, microsecond=0)
    else:
        return 0.0

    return max((next_day - now).total_seconds(), 0.0)


# ---- 入口 ----

def main():
    """cron 每日拉起模式（2026-07-26）：09:20 hermes cron 启动 → 内部 30s 循环
    → 15:05 自动退出（EOD 汰换 15:00 已执行）。非交易日直接退出。
    pid 守卫防多实例（cron 与手动启动撞车）。代码改动次日自然生效。"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )

    env_file = PROJECT_DIR / ".env"
    if env_file.exists():
        load_dotenv(env_file)

    # pid 防多实例
    pid_file = PROJECT_DIR / "plays" / "watchdog" / "data" / "health" / "watchdog.pid"
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    if pid_file.exists():
        try:
            old_pid = int(pid_file.read_text().strip())
            os.kill(old_pid, 0)
            logger.info(f"已有实例在跑 (PID {old_pid})，退出")
            return
        except (ValueError, PermissionError):
            pass
        except OSError:
            pass  # 旧进程不存在
    pid_file.write_text(str(os.getpid()))
    import atexit
    atexit.register(lambda: pid_file.unlink() if pid_file.exists() else None)

    # 非交易日不启动（cron 按工作日触发，节假日在此兜底）
    from plays.limit_up.utils import _is_trade_day, _today_str
    if not _is_trade_day(_today_str()):
        logger.info("非交易日，退出")
        return

    engine = get_engine()
    engine.start()

    logger.info("=" * 50)
    logger.info("盯盘引擎已启动！")
    logger.info("状态文件: %s", STATE_FILE)
    logger.info("轮询模式: 连续（一轮跑完即下一轮）")
    logger.info("盯盘上限: %d 只", MAX_WATCH)
    logger.info("=" * 50)
    logger.info("通过飞书发送指令: 盯/停/盯盘列表/清盯盘")

    try:
        while True:
            time.sleep(60)
            # 15:05 自动退出（EOD 汰换在 15:00 收盘轮已执行，留 5 分钟余量）
            if datetime.now().hour >= 15 and datetime.now().minute >= 5:
                logger.info("收盘退出（15:05），明日由 cron 重新拉起")
                engine.stop()
                return
            with engine._lock:
                count = len(engine._states)
            logger.debug("心跳: 引擎%s, 盯盘%d只", "运行中" if engine._running else "已停止", count)
    except KeyboardInterrupt:
        logger.info("收到停止信号，正在关闭...")
        engine.stop()
        logger.info("盯盘引擎已关闭")


if __name__ == "__main__":
    main()
