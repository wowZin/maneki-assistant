#!/usr/bin/env python3
"""maneki-pipe — 统一飞书 webhook + Claude Pipe 服务

架构：
  - 单个 Python 进程
  - FastAPI 收飞书 webhook（返回 202，不阻塞）
  - 异步队列 → claude CLI stdin（pipe 模式）
  - claude stdout → 飞书回复
  - systemd 自动保活

用法：
  python3 -m uvicorn pipelines.maneki.maneki_pipe:app --host 0.0.0.0 --port 8080
  # 或直接 python3 pipelines/maneki/maneki_pipe.py（自带 uvicorn）
"""
import asyncio
import json
import logging
import os
import subprocess
import sys
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import psutil
import yaml
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PIPE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = PIPE_DIR / "config.yaml"
INBOX_DIR = PIPE_DIR / "inbox"
PROGRESS_FILE = PIPE_DIR / "progress.json"

log = logging.getLogger("maneki_pipe")

# ═══════════════════════════════════════════════════════════
# 配置
# ═══════════════════════════════════════════════════════════

DEFAULT_CONFIG = {
    "claude": {
        "model": None,
        "max_turns": 40,
        "system_prompt_extra": "",
    },
    "feishu": {
        "app_id": "",
        "app_secret": "",
    },
    "claude_bin": "claude",
}

def load_config() -> dict:
    config = dict(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        user = yaml.safe_load(CONFIG_FILE.read_text()) or {}
        _deep_merge(config, user)
    load_dotenv(REPO_ROOT / ".env")
    if not config["feishu"]["app_id"]:
        config["feishu"]["app_id"] = os.getenv("FEISHU_BOT_APP_ID", "")
    if not config["feishu"]["app_secret"]:
        config["feishu"]["app_secret"] = os.getenv("FEISHU_BOT_APP_SECRET", "")
    return config

def _deep_merge(base, override):
    for k, v in override.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v

# ═══════════════════════════════════════════════════════════
# 飞书 API
# ═══════════════════════════════════════════════════════════

class FeishuAPI:
    def __init__(self, app_id: str, app_secret: str):
        self.app_id = app_id
        self.app_secret = app_secret
        self._token = None
        self._expires = 0

    async def _ensure_token(self) -> str:
        if self._token and time.time() < self._expires:
            return self._token
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(
                "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": self.app_id, "app_secret": self.app_secret},
            )
            data = r.json()
            self._token = data["tenant_access_token"]
            self._expires = time.time() + data.get("expire", 7200) - 60
            return self._token

    async def reply_markdown(self, message_id: str, text: str, dedup_set: set):
        """回复 markdown 卡片到飞书消息（产生已读+线程）。"""
        if message_id in dedup_set:
            log.info("dedup: already replied to %s", message_id[:16])
            return True
        dedup_set.add(message_id)
        token = await self._ensure_token()
        card = {
            "config": {"wide_screen_mode": True},
            "elements": [{"tag": "markdown", "content": text}],
        }
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(
                f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reply",
                headers={"Authorization": f"Bearer {token}"},
                json={"msg_type": "interactive", "content": json.dumps(card)},
            )
        ok = r.json().get("code") == 0
        if not ok:
            log.warning("reply failed: %s", r.text[:200])
        return ok

    async def send_markdown(self, chat_id: str, text: str):
        """发送 markdown 卡片到群聊。"""
        token = await self._ensure_token()
        card = {
            "config": {"wide_screen_mode": True},
            "elements": [{"tag": "markdown", "content": text}],
        }
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(
                "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
                headers={"Authorization": f"Bearer {token}"},
                json={"receive_id": chat_id, "msg_type": "interactive",
                      "content": json.dumps(card)},
            )
        ok = r.json().get("code") == 0
        if not ok:
            log.warning("send failed: %s", r.text[:200])
        return ok


# ═══════════════════════════════════════════════════════════
# Claude Pipe 管理
# ═══════════════════════════════════════════════════════════

SYSTEM_PROMPT = """你是 Maneki A股量化助手，通过飞书群聊与用户交互。你负责盯盘、分析股票、查询知识，也具备完整的代码读写能力。

## 你的能力

### 1. 股票分析
运行 `python plays/limit_up/pipeline.py` 获取五维度评分（基本面/技术面/资金面/情绪面/短线博弈）

### 2. 盯盘管理
运行 `python plays/watchdog/watchdog.py` 管理盯盘（使用 jvQuant 实时行情）

### 3. 知识查询
- 读取 `wiki/` 目录搜索 A 股概念和术语
- 知识库按 `wiki/concepts/`（跨玩法通用）和 `wiki/plays/xxx/entities/`（每日编译）组织

### 4. 回测与优化
- `python plays/limit_up/backtest/backtest.py --days 20` — 回测最近 N 个交易日
- 数据源：`wiki/raw/limit-up/analysis/`（历史归档）+ `plays/limit_up/data/analysis/`（当日）

### 5. 代码开发（你有完整的 Bash/Read/Write/Edit/Glob/Grep/Skill 工具）
- **读写文件** — Read/Write/Edit（注意 Python 模块名用下划线不用连字符）
- **搜索代码** — Glob/Grep 查找函数、类、引用
- **Git 操作** — 用 Bash 执行 git add/commit/push/fetch/checkout
- **运行测试** — Bash 执行 pytest
- **Skills** — 用 Skill 工具查看和加载项目技巧（如 `skill_view(name)` 加载已有 skill）

### 6. 回复方式
- 你的回复会自动发送到飞书群聊，直接输出文本即可
- 可以先简要描述你准备做什么，然后执行
- 对非股票问题也能友好回复

## 消息格式
每条消息格式: [chat_id:xxx] [message_id:xxx] [用户] 消息内容

## 行为准则
- 用户问股票 → 分析并给出评分
- 用户说"盯"/"停" → 管理盯盘
- 用户问概念 → 查 wiki 回答
- 用户要求改代码 → 先读代码理解，再改，改完后 git add/commit/push
- 用户问项目相关 → 查代码或 wiki 回答
- 闲聊 → 友好回复
"""


class ClaudePipe:
    """管理 claude CLI 子进程的 one-shot pipe。每次 query 起新进程，用完即毁。"""

    def __init__(self, config: dict):
        self.config = config
        self._loop = asyncio.get_event_loop()

    def _build_args(self) -> list[str]:
        claude_cfg = self.config["claude"]
        args = [
            self.config.get("claude_bin", "claude"),
            "--append-system-prompt", SYSTEM_PROMPT,
            "--allowedTools",
            "Read,Write,Edit,Glob,Grep,Bash,Skill,"
            "mcp__feishu__send_feishu_text,"
            "mcp__feishu__send_feishu_markdown,"
            "mcp__feishu__send_feishu_card",
            "--permission-prompt-tool", "stdio",
        ]
        if claude_cfg.get("max_turns"):
            args.extend(["--max-turns", str(claude_cfg["max_turns"])])
        return args

    async def query(self, text: str, chat_id: str, message_id: str,
                    feishu: FeishuAPI, dedup_set: set) -> None:
        """one-shot：起 claude → 写 query → 关 stdin → 读 stdout → 析构。"""
        args = self._build_args()
        log.info("spawning claude for query: %s", text[:60])

        global _current_proc
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(REPO_ROOT),
        )
        _current_proc = proc

        prompt = f"[chat_id:{chat_id}] [message_id:{message_id}] [用户] {text}"
        # 写 query + 关 stdin（Claude 需要 EOF 才开始处理）
        proc.stdin.write((prompt + "\n").encode())
        await proc.stdin.drain()
        proc.stdin.close()

        # 读全部 stdout
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=180)
        except asyncio.TimeoutError:
            log.warning("claude timeout (180s), killing")
            proc.kill()
            await proc.wait()
            raise
        finally:
            _current_proc = None
        output = stdout.decode().strip()
        log.info("claude done: %d chars output", len(output))

        # 发送到飞书
        if output:
            await feishu.reply_markdown(message_id, output, dedup_set)


# ═══════════════════════════════════════════════════════════
# FastAPI App
# ═══════════════════════════════════════════════════════════

config = load_config()
feishu = FeishuAPI(config["feishu"]["app_id"], config["feishu"]["app_secret"])
_dedup: set[str] = set()            # 回复去重（运行时，被杀/重启后清零）
_seen_msg_ids: set[str] = set()     # 已处理消息ID（持久化，防重启重复）
_queue: asyncio.Queue = asyncio.Queue()
_current_proc: asyncio.subprocess.Process | None = None  # 当前正在跑的 claude 进程

# 加载持久化去重
SEEN_FILE = PIPE_DIR / "inbox" / "seen_ids.json"
if SEEN_FILE.exists():
    try:
        data = json.loads(SEEN_FILE.read_text())
        _seen_msg_ids = set(data.get("ids", []))
        log.info("loaded %d seen message_ids from disk", len(_seen_msg_ids))
    except Exception:
        pass


def _persist_seen():
    """持久化已处理消息ID到磁盘。"""
    try:
        SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        SEEN_FILE.write_text(json.dumps({"ids": list(_seen_msg_ids)[-5000:]}, ensure_ascii=False))
    except Exception:
        pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用启动：启动队列消费者。每个 query 起独立 claude 进程。"""
    asyncio.create_task(_process_queue())
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok", "queue": _queue.qsize()}


@app.post("/webhook/feishu")
@app.post("/feishu/callback")
async def feishu_webhook(req: Request):
    """飞书 webhook 入口。立即返回 202，异步入队处理。"""
    body = await req.json()
    header = body.get("header", {})
    event = body.get("event", {})

    # 只处理 im.message.receive_v1
    if header.get("event_type") != "im.message.receive_v1":
        return JSONResponse({"challenge": body.get("challenge", "")} if "challenge" in body else {"ok": True})

    sender = event.get("sender", {}).get("sender_id", {}).get("open_id", "")
    chat_id = event.get("message", {}).get("chat_id", "")
    message_id = event.get("message", {}).get("message_id", "")
    msg_type = event.get("message", {}).get("message_type", "")
    content_raw = event.get("message", {}).get("content", "{}")

    if msg_type != "text":
        return JSONResponse({"ok": True})

    # 过滤机器人自身的消息，防止循环
    sender = event.get("sender", {})
    if sender.get("sender_type") == "app":
        return JSONResponse({"code": 0})

    # 过滤超时的旧消息（超过 5 分钟的丢弃，防止重启后飞书重发旧事件）
    msg_time_ms = header.get("create_time", 0)
    if msg_time_ms and (time.time() * 1000 - int(msg_time_ms)) > 300_000:
        return JSONResponse({"ok": True})

    # 持久化去重：已处理过的 message_id 不再处理（防重启后飞书重发旧事件）
    if message_id in _seen_msg_ids:
        return JSONResponse({"ok": True})
    _seen_msg_ids.add(message_id)
    _persist_seen()

    try:
        content = json.loads(content_raw)
        text = content.get("text", "")
    except (json.JSONDecodeError, TypeError):
        text = content_raw

    if not text:
        return JSONResponse({"ok": True})

    # 斜杠命令：不走 Claude，直接处理
    if text.startswith("/"):
        asyncio.create_task(_handle_slash_command(text, chat_id, message_id))
        return JSONResponse({"ok": True}, status_code=202)

    await _queue.put({
        "text": text,
        "sender": sender,
        "chat_id": chat_id,
        "message_id": message_id,
    })
    return JSONResponse({"ok": True}, status_code=202)


async def _process_queue():
    """后台任务：从队列取消息 → 起 claude 进程 → 回复。"""
    while True:
        item = await _queue.get()
        cp = ClaudePipe(config)
        try:
            await cp.query(
                text=item["text"],
                chat_id=item["chat_id"],
                message_id=item["message_id"],
                feishu=feishu,
                dedup_set=_dedup,
            )
            log.info("query done: chat=%s", item["chat_id"][:16])
        except Exception as e:
            log.error("query error: %s", e, exc_info=True)
        finally:
            _queue.task_done()


def _save_session(session_id: str):
    try:
        PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
        PROGRESS_FILE.write_text(json.dumps({"session_id": session_id}))
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════
# 斜杠命令
# ═══════════════════════════════════════════════════════════

async def _handle_slash_command(text: str, chat_id: str, message_id: str):
    """处理斜杠命令，不走 Claude。"""
    cmd = text.strip().split()
    name = cmd[0].lower()

    if name == "/status":
        mem = psutil.Process().memory_info().rss // 1024 // 1024
        reply = (
            f"**Maneki 状态**\n"
            f"├ 进程: ✅ 运行中\n"
            f"├ 内存: {mem}MB\n"
            f"├ 队列: {_queue.qsize()} 待处理\n"
            f"└ Uptime: `systemctl status maneki-pipe`"
        )
        await feishu.reply_markdown(message_id, reply, _dedup)

    elif name == "/log":
        n = 20
        if len(cmd) > 1 and cmd[1].isdigit():
            n = min(int(cmd[1]), 200)
        try:
            result = subprocess.run(
                ["journalctl", "-u", "maneki-pipe", "-n", str(n), "--no-pager", "-q"],
                capture_output=True, text=True, timeout=5,
            )
            lines = result.stdout.strip().split("\n")
            if not lines or all(not l.strip() for l in lines):
                reply = "暂无日志"
            else:
                # 只保留最后 n 行，太长截断
                shown = lines[-min(n, 40):]
                text = "\n".join(shown)
                if len(text) > 1500:
                    text = text[-1500:]
                reply = f"**最近日志 ({len(shown)}行)**\n```\n{text}\n```"
        except Exception as e:
            reply = f"读取日志失败: {e}"
        await feishu.reply_markdown(message_id, reply, _dedup)

    elif name == "/reset":
        log.info("slash /reset: killing current claude + clearing queue")
        # 杀掉当前正在跑的 claude 进程
        proc = _current_proc
        if proc and proc.returncode is None:
            proc.kill()
            log.info("  killed claude PID=%d", proc.pid)
        # 清空队列
        while not _queue.empty():
            try:
                _queue.get_nowait()
                _queue.task_done()
            except asyncio.QueueEmpty:
                break
        _dedup.clear()
        reply = "🔄 已终止当前处理，队列已清空"
        await feishu.reply_markdown(message_id, reply, _dedup)

    else:
        reply = f"未知命令: {name}。支持: `/status` `/log [N]` `/reset`"
        await feishu.reply_markdown(message_id, reply, _dedup)


# ═══════════════════════════════════════════════════════════
# 独立启动入口（不依赖外部 uvicorn）
# ═══════════════════════════════════════════════════════════

def main():
    import uvicorn
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    log.info("maneki-pipe starting on :8080")
    log.info("feishu webhook: POST /webhook/feishu")
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="info")


if __name__ == "__main__":
    main()
