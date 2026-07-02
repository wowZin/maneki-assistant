#!/usr/bin/env python3
"""wiki compile — 每日收盘后将当日扫描数据编译为知识库页面

触发时机：收盘复盘后（18:00）
流程：
  1. 读取当日 data/analysis/ 最新评分数据
  2. 统计关键指标：扫描总数、涨停覆盖、各维度均分
  3. 写入 wiki/entities/ 当日汇总页面
  4. 更新 index.md 和 log.md

用法：
  python3 wiki/compile.py [--date 20260522]
"""

import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
ANALYSIS_DIR = PROJECT_DIR / "plays" / "limit_up" / "data" / "analysis"
WIKI_DIR = PROJECT_DIR / "wiki"
ENTITIES_DIR = WIKI_DIR / "plays" / "limit-up" / "entities"
INDEX_FILE = WIKI_DIR / "index.md"
LOG_FILE = WIKI_DIR / "log.md"

DIMS = ["fundamental", "technical", "fundflow", "sentiment", "shortterm"]
DIM_CN = {"fundamental": "基本面", "technical": "技术面", "fundflow": "资金面",
          "sentiment": "情绪面", "shortterm": "短线博弈"}
WEIGHTS = {"fundamental": 1.5, "technical": 1.0, "fundflow": 1.0, "sentiment": 1.2, "shortterm": 1.5}


def weighted_top3(scores, w):
    contribs = [(scores.get(d, 0) or 0, w.get(d, 1.0)) for d in DIMS]
    contribs.sort(key=lambda x: x[0] * x[1], reverse=True)
    top3 = contribs[:3]
    return sum(s * w for s, w in top3) / sum(w for _, w in top3) if sum(w for _, w in top3) > 0 else 0


def compile_day(trade_date: str):
    """编译某交易日的扫描数据"""
    # 读取当日分析文件
    records = []
    for f in sorted(ANALYSIS_DIR.glob(f"{trade_date}*.json")):
        with open(f) as fh:
            d = json.load(fh)
        if isinstance(d, list):
            records.extend(d)

    if not records:
        print(f"  {trade_date}: 无数据")
        return False

    # 去重
    seen = set()
    unique = []
    for r in records:
        code = r.get("code", "").split(".")[0]
        if code and code not in seen:
            seen.add(code)
            unique.append(r)
    records = unique

    # 统计
    n = len(records)
    scores_by_dim = defaultdict(list)
    total_scores = []
    stars_dist = {"⭐⭐⭐⭐⭐": 0, "⭐⭐⭐⭐": 0, "⭐⭐⭐": 0, "不评级": 0}

    for r in records:
        scores = r.get("scores", {})
        total = weighted_top3(scores, WEIGHTS)
        total_scores.append(total)

        for d in DIMS:
            s = scores.get(d, 0)
            if s is not None:
                scores_by_dim[d].append(s)

        if total >= 55:
            stars_dist["⭐⭐⭐⭐⭐"] += 1
        elif total >= 45:
            stars_dist["⭐⭐⭐⭐"] += 1
        elif total >= 35:
            stars_dist["⭐⭐⭐"] += 1
        else:
            stars_dist["不评级"] += 1

    # 维度均分
    dim_avgs = {}
    for d in DIMS:
        vals = scores_by_dim[d]
        dim_avgs[d] = round(sum(vals) / len(vals), 1) if vals else 0

    # Top 10 股票
    scored = [(r.get("code", "").split(".")[0], r.get("name", ""), weighted_top3(r.get("scores", {}), WEIGHTS))
              for r in records]
    scored.sort(key=lambda x: x[2], reverse=True)
    top10 = scored[:10]

    # 编译页面
    date_display = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}"
    dim_avg_line = "  ".join(f"{DIM_CN[d]}={dim_avgs[d]:.0f}" for d in DIMS)
    total_avg = round(sum(total_scores) / len(total_scores), 1) if total_scores else 0

    content = f"""---
title: {date_display} 扫描汇总
created: {datetime.now().strftime("%Y-%m-%d")}
updated: {datetime.now().strftime("%Y-%m-%d")}
type: summary
tags: [daily, scan]
---

# {date_display} 扫描汇总

## 基础数据

| 指标 | 值 |
|------|:---:|
| 扫描股票总数 | {n} 只 |
| 总分均值 | {total_avg} |
| 维度均分 | {dim_avg_line} |

## 星级分布

| 星级 | 数量 | 占比 |
|------|:---:|:----:|
| ⭐⭐⭐⭐⭐ (≥55) | {stars_dist["⭐⭐⭐⭐⭐"]} | {round(stars_dist["⭐⭐⭐⭐⭐"]/n*100,1) if n else 0}% |
| ⭐⭐⭐⭐ (≥45) | {stars_dist["⭐⭐⭐⭐"]} | {round(stars_dist["⭐⭐⭐⭐"]/n*100,1) if n else 0}% |
| ⭐⭐⭐ (≥35) | {stars_dist["⭐⭐⭐"]} | {round(stars_dist["⭐⭐⭐"]/n*100,1) if n else 0}% |
| 不评级 (<35) | {stars_dist["不评级"]} | {round(stars_dist["不评级"]/n*100,1) if n else 0}% |

## Top 10 股票

| 排名 | 代码 | 名称 | 总分 |
|:---:|:----:|:----:|:----:|
""" + "\n".join(f"| {i+1} | {c} | {n} | {s:.1f} |" for i, (c, n, s) in enumerate(top10))

    # 推送与命中分析
    push_section = _compile_push_analysis(trade_date)
    if push_section:
        content += "\n\n" + push_section

    # 权重优化结果
    weight_section = _compile_weight_analysis(trade_date)
    if weight_section:
        content += "\n\n" + weight_section

    # 扫描信号与报告
    signal_section = _compile_signal_analysis(trade_date)
    if signal_section:
        content += "\n\n" + signal_section

    # 同步原始数据到 raw/
    _relocate_raw_data(trade_date)

    # 写入文件
    page_name = f"{trade_date}-扫描汇总.md"
    page_path = ENTITIES_DIR / page_name
    page_path.write_text(content, encoding="utf-8")
    print(f"  ✅ {page_name} — {n}只股票, 总分均值{total_avg}")

    # 更新 index.md
    update_index(trade_date, date_display, page_name, n)

    # 更新 log.md
    update_log(trade_date, page_name, n, total_avg)

    return True


def _relocate_raw_data(trade_date: str, play: str = "limit_up",
                        play_slug: str = "limit-up"):
    """搬迁玩法当日原始数据到 wiki/raw/<play_slug>/，删除玩法侧原文件。

    - 语义：move（copy + delete original）
    - 幂等：目标已存在则先 unlink 再 move
    - 并发保护：如玩法 data/pipeline.lock 存在（有活跃 pipeline），跳过等待下轮
    - 覆盖：signals / analysis / reports / pushed / weights 全量同步
    """
    import shutil

    play_data = PROJECT_DIR / "plays" / play / "data"
    if (play_data / "pipeline.lock").exists():
        print(f"  [wiki relocate] {play} pipeline 运行中，跳过搬迁")
        return

    raw_play_root = WIKI_DIR / "raw" / play_slug

    KINDS = ("signals", "analysis", "reports", "pushed", "weights")
    total_moved = 0
    for kind in KINDS:
        src_dir = play_data / kind
        if not src_dir.exists():
            continue
        dst_dir = raw_play_root / kind
        dst_dir.mkdir(parents=True, exist_ok=True)

        # glob 支持 json 与 md 后缀（reports 目录有 .md 文件）
        for f in sorted(list(src_dir.glob(f"{trade_date}*.json"))
                        + list(src_dir.glob(f"{trade_date}*.md"))):
            dst = dst_dir / f.name
            if dst.exists():
                dst.unlink()
            shutil.move(str(f), str(dst))
            total_moved += 1

    if total_moved:
        print(f"  [wiki relocate] {play_slug}: 搬迁 {total_moved} 个文件到 wiki/raw/{play_slug}/")


# 向后兼容别名（用旧名调用会 fallback 到新语义）
_sync_raw_data = _relocate_raw_data


def _compile_signal_analysis(trade_date: str) -> str:
    """编译扫描信号与报告数据"""
    SIGNALS_DIR = PROJECT_DIR / "plays" / "limit_up" / "data" / "signals"
    REPORTS_DIR = PROJECT_DIR / "plays" / "limit_up" / "data" / "reports"

    # 统计信号文件
    signal_files = sorted(SIGNALS_DIR.glob(f"{trade_date}*.json"))
    if not signal_files:
        return ""

    total_signals = 0
    for f in signal_files:
        try:
            with open(f) as fh:
                d = json.load(fh)
            stocks = d.get("stocks", []) if isinstance(d, dict) else (d if isinstance(d, list) else [])
            total_signals += len(stocks)
        except Exception:
            pass

    # 检查报告文件
    report_md = REPORTS_DIR / f"{trade_date}.md"
    report_json = REPORTS_DIR / f"{trade_date}.json"
    has_report = report_md.exists() or report_json.exists()

    section = f"""## 扫描信号与报告

| 指标 | 值 |
|------|:---:|
| 全天扫描次数 | {len(signal_files)} 次 |
| 累计信号量 | {total_signals} 条 |
| 复盘报告 | {'✅ 已生成' if has_report else '待生成'} |
"""
    return section


def _compile_weight_analysis(trade_date: str) -> str:
    """编译权重优化结果"""
    weight_file = PROJECT_DIR / "plays" / "limit_up" / "data" / "weights" / "ranking_optimized.json"
    if not weight_file.exists():
        return ""

    try:
        with open(weight_file) as f:
            data = json.load(f)
        base = data.get("baseline", {})
        data.get("recommended", {})

        base_w = "  ".join(f"{DIM_CN[d]}={base['weights'][d]:.1f}" for d in DIMS) if base.get("weights") else "无"
        base_push = f"信号池{base.get('pushed_count',0)}只(含涨停{base.get('push_limit_count',0)}只) 实战命中率{base.get('top3_hit_rate',0)}%"

        section = f"""## 权重优化状态

| 指标 | 值 |
|------|:---:|
| 当前权重 | {base_w} |
| 信号池 | {base_push} |
| 综合分 | {base.get('composite', '-')} |
| AUC | {base.get('auc', '-')} |
"""
        return section
    except Exception:
        return ""


def _compile_push_analysis(trade_date: str) -> str:
    """编译推送记录与实际涨停命中分析"""
    import requests

    PUSHED_DIR = PROJECT_DIR / "plays" / "limit_up" / "data" / "pushed"

    # 读取当日所有推送记录
    pushed_files = sorted(PUSHED_DIR.glob(f"{trade_date}*.json"))
    if not pushed_files:
        return ""

    all_pushed = []
    seen = set()
    for f in pushed_files:
        try:
            with open(f) as fh:
                d = json.load(fh)
            for item in (d if isinstance(d, list) else []):
                code = item.get("code", "").split(".")[0]
                if code and code not in seen:
                    seen.add(code)
                    all_pushed.append({
                        "code": code,
                        "name": item.get("name", ""),
                        "total": item.get("total", 0),
                        "time": f.stem.split("_")[1][:2] + ":" + f.stem.split("_")[1][2:4],
                    })
        except Exception:
            pass

    if not all_pushed:
        return ""

    # 获取实际涨停数据
    limit_codes = set()
    try:
        token = ""
        env_file = PROJECT_DIR / ".env"
        if env_file.exists():
            for line in env_file.read_text().split("\n"):
                if line.startswith("TUSHARE_TOKEN="):
                    token = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
        if token:
            payload = {
                "api_name": "limit_list_d",
                "token": token,
                "params": {"trade_date": trade_date, "limit_type": "U"},
            }
            resp = requests.post("http://api.tushare.pro", json=payload, timeout=15)
            data = resp.json()
            if data.get("code") == 0:
                fields = data["data"]["fields"]
                idx = {f: i for i, f in enumerate(fields)}
                for item in data["data"]["items"]:
                    code = item[idx.get("ts_code", 0)].split(".")[0]
                    if not code.startswith(("300", "301", "688", "8")):
                        limit_codes.add(code)
    except Exception as e:
        print(f"  ⚠️ 拉取涨停数据失败: {e}")

    # 标记命中
    hits = 0
    for p in all_pushed:
        p["hit"] = p["code"] in limit_codes
        if p["hit"]:
            hits += 1

    total = len(all_pushed)
    hit_rate = round(hits / total * 100, 1) if total > 0 else 0

    # 构建markdown
    hit_rows = "\n".join(
        f"| {p['time']} | {p['code']} | {p['name']} | {p['total']:.1f} | {'✅' if p['hit'] else '❌'} |"
        for p in all_pushed
    )

    section = f"""## 推送与命中分析

| 指标 | 值 |
|------|:---:|
| 推送总次数 | {len(pushed_files)} 次 |
| 去重推送股票 | {total} 只 |
| 实际涨停 | {hits} 只 |
| 推送命中率 | {hit_rate}% |

### 推送明细

| 时间 | 代码 | 名称 | 总分 | 涨停 |
|:---:|:----:|:----:|:----:|:---:|
{hit_rows}"""
    return section


def update_index(trade_date: str, date_display: str, page_name: str, count: int):
    """在 index.md 的 Entities 节追加当日记录"""
    if not INDEX_FILE.exists():
        return

    content = INDEX_FILE.read_text(encoding="utf-8")

    # 检查是否已存在当天的记录
    if f"[[{page_name}]]" in content:
        return  # 已存在，跳过

    # 在 Entities 节追加
    marker = "## Entities"
    new_entry = f"- [[{page_name}]] — {date_display} 扫描 {count} 只股票"
    if marker in content:
        # 找到 Entities 节末尾
        lines = content.split("\n")
        new_lines = []
        in_entities = False
        inserted = False
        for line in lines:
            new_lines.append(line)
            if line.startswith("## Entities"):
                in_entities = True
            elif in_entities and line.startswith("## "):
                if not inserted:
                    new_lines.append(new_entry)
                    inserted = True
                in_entities = False
        if not inserted:
            new_lines.append(new_entry)
        content = "\n".join(new_lines)

    # 更新页数统计
    import re
    total_match = re.search(r"Total pages: (\d+)", content)
    if total_match:
        old_total = int(total_match.group(1))
        content = content.replace(f"Total pages: {old_total}", f"Total pages: {old_total + 1}")

    INDEX_FILE.write_text(content, encoding="utf-8")


def update_log(trade_date: str, page_name: str, count: int, avg_score: float):
    """追加 log.md"""
    entry = f"\n## [{datetime.now().strftime('%Y-%m-%d')}] compile | {trade_date} 扫描汇总\n- Created: entities/{page_name} — {count} 只股票, 总分均值 {avg_score}\n"
    with open(LOG_FILE, "a") as f:
        f.write(entry)


def compile_watchdog(trade_date: str):
    """编译盯盘状态到 wiki"""
    STATE_FILE = PROJECT_DIR / "plays" / "watchdog" / "data" / "state.json"
    if not STATE_FILE.exists():
        return

    with open(STATE_FILE) as f:
        state = json.load(f)
    if not state:
        return

    # 获取股票名称
    names = {}
    try:
        import os
        import tushare as ts
        from dotenv import load_dotenv
        load_dotenv(PROJECT_DIR / ".env")
        ts.set_token(os.getenv("TUSHARE_TOKEN", ""))
        pro = ts.pro_api()
        for code in state.keys():
            df = pro.stock_basic(ts_code=code, fields="name")
            if not df.empty:
                names[code] = df.iloc[0]["name"]
    except Exception:
        pass

    date_display = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}"
    status_icons = {"watching": "👁 监控中", "signal_pending": "⏳ 信号待确认", "entered": "📈 已入场"}

    rows = []
    for code, st in state.items():
        name = names.get(code, code.split(".")[0])
        status_text = status_icons.get(st.get("status", ""), st.get("status", "?"))
        added = st.get("added_at", "")[:10]
        entry = f"入场价{st['entry_price']:.2f}" if st.get("entry_price") else "—"
        rows.append(f"| {code} | {name} | {added} | {status_text} | {entry} |")

    content = f"""---
title: {date_display} 盯盘状态
created: {datetime.now().strftime("%Y-%m-%d")}
updated: {datetime.now().strftime("%Y-%m-%d")}
type: watchdog
tags: [daily, watchdog]
---

# {date_display} 盯盘状态

## 当前盯盘 ({len(state)}/5)

| 代码 | 名称 | 开始日期 | 状态 | 入场价 |
|------|------|:---:|------|:---:|
{chr(10).join(rows)}

## 盯盘限制

- 上限: 5只
- 新盯盘前需确认是否有空位，满员时让用户选择释放哪只
"""

    # 写入
    watchdog_dir = WIKI_DIR / "plays" / "watchdog" / "entities"
    watchdog_dir.mkdir(parents=True, exist_ok=True)
    page_name = f"{trade_date}-盯盘状态.md"
    (watchdog_dir / page_name).write_text(content, encoding="utf-8")
    print(f"  ✅ {page_name} — {len(state)}只盯盘")


def main():
    # 默认取最近交易日
    target_date = ""
    for arg in sys.argv[1:]:
        if arg.startswith("--date="):
            target_date = arg.split("=")[1]

    if not target_date:
        target_date = datetime.now().strftime("%Y%m%d")

    print(f"📊 wiki compile — {target_date}")

    # limit_up 扫描汇总
    if ANALYSIS_DIR.exists():
        compile_day(target_date)
    else:
        print("  limit_up 分析数据目录不存在，跳过")

    # watchdog 盯盘状态
    compile_watchdog(target_date)

    print("✅ 编译完成")


if __name__ == "__main__":
    main()
