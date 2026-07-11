# pusher.py — 推送判断与去重

## 作用

`plays/limit_up/pusher.py` 负责判断评分结果是否满足推送条件，并做去重，最终调用 `pipeline_feishu.push_feishu()` 发送飞书卡片。

## 接口

```python
def check_and_push(results: list[dict], data_dir: Path) -> list[dict]:
    """检查并推送满足条件的股票。

    Args:
        results: 评分结果列表，每个结果必须含 code/name/total_score。
        data_dir: plays/limit_up/data 目录路径。

    Returns:
        实际推送的股票列表（可能为空）。
    """
```

## 推送规则

1. 只在交易时段内推送（`is_trading_time()`）
2. 过滤 `total_score >= ULTIMATE_PUSH_THRESHOLD`
3. 从 `data/pushed/{today}*.json` 读取历史已推代码去重
4. 调用 `pipeline_feishu.push_feishu(new)` 真正发送
5. **不再重复写 pushed 文件**，推送记录由 `pipeline_feishu.push_feishu()` 统一写入

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `ULTIMATE_PUSH_THRESHOLD` | 55 | 推送阈值 |

## 与 pipeline_feishu.push_feishu 的关系

- `pusher.check_and_push()` 做"哪些票值得推"的判断
- `pipeline_feishu.push_feishu()` 做"怎么推"的执行（排序、降噪、卡片渲染、HTTP 发送）

两者数据契约：`results` 中必须有 `total_score` 字段。

## 降噪说明

`pipeline_feishu.push_feishu()` 内部会再次降噪：同一只股票只有本次 `total_score` 高于上次推送时才再次推送，避免重复骚扰。
