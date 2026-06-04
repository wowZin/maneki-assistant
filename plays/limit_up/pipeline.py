#!/usr/bin/env python3
"""
涨停预测完整流程脚本
流程：异动扫描(东财API+代理) → 五维度评分 → 排序 → 飞书推送

用法:
  python plays/limit_up/pipeline.py                  # 完整流程(requests+代理)
  python plays/limit_up/pipeline.py --from-file=data/signals/xxx.json  # 从已有文件读取
"""

import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
import argparse
import requests

# 项目根目录
PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
PLAY_DIR = Path(__file__).resolve().parent
DATA_DIR = PLAY_DIR / "data"
sys.path.insert(0, str(PROJECT_DIR))

from scripts.tu_share import CONFIG, clear_tushare_cache  # noqa: E402
from plays.limit_up.utils import is_trading_time  # noqa: E402

# ===== Feishu 测试模式 =====
FEISHU_TEST_MODE = CONFIG.get("FEISHU_TEST_MODE", "").lower() == "true"
def feishu_title_prefix():
    return "测试-" if FEISHU_TEST_MODE else ""

# ===== Agent 权重配置 =====
AGENT_WEIGHTS = {
    "fundamental": float(CONFIG.get("AGENT_WEIGHT_FUNDAMENTAL", "1.5")),
    "technical": float(CONFIG.get("AGENT_WEIGHT_TECHNICAL", "1.0")),
    "fundflow": float(CONFIG.get("AGENT_WEIGHT_FUND_FLOW", "1.0")),
    "sentiment": float(CONFIG.get("AGENT_WEIGHT_SENTIMENT", "1.2")),
    "shortterm": float(CONFIG.get("AGENT_WEIGHT_SHORTTERM", "1.5")),
}

# ===== 1. 扫描异动股 =====
def scan_surge():
    """通过东方财富clist API获取异动候选股（requests+代理，涨速+涨幅双路合并）
    
    数据源: push2.eastmoney.com/api/qt/clist/get
    双路: ①涨速降序(f11) ②涨幅降序(f3) → 合并去重
    Returns: list[dict] - [{code, name}] 候选股列表，或None
    """
    from scripts.proxy_utils import request_with_proxy_retry

    if not is_trading_time():
        print(f"跳过扫描: 非交易时段 ({datetime.now().strftime('%H:%M')})")
        return None

    base_url = (
        "https://push2.eastmoney.com/api/qt/clist/get?"
        "np=1&fltt=2&invt=2&"
        "fs=m:0+t:6+f:!2,m:0+t:80+f:!2,m:1+t:2+f:!2,m:1+t:23+f:!2,m:0+t:81+s:262144+f:!2&"
        "fields=f12,f14,f2,f3,f11&pn=1&pz=200&po=1&dect=1&"
        "ut=fa5fd1943c7b386f172d6893dbfba10b"
    )

    def _fetch(fid):
        """单次API请求，返回过滤后的候选股列表"""
        url = f"{base_url}&fid={fid}"
        resp = request_with_proxy_retry(url, timeout=15)
        if resp is None:
            return []
        try:
            data = resp.json()
        except Exception:
            return []
        items = data.get("data", {}).get("diff", [])
        if not items:
            return []
        candidates = []
        for s in items:
            code = s.get("f12", "")
            name = s.get("f14", "")
            pct = s.get("f3")
            try:
                pct = float(pct) if pct and pct != "-" else 0
            except Exception:
                pct = 0
            # 过滤: ST/新股/创业板/科创板，涨幅2%-9.5%
            if re.search(r"ST|\*ST|退|N", name or ""):
                continue
            if re.match(r"^(300|301|688|8|4|920)", code):
                continue
            if pct < 2 or pct > 9.5:
                continue
            if "." not in code:
                code = f"{code}.SH" if code.startswith("6") else f"{code}.SZ"
            candidates.append({"code": code, "name": name, "pct_chg": pct})
        return candidates
    
    for attempt in range(3):
        try:
            # 双路扫描: 涨速+涨幅
            surge_candidates = _fetch("f11")
            pct_candidates = _fetch("f3")
            
            # 合并去重
            seen = set()
            merged = []
            for c in surge_candidates + pct_candidates:
                if c["code"] not in seen:
                    seen.add(c["code"])
                    merged.append(c)
            
            if not merged:
                print(f"  双路扫描均返回空(尝试{attempt+1}/3)")
                if attempt < 2:
                    time.sleep(2)
                    continue
                return None
            
            print(f"扫描完成(涨速{len(surge_candidates)}+涨幅{len(pct_candidates)}→合并{len(merged)}只)")
            return merged
            
        except Exception as e:
            print(f"  扫描失败(尝试{attempt+1}/3): {e}")
            if attempt < 2:
                time.sleep(2)
    
    return None

def load_from_file(filepath):
    """从已有文件加载信号"""
    path = Path(filepath)
    if not path.is_absolute():
        path = PROJECT_DIR / filepath
    
    with open(path) as f:
        raw = json.load(f)
    
    # 兼容两种格式：直接list 或 dict包含stocks
    if isinstance(raw, dict) and "stocks" in raw:
        stocks = raw["stocks"]
    elif isinstance(raw, list):
        stocks = raw
    else:
        print(f"无法解析信号文件格式: {type(raw)}")
        return None
    
    # 统一转换为标准格式 {code, name}
    candidates = []
    for s in stocks:
        code = s.get("代码") or s.get("code") or s.get("ts_code", "")
        name = s.get("名称") or s.get("name", "")
        # 补全ts_code格式 (002971 -> 002971.SZ, 603615 -> 603615.SH)
        if "." not in code:
            if code.startswith("6"):
                code = f"{code}.SH"
            else:
                code = f"{code}.SZ"
        candidates.append({"code": code, "name": name})
    
    print(f"从文件加载: {len(candidates)} 只候选股")
    return candidates

from plays.limit_up.filter import filter_candidates  # noqa: E402

# ===== 2. 基本面评分 =====
# Lazy import to avoid circular dependency
def score_fundamental(code):
    from plays.limit_up.strategies.fundamental import score_fundamental as _score_fundamental
    return _score_fundamental(code)

# ===== 3. 技术面评分 =====
def score_technical(code):
    from plays.limit_up.strategies.technical import score_technical as _score_technical
    return _score_technical(code)


# ===== 4. 资金面评分 =====
# 缓存当日资金流向数据（避免每次调用都重复请求）
_FUND_FLOW_CACHE = None
_FUND_FLOW_DATE = None

def score_fundflow(code):
    from plays.limit_up.strategies.fundflow import score_fundflow as _score_fundflow
    return _score_fundflow(code)

# 实时涨幅缓存（CDP/requests+代理获取，避免盘中Tushare无数据）
_REALTIME_PCT_CACHE = {}
_REALTIME_PCT_TS = ""
_POPULARITY_RANK_CACHE = {}  # {code: rank} 东方财富人气排名，取前300


def _batch_fetch_realtime_pct():
    """批量获取全市场实时涨跌幅，缓存到全局变量"""
    import requests as _req
    global _REALTIME_PCT_CACHE, _REALTIME_PCT_TS
    from datetime import datetime as _dt
    today = _dt.now().strftime("%Y%m%d")
    if _REALTIME_PCT_TS == today and _REALTIME_PCT_CACHE:
        return _REALTIME_PCT_CACHE

    # 盘后降级：从 Tushare daily 获取
    from plays.limit_up.utils import is_market_closed, batch_get_pct_tushare
    if is_market_closed():
        cache = batch_get_pct_tushare(today)
        if cache:
            _REALTIME_PCT_CACHE = cache
            _REALTIME_PCT_TS = today
            print(f"  [盘后] 实时涨幅降级Tushare: {len(cache)} 只股票")
            return cache
        return {}

    try:
        from scripts.proxy_utils import request_with_proxy_retry
        cache = {}
        # 逐页获取（每页最多100只，翻页至获取5000+）
        for page in range(1, 6):  # 最多5页，覆盖500只活跃股
            url = (
                "https://push2.eastmoney.com/api/qt/clist/get?"
                "np=1&fltt=2&invt=2&"
                "fs=m:0+t:6+f:!2,m:0+t:80+f:!2,m:1+t:2+f:!2,m:1+t:23+f:!2,m:0+t:81+s:262144+f:!2&"
                f"fields=f12,f3&fid=f3&pn={page}&pz=100&po=1&dect=1&"
                "ut=fa5fd1943c7b386f172d6893dbfba10b"
            )
            resp = request_with_proxy_retry(url, timeout=15)
            if resp is None:
                if page == 1:
                    break
                continue
            try:
                items = resp.json().get("data", {}).get("diff", [])
            except Exception:
                continue
            if not items:
                break
            for item in items:
                code = str(item.get("f12", ""))
                pct = item.get("f3")
                if code and pct is not None:
                    cache[code] = pct
            if len(items) < 100:
                break  # 最后一页

        if cache:
            _REALTIME_PCT_CACHE = cache
            _REALTIME_PCT_TS = today
            print(f"  实时涨幅缓存: {len(cache)} 只股票")
            return cache
    except Exception as e:
        print(f"  实时涨幅获取失败: {e}")
    return {}


def _get_popularity_rank(code: str) -> int | None:
    """获取个股东方财富人气排名（f62关注度降序，取前300名）
    
    返回排名(1-based)或None(获取失败/不在前300)
    """
    global _POPULARITY_RANK_CACHE, _REALTIME_PCT_TS
    from datetime import datetime as _dt
    today = _dt.now().strftime("%Y%m%d")
    if _REALTIME_PCT_TS != today or not _POPULARITY_RANK_CACHE:
        # 盘后降级：Tushare moneyflow 按主力净流入排序
        from plays.limit_up.utils import is_market_closed, get_popularity_rank_tushare
        if is_market_closed():
            cache = get_popularity_rank_tushare(today)
            if cache:
                _POPULARITY_RANK_CACHE = cache
                _REALTIME_PCT_TS = today
                print(f"  [盘后] 人气排名降级Tushare: {len(cache)} 只")
                code_short = code.split('.')[0]
                return _POPULARITY_RANK_CACHE.get(code_short)
            return None

        try:
            from scripts.proxy_utils import request_with_proxy_retry
            cache = {}
            for pg in range(1, 3):
                url = (
                    "https://push2.eastmoney.com/api/qt/clist/get?"
                    "np=1&fltt=2&invt=2&"
                    "fs=m:0+t:6+f:!2,m:0+t:80+f:!2,m:1+t:2+f:!2,m:1+t:23+f:!2&"
                    f"fields=f12,f62&fid=f62&pn={pg}&pz=100&po=1&"
                    "ut=fa5fd1943c7b386f172d6893dbfba10b"
                )
                r2 = request_with_proxy_retry(url, timeout=15)
                if r2 is None:
                    if pg == 1:
                        break
                    continue
                try:
                    items2 = r2.json().get("data", {}).get("diff", [])
                except Exception:
                    continue
                if not items2:
                    break
                for rank, item in enumerate(items2, 1 + (pg - 1) * 100):
                    c = str(item.get("f12", ""))
                    if c:
                        cache[c] = rank
                if len(items2) < 100:
                    break
            if cache:
                _POPULARITY_RANK_CACHE = cache
                _REALTIME_PCT_TS = today
                print(f"  人气排名缓存: {len(cache)} 只 (前{len(cache)})")
        except Exception as e:
            print(f"  人气排名获取失败: {e}")
            return None
    
    code_short = code.split('.')[0]
    return _POPULARITY_RANK_CACHE.get(code_short)

# 实时资金流缓存（东财API+代理，替代T+1 Tushare moneyflow）
_REALTIME_FUND_CACHE = {}  # code_short → {net_flow, vol_ratio, turnover, amount}
_REALTIME_FUND_TS = ""

def _get_realtime_fund_cache():
    """获取全市场实时资金流数据（带缓存，每轮pipeline只调一次）
    返回: {code_short: {net_flow(元), vol_ratio, turnover(%), amount(元)}}
    """
    global _REALTIME_FUND_CACHE, _REALTIME_FUND_TS
    today = datetime.now().strftime("%Y%m%d")
    if _REALTIME_FUND_CACHE and _REALTIME_FUND_TS == today:
        return _REALTIME_FUND_CACHE

    # 盘后降级：Tushare moneyflow + daily_basic
    from plays.limit_up.utils import is_market_closed, batch_get_fundflow_tushare
    if is_market_closed():
        cache = batch_get_fundflow_tushare(today)
        if cache:
            _REALTIME_FUND_CACHE = cache
            _REALTIME_FUND_TS = today
            print(f"  [盘后] 资金流降级Tushare: {len(cache)} 只股票")
            return cache
        return {}

    from scripts.proxy_utils import request_with_proxy_retry
    cache = {}
    for page in range(1, 6):  # 翻5页×100=500只
        url = (
            "https://push2.eastmoney.com/api/qt/clist/get?"
            "np=1&fltt=2&invt=2&"
            "fs=m:0+t:6+f:!2,m:0+t:80+f:!2,m:1+t:2+f:!2,m:1+t:23+f:!2&"
            f"fields=f12,f62,f10,f7,f6&fid=f62&pn={page}&pz=100&po=1&dect=1&"
            "ut=fa5fd1943c7b386f172d6893dbfba10b"
        )
        resp = request_with_proxy_retry(url, timeout=15)
        if resp is None:
            print(f"  实时资金流缓存第{page}页失败(已重试)")
            if page == 1:
                break  # 第1页失败则放弃
            continue
        try:
            items = resp.json().get("data", {}).get("diff", [])
        except Exception:
            continue
        if not items:
            break
        for s in items:
            code = s.get("f12", "")
            if not code:
                continue
            f62 = s.get("f62")
            try:
                net_flow = float(f62) if f62 and f62 != "-" else 0
            except Exception:
                net_flow = 0
            try:
                vol_ratio = float(s.get("f10", 0)) if s.get("f10") and s.get("f10") != "-" else 0
            except Exception:
                vol_ratio = 0
            try:
                turnover = float(s.get("f7", 0)) if s.get("f7") and s.get("f7") != "-" else 0
            except Exception:
                turnover = 0
            try:
                amount = float(s.get("f6", 0)) if s.get("f6") and s.get("f6") != "-" else 0
            except Exception:
                amount = 0
            cache[code] = {
                "net_flow": net_flow,      # 主力净流入(元)
                "vol_ratio": vol_ratio,     # 量比
                "turnover": turnover,       # 换手率(%)
                "amount": amount,           # 成交额(元)
            }
        if len(items) < 100:
            break
    
    if cache:
        _REALTIME_FUND_CACHE = cache
        _REALTIME_FUND_TS = today
        print(f"  实时资金流缓存: {len(cache)} 只")
    return cache


def score_sentiment(code):
    from plays.limit_up.strategies.sentiment import score_sentiment as _score_sentiment
    return _score_sentiment(code)


# ===== 6. 飞书推送 =====
def _get_feishu_token():
    """获取飞书 tenant_access_token"""
    import requests
    resp = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={
            "app_id": CONFIG["FEISHU_APP_ID"],
            "app_secret": CONFIG["FEISHU_APP_SECRET"]
        }
    )
    data = resp.json()
    return data.get("tenant_access_token")


def push_feishu(results):
    """发送飞书卡片

    推送规则：
    - 综合评级>=30的股票按涨幅+情绪排序取前5只推送
    - 排序键: pct_chg + sentiment/100 (涨幅优先，情绪打破平局)
    - 如果没有>=30的股票，不推送
    - 推送记录保存到 data/pushed/ 目录，供复盘使用
    """
    import requests

    def _stars(total):
        """综合评级: >=55 ⭐⭐⭐⭐⭐  >=45 ⭐⭐⭐⭐  >=35 ⭐⭐⭐  >=30 ⭐⭐"""
        if total >= 55: return "⭐ ⭐ ⭐ ⭐ ⭐"  # noqa: E701
        if total >= 45: return "⭐ ⭐ ⭐ ⭐"  # noqa: E701
        if total >= 35: return "⭐ ⭐ ⭐"  # noqa: E701
        if total >= 30: return "⭐ ⭐"  # noqa: E701
        return ""

    # 推送筛选 (Top5，阈值30，按涨幅+情绪排序)
    THRESHOLD = 30

    # 同日去重：已推送过的股票不再重复推送
    pushed_codes_today = set()
    pushed_dir = DATA_DIR / "pushed"
    today_prefix = datetime.now().strftime("%Y%m%d")
    if pushed_dir.exists():
        for pf in pushed_dir.glob(f"{today_prefix}_*.json"):
            try:
                for item in json.loads(pf.read_text()):
                    if isinstance(item, dict) and "code" in item:
                        pushed_codes_today.add(item["code"])
            except Exception:
                pass

    above_threshold = sorted(
        [r for r in results if r.get('total', 0) >= THRESHOLD and r.get('code') not in pushed_codes_today],
        key=lambda x: x.get('pct_chg', 0) + x.get('scores', {}).get('sentiment', 0) / 100,
        reverse=True
    )[:5]
    if above_threshold:
        push_list = above_threshold
        print(f"  推送池: {len(above_threshold)}只(综合评级>={THRESHOLD}, 按涨幅+情绪Top5{' 已去重' if pushed_codes_today else ''})")
    else:
        push_list = []
        print(f"  无>={THRESHOLD}评级股票，不推送 (已推{pushed_codes_today}跳过)")

    if not push_list:
        print("  无可推送股票")
        return False

    # 保存推送记录（供复盘使用）
    pushed_dir = DATA_DIR / "pushed"
    pushed_dir.mkdir(parents=True, exist_ok=True)
    pushed_file = pushed_dir / f"{datetime.now().strftime('%Y%m%d_%H%M')}.json"
    with open(pushed_file, "w") as f:
        json.dump(push_list, f, ensure_ascii=False, indent=2)
    print(f"  推送记录已保存: {pushed_file}")

    token = _get_feishu_token()

    if not token:
        print("飞书token获取失败")
        return False

    # 构建卡片
    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"{feishu_title_prefix()}涨停预测 Top5 ({datetime.now().strftime('%H:%M')})"},
            "template": "blue"
        },
        "elements": []
    }

    for r in push_list:
        s = r.get('scores', {})
        stars = _stars(r['total'])
        pct = r.get('pct_chg', 0)
        element = {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"**{r['code']} {r['name']}** {stars} 涨幅{pct:.1f}%\n"
                          f"综合评分:{r['total']:.1f}  情绪面:{s.get('sentiment',0):.0f} 资金面:{s.get('fundflow',0):.0f} 基本面:{s.get('fundamental',0):.0f}"
            }
        }
        card["elements"].append(element)

    # 发送
    resp = requests.post(
        "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "receive_id": CONFIG["FEISHU_CHAT_ID_SIGNAL"],
            "msg_type": "interactive",
            "content": json.dumps(card)
        }
    )

    result = resp.json()
    if result.get("code") == 0:
        print(f"飞书推送成功: {result['data']['message_id']}")
        return True
    else:
        print(f"飞书推送失败: {result}")
        return False


# ===== 主流程 =====

def _write_empty_result(reason=""):
    """写入零结果分析文件（兜底：避免扫空静默失败）"""
    now = datetime.now()
    ts = now.strftime("%Y%m%d_%H%M")
    output_path = DATA_DIR / "analysis" / f"{ts}.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    empty = [{"_empty": True, "reason": reason, "time": now.isoformat()}]
    with open(output_path, "w") as f:
        json.dump(empty, f, ensure_ascii=False)
    print(f"零结果已记录: {output_path}")


def _score_one(stock, l2api, weights, scored_cache, cache_hit):
    """单只股票五维评分"""
    code = stock["code"]
    if cache_hit:
        cached = scored_cache[code]
        scores = {dim: v[0] for dim, v in cached.items()}
        reasons = {dim: v[1] for dim, v in cached.items()}
    else:
        scores = {}
        reasons = {}

    from concurrent.futures import ThreadPoolExecutor, as_completed
    from plays.limit_up.strategies.shortterm import score_shortterm
    funcs = {"fundamental": score_fundamental, "technical": score_technical,
             "fundflow": score_fundflow, "sentiment": score_sentiment, "shortterm": score_shortterm}
    to_run = {dim: fn for dim, fn in funcs.items() if dim not in scores}
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(fn, code): dim for dim, fn in to_run.items()}
        for future in as_completed(futures):
            dim = futures[future]
            try:
                s, r = future.result()
                scores[dim] = s
                reasons[dim] = r
            except Exception as e:
                scores[dim] = 0
                reasons[dim] = f"评分异常: {e}"

    f_sc, t_sc = scores.get("fundamental", 0), scores.get("technical", 0)
    m_sc, s_sc = scores.get("fundflow", 0), scores.get("sentiment", 0)
    st_sc = scores.get("shortterm", 0)

    dc = [(f_sc, weights.get("fundamental", 1.0)), (t_sc, weights.get("technical", 1.0)),
          (m_sc, weights.get("fundflow", 1.0)), (s_sc, weights.get("sentiment", 1.0)),
          (st_sc, weights.get("shortterm", 1.5))]
    dc.sort(key=lambda x: x[0] * x[1], reverse=True)
    top3 = dc[:3]
    total = sum(s*w for s, w in top3) / sum(w for _, w in top3) if sum(w for _, w in top3) > 0 else 0
    rc = sum([f_sc >= 75, t_sc >= 75, m_sc >= 75, s_sc >= 75, st_sc >= 75])

    l2data = None
    if l2api:
        try:
            from scripts.l2_client import to_price
            mkt = l2api.get_market(code)
            vwap = l2api.get_vwap(code)
            kb = l2api.get_minute_kline(code, n=5)
            if mkt:
                l2data = {"last": to_price(mkt.get("last", "0")),
                          "bid1_p": to_price(mkt.get("bid_price", [""])[0]) if mkt.get("bid_price") else 0,
                          "ask1_p": to_price(mkt.get("ask_price", [""])[0]) if mkt.get("ask_price") else 0,
                          "vwap": round(vwap, 2) if vwap else None, "kline_bars": len(kb)}
        except Exception:
            pass

    return {"code": code, "name": stock["name"],
            "scores": {"fundamental": f_sc, "technical": t_sc, "fundflow": m_sc,
                       "sentiment": s_sc, "shortterm": st_sc},
            "reasons": {"fundamental": reasons.get("fundamental", ""), "technical": reasons.get("technical", ""),
                        "fundflow": reasons.get("fundflow", ""), "sentiment": reasons.get("sentiment", ""),
                        "shortterm": reasons.get("shortterm", "")},
            "weights": {k: f"{v:.1f}" for k, v in weights.items()},
            "total": total, "resonance": {"count": rc, "threshold": 75, "is_resonance": rc >= 3},
            "top3_score": round(total, 1), "pct_chg": round(stock.get("pct_chg", 0), 1),
            "l2api": l2data}


def _pre_rank(candidates, top_n=50):
    """涨停相关性预排：涨速 + 涨幅 + 人气排名"""
    pop_cache = _POPULARITY_RANK_CACHE
    scored = []
    for stock in candidates:
        surge = stock.get("surge", 0)
        pct = stock.get("pct_chg", 0)
        short = stock["code"].split(".")[0]
        score = 0
        if surge >= 5: score += 3  # noqa: E701
        elif surge >= 3: score += 2  # noqa: E701
        elif surge >= 2: score += 1  # noqa: E701
        if pct >= 7: score += 3  # noqa: E701
        elif pct >= 5: score += 2  # noqa: E701
        elif pct >= 3: score += 1  # noqa: E701
        rank = pop_cache.get(short)
        if rank is not None:
            if rank <= 100: score += 4  # noqa: E701
            elif rank <= 200: score += 3  # noqa: E701
            elif rank <= 300: score += 2  # noqa: E701
            elif rank <= 500: score += 1  # noqa: E701
        scored.append((score, stock))
    scored.sort(key=lambda x: x[0], reverse=True)
    ranked = [s for _, s in scored[:top_n]]
    print(f"[预排] {len(candidates)}只 -> Top{len(ranked)} (涨速+涨幅+人气)")
    for i, (sc, st) in enumerate(scored[:10]):
        print(f"  {i+1}. {st['code']} {st['name']} 分{sc} (涨速{st.get('surge',0):.1f} 涨幅{st.get('pct_chg',0):.1f})")
    return ranked


def main():
    parser = argparse.ArgumentParser(description="涨停预测流程")
    parser.add_argument("--from-file", help="从已有信号文件加载", default=None)
    parser.add_argument("--top", type=int, default=50, help="分析前N只股票（默认50）")
    parser.add_argument("--no-l2", action="store_true", help="跳过L2初始化（用于开盘早期无L2扫描）")
    args = parser.parse_args()

    clear_tushare_cache()

    # 数据源预检：关键源异常时阻塞执行，避免基于错误数据决策
    from scripts.health_check import preflight_check, _send_alert_sync  # noqa: E402
    if not preflight_check():
        print("[预检] 关键数据源异常，阻塞执行。详情见 data/health_state.json")
        _write_empty_result("预检阻断: 关键数据源不可用")
        return

    print("=" * 50)
    print(f"涨停预测流程启动: {datetime.now()}")
    print("=" * 50)

    # 1. 获取候选股
    if args.from_file:
        candidates = load_from_file(args.from_file)
    else:
        print("\n[1/5] 异动扫描(东财API+代理)...")
        candidates = scan_surge()

    if not candidates:
        print("无候选股，退出")
        _write_empty_result("扫描无候选股")
        return

    # 1.5 全系统过滤
    print("\n[1.5/5] 全系统过滤...")
    candidates = filter_candidates(candidates)
    if not candidates:
        print("过滤后无候选股，退出")
        _write_empty_result("过滤后无候选股")
        return

    # 加载权重（从 .env 通过 tu_share.CONFIG 统一读取）
    weights = dict(AGENT_WEIGHTS)

    # 今日缓存
    today_str = datetime.now().strftime("%Y%m%d")
    scored_cache = {}
    analysis_dir = DATA_DIR / "analysis"
    if analysis_dir.exists():
        for f in sorted(analysis_dir.glob(f"{today_str}*.json")):
            try:
                items = json.loads(f.read_text())
                if isinstance(items, list):
                    for item in items:
                        if "code" in item and "scores" in item:
                            scored_cache[item["code"]] = {
                                dim: (item["scores"][dim], item.get("reasons", {}).get(dim, ""))
                                for dim in item["scores"]}
            except Exception:
                pass

    # L2 健康告警（当日仅发一次，避免重复）
    def _alert_l2(client):
        flag = DATA_DIR / "analysis" / f".l2_alerted_{today_str}"
        if flag.exists():
            return
        flag.touch()
        hs = client.health_summary()
        ch_info = hs.get("channels", {})
        lines = [f"⚠️ L2 实时数据异常 ({datetime.now().strftime('%H:%M')})", ""]
        for ch_name, st in ch_info.items():
            data_age = f"{st['last_data_age_s']}s" if st.get("last_data_age_s") else "无数据"
            lines.append(f"  {ch_name}: 连接={'✅' if st.get('connected') else '❌'} "
                         f"数据={data_age} 重连={st.get('recent_reconnects', 0)}次")
        lines.append("")
        lines.append("→ 观测缩减至15s，评分不受影响")
        _send_alert_sync("\n".join(lines))

    # 1.6 l2api 启动（--no-l2 模式下跳过）
    l2api = None
    l2_unhealthy = False  # 标记L2是否不健康，供观测循环使用
    if CONFIG.get("L2API_ENABLED", "").lower() == "true" and not args.no_l2:
        from scripts.l2_client import get_client
        account = CONFIG.get("L2API_ACCOUNT", "")
        password = CONFIG.get("L2API_PASSWORD", "")
        if account and password:
            print("\n[1.6/5] l2api Level2 实时数据接入...")
            try:
                l2api = get_client(account=account, password=password)
                if not l2api._running:
                    l2api.start()
                # 快速健康评估：等待初始连接稳定
                time.sleep(3)
                if l2api.is_healthy():
                    print("  l2api 已就绪")
                else:
                    hs = l2api.health_summary()
                    print(f"  l2api 启动但连接不稳定: {hs}")
                    print(f"  → 批次观测将缩减到15s，评分不受影响")
                    l2_unhealthy = True
                    # 飞书告警（当日首次，复用健康检查通道 FEISHU_ALERT_CHAT_ID）
                    _alert_l2(l2api)
            except Exception as e:
                print(f"  l2api 启动失败: {e}")
                l2api = None
    elif args.no_l2:
        print("\n[1.6/5] l2api 已跳过 (--no-l2 模式)")
    else:
        print("\n[1.6/5] l2api 未启用")

    # 1.7 涨停相关性预排
    print("\n[1.7/5] 涨停相关性预排...")
    _get_popularity_rank("")  # 触发人气缓存
    candidates = _pre_rank(candidates, top_n=args.top)

    # 2. 分批 Level2 深度分析
    BATCH_SIZE = 25
    OBSERVE_SECONDS = 60
    batch_count = (len(candidates) + BATCH_SIZE - 1) // BATCH_SIZE
    print(f"\n[2/5] 分批深度分析: {len(candidates)}只 -> {batch_count}批x{BATCH_SIZE}只, {OBSERVE_SECONDS}s/轮")

    results = []
    for bi in range(batch_count):
        batch = candidates[bi * BATCH_SIZE : (bi + 1) * BATCH_SIZE]
        codes = [c["code"] for c in batch]
        print(f"\n{'='*40}")
        print(f"批次 {bi+1}/{batch_count}: {len(codes)}只 {codes[:3]}...")
        print(f"{'='*40}")

        if l2api:
            # 健康门控：L2 不稳定时缩减观测，减少超时风险
            obs = OBSERVE_SECONDS
            if not l2api.is_healthy():
                obs = 15
                print(f"  [l2api] ⚠️ L2连接不健康，观测缩减为{obs}s (原{OBSERVE_SECONDS}s)")
            else:
                print(f"  [l2api] L2连接健康，观测{obs}s")

            l2api.sync_subscriptions(codes)
            print(f"  [l2api] 订阅完成, 开始观测...")
            t0 = time.time()
            while time.time() - t0 < obs:
                time.sleep(1)
                # 早期退出：L2 在观测中途断开，已观察 >10s 后允许提前结束
                if obs > 15 and not l2api.is_healthy() and time.time() - t0 > 10:
                    print(f"  [l2api] 连接异常，提前结束观测 (已观测{time.time()-t0:.0f}s)")
                    break
            ready = sum(1 for c in codes if l2api.is_ready(c))
            print(f"  [l2api] 观测完成, 就绪{ready}/{len(codes)}")

        for stock in batch:
            code = stock["code"]
            cache_hit = code in scored_cache
            tag = "[缓存]" if cache_hit else "评分中"
            print(f"  {code} {stock['name']} {tag}")
            try:
                r = _score_one(stock, l2api, weights, scored_cache, cache_hit)
                results.append(r)
            except Exception as e:
                print(f"  {code} 评分失败: {e}")

        if l2api:
            l2api.unsubscribe(codes)

    # 排序
    results.sort(key=lambda x: x["total"], reverse=True)
    print("\n[排序结果]")
    for i, r in enumerate(results, 1):
        tag = " [共振]" if r.get("resonance", {}).get("is_resonance") else ""
        print(f"  {i}. {r['code']} {r['name']} - 总分:{r['total']:.1f}{tag}")

    # 保存结果
    output_dir = DATA_DIR / "analysis"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / f"{datetime.now().strftime('%Y%m%d_%H%M')}.json"
    with open(output_file, "w") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n结果已保存: {output_file}")

    # 3. 飞书推送
    print("\n[3/5] 飞书推送...")
    push_feishu(results)

    # l2api 常驻
    if l2api:
        print(f"\n[l2api] 保持连接 (当前订阅: {len(l2api.cache.get_subscribed())} 只)")

    print("\n" + "=" * 50)
    print("流程完成!")
    print("=" * 50)


if __name__ == "__main__":
    main()
