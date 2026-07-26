# 盯盘指标（已废弃，保留存档）

> ⚠️ 本文描述的是旧版 KAMA/ADX/ATR 指标方案，已废弃。
> 当前 indicators.py 只保留两个函数：`price_features(daily_rows)`（日级特征：
> position_20d/trailing_5/trailing_10/pullback 等）和 `realtime_row(...)`
> （L1 快照 + 日级特征 + 维度分 → 模型输入行）。信号见 signal-engine.md。
> 分钟 K 相关（klines 形参、minute_momentum）已于 2026-07-26 删除（从未使用）。

## 策略定位

双引擎动量-均值回归混合策略。主引擎自适应趋势识别（KAMA + ADX），副引擎均值回归精确入场（布林带 + RSI）。仅在大方向回调末端捕捉信号。

**适用标的**：A股日线（参数组 A）。

## 参数预设

| 指标 | A股 | 商品期货 | 外汇/加密 |
|------|:--:|:--:|:--:|
| KAMA | (10, 2, 30) | (14, 2, 40) | (20, 3, 50) |
| RSI | 3 | 5 | 7 |
| ATR | 20 | 20 | 20 |
| BB | (20, 2) | (20, 2) | (20, 2) |
| ADX | 14 | 14 | 14 |

## 指标计算

### KAMA（Kaufman 自适应均线）

```
ER = |close[n:] - close[:-n]| / sum(|close[i] - close[i-1]| for i in window)
SC = (ER × (2/(fast+1) - 2/(slow+1)) + 2/(slow+1))²
KAMA[0] = mean(close[:n+1])
KAMA[i] = KAMA[i-1] + SC × (close[i] - KAMA[i-1])
```

### EMA

```
EMA[0] = mean(series[first_valid : first_valid + period])
alpha = 2 / (period + 1)
EMA[i] = alpha × series[i] + (1 - alpha) × EMA[i-1]
```

自动跳过前导 NaN 值从第一个有效数据点开始计算。

### SMA

```
SMA[i] = mean(close[i-period+1 : i+1])
```

### ADX(14) + +/-DI

标准 ADX/DMI 计算，含 +DI、-DI、ADX。

### ATR(20)

```
TR = max(high-low, |high-prev_close|, |low-prev_close|)
ATR = EMA(TR, 20)
```

### 布林带 (20, 2)

```
MID = SMA(close, 20)
STD = rolling_std(close, 20)
UPPER = MID + 2 × STD
LOWER = MID - 2 × STD
BW_PCT = 当前带宽在近20日带宽中的百分位排名
```

### RSI(3)

标准 Wilder RSI 计算。

## 信号流程

### Step 1：趋势过滤

必须全部满足：

1. KAMA > EMA(KAMA, 20)
2. Close > SMA(20)
3. ADX > 20 且 +DI > -DI

### Step 2：回调待机

RSI 阈值动态调整：
- 布林带宽百分位 < 0.3 → RSI < 20
- 否则 → RSI < 15

同时满足：当前价 ≤ 布林下轨

触发后标记为"观察信号"，记录触发低点。

### Step 3：计分入场

下一根K线时对三项条件计分：

| 条件 | 规则 | 分值 |
|------|------|:--:|
| 价格验证（多） | 当前价 > 信号低点 + 0.3×ATR | 1分 |
| 价格验证（空） | 当前价 < 信号高点 - 0.3×ATR | 1分 |
| 放量 | 当前量 > 20均量 × 1.1 | 1分 |
| 未过度溢价 | direction × (VWAP - 开盘价) < 0.5×ATR | 1分 |

- ≥2分 → 入场，记录入场价
- <2分 → 信号作废，重置为 watching

> **注意**：当前引擎只使用做多方向（direction=1），做空参数为未来预留。

### 出场规则

1. **移动止损**：自入场最高点回落 2×ATR → 平仓
2. **条件时间止损**：持仓>15根K线 且 (ADX<20 或 最大浮盈<0.5×ATR) → 平仓
3. **趋势反转**：KAMA下穿EMA 且 -DI>+DI → 平仓
4. **止盈提醒**：浮盈达 3×ATR → 推送提醒建议平50%

## 引擎运行时行为（旧版描述，已不适用）

- 每 60 秒扫描一轮（cron 09:20 拉起，15:05 自退）
- 每日首次扫描时更新日线（Tushare daily 接口，取 120 条历史数据）
- 实时数据（价格/VWAP/盘口）从 ws_daemon 共享内存 /dev/shm/ws_snap.json 读取，无分钟 K
- 信号触发和出场均通过飞书推送