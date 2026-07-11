# pipeline.py — 打板 daemon 主循环

## 作用

`plays/limit_up/pipeline.py` 是涨停预测玩法的常驻 daemon，负责：

1. 交易日前构建/加载候选池
2. 盘中每分钟全量扫描候选池行情
3. 更新待评分栈并按涨速排序
4. 对栈顶股票做五维度粗评 + L2 灰区确认
5. 保存分析结果并触发飞书推送
6. 写入心跳文件供 health_patrol 巡检

## 启动方式

### 常驻 daemon

```bash
python plays/limit_up/pipeline.py --daemon
```

### 模拟模式（测试用）

```bash
# 从 09:30 开始模拟，跑 3 轮后退出
python plays/limit_up/pipeline.py --daemon --sim-time 0930 --sim-rounds 3

# 模拟时间每轮推进 60 秒
python plays/limit_up/pipeline.py --daemon --sim-time 0930 --sim-tick 60 --sim-rounds 10
```

### 单次运行（非 daemon）

```bash
python plays/limit_up/pipeline.py
```

单次模式执行一轮完整扫描评分后退出，不进入常驻循环。

## 命令行参数

| 参数 | 说明 |
|------|------|
| `--daemon` | 常驻 daemon 模式 |
| `--sim-time HHMM` | 模拟起始时间，如 `0930` |
| `--sim-tick SECONDS` | 模拟模式下每轮推进的秒数 |
| `--sim-rounds N` | 模拟模式下运行 N 轮后退出（0=无限） |

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `STAGE1_TOP_N` | 20 | 栈顶取多少只进入粗评 |
| `ULTIMATE_PUSH_THRESHOLD` | 55 | 直接推送阈值 |
| `L2_GREY_LOW` | 45 | L2 灰区下限 |
| `POOL_TIME` | 915 | 建池时间（HHMM） |
| `TRADE_START` | 930 | 上午开盘时间 |
| `TRADE_END` | 1130 | 上午收盘时间 |
| `TRADE_PM_START` | 1300 | 下午开盘时间 |
| `TRADE_PM_END` | 1500 | 下午收盘时间 |
| `LIMIT_UP_SCAN_RPS` | 30 | 同花顺扫描每秒请求数 |
| `FEISHU_TEST_MODE` | false | 飞书测试模式 |
| `HEARTBEAT_INTERVAL_SECONDS` | 60 | 心跳写入间隔 |

## 主循环流程

```
1. 交易日判断（缓存）
2. 开盘前构建候选池（pool_builder.ensure_pool）
3. 交易时段内循环：
   a. scanner.scan_batch() 批量扫描
   b. filter_realtime() 实时过滤一字板
   c. stack.update() 更新待评分栈
   d. pop_top(STAGE1_TOP_N) 取栈顶 N 只
   e. stage1_rough() 五维度并行评分
   f. stage2_deep() 灰区 L2 确认
   g. save_analysis() 写入 analysis
   h. check_and_push() 触发飞书推送
   i. save_queue() 持久化栈
   j. _write_heartbeat() 写入心跳
4. 收到 SIGINT/SIGTERM 时优雅退出，关闭 WS
```

## 异常处理

主循环每轮都有 `try/except Exception` 包裹，单轮失败会打印异常并继续下一轮，不会因单次网络抖动或数据异常导致 daemon 崩溃。

## WS 生命周期

- 每轮对栈顶股票 subscribe L1
- 灰区股票额外 subscribe L2/L10
- 轮结束后 unsubscribe 不再在栈顶的股票
- daemon 退出时调用 `_close_ws()` 关闭连接

## 心跳与健康检查

- pidfile: `data/health/pipeline_daemon.pid`
- heartbeat: `data/health/pipeline_heartbeat.json`

`health_patrol.py` 读取心跳文件判断 daemon 是否存活，不再按运行时长杀进程。
