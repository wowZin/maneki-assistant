"""模型分因子：加载离线训练的树模型，输出连续 0–100 分。

行为：
1. 从 LIMIT_UP_MODEL_PATH 懒加载模型；加载失败则回退到 quality_combo。
2. 把 row 转成模型特征向量，缺失值用训练时中位数填充。
3. 应用追高护栏做乘性调整。
4. 返回 0–100 连续分。

环境变量：
- LIMIT_UP_USE_MODEL: true 时启用（由 factors/__init__.py 控制）。
- LIMIT_UP_MODEL_PATH: 模型目录，默认 plays/limit_up/data/backtest/models。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import numpy as np

from plays.limit_up.factors._helpers import safe
from plays.limit_up.factors.optimized.quality_combo import factor_quality_combo
from plays.limit_up.factors.shortterm.guardrail import factor_chasing_guardrail

DEFAULT_MODEL_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "backtest" / "models"

_MODEL: Any = None
_MODEL_DIR: Path | None = None
_LOAD_ERROR: str | None = None


def _load_model():
    """懒加载 LimitUpModel。"""
    global _MODEL, _MODEL_DIR, _LOAD_ERROR
    if _MODEL is not None:
        return _MODEL

    model_path = os.environ.get("LIMIT_UP_MODEL_PATH", "")
    model_dir = Path(model_path) if model_path else DEFAULT_MODEL_DIR
    _MODEL_DIR = model_dir

    joblib_file = model_dir / "limit_up_model.joblib"
    if not joblib_file.exists():
        _LOAD_ERROR = f"模型文件不存在: {joblib_file}"
        return None

    try:
        from plays.limit_up.backtest.model import LimitUpModel
        _MODEL = LimitUpModel.load(model_dir)
        return _MODEL
    except Exception as e:
        _LOAD_ERROR = f"模型加载失败: {e}"
        return None


def factor_model_score(row) -> float:
    """返回模型分（0–100），模型不可用时回退到 quality_combo。"""
    model = _load_model()
    if model is None:
        return factor_quality_combo(row)

    # 转成模型特征向量（LimitUpModel._prepare_x 支持 dict/Series/DataFrame）
    try:
        score = model.predict_score(row)
        if isinstance(score, (list, tuple)):
            score = float(score[0])
        elif isinstance(score, np.ndarray):
            score = float(score.item())
        else:
            score = float(score)
    except Exception:
        return factor_quality_combo(row)

    # 追高护栏
    score = factor_chasing_guardrail(
        score,
        trailing_5=safe(row.get("trailing_5"), 0.0),
        trailing_10=safe(row.get("trailing_10"), 0.0),
        position_20d=safe(row.get("position_20d"), 0.5),
        pullback_10d=safe(row.get("pullback_10d"), 0.1),
    )
    return round(max(0.0, min(100.0, score)), 2)


def clear_model_cache():
    """用于测试：清空懒加载缓存。"""
    global _MODEL, _MODEL_DIR, _LOAD_ERROR
    _MODEL = None
    _MODEL_DIR = None
    _LOAD_ERROR = None
