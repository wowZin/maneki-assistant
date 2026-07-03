"""audit.py 自身的单测（无网络依赖）。"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from scripts import audit


def setup_function(_fn):
    audit.reset()


def test_record_and_summary():
    audit.record("test_src", "test_api", ok=True, items=5, latency_ms=100)
    s = audit.summary()
    assert "test_src" in s
    assert "5" in s


def test_summary_by_api():
    audit.record("tushare", "daily", ok=True, items=3, latency_ms=100)
    audit.record("tushare", "daily_basic", ok=True, items=2, latency_ms=200)
    audit.record("tushare", "daily", ok=False, latency_ms=0,
                 extra="ERR:Timeout|read timeout")
    s = audit.summary_by_api()
    assert "tushare.daily" in s
    assert "tushare.daily_basic" in s
    assert "ERR:" in s


def test_dump_and_read_back():
    audit.record("src1", "api1", ok=True, items=1)
    audit.record("src1", "api1", ok=False, extra="ERR:X|y")
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "audit.log"
        audit.dump(p)
        lines = p.read_text().strip().split("\n")
        assert len(lines) == 2
        first = json.loads(lines[0])
        assert first["source"] == "src1"
        assert first["ok"] is True


def test_dump_appends():
    """dump 是追加语义，多次调用不覆盖已有内容。"""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "audit.log"
        audit.record("s", "a", ok=True, items=1)
        audit.dump(p)
        audit.reset()
        audit.record("s", "a", ok=True, items=2)
        audit.dump(p)
        lines = p.read_text().strip().split("\n")
        assert len(lines) == 2  # 追加


def test_format_error():
    try:
        raise ValueError("something bad")
    except ValueError as e:
        s = audit.format_error(e, {"code": "600176", "date": "20260701"})
    assert s.startswith("ERR:ValueError|")
    assert "something bad" in s
    assert "code=600176" in s


def test_call_with_audit_success():
    result = audit.call_with_audit("src", "api", lambda x: x * 2, 5)
    assert result == 10
    recs = audit.records()
    assert recs[0]["ok"] is True


def test_call_with_audit_failure():
    try:
        audit.call_with_audit("src", "api", lambda: 1 / 0)
    except ZeroDivisionError:
        pass
    recs = audit.records()
    assert recs[0]["ok"] is False
    assert "ERR:ZeroDivisionError" in recs[0]["extra"]


def test_reset_clears_records():
    audit.record("s", "a", ok=True)
    audit.reset()
    assert audit.records() == []
