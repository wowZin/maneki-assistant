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
  batch_quotes(候选池) → 实时行情
       ↓
  更新栈：去重 / 涨速排序 / 死票淘汰
       ↓
  评分线程取栈顶20只 → 信号池 → 阈值推送
```

## 数据存储

| 数据 | 位置 | 格式 | 更新频率 | 说明 |
|------|------|------|---------|------|
| **候选池** | `data/pool/pool_{date}.json` | `list[code]` | 每日1次(开盘) | 50-200亿主板非ST非次新，~1100只 |
| **栈(待评分)** | `data/queue/queue.json` | `list[{code,pct_chg,speed,ts}]` | 每分钟覆写 | 内存为主，文件做持久化(防重启丢失) |
| **信号池(评分结果)** | `data/signals/signals.json` | `list[{code,scores,total,ts}]` | 每次评分追加 | 供复盘/飞书推送读取 |
| **推送记录** | `data/pushed/{datetime}.json` | `list[{code,scores,total}]` | 推送时写入 | 沿用现有格式 |

## 模块拆分

### 1. pool_builder.py — 候选池构建（每日1次）

```
输入: 无（自动从 daily_basic 拉取）
输出: data/pool/pool_{date}.json
流程:
  1. call_tushare('daily_basic') — 全市场，20积分
  2. 过滤: 主板(00/60) + 非ST + 非次新(>120天) + 市值50-200亿
  3. 写入 pool.json
```

**单测**: `tests/pipeline/test_pool_builder.py`
- ✓ 返回list且>=500只
- ✓ 不含ST股
- ✓ 不含创业板/科创板/北交所
- ✓ 市值在50-200亿区间

### 2. stack.py — 待评分栈管理

```
class ScoreStack:
    属性:
      items: dict[code → {pct_chg, speed, ts}]
      prev_pct: dict[code → 上次pct_chg]  # 用于计算涨速
    
    方法:
      update(quotes: dict[code → batch_quote]):
        遍历quotes:
          涨幅<=0 → items剔除(死票)
          新code(涨幅>0) → 入栈
          已存code → 更新涨幅+转速
        
        for each item:
          speed = pct_chg - prev_pct[code]
          score = pct_chg * 0.3 + speed * 0.7
        
        按score降序排序
        prev_pct = 本轮涨幅
      
      pop_top(n: int) → list[code]:
        从栈顶取评分（不移除，下轮重排）
      
      to_json() / from_json():
        持久化/恢复
```

**单测**: `tests/pipeline/test_stack.py`
- ✓ 涨幅>0的票入栈
- ✓ 涨幅<=0的票踢出
- ✓ 涨速计算正确
- ✓ 排序按 score = pct*0.3 + speed*0.7
- ✓ JSON序列化/反序列化
- ✓ 空栈pop_top返回空list

### 3. scanner.py — 每分钟行情扫描（替代旧 scan_surge）

```
每60秒循环:
  1. 读 pool.json
  2. ths.get_batch_quotes(pool) → ~19s
  3. 传给 stack.update()
```

**单测**: `tests/pipeline/test_scanner.py`
- ✓ batch_quotes 返回1100只以上
- ✓ 每只含 pct_chg 字段
- ✓ 连接失败时降级（保留上次数据继续运行）

### 4. pipeline.py — 主循环

```
1. pool_builder 初始化(如pool不存在或过期)
2. 恢复上次的queue.json
3. while True:
   a. batch_quotes → stack.update()
   b. 评分线程从stack取20只 → 评分 → signals.json追加
   c. if signals有符合阈值的 → 推送 → pushed.json
   d. 覆写queue.json
   e. sleep(60秒)
```

**单测**: `tests/pipeline/test_pipeline.py`
- ✓ 主循环可启动/停止
- ✓ 评分线程不阻塞扫描
- ✓ 崩溃后能从queue.json恢复

### 5. filter.py — 简化（只保留实时规则）

原7条规则拆分：
- 规则1(ST)/2(次新)/3(板块)/5(市值) → 移到 pool_builder
- 规则6(换手率) → 旧T-1数据，全部弃用（候选池已50-200亿起步）
- 规则4(停牌) → batch_quotes无数据自动忽略
- 规则7(一字板) → 保留，从实时涨幅+换手判断

```python
def filter_realtime(code: str, quote: dict) -> tuple[bool, str]:
    \"\"\"实时过滤，只保留一字板判断\"\"\"
    pct = float(quote.get('pct_chg',0) or 0)
    turnover = float(quote.get('turnover',0) or 0)
    if pct >= 9.5 and turnover < 0.5:
        return True, '一字板'
    if pct <= -9.5 and turnover < 0.5:
        return True, '一字跌停'
    return False, ''
```

**单测**: `tests/pipeline/test_filter.py`
- ✓ 一字板(涨幅>9.5%+换手<0.5%)被过滤
- ✓ 正常涨停(涨幅>9.5%+换手>0.5%)不被过滤
- ✓ 涨幅不足5%不被过滤

## 数据流全景

```
pool_builder.py (每日1次, 开盘, 20积分)
  → data/pool/pool_20260710.json (1100只)

[每分钟循环]
  scanner:
    batch_quotes(1100只) → 19s
       ↓
  stack.py:
    更新栈 → 去重/涨速排序/死票淘汰
       ↓
       ├── 队列有空? → 取20只 → 评分模块 → signals.json
       └── 队列忙? → 跳过评分，继续下一轮扫描

signal_pusher (独立逻辑，读signals.json):
  筛选total_score >= 55的 → 推送飞书 → pushed.json
```

## 系统服务

```
systemd pipeline-daemon.service:
  Type=simple
  ExecStart=python3 plays/limit_up/pipeline.py --daemon
  WorkingDirectory=/root/maneki-agent
  开机自启
  崩溃自动重启(Restart=always)
```

## 删除项

- cron job: `maneki-morning-*`, `maneki-afternoon-*`（pipeline cron）
- `data/scan_cache.json`
- pipeline.py 中 scan_surge() 热门榜逻辑
- filter.py 中 Tushare daily_basic 逐股查询（改为 pool_builder 一次搞定）

## 不变项

- `strategies/` 评分维度 — 不变
- `score.py` / `total.py` 聚合逻辑 — 不变
- `backtest/` 回测框架 — 不变
- `factors/` 因子 — 不变
- `data/pushed/` 推送格式 — 不变
- 飞书推送卡片格式 — 不变
