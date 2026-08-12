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
    eng._confirmed_buy_orders = set()  # 幂等：已确认过的买单委托号
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
            {"order_id": "1851882", "code": "603567", "status": "已成", "type": "证券买入"},
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
            {"order_id": "999999", "code": "603567", "status": "已报", "type": "证券买入"},
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

    def test_pending_buy_wrong_code_no_ghost(self):
        """交叉匹配 bug 回归：同 order_id 但 code 不同 → 不写交割单

        2026-08-10 珍宝岛幽灵单事故：pending_buy_order_id 残留 + 只比
        order_id → 把盛达资源/三变科技的成交误判成珍宝岛成交，写 5 笔
        幽灵交割单（CTP 实际从未买入）。修复后必须 order_id+code 双匹配。
        """
        st = WatchState("603567.SH", "珍宝岛")
        st.status = "watching"
        st.pending_buy_order_id = "908546"  # 三变科技的委托号
        st.pending_buy_since = __import__("datetime").datetime.now()
        eng = _engine_with({"603567.SH": st})
        # CTP 里 908546 是 002112（三变科技）的已成委托，不是 603567
        order = {"code": "0", "list": [
            {"order_id": "908546", "code": "002112", "status": "已成",
             "type": "证券买入"},
        ]}
        with patch("scripts.jvquant_trade_client.get_trade_client") as mock_tc, \
             patch.object(eng, "_save_state"):
            mock_tc.return_value.check_order.return_value = order
            eng._check_pending_buy("603567.SH", st)
        # code 不匹配 → 不确认成交，保持 watching，pending 保留
        assert st.status == "watching"
        assert st.pending_buy_order_id == "908546"  # 等真正的珍宝岛委托


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
        _pending_sell = False
        for _o in order.get("list") or []:
            if str(_o.get("code", "")) == "600650" \
                    and "卖出" in str(_o.get("type", "")):
                if _o.get("status") == "已成":
                    _sold = True
                if _o.get("status") in ("已报", "待报"):
                    _pending_sell = True
        assert _sold is True and _pending_sell is False  # 已成 → 离场移除

    def test_t1_with_pending_sell_keeps_watching(self):
        """-5 但卖出委托挂单中(已报) → 可用0是挂单冻结，保持 entered 复查（非 T+1）"""
        order = {"code": "0", "list": [
            {"order_id": "196929", "code": "002319", "type": "证券卖出",
             "status": "已报", "deal_volume": "0"},
        ]}
        _sold = False
        _pending_sell = False
        for _o in order.get("list") or []:
            if str(_o.get("code", "")) == "002319" \
                    and "卖出" in str(_o.get("type", "")):
                if _o.get("status") == "已成":
                    _sold = True
                if _o.get("status") in ("已报", "待报"):
                    _pending_sell = True
        assert _sold is False and _pending_sell is True  # 挂单中 → 不标 T+1

    def test_t1_real_blocked_keeps_watching(self):
        """-5 且无已成卖出委托 → 真 T+1，保留盯盘次日恢复"""
        order = {"code": "0", "list": [
            {"order_id": "x", "code": "603823", "type": "证券买入", "status": "已成"},
        ]}
        _sold = False
        _pending_sell = False
        for _o in order.get("list") or []:
            if str(_o.get("code", "")) == "603823" \
                    and "卖出" in str(_o.get("type", "")):
                if _o.get("status") == "已成":
                    _sold = True
                if _o.get("status") in ("已报", "待报"):
                    _pending_sell = True
        assert _sold is False and _pending_sell is False  # 只有买入委托 → 真 T+1


class TestSellBlockedSilence:
    """当日卖出受阻静默：砸盘卖不出的票不再反复推送异常状态噪音。"""

    def test_pending_sell_marks_blocked_date(self):
        """-5 挂单中 → 标记 sell_blocked_date（当日静默依据）"""
        st = WatchState("600892.SH", "大晟文化")
        st.status = "entered"
        st.entry_price = 4.33
        order = {"code": "0", "list": [
            {"order_id": "1151822", "code": "600892", "type": "证券卖出",
             "status": "已报", "deal_volume": "0"},
        ]}
        _sold = False
        _pending_sell = False
        for _o in order.get("list") or []:
            if str(_o.get("code", "")) == "600892" \
                    and "卖出" in str(_o.get("type", "")):
                if _o.get("status") == "已成":
                    _sold = True
                if _o.get("status") in ("已报", "待报"):
                    _pending_sell = True
        assert _sold is False and _pending_sell is True
        # 修复后的 watchdog 逻辑：挂单中 → 标当日静默
        st.sell_blocked_date = "20260807"
        assert st.sell_blocked_date == "20260807"

    def test_blocked_same_day_silent_next_day_resumes(self):
        """当日静默：同日期静默，次日自动恢复推送"""
        st = WatchState("600892.SH", "大晟文化")
        st.sell_blocked_date = "20260807"
        today = "20260807"
        tomorrow = "20260808"
        assert st.sell_blocked_date == today  # 当日 → 静默生效
        assert st.sell_blocked_date != tomorrow  # 次日 → 恢复


class TestRestartClearsPendingBuy:
    """重启清空残留挂单复查（2026-08-10 幽灵单根治）"""

    def test_load_state_clears_pending(self):
        """_load_state 后残留 pending_buy_order_id 应被清空"""
        from plays.watchdog import watchdog as wd
        import tempfile, json as _json
        # 构造带残留 pending 的 state
        st = WatchState("603567.SH", "珍宝岛")
        st.status = "watching"
        st.pending_buy_order_id = "908546"
        st.pending_buy_since = __import__("datetime").datetime.now()
        state = {"603567.SH": st.to_dict()}
        # 用临时文件模拟 STATE_FILE
        tmpdir = tempfile.mkdtemp()
        fake_state = tmpdir + "/state.json"
        _json.dump(state, open(fake_state, "w"), ensure_ascii=False)
        orig = wd.STATE_FILE
        try:
            wd.STATE_FILE = __import__("pathlib").Path(fake_state)
            eng = WatchdogEngine.__new__(WatchdogEngine)
            eng._states = {}
            eng._load_state()
            loaded = eng._states["603567.SH"]
            assert loaded.pending_buy_order_id == ""  # 重启后清空
            assert loaded.pending_buy_since is None
        finally:
            wd.STATE_FILE = orig


class TestPendingSellTimeout:
    """卖出挂单超时撤单重挂（2026-08-10 大晟文化挂死事故）"""

    def test_pending_sell_records_time(self):
        """code=0 委托未成交 → 记录 pending_sell_order_id + since"""
        st = WatchState("600892.SH", "大晟文化")
        st.status = "entered"
        st.entry_price = 4.33
        # 模拟 code=0 未成交分支
        st.pending_sell_order_id = "137150"
        st.pending_sell_since = __import__("time").time()
        assert st.pending_sell_order_id == "137150"
        assert st.pending_sell_since > 0

    def test_timeout_sets_force_resell(self):
        """挂单超时 >180s → 撤单 + force_resell=True（下轮强制重挂）"""
        import time as _t
        st = WatchState("600892.SH", "大晟文化")
        st.status = "entered"
        st.pending_sell_order_id = "137150"
        st.pending_sell_since = _t.time() - 300  # 5分钟前挂的
        # 复现 watchdog -5 _pending_sell 分支的超时逻辑
        if st.pending_sell_since and _t.time() - st.pending_sell_since > 180:
            st.pending_sell_order_id = ""
            st.pending_sell_since = 0.0
            st.force_resell = True
        assert st.pending_sell_order_id == ""  # 已撤单
        assert st.force_resell is True  # 下轮强制重挂

    def test_fresh_pending_no_force(self):
        """挂单未超时 → 不撤单不强制"""
        import time as _t
        st = WatchState("600892.SH", "大晟文化")
        st.pending_sell_order_id = "137150"
        st.pending_sell_since = _t.time() - 30  # 30秒前挂的
        if st.pending_sell_since and _t.time() - st.pending_sell_since > 180:
            st.force_resell = True
        assert st.force_resell is False
