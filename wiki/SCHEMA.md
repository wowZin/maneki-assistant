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
└── raw/                   # 原始数据（不可变）
    ├── articles/
    ├── signals/
    ├── analysis/
    ├── reports/
    └── weights/
```

## Conventions

- File names: lowercase-hyphens, no spaces
- Every wiki page starts with YAML frontmatter
- Use `[[plays/xxx/path]]` for cross-play links
- New pages must be added to `index.md`
- Every action appended to `log.md`

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
- **data-source**: eastmoney, tushare, proxy
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
