"""候选池构建单测。

红线：真实 API 调用，禁止 mock。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from plays.limit_up.pool_builder import (
    POOL_DIR,
    build_pool,
    save_pool,
    load_pool,
    ensure_pool,
    _is_main_board,
    _is_excluded_board,
    _load_stock_basic,
    _load_market_data,
)


class TestIsMainBoard:
    """代码规则：主板判断"""

    def test_00_prefix(self):
        assert _is_main_board("000001.SZ") is True

    def test_60_prefix(self):
        assert _is_main_board("600519.SH") is True

    def test_30_prefix(self):
        assert _is_main_board("300750.SZ") is False

    def test_68_prefix(self):
        assert _is_main_board("688981.SH") is False

    def test_8_prefix(self):
        assert _is_main_board("872925.BJ") is False


class TestIsExcludedBoard:
    """代码规则：排除板块"""

    def test_300_excluded(self):
        assert _is_excluded_board("300750.SZ") is True

    def test_688_excluded(self):
        assert _is_excluded_board("688981.SH") is True

    def test_00_not_excluded(self):
        assert _is_excluded_board("000001.SZ") is False

    def test_60_not_excluded(self):
        assert _is_excluded_board("600519.SH") is False


class TestLoadStockBasic:
    """stock_basic 数据源有效性"""

    def test_returns_data(self):
        data = _load_stock_basic()
        assert len(data) > 4000  # A股市场至少4000只
        # 检查格式
        sample = list(data.values())[0]
        assert "name" in sample
        assert "list_date" in sample

    def test_contains_major_stocks(self):
        data = _load_stock_basic()
        assert "000001.SZ" in data
        assert "600519.SH" in data


class TestLoadMarketData:
    """daily_basic 全市场数据有效性"""

    @pytest.mark.parametrize("date", ["20260710", "20260709", "20260708"])
    def test_returns_full_market(self, date):
        data = _load_market_data(date)
        assert len(data) > 4000  # 全市场至少4000只
        # 检查字段
        sample = data[0]
        assert "ts_code" in sample
        assert "circ_mv" in sample


class TestBuildPool:
    """候选池构建核心逻辑"""

    def test_pool_size_in_range(self):
        """候选池应该在合理数量范围内"""
        pool = build_pool("20260710")
        assert len(pool) >= 2500, f"候选池太小: {len(pool)}"
        assert len(pool) <= 4000, f"候选池太大: {len(pool)}"  # 全市场主板非ST满120天 ≈3032（2026-07 取消市值带后）

    def test_no_st_stocks(self):
        """池中不应有ST股"""
        pool = build_pool("20260710")
        for s in pool:
            assert "ST" not in s["name"] and "*ST" not in s["name"], f"含ST股: {s}"

    def test_all_main_board(self):
        """所有股票应为主板(00/60开头)"""
        pool = build_pool("20260710")
        for s in pool:
            assert _is_main_board(s["code"]), f"非主板: {s}"

    def test_no_market_cap_filter(self):
        """2026-07-25 起取消 50-300亿 市值带：池中应同时存在 <50亿 和 >300亿 的票。

        circ_mv 单位万元：50亿=500,000 万，300亿=3,000,000 万。
        """
        pool = build_pool("20260710")
        mvs = [float(s.get("circ_mv", 0) or 0) for s in pool]
        assert any(0 < mv < 500_000 for mv in mvs), \
            "池中无 <50亿 小票（市值下限过滤疑似仍存在）"
        assert any(mv > 3_000_000 for mv in mvs), \
            "池中无 >300亿 大票（市值上限过滤疑似仍存在）"

    def test_unique_codes(self):
        """候选池不应有重复代码"""
        pool = build_pool("20260710")
        codes = [s["code"] for s in pool]
        assert len(codes) == len(set(codes))

    def test_has_required_fields(self):
        """每个记录应有code和name"""
        pool = build_pool("20260710")
        for s in pool:
            assert "code" in s
            assert "name" in s

    @pytest.mark.parametrize("date", ["20260709", "20260708"])
    def test_different_dates(self, date):
        """不同日期应都能构建"""
        pool = build_pool(date)
        assert len(pool) >= 500


class TestSaveAndLoadPool:
    """持久化"""

    def test_save_and_load_roundtrip(self):
        pool = build_pool("20260710")
        save_pool(pool, "20260710")
        loaded = load_pool("20260710")
        assert loaded is not None
        assert len(loaded) == len(pool)
        assert loaded[0]["code"] == pool[0]["code"]

    def test_load_nonexistent(self):
        loaded = load_pool("19990101")
        assert loaded is None


class TestEnsurePool:
    """ensure_pool 缓存逻辑"""

    def test_ensure_creates_pool(self):
        # 强制重建
        pool = ensure_pool("20260710", force=True)
        assert len(pool) >= 500

    def test_ensure_uses_cache(self):
        # 第一次之后，不传 force 应该使用缓存
        pool1 = ensure_pool("20260710", force=True)
        pool2 = ensure_pool("20260710")  # 命中缓存
        assert len(pool2) == len(pool1)
