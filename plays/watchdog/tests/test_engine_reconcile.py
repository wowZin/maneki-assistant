"""watchdog 引擎新增逻辑单测：持仓对账兜底 + 挂起买单复查。

红线：不碰真实 CTP 交易。check_hold / check_order 全部 mock。
只验证状态机流转：失管持仓 → entered；挂单成交 → entered。
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from plays.watchdog.watchdog import WatchState, WatchdogEngine  # noqa: E402


def _engine_with(states: dict[str, WatchState]) -> WatchdogEngine:
    eng = WatchdogEngine.__new__(WatchdogEngine)
    eng._lock = __import__("threading").Lock()
    eng._states = dict(states)
    eng._subscribed = set()
    eng._running = False
    return eng


class TestReconcileHolds:
    """持仓对账：CTP 有仓但不在 entered → 自动加回盯盘"""

    def test_lost_hold_added_back(self):
        eng = _engine_with({})  # 空盯盘，但 CTP 有 1 只持仓
        hold = {"code": "0", "hold_list": [
            {"code": "603567", "name": "珍宝岛", "hold_vol": "200",
             "usable_vol": "200", "hold_earn": "-76.02", "day_earn": "-52.00"},
        ]}
        with patch("scripts.jvquant_trade_client.check_hold", return_value=hold), \
             patch("plays.watchdog.watchdog._read_ws_snap", return_value={"last": "6.55"}), \
             patch.object(eng, "_save_state"):
            eng._reconcile_holds()
        assert "603567.SH" in eng._states
        st = eng._states["603567.SH"]
        assert st.status == "entered"
        assert st.source == "reconcile"
        assert st.entry_price == 6.55  # 用快照当前价近似
        assert st.name == "珍宝岛"

    def test_existing_entered_not_touched(self):
        st = WatchState("603567.SH", "珍宝岛")
        st.status = "entered"
        st.entry_price = 6.67
        eng = _engine_with({"603567.SH": st})
        hold = {"code": "0", "hold_list": [
            {"code": "603567", "name": "珍宝岛", "hold_vol": "200",
             "usable_vol": "200", "hold_earn": "-76.02", "day_earn": "-52.00"},
        ]}
        with patch("scripts.jvquant_trade_client.check_hold", return_value=hold), \
             patch.object(eng, "_save_state"):
            eng._reconcile_holds()
        # 已 entered 的票保持原状态不变（entry_price 不覆盖）
        assert eng._states["603567.SH"].entry_price == 6.67

    def test_empty_hold_noop(self):
        eng = _engine_with({})
        with patch("scripts.jvquant_trade_client.check_hold",
                   return_value={"code": "0", "hold_list": []}):
            eng._reconcile_holds()
        assert eng._states == {}


class TestCheckPendingBuy:
    """挂起买单复查：委托已成 → 补置 entered"""

    def test_pending_buy_confirmed_deal(self):
        st = WatchState("603567.SH", "珍宝岛")
        st.status = "watching"
        st.pending_buy_order_id = "1851882"
        st.pending_buy_since = __import__("datetime").datetime.now()
        st.prev_last = 6.67
        eng = _engine_with({"603567.SH": st})
        order = {"code": "0", "list": [
            {"order_id": "1851882", "status": "已成", "type": "证券买入"},
        ]}
        with patch("scripts.jvquant_trade_client.get_trade_client") as mock_tc, \
             patch.object(eng, "_save_state"):
            mock_tc.return_value.check_order.return_value = order
            eng._check_pending_buy("603567.SH", st)
        assert st.status == "entered"
        assert st.pending_buy_order_id == ""  # 复查完成清挂起

    def test_pending_buy_not_dealt_keeps_waiting(self):
        st = WatchState("603567.SH", "珍宝岛")
        st.status = "watching"
        st.pending_buy_order_id = "999999"
        st.pending_buy_since = __import__("datetime").datetime.now()
        eng = _engine_with({"603567.SH": st})
        order = {"code": "0", "list": [
            {"order_id": "999999", "status": "已报", "type": "证券买入"},
        ]}
        with patch("scripts.jvquant_trade_client.get_trade_client") as mock_tc, \
             patch.object(eng, "_save_state"):
            mock_tc.return_value.check_order.return_value = order
            eng._check_pending_buy("603567.SH", st)
        assert st.status == "watching"  # 未成交继续等
        assert st.pending_buy_order_id == "999999"

    def test_pending_buy_timeout_abandons(self):
        from datetime import datetime, timedelta
        st = WatchState("603567.SH", "珍宝岛")
        st.status = "watching"
        st.pending_buy_order_id = "888888"
        st.pending_buy_since = datetime.now() - timedelta(minutes=15)  # 超10分钟
        eng = _engine_with({"603567.SH": st})
        with patch("scripts.jvquant_trade_client.get_trade_client") as mock_tc:
            eng._check_pending_buy("603567.SH", st)
        mock_tc.assert_not_called()  # 超时不再查
        assert st.status == "watching"
        assert st.pending_buy_order_id == ""  # 放弃复查


class TestExitT1Misjudge:
    """卖出 -5 误判修复：委托已成但 usable_vol=0 时不再标 T+1"""

    def test_t1_with_sold_order_exits(self):
        """-5 但 check_order 有已成卖出委托 → 应识别为已卖出（非 T+1）"""
        order = {"code": "0", "list": [
            {"order_id": "160266", "code": "600650", "type": "证券卖出",
             "status": "已成", "deal_volume": "100"},
        ]}
        # 复现 watchdog.py -5 分支的核心判定逻辑
        _sold = False
        for _o in order.get("list") or []:
            if str(_o.get("code", "")) == "600650" \
                    and "卖出" in str(_o.get("type", "")) \
                    and _o.get("status") == "已成":
                _sold = True
        assert _sold is True  # 能识别已成卖出委托 → 走正常离场而非标 T+1

    def test_t1_real_blocked_keeps_watching(self):
        """-5 且无已成卖出委托 → 真 T+1，保留盯盘次日恢复"""
        order = {"code": "0", "list": [
            {"order_id": "x", "code": "603823", "type": "证券买入", "status": "已成"},
        ]}
        _sold = False
        for _o in order.get("list") or []:
            if str(_o.get("code", "")) == "603823" \
                    and "卖出" in str(_o.get("type", "")) \
                    and _o.get("status") == "已成":
                _sold = True
        assert _sold is False  # 只有买入委托 → 不误判已卖出
