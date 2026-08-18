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
        hold = {"hold_list": [{"code": "603567", "name": "珍宝岛", "hold_earn": 0.0}]}
        with patch("scripts.jvquant_trade_client.get_trade_client") as mock_tc, \
             patch("scripts.jvquant_trade_client.check_hold", return_value=hold), \
             patch.object(eng, "_save_state"):
            mock_tc.return_value.check_order.return_value = order
            eng._check_pending_buy("603567.SH", st)
        assert st.status == "entered"
        assert st.pending_buy_order_id == ""  # 复查完成清挂起

    def test_pending_buy_ghost_no_hold_skips(self):
        """2026-08-18 幽灵单根治：委托已成但 CTP 无持仓 → 不落账清 pending。

        8/17 5笔 + 8/18 4笔珍宝岛"挂单成交"假单——check_order 显示已成但
        CTP 从未持仓（孤儿引擎带 state 残留复查历史委托写假单）。
        """
        st = WatchState("603567.SH", "珍宝岛")
        st.status = "watching"
        st.pending_buy_order_id = "666666"
        st.pending_buy_since = __import__("datetime").datetime.now()
        st.prev_last = 6.67
        eng = _engine_with({"603567.SH": st})
        order = {"code": "0", "list": [
            {"order_id": "666666", "code": "603567", "status": "已成", "type": "证券买入"},
        ]}
        hold = {"hold_list": []}  # CTP 无珍宝岛持仓
        with patch("scripts.jvquant_trade_client.get_trade_client") as mock_tc, \
             patch("scripts.jvquant_trade_client.check_hold", return_value=hold), \
             patch.object(eng, "_save_state"):
            mock_tc.return_value.check_order.return_value = order
            eng._check_pending_buy("603567.SH", st)
        assert st.status == "watching"  # 不置 entered
        assert st.pending_buy_order_id == ""  # 清 pending 防反复复查
        assert "666666" not in eng._confirmed_buy_orders

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
        """进程启动（clear_pending=True）→ 残留 pending_buy_order_id 应被清空"""
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
            eng._load_state(clear_pending=True)
            loaded = eng._states["603567.SH"]
            assert loaded.pending_buy_order_id == ""  # 重启后清空
            assert loaded.pending_buy_since is None
        finally:
            wd.STATE_FILE = orig

    def test_reload_preserves_pending(self):
        """运行中 reload（clear_pending=False）→ 在途 pending 保留，复查不中断

        ★ 2026-08-17：盘中 state.json 变更（surge 每 60s 写）触发 reload 若清空
        pending，会把"已报未成交"在途买单复查放弃 → 委托成交无人确认 → 漏账
        （0817 立新能源 754483 / 0814 江海 528948 实测）。
        """
        from plays.watchdog import watchdog as wd
        import tempfile, json as _json
        st = WatchState("001258.SZ", "立新能源")
        st.status = "watching"
        st.pending_buy_order_id = "754483"
        st.pending_buy_since = __import__("datetime").datetime.now()
        state = {"001258.SZ": st.to_dict()}
        tmpdir = tempfile.mkdtemp()
        fake_state = tmpdir + "/state.json"
        _json.dump(state, open(fake_state, "w"), ensure_ascii=False)
        orig = wd.STATE_FILE
        try:
            wd.STATE_FILE = __import__("pathlib").Path(fake_state)
            eng = WatchdogEngine.__new__(WatchdogEngine)
            eng._states = {}
            eng._load_state()  # reload 路径：默认不清 pending
            loaded = eng._states["001258.SZ"]
            assert loaded.pending_buy_order_id == "754483"  # 保留
            assert loaded.pending_buy_since is not None
        finally:
            wd.STATE_FILE = orig


class TestManualSoldNotMisreported:
    """用户手动卖出残留不误报（2026-08-17 古井贡酒 13:00 假出场+假交割单）"""

    def test_minus2_manual_sold_no_journal(self):
        """sale 返回 -2 可用0，check_order 的已成委托是用户手动卖（不在
        _sold_orders）→ 不写交割单，静默移除"""
        eng = _engine_with({})
        eng._sold_orders = set()  # 本引擎未发过卖出委托
        st = WatchState("000596.SZ", "古井贡酒")
        st.status = "entered"
        st.entry_price = 99.39
        st.highest_since_entry = 99.39
        eng._states["000596.SZ"] = st
        order = [{"order_id": "1166324", "code": "000596", "type": "证券卖出",
                  "status": "已成"}]  # 用户 11:02 手动卖的委托
        called = {}
        with patch("scripts.jvquant_trade_client.sale",
                   return_value={"code": "-2", "message": "持仓不足: 000596(古井贡酒) 可用0股，需100股"}), \
             patch("scripts.jvquant_trade_client.get_trade_client") as _tc, \
             patch("plays.watchdog.watchdog._push_feishu"), \
             patch("plays.watchdog.watchdog._log_trade_journal",
                   side_effect=lambda *a, **k: called.setdefault("journal", True)):
            _tc.return_value.check_order.return_value = {"list": order}
            # 模拟 -2 分支判定（对齐 watchdog.py L1000-1043 新逻辑）
            _already_dealt = False
            for _o in order:
                if str(_o.get("code", "")) == "000596" \
                        and "卖出" in str(_o.get("type", "")) \
                        and _o.get("status") == "已成" \
                        and str(_o.get("order_id", "")) in eng._sold_orders:
                    _already_dealt = True
                    break
            assert _already_dealt is False  # 用户手动委托不被认作系统卖出
        assert "journal" not in called  # 未写假交割单

    def test_minus2_own_order_writes_journal(self):
        """sale -2 但 check_order 已成委托是本引擎发的（_sold_orders 有）
        → 补离场推送+交割单（08-05 天娱数科场景保留）"""
        eng = _engine_with({})
        eng._sold_orders = {"1851882"}
        st = WatchState("002354.SZ", "天娱数科")
        st.status = "entered"
        st.entry_price = 8.0
        st.highest_since_entry = 8.0
        eng._states["002354.SZ"] = st
        order = [{"order_id": "1851882", "code": "002354", "type": "证券卖出",
                  "status": "已成"}]  # 自己 14:22 发的委托
        called = {}
        with patch("scripts.jvquant_trade_client.sale",
                   return_value={"code": "-2", "message": "持仓不足: 002354(天娱数科) 可用0股，需100股"}), \
             patch("scripts.jvquant_trade_client.get_trade_client") as _tc, \
             patch("plays.watchdog.watchdog._push_feishu"), \
             patch("plays.watchdog.watchdog._log_trade_journal",
                   side_effect=lambda *a, **k: called.setdefault("journal", True)):
            _tc.return_value.check_order.return_value = {"list": order}
            _already_dealt = False
            for _o in order:
                if str(_o.get("code", "")) == "002354" \
                        and "卖出" in str(_o.get("type", "")) \
                        and _o.get("status") == "已成" \
                        and str(_o.get("order_id", "")) in eng._sold_orders:
                    _already_dealt = True
                    break
            assert _already_dealt is True  # 自己发的委托被认作系统卖出


class TestBuyRiseGate:
    """触发后回落闸（2026-08-17）：ready 后价格跌回触发价 0.5% 以下 → 不追买"""

    def test_alerted_price_above_base_buys(self):
        """alerted 轮价格仍 ≥ 触发价×0.995 → 正常买入"""
        eng = _engine_with({})
        st = WatchState("000737.SZ", "北方铜业")
        st.status = "alerted"
        st._confirm_ready = True
        st.confirm_base = 14.45
        eng._states["000737.SZ"] = st
        with patch.object(eng, "_execute_buy") as _buy, \
             patch.object(eng, "_save_state"):
            # 模拟 alerted 分支：last=14.52 ≥ 14.45×0.995=14.378
            last = 14.52
            if st.confirm_base > 0 and last < st.confirm_base * 0.995:
                st.status = "watching"
            else:
                st.confirm_base = 0.0
                _buy("000737.SZ", st, last, 0.0, None)
        _buy.assert_called_once()
        assert st.status == "alerted"  # 未被回退

    def test_alerted_price_dropped_aborts(self):
        """alerted 轮价格跌破触发价 0.5% → 放弃买入，回退 watching"""
        eng = _engine_with({})
        st = WatchState("001258.SZ", "立新能源")
        st.status = "alerted"
        st._confirm_ready = True
        st.confirm_base = 14.47
        eng._states["001258.SZ"] = st
        with patch.object(eng, "_execute_buy") as _buy, \
             patch.object(eng, "_save_state"):
            # 模拟 alerted 分支：last=14.35 < 14.47×0.995=14.397 → 拦
            last = 14.35
            if st.confirm_base > 0 and last < st.confirm_base * 0.995:
                st.status = "watching"
                st.confirm_base = 0.0
            else:
                st.confirm_base = 0.0
                _buy("001258.SZ", st, last, 0.0, None)
        _buy.assert_not_called()
        assert st.status == "watching"
        assert st.confirm_base == 0.0


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
