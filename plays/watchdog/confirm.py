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

import os

# ── 买入确认器参数 ──
HI_WINDOW = 10        # 创新高窗口（轮）：当前价需 ≥ 前 10 轮最高
HI_TOL = 0.995        # 创新高容差：允许 0.5%（分钟噪声）
VOL_MULT = 1.5        # 放量倍数：窗口内任一轮量 > 前 5 轮均量 × 1.5
STAND_ROUNDS = 3      # 站稳轮数：触发后连续 3 轮不跌回触发价 0.5% 以下
STAND_TOL = 0.995
# 2026-08-21 回调低吸（用户拍板方向2，回测 100 笔 30min+0.30%/胜率52%
# vs 追拉升 -1.2~-2.2%）：强势票(当日涨幅≥3%)从高点回调≥3%企稳后买入——
# 买在支撑位而非拉升末端（追拉升确认器系统性买在 69% 高位区间）。
PULLBACK_STRONG_PCT = float(os.getenv("CONFIRM_PULLBACK_STRONG", "3.0"))  # 当日最高/昨收 ≥3% = 拉升过
PULLBACK_PCT = float(os.getenv("CONFIRM_PULLBACK_PCT", "3.0"))            # 从当日高点回落 ≥3% = 回调到位
PULLBACK_STABLE = int(os.getenv("CONFIRM_PULLBACK_STABLE", "2"))          # 企稳轮数（不创新低）
# 2026-08-13 拉升条件：触发前窗口内必须从低点明显拉升 ≥ 此百分比。
# 根因（8/13 实盘 18 买 0 持续）：确认器"创新高+站稳"偏爱横盘票——
# 冲高完横住的票完美满足站稳 3 轮，买入即趋势停滞（买完就回落）；
# 真趋势票是"拉升中"（价格仍在加速），横盘创新高（拉升 <2%）不触发。
# 2026-08-17 调 2.0→1.5：两天 snapshot_log 回放（0814 弱势/0817 强势），
# 触发 +63%~+97%，30min 均收益改善（-0.58→-0.43 / -0.22→-0.15），
# 胜率持平——watching 票主要卡"拉升不足 2%"，放宽只提转化不降质。
RISE_MIN_PCT = float(os.getenv("CONFIRM_RISE_MIN_PCT", "1.5"))
# 2026-08-17 触发时"仍在涨"容差（D 变体，回放 0814/0817 验证）：
# 当前轮 ≥ 上一轮×此容差 才算"拉升中"；否则是"拉高后横盘"（603284 实测
# 13:19 急拉 45.95 → 13:20 横盘 45.85 仍被创新高 0.5% 容差放行，买在滞涨位）。
# 回放：触发仅 -3%（vs 只放宽拉升门槛），胜率/30min 收益不降（0817 50%/-0.03%）。
RISE_RISING_TOL = float(os.getenv("CONFIRM_RISE_RISING_TOL", "0.998"))
# 2026-08-14 拉升持续性（用户拍板，8/14 实盘 5 买 4 亏验证）：
# 拉升必须跨越 ≥RISE_SPAN_ROUNDS 轮且创新高 ≥RISE_PEAKS 次（多波）。
# 根因：仅"拉升≥2%"仍捕捉 1-3 分钟急拉（索菱/江海/黄河 8/14 实测）——
# 急拉正是脉冲形态；趋势是持续多波推进（大东方 8/13：6 分钟 3 次新高）。
RISE_SPAN_ROUNDS = int(os.getenv("CONFIRM_RISE_SPAN", "4"))
RISE_PEAKS = int(os.getenv("CONFIRM_RISE_PEAKS", "2"))
# 2026-08-14 当日形态过滤（1 周 117 笔验证：high_decay 35% 胜率 -0.86%，
# 唯一明显负收益形态；rising 58% +0.39% 最好，不能拦）。触发候选时由
# watchdog 拉 THS 当日分时算形态，high_decay（高位衰落）不触发。
SHAPE_HIGH_DECAY_POS = float(os.getenv("SHAPE_HIGH_DECAY_POS", "0.65"))
SHAPE_HIGH_DECAY_DROP = float(os.getenv("SHAPE_HIGH_DECAY_DROP", "-1.5"))
# 2026-08-14 去钝化（用户拍板）：站稳 3 轮→2 轮。原 3 轮=横盘容忍 3 分钟，
# 买入时已错过拉升段（老百姓 8/14：09:51 冲高，3 轮站稳后买入已在回落）。
STAND_ROUNDS = int(os.getenv("STAND_ROUNDS", "2"))

# ── 卖出确认器参数 ──
# 2026-08-18 回撤 4%→2%（用户拍板，106 笔次日回测）：
# 4% 把隔夜低开放大成 3-4% 实亏（0817/0818 早上 13 笔止损全 3%+，0818 单日 -1102）；
# 2% 确认跌破才卖（防诱空：低开 -1% 不卖等反弹），平均 -0.98%→-0.76%，
# 亏>3% 从 33 笔→0 笔（单笔最大亏损封顶 -2%）。
SELL_PULLBACK = float(os.getenv("SELL_PULLBACK", "0.02"))
# 2026-08-18 固定止损（8 月初尾部保护机制，0818 拍板恢复）：跌破入场价 4% 必卖。
# 作用=亏损封顶（单笔最大 -4%），与"最高点回撤 2%"互补——高位横盘票回撤线触发慢，
# 入场价基准保证"从买入算亏 4% 必卖"，构成小亏换大赚的尾部（8 月初 21 笔 -1091 元）。
FIXED_STOP = float(os.getenv("FIXED_STOP", "0.04"))
# 2026-08-14 去钝化（用户拍板）：回撤确认 2 轮→1 轮。原 2 轮=等 2 分钟确认，
# 确认时已 -5~6%（天山 8/13 -7.4% 才卖，第一轮 -4% 就该跑）。
# 防插针由 PIN_DROP 判定保留（单轮下砸>2% 且收回且回到回撤线以上 → 不卖）。
SELL_ROUNDS = int(os.getenv("SELL_ROUNDS", "1"))
PIN_DROP = 0.02       # 防插针：单轮下砸 >2%
# 2026-08-18 恢复线 0.98→0.99：SELL_PULLBACK 降到 0.02 后回撤线(0.98)与恢复线
# 重合 → 插针分支被趋势恢复短路失效（last>0.98 恒先命中恢复）。0.99 让
# "深砸后收回但未创新高"(0.98<last<0.99) 走插针重置，只有反弹回 99% 才算恢复。
RECOVER_TOL = 0.99    # 趋势恢复：反弹 ≥ 最高 × 0.99 → 取消卖出


def classify_intraday_shape(prices: list[float]) -> str:
    """当日分时形态分类（2026-08-14，基于 THS 当日分钟价序列）。

    返回: 'high_decay' 高位衰落(高点回落+高位反抽=脉冲, 不买)
          'rising' 持续拉升(贴顶) / 'low_rise' 低位震荡向上 / 'flat' 横盘
    验证(1周117笔): high_decay 35%胜率-0.86% 最差; rising 58%+0.39% 最好。
    """
    if len(prices) < 10:
        return "flat"
    hi, lo = max(prices), min(prices)
    last = prices[-1]
    if hi <= lo:
        return "flat"
    pos = (last - lo) / (hi - lo)
    drop_hi = (last / hi - 1) * 100
    if pos > SHAPE_HIGH_DECAY_POS and drop_hi < SHAPE_HIGH_DECAY_DROP:
        return "high_decay"
    return "ok"


def trend_up_trigger(price_hist: list[float], vol_hist: list[float]) -> tuple[bool, str]:
    """买入触发：当前价创新高 + 窗口内有放量轮。

    price_hist: 最近 N 轮收盘价（含当前，末尾为最新）
    vol_hist:   最近 N 轮成交量（含当前，末尾为最新）
    返回 (是否触发, 原因)
    """
    if len(price_hist) < HI_WINDOW + 1 or len(vol_hist) < HI_WINDOW + 1:
        return False, "窗口不足"
    last = price_hist[-1]
    # 2026-08-17 触发时"仍在涨"（D 变体）：当前轮 ≥ 上一轮×容差才算拉升中。
    # 拦"拉高后横盘"——603284 13:19 急拉 45.95 → 13:20 横盘 45.85 仍被
    # 创新高 0.5% 容差判为"新高"放行，买在滞涨位；真趋势触发时点还在涨。
    if len(price_hist) >= 2 and last < price_hist[-2] * RISE_RISING_TOL:
        return False, f"触发轮回落(现{last:.2f}<上轮{price_hist[-2]:.2f})"
    hi = max(price_hist[-HI_WINDOW - 1:-1])
    if last < hi * HI_TOL:
        return False, f"非新高(前{HI_WINDOW}轮高{hi:.2f}, 现{last:.2f})"
    # 2026-08-13 拉升条件：入场前窗口内必须从低点明显拉升（≥ RISE_MIN_PCT）。
    # 横盘创新高（拉升不足）＝冲高完横住＝趋势停滞，买完即回落，不触发。
    _lo = min(price_hist[-HI_WINDOW - 1:-1])
    _rise = (last / _lo - 1) * 100 if _lo > 0 else 0
    if _rise < RISE_MIN_PCT:
        return False, f"横盘创新高(拉升{_rise:.1f}%<{RISE_MIN_PCT}%)"
    # 2026-08-14 拉升持续性：排除 1-3 分钟急拉（脉冲形态）。
    # ① 拉升跨越轮数：窗口内低点 → 当前 ≥ RISE_SPAN_ROUNDS 轮
    _seg = price_hist[-HI_WINDOW - 1:-1]
    _lo_idx = min(range(len(_seg)), key=lambda i: _seg[i])
    _span = len(_seg) - 1 - _lo_idx
    if _span < RISE_SPAN_ROUNDS:
        return False, f"急拉(仅{_span}轮<{RISE_SPAN_ROUNDS})"
    # ② 多波：窗口内创新高次数 ≥ RISE_PEAKS（单波冲高=脉冲，多波推进=趋势）
    _peaks = 0
    _h = _seg[0]
    for _p in _seg:
        if _p > _h:
            _peaks += 1
            _h = _p
    if _peaks < RISE_PEAKS:
        return False, f"单波(峰{_peaks}<{RISE_PEAKS})"
    # 窗口内任一轮放量（量 > 该轮前 5 轮均量 × VOL_MULT）
    for j in range(len(vol_hist) - HI_WINDOW, len(vol_hist)):
        v5 = vol_hist[max(0, j - 5):j]
        v5 = [v for v in v5 if v > 0]
        if len(v5) >= 3 and vol_hist[j] > sum(v5) / len(v5) * VOL_MULT:
            return True, f"创新高{last:.2f}+拉升{_rise:.1f}%/{_span}轮/{_peaks}峰+放量"
    return False, f"创新高+拉升{_rise:.1f}%但窗口无放量"


def pullback_trigger(price_hist: list[float], vol_hist: list[float],
                     day_high: float, prev_close: float) -> tuple[bool, str]:
    """回调低吸触发（2026-08-21 用户拍板方向2，替代追拉升）。

    1. 强势确认：当日最高/昨收 ≥ PULLBACK_STRONG_PCT —— 有资金拉升过
    2. 回调到位：当前价从当日最高回落 ≥ PULLBACK_PCT —— 回到支撑区
    3. 企稳：最近 PULLBACK_STABLE 轮不创新低 —— 止跌确认
    """
    if len(price_hist) < PULLBACK_STABLE + 2:
        return False, "窗口不足"
    if day_high <= 0 or prev_close <= 0:
        return False, "无昨收/当日高点"
    last = price_hist[-1]
    up = (day_high / prev_close - 1) * 100
    if up < PULLBACK_STRONG_PCT:
        return False, f"未拉升过(高{day_high:.2f}/昨收{prev_close:.2f} {up:.1f}%<{PULLBACK_STRONG_PCT}%)"
    drop = (last / day_high - 1) * 100
    if drop > -PULLBACK_PCT:
        return False, f"回调不足(现{last:.2f}/高{day_high:.2f} {drop:.1f}%>-{PULLBACK_PCT}%)"
    # 企稳：最近 PULLBACK_STABLE 轮不创新低（尾部最低 >= 回调段低点）
    _tail = price_hist[-PULLBACK_STABLE - 1:-1]
    _seg_lo = min(price_hist[:-1]) if len(price_hist) > 1 else last
    if last < _seg_lo:
        return False, "仍在创新低"
    if len(_tail) >= 2 and _tail[-1] < _tail[-2]:
        return False, "企稳不足(上轮更低)"
    return True, f"回调低吸: 高{day_high:.2f}回撤{drop:.1f}% 企稳@ {last:.2f}"


def check_buy_confirm(price_hist: list[float], vol_hist: list[float],
                      base: float, stand_count: int,
                      day_high: float = 0.0, prev_close: float = 0.0) -> tuple[str, float, int]:
    """买入确认器状态机（每轮调用）。

    2026-08-21：base<=0 时优先回调低吸触发（用户拍板方向2）——传入
    day_high/prev_close 即走回调低吸；不传则回退原追拉升触发（旧测试兼容）。

    返回 (action, base, stand_count)：
      action: 'trigger' 首次触发（记录 base）
              'stand'   站稳一轮（stand_count+1）
              'reset'   站不稳/跌破，重置
              'ready'   站稳 STAND_ROUNDS 轮 → 可以买入
    """
    if base <= 0:
        if day_high > 0 and prev_close > 0:
            ok, _ = pullback_trigger(price_hist, vol_hist, day_high, prev_close)
        else:
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
                       pullback_count: int, entry_price: float = 0.0,
                       prev_close: float = 0.0, is_overnight: bool = False,
                       now=None) -> tuple[bool, str, int]:
    """卖出确认器（每轮调用）。

    2026-08-18 组合离场（恢复 8 月初盈利机制，用户拍板，0818 回测 13 只隔夜仓
    高位出场 9/13 优于实际止损 +~1200 元）：
      1) 高位出场：隔夜仓开盘窗口(09:30-09:45)低开(现价<昨收×0.995) → 主动卖
         （不等回撤确认——隔夜回吐开盘集中释放，等确认反而卖更低）
      2) 固定止损：跌破入场价×(1-FIXED_STOP) → 卖（尾部兜底，单笔最大亏封顶）
      3) 趋势恢复 / 插针 / 回撤止损（原逻辑）
    新参数可选（默认不启用新机制，旧调用/测试兼容）。

    返回 (是否卖出, 原因, 新计数)
    """
    if highest <= 0 or last <= 0:
        return False, "无数据", pullback_count
    # 1) 高位出场：隔夜仓开盘低开即卖（09:30-09:45 窗口，昨收 0.5% 低开）
    if is_overnight and prev_close > 0 and now is not None:
        _hhmm = now.hour * 100 + now.minute
        if 930 <= _hhmm < 945 and last < prev_close * 0.995:
            return True, f"高位出场: 隔夜低开{(prev_close / last - 1) * 100:.1f}% 开盘卖", 0
    # 2) 固定止损：跌破入场价 -FIXED_STOP → 尾部兜底
    if entry_price > 0 and last <= entry_price * (1 - FIXED_STOP):
        return True, f"固定止损: 入场{entry_price:.2f} 现价{last:.2f}", 0
    # 趋势恢复：反弹回最高点 99% 以上 → 取消卖出
    if last >= highest * RECOVER_TOL:
        return False, "趋势恢复", 0
    # 插针：上一轮深跌(>2%) + 本轮收回 + 价格回到回撤线以上 → 洗盘，不计数
    # 2026-08-13 修复：原条件(last>=prev_last 且 prev_last<最高×0.98)过宽——
    # 持续下跌中的微小反弹(13.40→13.43)也被判"插针收回"，回撤计数永远清零，
    # 回撤 7.8% 仍不卖（天山铝业/金桥/武汉凡谷 08-13 实测）。
    if prev_last > 0 and last >= prev_last \
            and prev_last < highest * (1 - PIN_DROP) \
            and last > highest * (1 - SELL_PULLBACK):
        return False, "插针收回", 0
    # 回撤判定
    if last <= highest * (1 - SELL_PULLBACK):
        new_count = pullback_count + 1
        if new_count >= SELL_ROUNDS:
            return True, f"回撤{SELL_PULLBACK*100:.0f}%连续{new_count}轮", new_count
        return False, f"回撤{SELL_PULLBACK*100:.0f}%第{new_count}轮", new_count
    return False, "回撤不足", 0
