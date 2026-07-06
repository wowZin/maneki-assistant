"""打板玩法因子库。

按维度组织，每个因子函数签名 `factor_xxx(row) -> float`。
row 可以是 pandas.Series 或 dict，两种都能读到面板/factor_ctx 的字段。

因子注册表 REGISTRY 是运行时唯一事实：
- pipeline 通过 REGISTRY 查找因子函数
- backtest/validate.py 通过 REGISTRY 遍历因子
- factors.md 是文档层唯一事实，与 REGISTRY 一一对应

新增因子必须：
1. 在对应 factors/<dim>/*.py 增加 factor_xxx 函数
2. 在本文件 REGISTRY 登记
3. 在 docs/factors.md 增加说明
4. 在 tests/factors/ 增加真实调用单测
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

# 确保 .env 在判断模型开关前加载（cron 任务没有 shell 环境变量）
_env_paths = [
    Path(__file__).resolve().parent.parent.parent.parent / ".env",
    Path("/root/maneki-agent/.env"),
]
for _p in _env_paths:
    if _p.exists():
        try:
            for _line in _p.read_text().splitlines():
                _line = _line.strip()
                if _line and not _line.startswith("#") and "=" in _line:
                    _k, _v = _line.split("=", 1)
                    os.environ.setdefault(_k.strip(), _v.strip())
        except Exception:
            pass

# ── fundamental ─────────────────────────────────────────
from plays.limit_up.factors.fundamental.rebuilt import factor_fundamental_rebuilt

# ── technical ───────────────────────────────────────────
from plays.limit_up.factors.technical.rebuilt import factor_technical_rebuilt
from plays.limit_up.factors.technical.nonlinear import factor_technical_nonlinear
from plays.limit_up.factors.technical.pullback import (
    factor_pullback_quality,
    factor_pullback_from_peak,
    factor_position_optimal,
)
from plays.limit_up.factors.technical.volume import (
    factor_vol_expansion_quality,
    factor_amount_acceleration,
    factor_amount_surge,
)
from plays.limit_up.factors.technical.pattern import (
    factor_reversal_signal,
    factor_gap_up_quality,
    factor_consecutive_strength,
)
from plays.limit_up.factors.technical.breakout import factor_breakout_quality

# ── fundflow ────────────────────────────────────────────
from plays.limit_up.factors.fundflow.rebuilt import factor_fundflow_rebuilt

# ── sentiment ───────────────────────────────────────────
from plays.limit_up.factors.sentiment.amount_boosted import factor_sentiment_amount_boosted
from plays.limit_up.factors.sentiment.position_combo import factor_sentiment_position_combo
from plays.limit_up.factors.sentiment.volatility_combo import factor_sentiment_volatility_combo
from plays.limit_up.factors.sentiment.amount_combo import factor_sentiment_amount_combo
from plays.limit_up.factors.sentiment.ensemble import factor_sentiment_ensemble
from plays.limit_up.factors.sentiment.pure_boosted import factor_sentiment_pure_boosted

# ── shortterm ───────────────────────────────────────────
from plays.limit_up.factors.shortterm.limit_gene import (
    factor_limit_up_gene_20d,
    factor_limit_up_gene_60d,
    factor_limit_up_gene_composite,
)
from plays.limit_up.factors.shortterm.limit_gene_amount import factor_limit_gene_amount
from plays.limit_up.factors.shortterm.limit_gene_momentum import factor_limit_gene_momentum
from plays.limit_up.factors.shortterm.trailing import factor_trailing_momentum
from plays.limit_up.factors.shortterm.intraday import factor_intraday_strength
from plays.limit_up.factors.shortterm.turnover import factor_turnover_momentum
from plays.limit_up.factors.shortterm.growth import factor_growth_momentum
from plays.limit_up.factors.shortterm.guardrail import factor_chasing_guardrail  # 特殊签名，不入 REGISTRY

# ── crossdim (mine 备用，不入 total_score) ────────────
from plays.limit_up.factors.crossdim.divergence import factor_dimension_divergence
from plays.limit_up.factors.crossdim.quality_bonus import factor_total_quality_bonus

# ── optimized（训练集优化后的生产因子）───────────────────
from plays.limit_up.factors.optimized.quality_gate import factor_quality_gate
from plays.limit_up.factors.optimized.quality_combo import factor_quality_combo
from plays.limit_up.factors.optimized.model_score import factor_model_score


REGISTRY: dict[str, Callable] = {
    # fundamental
    "fundamental_rebuilt": factor_fundamental_rebuilt,
    # technical
    "technical_rebuilt": factor_technical_rebuilt,
    "technical_nonlinear": factor_technical_nonlinear,
    "pullback_quality": factor_pullback_quality,
    "pullback_from_peak": factor_pullback_from_peak,
    "position_optimal": factor_position_optimal,
    "vol_expansion_quality": factor_vol_expansion_quality,
    "amount_acceleration": factor_amount_acceleration,
    "amount_surge": factor_amount_surge,
    "reversal_signal": factor_reversal_signal,
    "gap_up_quality": factor_gap_up_quality,
    "consecutive_strength": factor_consecutive_strength,
    "breakout_quality": factor_breakout_quality,
    # fundflow
    "fundflow_rebuilt": factor_fundflow_rebuilt,
    # sentiment
    "sentiment_amount_boosted": factor_sentiment_amount_boosted,
    "sentiment_position_combo": factor_sentiment_position_combo,
    "sentiment_volatility_combo": factor_sentiment_volatility_combo,
    "sentiment_amount_combo": factor_sentiment_amount_combo,
    "sentiment_ensemble": factor_sentiment_ensemble,
    "sentiment_pure_boosted": factor_sentiment_pure_boosted,
    # shortterm
    "limit_up_gene_20d": factor_limit_up_gene_20d,
    "limit_up_gene_60d": factor_limit_up_gene_60d,
    "limit_up_gene_composite": factor_limit_up_gene_composite,
    "limit_gene_amount": factor_limit_gene_amount,
    "limit_gene_momentum": factor_limit_gene_momentum,
    "trailing_momentum": factor_trailing_momentum,
    "intraday_strength": factor_intraday_strength,
    "turnover_momentum": factor_turnover_momentum,
    "growth_momentum": factor_growth_momentum,
    # crossdim (mine 备用)
    "dimension_divergence": factor_dimension_divergence,
    "total_quality_bonus": factor_total_quality_bonus,
    # optimized
    "quality_gate": factor_quality_gate,
    "quality_combo": factor_quality_combo,
    "model_score": factor_model_score,
}


DIMENSIONS: dict[str, list[str]] = {
    "fundamental": ["fundamental_rebuilt"],
    "technical": [
        "technical_rebuilt", "technical_nonlinear",
        "pullback_quality", "pullback_from_peak", "position_optimal",
        "vol_expansion_quality", "amount_acceleration", "amount_surge",
        "reversal_signal", "gap_up_quality", "consecutive_strength",
        "breakout_quality",
    ],
    "fundflow": ["fundflow_rebuilt"],
    "sentiment": [
        "sentiment_amount_boosted", "sentiment_position_combo",
        "sentiment_volatility_combo", "sentiment_amount_combo",
        "sentiment_ensemble", "sentiment_pure_boosted",
    ],
    "shortterm": [
        "limit_up_gene_20d", "limit_up_gene_60d", "limit_up_gene_composite",
        "limit_gene_amount", "limit_gene_momentum",
        "trailing_momentum", "intraday_strength",
        "turnover_momentum", "growth_momentum",
    ],
    "crossdim": ["dimension_divergence", "total_quality_bonus"],
    "optimized": ["quality_gate", "quality_combo", "model_score"],
}


# 生产总分组件：默认仍用 quality_combo；设置 LIMIT_UP_USE_MODEL=true 切换到 model_score
_use_model = os.environ.get("LIMIT_UP_USE_MODEL", "").lower() in ("true", "1", "yes")
TOTAL_SCORE_COMPONENTS: dict[str, float] = {
    "model_score": 1.0,
} if _use_model else {
    "quality_combo": 1.0,
}


__all__ = [
    "REGISTRY",
    "DIMENSIONS",
    "TOTAL_SCORE_COMPONENTS",
    "factor_chasing_guardrail",
]
