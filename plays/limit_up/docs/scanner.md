# surge_scanner.py — 盘中异动扫描 → watchdog surge 盯盘

> 2026-07-26 当前实现：`plays/limit_up/surge_scanner.py` 是盘中唯一的扫描器，每 60s 一轮，把异动票路由进 watchdog 盯盘（不再直接推送）。
> 旧 `plays/limit_up/scanner.py`（RPS 限流批量行情封装）仍在仓库中，但生产盘中扫描已由 surge_scanner 取代。

## 启动方式

```bash
python3 plays/limit_up/surge_scanner.py            # 扫描一次
python3 plays/limit_up/surge_scanner.py --daemon   # 每 60s 循环（生产模式，cron 拉起）
python3 plays/limit_up/surge_scanner.py --dry-run  # 只打印路由决策，不写 watchdog/signals
```

- daemon 扫描窗口：**09:35–11:30 / 13:00–15:00**（代码 `935 <= hhmm < 1130 or 1300 <= hhmm < 1500`），非交易日不扫（tushare `trade_cal`，接口失败按星期兜底）。
- pid 守卫：`data/health/surge_scanner.pid`，防 cron 与手动启动撞车。
- 单轮异常（THS 超时/文件竞争）吞掉打印，下轮重试，不杀 daemon。

## 扫描池（先筛后拉，THS 压力最小化）

先在本地定池，再只对池内股票拉行情（约千只/轮）：

| 池 | 口径 | 路由 |
|----|------|------|
| ① 主闸池 | 当日面板 `model_score >= 20`（`SURGE_PANEL_SCORE`） | 直接通过 |
| ② 排雷池 | （昨日涨停 ∪ 前 20 日涨停基因）− 主闸池 | 走排雷检查 |

- 面板：`wiki/raw/limit-up/panel/{date}.parquet`，只取主板（`00`/`60`）。`model_score` 由 pipeline 09:30 全量写回；面板不存在或 `model_score` 未写回时本轮**全走排雷**，且**不写日缓存**（防把"主闸空"锁死一整天，下轮重试）。
- 昨日涨停/涨停基因：tushare `limit_list_d`（一次拉 45 天，limit_type=U），经 `is_tradable_stock` 过滤；基因窗口 = 最近 21 个涨停日中去昨日外的 20 日。
- 日缓存：`data/pool/surge_universe_{date}.json`。
- 名称来源：`data/pool/pool_{date}.json`（缺失时 surge 自治调 `pool_builder.ensure_pool` 补齐）+ 涨停名单。

## 行情与异动窗口

- 行情源：`ths_client.get_batch_quotes_fast`（并发线程池，默认 24 线程，`SURGE_QUOTE_WORKERS`）。
- 异动窗口：**5.0% ≤ 涨幅 < 9.8%**（`SURGE_PCT_LOW` / `SURGE_PCT_HIGH`；上限 9.8 留 0.2% 防已封板，9.0 会丢连板秒板窗口）。

## 排雷条件

| 通道 | 条件 |
|------|------|
| 首板（非昨日涨停） | 3 项全过：量比 ≥ 2（`SURGE_VOL_RATIO`）+ 窄概念联动 ≥ 2 只 + 筹码不压顶 |
| 昨日涨停（连板） | 2 项全过：量比 ≥ 2 + 筹码不压顶 |

- **筹码不压顶**（`cyq_no_pressure`）：T-1 收盘 ≥ T-1 筹码成本中位（`cost_50pct`，读 `panel/cyq_perf.parquet` + `panel/daily/{t1}.parquet`）；无数据不拦。
- **窄概念联动**（`sector_resonance`）：本轮异动候选中同概念票 ≥ `min_peers=2` 只（含自己）；概念成员 > 300 的宽概念剔除（`SURGE_CONCEPT_MAX_SIZE`），否则联动恒真。概念成员读 `panel/concept/concept_members.parquet` 或 `backtest/cache/concept_members.parquet`。

**无数量上限**（2026-07-26 拍板：入盯不设限）。

## 三处写入 + 两份日志

通过的票（全部按 code 去重覆盖，tmp+rename 原子写）：

| 写入 | 路径 | 说明 |
|------|------|------|
| ① watchdog state | `plays/watchdog/data/state.json` | `source="surge"`，必带面板 `dim_scores`（五维度分）与 `daily_basic`（circ_mv/pe/pb）——否则 watchdog realtime_row 的维度分=0，模型分被系统性压低，入场闸（min_model_score=40）永远够不到。写后回读校验，被覆盖则重试（最多 3 次） |
| ② analysis | `plays/limit_up/data/analysis/{date}.json` | 与 pipeline 同构记录 + `source="surge"`；`score_mode` 为 `model_score`（分≥20）或 `surge_screen`（排雷票） |
| ③ pushed 存档 | `plays/limit_up/data/pushed/{date}_surge.json` | 供回测 |

日志：

| 日志 | 路径 | 说明 |
|------|------|------|
| 路由决策 | `plays/limit_up/data/signals/{date}.json` | 每只候选的 route/pass，盘后归档 |
| 候选快照 | `plays/limit_up/data/snapshot_log/{date}.parquet` | 异动候选实时快照（价/买卖一/换手/量比/内外盘/vwap/面板分），盘中模型训练素材 |

## surge 票的下游语义（watchdog 侧）

- 只发【surge】入场信号，无信号不通知（触发/异常均静默，critical 异常仍移除）。
- 盘后（15:00 收盘轮）零信号自动汰换。详见 `plays/watchdog/docs/watchdog.md`。

## 环境变量汇总

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `SURGE_PCT_LOW` | 5.0 | 异动涨幅下限 |
| `SURGE_PCT_HIGH` | 9.8 | 异动涨幅上限（不含） |
| `SURGE_PANEL_SCORE` | 20 | 主闸面板分阈值 |
| `SURGE_VOL_RATIO` | 2.0 | 排雷量比下限 |
| `SURGE_CONCEPT_MAX_SIZE` | 300 | 窄概念成员上限 |
| `SURGE_QUOTE_WORKERS` | 24 | THS 并发线程数 |
