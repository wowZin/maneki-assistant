# pipeline.py — 打板早盘评分（一次性进程）

> 2026-07-26 重构后：`plays/limit_up/pipeline.py` 是**一次性进程**，由 hermes cron 每日 09:30 触发，跑完即退出。不再常驻、不再盘中循环、无心跳文件。

## 职责

1. 交易日判断（非交易日直接退出）
2. `stk_auction` 按日期全量拉当日集合竞价（9:25 快照），刷新面板 `auc_*` 列并重算 `shortterm` 维度分
3. 对面板全量股票做 XGBoost 模型评分（`factor_model_score_batch`）
4. `model_score` 全量写回面板（surge_scanner 的主闸直接读面板）
5. 主板记录合并写 `analysis`，Top-N + ≥55 地板触发飞书推送
6. 任何未捕获异常 → crash 日志 + 飞书通知 + 非零退出

## 启动方式

```bash
python3 plays/limit_up/pipeline.py                     # 跑一次（cron 调用）
python3 plays/limit_up/pipeline.py --date 20260724     # 指定日期（测试用）
```

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `ULTIMATE_PUSH_THRESHOLD` | 55 | 推送地板分（绝对阈值） |
| `PUSH_TOP_N` | 3 | 每次推送的相对 Top-N |
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | — | 失败告警通知凭证 |
| `FEISHU_CHAT_ID_SIGNAL`（回退 `FEISHU_BOT_CHAT_ID`） | — | 告警接收群 |

## 流程细节

### ① 竞价刷新面板（`_refresh_panel_auction`）

- 数据源：Tushare `stk_auction`，按 `trade_date` **全市场一次调用**（禁逐股），重试 3 次（间隔 2s/4s/6s）。
- 刷新列（面板 `wiki/raw/limit-up/panel/{date}.parquet`）：
  - `auc_amount` / `auc_vol` / `auc_pct`（竞价额/量/涨幅，`auc_pct = price/pre_close - 1`）
  - `auc_amt_ratio` = `auc_amount ÷ avg_amount_5d`（面板列）
  - `auc_vol_ratio` = `auc_vol ÷ T-1 日 vol`（T-1 vol 读 `wiki/raw/limit-up/panel/daily/{prev_date}.parquet`）
  - `shortterm` 维度分重算（五维度中唯一吃竞价的分）：`base 10 + 量比分档 5/10/20`，公式与 `panel_builder._add_strategy_scores` 一致：
    - `auc_amt_ratio > 1` → +20；`> 0.5` → +10；`> 0.1` → +5；否则 +0（上限 100）
- 持续失败：写 `plays/limit_up/data/health/pipeline_crash.log` + 飞书通知一次，**不阻断**（面板保留 T-1 夜间竞价值兜底）。
- 面板文件不存在则直接报错返回 False。

### ② 全量静态模型评分（`morning_pass`）

- 输入：当日面板全量（panel_builder 00:01 构建 + ① 竞价刷新）。
- 竞价涨幅 `auc_pct` 覆盖 `pct_chg_score_day` 特征（09:30 连续竞价尚未开始，用竞价涨幅当当日涨幅）。
- 评分：`plays.limit_up.factors.optimized.model_score.factor_model_score_batch`（XGBoost grid_v1，64 特征 hit-only，含追高护栏，详见 [score.md](./score.md)）。
- `model_score` 全量写回面板 parquet（面板 = T-1 特征 + 当日竞价 + 早盘模型分，终态）。

### ③ analysis 合并

- 只组装主板（代码 `00`/`60` 开头，打板不看 20cm）记录：
  `{code, name, model_score, total_score, score_mode="model_score", pct_chg, scores{technical,fundflow,sentiment,shortterm}, fundamental}`
  （维度分直接取面板列；名称来自 `data/pool/pool_{date}.json` + analysis 旧记录）
- 与已有 `plays/limit_up/data/analysis/{date}.json` **按 code 去重合并**，原子写入（tmp + rename）。

### ④ 推送

- 候选：`model_score >= ULTIMATE_PUSH_THRESHOLD`（默认 55）且竞价涨幅 < 9.8%（已封板不推）。
- 按 `model_score` 降序取 Top-N（`PUSH_TOP_N`，默认 3），调 `pusher.check_and_push` → `pipeline_feishu.push_feishu`（卡片格式见 [pusher.md](./pusher.md)）。
- ≥55 的全量候选带留在 analysis 供回测；`data/pushed/` 存档 = 真实推送的 Top-N。

### ⑤ 异常处理

- 任何未捕获异常：写 `data/health/pipeline_crash.log` + 飞书文本通知（绕过 pusher 的交易时间闸）+ `sys.exit(1)`。

## 日志

- `logs/pipeline.log`（RotatingFileHandler，5MB × 3 备份）+ stdout（cron 输出）。

## 与其他进程的关系

| 进程 | 关系 |
|------|------|
| `panel_builder.py`（00:01 cron） | 产出当日面板；pipeline 在其上做竞价刷新和评分写回 |
| `surge_scanner.py`（09:35 起 daemon） | 主闸直接读面板 `model_score >= 20`；pipeline 不写回时 surge 全走排雷 |
| `watchdog`（09:20 cron） | 不直接交互；watchdog 从 analysis 读维度分 |
