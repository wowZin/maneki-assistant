# 打板玩法 V2 · 架构设计

## 背景

现有 pipeline 基于同花顺热门榜（100只）+ 缓存扫描，存在三个致命缺陷：

1. **数据源失效**：热门榜按搜索热度排序，涨停/上涨比例极低，活跃信号被大量噪音淹没
2. **时机缺失**：10 分钟 cron 间隔太长，股票从 +2% 拉到涨停只需 3-5 分钟，每次发现时已买不进去
3. **候选池窄**：100 只热门股 + ~200 只缓存 = 300 只，每日涨停股超 50% 不在池内

## V2 核心思路

生产者-消费者模式：**每分钟全量扫描 → 涨幅>0的入栈（去重+涨速排序） → 评分线程从栈顶取20只**

```
每分钟循环:
  scanner.scan_batch(候选池) → 实时行情
       ↓
  更新栈：去重 / 涨速排序 / 死票淘汰
       ↓
  评分线程取栈顶20只 → analysis文件 → 阈值推送
```

## 数据存储

| 数据 | 位置 | 格式 | 更新频率 | 说明 |
|------|------|------|---------|------|
| **候选池** | `data/pool/pool_{date}.json` | `list[{code,name,circ_mv}]` | 每日1次(开盘) | 50-300亿主板非ST非次新 |
| **栈(待评分)** | `data/queue/queue_{date}.json` | `dict{items, prev_pct}` | 每分钟覆写 | 内存为主，文件做持久化(防重启丢失) |
| **评分结果** | `data/analysis/{date}_{time}.json` | `list[{code,name,scores,total_score}]` | 每次评分写入 | 供复盘/飞书推送读取 |
| **推送记录** | `data/pushed/{date}_{time}.json` | `list[{code,name,total_score}]` | 推送时写入 | 沿用现有格式 |
| **daemon 心跳** | `data/health/pipeline_heartbeat.json` | `{pid,ts,epoch}` | 每轮写入 | health_patrol 检查用 |

## 模块拆分

### 1. pool_builder.py — 候选池构建（每日1次）

```
输入: 无（自动从 daily_basic 拉取）
输出: data/pool/pool_{date}.json
流程:
  1. call_tushare('daily_basic') — 全市场，20积分
  2. 过滤: 主板(00/60) + 非ST + 非次新(>120天) + 市值50-300亿
  3. 按流通市值降序写入 pool.json
```

**单测**: `tests/pipeline/test_pool_builder.py`
- ✓ 返回list且>=500只
- ✓ 不含ST股
- ✓ 不含创业板/科创板/北交所
- ✓ 市值在50-300亿区间

### 2. stack.py — 待评分栈管理

```
class ScoreStack:
    属性:
      items: dict[code → {pct_chg, speed, ts}]
      prev_pct: dict[code → 上次pct_chg]  # 用于计算涨速

    方法:
      update(quotes: dict[code → batch_quote], name_map=None):
        遍历quotes:
          涨幅<=0 → items剔除(死票)
          新code(涨幅>0) → 入栈
          已存code → 更新涨幅+涨速

        for each item:
          speed = pct_chg - prev_pct[code]
          score = pct_chg * 0.3 + speed * 0.7

        按score降序排序
        prev_pct = 本轮涨幅

      pop_top(n: int) → list[Item]:
        从栈顶取评分（不移除，下轮重排）

      to_dict() / from_dict():
        持久化/恢复
```

**单测**: `tests/pipeline/test_stack.py`
- ✓ 涨幅>0的票入栈
- ✓ 涨幅<=0的票踢出
- ✓ 涨速计算正确
- ✓ 排序按 score = pct*0.3 + speed*0.7
- ✓ JSON序列化/反序列化
- ✓ 空栈pop_top返回空list

### 3. scanner.py — 候选池批量扫描（带 RPS 限流）

```
每轮循环:
  1. 读 pool.json
  2. 分片调用 ths.get_batch_quotes(chunk)
  3. chunk 间 sleep 控制 RPS（默认 30，可配 LIMIT_UP_SCAN_RPS）
  4. 结果注入 realtime_ctx
  5. 返回 {short_code: quote}
```

**单测**: `tests/pipeline/test_scanner.py`
- ✓ 分片调用
- ✓ 返回字段包含 pct_chg / vol_ratio / turnover / inner_vol / outer_vol
- ✓ chunk 间有 sleep 限流

### 4. pipeline.py — 主循环 daemon

```
1. pool_builder 初始化(如pool不存在或过期)
2. while True:
   a. scanner.scan_batch() → stack.update()
   b. 取栈顶20只 → stage1_rough 五维度评分
   c. total_score >= 55 → 直接推送
      total_score [45,55) → stage2_deep L2确认 → 推送/拒绝
      total_score < 45 → 丢弃
   d. save_analysis() 写入 data/analysis/{date}_{time}.json
   e. check_and_push() 调用 pipeline_feishu.push_feishu()
   f. 覆写 queue.json
   g. 写入 heartbeat
```

启动命令:
```bash
python plays/limit_up/pipeline.py --daemon
```

参数:
- `--daemon`: 常驻 daemon 模式
- `--sim-time HHMM`: 模拟时间
- `--sim-rounds N`: 模拟 N 轮后退出

### 5. filter.py — 简化（只保留实时规则）

原静态规则拆分：
- 规则1(ST)/2(次新)/3(板块)/5(市值) → 移到 pool_builder
- 规则6(换手率) → 旧T-1数据，全部弃用（候选池已50-300亿起步）
- 规则4(停牌) → batch_quotes无数据自动忽略
- 规则7(一字板) → 保留，从实时涨幅+换手判断

```python
def filter_realtime(quote: dict[str, Any]) -> tuple[bool, str]:
    """实时过滤，只保留一字板判断"""
    pct = float(quote.get('pct_chg',0) or 0)
    turnover = float(quote.get('turnover',0) or 0)
    if pct >= 9.5 and turnover < 0.5:
        return True, '一字板涨停'
    if pct <= -9.5 and turnover < 0.5:
        return True, '一字跌停'
    return False, ''
```

`filter_candidates(candidates)` 已废弃，静态过滤在候选池阶段完成。

**单测**: `tests/pipeline/test_filter.py`
- ✓ 一字板(涨幅>9.5%+换手<0.5%)被过滤
- ✓ 正常涨停(涨幅>9.5%+换手>0.5%)不被过滤
- ✓ 涨幅不足5%不被过滤

### 6. realtime_ctx.py — 实时数据桥接

```python
set_realtime_quotes(quotes: dict[str, dict])   # 注入完整 batch_quotes
set_l1_snapshots(snapshots: dict[str, dict])   # 注入 WS L1 盘口
get_realtime_pct(code) -> float | None
get_inner_outer_ratio(code) -> float | None
get_vol_ratio(code) -> float | None
get_turnover(code) -> float | None
get_bid_ask_ratio(code) -> float | None
```

所有操作受 `threading.RLock` 保护。

**单测**: `tests/pipeline/test_realtime_ctx.py`

### 7. pusher.py — 推送判断与去重

```python
check_and_push(results: list[dict], data_dir: Path) -> list[dict]
```

- 过滤 `total_score >= PUSH_THRESHOLD`
- 从 `data/pushed/{date}*.json` 去重
- 调用 `pipeline_feishu.push_feishu()` 发送飞书卡片
- 不再重复写 pushed 文件

**单测**: `tests/pipeline/test_pusher.py`

## 数据流全景

```
pool_builder.py (每日1次, 开盘, 20积分)
  → data/pool/pool_20260710.json (~1195只)

[每分钟循环]
  scanner.py:
    scan_batch(~1195只) → 分片限流
       ↓
  stack.py:
    更新栈 → 去重/涨速排序/死票淘汰
       ↓
  pipeline.py:
    取栈顶20只 → stage1_rough 五维度评分
       ↓
    total_score ≥ 55 → 推送
    total_score [45,55) → stage2_deep L2确认 → 推送/拒绝
    total_score < 45 → 丢弃
       ↓
    save_analysis() → data/analysis/{date}_{time}.json
    check_and_push() → pipeline_feishu.push_feishu() → data/pushed/{date}_{time}.json
```

## 总分说明

- daemon 盘中使用 `_raw_score` 的五维度 Top-3 加权，结果字段为 `total_score`，并附加 `score_mode="daemon_weighted"`。
- 一次性流程 `pipeline_feishu.py` 使用 `total.py::total_score(row)`（XGBoost/quality_combo），字段同样为 `total_score`。
- 展示/下游统一读取 `total_score`；旧文件中的 `total` 字段做兼容 fallback。

## 系统服务

推荐用 systemd 管理 daemon（仓库内未提供 unit 文件，需自行创建）：

```ini
[Unit]
Description=Maneki limit_up pipeline daemon
After=network.target

[Service]
Type=simple
WorkingDirectory=/path/to/maneki-assistant
ExecStart=python3 plays/limit_up/pipeline.py --daemon
Restart=always
RestartSec=5
User=your-user

[Install]
WantedBy=multi-user.target
```

健康巡检（不杀 daemon，仅检查心跳）：
```bash
python plays/limit_up/health_patrol.py --dry-run
```

## 删除项

- cron job: `maneki-morning-*`, `maneki-afternoon-*`（pipeline cron）
- `data/scan_cache.json`
- pipeline.py 中 scan_surge() 热门榜逻辑
- filter.py 中 Tushare daily_basic 逐股查询（改为 pool_builder 一次搞定）

## 不变项

- `strategies/` 评分维度函数签名 — 不变
- `backtest/` 回测框架 — 不变
- `factors/` 因子 — 不变
- `data/pushed/` 推送格式 — 不变
- 飞书推送卡片格式 — 不变
