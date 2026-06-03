#!/usr/bin/env python3
"""数据源健康巡检系统
================================
监控 Tushare / 东财缓存 / Level2 TCP / 代理 四大数据源。
支持: 数据异常检测、飞书告警、熔断阻断、状态持久化。

用法:
  python scripts/health_check.py           # 快速预检 (仅 CRITICAL+WARNING)
  python scripts/health_check.py --full    # 全量巡检 (含 INFO 级别)
  python scripts/health_check.py --quiet   # 静默模式 (不打印, 仅返回退出码)

集成:
  from scripts.health_check import preflight_check
  if not preflight_check(): return  # pipeline 阻塞
"""

import json
import sys
import time
import logging
import asyncio
from pathlib import Path
from datetime import datetime, timedelta
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional

# 确保项目根目录在 sys.path 中 (支持从任意目录调用)
_PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(_PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(_PROJECT_DIR))

logger = logging.getLogger("health_check")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

PROJECT_DIR = _PROJECT_DIR
DATA_DIR = PROJECT_DIR / "data"
STATE_FILE = DATA_DIR / "health_state.json"

# ═══════════════════════════════════════════════════════════
# 数据模型
# ═══════════════════════════════════════════════════════════


class Severity(Enum):
    OK = "OK"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


@dataclass
class HealthStatus:
    source: str
    severity: Severity
    message: str
    latency_ms: float = 0
    detail: dict = field(default_factory=dict)
    checked_at: str = field(default_factory=lambda: datetime.now().isoformat())


@dataclass
class HealthReport:
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    overall: Severity = Severity.OK
    checks: list = field(default_factory=list)
    circuit_breaker_active: bool = False

    def to_dict(self):
        return {
            "timestamp": self.timestamp,
            "overall": self.overall.value,
            "checks": [
                {
                    "source": c.source,
                    "severity": c.severity.value,
                    "message": c.message,
                    "latency_ms": c.latency_ms,
                    "detail": c.detail,
                    "checked_at": c.checked_at,
                }
                for c in self.checks
            ],
            "circuit_breaker_active": self.circuit_breaker_active,
        }


# ═══════════════════════════════════════════════════════════
# 熔断器
# ═══════════════════════════════════════════════════════════


class CircuitBreaker:
    """持久化熔断器，状态存储在 data/health_state.json"""

    COOLDOWN_MINUTES = 5

    @staticmethod
    def load() -> dict:
        if STATE_FILE.exists():
            try:
                return json.loads(STATE_FILE.read_text())
            except Exception:
                pass
        return {}

    @staticmethod
    def save(state: dict):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))

    @classmethod
    def is_active(cls) -> bool:
        state = cls.load()
        cb = state.get("circuit_breaker", {})
        if not cb.get("active"):
            return False
        cooldown = cb.get("cooldown_until", "")
        if cooldown:
            try:
                if datetime.fromisoformat(cooldown) < datetime.now():
                    state["circuit_breaker"]["active"] = False
                    state["circuit_breaker"]["consecutive_failures"] = 0
                    cls.save(state)
                    return False
            except Exception:
                pass
        return True

    @classmethod
    def trip(cls, reasons: list[str]):
        state = cls.load()
        cb = state.setdefault("circuit_breaker", {})
        failures = cb.get("consecutive_failures", 0) + 1
        cb.update({
            "active": True,
            "triggered_at": datetime.now().isoformat(),
            "triggered_by": reasons,
            "cooldown_until": (
                datetime.now() + timedelta(minutes=cls.COOLDOWN_MINUTES)
            ).isoformat(),
            "consecutive_failures": failures,
        })
        cls.save(state)
        logger.warning("熔断触发: %s (连续%d次)", reasons, failures)

    @classmethod
    def reset(cls):
        state = cls.load()
        state["circuit_breaker"] = {"active": False, "consecutive_failures": 0}
        state["last_ok_at"] = datetime.now().isoformat()
        cls.save(state)


# ═══════════════════════════════════════════════════════════
# 飞书告警
# ═══════════════════════════════════════════════════════════


def _load_alert_chat_id() -> str:
    """从 .env 读取 FEISHU_ALERT_CHAT_ID"""
    try:
        env_file = PROJECT_DIR / ".env"
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                line = line.strip()
                if line.startswith("FEISHU_ALERT_CHAT_ID="):
                    return line.split("=", 1)[1]
    except Exception:
        pass
    return ""


def _send_alert_sync(text: str) -> bool:
    """同步发送飞书告警消息（包装异步 FeishuClient）"""
    chat_id = _load_alert_chat_id()
    if not chat_id:
        logger.warning("飞书告警跳过: 未配置 FEISHU_ALERT_CHAT_ID")
        return False

    try:
        from feishu_bot.feishu_client import FEISHU_CLIENT

        async def _send():
            return await FEISHU_CLIENT.send_text(chat_id, text)

        result = asyncio.run(_send())
        if isinstance(result, dict) and result.get("code") == 0:
            logger.info("飞书告警已发送")
            return True
        logger.error("飞书告警发送失败: %s", result)
    except Exception as e:
        logger.error("飞书告警异常: %s", e)
    return False


# ═══════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════


def _is_market_hours() -> bool:
    """判断是否在交易时段 (9:30-11:30, 13:00-15:00, 工作日)"""
    try:
        from plays.limit_up.utils import is_trading_time
        return is_trading_time()
    except Exception:
        now = datetime.now()
        return (
            now.weekday() < 5
            and ((9, 30) <= (now.hour, now.minute) < (11, 30)
                 or (13, 0) <= (now.hour, now.minute) < (15, 0))
        )


def _timed_check(source: str, fn, *args) -> HealthStatus:
    """包装检查函数，自动计时"""
    t0 = time.time()
    try:
        msg, sev, detail = fn(*args)
    except Exception as e:
        msg = str(e)
        sev = Severity.CRITICAL
        detail = {"error": str(e)}
    elapsed = (time.time() - t0) * 1000
    return HealthStatus(
        source=source,
        severity=sev,
        message=msg,
        latency_ms=round(elapsed, 1),
        detail=detail,
    )


# ═══════════════════════════════════════════════════════════
# 1. Tushare API 巡检
# ═══════════════════════════════════════════════════════════

# 分级: CRITICAL=阻塞, WARNING=降级, INFO=仅记录
TUSHARE_CRITICAL = ["daily", "daily_basic", "moneyflow", "stk_factor_pro", "limit_list_d"]
TUSHARE_WARNING = [
    "trade_cal", "stock_basic", "fina_indicator", "balancesheet", "income",
    "top_list", "top_inst", "limit_cpt_list", "daily_info", "limit_step",
]
TUSHARE_INFO = [
    "stk_holdernumber", "share_float", "forecast", "hk_hold", "margin_detail",
]

# 各 API 的最小探测参数
TUSHARE_PROBE_PARAMS = {
    "daily": {"ts_code": "000001.SZ", "limit": 1},
    "daily_basic": {"ts_code": "000001.SZ", "limit": 1},
    "moneyflow": {"ts_code": "000001.SZ", "limit": 1},
    "stk_factor_pro": {"ts_code": "000001.SZ", "limit": 1},
    "limit_list_d": {"trade_date": datetime.now().strftime("%Y%m%d"), "limit": 1},
    "trade_cal": {"exchange": "SSE", "start_date": datetime.now().strftime("%Y%m%d"),
                  "end_date": datetime.now().strftime("%Y%m%d")},
    "stock_basic": {"ts_code": "000001.SZ"},
    "fina_indicator": {"ts_code": "000001.SZ", "limit": 1},
    "balancesheet": {"ts_code": "000001.SZ", "limit": 1},
    "income": {"ts_code": "000001.SZ", "limit": 1},
    "top_list": {"trade_date": datetime.now().strftime("%Y%m%d")},
    "top_inst": {"trade_date": datetime.now().strftime("%Y%m%d"), "limit": 1},
    "limit_cpt_list": {"trade_date": datetime.now().strftime("%Y%m%d"), "limit": 1},
    "daily_info": {"trade_date": datetime.now().strftime("%Y%m%d"), "ts_code": "SSE", "limit": 1},
    "limit_step": {"trade_date": datetime.now().strftime("%Y%m%d"), "limit": 1},
    "stk_holdernumber": {"ts_code": "000001.SZ", "limit": 1},
    "share_float": {"ts_code": "000001.SZ", "limit": 1},
    "forecast": {"ts_code": "000001.SZ", "limit": 1},
    "hk_hold": {"ts_code": "000001.SZ", "exchange": "SZ", "limit": 1},
    "margin_detail": {"ts_code": "000001.SZ", "limit": 1},
}


def _check_single_tushare(api_name: str, criticality: str) -> HealthStatus:
    """检查单个 Tushare API"""
    from scripts.tu_share import call_tushare, clear_tushare_cache
    clear_tushare_cache()

    params = TUSHARE_PROBE_PARAMS.get(api_name, {"limit": 1})
    t0 = time.time()

    try:
        r = call_tushare(api_name, params, "", timeout=5)
        if not isinstance(r, dict):
            return HealthStatus(
                source=f"tushare:{api_name}",
                severity=Severity.CRITICAL if criticality == "critical" else Severity.WARNING,
                message=f"返回类型异常: {type(r).__name__}",
                latency_ms=(time.time() - t0) * 1000,
            )

        code = r.get("code", -1)
        data = r.get("data") or {}
        items = data.get("items", [])
        elapsed = (time.time() - t0) * 1000

        if code != 0:
            msg = r.get("msg", f"错误码{code}")
            return HealthStatus(
                source=f"tushare:{api_name}",
                severity=Severity.CRITICAL if criticality == "critical" else Severity.WARNING,
                message=f"API错误: {msg}",
                latency_ms=elapsed,
                detail={"code": code, "msg": msg},
            )

        # 数据就绪检查：交易时段 CRITICAL API 不应返回空
        if not items and criticality == "critical" and _is_market_hours():
            # 盘后空是正常的（T+1 数据未就绪），盘前/盘中空是异常
            return HealthStatus(
                source=f"tushare:{api_name}",
                severity=Severity.CRITICAL,
                message=f"交易时段返回空数据",
                latency_ms=elapsed,
                detail={"items_count": 0},
            )

        detail = {"items_count": len(items)}
        if items and len(data.get("fields", [])) > 0:
            detail["latest_date"] = str(items[0][0]) if items[0] else "?"

        return HealthStatus(
            source=f"tushare:{api_name}",
            severity=Severity.OK,
            message=f"正常 ({len(items)}条)" if items else "空(非交易时段/正常)",
            latency_ms=elapsed,
            detail=detail,
        )

    except Exception as e:
        return HealthStatus(
            source=f"tushare:{api_name}",
            severity=Severity.CRITICAL if criticality == "critical" else Severity.WARNING,
            message=f"异常: {e}",
            latency_ms=(time.time() - t0) * 1000,
            detail={"error": str(e)},
        )


def check_tushare_apis(full: bool = False) -> list[HealthStatus]:
    """检查 Tushare API 连通性和数据有效性"""
    results = []

    for api in TUSHARE_CRITICAL:
        results.append(_check_single_tushare(api, "critical"))

    for api in TUSHARE_WARNING:
        results.append(_check_single_tushare(api, "warning"))

    if full:
        for api in TUSHARE_INFO:
            results.append(_check_single_tushare(api, "info"))

    return results


# ═══════════════════════════════════════════════════════════
# 2. 东财实时缓存巡检
# ═══════════════════════════════════════════════════════════


def check_eastmoney_caches() -> list[HealthStatus]:
    """检查东财实时缓存的数据完整性和异常值"""
    results = []
    t0 = time.time()

    try:
        import plays.limit_up.pipeline as pl

        # 检查涨跌幅缓存
        pct_cache = getattr(pl, "_REALTIME_PCT_CACHE", {})
        pct_ts = getattr(pl, "_REALTIME_PCT_TS", "")

        if _is_market_hours():
            if not pct_cache or pct_ts != datetime.now().strftime("%Y%m%d"):
                results.append(HealthStatus(
                    source="eastmoney:pct_cache",
                    severity=Severity.OK,
                    message="缓存为空(跨进程巡检,跳过)",
                    latency_ms=(time.time() - t0) * 1000,
                    detail={"cache_size": len(pct_cache), "cache_date": pct_ts},
                ))
            else:
                # 异常值检测
                anomalies = []
                for code, pct in list(pct_cache.items())[:500]:
                    try:
                        val = float(pct)
                        if abs(val) > 30:
                            anomalies.append(f"{code}={val}%")
                    except (ValueError, TypeError):
                        anomalies.append(f"{code}={pct}")
                if len(anomalies) > len(pct_cache) * 0.1:
                    results.append(HealthStatus(
                        source="eastmoney:pct_cache",
                        severity=Severity.CRITICAL,
                        message=f"涨跌幅异常值过多: {len(anomalies)}/{len(pct_cache)}",
                        detail={"anomaly_sample": anomalies[:5]},
                    ))
                elif anomalies:
                    results.append(HealthStatus(
                        source="eastmoney:pct_cache",
                        severity=Severity.WARNING,
                        message=f"涨跌幅异常值: {len(anomalies)}个",
                        detail={"anomaly_sample": anomalies[:5]},
                    ))
                else:
                    results.append(HealthStatus(
                        source="eastmoney:pct_cache",
                        severity=Severity.OK,
                        message=f"正常 ({len(pct_cache)}只)",
                        latency_ms=(time.time() - t0) * 1000,
                    ))
        else:
            results.append(HealthStatus(
                source="eastmoney:pct_cache",
                severity=Severity.OK,
                message="非交易时段,跳过",
                latency_ms=(time.time() - t0) * 1000,
            ))

        # 检查资金流缓存
        fund_cache = getattr(pl, "_REALTIME_FUND_CACHE", {})
        fund_ts = getattr(pl, "_REALTIME_FUND_TS", "")

        if _is_market_hours():
            if not fund_cache or fund_ts != datetime.now().strftime("%Y%m%d"):
                results.append(HealthStatus(
                    source="eastmoney:fund_cache",
                    severity=Severity.OK,
                    message="缓存为空(跨进程巡检,跳过)",
                    detail={"cache_size": len(fund_cache), "cache_date": fund_ts},
                ))
            else:
                # 全零检测
                zero_count = sum(
                    1 for v in fund_cache.values()
                    if v.get("net_flow", 0) == 0 and v.get("amount", 0) == 0
                )
                total = len(fund_cache)
                results.append(HealthStatus(
                    source="eastmoney:fund_cache",
                    severity=Severity.OK,
                    message=f"正常 ({total}只, 全零{zero_count}只)" if total > 0 else "空",
                    detail={"total": total, "all_zero": zero_count},
                    latency_ms=(time.time() - t0) * 1000,
                ))
        else:
            results.append(HealthStatus(
                source="eastmoney:fund_cache",
                severity=Severity.OK,
                message="非交易时段,跳过",
                latency_ms=(time.time() - t0) * 1000,
            ))

    except Exception as e:
        results.append(HealthStatus(
            source="eastmoney:caches",
            severity=Severity.CRITICAL,
            message=f"缓存检查异常: {e}",
            latency_ms=(time.time() - t0) * 1000,
            detail={"error": str(e)},
        ))

    return results


# ═══════════════════════════════════════════════════════════
# 3. Level2 TCP 巡检
# ═══════════════════════════════════════════════════════════


def check_l2_connection() -> list[HealthStatus]:
    """检查 Level2 TCP 连接状态和数据到达"""
    results = []
    t0 = time.time()

    try:
        from scripts.tu_share import CONFIG
        enabled = CONFIG.get("L2API_ENABLED", "").lower() == "true"

        if not enabled:
            results.append(HealthStatus(
                source="l2:connection",
                severity=Severity.OK,
                message="未启用 (L2API_ENABLED=false)",
                latency_ms=(time.time() - t0) * 1000,
            ))
            return results

        from scripts.l2_client import has_client, get_client

        if not has_client():
            results.append(HealthStatus(
                source="l2:connection",
                severity=Severity.OK,
                message="未初始化(非pipeline上下文,跳过)",
                latency_ms=(time.time() - t0) * 1000,
            ))
            return results

        client = get_client()
        running = client._running
        subscribed = client.cache.get_subscribed()

        results.append(HealthStatus(
            source="l2:connection",
            severity=Severity.OK if running else Severity.WARNING,
            message=f"{'运行中' if running else '已停止'}, 订阅{len(subscribed)}只",
            detail={"running": running, "subscribed_count": len(subscribed)},
            latency_ms=(time.time() - t0) * 1000,
        ))

        # 数据就绪检测
        if running and subscribed:
            stale_count = 0
            sample_sym = None
            for sym in list(subscribed)[:5]:
                mkt = client.get_market(sym)
                if mkt is None:
                    stale_count += 1
                else:
                    sample_sym = sym
                    break

            if stale_count == len(list(subscribed)[:5]):
                results.append(HealthStatus(
                    source="l2:data",
                    severity=Severity.WARNING if _is_market_hours() else Severity.OK,
                    message=f"无数据到达(订阅{len(subscribed)}只, 抽样{min(5,len(subscribed))}只均无数据)"
                    if _is_market_hours() else f"非交易时段(订阅{len(subscribed)}只)",
                    detail={"subscribed": len(subscribed), "stale_sample": stale_count},
                    latency_ms=(time.time() - t0) * 1000,
                ))
            elif sample_sym:
                from scripts.l2_client import to_price
                mkt = client.get_market(sample_sym)
                last = to_price(mkt.get("last", "0")) if mkt else 0
                results.append(HealthStatus(
                    source="l2:data",
                    severity=Severity.OK,
                    message=f"数据正常 ({sample_sym} last={last:.2f})",
                    detail={"sample_symbol": sample_sym, "last_price": last},
                    latency_ms=(time.time() - t0) * 1000,
                ))

    except Exception as e:
        results.append(HealthStatus(
            source="l2:connection",
            severity=Severity.WARNING,
            message=f"检查异常: {e}",
            latency_ms=(time.time() - t0) * 1000,
            detail={"error": str(e)},
        ))

    return results


# ═══════════════════════════════════════════════════════════
# 4. 代理巡检
# ═══════════════════════════════════════════════════════════


def check_proxy() -> list[HealthStatus]:
    """检查代理连通性和东财API可达性"""
    results = []
    t0 = time.time()

    try:
        from scripts.proxy_utils import get_proxy_ip, request_with_proxy_retry

        # 1. 代理IP获取
        proxy_addr = get_proxy_ip(force_refresh=True)
        if proxy_addr is None:
            results.append(HealthStatus(
                source="proxy:acquire",
                severity=Severity.CRITICAL,
                message="无法获取代理IP(配额耗尽或服务异常)",
                latency_ms=(time.time() - t0) * 1000,
            ))
            return results

        results.append(HealthStatus(
            source="proxy:acquire",
            severity=Severity.OK,
            message=f"获取成功 ({proxy_addr})",
            latency_ms=(time.time() - t0) * 1000,
            detail={"proxy": proxy_addr},
        ))

        # 2. 东财API可达性
        t1 = time.time()
        url = (
            "https://push2.eastmoney.com/api/qt/stock/get"
            "?secid=1.000001&fields=f43,f57,f170"
        )
        resp = request_with_proxy_retry(url, max_retries=1, timeout=15)
        if resp is None:
            results.append(HealthStatus(
                source="proxy:eastmoney",
                severity=Severity.WARNING,
                message="通过代理无法访问东财API(重试后仍失败)",
                latency_ms=(time.time() - t1) * 1000,
            ))
        elif resp.status_code != 200:
            results.append(HealthStatus(
                source="proxy:eastmoney",
                severity=Severity.WARNING,
                message=f"东财返回非200: {resp.status_code}",
                latency_ms=(time.time() - t1) * 1000,
            ))
        else:
            try:
                data = resp.json()
                d = data.get("data", {})
                if d and d.get("f57"):
                    results.append(HealthStatus(
                        source="proxy:eastmoney",
                        severity=Severity.OK,
                        message=f"东财API正常 (code={d.get('f57')})",
                        latency_ms=(time.time() - t1) * 1000,
                    ))
                else:
                    # 检查是否是反爬页面
                    text = resp.text[:200]
                    if "<!DOCTYPE" in text or "验证" in text:
                        results.append(HealthStatus(
                            source="proxy:eastmoney",
                            severity=Severity.WARNING,
                            message="代理IP可能被东财拦截(返回HTML非JSON)",
                            latency_ms=(time.time() - t1) * 1000,
                        ))
                    else:
                        results.append(HealthStatus(
                            source="proxy:eastmoney",
                            severity=Severity.WARNING,
                            message="东财返回空数据",
                            latency_ms=(time.time() - t1) * 1000,
                        ))
            except Exception:
                results.append(HealthStatus(
                    source="proxy:eastmoney",
                    severity=Severity.WARNING,
                    message="东财响应非JSON",
                    latency_ms=(time.time() - t1) * 1000,
                ))

    except Exception as e:
        results.append(HealthStatus(
            source="proxy",
            severity=Severity.CRITICAL,
            message=f"代理检查异常: {e}",
            latency_ms=(time.time() - t0) * 1000,
            detail={"error": str(e)},
        ))

    return results


# ═══════════════════════════════════════════════════════════
# 顶层接口
# ═══════════════════════════════════════════════════════════


def preflight_check() -> bool:
    """Pipeline 预检: 快速检查关键数据源, 不正常则阻塞执行。

    Returns:
        True: 可以安全执行
        False: 关键数据源异常, 应阻塞 pipeline

    检查范围: 仅 CRITICAL Tushare API + 代理可达性
    耗时: < 5s
    """
    # 1. 熔断检查
    if CircuitBreaker.is_active():
        state = CircuitBreaker.load()
        cb = state.get("circuit_breaker", {})
        logger.warning(
            "[预检] 熔断器激活中 → 阻塞 pipeline (触发原因: %s, 冷却至 %s)",
            cb.get("triggered_by", []), cb.get("cooldown_until", ""),
        )
        return False

    # 2. 关键检查
    critical_failures = []

    # Tushare CRITICAL API
    for api in TUSHARE_CRITICAL:
        status = _check_single_tushare(api, "critical")
        if status.severity == Severity.CRITICAL:
            critical_failures.append(f"tushare:{api}: {status.message}")

    # 代理 (不检查东财缓存, 因为 preflight 可能在 pipeline 填充缓存之前运行)
    proxy_results = check_proxy()
    for s in proxy_results:
        if s.severity == Severity.CRITICAL:
            critical_failures.append(f"{s.source}: {s.message}")

    # 3. 判定
    if critical_failures:
        CircuitBreaker.trip(critical_failures)
        alert_text = (
            f"🚨 [数据源告警] {datetime.now().strftime('%H:%M:%S')}\n"
            f"严重程度: CRITICAL\n"
            f"Pipeline 已阻塞\n"
            f"原因:\n  " + "\n  ".join(f"• {f}" for f in critical_failures)
        )
        _send_alert_sync(alert_text)
        logger.error("[预检] 不通过: %s", critical_failures)
        return False

    CircuitBreaker.reset()
    logger.info("[预检] 通过")
    return True


def run_health_check(full: bool = False, quiet: bool = False) -> HealthReport:
    """运行完整健康巡检 (cron / 手动调用)。

    Args:
        full: True=检查所有API含INFO级别, False=仅CRITICAL+WARNING
        quiet: True=不打印详细输出

    Returns:
        HealthReport
    """
    report = HealthReport()
    report.circuit_breaker_active = CircuitBreaker.is_active()

    # 1. Tushare
    report.checks.extend(check_tushare_apis(full=full))

    # 2. 东财缓存
    report.checks.extend(check_eastmoney_caches())

    # 3. Level2
    report.checks.extend(check_l2_connection())

    # 4. 代理
    report.checks.extend(check_proxy())

    # 汇总
    severities = [c.severity for c in report.checks]
    if Severity.CRITICAL in severities:
        report.overall = Severity.CRITICAL
    elif Severity.WARNING in severities:
        report.overall = Severity.WARNING

    # 持久化
    state = CircuitBreaker.load()
    history = state.setdefault("alert_history", [])
    history.append(report.to_dict())
    history[:] = history[-50:]  # 保留最近50条
    CircuitBreaker.save(state)

    # 告警
    if report.overall in (Severity.CRITICAL, Severity.WARNING):
        criticals = [c for c in report.checks if c.severity == Severity.CRITICAL]
        warnings = [c for c in report.checks if c.severity == Severity.WARNING]
        lines = [
            f"{'🚨' if report.overall == Severity.CRITICAL else '⚠️'} [巡检报告] {datetime.now().strftime('%H:%M:%S')}",
            f"总体: {report.overall.value}",
        ]
        if criticals:
            lines.append(f"CRITICAL ({len(criticals)}):")
            for c in criticals:
                lines.append(f"  • {c.source}: {c.message}")
        if warnings:
            lines.append(f"WARNING ({len(warnings)}):")
            for c in warnings[:5]:
                lines.append(f"  • {c.source}: {c.message}")
            if len(warnings) > 5:
                lines.append(f"  ... 及其他{len(warnings)-5}项")
        _send_alert_sync("\n".join(lines))

    # 输出
    if not quiet:
        print(f"\n{'='*60}")
        print(f"数据源健康巡检 {datetime.now().strftime('%Y-%m-%d %H:%M')}")
        print(f"总体: {report.overall.value} | 熔断: {'激活' if report.circuit_breaker_active else '正常'}")
        print(f"{'='*60}")
        for c in report.checks:
            icon = {"OK": "✅", "WARNING": "⚠️", "CRITICAL": "🚨"}[c.severity.value]
            print(f"  {icon} {c.source}: {c.message} ({c.latency_ms:.0f}ms)")
        print()

    return report


# ═══════════════════════════════════════════════════════════
# CLI 入口
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="数据源健康巡检")
    parser.add_argument("--full", action="store_true", help="全量巡检(含INFO级)")
    parser.add_argument("--quiet", action="store_true", help="静默模式")
    parser.add_argument("--preflight", action="store_true", help="仅预检(preflight_check)")
    args = parser.parse_args()

    if args.preflight:
        ok = preflight_check()
        print(f"预检: {'✅ 通过' if ok else '🚨 阻塞'}")
        sys.exit(0 if ok else 1)

    report = run_health_check(full=args.full, quiet=args.quiet)
    sys.exit(0 if report.overall != Severity.CRITICAL else 1)
