"""数据源调用审计 — 每次 API 调用记录成功/失败/条数/耗时

红线：所有数据源客户端必须调用 record() 记录成功与失败路径。
错误 extra 结构化为 ERR:<异常类>|<msg60>|params:<key>。
"""

import json
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

_lock = threading.Lock()
_records: list[dict] = []


def record(source: str, api: str, *, ok: bool = True, items: int = 0,
           latency_ms: float = 0, extra: str = ""):
    """记录一次数据源调用"""
    with _lock:
        _records.append({
            "ts": time.time(),
            "source": source,
            "api": api,
            "ok": ok,
            "items": items,
            "latency_ms": round(latency_ms, 1),
            "extra": extra,
        })


def format_error(exc: BaseException, params: Any = None) -> str:
    """构造结构化错误 extra：ERR:<异常类>|<msg60>|params:<key>"""
    cls = type(exc).__name__
    msg = str(exc).replace("\n", " ")[:60]
    if params is None:
        return f"ERR:{cls}|{msg}"
    if isinstance(params, dict):
        params_str = ",".join(f"{k}={v}" for k, v in list(params.items())[:3])
    else:
        params_str = str(params)[:40]
    return f"ERR:{cls}|{msg}|params:{params_str}"


def call_with_audit(source: str, api: str, fn, *args, items_of=None, extra_of=None, **kwargs):
    """带审计的调用包装器：
    - 记录成功/失败、条数、耗时
    - items_of(result) 返回条数（默认 1）
    - extra_of(result) 返回附加信息（默认 ""）
    异常向外抛出，调用方决定是否降级。
    """
    t0 = time.perf_counter()
    try:
        result = fn(*args, **kwargs)
        latency_ms = (time.perf_counter() - t0) * 1000
        items = items_of(result) if items_of else 1
        extra = extra_of(result) if extra_of else ""
        record(source, api, ok=True, items=items, latency_ms=latency_ms, extra=extra)
        return result
    except Exception as e:
        latency_ms = (time.perf_counter() - t0) * 1000
        params_summary = None
        if args:
            params_summary = args[0] if isinstance(args[0], str) else None
        record(source, api, ok=False, items=0, latency_ms=latency_ms,
               extra=format_error(e, params_summary))
        raise


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


def summary_by_api() -> str:
    """按 (source, api) 拆分的汇总，便于排查慢接口。"""
    with _lock:
        if not _records:
            return "[数据审计-by_api] 无记录"

        buckets: dict[tuple[str, str], dict] = defaultdict(
            lambda: {"total": 0, "ok": 0, "fail": 0, "items": 0, "latency": 0.0, "last_err": ""}
        )
        for r in _records:
            b = buckets[(r["source"], r["api"])]
            b["total"] += 1
            if r["ok"]:
                b["ok"] += 1
                b["items"] += r["items"]
                b["latency"] += r["latency_ms"]
            else:
                b["fail"] += 1
                if r["extra"]:
                    b["last_err"] = r["extra"]

        lines = ["[数据审计-by_api]"]
        for (src, api), b in sorted(buckets.items()):
            avg = (b["latency"] / b["ok"]) if b["ok"] else 0
            icon = "❌" if b["ok"] == 0 else ("⚠️" if b["fail"] else "✅")
            line = f"  {src}.{api} {icon} ok={b['ok']}/{b['total']} items={b['items']} avg={avg:.0f}ms"
            if b["last_err"]:
                line += f" last_err={b['last_err']}"
            lines.append(line)
        return "\n".join(lines)


def dump(path: Path | str) -> Path:
    """把当前 _records 追加到 JSONL 文件，供事后分析。返回目标 Path。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        records_snapshot = list(_records)
    with open(p, "a", encoding="utf-8") as fh:
        for r in records_snapshot:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return p


def records() -> list[dict]:
    """返回当前记录快照（供测试使用）。"""
    with _lock:
        return list(_records)


def reset():
    """清空记录（每次 pipeline 启动时调用）"""
    with _lock:
        _records.clear()
