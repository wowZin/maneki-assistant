#!/usr/bin/env python3
"""L2 守护进程 TCP 客户端

用法:
    from scripts.l2_daemon_client import daemon_alive, daemon_get_market, ...

所有函数通过 socket 连接 L2 守护进程 (127.0.0.1:18999) 通信，
替代 scripts.l2_client 的直连方式，解决多进程互踢问题。
"""

import json
import socket
from typing import Optional

_HOST = "127.0.0.1"
_PORT = 18999


def daemon_cmd(cmd: str, timeout: int = 5) -> str:
    """向 L2 守护进程发送命令, 返回响应字符串"""
    s = socket.create_connection((_HOST, _PORT), timeout=timeout)
    s.sendall((cmd + "\n").encode())
    resp = s.recv(32768).decode().strip()
    s.close()
    return resp


def daemon_alive() -> bool:
    try:
        return daemon_cmd("PING", timeout=2) == "PONG"
    except Exception:
        return False


def daemon_subscribe(codes: list[str]):
    """订阅标的"""
    if codes:
        daemon_cmd(f"SUB {' '.join(codes)}")


def daemon_unsubscribe(codes: list[str]):
    """退订标的"""
    if codes:
        daemon_cmd(f"UNSUB {' '.join(codes)}")


def daemon_subscribed() -> list[str]:
    """获取当前订阅列表"""
    resp = daemon_cmd("SUBSCRIBED")
    if resp:
        try:
            return json.loads(resp)
        except Exception:
            pass
    return []


def daemon_get_market(code: str) -> Optional[dict]:
    """获取个股实时行情"""
    resp = daemon_cmd(f"MARKET {code}")
    if resp == "NULL":
        return None
    try:
        return json.loads(resp)
    except Exception:
        return None


def daemon_get_vwap(code: str) -> Optional[float]:
    """获取 VWAP"""
    resp = daemon_cmd(f"VWAP {code}")
    try:
        return float(resp)
    except (ValueError, TypeError):
        return None


def daemon_get_kline(code: str, n: int = 30) -> list:
    """获取最近 n 根分钟 K 线"""
    resp = daemon_cmd(f"KLINE {code} {n}")
    if resp == "NULL" or not resp:
        return []
    try:
        return json.loads(resp)
    except Exception:
        return []


def daemon_is_ready(code: str) -> bool:
    """检查标的行情数据是否就绪"""
    return daemon_cmd(f"IS_READY {code}") == "1"


def daemon_health() -> dict:
    """获取守护进程健康状态"""
    resp = daemon_cmd("HEALTH")
    try:
        return json.loads(resp)
    except Exception:
        return {}


def daemon_is_healthy() -> bool:
    """守护进程是否健康"""
    h = daemon_health()
    return h.get("healthy", False)
