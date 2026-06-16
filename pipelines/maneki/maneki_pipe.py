#!/usr/bin/env python3
"""Maneki 股票助手 — Claude SDK 管道

feishu webhook → 写入 inbox → Claude SDK 轮询 → 自主决策 → 回复

用法:
  python3 pipelines/maneki/maneki_pipe.py
"""

import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

import yaml
from claude_code_sdk import (
    ClaudeSDKClient,
    ClaudeCodeOptions,
    AssistantMessage,
    ResultMessage,
    TextBlock,
    PermissionResultAllow,
    PermissionResultDeny,
    tool,
    create_sdk_mcp_server,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PIPE_DIR = Path(__file__).resolve().parent
INBOX_DIR = PIPE_DIR / "inbox"
PROGRESS_FILE = PIPE_DIR / "progress.json"
CONFIG_FILE = PIPE_DIR / "config.yaml"

log = logging.getLogger("maneki_pipe")

# 防重复发送：每个 message_id 只发一次飞书消息
_REPLIED_MSGS: set[str] = set()

# ═══════════════════════════════════════════════════════════
# 配置
# ═══════════════════════════════════════════════════════════

DEFAULT_CONFIG = {
    "claude": {
        "model": None,
        "max_turns": None,
        "system_prompt_extra": "",
    },
    "poll_interval": 2.0,
    "feishu": {
        "app_id": "",
        "app_secret": "",
        "chat_ids": [],
    },
}


def load_config():
    config = dict(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        user = yaml.safe_load(CONFIG_FILE.read_text()) or {}
        _deep_merge(config, user)
    # Load feishu credentials from .env
    from dotenv import load_dotenv
    load_dotenv(REPO_ROOT / ".env")
    config["feishu"]["app_id"] = os.getenv("FEISHU_BOT_APP_ID", "")
    config["feishu"]["app_secret"] = os.getenv("FEISHU_BOT_APP_SECRET", "")
    return config


def _deep_merge(base, override):
    for k, v in override.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


# ═══════════════════════════════════════════════════════════
# Progress
# ═══════════════════════════════════════════════════════════

class Progress:
    def __init__(self):
        self._data = {}
        if PROGRESS_FILE.exists():
            self._data = json.loads(PROGRESS_FILE.read_text())

    def save(self, **kwargs):
        self._data.update({k: v for k, v in kwargs.items() if v is not None})
        PROGRESS_FILE.write_text(json.dumps(self._data, ensure_ascii=False, indent=2))

    @property
    def session_id(self):
        return self._data.get("session_id")

    @property
    def positions(self):
        return self._data.get("positions", {})


# ═══════════════════════════════════════════════════════════
# Inbox
# ═══════════════════════════════════════════════════════════

def read_inbox(chat_id: str, position: int) -> tuple[list[dict], int]:
    """读取指定群聊的未读消息"""
    file = INBOX_DIR / f"{chat_id}.jsonl"
    if not file.exists():
        return [], 0

    messages = []
    new_pos = position
    with open(file) as f:
        f.seek(position)
        while True:
            line = f.readline()
            if not line:
                new_pos = f.tell()
                break
            line = line.strip()
            if not line:
                new_pos = f.tell()
                continue
            try:
                messages.append(json.loads(line))
            except json.JSONDecodeError:
                pass
            new_pos = f.tell()
    return messages, new_pos


def write_inbox(chat_id: str, message: dict):
    """写入消息到 inbox"""
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    file = INBOX_DIR / f"{chat_id}.jsonl"
    with open(file, "a") as f:
        f.write(json.dumps(message, ensure_ascii=False) + "\n")


# ═══════════════════════════════════════════════════════════
# Feishu MCP Tools
# ═══════════════════════════════════════════════════════════

class FeishuAPI:
    def __init__(self, app_id: str, app_secret: str):
        self.app_id = app_id
        self.app_secret = app_secret
        self._token = None
        self._expires = 0

    async def get_token(self):
        if self._token and time.time() < self._expires:
            return self._token
        import httpx
        async with httpx.AsyncClient(timeout=10) as c:
            resp = await c.post(
                "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": self.app_id, "app_secret": self.app_secret},
            )
            data = resp.json()
            self._token = data["tenant_access_token"]
            self._expires = time.time() + data.get("expire", 7200) - 60
            return self._token

    async def send_text(self, chat_id: str, content: str):
        token = await self.get_token()
        import httpx
        async with httpx.AsyncClient(timeout=15) as c:
            resp = await c.post(
                "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
                headers={"Authorization": f"Bearer {token}"},
                json={"receive_id": chat_id, "msg_type": "text",
                      "content": json.dumps({"text": content})},
            )
            return resp.json()

    async def send_card(self, chat_id: str, card: dict):
        token = await self.get_token()
        import httpx
        async with httpx.AsyncClient(timeout=15) as c:
            resp = await c.post(
                "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
                headers={"Authorization": f"Bearer {token}"},
                json={"receive_id": chat_id, "msg_type": "interactive",
                      "content": json.dumps(card)},
            )
            return resp.json()

    async def reply_text(self, message_id: str, content: str):
        """回复指定消息（产生已读状态 + 线程回复）"""
        token = await self.get_token()
        import httpx
        async with httpx.AsyncClient(timeout=15) as c:
            resp = await c.post(
                f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reply",
                headers={"Authorization": f"Bearer {token}"},
                json={"msg_type": "text", "content": json.dumps({"text": content})},
            )
            return resp.json()

    async def reply_card(self, message_id: str, card: dict):
        """回复卡片消息"""
        token = await self.get_token()
        import httpx
        async with httpx.AsyncClient(timeout=15) as c:
            resp = await c.post(
                f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reply",
                headers={"Authorization": f"Bearer {token}"},
                json={"msg_type": "interactive", "content": json.dumps(card)},
            )
            return resp.json()


def build_feishu_mcp(feishu: FeishuAPI):
    @tool("send_feishu_text", "回复文本消息到飞书群聊（带message_id则作为回复，否则发送到群聊）", {
        "type": "object",
        "properties": {
            "chat_id": {"type": "string", "description": "群聊ID"},
            "content": {"type": "string", "description": "消息内容"},
            "message_id": {"type": "string", "description": "回复的消息ID（可选，提供则回复到该消息下方）"},
        },
        "required": ["chat_id", "content"],
    })
    async def send_feishu_text(args):
        if args.get("message_id"):
            result = await feishu.reply_text(args["message_id"], args["content"])
        else:
            result = await feishu.send_text(args["chat_id"], args["content"])
        ok = result.get("code") == 0
        return {"content": [{"type": "text", "text": "已发送" if ok else f"发送失败: {result}"}]}

    @tool("send_feishu_markdown", "回复Markdown消息到飞书群聊（带message_id则作为回复，否则发送到群聊）", {
        "type": "object",
        "properties": {
            "chat_id": {"type": "string", "description": "群聊ID"},
            "content": {"type": "string", "description": "Markdown内容"},
            "message_id": {"type": "string", "description": "回复的消息ID（可选，提供则回复到该消息下方）"},
        },
        "required": ["chat_id", "content"],
    })
    async def send_feishu_markdown(args):
        msg_id = args.get("message_id", "")[:20]
        chat_id = args.get("chat_id", "")[:20]
        log.info("MCP send_feishu_markdown called: chat=%s msg=%s len=%d",
                 chat_id, msg_id, len(args.get("content", "")))
        # 防重复：同一 message_id 只发一次
        mid = args.get("message_id", "")
        if mid and mid in _REPLIED_MSGS:
            log.info("dedup: already replied to %s, skip", mid[:20])
            return {"content": [{"type": "text", "text": "已发送"}]}
        _REPLIED_MSGS.add(mid)
        token = await feishu.get_token()
        import httpx
        import json
        payload = {
            "config": {"wide_screen_mode": True},
            "elements": [{"tag": "markdown", "content": args["content"]}],
        }
        if args.get("message_id"):
            async with httpx.AsyncClient(timeout=15) as c:
                resp = await c.post(
                    f"https://open.feishu.cn/open-apis/im/v1/messages/{args['message_id']}/reply",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"msg_type": "interactive", "content": json.dumps(payload)},
                )
            result = resp.json()
            ok = result.get("code") == 0
            return {"content": [{"type": "text", "text": "已发送" if ok else f"发送失败: {result}"}]}
        else:
            async with httpx.AsyncClient(timeout=15) as c:
                resp = await c.post(
                    "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"receive_id": args["chat_id"], "msg_type": "interactive",
                          "content": json.dumps(payload)},
                )
            result = resp.json()
            ok = result.get("code") == 0
            return {"content": [{"type": "text", "text": "已发送" if ok else f"发送失败: {result}"}]}

    @tool("send_feishu_card", "回复卡片消息到飞书群聊（带message_id则作为回复，否则发送到群聊）", {
        "type": "object",
        "properties": {
            "chat_id": {"type": "string", "description": "群聊ID"},
            "card": {"type": "object", "description": "飞书卡片 JSON"},
            "message_id": {"type": "string", "description": "回复的消息ID（可选，提供则回复到该消息下方）"},
        },
        "required": ["chat_id", "card"],
    })
    async def send_feishu_card(args):
        if args.get("message_id"):
            result = await feishu.reply_card(args["message_id"], args["card"])
        else:
            result = await feishu.send_card(args["chat_id"], args["card"])
        ok = result.get("code") == 0
        return {"content": [{"type": "text", "text": "已发送" if ok else f"发送失败: {result}"}]}

    return create_sdk_mcp_server(
        name="feishu", version="1.0.0",
        tools=[send_feishu_text, send_feishu_markdown, send_feishu_card],
    )


# ═══════════════════════════════════════════════════════════
# System Prompt
# ═══════════════════════════════════════════════════════════

def build_system_prompt(inbox_dir: str) -> str:
    return f"""你是 Maneki A股量化助手，通过飞书群聊与用户交互。

## 你的能力

1. **分析股票(V1评分)** — 运行 `python plays/limit_up/pipeline.py` 获取五维度评分
2. **分析股票(V2信号)** — 运行 `python plays/limit_up/pipeline_v2.py` 用信号模式分析涨停候选股
3. **盯盘管理** — 运行 `python plays/watchdog/watchdog.py` 管理盯盘
4. **知识查询** — 读取 `wiki/` 目录搜索A股概念和术语
5. **信号优化** — 运行 `python plays/limit_up/optimize_v2.py --days 14` 优化信号阈值

## 回应方式

- 回复时**必须**带上 `message_id` 参数，否则回复会变成群聊独立消息而不是在原消息下方
- 只能用 `send_feishu_markdown` 发送格式化内容（包括分析结果、评分报告等所有回复）
- 每次消息只发送一次回复，不要多次发送

## 群聊消息格式

每条消息格式: [chat_id:xxx] [message_id:xxx] [用户] 消息内容

## 消息历史

消息文件在 {inbox_dir}/{{chat_id}}.jsonl，需要上下文时可以读取。

## 行为准则

- 用户问股票 → 分析并给出评分
- 用户说"盯"/"停" → 管理盯盘
- 用户问概念 → 查 wiki 回答
- 闲聊 → 友好回复
- 记住上下文：用户追问时基于之前的分析结果回答"""


# ═══════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════

async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    config = load_config()
    claude_cfg = config["claude"]
    feishu_cfg = config["feishu"]

    if not feishu_cfg["app_id"]:
        log.error("未配置 FEISHU_BOT_APP_ID，请在 .env 中设置")
        sys.exit(1)

    progress = Progress()
    feishu = FeishuAPI(feishu_cfg["app_id"], feishu_cfg["app_secret"])
    feishu_mcp = build_feishu_mcp(feishu)

    # Collect all chat_ids from inbox and config
    known_chats = set(feishu_cfg.get("chat_ids", []))
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    for f in INBOX_DIR.glob("*.jsonl"):
        known_chats.add(f.stem)

    if not known_chats:
        log.warning("没有已知群聊，等待飞书消息...")

    system_prompt = build_system_prompt(str(INBOX_DIR))
    if claude_cfg.get("system_prompt_extra"):
        system_prompt += "\n\n" + claude_cfg["system_prompt_extra"]

    # Allowed tools: from config, with feishu MCP auto-injected
    allowed_tools = list(config.get("allowed_tools", [
        "Read", "Write", "Edit", "Glob", "Grep", "Bash", "Skill",
    ]))
    allowed_tools += [
        "mcp__feishu__send_feishu_text",
        "mcp__feishu__send_feishu_markdown",
        "mcp__feishu__send_feishu_card",
    ]

    async def auto_approve(tool_name, tool_input, context):
        if tool_name in allowed_tools:
            return PermissionResultAllow()
        return PermissionResultDeny(message=f"Tool {tool_name} not allowed")

    poll = config.get("poll_interval", 2.0)

    while True:
        # 每次循环重新读取 progress（超时后 session_id 可能已重置）
        progress = Progress()
        options = ClaudeCodeOptions(
            allowed_tools=allowed_tools,
            max_turns=claude_cfg.get("max_turns"),
            model=claude_cfg.get("model"),
            append_system_prompt=system_prompt,
            can_use_tool=auto_approve,
            resume=progress.session_id,
            mcp_servers={"feishu": feishu_mcp},
        )

        async with ClaudeSDKClient(options=options) as client:
            if progress.session_id:
                log.info("resuming session: %s", progress.session_id[:12])
            log.info("maneki pipe started, polling inbox every %.1fs", poll)

            restart_client = False

            while not restart_client:
                # 每次循环刷新已知群聊（支持运行时新增）
                for f in INBOX_DIR.glob("*.jsonl"):
                    known_chats.add(f.stem)
                for chat_id in list(known_chats):
                    # 兼容旧格式(int)和新格式([position, inode])
                    raw_pos = progress.positions.get(chat_id, 0)
                    if isinstance(raw_pos, list):
                        pos, last_inode = raw_pos[0], raw_pos[1]
                    else:
                        pos, last_inode = raw_pos, None

                    fpath = INBOX_DIR / f"{chat_id}.jsonl"
                    current_inode = fpath.stat().st_ino if fpath.exists() else None

                    # 检测文件是否被替换（inode 变化或文件大小 <= 旧位置）
                    if pos > 0 and fpath.exists():
                        if last_inode is not None and current_inode != last_inode:
                            log.info("file replaced (inode changed) for %s, reset position from %d to 0",
                                     chat_id, pos)
                            pos = 0
                        elif fpath.stat().st_size <= pos:
                            log.info("file replaced (size %d <= pos %d) for %s, reset position to 0",
                                     fpath.stat().st_size, pos, chat_id)
                            pos = 0

                    messages, new_pos = read_inbox(chat_id, pos)

                    # 🐛 FIX: 保存 position 必须在处理消息之前，
                    # 否则 Claude 处理期间下一轮 poll 会重复读取同一条消息，造成无限回复
                    if new_pos > pos:
                        progress.save(
                            session_id=progress.session_id,
                            positions={**progress.positions, chat_id: [new_pos, current_inode]},
                        )

                    for msg in messages:
                        text = msg.get("text", "")
                        sender = msg.get("sender", "unknown")
                        message_id = msg.get("message_id", "")
                        if not text:
                            continue

                        prompt = f"[chat_id:{chat_id}] [message_id:{message_id}] [{sender}] {text}"
                        log.info("msg: %s", prompt[:120])
                        log.info("query start: chat=%s turn=%s", chat_id, progress.session_id[:12] if progress.session_id else "new")

                        await client.query(prompt)

                        # 响应超时兜底：为 _collect 创建独立 task, 超时后 cancel + 重启 client
                        async def _collect():
                            turn_count = 0
                            final_session_id = None
                            try:
                                async for message in client.receive_response():
                                    turn_count += 1
                                    msg_type = type(message).__name__
                                    if isinstance(message, AssistantMessage):
                                        has_tool = any(
                                            getattr(b, 'type', None) == "tool_use"
                                            for b in (message.content or [])
                                        )
                                        texts = [
                                            getattr(b, 'text', '')[:60]
                                            for b in (message.content or [])
                                            if hasattr(b, 'text')
                                        ]
                                        log.info("turn%d: AssistantMessage tools=%s text=%s",
                                                 turn_count, has_tool, texts[0] if texts else "")
                                    elif isinstance(message, ResultMessage):
                                        if not message.is_error:
                                            final_session_id = message.session_id
                                            log.info("turn%d: ResultMessage OK turns=%d", turn_count, message.num_turns)
                                        else:
                                            log.error("turn%d: ResultMessage ERROR %s", turn_count, message.subtype)
                                    else:
                                        log.info("turn%d: %s", turn_count, msg_type)
                            except asyncio.CancelledError:
                                log.warning("_collect cancelled (timeout)")
                                raise
                            return turn_count, final_session_id

                        task = asyncio.create_task(_collect())
                        done, pending = await asyncio.wait({task}, timeout=120)
                        if pending:
                            task.cancel()
                            log.error("query timeout 120s for chat=%s, restarting claude", chat_id)
                            # 放弃旧 session, 退出 context manager 杀死 claude 进程
                            fpath2 = INBOX_DIR / f"{chat_id}.jsonl"
                            cur_ino = fpath2.stat().st_ino if fpath2.exists() else None
                            progress.save(
                                session_id=None,
                                positions={**progress.positions, chat_id: [new_pos, cur_ino]},
                            )
                            restart_client = True
                            break
                        else:
                            turns, sid = task.result()
                            if sid:
                                fpath2 = INBOX_DIR / f"{chat_id}.jsonl"
                                cur_ino = fpath2.stat().st_ino if fpath2.exists() else None
                                progress.save(
                                    session_id=sid,
                                    positions={**progress.positions, chat_id: [new_pos, cur_ino]},
                                )
                            log.info("query done: chat=%s turns=%d", chat_id, turns)

                    if restart_client:
                        break

                if restart_client:
                    break

                await asyncio.sleep(poll)

                # 清理已读完的 inbox 文件
                for chat_id in list(known_chats):
                    raw_pos = progress.positions.get(chat_id, 0)
                    pos = raw_pos[0] if isinstance(raw_pos, list) else raw_pos
                    if pos > 0:
                        fpath = INBOX_DIR / f"{chat_id}.jsonl"
                        if fpath.exists() and fpath.stat().st_size == pos:
                            fpath.unlink()
                            log.info("cleaned inbox for %s", chat_id)

        if restart_client:
            log.info("recreating Claude SDK client...")
            continue  # 外层 while → 重新创建 client
        else:
            break  # 正常退出（不会发生）


if __name__ == "__main__":
    asyncio.run(main())