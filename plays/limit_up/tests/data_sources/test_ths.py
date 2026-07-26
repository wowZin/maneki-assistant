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


def test_get_batch_quotes_fast_real_pool():
    """get_batch_quotes_fast 对真实 pool 前50只：成功率≥90%、字段齐、耗时<30s。"""
    import json
    import time
    from pathlib import Path

    play_dir = Path(__file__).resolve().parents[2]  # plays/limit_up
    panel_dir = play_dir.parent.parent / "wiki" / "raw" / "limit-up" / "panel"
    td = max(p.stem for p in panel_dir.glob("*.parquet")
             if p.stem.isdigit() and len(p.stem) == 8)
    pool = json.loads((play_dir / "data" / "pool" / f"pool_{td}.json").read_text())
    codes = [s["code"] for s in pool[:50]]
    assert len(codes) == 50

    ths = get_ths_client()
    t0 = time.time()
    res = ths.get_batch_quotes_fast(codes, workers=16)
    elapsed = time.time() - t0

    assert elapsed < 30, f"批量行情耗时 {elapsed:.1f}s ≥ 30s"
    ok = {c: q for c, q in res.items() if q}
    assert len(ok) / len(codes) >= 0.9, f"成功率 {len(ok)}/{len(codes)} < 90%"
    for c, q in ok.items():
        for f in ("pct_chg", "vol_ratio", "price"):
            assert f in q, f"{c} 行情缺字段 {f}"
