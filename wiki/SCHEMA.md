# Wiki Schema — Maneki 知识库

## Domain

A股量化策略。按玩法分目录管理，每个玩法独立的数据和知识。

## Directory Structure

```
wiki/
├── SCHEMA.md              # 本文件
├── index.md               # 内容索引
├── log.md                 # 操作日志
├── concepts/              # 跨玩法通用知识
│   ├── 五维度评分体系.md
│   ├── 评估指标说明.md
│   └── ...
├── plays/                 # 玩法专属数据
│   ├── limit-up/
│   │   └── entities/      # 每日编译汇总
│   ├── watchdog/
│   │   └── entities/      # 每日盯盘状态编译
│   └── xxx/               # 其他玩法
├── queries/               # FAQ
└── raw/                   # 原始数据（不可变），按玩法分层
    ├── articles/          # 跨玩法通用
    ├── concepts/          # 跨玩法通用（预留）
    ├── history/           # 跨玩法通用
    ├── limit-up/          # 打板玩法
    │   ├── signals/
    │   ├── analysis/
    │   ├── pushed/
    │   ├── reports/
    │   └── weights/
    └── watchdog/          # 盯盘（未来）
        └── state/
```

## Conventions

- File names: lowercase-hyphens, no spaces
- Every wiki page starts with YAML frontmatter
- Use `[[plays/xxx/path]]` for cross-play links
- New pages must be added to `index.md`
- Every action appended to `log.md`

## raw/ 语义

- **不可变归档**：一旦落入 `raw/<play>/<kind>/`，视为 immutable 只读，禁止手工编辑。
- **按玩法分层**：`raw/<play>/{signals,analysis,pushed,reports,weights,...}/{trade_date}*` 与 `plays/<play>/data/` 对称，扩展新玩法只需新建顶层子目录。
- **每日搬迁**：`wiki/compile.py` 在收盘 compile 时把当日 `plays/<play>/data/<kind>/{trade_date}*` **move**（copy + delete original）到 `raw/<play>/<kind>/`，保证玩法运行时目录不堆积历史。
- **顶层跨玩法内容**：`articles/`（文章知识）、`concepts/`（跨玩法概念素材，预留）、`history/`（历史比对基线）留在 `raw/` 顶层。

## Frontmatter

```yaml
---
title: Page Title
created: YYYY-MM-DD
updated: YYYY-MM-DD
type: entity | concept | comparison | query | summary | watchdog
tags: [dimension, methodology, data-source, weight, scan, review]
sources: [raw/articles/source.md]
---
```

## Tag Taxonomy

- **dimension**: fundamental, technical, fundflow, sentiment, shortterm
- **methodology**: scoring, ranking, weighting, threshold
- **data-source**: ths, tushare
- **weight**: optimization, ab-comparison
- **scan**: intraday, closing-review
- **pipeline**: push, feishu, review
- **watchdog**: daily, monitoring, signal
- **metric**: auc, hit-rate, coverage, rank

## Page Thresholds

- **Create a page** when a concept appears in 2+ sources or is central to one
- **Don't create** for passing mentions
- **Split** pages over 200 lines

## Entity Pages

One page per notable entity. Include: overview, key facts, relationships.

## Concept Pages

One per concept. Include: definition, methodology, related concepts.
