#!/usr/bin/env python3
"""
L2 数据守护进程 — 单例管理 L2 连接，多客户端通过 localhost TCP 共享。

解决: pipeline + watchdog 同账号多进程互踢导致 KICK
用法: python scripts/l2_daemon.py                    # 前台运行
      python scripts/l2_daemon.py --daemon            # 后台运行
"""

import socket
import threading
import time
import json
import sys
import os
import atexit
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

HOST = "127.0.0.1"
PORT = 18999  # 本地代理端口
PID_FILE = Path("/root/maneki-agent/plays/limit_up/data/.l2_daemon.pid")

# ── L2 客户端 (复用现有代码) ──
from scripts.l2_client import L2Client, normalize_code
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

ACCOUNT = os.getenv("L2API_ACCOUNT", "")
PASSWORD = os.getenv("L2API_PASSWORD", "")


class L2Proxy:
    """本地 L2 代理: 维护一个 L2Client, 接受多个本地客户端连接"""
    
    def __init__(self):
        self.l2 = L2Client(account=ACCOUNT, password=PASSWORD)
        self.l2.start()
        self.clients: list[socket.socket] = []
        self._lock = threading.Lock()
        self._running = True
        
    def broadcast(self, data: bytes):
        """广播 L2 数据给所有连接的客户端"""
        with self._lock:
            dead = []
            for c in self.clients:
                try:
                    c.sendall(data)
                except Exception:
                    dead.append(c)
            for c in dead:
                self.clients.remove(c)
    
    def handle_client(self, sock: socket.socket):
        """处理客户端连接: 接收订阅/查询请求, 返回数据"""
        sock.settimeout(1)
        buf = b""
        try:
            with self._lock:
                self.clients.append(sock)
            
            while self._running:
                try:
                    data = sock.recv(4096)
                    if not data:
                        break
                    buf += data
                    # 按换行分割命令
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        self._handle_command(sock, line.decode().strip())
                except socket.timeout:
                    continue
                except Exception:
                    break
        finally:
            with self._lock:
                if sock in self.clients:
                    self.clients.remove(sock)
            try:
                sock.close()
            except Exception:
                pass
    
    def _handle_command(self, sock, cmd: str):
        """处理客户端命令"""
        parts = cmd.split()
        if not parts:
            return
        
        action = parts[0].upper()
        
        if action == "SUB":
            codes = parts[1:]
            self.l2.subscribe([normalize_code(c) for c in codes])
            sock.sendall(b"OK SUB\n")
        
        elif action == "UNSUB":
            codes = parts[1:]
            self.l2.unsubscribe([normalize_code(c) for c in codes])
            sock.sendall(b"OK UNSUB\n")
        
        elif action == "MARKET":
            code = normalize_code(parts[1]) if len(parts) > 1 else ""
            max_age = float(parts[2]) if len(parts) > 2 else 120.0
            mkt = self.l2.get_market(code, max_age=max_age) if code else None
            sock.sendall((json.dumps(mkt) + "\n").encode() if mkt else b"NULL\n")
        
        elif action == "VWAP":
            code = normalize_code(parts[1]) if len(parts) > 1 else ""
            vwap = self.l2.get_vwap(code) if code else None
            sock.sendall(f"{vwap}\n".encode())
        
        elif action == "KLINE":
            code = normalize_code(parts[1]) if len(parts) > 1 else ""
            n = int(parts[2]) if len(parts) > 2 else 30
            kb = self.l2.get_minute_kline(code, n) if code else []
            sock.sendall((json.dumps(kb) + "\n").encode())
        
        elif action == "IS_READY":
            code = normalize_code(parts[1]) if len(parts) > 1 else ""
            ready = self.l2.is_ready(code) if code else False
            sock.sendall(f"{'1' if ready else '0'}\n".encode())
        
        elif action == "HEALTH":
            hs = self.l2.health_summary()
            sock.sendall((json.dumps(hs) + "\n").encode())
        
        elif action == "SUBSCRIBED":
            subs = list(self.l2.cache.get_subscribed())
            sock.sendall((json.dumps(subs) + "\n").encode())
        
        elif action == "PING":
            sock.sendall(b"PONG\n")
        
        else:
            sock.sendall(f"ERR unknown command: {action}\n".encode())
    
    def stop(self):
        self._running = False
        self.l2.stop()


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--daemon", action="store_true")
    args = parser.parse_args()
    
    if args.daemon:
        # 后台运行
        pid = os.fork()
        if pid > 0:
            print(f"L2 daemon started (PID={pid})")
            sys.exit(0)
        os.setsid()
    
    # 写 PID 文件
    PID_FILE.write_text(str(os.getpid()))
    atexit.register(lambda: PID_FILE.unlink(missing_ok=True))
    
    if ACCOUNT == "YOUR_ACCOUNT" or not ACCOUNT:
        print("❌ 请先在 .env 中配置 L2API_ACCOUNT / L2API_PASSWORD")
        sys.exit(1)
    
    proxy = L2Proxy()
    print(f"L2 daemon 启动, 监听 {HOST}:{PORT}")
    
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((HOST, PORT))
    server.listen(5)
    server.settimeout(1)

    # 收盘后(15:05)自动退出
    def _is_market_hours():
        from datetime import datetime
        now = datetime.now()
        hhmm = now.hour * 100 + now.minute
        return 925 <= hhmm <= 1505 and now.weekday() < 5

    try:
        while True:
            try:
                sock, addr = server.accept()
                t = threading.Thread(target=proxy.handle_client, args=(sock,), daemon=True)
                t.start()
            except socket.timeout:
                if not _is_market_hours():
                    print("收盘, L2 daemon 自动退出")
                    break
                continue
    except KeyboardInterrupt:
        print("\nL2 daemon 停止")
    finally:
        proxy.stop()
        server.close()


if __name__ == "__main__":
    main()
