"""确认器单测：趋势上涨确认（买入）+ 趋势下跌确认（卖出）。"""
import sys
sys.path.insert(0, '/root/maneki-agent')
from plays.watchdog import confirm as cf


class TestTrendUpTrigger:
    def test_trigger_new_high_with_volume(self):
        # 前10轮最高 10.0, 当前 10.05(创新高) + 窗口内放量轮(第5轮量=均量2倍)
        prices = [9.8, 9.9, 9.95, 9.9, 9.85, 9.9, 9.95, 9.98, 10.0, 9.99, 10.05]
        vols = [100, 120, 90, 110, 100, 300, 120, 130, 140, 150, 160]  # 第5轮 300 放量
        ok, reason = cf.trend_up_trigger(prices, vols)
        assert ok, reason
        assert "创新高" in reason

    def test_not_trigger_below_high(self):
        # 当前 9.99 < 前10轮最高 10.0×0.995=9.95 → 还是高于9.95, 会触发? 用明显低于的
        # 9.85 同时低于上一轮 9.99 → 新 D 条件"触发轮回落"先拦（也属"当前低于高位"）
        prices = [9.8, 9.9, 9.95, 9.9, 9.85, 9.9, 9.95, 9.98, 10.0, 9.99, 9.85]
        vols = [100, 120, 90, 110, 100, 300, 120, 130, 140, 150, 160]
        ok, reason = cf.trend_up_trigger(prices, vols)
        assert not ok, reason
        assert ("非新高" in reason) or ("触发轮回落" in reason)

    def test_not_trigger_no_volume(self):
        # 创新高但窗口内无放量
        prices = [9.8, 9.9, 9.95, 9.9, 9.85, 9.9, 9.95, 9.98, 10.0, 9.99, 10.05]
        vols = [100, 120, 90, 110, 100, 120, 120, 130, 140, 150, 160]  # 无放量轮
        ok, reason = cf.trend_up_trigger(prices, vols)
        assert not ok, reason
        assert "无放量" in reason

    def test_insufficient_window(self):
        ok, reason = cf.trend_up_trigger([9.8, 9.9], [100, 120])
        assert not ok
        assert "窗口不足" in reason


class TestBuyConfirmFSM:
    def test_trigger_stand_ready(self):
        # 触发后连续站稳 3 轮 → ready
        base, count = 0.0, 0
        # 触发轮
        prices = [9.8, 9.9, 9.95, 9.9, 9.85, 9.9, 9.95, 9.98, 10.0, 9.99, 10.05]
        vols = [100, 120, 90, 110, 100, 300, 120, 130, 140, 150, 160]
        action, base, count = cf.check_buy_confirm(prices, vols, base, count)
        assert action == "trigger", action
        assert base == 10.05
        # 站稳轮 1
        prices.append(10.06)
        vols.append(150)
        action, base, count = cf.check_buy_confirm(prices[-cf.HI_WINDOW-1:], vols[-cf.HI_WINDOW-1:], base, count)
        assert action == "stand", action
        # 站稳轮 2 → ready (2026-08-14 去钝化: 3轮→2轮)
        prices.append(10.07)
        vols.append(140)
        action, base, count = cf.check_buy_confirm(prices[-cf.HI_WINDOW-1:], vols[-cf.HI_WINDOW-1:], base, count)
        assert action == "ready", action

    def test_trigger_then_drop_resets(self):
        base, count = 0.0, 0
        prices = [9.8, 9.9, 9.95, 9.9, 9.85, 9.9, 9.95, 9.98, 10.0, 9.99, 10.05]
        vols = [100, 120, 90, 110, 100, 300, 120, 130, 140, 150, 160]
        action, base, count = cf.check_buy_confirm(prices, vols, base, count)
        assert action == "trigger"
        # 触发后跌破站稳线(10.05×0.995=9.99975) → reset
        prices.append(9.90)
        vols.append(150)
        action, base, count = cf.check_buy_confirm(prices[-cf.HI_WINDOW-1:], vols[-cf.HI_WINDOW-1:], base, count)
        assert action == "reset", action
        assert base == 0.0

    def test_wait_before_trigger(self):
        # 当前 9.94 < 前10轮最高 10.0×0.995=9.95 → 不触发
        prices = [9.8, 9.9, 9.95, 9.9, 9.85, 9.9, 9.95, 9.98, 10.0, 9.99, 9.94]
        vols = [100, 120, 90, 110, 100, 300, 120, 130, 140, 150, 160]
        action, base, count = cf.check_buy_confirm(prices, vols, 0.0, 0)
        assert action == "wait", action


class TestSellConfirm:
    def test_pullback_one_round_sell(self):
        # 2026-08-14 去钝化: 回撤第 1 轮就卖(原 2 轮)；2026-08-18 阈值 4%→2%
        ok, reason, cnt = cf.check_sell_confirm(10.0, 9.79, 9.90, 0)
        assert ok, reason
        assert "回撤" in reason

    def test_no_sell_within_2pct(self):
        # 回撤 1.5% < 2% → 不卖
        ok, reason, cnt = cf.check_sell_confirm(10.0, 9.85, 9.90, 0)
        assert not ok and cnt == 0

    def test_pin_resets_count(self):
        # 插针: 上一轮 9.50(深跌5%), 本轮收回 9.85(回到 2% 回撤线以上但未到 99% 恢复线) → 洗盘重置
        ok, reason, cnt = cf.check_sell_confirm(10.0, 9.85, 9.50, 1)
        assert not ok and cnt == 0
        assert "插针" in reason

    def test_continuous_drop_sells_immediately(self):
        # 回撤第 1 轮立即卖(持续下跌不误判插针)
        ok, reason, cnt = cf.check_sell_confirm(14.56, 13.43, 13.40, 0)
        assert ok, reason
        assert "回撤" in reason

    def test_recovery_resets_count(self):
        # 反弹回最高 99% 以上 → 取消
        ok, reason, cnt = cf.check_sell_confirm(10.0, 9.92, 9.50, 1)
        assert not ok and cnt == 0
        assert "恢复" in reason

    def test_no_data(self):
        ok, reason, cnt = cf.check_sell_confirm(0.0, 9.0, 9.0, 0)
        assert not ok

    def test_overnight_gap_down_sells(self):
        # 2026-08-18 高位出场：隔夜仓开盘低开(现价<昨收×0.995) → 主动卖
        from datetime import datetime
        ok, reason, cnt = cf.check_sell_confirm(
            10.2, 9.80, 10.1, 0,
            entry_price=10.0, prev_close=10.0, is_overnight=True,
            now=datetime(2026, 8, 18, 9, 31, 0))
        assert ok and "高位出场" in reason

    def test_overnight_gap_up_waits(self):
        # 高开(现价>昨收) → 不触发高位出场（让它跑）
        from datetime import datetime
        ok, reason, cnt = cf.check_sell_confirm(
            10.2, 10.05, 10.0, 0,
            entry_price=10.0, prev_close=9.90, is_overnight=True,
            now=datetime(2026, 8, 18, 9, 31, 0))
        assert not ok and "高位出场" not in reason

    def test_high_exit_only_overnight_window(self):
        # 非隔夜仓 / 窗口外(10:00) → 不触发高位出场（回撤未触发时也不卖）
        from datetime import datetime
        ok, reason, _ = cf.check_sell_confirm(
            10.2, 10.05, 10.1, 0,
            entry_price=10.0, prev_close=10.0, is_overnight=True,
            now=datetime(2026, 8, 18, 10, 0, 0))
        assert not ok and "高位出场" not in reason
        ok2, _, _ = cf.check_sell_confirm(
            10.2, 10.05, 10.1, 0,
            entry_price=10.0, prev_close=10.0, is_overnight=False,
            now=datetime(2026, 8, 18, 9, 31, 0))
        assert not ok2 and "高位出场" not in reason

    def test_fixed_stop_sells(self):
        # 2026-08-18 固定止损：跌破入场价 -4% → 卖（尾部兜底）
        ok, reason, cnt = cf.check_sell_confirm(
            10.5, 9.55, 9.60, 0, entry_price=10.0)
        assert ok and "固定止损" in reason

    def test_fixed_stop_not_triggered_above_line(self):
        # 未跌破入场价 -4% 且回撤未触发 → 不卖
        ok, reason, cnt = cf.check_sell_confirm(
            9.85, 9.70, 9.60, 0, entry_price=10.0)
        assert not ok and "固定止损" not in reason


class TestRiseCondition:
    """2026-08-13 拉升条件：横盘创新高不触发（冲高完横住的票不是趋势）。"""

    def test_no_trigger_flat_high(self):
        # 横盘序列: 10 轮都在 10.0~10.02 附近, 最后 10.01 创新高但拉升 <2%
        prices = [10.0, 10.01, 10.0, 10.01, 10.02, 10.0, 10.01, 10.0, 10.01, 10.02, 10.01]
        vols = [100] * 11
        ok, reason = cf.trend_up_trigger(prices, vols)
        assert not ok
        assert "拉升" in reason

    def test_trigger_with_rise(self):
        # 前 10 轮从 9.8 拉到 10.3 (拉升 5%), 最后 10.35 创新高 + 放量
        prices = [9.8, 9.85, 9.9, 9.95, 10.0, 10.05, 10.1, 10.15, 10.2, 10.3, 10.35]
        vols = [100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 300]
        ok, reason = cf.trend_up_trigger(prices, vols)
        assert ok, reason

    def test_no_trigger_last_round_drop(self):
        """2026-08-17 D 变体：触发轮回落（当前 < 上一轮×0.998）→ 不触发。

        603284 实测：13:19 急拉 45.95 → 13:20 横盘 45.85 仍被创新高 0.5%
        容差放行买在滞涨位；触发时点必须在涨才是"趋势持续"。
        """
        # 序列整体拉升但最后一轮从 10.35 回落到 10.30（拉高后横盘）
        prices = [9.8, 9.85, 9.9, 9.95, 10.0, 10.05, 10.1, 10.15, 10.2, 10.35, 10.30]
        vols = [100, 100, 100, 100, 100, 100, 100, 100, 100, 300, 100]
        ok, reason = cf.trend_up_trigger(prices, vols)
        assert not ok
        assert "触发轮回落" in reason

    def test_trigger_last_round_still_rising(self):
        """D 变体：触发轮仍高于上一轮（拉升中）→ 正常触发"""
        prices = [9.8, 9.85, 9.9, 9.95, 10.0, 10.05, 10.1, 10.15, 10.2, 10.3, 10.35]
        vols = [100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 300]
        ok, reason = cf.trend_up_trigger(prices, vols)
        assert ok, reason
