"""push_feishu 排序与阈值测试（dry-run，不真实发飞书）。"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from plays.limit_up import pipeline as pl


@pytest.fixture(autouse=True)
def _dry_run_feishu(monkeypatch):
    """打断 _get_feishu_token 与 requests.post 避免真实推送。"""
    monkeypatch.setattr(pl, "_get_feishu_token", lambda: "")


def test_push_feishu_returns_false_when_empty():
    assert pl.push_feishu([]) is False


def test_push_feishu_below_threshold_returns_false(monkeypatch):
    from scripts.tu_share import CONFIG
    monkeypatch.setitem(CONFIG, "ULTIMATE_PUSH_THRESHOLD", "50")
    results = [
        {"code": "600001.SH", "name": "低分", "pct_chg": 1.0,
         "total_score": 20, "scores": {"sentiment": 40}},
    ]
    assert pl.push_feishu(results) is False


def test_push_feishu_ordering_by_total_score(monkeypatch, tmp_path):
    """push_list 应按 total_score 降序取满足阈值的前 3 只。"""
    monkeypatch.setattr(pl, "DATA_DIR", tmp_path)
    results = [
        {"code": "600001.SH", "name": "A", "pct_chg": 3.0,
         "total_score": 45, "scores": {"sentiment": 55, "fundflow": 40, "shortterm": 50}},
        {"code": "600002.SH", "name": "B", "pct_chg": 5.0,
         "total_score": 60, "scores": {"sentiment": 65, "fundflow": 60, "shortterm": 55}},
        {"code": "600003.SH", "name": "C", "pct_chg": 1.0,
         "total_score": 30, "scores": {"sentiment": 40, "fundflow": 30, "shortterm": 45}},
    ]
    # 阈值放宽，三只全部通过
    monkeypatch.setitem(__import__("scripts.tu_share", fromlist=["CONFIG"]).CONFIG,
                        "ULTIMATE_PUSH_THRESHOLD", "20")
    pl.push_feishu(results)

    pushed_dir = tmp_path / "pushed"
    files = list(pushed_dir.glob("*.json"))
    assert len(files) == 1, f"应写 1 个 pushed 文件: {files}"
    import json
    written = json.loads(files[0].read_text())
    assert [r["code"] for r in written] == ["600002.SH", "600001.SH", "600003.SH"], \
        f"排序应按 total_score 降序: {written}"


def test_push_feishu_default_threshold_85(monkeypatch, tmp_path):
    """默认阈值 85：只有 total_score >= 85 才会推送。"""
    monkeypatch.setattr(pl, "DATA_DIR", tmp_path)
    monkeypatch.setitem(__import__("scripts.tu_share", fromlist=["CONFIG"]).CONFIG,
                        "ULTIMATE_PUSH_THRESHOLD", "85")
    results = [
        {"code": "600001.SH", "name": "A", "pct_chg": 3.0,
         "total_score": 80, "scores": {"sentiment": 55}},
        {"code": "600002.SH", "name": "B", "pct_chg": 5.0,
         "total_score": 100, "scores": {"sentiment": 65}},
    ]
    pl.push_feishu(results)
    files = list((tmp_path / "pushed").glob("*.json"))
    assert len(files) == 1
    import json
    written = json.loads(files[0].read_text())
    assert [r["code"] for r in written] == ["600002.SH"]
