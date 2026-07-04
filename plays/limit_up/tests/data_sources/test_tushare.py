"""Tushare 客户端真实调用测试。"""

from __future__ import annotations

import pytest

from scripts import audit
from scripts.tu_share import call_tushare, clear_tushare_cache


@pytest.fixture(autouse=True)
def _reset_audit():
    audit.reset()
    yield


def test_stock_basic_returns_data():
    resp = call_tushare("stock_basic", {"list_status": "L"}, "ts_code,name,industry")
    items = resp.get("data", {}).get("items", [])
    assert len(items) > 100, f"stock_basic 至少应有几百条: 实际 {len(items)}"


def test_daily_returns_recent_bar(sample_code):
    resp = call_tushare("daily", {"ts_code": sample_code}, "trade_date,close,pct_chg")
    items = resp.get("data", {}).get("items", [])
    assert items, f"{sample_code} daily 无数据"


def test_audit_records_success():
    call_tushare("stock_basic", {"ts_code": "600176.SH"}, "ts_code,name")
    recs = audit.records()
    assert any(r["source"] == "tushare" and r["ok"] for r in recs), \
        f"tushare 成功调用未被 audit: {recs}"


def test_audit_records_failure():
    clear_tushare_cache()
    # 传入不存在的 api → 应记 ok=False 而非静默
    resp = call_tushare("this_api_does_not_exist", {}, "x")
    # tushare wrapper 可能返回 {} 或抛错，两种都可接受
    recs = audit.records()
    tushare_records = [r for r in recs if r["source"] == "tushare"]
    assert tushare_records, "未记录任何 tushare 调用"
    # 至少要有一条 fail 或者 items=0
    assert any((not r["ok"]) or r["items"] == 0 for r in tushare_records), \
        f"错误 API 应被记录为失败或零条: {tushare_records}"


def test_audit_summary_reports_tushare_calls():
    call_tushare("stock_basic", {"ts_code": "600176.SH"}, "ts_code,name")
    s = audit.summary()
    assert "tushare" in s


def test_ths_daily_returns_concept_data():
    resp = call_tushare("ths_daily", {"trade_date": "20260701"}, "")
    items = resp.get("data", {}).get("items", [])
    assert len(items) > 100, f"ths_daily 应返回大量概念: 实际 {len(items)}"


def test_ths_member_returns_members():
    resp = call_tushare("ths_member", {"ts_code": "882001.TI"}, "")
    items = resp.get("data", {}).get("items", [])
    assert items, "ths_member 对 882001.TI 应返回成分股"


def test_top_list_returns_data():
    resp = call_tushare("top_list", {"trade_date": "20260701"}, "")
    items = resp.get("data", {}).get("items", [])
    assert len(items) > 10, f"top_list 应返回上榜数据: 实际 {len(items)}"


def test_top_inst_returns_data():
    resp = call_tushare("top_inst", {"trade_date": "20260701"}, "")
    items = resp.get("data", {}).get("items", [])
    assert len(items) > 10, f"top_inst 应返回席位数据: 实际 {len(items)}"
