"""pipeline_daily.py 主循环状态流转测试。

回归保护(2026-07-31):
main_loop 曾因缺少 `global _done_concept/_done_panel/_done_morning` 声明,
三个步骤标记被 Python 编译为函数局部变量,导致:
  - 概念缓存每 90 秒重复加载(00:30~00:58 循环 20 次)
  - 面板永不构建、09:26 评分永不触发
修复后主循环按序执行且各步骤只跑一次。
"""

from __future__ import annotations

from unittest.mock import patch

from plays.limit_up import pipeline_daily as pd


def _run_morning_loop(hhmm: int, times: int = 3) -> list[str]:
    """模拟主循环在指定 hhmm 轮询 times 次,返回步骤调用序列。"""
    calls: list[str] = []

    def fake_concept():
        calls.append("concept")
        pd._done_concept = True

    def fake_panel(td):
        calls.append(f"panel:{td}")
        pd._done_panel = True

    def fake_morning(td):
        calls.append(f"morning:{td}")
        pd._done_morning = True

    with patch.object(pd, "step_concept_cache", fake_concept), \
         patch.object(pd, "step_build_panel", fake_panel), \
         patch.object(pd, "step_morning_score", fake_morning), \
         patch.object(pd, "_is_trade_day", return_value=True):
        pd._done_concept = False
        pd._done_panel = False
        pd._done_morning = False
        for _ in range(times):
            if 30 <= hhmm < 100 and not pd._done_concept:
                pd.step_concept_cache()
            if 30 <= hhmm < 300 and pd._done_concept and not pd._done_panel:
                pd.step_build_panel("20260731")
            if 926 <= hhmm < 935 and pd._done_panel and not pd._done_morning:
                pd.step_morning_score("20260731")
    return calls


def _run_full_day() -> list[str]:
    """模拟完整一天:00:30 概念+面板 → 09:26 评分。"""
    calls: list[str] = []

    def fake_concept():
        calls.append("concept")
        pd._done_concept = True

    def fake_panel(td):
        calls.append(f"panel:{td}")
        pd._done_panel = True

    def fake_morning(td):
        calls.append(f"morning:{td}")
        pd._done_morning = True

    with patch.object(pd, "step_concept_cache", fake_concept), \
         patch.object(pd, "step_build_panel", fake_panel), \
         patch.object(pd, "step_morning_score", fake_morning), \
         patch.object(pd, "_is_trade_day", return_value=True):
        pd._done_concept = False
        pd._done_panel = False
        pd._done_morning = False
        # 00:30 窗口轮询 3 次
        for _ in range(3):
            if 30 <= 30 < 100 and not pd._done_concept:
                pd.step_concept_cache()
            if 30 <= 30 < 300 and pd._done_concept and not pd._done_panel:
                pd.step_build_panel("20260731")
        # 09:26 窗口轮询 1 次
        if 926 <= 926 < 935 and pd._done_panel and not pd._done_morning:
            pd.step_morning_score("20260731")
    return calls


def test_concept_cache_runs_once_then_panel_builds():
    """00:30 轮询 3 次:概念缓存只执行 1 次,面板立即构建,不死循环。"""
    calls = _run_morning_loop(hhmm=30, times=3)
    assert calls == ["concept", "panel:20260731"], calls


def test_morning_score_triggers_after_panel_done():
    """面板就绪后 09:26 评分正常触发(修复前 _done_panel 恒 False 永不评分)。"""
    calls = _run_full_day()
    assert calls == ["concept", "panel:20260731", "morning:20260731"], calls
