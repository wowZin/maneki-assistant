#!/usr/bin/env python3
"""panel_builder 单元测试 — 基于真实 T-1 数据，不 mock 接口。

测试用面板文件：wiki/raw/limit-up/panel/{date}.parquet（--quick 产出）。
"""

import json
import os
import sys
from pathlib import Path

import pandas as pd
import pytest

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from plays.limit_up.panel_builder import _prev_trade_date, data_qc, RAW_DIR


# ═══════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def panel_df():
    """真实面板 parquet（最近一个交易日的 {date}.parquet）。

    注意：panel/ 目录下还有 stk_factor_pro.parquet/cyq_perf.parquet 等
    非日面板文件，必须按 8 位日期文件名过滤，否则会拿错文件。
    """
    files = sorted(f for f in RAW_DIR.glob("*.parquet")
                   if f.stem.isdigit() and len(f.stem) == 8)
    if not files:
        pytest.skip("无面板 parquet，先跑 python3 panel_builder.py --quick")
    df = pd.read_parquet(files[-1])
    assert len(df) > 0, f"{files[-1]} 为空"
    return df


@pytest.fixture(scope="module")
def model_features():
    """XGBoost 模型期望的 64 特征列表。"""
    from plays.limit_up.factors.optimized.model_score import _load_model
    m = _load_model()
    return set(m.feature_cols)


# ═══════════════════════════════════════════════════════════════
# 1. 数据完整性
# ═══════════════════════════════════════════════════════════════

class TestDataIntegrity:
    """面板数据基本结构检查。"""

    def test_has_stocks(self, panel_df):
        assert len(panel_df) >= 50, f"股票数({len(panel_df)}) < 50"

    def test_has_code_and_pit_date(self, panel_df):
        assert "code" in panel_df.columns
        assert "pit_date" in panel_df.columns

    def test_all_values_float64(self, panel_df):
        """所有特征列应为 float64。"""
        for c in panel_df.columns:
            if c in ("code", "pit_date", "name"):
                continue
            assert panel_df[c].dtype == "float64", f"{c} 类型={panel_df[c].dtype}"

    def test_no_nulls(self, panel_df):
        """T-1 数据零缺失（例外：auc_pct——停牌股当日无竞价，NaN 为设计行为，
        XGBoost 原生处理 NaN；pipeline 覆盖 pct_chg_score_day 时只取非空）。"""
        nulls = panel_df.isna().sum()
        bad = nulls[(nulls > 0) & (nulls.index != "auc_pct")]
        assert len(bad) == 0, f"缺失字段: {dict(bad)}"


# ═══════════════════════════════════════════════════════════════
# 2. 特征完整性
# ═══════════════════════════════════════════════════════════════

class TestFeatureCompleteness:
    """面板包含 59 个可离线计算的 PIT 特征（另 5 策略分需实时数据）。"""

    def test_contains_64_features(self, panel_df):
        """面板 ≥64 特征（59 PIT + 5 策略分；另有 auc_pct/model_score 运营列）。"""
        feat_cols = [c for c in panel_df.columns if c not in ("code", "pit_date")]
        assert len(feat_cols) >= 64, f"特征数={len(feat_cols)}, 期望≥64"

    def test_strategy_scores_in_panel(self, panel_df):
        """fundamental/technical/fundflow/sentiment/shortterm 现在也在面板中。"""
        for s in ("fundamental", "technical", "fundflow", "sentiment", "shortterm"):
            assert s in panel_df.columns, f"{s} 应在面板中"

    def test_59_plus_5_strategy_equals_64(self, panel_df, model_features):
        """59(T-1) + 5(实时策略) = 64(模型输入)。"""
        panel_feats = set(c for c in panel_df.columns if c not in ("code", "pit_date"))
        panel_feats |= {"fundamental", "technical", "fundflow", "sentiment", "shortterm"}
        expected = set(model_features)
        missing = expected - panel_feats
        assert len(missing) == 0, f"面板缺特征: {missing}"


# ═══════════════════════════════════════════════════════════════
# 3. 数据质检（data_qc）
# ═══════════════════════════════════════════════════════════════

class TestDataQC:
    """data_qc 函数在真实数据上的行为。"""

    def test_qc_runs_without_error(self, panel_df):
        report = data_qc(panel_df)
        assert report["total_stocks"] == len(panel_df)
        assert report["total_features"] >= 64

    def test_qc_detects_high_zero_rate(self, panel_df):
        """max_step 零值率应 > 90%（多数非连板）。"""
        report = data_qc(panel_df)
        warnings = {w.split(":")[0]: w for w in report["warnings"]}
        assert "max_step" in warnings

    def test_qc_zero_missing(self, panel_df):
        report = data_qc(panel_df)
        # auc_pct 允许缺失（停牌股无竞价，设计行为）
        unexpected = {k: v for k, v in report["missing_rate"].items() if k != "auc_pct"}
        assert len(unexpected) == 0, f"有缺失: {unexpected}"

    def test_qc_feature_stats_have_expected_keys(self, panel_df):
        report = data_qc(panel_df)
        feat = report["feature_stats"]
        assert "mean" in feat.get("circ_mv", {}), f"circ_mv stat 缺失"
        assert "zeros%" in feat.get("circ_mv", {})
        # 验证 daily_basic 数据正常
        assert feat["circ_mv"]["mean"] > 0, "circ_mv 均值=0 (daily_basic 可能未取到)"


# ═══════════════════════════════════════════════════════════════
# 4. 工具函数
# ═══════════════════════════════════════════════════════════════

class TestUtils:
    """工具函数逻辑。"""

    def test_prev_trade_date_returns_yesterday(self):
        """_prev_trade_date 返回的应是交易日的昨天。"""
        prev = _prev_trade_date("20260715")
        assert len(prev) == 8, f"日期格式: {prev}"
        assert prev < "20260715", f"prev({prev}) >= today"

    def test_prev_trade_date_valid_format(self):
        """返回的日期是交易日（通过真实 trade_cal 验证）。"""
        from scripts.tu_share import call_tushare
        prev = _prev_trade_date("20260715")
        cal = call_tushare("trade_cal", {"exchange": "SSE", "start_date": prev, "end_date": prev},
                           "cal_date,is_open")
        items = cal.get("data", {}).get("items", [])
        assert items and len(items[0]) > 1 and items[0][1] == 1, f"{prev} 不是交易日"


# ═══════════════════════════════════════════════════════════════
# 5. 边界情况
# ═══════════════════════════════════════════════════════════════

class TestEdgeCases:
    """边界检查。"""

    def test_circ_mv_positive(self, panel_df):
        """流通市值应 > 0（daily_basic 数据正常）。"""
        assert panel_df["circ_mv"].min() >= 0, "circ_mv 有负值"
        assert panel_df["circ_mv"].mean() > 1, f"circ_mv 均值={panel_df['circ_mv'].mean()} (可能为空)"

    def test_pe_pb_not_all_default(self, panel_df):
        """PE/PB 不应全是 999（兜底值）。"""
        pe_999 = (panel_df["pe"] == 999.0).sum()
        assert pe_999 < len(panel_df), f"PE全为999.0 (daily_basic 可能未取到)"
        pb_999 = (panel_df["pb"] == 999.0).sum()
        assert pb_999 < len(panel_df), f"PB全为999.0"

    def test_limit_up_count_20d_range(self, panel_df):
        """20日涨停次数量级合理。"""
        mean = panel_df["limit_up_count_20d"].mean()
        max_v = panel_df["limit_up_count_20d"].max()
        assert 0 <= mean <= 20, f"20日涨停均值={mean}"
        assert 0 <= max_v <= 20, f"20日涨停最大={max_v}"

    def test_pct_chg_reasonable_range(self, panel_df):
        """昨日 pct_chg 在合理范围内。"""
        mean = panel_df["pct_chg_score_day"].mean()
        assert -10 <= mean <= 10, f"pct_chg 均值={mean} 异常"

    def test_amplitude_reasonable(self, panel_df):
        """振幅在合理范围。"""
        mean = panel_df["amplitude"].mean()
        assert 0 <= mean <= 20, f"振幅均值={mean}%"
class TestDataQCFunction:
    """直接调用 data_qc 函数（不走 fixture），防止面板新增列破坏 QC。"""

    def test_qc_with_name_column(self):
        """带 name 字符串列的 DataFrame 不应导致 data_qc 崩溃。"""
        from plays.limit_up.panel_builder import data_qc
        df = pd.DataFrame({
            "code": ["000001.SZ", "000002.SZ"],
            "pit_date": ["20260725", "20260725"],
            "name": ["平安银行", "万科A"],
            "circ_mv": [1e7, 2e7],
            "pct_chg": [2.5, -1.0],
            "model_score": [72.5, 45.0],
        })
        report = data_qc(df)
        assert report["total_stocks"] == 2
        assert "name" not in report["feature_stats"], "name 列不应进入数值统计"
        assert "circ_mv" in report["feature_stats"], "数值列应正常统计"

