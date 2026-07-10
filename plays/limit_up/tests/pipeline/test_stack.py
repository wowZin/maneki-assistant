"""待评分栈单测。"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from plays.limit_up.stack import (
    ScoreStack, save_queue, load_queue, clear_queue,
    QUEUE_DIR,
)


def _make_quotes(pairs: list[tuple[str, float, str]]) -> dict[str, dict]:
    """辅助：造查询结果。 (code, pct_chg, name)"""
    return {
        code: {"pct_chg": pct, "name": name}
        for code, pct, name in pairs
    }


class TestStackInit:
    def test_empty_stack(self):
        s = ScoreStack()
        assert s.size == 0
        assert s.pop_top(10) == []


class TestStackUpdate:
    def test_new_stock_enters_stack(self):
        s = ScoreStack()
        quotes = _make_quotes([("000001.SZ", 3.5, "平安银行")])
        s.update(quotes)
        assert s.size == 1
        item = list(s.items.values())[0]
        assert item.code == "000001.SZ"
        assert item.name == "平安银行"
        assert item.pct_chg == 3.5
        assert item.speed == 0.0  # 首次入栈，涨速=0

    def test_negative_pct_excluded(self):
        s = ScoreStack()
        quotes = _make_quotes([("000001.SZ", -2.0, "平安银行")])
        s.update(quotes)
        assert s.size == 0  # 不进入栈

    def test_zero_pct_excluded(self):
        s = ScoreStack()
        quotes = _make_quotes([("000001.SZ", 0.0, "平安银行")])
        s.update(quotes)
        assert s.size == 0

    def test_multiple_stocks(self):
        s = ScoreStack()
        quotes = _make_quotes([
            ("000001.SZ", 2.0, "平安银行"),
            ("000002.SZ", 3.0, "万科A"),
            ("000003.SZ", 4.0, "某股"),
        ])
        s.update(quotes)
        assert s.size == 3

    def test_drop_from_stack(self):
        """涨幅从正变负，应该踢出栈"""
        s = ScoreStack()
        s.update(_make_quotes([("000001.SZ", 3.0, "")]))
        assert s.size == 1
        s.update(_make_quotes([("000001.SZ", -1.0, "")]))
        assert s.size == 0

    def test_speed_calculation(self):
        s = ScoreStack()
        # 第一轮：+2.0%，涨速=0
        s.update(_make_quotes([("000001.SZ", 2.0, "")]))
        assert s.items["000001.SZ"].speed == 0.0
        # 第二轮：+3.0%，涨速=1.0
        s.update(_make_quotes([("000001.SZ", 3.0, "")]))
        assert s.items["000001.SZ"].speed == 1.0


class TestStackSorting:
    def test_score_formula(self):
        """score = pct * 0.3 + speed * 0.7"""
        s = ScoreStack()
        s.update(_make_quotes([
            ("A", 2.0, ""),  # 首次: speed=0, score=2*0.3 = 0.6
        ]))
        # 第二轮
        s.update(_make_quotes([
            ("A", 4.0, ""),  # speed=2.0, score=4*0.3+2*0.7 = 1.2+1.4 = 2.6
            ("B", 3.0, ""),  # speed=0, score=3*0.3 = 0.9
        ]))
        assert s.items["A"].score == pytest.approx(2.6)
        assert s.items["B"].score == pytest.approx(0.9)

    def test_pop_top_returns_highest_score_first(self):
        s = ScoreStack()
        s.prev_pct = {"A": 0, "B": 0, "C": 0}
        s.items = {
            "A": ScoreStack.Item("A", "", 5.0, 2.0, 5*0.3+2*0.7, 0),
            "B": ScoreStack.Item("B", "", 3.0, 1.0, 3*0.3+1*0.7, 0),
            "C": ScoreStack.Item("C", "", 4.0, 1.5, 4*0.3+1.5*0.7, 0),
        }
        top = s.pop_top(2)
        # A: 5*0.3+2*0.7=1.5+1.4=2.9, C: 4*0.3+1.5*0.7=1.2+1.05=2.25, B: 3*0.3+1*0.7=0.9+0.7=1.6
        # Sorted: A(2.9) > C(2.25) > B(1.6)
        assert [item.code for item in top] == ["A", "C"]

    def test_pop_top_more_than_available(self):
        s = ScoreStack()
        s.update(_make_quotes([("A", 2.0, "")]))
        top = s.pop_top(100)
        assert len(top) == 1


class TestStackPersistence:
    def test_save_and_load_roundtrip(self, tmp_path):
        s = ScoreStack()
        s.update(_make_quotes([
            ("000001.SZ", 3.5, "平安银行"),
            ("000002.SZ", 2.8, "万科A"),
        ]))
        # 再更新一次产生涨速
        s.update(_make_quotes([
            ("000001.SZ", 4.0, "平安银行"),
            ("000002.SZ", 2.0, "万科A"),  # 涨速负的，score低
        ]))

        d = s.to_dict()
        restored = ScoreStack.from_dict(d)
        assert restored.size == 2
        assert restored.items["000001.SZ"].pct_chg == 4.0
        assert restored.items["000001.SZ"].speed == 0.5
        assert restored.prev_pct["000001.SZ"] == 4.0

    def test_save_queue_file(self):
        s = ScoreStack()
        s.update(_make_quotes([("000001.SZ", 2.0, "")]))
        path = save_queue(s, "20260710_test")
        assert path.exists()
        loaded = load_queue("20260710_test")
        assert loaded is not None
        assert loaded.size == 1

    def test_clear_queue(self):
        s = ScoreStack()
        s.update(_make_quotes([("A", 2.0, "")]))
        save_queue(s, "20260710_clear_test")
        assert load_queue("20260710_clear_test") is not None
        clear_queue("20260710_clear_test")
        assert load_queue("20260710_clear_test") is None

    def test_load_nonexistent(self):
        loaded = load_queue("19990101")
        assert loaded is None


class TestStackClear:
    def test_clear_empties_stack(self):
        s = ScoreStack()
        s.update(_make_quotes([("A", 2.0, "")]))
        assert s.size == 1
        s.clear()
        assert s.size == 0
        assert s.prev_pct == {}
