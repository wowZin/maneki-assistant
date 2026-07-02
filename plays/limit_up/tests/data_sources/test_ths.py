"""同花顺 Cookie 直连客户端真实调用测试。"""

from __future__ import annotations

import pytest

from scripts import audit
from scripts.ths_client import get_ths_client


@pytest.fixture(autouse=True)
def _reset_audit():
    audit.reset()
    yield


def test_client_has_cookie():
    ths = get_ths_client()
    assert ths.has_cookie, "THS_COOKIE 未加载"


def test_get_hot_list():
    ths = get_ths_client()
    items = ths.get_hot_list()
    assert items and len(items) > 20, f"同花顺热榜应有约 100 条: 实际 {len(items) if items else 0}"

    recs = [r for r in audit.records() if r["source"] == "ths"]
    assert recs, "同花顺 API 调用未被 audit"


def test_get_quote(sample_code_short):
    ths = get_ths_client()
    q = ths.get_quote(sample_code_short)
    # 非交易时段可能返回 None；仅确认调用不抛异常且被 audit
    recs = [r for r in audit.records() if r["source"] == "ths"]
    assert recs, "get_quote 未被 audit"


def test_audit_summary_by_api_contains_ths():
    ths = get_ths_client()
    ths.get_hot_list()
    s = audit.summary_by_api()
    assert "ths" in s
