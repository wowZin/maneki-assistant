# filter.py — 实时过滤

## 作用

`plays/limit_up/filter.py` 只做**实时可判断**的过滤规则。静态过滤规则（ST/次新/板块/市值）已迁移到 `pool_builder.py`，在候选池构建阶段完成。

## 接口

```python
def filter_realtime(quote: dict[str, Any]) -> tuple[bool, str]:
    """实时过滤。

    Args:
        quote: get_batch_quotes 返回的单只股票行情

    Returns:
        (是否被排除, 排除理由)
    """


def filter_candidates(candidates: list[dict]) -> list[dict]:
    """旧接口兼容桩。静态过滤已迁移到 pool_builder，本函数直接返回原列表。"""
```

## 实时规则

### 一字板涨停

- 涨幅 `pct_chg >= 9.5%`
- 现价 `price >= limit_up_price`
- 换手 `turnover < 0.5%`

### 一字跌停

- 涨幅 `pct_chg <= -9.5%`
- 现价 `price <= limit_down_price`
- 换手 `turnover < 0.5%`

## 为什么静态规则移到 pool Builder

- ST/次新/板块/市值 都是 T-1 已知条件，不需要每分钟重复判断
- 在开盘前构建候选池时一次性过滤，减少盘中计算量和 Tushare 调用

## 测试重点

- 一字板（涨停+低换手）被过滤
- 正常涨停（高换手）不被过滤
- 涨幅不足时不被过滤
- 一字跌停被过滤
