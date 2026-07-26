# 候选过滤 — pool_builder 静态口径 + filter 实时规则

## 一、pool_builder.py — 候选池构建（静态过滤）

每日一次，开盘前用 Tushare `daily_basic` 全市场数据 + `stock_basic` 筛选主板候选股。

```bash
python plays/limit_up/pool_builder.py              # 构建当日池
python plays/limit_up/pool_builder.py --date 20260710 --force
```

**输出**：`data/pool/pool_{date}.json`，按流通市值降序，每条含 `{code, name, circ_mv, pe, pb, turnover_rate, volume_ratio}`。

### 过滤规则（2026-07-25 起，全市场主板口径，已取消市值带）

| 规则 | 口径 | 代码出处 |
|------|------|----------|
| 主板 | 代码 `00`/`60` 开头（`MAIN_BOARD_PREFIXES`） | `pool_builder.py` |
| 排除板块 | 创业板/科创板/北交所（`300/301/688/8/4/920/430`，`EXCLUDED_PREFIXES`） | 同上 |
| 非 ST | 名称含 `ST`/`*ST` 剔除 | 同上 |
| 非次新 | 上市满 120 天（`MIN_LISTING_DAYS = 120`） | 同上 |

**市值带已取消**：`MARKET_CAP_MIN/MAX`（50/300 亿）常量仍留在文件中但**不参与过滤**——小票是首板/连板主力，2026-07-25 起与 panel_builder 全市场口径对齐。

### 使用方

- `surge_scanner`：读池取名称；池缺失时 surge 自治调 `ensure_pool()` 补齐（pipeline 改一次性进程后不再建池）。
- `ensure_pool(trade_date, force=False)`：有缓存直接用，无缓存才构建。

## 二、filter.py — 实时过滤

只做**实时可判断**的过滤。静态规则全部在 pool_builder 阶段完成，`filter_candidates()` 是恒等返回的兼容桩。

### 实时规则（`filter_realtime(quote)`）

| 规则 | 条件 |
|------|------|
| 一字板涨停 | `pct_chg >= 9.5%` 且 `price >= limit_up` 且 `turnover < 0.5%` |
| 一字跌停 | `pct_chg <= -9.5%` 且 `price <= limit_down` 且 `turnover < 0.5%` |

返回 `(是否排除, 排除理由)`。

## 三、面板口径（panel_builder，与池对齐但更广）

面板（`wiki/raw/limit-up/panel/{date}.parquet`）覆盖**全市场主板 + 创业/科创非 ST**（过滤 `300/301/688` 之外的口径见 `panel_builder.load_stock_list`：SH/SZ、非 8/4 开头、非 ST/退、上市满 60 天），供模型评分用；打板推送/入盯环节再收窄到主板（`00`/`60`）。
