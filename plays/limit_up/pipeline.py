#!/usr/bin/env python3
"""
涨停预测完整流程脚本
流程：异动扫描(东财API+代理) → 五维度评分 → 排序 → 飞书推送

用法:
  python scripts/zt_pipeline.py                  # 完整流程(requests+代理)
  python scripts/zt_pipeline.py --from-file=data/signals/xxx.json  # 从已有文件读取
"""

import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
import argparse
import requests

# 项目根目录
PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_DIR))

# 从.env加载配置
def load_env():
    env_file = PROJECT_DIR / ".env"
    config = {}
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                config[key] = value
    return config

CONFIG = load_env()

# ===== Feishu 测试模式 =====
FEISHU_TEST_MODE = CONFIG.get("FEISHU_TEST_MODE", "").lower() == "true"
def feishu_title_prefix():
    """测试模式下返回'测试-'前缀"""
    return "测试-" if FEISHU_TEST_MODE else ""

# ===== Agent 权重配置（从.env读取，默认=1） =====
AGENT_WEIGHTS = {
    "fundamental": float(CONFIG.get("AGENT_WEIGHT_FUNDAMENTAL", "1.5")),
    "technical": float(CONFIG.get("AGENT_WEIGHT_TECHNICAL", "1.0")),
    "fundflow": float(CONFIG.get("AGENT_WEIGHT_FUND_FLOW", "1.0")),
    "sentiment": float(CONFIG.get("AGENT_WEIGHT_SENTIMENT", "1.2")),
    "shortterm": float(CONFIG.get("AGENT_WEIGHT_SHORTTERM", "1.5")),
}

# ===== Tushare API 缓存层 =====
# 同一只股票在同一次流水线执行中，相同api_name+params的调用只发一次请求
_TUSHARE_CACHE = {}

def call_tushare(api_name, token, params, fields="", timeout=10):
    """带缓存的Tushare API调用，避免同一股票重复请求同一接口"""
    cache_key = (api_name, json.dumps(params, sort_keys=True), fields)
    if cache_key in _TUSHARE_CACHE:
        return _TUSHARE_CACHE[cache_key]
    try:
        payload = {
            "api_name": api_name,
            "token": token,
            "params": params,
        }
        if fields:
            payload["fields"] = fields
        resp = requests.post("https://api.tushare.pro", json=payload, timeout=timeout)
        result = resp.json()
        _TUSHARE_CACHE[cache_key] = result
        return result
    except Exception:
        _TUSHARE_CACHE[cache_key] = {}  # 缓存失败结果，避免重试
        return {}

def clear_tushare_cache():
    """清空Tushare缓存（流水线开始时调用）"""
    global _TUSHARE_CACHE
    _TUSHARE_CACHE = {}

# ===== stock_basic 行业映射缓存（静态表，一次加载全量） =====
_INDUSTRY_MAP = {}  # ts_code → industry
_INDUSTRY_PEERS = {}  # industry → [ts_code, ...]

def _ensure_industry_map():
    """确保行业映射已加载（惰性初始化）"""
    global _INDUSTRY_MAP, _INDUSTRY_PEERS
    if _INDUSTRY_MAP:
        return
    try:
        token = CONFIG.get("TUSHARE_TOKEN", "")
        resp = call_tushare("stock_basic", token, {"list_status": "L"}, "ts_code,industry")
        items = resp.get("data", {}).get("items", [])
        for item in items:
            if len(item) >= 2:
                code, ind = item[0], (item[1] or '')
                _INDUSTRY_MAP[code] = ind
                if ind:
                    _INDUSTRY_PEERS.setdefault(ind, []).append(code)
        print(f"  行业映射缓存: {len(_INDUSTRY_MAP)}只股票, {len(_INDUSTRY_PEERS)}个行业")
    except Exception as e:
        print(f"  行业映射加载失败: {e}")

def get_industry(code):
    """获取个股所属行业"""
    _ensure_industry_map()
    return _INDUSTRY_MAP.get(code, '')

def get_industry_peers(industry, limit=20):
    """获取同行业股票列表"""
    _ensure_industry_map()
    peers = _INDUSTRY_PEERS.get(industry, [])
    return peers[:limit]

def clear_industry_cache():
    """清空行业缓存（测试用）"""
    global _INDUSTRY_MAP, _INDUSTRY_PEERS
    _INDUSTRY_MAP = {}
    _INDUSTRY_PEERS = {}

# ===== 全局工具函数 =====
def safe_float(val):
    """安全转换为float，失败返回0.0"""
    if val is None:
        return 0.0
    try:
        return float(val)
    except:
        return 0.0

def safe_float_none(val):
    """安全转换为float，失败返回None（用于需要区分None和0的场景）"""
    if val is None:
        return None
    try:
        return float(val)
    except:
        return None

def safe_int_none(val):
    """安全转换为int，失败返回None"""
    if val is None:
        return None
    try:
        return int(val)
    except:
        return None

def is_trading_time():
    """判断当前是否在A股交易时间(9:30~15:00，工作日)"""
    from datetime import datetime
    now = datetime.now()
    # 周末不算交易日
    if now.weekday() >= 5:
        return False
    # 9:30~15:00
    if now.hour < 9 or (now.hour == 9 and now.minute < 30):
        return False
    if now.hour >= 15:
        return False
    return True

def list_to_dict(items, fields):
    """将Tushare返回的list格式转为dict格式"""
    if not items or not fields:
        return []
    result = []
    for item in items:
        if isinstance(item, dict):
            result.append(item)
        elif isinstance(item, (list, tuple)):
            d = {}
            for i, f in enumerate(fields):
                if i < len(item):
                    d[f] = item[i]
            result.append(d)
    return result

# ===== 1. 扫描异动股 =====
def scan_surge():
    """通过东方财富clist API获取异动候选股（requests+代理，涨速+涨幅双路合并）
    
    数据源: push2.eastmoney.com/api/qt/clist/get
    双路: ①涨速降序(f11) ②涨幅降序(f3) → 合并去重
    Returns: list[dict] - [{code, name}] 候选股列表，或None
    """
    import re
    from scripts.proxy_utils import get_proxies_dict
    
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
        proxies = get_proxies_dict()
        resp = requests.get(url, proxies=proxies, timeout=15)
        data = resp.json()
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
            except:
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

# ===== 1.5 全系统过滤规则 (所有Agent共用) =====
def filter_candidates(candidates):
    """
    全系统7条过滤规则：满足任一条件直接排除，不进入分析
    1. ST/*ST/退市整理期
    2. 上市不满60日新股
    3. 创业板(30xxxx.SZ) / 科创板(688xxx.SH) / 北交所(8xxxxx.BJ)
    4. 当日停牌
    5. 自由流通市值 < 20亿
    6. 5日均换手率 < 2%
    7. 连续一字板（无法买入）
    """
    from datetime import datetime, timedelta
    
    token = CONFIG["TUSHARE_TOKEN"]
    today_str = datetime.now().strftime("%Y%m%d")
    filtered_in = []
    filter_log = []
    
    for stock in candidates:
        code = stock["code"]
        name = stock.get("name", "")
        vetoed = False
        veto_reason = ""
        
        # 规则3: 创业板/科创板/北交所 (纯代码判断，无需API)
        pure_code = code.split(".")[0]
        if pure_code.startswith("30") or pure_code.startswith("688") or pure_code.startswith("8") or pure_code.startswith("4"):
            # 30开头=创业板, 688开头=科创板, 8/4开头=北交所
            suffix = code.split(".")[-1] if "." in code else ""
            if pure_code.startswith("30"):
                vetoed = True
                veto_reason = f"规则3: 创业板({code})"
            elif pure_code.startswith("688"):
                vetoed = True
                veto_reason = f"规则3: 科创板({code})"
            elif pure_code.startswith("8") or pure_code.startswith("4"):
                # 8开头或4开头且后缀BJ(或无后缀)为北交所
                if suffix == "BJ" or suffix == "":
                    vetoed = True
                    veto_reason = f"规则3: 北交所({code})"
        
        if vetoed:
            filter_log.append(f"  [排除] {code} {name}: {veto_reason}")
            continue
        
        # 规则1/2/5/6/7/4 需要Tushare API数据，批量获取daily_basic
        try:
            resp_data = call_tushare(
                "daily_basic", token,
                {"ts_code": code, "trade_date": today_str},
                "ts_code,close,turnover_rate,turnover_rate_f,circ_mv,total_mv,pct_chg"
            )
            items = resp_data.get("data", {}).get("items", [])
            if not items:
                # 当日无数据(可能停牌或非交易日)，取最近一日
                resp_data = call_tushare(
                    "daily_basic", token,
                    {"ts_code": code},
                    "trade_date,ts_code,close,turnover_rate,turnover_rate_f,circ_mv,total_mv,pct_chg"
                )
                items = resp_data.get("data", {}).get("items", [])
            
            if not items:
                filter_log.append(f"  [排除] {code} {name}: 无行情数据")
                continue
            
            latest = items[0]
            field_map = resp_data.get("data", {}).get("fields", [])
            basic = dict(zip(field_map, latest))
            
            # 规则5: 自由流通市值 < 20亿 (circ_mv单位: 万元)
            circ_mv = safe_float(basic.get("circ_mv"))
            if circ_mv and circ_mv < 200000:  # 20亿=200000万
                vetoed = True
                veto_reason = f"规则5: 流通市值{circ_mv/10000:.1f}亿<20亿"
            
            # 规则6: 换手率 < 2% (取turnover_rate_f自由流通换手)
            turnover = safe_float(basic.get("turnover_rate_f")) or safe_float(basic.get("turnover_rate"))
            if not vetoed and turnover and turnover < 2:
                vetoed = True
                veto_reason = f"规则6: 换手率{turnover:.1f}%<2%"
            
            # 规则7: 连续一字板 (pct_chg接近10%或20%且换手极低)
            if not vetoed:
                pct_chg = safe_float(basic.get("pct_chg"))
                if pct_chg and turnover:
                    # 一字涨停: 涨幅>=9.9% 且 换手<0.5%
                    if pct_chg >= 9.9 and turnover < 0.5:
                        vetoed = True
                        veto_reason = f"规则7: 一字板(涨幅{pct_chg:.1f}%换手{turnover:.2f}%)"
                    # 一字跌停
                    elif pct_chg <= -9.9 and turnover < 0.5:
                        vetoed = True
                        veto_reason = f"规则7: 一字跌停(涨幅{pct_chg:.1f}%换手{turnover:.2f}%)"
            
        except Exception as e:
            filter_log.append(f"  [警告] {code} {name}: 数据获取失败({e}), 保留")
        
        if vetoed:
            filter_log.append(f"  [排除] {code} {name}: {veto_reason}")
            continue
        
        # 规则1: ST/*ST — 通过stock_basic查询
        try:
            resp = call_tushare("stock_basic", token, {"ts_code": code}, "ts_code,name,list_date")
            items = resp.get("data", {}).get("items", [])
            if items:
                stock_name = items[0][1] if len(items[0]) > 1 else name
                list_date = items[0][2] if len(items[0]) > 2 else None
                
                # 规则1: ST
                if stock_name and ("ST" in stock_name or "st" in stock_name.lower()):
                    vetoed = True
                    veto_reason = f"规则1: ST股({stock_name})"
                
                # 规则2: 上市不满60日
                if not vetoed and list_date:
                    try:
                        list_dt = datetime.strptime(str(list_date), "%Y%m%d")
                        days_since_list = (datetime.now() - list_dt).days
                        if days_since_list < 60:
                            vetoed = True
                            veto_reason = f"规则2: 上市{days_since_list}日<60日"
                    except:
                        pass
        except:
            pass
        
        if vetoed:
            filter_log.append(f"  [排除] {code} {name}: {veto_reason}")
            continue
        
        filtered_in.append(stock)
    
    print(f"\n[过滤] 输入{len(candidates)}只 → 保留{len(filtered_in)}只 → 排除{len(candidates)-len(filtered_in)}只")
    if filter_log:
        for log in filter_log:
            print(log)
    
    return filtered_in

# ===== 2. 基本面评分 (V1.0 五维度量化) =====
# Lazy import to avoid circular dependency
def score_fundamental(code):
    from plays.limit_up.agents.fundamental_agent import score_fundamental as _score_fundamental
    return _score_fundamental(code)

# ===== 3. 技术面评分 V1.0 (五维度量化) =====
def score_technical(code):
    from plays.limit_up.agents.technical_agent import score_technical as _score_technical
    return _score_technical(code)


# ===== 4. 资金面评分 (五维度量化评分 V1.0) =====
# 缓存当日资金流向数据（避免每次调用都重复请求）
_FUND_FLOW_CACHE = None
_FUND_FLOW_DATE = None

def score_fundflow(code):
    from plays.limit_up.agents.fundflow_agent import score_fundflow as _score_fundflow
    return _score_fundflow(code)

# V2.4: 实时涨幅缓存（CDP/requests+代理获取，避免盘中Tushare无数据）
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
    
    try:
        from scripts import proxy_utils as _pu
        proxies = _pu.get_proxies_dict() if _pu.is_proxy_enabled() else None
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
            resp = _req.get(url, proxies=proxies, timeout=10)
            data = resp.json()
            items = data.get("data", {}).get("diff", [])
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
        try:
            import requests as _req
            from scripts import proxy_utils as _pu
            proxies = _pu.get_proxies_dict() if _pu.is_proxy_enabled() else None
            cache = {}
            for pg in range(1, 3):
                url = (
                    "https://push2.eastmoney.com/api/qt/clist/get?"
                    "np=1&fltt=2&invt=2&"
                    "fs=m:0+t:6+f:!2,m:0+t:80+f:!2,m:1+t:2+f:!2,m:1+t:23+f:!2&"
                    f"fields=f12,f62&fid=f62&pn={pg}&pz=100&po=1&"
                    "ut=fa5fd1943c7b386f172d6893dbfba10b"
                )
                r2 = _req.get(url, proxies=proxies, timeout=8)
                items2 = r2.json().get("data", {}).get("diff", [])
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

# V2.4: 实时资金流缓存（东财API+代理，替代T+1 Tushare moneyflow）
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
    
    from scripts.proxy_utils import get_proxies_dict
    cache = {}
    for page in range(1, 6):  # 翻5页×100=500只
        try:
            proxies = get_proxies_dict()
            url = (
                "https://push2.eastmoney.com/api/qt/clist/get?"
                "np=1&fltt=2&invt=2&"
                "fs=m:0+t:6+f:!2,m:0+t:80+f:!2,m:1+t:2+f:!2,m:1+t:23+f:!2&"
                f"fields=f12,f62,f10,f7,f6&fid=f62&pn={page}&pz=100&po=1&dect=1&"
                "ut=fa5fd1943c7b386f172d6893dbfba10b"
            )
            resp = requests.get(url, proxies=proxies, timeout=10)
            items = resp.json().get("data", {}).get("diff", [])
            if not items:
                break
            for s in items:
                code = s.get("f12", "")
                if not code:
                    continue
                f62 = s.get("f62")
                try:
                    net_flow = float(f62) if f62 and f62 != "-" else 0
                except:
                    net_flow = 0
                try:
                    vol_ratio = float(s.get("f10", 0)) if s.get("f10") and s.get("f10") != "-" else 0
                except:
                    vol_ratio = 0
                try:
                    turnover = float(s.get("f7", 0)) if s.get("f7") and s.get("f7") != "-" else 0
                except:
                    turnover = 0
                try:
                    amount = float(s.get("f6", 0)) if s.get("f6") and s.get("f6") != "-" else 0
                except:
                    amount = 0
                cache[code] = {
                    "net_flow": net_flow,      # 主力净流入(元)
                    "vol_ratio": vol_ratio,     # 量比
                    "turnover": turnover,       # 换手率(%)
                    "amount": amount,           # 成交额(元)
                }
            if len(items) < 100:
                break
        except Exception as e:
            print(f"  实时资金流缓存第{page}页失败: {e}")
            break
    
    if cache:
        _REALTIME_FUND_CACHE = cache
        _REALTIME_FUND_TS = today
        print(f"  实时资金流缓存: {len(cache)} 只")
    return cache


def score_sentiment(code):
    from plays.limit_up.agents.sentiment_agent import score_sentiment as _score_sentiment
    return _score_sentiment(code)


# ===== 6. 飞书推送 =====
def push_feishu(results):
    """发送飞书卡片

    推送规则：
    - 综合评级>=35(⭐⭐⭐)的股票按总分降序取前3只推送
    - 如果没有>=35的股票，不推送
    - 推送记录保存到 data/pushed/ 目录，供复盘使用
    """
    import requests

    def _stars(total):
        """综合评级: >=55 ⭐⭐⭐⭐⭐  >=45 ⭐⭐⭐⭐  >=35 ⭐⭐⭐"""
        if total >= 55: return "⭐ ⭐ ⭐ ⭐ ⭐"
        if total >= 45: return "⭐ ⭐ ⭐ ⭐"
        if total >= 35: return "⭐ ⭐ ⭐"
        return ""

    # 推送筛选 (V2.6: 加权Top3择优，阈值35)
    THRESHOLD = 35
    above_threshold = sorted(
        [r for r in results if r.get('total', 0) >= THRESHOLD],
        key=lambda x: x.get('total', 0), reverse=True
    )[:3]
    if above_threshold:
        push_list = above_threshold
        print(f"  推送池: {len(above_threshold)}只(综合评级>={THRESHOLD}前3)")
    else:
        push_list = []
        print(f"  无>={THRESHOLD}评级股票，不推送")

    if not push_list:
        print("  无可推送股票")
        return False

    # 保存推送记录（供复盘使用）
    pushed_dir = PROJECT_DIR / "data" / "pushed"
    pushed_dir.mkdir(parents=True, exist_ok=True)
    pushed_file = pushed_dir / f"{datetime.now().strftime('%Y%m%d_%H%M')}.json"
    with open(pushed_file, "w") as f:
        json.dump(push_list, f, ensure_ascii=False, indent=2)
    print(f"  推送记录已保存: {pushed_file}")

    # 获取token
    resp = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={
            "app_id": CONFIG["FEISHU_APP_ID"],
            "app_secret": CONFIG["FEISHU_APP_SECRET"]
        }
    )
    _feishu_resp = resp.json()
    token = _feishu_resp.get("tenant_access_token")

    if not token:
        print("飞书token获取失败")
        return False

    # 构建卡片
    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"{feishu_title_prefix()}涨停预测信号 ({datetime.now().strftime('%Y-%m-%d %H:%M')})"},
            "template": "blue"
        },
        "elements": []
    }

    for r in push_list:
        s = r.get('scores', {})
        stars = _stars(r['total'])
        element = {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"**{r['code']} {r['name']}** {stars}\n"
                          f"综合评分:{r['total']:.1f}  基本面:{s.get('fundamental',0):.0f} 技术面:{s.get('technical',0):.0f} 资金面:{s.get('fundflow',0):.0f} 情绪面:{s.get('sentiment',0):.0f} 短线:{s.get('shortterm',0):.0f}"
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
    output_path = PROJECT_DIR / "data" / "analysis" / f"{ts}.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    empty = [{"_empty": True, "reason": reason, "time": now.isoformat()}]
    with open(output_path, "w") as f:
        json.dump(empty, f, ensure_ascii=False)
    print(f"零结果已记录: {output_path}")

def main():
    parser = argparse.ArgumentParser(description="涨停预测流程")
    parser.add_argument("--from-file", help="从已有信号文件加载", default=None)
    parser.add_argument("--top", type=int, default=50, help="分析前N只股票（默认50，覆盖全部候选股）")
    args = parser.parse_args()
    
    clear_tushare_cache()  # 每次流水线启动清空API缓存
    
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
        # 兜底：扫空也写零结果文件，避免静默失败
        _write_empty_result("扫描无候选股")
        return
    
    # 取前N只做分析
    candidates = candidates[:args.top]
    print(f"分析候选股: {[c['code'] for c in candidates]}")
    
    # 1.5 全系统过滤
    print("\n[1.5/5] 全系统过滤...")
    candidates = filter_candidates(candidates)
    if not candidates:
        print("过滤后无候选股，退出")
        _write_empty_result("过滤后无候选股")
        return

    # 1.6 l2api Level2 实时数据接入
    l2api = None
    if CONFIG.get("L2API_ENABLED", "").lower() == "true":
        from plays.limit_up.l2api_client import get_client, has_client
        l2api_account = CONFIG.get("L2API_ACCOUNT", "")
        l2api_password = CONFIG.get("L2API_PASSWORD", "")
        if l2api_account and l2api_password:
            print("\n[1.6/5] l2api Level2 实时数据接入...")
            try:
                l2api = get_client(account=l2api_account, password=l2api_password)
                if not has_client() or not l2api._running:
                    l2api.start()
                # 试用期 TopicCnt=30, 留1个给盯盘, 最多订阅29只
                l2api_codes = [c["code"] for c in candidates[:29]]
                l2api.sync_subscriptions(l2api_codes)
                print(f"  l2api 订阅 {len(l2api_codes)} 只 (差量: +{len([c for c in l2api_codes if c not in l2api.cache.get_subscribed()])}/-{len(l2api.cache.get_subscribed() - set(l2api_codes))})")
                time.sleep(3)
                ready_count = sum(1 for c in l2api_codes if l2api.is_ready(c))
                print(f"  l2api 数据就绪: {ready_count}/{len(l2api_codes)}")
            except Exception as e:
                print(f"  l2api 启动失败: {e}")
                l2api = None
        else:
            print("\n[1.6/5] l2api 未配置账号密码，跳过")
    
    # 2-5. 四维度评分 + 短线博弈
    # 前置过滤：加载今日已分析过的股票得分，避免重复调用Tushare API
    today_str = datetime.now().strftime("%Y%m%d")
    scored_cache = {}  # {code: {dim: (score, reason)}}
    analysis_dir = PROJECT_DIR / "data" / "analysis"
    if analysis_dir.exists():
        for f in sorted(analysis_dir.glob(f"{today_str}*.json")):
            try:
                items = json.loads(f.read_text())
                if isinstance(items, list):
                    for item in items:
                        if "code" in item and "scores" in item:
                            code = item["code"]
                            scored_cache[code] = {
                                dim: (item["scores"][dim], item.get("reasons", {}).get(dim, ""))
                                for dim in item["scores"]
                            }
            except Exception:
                pass
    if scored_cache:
        print(f"[缓存] 今日已有 {len(scored_cache)} 只股票的评分记录")

    results = []
    for stock in candidates:
        code = stock["code"]
        name = stock["name"]
        print(f"\n[分析] {code} {name}")
        
        # 今日已有评分 → 复用基本面和技术面（T+1静态数据），盘面实时 agent 仍重新评分
        if code in scored_cache:
            cached = scored_cache[code]
            scores = {dim: v[0] for dim, v in cached.items()}
            reasons = {dim: v[1] for dim, v in cached.items()}
            print(f"  [缓存命中] {list(scores.keys())} 复用 {list(cached.keys())}")
        else:
            scores = {}
            reasons = {}
        
        print("  五维度并行评分...", flush=True)
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from plays.limit_up.agents.shortterm_agent import score_shortterm
        scoring_funcs = {
            "fundamental": score_fundamental,
            "technical": score_technical,
            "fundflow": score_fundflow,
            "sentiment": score_sentiment,
            "shortterm": score_shortterm,
        }
        # 缓存命中时，跳过基本面和技术面的API调用
        funcs_to_run = {dim: fn for dim, fn in scoring_funcs.items() if dim not in scores}
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {executor.submit(fn, code): dim for dim, fn in funcs_to_run.items()}
            for future in as_completed(futures):
                dim = futures[future]
                try:
                    s, r = future.result()
                    scores[dim] = s
                    reasons[dim] = r
                    print(f"    {dim}: {s}")
                except Exception as e:
                    scores[dim] = 0
                    reasons[dim] = f"评分异常: {e}"
                    print(f"    {dim}: 异常 {e}")
        
        f_score = scores.get("fundamental", 0)
        t_score = scores.get("technical", 0)
        m_score = scores.get("fundflow", 0)
        s_score = scores.get("sentiment", 0)
        st_score = scores.get("shortterm", 0)
        f_reason = reasons.get("fundamental", "")
        t_reason = reasons.get("technical", "")
        m_reason = reasons.get("fundflow", "")
        s_reason = reasons.get("sentiment", "")
        st_reason = reasons.get("shortterm", "")
        
        # 加载权重（支持从.env覆写）
        weights = {
            "fundamental": 1.5,
            "technical": 1.0,
            "fundflow": 0.5,
            "sentiment": 1.2,
            "shortterm": 1.5,
        }
        if (PROJECT_DIR / ".env").exists():
            with open(PROJECT_DIR / ".env") as _wf:
                for _wl in _wf:
                    _wl = _wl.strip()
                    if _wl.startswith("AGENT_WEIGHT_FUNDAMENTAL="):
                        weights["fundamental"] = float(_wl.split("=", 1)[1].strip())
                    elif _wl.startswith("AGENT_WEIGHT_TECHNICAL="):
                        weights["technical"] = float(_wl.split("=", 1)[1].strip())
                    elif _wl.startswith("AGENT_WEIGHT_FUND_FLOW="):
                        weights["fundflow"] = float(_wl.split("=", 1)[1].strip())
                    elif _wl.startswith("AGENT_WEIGHT_SENTIMENT="):
                        weights["sentiment"] = float(_wl.split("=", 1)[1].strip())
                    elif _wl.startswith("AGENT_WEIGHT_SHORTTERM="):
                        weights["shortterm"] = float(_wl.split("=", 1)[1].strip())
        
        # V2.6: 加权Top3择优（按加权贡献选前3维，取加权均值）
        # 让权重真正影响哪3个维度进Top3以及贡献大小
        dim_contribs = [
            (f_score, weights.get("fundamental", 1.0)),
            (t_score, weights.get("technical", 1.0)),
            (m_score, weights.get("fundflow", 1.0)),
            (s_score, weights.get("sentiment", 1.0)),
            (st_score, weights.get("shortterm", 1.5)),
        ]
        dim_contribs.sort(key=lambda x: x[0] * x[1], reverse=True)
        top3 = dim_contribs[:3]
        total = sum(s * w for s, w in top3) / sum(w for _, w in top3) if sum(w for _, w in top3) > 0 else 0
        
        # 多维度共振判定（≥3个维度得分≥75）
        resonance_threshold = 75
        resonance_count = sum([
            f_score >= resonance_threshold,
            t_score >= resonance_threshold,
            m_score >= resonance_threshold,
            s_score >= resonance_threshold,
            st_score >= resonance_threshold,
        ])
        resonance_flag = resonance_count >= 3
        
        # 记录权重信息（用于调试和复盘）
        weight_info = {k: f"{v:.1f}" for k, v in weights.items()}

        # 附加 l2api Level2 数据 (验证用)
        l2data = None
        if l2api:
            try:
                from plays.limit_up.l2api_client import to_price, to_volume
                market = l2api.get_market(code)
                kline_5m = l2api.get_minute_kline(code, n=5)
                vwap = l2api.get_vwap(code)
                if market:
                    l2data = {
                        "last": to_price(market.get("last", "0")),
                        "open": to_price(market.get("open", "0")),
                        "high": to_price(market.get("high", "0")),
                        "low": to_price(market.get("low", "0")),
                        "prev_close": to_price(market.get("prev_close", "0")),
                        "trade_volume": to_volume(market.get("trade_volume", "0")),
                        "trade_amount": to_price(market.get("trade_amount", "0")),
                        "bid1_price": to_price(market.get("bid_price", [""])[0]) if market.get("bid_price") else 0,
                        "bid1_qty": to_volume(market.get("bid_qty", [""])[0]) if market.get("bid_qty") else 0,
                        "ask1_price": to_price(market.get("ask_price", [""])[0]) if market.get("ask_price") else 0,
                        "ask1_qty": to_volume(market.get("ask_qty", [""])[0]) if market.get("ask_qty") else 0,
                        "total_bid_volume": to_volume(market.get("total_bid_volume", "0")),
                        "total_ask_volume": to_volume(market.get("total_ask_volume", "0")),
                        "vwap": round(vwap, 2) if vwap else None,
                        "kline_bars": len(kline_5m),
                    }
            except Exception as e:
                l2data = {"error": str(e)}

        results.append({
            "code": code,
            "name": name,
            "scores": {
                "fundamental": f_score,
                "technical": t_score,
                "fundflow": m_score,
                "sentiment": s_score,
                "shortterm": st_score,
            },
            "reasons": {
                "fundamental": f_reason,
                "technical": t_reason,
                "fundflow": m_reason,
                "sentiment": s_reason,
                "shortterm": st_reason,
            },
            "weights": weight_info,
            "total": total,
            "resonance": {
                "count": resonance_count,
                "threshold": resonance_threshold,
                "is_resonance": resonance_flag
            },
            "top3_score": round(total, 1),
            "pct_chg": round(stock.get("pct_chg", 0), 1),
            "l2api": l2data,  # Level2 实时数据快照
        })
    
    # 排序（全按总分排序，总分=Top3均值）
    by_total = sorted(results, key=lambda x: x["total"], reverse=True)
    results = by_total

    print("\n[排序结果]")
    for i, r in enumerate(by_total, 1):
        resonance_tag = " [共振]" if r.get("resonance", {}).get("is_resonance") else ""
        print(f"  {i}. {r['code']} {r['name']} - 总分:{r['total']:.1f}{resonance_tag}")
    
    # 保存结果
    output_dir = PROJECT_DIR / "data" / "analysis"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / f"{datetime.now().strftime('%Y%m%d_%H%M')}.json"
    with open(output_file, "w") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n结果已保存: {output_file}")
    
    # 6. 飞书推送
    print("\n[飞书推送]...")
    push_feishu(results)

    # 7. l2api 常驻,不在此次关闭 (供盯盘和后续扫描复用)
    if l2api:
        print(f"\n[l2api] 保持连接 (当前订阅: {len(l2api.cache.get_subscribed())} 只)")

    print("\n" + "=" * 50)
    print("流程完成!")
    print("=" * 50)

if __name__ == "__main__":
    main()
