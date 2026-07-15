#!/usr/bin/env python3
"""待评分栈管理 — 生产者-消费者模式的评分队列。

每轮扫描后调用 update() 入栈/更新/淘汰，评分线程调用 pop_top() 取票。

排序逻辑：score = pct_chg * 0.7 + speed * 0.3
  - pct_chg: 当日涨跌幅 (%)
  - speed: 涨速 = 本轮 pct_chg - 上轮 pct_chg

持久化：to_json / from_json 供进程重启恢复。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

PLAY_DIR = Path(__file__).resolve().parent
QUEUE_DIR = PLAY_DIR / "data" / "queue"
QUEUE_DIR.mkdir(parents=True, exist_ok=True)

WEIGHT_PCT = 0.7  # 涨幅权重
WEIGHT_SPEED = 0.3  # 涨速权重


class ScoreStack:
    """待评分栈。

    items: dict[code, Item]
    prev_pct: dict[code, float] — 上轮涨跌幅，用于计算涨速
    """

    class Item:
        __slots__ = ("code", "name", "pct_chg", "speed", "score", "ts")

        def __init__(self, code: str, name: str = "", pct_chg: float = 0.0,
                     speed: float = 0.0, score: float = 0.0, ts: float = 0.0):
            self.code = code
            self.name = name
            self.pct_chg = pct_chg
            self.speed = speed
            self.score = score
            self.ts = ts

        def to_dict(self) -> dict:
            return {
                "code": self.code,
                "name": self.name,
                "pct_chg": round(self.pct_chg, 2),
                "speed": round(self.speed, 2),
                "score": round(self.score, 2),
                "ts": self.ts,
            }

        @classmethod
        def from_dict(cls, d: dict) -> "ScoreStack.Item":
            return cls(
                code=d["code"],
                name=d.get("name", ""),
                pct_chg=float(d.get("pct_chg", 0)),
                speed=float(d.get("speed", 0)),
                score=float(d.get("score", 0)),
                ts=float(d.get("ts", 0)),
            )

    def __init__(self):
        self.items: dict[str, "ScoreStack.Item"] = {}
        self.prev_pct: dict[str, float] = {}

    def update(self, quotes: dict[str, dict], name_map: dict[str, str] | None = None):
        """用 batch_quotes 结果更新栈。

        Args:
            quotes: {code: {pct_chg, name, ...}} 来自 THS get_batch_quotes
            name_map: {code: name} 候选池名称映射，用于 quote 中无 name 时回填。
        """
        now = time.time()
        name_map = name_map or {}
        seen_codes: set[str] = set()

        for code, q in quotes.items():
            pct_chg = float(q.get("pct_chg", 0) or 0)
            name = q.get("name", "") or q.get("f_name", "") or name_map.get(code, "")

            seen_codes.add(code)

            # 死票：涨幅<=0，踢出栈
            if pct_chg <= 0:
                self.items.pop(code, None)
                self.prev_pct.pop(code, None)
                continue

            # 涨速
            prev = self.prev_pct.get(code, pct_chg)
            speed = pct_chg - prev

            # 评分
            score = pct_chg * WEIGHT_PCT + speed * WEIGHT_SPEED

            if code in self.items:
                # 更新已有
                item = self.items[code]
                item.pct_chg = pct_chg
                item.speed = speed
                item.score = score
                item.ts = now
                if name:
                    item.name = name
            else:
                # 新入栈
                self.items[code] = self.Item(
                    code=code, name=name,
                    pct_chg=pct_chg, speed=speed,
                    score=score, ts=now,
                )

            self.prev_pct[code] = pct_chg

        # 本轮未返回的股票：淘汰 stale  prev_pct，避免跨多轮涨速失真
        for code in list(self.prev_pct.keys()):
            if code not in seen_codes and code not in self.items:
                self.prev_pct.pop(code, None)

    def pop_top(self, n: int = 20) -> list[Item]:
        """取评分最高的 N 只（不移除，仅返回副本）。"""
        sorted_items = sorted(
            self.items.values(),
            key=lambda x: x.score,
            reverse=True,
        )
        return sorted_items[:n]

    def top_n_codes(self, n: int = 20) -> list[str]:
        """取评分最高的 N 只的代码列表。"""
        return [item.code for item in self.pop_top(n)]

    @property
    def size(self) -> int:
        return len(self.items)

    def clear(self):
        """清空栈（新的一天）。"""
        self.items.clear()
        self.prev_pct.clear()

    def to_dict(self) -> dict:
        return {
            "items": [item.to_dict() for item in self.items.values()],
            "prev_pct": self.prev_pct,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ScoreStack":
        s = cls()
        for item_dict in d.get("items", []):
            item = cls.Item.from_dict(item_dict)
            s.items[item.code] = item
        s.prev_pct = {k: float(v) for k, v in d.get("prev_pct", {}).items()}
        return s


def save_queue(stack: ScoreStack, trade_date: str | None = None) -> Path:
    """持久化栈到 data/queue/queue.json。"""
    from datetime import datetime
    td = trade_date or datetime.now().strftime("%Y%m%d")
    path = QUEUE_DIR / f"queue_{td}.json"
    with open(path, "w") as f:
        json.dump(stack.to_dict(), f, ensure_ascii=False)
    return path


def load_queue(trade_date: str | None = None) -> ScoreStack | None:
    """从 data/queue/queue.json 恢复栈。"""
    from datetime import datetime
    td = trade_date or datetime.now().strftime("%Y%m%d")
    path = QUEUE_DIR / f"queue_{td}.json"
    if not path.exists():
        return None
    try:
        with open(path) as f:
            d = json.load(f)
        return ScoreStack.from_dict(d)
    except (json.JSONDecodeError, KeyError):
        return None


def clear_queue(trade_date: str | None = None):
    """删除当日队列文件。"""
    from datetime import datetime
    td = trade_date or datetime.now().strftime("%Y%m%d")
    path = QUEUE_DIR / f"queue_{td}.json"
    if path.exists():
        path.unlink()
