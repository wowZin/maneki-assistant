"""wiki/compile.py::_relocate_raw_data 冒烟测试。

不需要网络或 token，只测搬迁逻辑本身。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from wiki.compile import _relocate_raw_data  # noqa: E402


# 使用远期日期避免与真实数据冲突
TEST_DATE = "20260710"


@pytest.fixture
def temp_files():
    """造 test 文件，测试完清理。"""
    play_data = PROJECT_DIR / "plays" / "limit_up" / "data"
    raw = PROJECT_DIR / "wiki" / "raw" / "limit-up"

    files = {
        play_data / "analysis" / f"{TEST_DATE}_9999.json":
            json.dumps([{"code": "600001.SH", "total_score": 45}]),
        play_data / "pushed" / f"{TEST_DATE}_9999.json":
            json.dumps([{"code": "600001.SH"}]),
        play_data / "reports" / f"{TEST_DATE}.md": "# smoke report",
    }
    for f, content in files.items():
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content)

    yield files, raw

    # 清理：源 + 目标
    for f in files:
        f.unlink(missing_ok=True)
        dst = raw / f.parent.name / f.name
        dst.unlink(missing_ok=True)


def test_relocate_moves_files(temp_files):
    """搬迁后源消失，目标出现。"""
    files, raw = temp_files
    _relocate_raw_data(TEST_DATE)
    for f in files:
        assert not f.exists(), f"源应消失: {f}"
        dst = raw / f.parent.name / f.name
        assert dst.exists(), f"目标应存在: {dst}"


def test_relocate_is_idempotent(temp_files):
    """第二次调用是 no-op，目标仍存在，源仍不存在。"""
    files, raw = temp_files
    _relocate_raw_data(TEST_DATE)
    _relocate_raw_data(TEST_DATE)  # 幂等
    for f in files:
        dst = raw / f.parent.name / f.name
        assert dst.exists(), f"第二次后目标应仍存在: {dst}"
        assert not f.exists(), f"源应仍不存在: {f}"


def test_relocate_respects_pipeline_lock(temp_files):
    """pipeline.lock 存在时应跳过搬迁。"""
    files, raw = temp_files
    play_data = PROJECT_DIR / "plays" / "limit_up" / "data"
    lock = play_data / "pipeline.lock"
    lock.write_text("99999")
    try:
        _relocate_raw_data(TEST_DATE)
        # 有 lock 时，源文件应仍存在
        for f in files:
            assert f.exists(), f"有 lock 时源不应被搬走: {f}"
    finally:
        lock.unlink(missing_ok=True)


def test_relocate_preserves_content(temp_files):
    """搬迁后文件内容不变。"""
    files, raw = temp_files
    original = {f: f.read_text() for f in files}
    _relocate_raw_data(TEST_DATE)
    for f, content in original.items():
        dst = raw / f.parent.name / f.name
        assert dst.read_text() == content, f"内容改变: {dst}"


def test_relocate_overwrites_existing(temp_files):
    """目标已存在时应先 unlink 再 move（幂等语义）。"""
    files, raw = temp_files
    # 先造一个"旧版"目标文件
    first_key = next(iter(files))
    dst = raw / first_key.parent.name / first_key.name
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text("OLD CONTENT")
    # 搬迁应覆盖
    _relocate_raw_data(TEST_DATE)
    assert dst.exists()
    assert dst.read_text() != "OLD CONTENT", "旧内容应被覆盖"
