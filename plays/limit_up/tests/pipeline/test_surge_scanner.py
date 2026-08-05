"""surge_scanner.py（盘中异动扫描 → watchdog surge）核心逻辑单测。

红线：面板 / pool / limit_list_d / cyq / concept 全部真实读取，禁止 mock 数据接口。
日缓存与 analysis/pushed 写入一律打到 tmp_path，真实目录只读不改。
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from plays.limit_up import surge_scanner as ss  # noqa: E402

REAL_PLAY_DIR = PROJECT_ROOT / "plays" / "limit_up"

# pipeline 记录必备字段（surge 记录需与之同构）
PIPELINE_FIELDS = {"code", "name", "model_score", "total_score",
                   "score_mode", "pct_chg", "scores", "fundamental"}


@pytest.fixture()
def uni(tmp_path, monkeypatch, latest_panel_date):
    """对最近面板日期真实构建扫描宇宙（日缓存打到 tmp_path）。

    返回 (td, universe, pool_dir)。面板/limit_list_d 真实读取。
    2026-08-05：pool_builder 已删除，名称改读面板 name 列，不再复制 pool 文件。
    """
    td = latest_panel_date
    play = tmp_path / "limit_up"
    pool_dir = play / "data" / "pool"
    pool_dir.mkdir(parents=True)
    monkeypatch.setattr(ss, "PLAY_DIR", play)
    u = ss.build_universe(td)
    return td, u, pool_dir


class TestBuildUniverse:
    """扫描宇宙：面板分 + 昨日涨停/基因 + 名称，日缓存。"""

    def test_universe_real_data(self, uni):
        td, u, _ = uni
        # scores 非空且 key 全部主板
        assert u["scores"], f"{td} 面板 scores 为空（pipeline 未评分？）"
        for c in u["scores"]:
            assert c[:2] in ("00", "60"), f"scores 含非主板: {c}"
        # 昨日涨停 / 前20日涨停基因非空（真实 limit_list_d）
        assert u["yesterday_limit"], "昨日涨停为空"
        assert u["gene"], "前20日涨停基因为空"
        # names 覆盖 scores 的 90%+
        cov = sum(1 for c in u["scores"] if c in u["names"]) / len(u["scores"])
        assert cov >= 0.9, f"names 覆盖率 {cov:.1%} < 90%"
        # dims/basics 与 scores 同键（面板列）
        for c in list(u["scores"])[:50]:
            assert c in u["dims"] and c in u["basics"]

    def test_second_call_hits_cache(self, uni):
        td, u, pool_dir = uni
        cache = pool_dir / f"surge_universe_{td}.json"
        assert cache.exists(), "首次构建未写日缓存"
        mtime = cache.stat().st_mtime
        u2 = ss.build_universe(td)  # 二次调用
        assert cache.stat().st_mtime == mtime, "二次调用重写了缓存（未命中）"
        assert u2["scores"] == u["scores"]


class TestRouting:
    """路由逻辑：主闸≥20 直通，<20 走排雷；排雷检查对真实票返回 bool。"""

    def test_main_gate_and_screen_routing(self, uni):
        td, u, _ = uni
        scores = u["scores"]
        # scan() 同款路由预筛
        main_pool = {c for c, s in scores.items() if s >= ss.SURGE_PANEL_SCORE}
        screen_pool = (set(u["yesterday_limit"]) | set(u["gene"])) - main_pool
        assert main_pool, f"真实面板无主闸票（model_score≥{ss.SURGE_PANEL_SCORE:.0f}）"
        assert screen_pool, "排雷池为空（昨日涨停∪基因 应覆盖主闸外票）"

        # 主闸票：面板分≥阈值 → scan 中 c in main_pool 直接过（ok=True）
        c_main = sorted(main_pool)[0]
        assert scores[c_main] >= ss.SURGE_PANEL_SCORE

        # 排雷票：不在主闸池（分<20 或无分）→ scan 中走排雷分支
        c_scr = sorted(screen_pool)[0]
        assert c_scr not in main_pool
        assert scores.get(c_scr, 0.0) < ss.SURGE_PANEL_SCORE

        # 排雷两项真实检查返回 bool（cyq 筹码 / 窄概念联动）
        assert isinstance(ss.cyq_no_pressure(c_scr, td), bool)
        round_codes = sorted(screen_pool)[:30]
        assert isinstance(ss.sector_resonance(c_scr, round_codes), bool)
        # 昨日涨停真实票也过一遍 cyq（连板通道：量比+筹码）
        if u["yesterday_limit"]:
            c_lb = sorted(u["yesterday_limit"])[0]
            assert isinstance(ss.cyq_no_pressure(c_lb, td), bool)


class TestSurgeRecord:
    """_surge_record：score_mode 路由 + 与 pipeline 记录同构。"""

    def test_score_mode_and_fields(self, uni):
        td, u, _ = uni
        scores, dims, names = u["scores"], u["dims"], u["names"]

        # 主闸票（真实最高面板分）→ model_score
        c_main = max(scores, key=scores.get)
        rec_main = ss._surge_record(c_main, names.get(c_main, ""), 6.5,
                                    scores[c_main], dims.get(c_main))
        assert rec_main["score_mode"] == "model_score"
        assert rec_main["model_score"] == scores[c_main]

        # 排雷票（真实低分票 <20）→ surge_screen
        lows = [c for c, s in scores.items() if s < ss.SURGE_PANEL_SCORE]
        assert lows, "面板无 <20 低分票"
        c_low = lows[0]
        rec_low = ss._surge_record(c_low, names.get(c_low, ""), 5.5,
                                   scores[c_low], dims.get(c_low))
        assert rec_low["score_mode"] == "surge_screen"

        # 无分票（面板外）→ surge_screen，model_score=None
        rec_none = ss._surge_record("000001.SZ", "平安银行", 5.0, None, None)
        assert rec_none["score_mode"] == "surge_screen"
        assert rec_none["model_score"] is None
        assert rec_none["source"] == "surge"

        # 三种记录均与 pipeline 记录同构（字段超集）
        for r in (rec_main, rec_low, rec_none):
            assert PIPELINE_FIELDS <= set(r), f"缺字段: {PIPELINE_FIELDS - set(r)}"
            assert {"technical", "fundflow", "sentiment", "shortterm"} <= set(r["scores"])


class TestWriteDedup:
    """_write_analysis / _write_pushed：按 code 去重覆盖（同 code 写两次只留新记录）。"""

    TD = "20990101"  # 虚构日期，文件只落 tmp_path

    def _rec(self, code, pct):
        return {"code": code, "name": f"n-{code}", "model_score": 30.0,
                "total_score": 30.0, "score_mode": "model_score",
                "pct_chg": pct, "scores": {}, "fundamental": 0.0,
                "source": "surge"}

    def test_write_analysis_dedup_overwrite(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ss, "ANALYSIS_DIR", tmp_path)
        r1 = self._rec("000001.SZ", 5.0)
        r2 = self._rec("600519.SH", 6.0)
        ss._write_analysis([r1, r2], self.TD)
        # 同 code 再写一次（新 pct），应覆盖旧记录
        ss._write_analysis([self._rec("000001.SZ", 7.7)], self.TD)

        data = json.loads((tmp_path / f"{self.TD}.json").read_text())
        assert len(data) == 2, f"去重失败: {len(data)} 条"
        by_code = {r["code"]: r for r in data}
        assert by_code["000001.SZ"]["pct_chg"] == 7.7, "同 code 未覆盖为新记录"
        assert by_code["600519.SH"]["pct_chg"] == 6.0

    def test_write_pushed_dedup_overwrite(self, tmp_path, monkeypatch):
        play = tmp_path / "limit_up"
        monkeypatch.setattr(ss, "PLAY_DIR", play)
        r1 = self._rec("000001.SZ", 5.0)
        r2 = self._rec("600519.SH", 6.0)
        ss._write_pushed([r1, r2], self.TD)
        ss._write_pushed([self._rec("000001.SZ", 8.8)], self.TD)

        pf = play / "data" / "pushed" / f"{self.TD}_surge.json"
        assert pf.exists()
        data = json.loads(pf.read_text())
        assert len(data) == 2, f"去重失败: {len(data)} 条"
        by_code = {r["code"]: r for r in data}
        assert by_code["000001.SZ"]["pct_chg"] == 8.8, "同 code 未覆盖为新记录"

    def test_write_empty_noop(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ss, "ANALYSIS_DIR", tmp_path)
        play = tmp_path / "limit_up2"
        monkeypatch.setattr(ss, "PLAY_DIR", play)
        ss._write_analysis([], self.TD)
        ss._write_pushed([], self.TD)
        assert not (tmp_path / f"{self.TD}.json").exists()
        assert not (play / "data" / "pushed" / f"{self.TD}_surge.json").exists()
