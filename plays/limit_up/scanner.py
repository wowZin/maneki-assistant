#!/usr/bin/env python3
"""候选池批量扫描 — 带 RPS 限流的同花顺行情拉取。

将全市场候选池分片调用 THS get_batch_quotes，避免逐只请求过快触发反爬。
同时负责把完整 quote 字段注入 realtime_ctx，供策略层读取。
"""

from __future__ import annotations

import os
import time
from typing import Any

from scripts.ths_client import get_ths_client

DEFAULT_MAX_RPS = float(os.environ.get("LIMIT_UP_SCAN_RPS", "30"))
MIN_CHUNK_SIZE = 10


def _short_code(code: str) -> str:
    """统一转换为 6 位短代码。"""
    return code.split(".")[0]


def scan_batch(
    pool_codes: list[str],
    max_rps: float = DEFAULT_MAX_RPS,
    inject_realtime: bool = True,
) -> dict[str, dict]:
    """批量扫描候选池行情。

    Args:
        pool_codes: 候选股代码列表，支持短代码或带后缀代码。
        max_rps: 每秒最大请求数（实际为 chunk 间间隔控制）。
        inject_realtime: 是否将结果注入全局 realtime_ctx 缓存。
            注意：默认 True，调用方若不需要副作用可显式传 False。

    Returns:
        {short_code: quote_dict}
    """
    from plays.limit_up.strategies.realtime_ctx import set_realtime_quotes

    ths = get_ths_client()
    chunk_size = max(MIN_CHUNK_SIZE, int(max_rps))
    results: dict[str, dict] = {}

    for i in range(0, len(pool_codes), chunk_size):
        chunk = pool_codes[i : i + chunk_size]
        batch = ths.get_batch_quotes(chunk)
        for code, quote in batch.items():
            if quote is None:
                continue
            short = _short_code(code)
            results[short] = quote

        # 限流：chunk 之间 sleep，最后一批不需要
        if i + chunk_size < len(pool_codes):
            sleep_seconds = chunk_size / max_rps
            time.sleep(sleep_seconds)

    if inject_realtime:
        set_realtime_quotes(results)

    return results


def build_name_map(pool: list[dict]) -> dict[str, str]:
    """从候选池构建 {short_code: name} 映射。"""
    name_map: dict[str, str] = {}
    for item in pool:
        code = item.get("code", "")
        name = item.get("name", "")
        if code:
            name_map[_short_code(code)] = name
    return name_map
