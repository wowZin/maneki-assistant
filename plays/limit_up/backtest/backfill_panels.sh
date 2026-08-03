#!/usr/bin/env bash
# 批量构建历史评分日面板（_PANEL_DATE 机制，数据走缓存不拉 Tushare）
# 用法: bash backfill_panels.sh [起始日] [结束日]
set -u
START=${1:-20260702}
END=${2:-20260714}
cd /root/maneki-agent || exit 1

for d in $(seq 1 31); do :; done  # noop
# 生成交易日序列：跳过周末（粗略，用 python 精确判定）
DATES=$(python3 - <<PY
import sys
sys.path.insert(0, '.')
from plays.limit_up.backtest.dataset import _trade_dates
ds = _trade_dates("$START", "$END")
print(" ".join(ds))
PY
)

for d in $DATES; do
  if [ -f "wiki/raw/limit-up/panel/${d}.parquet" ]; then
    echo "[skip] $d 面板已存在"
    continue
  fi
  echo "===== build $d ====="
  _PANEL_DATE="$d" python3 plays/limit_up/panel_builder.py 2>&1 | grep -E "已保存|构建完成|策略分|ERROR|Traceback|error" | head -5
  if [ $? -ne 0 ]; then
    echo "[FAIL] $d"
  fi
done
echo "===== 批量构建完成 ====="
