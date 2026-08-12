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
        prices = [9.8, 9.9, 9.95, 9.9, 9.85, 9.9, 9.95, 9.98, 10.0, 9.99, 9.85]
        vols = [100, 120, 90, 110, 100, 300, 120, 130, 140, 150, 160]
        ok, reason = cf.trend_up_trigger(prices, vols)
        assert not ok, reason
        assert "非新高" in reason

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
        # 站稳轮 2
        prices.append(10.07)
        vols.append(140)
        action, base, count = cf.check_buy_confirm(prices[-cf.HI_WINDOW-1:], vols[-cf.HI_WINDOW-1:], base, count)
        assert action == "stand", action
        # 站稳轮 3 → ready
        prices.append(10.08)
        vols.append(130)
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
    def test_pullback_two_rounds_sell(self):
        # 最高 10.0, 回撤 4% = 9.60; 连续 2 轮 ≤ 9.60 → 卖
        ok, reason, cnt = cf.check_sell_confirm(10.0, 9.59, 9.90, 0)
        assert not ok and cnt == 1
        ok, reason, cnt = cf.check_sell_confirm(10.0, 9.58, 9.59, 1)
        assert ok, reason
        assert "回撤" in reason

    def test_no_sell_within_4pct(self):
        ok, reason, cnt = cf.check_sell_confirm(10.0, 9.65, 9.70, 0)
        assert not ok and cnt == 0

    def test_pin_resets_count(self):
        # 插针: 上一轮 9.50(相对最高跌5%), 本轮收回 9.60 → 重置
        ok, reason, cnt = cf.check_sell_confirm(10.0, 9.60, 9.50, 1)
        assert not ok and cnt == 0
        assert "插针" in reason or "恢复" in reason

    def test_recovery_resets_count(self):
        # 反弹回最高 98% 以上 → 取消
        ok, reason, cnt = cf.check_sell_confirm(10.0, 9.85, 9.50, 1)
        assert not ok and cnt == 0
        assert "恢复" in reason

    def test_no_data(self):
        ok, reason, cnt = cf.check_sell_confirm(0.0, 9.0, 9.0, 0)
        assert not ok
