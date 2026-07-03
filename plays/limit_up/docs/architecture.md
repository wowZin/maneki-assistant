# 打板玩法 · 架构说明

> 最新架构，已废弃所有 v1/v2/v3 版本概念。此文档为唯一事实。

## 一、分层与模块契约

```
scanner → filter → cache_layer → pre_rank → quote_cache
       → scoring (五维度并行) → total (唯一总分) → push
```

| 模块 | 文件 | 职责 | 输入 | 输出 | 副作用 |
|------|------|------|------|------|--------|
| scanner | `scanner.py` | 候选股扫描 | THS 热门榜 / from-file | `list[{code,name,pct_chg}]` | 无 |
| filter | `filter.py` | 7 规则硬性过滤 | 候选列表 | 过滤后列表 | 无 |
| cache_layer | `cache_layer.py` | 同日评分缓存 | trade_date | `dict[code → scored_record]` | 只读 wiki/raw + data |
| pre_rank | `pre_rank.py` | 预排序 (涨速+涨幅+人气) | 候选列表 + THS 热榜 | Top-N | 无 |
| quote_cache | `quote_cache.py` | 数据预取 | 候选列表 | 内部缓存 | THS/Tushare/jvquant API 调用 |
| scoring | `scoring.py` | 五维度并行评分 | 候选列表 + quote_cache | 每只 `{scores, factors}` | ThreadPoolExecutor |
| total | `total.py` | 唯一总分聚合 | 单只记录 | `float total_score` | 无 |
| push | `push.py` | 排序 + 飞书推送 + 落盘 | 全量评分记录 | Top-3 | 飞书 API + `data/analysis` `data/pushed` 写入 |
| pipeline | `pipeline.py` | 编排 | argv | 退出码 | 全流程副作用 |

**pipeline.py 目标 < 300 行**，只做流程串联与日志打印，不做具体计算。

## 二、维度架构

五个维度 + 一个辅助判据：

| 维度 | 评分模块 | 说明 |
|------|----------|------|
| fundamental | `strategies/fundamental.py` | 小市值 / 业绩突变 / 筹码集中 / 题材广度 |
| technical | `strategies/technical.py` | 量能质量 / 趋势位置 / 筹码市值 / 形态确认 |
| fundflow | `strategies/fundflow.py` | 中单+主力+龙虎榜+融资资金 |
| sentiment | `strategies/sentiment.py` | 市场状态 / 题材 / 分层 / 人气 / 竞价 |
| shortterm | `strategies/shortterm.py` | 涨停基因 / 开盘博弈 / 位置波动 / 连板溢价 |
| first_board | `strategies/first_board.py` | **辅助**，首板专属判据，不入 total_score |

每个维度输出 `(score: float 0-100, reason: str)`。因子下沉到 `factors/<dim>/`，被维度评分模块与 total_score 复用。

## 三、唯一总分（去版本化）

`plays/limit_up/total.py::total_score(row)` 是全系统唯一的总分聚合入口。公式与权重来源见 [`score.md`](./score.md)。

**已废弃字段（不再计算、不再输出）**：

- `new_total_v2`, `balanced_total`, `balanced_total_v2`
- `sentiment_adaptive_total`, `sentiment_adaptive_total_pit`, `sentiment_conditional_pit`
- `ultimate_total_v1`, `v2`, `v3`, `v4`, `v5`
- `cpt_amount_percentile`, `cpt_amount_combo`, `balanced_ensemble`
- `deep_total_v5`, `sentiment_position_combo_pit` 等 combo 字段（作为总分字段被移除；作为子因子仍保留在 `factors/sentiment/`）

## 四、数据流

```
09:30-15:00 (盘中):
  pipeline.main
    → 写 data/analysis/{HHMM}.json
    → 写 data/pushed/{HHMM}.json
    → 写 data/logs/audit_{trade_date}.log

18:00 (收盘后):
  wiki/compile.py
    → 读 data/analysis + data/pushed + data/reports + data/signals + data/weights
    → 生成 wiki/plays/limit-up/entities/{trade_date}-扫描汇总.md
    → 搬迁 (move): data/{kind}/{trade_date}* → wiki/raw/limit-up/{kind}/
    → 玩法 data/ 下该日文件消失

后续读取:
  review.py / health_patrol.py / backtest/data.py
    → 当日: 优先 data/，回落 wiki/raw/limit-up/
    → 历史: 直接 wiki/raw/limit-up/
```

`wiki/raw/<play>/` 是 immutable 只读归档（按玩法分层，方便未来扩展）；`plays/<play>/data/` 是运行时可写工作区，当日之后被清空。

**wiki/raw 目录布局**：

```
wiki/raw/
├── articles/           # 跨玩法通用知识
├── concepts/           # 跨玩法通用（预留）
├── history/            # 跨玩法通用历史
├── limit-up/           # 本玩法归档
│   ├── signals/
│   ├── analysis/
│   ├── pushed/
│   ├── reports/
│   └── weights/
└── watchdog/           # 其他玩法（未来）
    └── state/
```

## 五、数据源与审计

| 客户端 | 主要用途 | 审计要求 |
|--------|----------|----------|
| `scripts/tu_share.py` | Tushare 全接口 | 每次调用 record；错误 `extra` 含结构化上下文 |
| `scripts/ths_client.py` | 同花顺 Cookie 直连（行情/热榜/概念） | 同上；首触发的 fetch 也需 record |
| `scripts/jvquant_client.py` | jvQuant 历史资金流/分钟/K线/L2 | 全 public 方法用 `_call_with_audit` 装饰 |
| `scripts/jvquant_ws_client.py` | jvQuant WebSocket 实时深度 | connect/subscribe/unsubscribe/reconnect 事件级别 record |

`scripts/audit.py` 在 `pipeline.main()` 结束时 `dump()` 到 `data/logs/audit_{trade_date}.log`，`summary()` 按 api 拆分。

## 六、测试红线

`plays/limit_up/tests/conftest.py` 在 `pytest_configure` 检查 `TUSHARE_TOKEN / THS_COOKIE / JVQUANT_TOKEN`，任一缺失则抛 `RuntimeError`，测试立即 fail。所有单测**真实调用**，禁止 mock 数据。

覆盖范围：

- 每个 `factors/<dim>/*.py` 的每个因子函数
- 每个 `strategies/<dim>.py` 的 `score_<dim>` 函数
- 每个 pipeline 模块（scanner / filter / pre_rank / scoring / push / cache_layer）
- 每个数据源客户端的成功与失败路径
- wiki `_relocate_raw_data` 的搬迁与下游 fallback

## 七、目录结构

见 `docs/factors.md` 与项目根 `CLAUDE.md`。
