# 项目约束规则

## 新增玩法扩展规范

### 1. 目录结构

新建 `plays/新玩法名/`，必须包含：

```
plays/新玩法名/
├── __init__.py
├── docs              ← 评分维度策略说明集合（必要，与 strategies 必须一一对应）
│   ├── score.md      ← 评分规则说明（必要，与 strategies/xxx_dim.py 必须一一对应）
│   └── xxx_dim.md    ← 策略说明（必要，与 strategies/xxx_dim.py 必须一一对应）
├── strategies/       ← 评分策略实现集合（必要，至少1个，需要与 docs 保持一一对应）
│   ├── __init__.py
│   └── xxx_dim.py    ← 维度策略实现（必要，与 docs/xxx_dim.md 必须一一对应）
├── datasources/      ← 数据源目录（必要）
│   ├── __init__.py
│   └── xxx.py        ← 数据源，一个数据一个单独文件（必须有单测一一对应）
│   └── tests         ← 数据源单测（与数据源一一对应）
├── output/              ← 分析数据目录（必要）
│   └── (analysis/ signals/ pushed/ 等子目录按需创建)
├── score.py          ← 评分规则（必要，与 docs/score.md 必须逻辑严格一致）
├── pipeline.py       ← 主流程（必要）
├── review.py         ← 复盘（可选）
├── health_patrol.py  ← 健康巡检（可选）
└── optimize.py       ← 优化（可选）
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

- 实时数据：优先 **requests+代理 东方财富API**（push2.eastmoney.com）
- Level2 数据：l2api (三方 level2 数据源)
- 非实时数据：优先 **tushare** 获取
- 代理模块统一使用 `scripts/proxy_utils.py`
- tushare sdk 统一使用 `scripts/tu_share.py`
- level2 sdk 统一使用 `scripts/l2_client.py`
- 代理IP配置：`.env` 中 `PROXY_ENABLED=true`

### 4. Pipeline 主流程规范

```python
def main():
    # 1. 扫描获取候选股
    candidates = scan_surge()
    
    # 2. 多维度并行评分
    results = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(score_xxx, code): dim for dim, fn in funcs.items()}
        for future in as_completed(futures):
            results.append(...)
    
    # 3. 聚合排序
    # 4. 推送 / 保存
```

### 5. 飞书Bot路由约束

新增玩法需要在 `feishu_bot/handler.py` 中注册路由：

```python
# 在 parse_stock_codes 和 _query_wiki 之间添加路由逻辑
# 例如：检查消息中是否包含新玩法关键词，路由到对应 pipeline
```

### 6. 知识库同步

- 跨玩法通用知识 → `wiki/concepts/`
- 玩法专属数据 → `wiki/plays/新玩法名/entities/`
- wiki compile 脚本需同步更新以支持新玩法

## 代码修改约束

- **scripts/** 是共享基础设施，修改前必须确认不影响其他玩法
- **feishu_bot/handler.py** 是统一入口，修改路由逻辑时确保不破坏现有玩法
- 新增玩法时优先新建 `plays/xxx/` 目录，不修改现有玩法代码

## 文档先行原则

- 所有改动先更新 `docs/` 或本文件 → 用户审核 → 再改代码
- 新增玩法必须先写该玩法的设计说明文档
- 策略优化：文档先行 → 策略/因子代码实现 → 单测
