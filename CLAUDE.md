# 项目约束规则

## 项目顶层结构

```
maneki-agent/
├── plays/                ← 各玩法（垂直隔离，互不依赖）
├── scripts/              ← 共享基础设施（代理、Level2 SDK、Tushare封装等）
├── feishu_bot/           ← 飞书统一入口（路由到各玩法）
├── wiki/                 ← 知识库（跨玩法概念 + 玩法专属数据）
├── skills/               ← 技能（依赖 llm 写作的能力）
├── tests/                ← 顶层单测（跨玩法共享工具/基础设施）
└── .env                  ← 环境变量
```

- 严禁在顶层新增目录（除非确认为跨玩法共享的基础设施）
- `data/` 目录不设顶层软链接，所有数据产出归各玩法自己的 `data/` 目录

## 新增玩法扩展规范

### 1. 目录结构

新建 `plays/新玩法名/`，必须包含：

```
plays/新玩法名/
├── __init__.py
├── docs/              ← 评分维度策略说明集合（必要，与 strategies 一一对应）
│   ├── score.md       ← 总评分规则
│   └── xxx.md         ← 维度策略说明（必要，与 strategies/xxx.py 一一对应）
├── strategies/        ← 维度策略实现集合（必要，至少1个）
│   ├── __init__.py
│   └── xxx.py         ← 维度策略实现（必要，与 docs/xxx.md 一一对应）
├── tests/             ← 本玩法测试（必要）
│   ├── __init__.py
│   └── test_xxx.py    ← 每个策略文件都要有对应单测
├── data/              ← 分析数据目录（必要）
│   └── (analysis/ signals/ pushed/ 等子目录按需创建)
├── score.py           ← 总评分聚合规则（必要，与 docs/score.md 逻辑严格一致）
├── filter.py          ← 候选股过滤规则（必要）
├── pipeline.py        ← 主流程编排，只做流程串联不做具体逻辑（必要）
├── review.py          ← 复盘（可选）
├── health_patrol.py   ← 健康巡检（可选）
└── optimize.py        ← 优化（可选）
```

### 2. 评分策略签名规范

所有评分函数的输入输出必须统一：

```python
def score_xxx(code: str) -> tuple[int | float, str]:
    """返回 (分数, 理由简述)"""
```

- `code`: 带后缀的股票代码，如 `"000001.SZ"` 或 `"600519.SH"`
- 返回 `(score, reason)`，其中 score 为 0-100 数值，reason 为字符串

### 3. 数据源约束

- 实时数据：优先 **同花顺 Cookie 直连**（ths_client.py）
- 非实时数据：优先 **tushare** 获取
- tushare sdk 统一使用 `scripts/tu_share.py`
- 代理IP配置已废弃（全量迁移至同花顺直连）

### 4. Pipeline 主流程规范

pipeline.py 只做流程编排，具体逻辑拆到独立文件：

```python
def main():
    # 1. 扫描获取候选股
    candidates = scan_surge()
    
    # 2. 过滤（filter.py）
    candidates = filter_candidates(candidates)
    
    # 3. 多维度并行评分（strategies/ 下的各维度）
    results = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(score_xxx, code): dim for dim, fn in funcs.items()}
        for future in as_completed(futures):
            results.append(...)
    
    # 4. 评分聚合（score.py）
    # 5. 推送 / 保存
```

### 5. 飞书Bot路由约束

新增玩法需要在 `feishu_bot/handler.py` 中注册路由：

```python
# 在 parse_stock_codes 和 _query_wiki 之间添加路由逻辑
# 例如：检查消息中是否包含新玩法关键词，路由到对应 pipeline
```

## 知识库同步

- 跨玩法通用知识 → `wiki/concepts/`
- 玩法专属数据 → `wiki/plays/新玩法名/entities/`
- wiki compile 脚本需同步更新以支持新玩法

## wiki/raw/ 原始数据归档规范

`wiki/raw/` 存档管线/扫描产生的原始数据，按玩法命名空间分目录，禁止在 `wiki/raw/` 根目录存放文件。

### 目录规范

```
wiki/raw/
└── <play-slug>/              ← 玩法名（连字符形式，如 limit-up）
    ├── analysis/              ← pipeline 每日扫描评分结果（datetime.json）
    ├── pushed/                ← 推送记录（datetime.json）
    ├── signals/               ← 手动/外部信号文件（datetime.json）
    ├── reports/               ← 复盘报告（date.json / date.md）
    ├── weights/               ← 权重优化结果
    ├── panel/                 ← 回测面板数据（parquet）
    └── training/              ← 训练集（CSV）
```

### 关键规则

1. **按玩法分目录** — `wiki/raw/limit-up/xxx`，不允许 `wiki/raw/xxx`
2. **`_relocate_raw_data()` 自动搬迁** — compile.py 在编译完成后将 `plays/<play>/data/` 下的当日文件 mv 到 `wiki/raw/<play-slug>/`
3. **`_gc_stale_raw_data()` 防漏** — compile 启动时自动清理 `plays/<play>/data/` 下非今日残留文件
4. **读取路径** — 回测/查询统一走 `wiki/raw/<play-slug>/`，不走 plays/<play>/data/
5. **禁止写入 `wiki/raw/` 根目录** — 任何代码都不可直接写 `wiki/raw/xxx`，必须写 `wiki/raw/<play-slug>/xxx`
6. **只存原始数据** — `wiki/raw/` 只存管线/扫描产生的 JSON/MD/CSV/parquet，不是文档目录。文档放 `wiki/concepts/` 或 `wiki/plays/`

### 当前玩法

| play | play-slug | raw 路径 |
|------|-----------|----------|
| limit_up | limit-up | `wiki/raw/limit-up/` |

## 代码修改约束

- **scripts/** 是共享基础设施，修改前必须确认不影响所有引用方
- **feishu_bot/handler.py** 是统一入口，修改路由逻辑时确保不破坏现有玩法
- 新增玩法时优先新建 `plays/xxx/` 目录，不修改现有玩法代码
- plays 之间严格垂直隔离，禁止跨玩法 import

## 文档先行原则

- 所有改动先更说明类文件(尤其是玩法策略 docs) → 用户审核 → 再改代码
- 新增玩法必须先写该玩法的设计说明文档
- 策略优化：文档先行 → 策略/因子代码实现 → 单测
