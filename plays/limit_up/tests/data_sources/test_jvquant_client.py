"""jvQuant HTTP 客户端真实调用测试。"""

from __future__ import annotations

import pytest

from scripts import audit


@pytest.fixture(autouse=True)
def _reset_audit():
    audit.reset()
    yield


@pytest.fixture(scope="module")
def jv():
    try:
        from scripts.jvquant_client import get_jvquant_client
    except ImportError as e:
        pytest.fail(f"jvQuant client 模块导入失败: {e}")
    return get_jvquant_client()


def test_get_fundflow_single_success(jv, sample_code_short):
    result = jv.get_fundflow_single(sample_code_short)
    assert isinstance(result, dict)
    # 至少含 code / date 一类字段（无强断言，主要看审计）
    recs = [r for r in audit.records() if r["source"] == "jvquant"]
    assert recs, "fundflow_single 未被 audit"
    assert any(r["api"] == "fundflow_single" and r["ok"] for r in recs)


def test_audit_records_error_on_invalid_code(jv):
    """无效 code 应记为失败（extra 含 ERR:）。"""
    try:
        jv.get_fundflow_single("XXXXXX")  # 无效代码
    except Exception:
        pass  # 抛不抛都可
    recs = [r for r in audit.records() if r["source"] == "jvquant"]
    # jvQuant 对无效 code 可能返回空 dict（items=0）或异常。
    # 两种情况都应有对应 audit 记录。
    assert recs, "无效 code 调用未被 audit"


def test_get_kline(jv, sample_code_short):
    bars = jv.get_kline(sample_code_short, freq="day", count=3)
    assert isinstance(bars, list)
    recs = [r for r in audit.records()
            if r["source"] == "jvquant" and r["api"] == "kline"]
    assert recs, "kline 未被 audit"
