# 打板玩法 · 架构设计（2026-07-26 定稿）

## 总览

面板驱动的四层架构：**夜间一次性构建全市场面板，早盘一次性评分推送，盘中 surge 发现 + watchdog 信号**。
无常驻评分进程（全部 hermes cron 拉起），面板是唯一事实源。

```
00:01  panel_builder   全市场面板（T-1 特征 64 列 + 五维度分）
00:30  concept_cache   概念缓存增量刷新
08:55  ws_daemon       jvQuant WS 独占（共享内存快照）
09:20  watchdog        盯盘引擎拉起（cron，15:05 自退）
09:30  pipeline        竞价刷面板 → 全量评分 → Top3+≥55 推送（一次性，~4s）
09:35  surge_scanner   每 60s 扫描（发现异动 → watchdog/analysis/pushed）
15:00  watchdog        EOD 汰换（surge 零信号票移除）
18:00  review          收盘复盘
20:00  wiki compile    数据归档
```

## 一、面板（wiki/raw/limit-up/panel/{date}.parquet）

panel_builder 每日 00:01 构建：全市场非 ST（SH/SZ 主板+创业+科创，4993 只），
`build_pit_features()` 产出 59 个 T-1 PIT 特征 + `_add_strategy_scores()` 补 5 个维度分
（fundamental/technical/fundflow/sentiment/shortterm），共 64 个模型输入列。

pipeline 09:30 追加两组运营列：
- `auc_*`（auc_amount/auc_vol/auc_amt_ratio/auc_vol_ratio/auc_pct）：当日 9:25 竞价，
  stk_auction 按日期全量拉取刷新，并重算 shortterm（唯一吃竞价的维度分）
- `model_score`：全量 XGBoost 评分写回（surge 主闸直接读面板）

面板终态 = T-1 特征 + 当日竞价 + 早盘模型分。

## 二、pipeline（一次性进程，cron 09:30）

`plays/limit_up/pipeline.py`（~300 行，非常驻）。流程：

1. 非交易日退出
2. `_refresh_panel_auction`：stk_auction 按日期全量（重试 3 次，持续失败写
   pipeline_crash.log + 飞书通知一次后静默），刷新 auc_* + 重算 shortterm
3. `morning_pass`：全量 `factor_model_score_batch`（grid_v1，64 特征 hit-only），
   model_score 写回面板；主板(00/60)记录合并写 analysis/{date}.json（按 code 去重）
4. 推送：显式 **Top-N（默认3，PUSH_TOP_N）+ ≥55 地板（PUSH_THRESHOLD）**，
   经 pusher.check_and_push → 飞书卡片（每条三行：代码名称星标/涨幅/总分）
5. 异常自通知（_notify_text）+ 非零退出；日志 logs/pipeline.log（RotatingFile）

## 三、surge_scanner（每 60s，daemon + cron 09:30 兜底）

`plays/limit_up/surge_scanner.py`。先筛后拉：

```
扫描池 = 主闸池(面板 model_score ≥ 20) ∪ 排雷池(昨日涨停∪前20日基因 中 <20 的)
       ≈ 950 只（vs 旧版全宇宙 3800，THS 调用 -75%）
行情   = ths_client.get_batch_quotes_fast（24 线程并发，~20s/轮）
窗口   = 5% ≤ pct < 9.8%
路由   = 主闸直通；排雷：首板 量比≥2+窄概念联动≥2+筹码不压顶（3项）
         昨日涨停 量比≥2+筹码不压顶（2项）
写入   = watchdog state(source=surge, 带 dim_scores/daily_basic)
         + analysis/{date}.json + pushed/{date}_surge.json（均按 code 去重原子写）
         + signals/{date}.json（全部路由决策）+ snapshot_log/{date}.parquet（候选快照）
```

关键机制：universe 日缓存但**面板无 model_score 时不写缓存**（防"主闸空"锁死全天）；
_wd_add 写后回读校验防 watchdog 引擎覆盖竞态；pid 守卫防多实例；无数量上限。

## 四、watchdog（cron 09:20 拉起，15:05 自退）

`plays/watchdog/`。60s 轮询，数据走 ws_daemon 共享内存（零 HTTP）：

```
每轮每股：realtime_row(L1 快照 + 日级特征 + 面板维度分)
  → compute_factor_scores（16 实时因子 + 同一 XGBoost 实时打分）
  → check_entry（实时模型分 ≥40 + L1 盘口确认）
  → watching →(信号满 30s 仍触发)→ entered →(check_exit)→ 移除
```

- **surge 票静默**：只发【surge】入场/出场，触发/异常不推；盘后零信号汰换（entered 保留）
- **时间语义真实化**：netflow 采样 ≥60s/点（10 点≈10 分钟窗）、入场确认按信号满 30s、
  时间止损按 entry_at 真实持仓分钟
- **netflow 300s 节流**（jvQuant REST，资金流向是累计值）
- 上限：手动 20 / surge 无上限

## 五、pool_builder（全市场主板）

`data/pool/pool_{date}.json`：主板(00/60) + 非 ST + 上市满 120 天，**无市值带**
（2026-07-25 取消 50-300 亿，1369→3032 只；小票是首板/连板主力）。
用途：surge 名称映射 + ad-hoc 流程。

## 六、评分体系

- **模型分（唯一决策依据）**：grid_v1 XGBoost，64 特征 hit-only
  （win 头 AUC 0.36 反预测已废弃，blend_hit=1.0）
- **五维度分**：降级为 64 特征中的 5 个普通输入（各占 ~1.4% 重要性），
  不再用于选股展示；推送卡片不展示维度分
- **加权总分**：已删除（旧 _weighted_total_score，从未进过训练）

## 数据源约束

| 场景 | 数据源 | 说明 |
|------|--------|------|
| 实时行情（surge/ad-hoc） | ths_client realhead | Cookie 直连，并发 get_batch_quotes_fast |
| 盯盘实时（watchdog） | jvQuant WS | ws_daemon 独占，共享内存快照（≤400 只） |
| 日线/基本面/竞价 | Tushare | 按 trade_date 全量，**禁逐股** |
| 资金流向（watchdog） | jvQuant REST | 300s 节流 |

## 定时任务（hermes cron）

| 任务 | 时间(工作日) | 说明 |
|------|------|------|
| panel-builder-nightly | 00:01 | 全市场面板 |
| concept-refresh | 00:30 | 概念缓存增量 |
| ws-daemon | 08:55 | WS 守护（15:00 自退） |
| watchdog-day | 09:20 | 盯盘引擎（15:05 自退） |
| pipeline-morning | 09:30 | 早盘评分推送（一次性） |
| surge-scanner | 09:30 | surge daemon（pid 守卫防重） |
| review/closing | 18:00 | 复盘 |
| wiki-compile | 20:00 | 归档 + git push |

## 已废弃（2026-07-25 重构删除）

- pipeline 常驻 daemon + 心跳/pidfile（systemd 已 disable）
- 旧扫描栈路径（_run_one_round/stage1_rough/stage2_deep/_raw_score/加权总分）
- 分钟 K 相关（klines 形参、minute_momentum——从未使用）
- T-1 预取缓存、WS 直连残留、时间模拟
- surge≥45 推送分支、每轮/每日限量、SURGE_MAX_WATCH
