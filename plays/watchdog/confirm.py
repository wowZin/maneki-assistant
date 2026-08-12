"""确认器模块（2026-08-12 用户拍板简化蓝图）。

把系统的"信号确认层"收敛成两个确认器：
  买入 = 趋势上涨确认（创新高 + 窗口放量 + 站稳）+ 防诱多（站稳天然覆盖）
  卖出 = 趋势下跌确认（最高点回撤 4% 连续 2 轮）+ 防诱空洗盘（插针/缩量回踩不卖）

替代的旧逻辑（全部移除）：
  - check_entry 的 vwap_break/L1 盘口防诱多
  - 3 轮同类型信号确认 + alerted 30s 价格不跌
  - 净上涨门禁（entry_base_price × 1.005）
  - 固定止损/移动止损/止盈/回调/反转/资金流/高位回撤 2%

设计依据（2026-08-12 回放）：
  - 19 笔追高买入全部发生在"非新高"（低于前 10 分钟最高 0.1~1.8%）位置；
    早盘创新高站稳确认器首触发买入 → 收盘 +1.96% vs 实际买入 -0.13%
  - 16 笔卖出里 7 笔卖飞（卖出后 30min 涨 >0.5%，安记食品收盘涨停 +11.34%）——
    旧"高位回撤 2%"阈值过灵敏，改为 4% + 防插针 + 趋势恢复
"""

from __future__ import annotations

# ── 买入确认器参数 ──
HI_WINDOW = 10        # 创新高窗口（轮）：当前价需 ≥ 前 10 轮最高
HI_TOL = 0.995        # 创新高容差：允许 0.5%（分钟噪声）
VOL_MULT = 1.5        # 放量倍数：窗口内任一轮量 > 前 5 轮均量 × 1.5
STAND_ROUNDS = 3      # 站稳轮数：触发后连续 3 轮不跌回触发价 0.5% 以下
STAND_TOL = 0.995

# ── 卖出确认器参数 ──
SELL_PULLBACK = 0.04  # 最高点回撤 4%
SELL_ROUNDS = 2       # 连续 2 轮回撤才卖
PIN_DROP = 0.02       # 防插针：单轮下砸 >2%
RECOVER_TOL = 0.98    # 趋势恢复：反弹 ≥ 最高 × 0.98 → 取消卖出


def trend_up_trigger(price_hist: list[float], vol_hist: list[float]) -> tuple[bool, str]:
    """买入触发：当前价创新高 + 窗口内有放量轮。

    price_hist: 最近 N 轮收盘价（含当前，末尾为最新）
    vol_hist:   最近 N 轮成交量（含当前，末尾为最新）
    返回 (是否触发, 原因)
    """
    if len(price_hist) < HI_WINDOW + 1 or len(vol_hist) < HI_WINDOW + 1:
        return False, "窗口不足"
    last = price_hist[-1]
    hi = max(price_hist[-HI_WINDOW - 1:-1])
    if last < hi * HI_TOL:
        return False, f"非新高(前{HI_WINDOW}轮高{hi:.2f}, 现{last:.2f})"
    # 窗口内任一轮放量（量 > 该轮前 5 轮均量 × VOL_MULT）
    for j in range(len(vol_hist) - HI_WINDOW, len(vol_hist)):
        v5 = vol_hist[max(0, j - 5):j]
        v5 = [v for v in v5 if v > 0]
        if len(v5) >= 3 and vol_hist[j] > sum(v5) / len(v5) * VOL_MULT:
            return True, f"创新高{last:.2f}+窗口放量"
    return False, f"创新高但窗口无放量"


def check_buy_confirm(price_hist: list[float], vol_hist: list[float],
                      base: float, stand_count: int) -> tuple[str, float, int]:
    """买入确认器状态机（每轮调用）。

    返回 (action, base, stand_count)：
      action: 'trigger' 首次触发（记录 base）
              'stand'   站稳一轮（stand_count+1）
              'reset'   站不稳/跌破，重置
              'ready'   站稳 STAND_ROUNDS 轮 → 可以买入
    """
    if base <= 0:
        ok, _ = trend_up_trigger(price_hist, vol_hist)
        if ok:
            return "trigger", price_hist[-1], 0
        return "wait", 0.0, 0
    # 已触发：检查站稳
    last = price_hist[-1]
    thr = base * STAND_TOL
    if last < thr:
        return "reset", 0.0, 0
    new_count = stand_count + 1
    if new_count >= STAND_ROUNDS:
        return "ready", base, new_count
    return "stand", base, new_count


def check_sell_confirm(highest: float, last: float, prev_last: float,
                       pullback_count: int) -> tuple[bool, str, int]:
    """卖出确认器（每轮调用）。

    趋势下跌确认：last <= highest × (1-SELL_PULLBACK) 连续 SELL_ROUNDS 轮 → 卖
    防诱空洗盘：
      - 插针：单轮下砸 >PIN_DROP 且本轮收回（last >= prev_last）→ 重置计数
      - 趋势恢复：last >= highest × RECOVER_TOL → 重置计数
    返回 (是否卖出, 原因, 新计数)
    """
    if highest <= 0 or last <= 0:
        return False, "无数据", pullback_count
    # 趋势恢复：反弹回最高点 98% 以上 → 取消卖出
    if last >= highest * RECOVER_TOL:
        return False, "趋势恢复", 0
    # 插针：单轮大跌后立即收回 → 洗盘，不计数
    if prev_last > 0 and last >= prev_last and prev_last < highest * (1 - PIN_DROP):
        return False, "插针收回", 0
    # 回撤判定
    if last <= highest * (1 - SELL_PULLBACK):
        new_count = pullback_count + 1
        if new_count >= SELL_ROUNDS:
            return True, f"回撤{SELL_PULLBACK*100:.0f}%连续{new_count}轮", new_count
        return False, f"回撤{SELL_PULLBACK*100:.0f}%第{new_count}轮", new_count
    return False, "回撤不足", 0
