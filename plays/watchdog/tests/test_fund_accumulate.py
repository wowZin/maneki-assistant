#!/usr/bin/env python3
"""fund_accumulate_trigger 信号逻辑单测（合成数据）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path("/root/maneki-agent")))
from plays.watchdog.backtest_fund_accumulate import fund_accumulate_trigger


def mk(rounds):
    """构造记录列表，每轮 (last, high, super_net, big_net, big_buy, big_sell)"""
    return [
        {
            "last": last, "high": high,
            "super_net_amount": sn, "big_net_amount": bn,
            "big_buy_amount": bb, "big_sell_amount": bs,
        }
        for last, high, sn, bn, bb, bs in rounds
    ]


P = dict(N=5, K=3, top_tol=0.02, max_pct=5.0)

# 1) 主力持续净流入 + 不追顶(现价3%低于高点) + 主动买占优 → 触发
r = mk([(9.9, 10.2, 100, 100, 200, 50)] * 6)
assert fund_accumulate_trigger(r, P), "case1 应触发"

# 2) 追顶（现价贴近当日高点）→ 不触发
r = mk([(10.19, 10.2, 100, 100, 200, 50)] * 6)
assert not fund_accumulate_trigger(r, P), "case2 追顶不应触发"

# 3) 主动卖占优（big_sell > big_buy）→ 不触发
r = mk([(9.9, 10.2, 100, 100, 50, 200)] * 6)
assert not fund_accumulate_trigger(r, P), "case3 主动卖占优不应触发"

# 4) 主力净流出 → 不触发
r = mk([(9.9, 10.2, -100, -100, 200, 50)] * 6)
assert not fund_accumulate_trigger(r, P), "case4 净流出不应触发"

# 5) 单轮脉冲（仅最后一轮为正）→ 不触发（需 ≥K 轮为正）
r = mk([(9.9, 10.2, 0, 0, 0, 0)] * 5 + [(9.9, 10.2, 1000, 1000, 2000, 0)])
assert not fund_accumulate_trigger(r, P), "case5 单轮脉冲不应触发"

# 6) 窗口不足 → 不触发
r = mk([(9.9, 10.2, 100, 100, 200, 50)] * 3)
assert not fund_accumulate_trigger(r, P), "case6 窗口不足不应触发"

print("fund_accumulate_trigger 全部 6 例单测通过")
