#!/usr/bin/env python3
"""fund_accumulate_confirm 生产函数单测（差分吸筹逻辑）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path("/root/maneki-agent")))
from plays.watchdog.confirm import fund_accumulate_confirm

# bignet_hist 是「当日累计」值，函数内部差分
# 1) 持续吸筹（累计递增，每轮+100）→ 触发
h = [0.0, 100.0, 200.0, 300.0, 400.0, 500.0]
ok, reason = fund_accumulate_confirm(h, last=10.0, day_high=10.5,
                                     prev_close=9.8, big_buy=200.0, big_sell=50.0)
assert ok, f"case1 应触发，实际: {reason}"

# 2) 净流出（累计递减）→ 不触发
h = [500.0, 400.0, 300.0, 200.0, 100.0, 0.0]
ok, _ = fund_accumulate_confirm(h, last=10.0, day_high=10.5,
                                prev_close=9.8, big_buy=200.0, big_sell=50.0)
assert not ok, "case2 净流出不应触发"

# 3) 单笔脉冲（只有最后一轮+1000，其余0）→ 不触发（pos_rounds=1<K=3）
h = [0.0, 0.0, 0.0, 0.0, 0.0, 1000.0]
ok, reason = fund_accumulate_confirm(h, last=10.0, day_high=10.5,
                                     prev_close=9.8, big_buy=200.0, big_sell=50.0)
assert not ok, f"case3 单笔脉冲不应触发，实际: {reason}"

# 4) 现价=当日高点（原"追顶"场景）→ 现在触发（"不追顶"已移除，2026-08-28）
h = [0.0, 100.0, 200.0, 300.0, 400.0, 500.0]
ok, reason = fund_accumulate_confirm(h, last=10.0, day_high=10.0,
                                     prev_close=9.8, big_buy=200.0, big_sell=50.0)
assert ok, f"case4 现价=高点应触发（不追顶已移除），实际: {reason}"

# 5) 主动卖占优 → 不触发
h = [0.0, 100.0, 200.0, 300.0, 400.0, 500.0]
ok, reason = fund_accumulate_confirm(h, last=10.0, day_high=10.5,
                                     prev_close=9.8, big_buy=50.0, big_sell=200.0)
assert not ok, f"case5 主动卖占优不应触发，实际: {reason}"

# 6) 窗口不足 → 不触发
ok, _ = fund_accumulate_confirm([0.0, 100.0, 200.0], last=10.0, day_high=10.5,
                                prev_close=9.8, big_buy=200.0, big_sell=50.0)
assert not ok, "case6 窗口不足不应触发"

# 7) 涨幅超限（prev_close 低 → 涨幅>5%）→ 不触发
h = [0.0, 100.0, 200.0, 300.0, 400.0, 500.0]
ok, reason = fund_accumulate_confirm(h, last=11.0, day_high=11.2,
                                     prev_close=9.5, big_buy=200.0, big_sell=50.0)
assert not ok, f"case7 涨幅超限不应触发，实际: {reason}"

print("fund_accumulate_confirm 全部 7 例单测通过")
