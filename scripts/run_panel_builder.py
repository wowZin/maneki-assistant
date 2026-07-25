#!/usr/bin/env python3
"""panel_builder 入口：交易日自动构建 T-1 面板，非交易日跳过。
环境变量 _PANEL_DATE 可覆盖日期（YYYYMMDD），用于批量重建。"""
import os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from plays.limit_up.utils import _is_trade_day, _today_str

today = os.environ.get("_PANEL_DATE") or _today_str()
if not _is_trade_day(today):
    print(f"[panel_builder] {today} 非交易日，跳过")
    sys.exit(0)

from plays.limit_up.panel_builder import main
main()
