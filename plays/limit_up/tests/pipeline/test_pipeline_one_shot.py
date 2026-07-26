"""pipeline.py（2026-07-25 重构一次性进程）核心逻辑单测。

红线：stk_auction / 面板 / XGBoost 模型评分全部真实执行，禁止 mock 数据接口。
写文件一律打到 tmp_path 副本（面板/analysis/pool 真实文件只读不改）；
仅隔离告警/推送副作用通道（非数据接口），防止测试误发飞书。
"""

from __future__ import annotations

import json
import math
import shutil
import sys
from pathlib import Path

import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from plays.limit_up import pipeline  # noqa: E402
from plays.limit_up.utils import _is_trade_day  # noqa: E402

REAL_PANEL_DIR = PROJECT_ROOT / "wiki" / "raw" / "limit-up" / "panel"
REAL_PLAY_DIR = PROJECT_ROOT / "plays" / "limit_up"


def _shortterm_expected(auc_amt_ratio) -> float:
    """手算 shortterm 分档（与 pipeline._refresh_panel_auction / panel_builder 同公式）：
    base10 + 量比分档（>1→20，>0.5→10，>0.1→5，否则0），clip 100。"""
    a = float(auc_amt_ratio) if auc_amt_ratio is not None else 0.0
    if math.isnan(a):
        a = 0.0
    return min(10.0 + (20 if a > 1 else (10 if a > 0.5 else (5 if a > 0.1 else 0))), 100.0)


@pytest.fixture()
def tmp_env(tmp_path, monkeypatch, latest_panel_date):
    """把 PANEL_FILE/PLAY_DIR/ANALYSIS_FILE/HEALTH_DIR 打到 tmp_path。

    面板与 pool/analysis 用真实文件的副本，保证真实数据 + 零污染。
    """
    td = latest_panel_date
    panel_copy = tmp_path / f"{td}.parquet"
    shutil.copy(REAL_PANEL_DIR / f"{td}.parquet", panel_copy)

    play = tmp_path / "limit_up"
    (play / "data" / "pool").mkdir(parents=True)
    (play / "data" / "analysis").mkdir(parents=True)
    pool_src = REAL_PLAY_DIR / "data" / "pool" / f"pool_{td}.json"
    if pool_src.exists():
        shutil.copy(pool_src, play / "data" / "pool" / pool_src.name)
    af_src = REAL_PLAY_DIR / "data" / "analysis" / f"{td}.json"
    if af_src.exists():
        shutil.copy(af_src, play / "data" / "analysis" / af_src.name)

    monkeypatch.setattr(pipeline, "PANEL_FILE", lambda d: panel_copy)
    monkeypatch.setattr(pipeline, "PLAY_DIR", play)
    monkeypatch.setattr(
        pipeline, "ANALYSIS_FILE",
        lambda d: play / "data" / "analysis" / f"{d}.json")
    monkeypatch.setattr(pipeline, "HEALTH_DIR", tmp_path / "health")
    # 副作用隔离（非数据接口）：失败告警不发真飞书；推送闸强制关闭，防交易时段误推
    monkeypatch.setattr(pipeline, "_notify_text", lambda text: None)
    monkeypatch.setattr("plays.limit_up.pusher.is_trading_time", lambda: False)
    return td, panel_copy, play


class TestRefreshPanelAuction:
    """① 竞价刷新面板：stk_auction 按日期全量真实拉取。"""

    def test_real_auction_refresh(self, tmp_env):
        td, panel_copy, _ = tmp_env
        ok = pipeline._refresh_panel_auction(td)
        assert ok, f"{td} stk_auction 真实拉取连续失败（不应发生）"

        df = pd.read_parquet(panel_copy)
        # auc_pct 列存在（刷新后必有，即使部分票无竞价）
        assert "auc_pct" in df.columns
        # 竞价额>0 的票应覆盖全市场大部分（真实全市场竞价 ~5000 只）
        n_auc = int((df["auc_amount"] > 0).sum())
        assert n_auc > 1000, f"auc_amount>0 仅 {n_auc} 行"
        # shortterm 分档值域：10 + {0,5,10,20} = {10,15,20,30}
        st_values = set(df["shortterm"].dropna().astype(float).unique().tolist())
        assert st_values <= {10.0, 15.0, 20.0, 30.0}, f"shortterm 异常取值: {st_values}"
        # 抽 10 行手算核对 shortterm 与 auc_amt_ratio 分档一致
        sample = df.sample(10, random_state=42)
        for _, r in sample.iterrows():
            expected = _shortterm_expected(r["auc_amt_ratio"])
            actual = float(r["shortterm"])
            assert abs(actual - expected) < 1e-6, (
                f"{r['code']}: shortterm={actual} 与手算 {expected} 不一致 "
                f"(auc_amt_ratio={r['auc_amt_ratio']})")


class TestMorningPass:
    """② 全量静态模型评分（真实 XGBoost，pct 用竞价涨幅）。"""

    REQUIRED_FIELDS = {"code", "name", "model_score", "total_score",
                       "score_mode", "pct_chg", "scores", "fundamental"}
    DIM_KEYS = {"technical", "fundflow", "sentiment", "shortterm"}

    def test_real_morning_pass(self, tmp_env):
        td, panel_copy, play = tmp_env
        recs = pipeline.morning_pass(td)
        assert len(recs) > 1000, f"主板记录太少: {len(recs)}"

        for r in recs:
            # 全部主板（00/60 开头，打板不看 20cm）
            assert r["code"][:2] in ("00", "60"), f"非主板: {r['code']}"
            # model_score 落在 0-100
            assert 0.0 <= r["model_score"] <= 100.0, (
                f"{r['code']} model_score 越界: {r['model_score']}")
            # 记录字段齐全
            assert self.REQUIRED_FIELDS <= set(r), (
                f"{r['code']} 缺字段: {self.REQUIRED_FIELDS - set(r)}")
            assert self.DIM_KEYS == set(r["scores"]), (
                f"{r['code']} scores 维度不齐: {set(r['scores'])}")
            assert r["score_mode"] == "model_score"

        # model_score 全量写回面板：非空行数 == 面板行数
        df = pd.read_parquet(panel_copy)
        assert "model_score" in df.columns
        n_notna = int(df["model_score"].notna().sum())
        assert n_notna == len(df), f"model_score 非空 {n_notna} != 面板行数 {len(df)}"

        # analysis 合并落盘（tmp 副本）：按 code 去重
        af = play / "data" / "analysis" / f"{td}.json"
        assert af.exists()
        data = json.loads(af.read_text())
        codes = [r["code"] for r in data]
        assert len(codes) == len(set(codes)), "analysis 存在重复 code"


class TestNonTradeDayExit:
    """③ 非交易日退出逻辑：main 对周日日期不抛异常、直接返回。"""

    def test_sunday_exits_clean(self, monkeypatch):
        # 真实 tushare 交易日历判断：2026-07-26 是周日
        assert _is_trade_day("20260726") is False, "20260726(周日) 被误判为交易日"
        monkeypatch.setattr(sys, "argv", ["pipeline.py", "--date", "20260726"])
        # 非交易日直接 return，不抛异常、不进入评分流程
        assert pipeline.main() is None
