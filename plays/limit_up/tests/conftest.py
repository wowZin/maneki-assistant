"""打板玩法测试通用 fixture 与红线断言。

红线：`TUSHARE_TOKEN / THS_COOKIE / JVQUANT_TOKEN` 三者任一缺失，
pytest 直接 fail（不 skip）。所有单测真实调用真实数据源，禁止 mock。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# 项目根加入 sys.path，方便所有测试 import
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


REQUIRED_ENV = ("TUSHARE_TOKEN", "THS_COOKIE", "JVQUANT_TOKEN")


def _load_env():
    """从项目根 .env 加载环境变量到 os.environ（若尚未加载）。"""
    env_file = PROJECT_ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def pytest_configure(config):
    """启动时严格检查关键环境变量。缺一律 fail。"""
    _load_env()
    missing = [k for k in REQUIRED_ENV if not os.getenv(k)]
    if missing:
        raise RuntimeError(
            f"缺失环境变量: {missing}. 打板玩法测试红线要求真实调用不 mock，"
            f"缺 token 一律 fail。请在项目根 .env 补齐。"
        )


import pandas as pd  # noqa: E402  # after path setup
import pytest  # noqa: E402


# ── 通用样本股票（真实调用用） ─────────────────
# 挑选流动性充足、上市已久、非 ST/非科创板/非创业板的主板股票
SAMPLE_CODE = "600176.SH"       # 中国巨石（主板）
SAMPLE_CODE_SHORT = "600176"
SAMPLE_CODE_2 = "000001.SZ"     # 平安银行（主板）


@pytest.fixture(scope="session")
def latest_panel_date() -> str:
    """最近一个有面板 parquet 的交易日（8位日期文件名取最大，动态发现不硬编码）。"""
    panel_dir = PROJECT_ROOT / "wiki" / "raw" / "limit-up" / "panel"
    dates = sorted(
        p.stem for p in panel_dir.glob("*.parquet")
        if p.stem.isdigit() and len(p.stem) == 8
    )
    assert dates, f"无面板文件: {panel_dir}"
    return dates[-1]


@pytest.fixture(scope="session")
def sample_code() -> str:
    """真实调用测试用样本股票代码（600176.SH）。"""
    return SAMPLE_CODE


@pytest.fixture(scope="session")
def sample_code_short() -> str:
    return SAMPLE_CODE_SHORT


@pytest.fixture(scope="session")
def synthetic_row() -> pd.Series:
    """离线因子单测用的合成面板行。字段覆盖所有因子依赖列。"""
    return pd.Series({
        "sentiment": 45.0, "shortterm": 55.0, "technical": 60.0,
        "fundflow": 40.0, "fundamental": 50.0,
        "position_20d": 0.60, "trailing_10": 0.10, "trailing_5": 0.05,
        "pct_chg_std_10d": 5.0, "pct_chg_std_5d": 4.0,
        "limit_up_count_20d": 2, "limit_up_count_60d": 4,
        "avg_amount_5d": 1_500_000, "turnover_rate": 8.0,
        "turnover_rate_f": 10.0, "volume_ratio": 1.4,
        "circ_mv": 500_000, "pb": 4.0, "pe": 40.0, "n_concepts": 4,
        "sector_heat": 1.5, "sector_rank": 0.3,
        "dt_is_listed": 1.0, "dt_net_amount": 1_000_000,
        "dt_net_rate": 2.5, "dt_l_buy_ratio": 0.6,
        "dt_n_exalter": 3.0, "dt_inst_net_buy": 500_000,
        "dt_hot_net_buy": 300_000, "dt_inst_sell_ratio": 0.0,
        "pullback_10d": 0.08, "pullback_20d": 0.10,
        "vol_ratio_proxy": 1.4, "amount_ratio": 1.6,
        "amount_3d_increasing": 1, "pct_chg_score_day": 3.5,
        "gap_up": 2.0, "consecutive_up": 3, "avg_pct_chg_5d": 2.0,
        "reversal_signal": 0, "upper_shadow_pct": 20,
        "net_mf_ratio": 0.15, "first_board": 0,
    })
