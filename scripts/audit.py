"""数据源调用审计 — 每次 API 调用记录成功/失败/条数/耗时"""

import time
import threading
from collections import defaultdict

_lock = threading.Lock()
_records: list[dict] = []


def record(source: str, api: str, *, ok: bool = True, items: int = 0,
           latency_ms: float = 0, extra: str = ""):
    """记录一次数据源调用"""
    with _lock:
        _records.append({
            "source": source,
            "api": api,
            "ok": ok,
            "items": items,
            "latency_ms": round(latency_ms, 1),
            "extra": extra,
        })


def summary() -> str:
    """生成一行汇总字符串"""
    with _lock:
        if not _records:
            return "[数据审计] 无记录"

        by_source = defaultdict(lambda: {"total": 0, "ok": 0, "fail": 0,
                                          "items": 0, "latency": 0, "extras": []})
        for r in _records:
            s = by_source[r["source"]]
            s["total"] += 1
            if r["ok"]:
                s["ok"] += 1
                s["items"] += r["items"]
                s["latency"] += r["latency_ms"]
            else:
                s["fail"] += 1
            if r["extra"]:
                s["extras"].append(r["extra"])

        parts = ["[数据审计]"]
        for src, st in sorted(by_source.items()):
            if st["fail"] > 0:
                icon = "❌" if st["ok"] == 0 else "⚠️"
            else:
                icon = "✅" if st["items"] > 0 else "⚪"

            detail_parts = [f"{st['ok']}/{st['total']}次"]
            if st["items"]:
                detail_parts.append(f"{st['items']}条")
            if st["ok"] > 0:
                avg_ms = st["latency"] / st["ok"]
                detail_parts.append(f"avg{avg_ms:.0f}ms")
            if st["extras"]:
                for e in st["extras"][:2]:
                    detail_parts.append(e)

            parts.append(f"{src}{icon}({', '.join(detail_parts)})")

        return " ".join(parts)


def reset():
    """清空记录（每次 pipeline 启动时调用）"""
    with _lock:
        _records.clear()
