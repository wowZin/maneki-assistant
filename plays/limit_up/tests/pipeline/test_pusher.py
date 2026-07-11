"""pusher.py 单元测试。"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from plays.limit_up import pusher


@patch("plays.limit_up.pusher.is_trading_time")
@patch("plays.limit_up.pipeline_feishu.push_feishu")
def test_check_and_push_filters_by_threshold(mock_push, mock_trading):
    pusher.PUSH_THRESHOLD = 55
    mock_trading.return_value = True
    mock_push.return_value = None

    data_dir = Path("/tmp/test_pusher")
    data_dir.mkdir(parents=True, exist_ok=True)

    results = [
        {"code": "000001.SZ", "name": "A", "total_score": 60},
        {"code": "000002.SZ", "name": "B", "total_score": 50},
        {"code": "000003.SZ", "name": "C", "total_score": 40},
    ]

    pushed = pusher.check_and_push(results, data_dir)
    assert len(pushed) == 1
    assert pushed[0]["code"] == "000001.SZ"
    mock_push.assert_called_once()


@patch("plays.limit_up.pusher.is_trading_time")
@patch("plays.limit_up.pipeline_feishu.push_feishu")
def test_check_and_push_deduplicates(mock_push, mock_trading, tmp_path):
    pusher.PUSH_THRESHOLD = 55
    mock_trading.return_value = True
    mock_push.return_value = None

    pushed_dir = tmp_path / "pushed"
    pushed_dir.mkdir()
    # 已推送 000001
    (pushed_dir / "20260711_1000.json").write_text(
        json.dumps([{"code": "000001.SZ", "total_score": 55}])
    )

    results = [
        {"code": "000001.SZ", "name": "A", "total_score": 60},
        {"code": "000002.SZ", "name": "B", "total_score": 60},
    ]

    pushed = pusher.check_and_push(results, tmp_path)
    assert len(pushed) == 1
    assert pushed[0]["code"] == "000002.SZ"


@patch("plays.limit_up.pusher.is_trading_time")
def test_check_and_push_skips_when_not_trading(mock_trading):
    pusher.PUSH_THRESHOLD = 55
    mock_trading.return_value = False
    results = [{"code": "000001.SZ", "name": "A", "total_score": 60}]
    pushed = pusher.check_and_push(results, Path("/tmp/test_pusher2"))
    assert pushed == []


@patch("plays.limit_up.pusher.is_trading_time")
@patch("plays.limit_up.pipeline_feishu.push_feishu")
def test_check_and_push_empty_results(mock_push, mock_trading):
    pusher.PUSH_THRESHOLD = 55
    mock_trading.return_value = True
    pushed = pusher.check_and_push([], Path("/tmp/test_pusher3"))
    assert pushed == []
    mock_push.assert_not_called()


def test_load_pushed_codes_ignores_invalid_files(tmp_path):
    pushed_dir = tmp_path / "pushed"
    pushed_dir.mkdir()
    (pushed_dir / "20260711_1000.json").write_text("not json")
    (pushed_dir / "20260711_1001.json").write_text(
        json.dumps([{"code": "000001.SZ"}])
    )
    codes = pusher._load_pushed_codes(pushed_dir, "20260711")
    assert codes == {"000001.SZ"}
