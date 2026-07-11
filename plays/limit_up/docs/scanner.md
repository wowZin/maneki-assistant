# scanner.py — 候选池批量扫描

## 作用

`plays/limit_up/scanner.py` 封装对同花顺 `THSClient.get_batch_quotes()` 的批量调用，并做 caller 侧 RPS 限流，降低触发反爬或 Cookie 失效的风险。

## 接口

```python
def scan_batch(
    pool_codes: list[str],
    max_rps: float = DEFAULT_MAX_RPS,
    inject_realtime: bool = True,
) -> dict[str, dict]:
    """批量扫描候选池行情。

    Args:
        pool_codes: 候选股代码列表，支持短代码或带后缀代码。
        max_rps: 每秒最大请求数（实际为 chunk 间间隔控制）。
        inject_realtime: 是否将结果注入 realtime_ctx。

    Returns:
        {short_code: quote_dict}
    """


def build_name_map(pool: list[dict]) -> dict[str, str]:
    """从候选池构建 {short_code: name} 映射。"""
```

## 限流策略

- chunk_size = max(10, int(max_rps))
- 对每个 chunk 调用 `ths.get_batch_quotes(chunk)`
- chunk 之间 sleep: `chunk_size / max_rps` 秒
- 默认 `max_rps=30`，1195 只约 40 秒完成

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `LIMIT_UP_SCAN_RPS` | 30 | 默认每秒请求数 |

## 返回字段

返回 `{short_code: quote}`，quote 来自 `THSClient.get_batch_quotes()`，包含：

- `pct_chg`: 涨跌幅
- `price`: 现价
- `open` / `high` / `low` / `pre_close`
- `vol_ratio`: 量比
- `turnover`: 换手率
- `inner_vol` / `outer_vol`: 内盘/外盘
- `bid1` / `ask1`: 买卖一价
- `bid1_vol` / `ask1_vol`: 买卖一量
- `amount`: 成交额

当 `inject_realtime=True` 时，完整 quote dict 被注入 `realtime_ctx`，供 `technical.py` / `shortterm.py` 读取实时字段。

## 注意事项

- `scripts/ths_client.py` 内部仍是逐只请求，因此 caller 侧限流是必要的。
- 若后续同花顺接口支持真批量，可优化 `ths_client.py` 内部实现，本模块接口不变。
