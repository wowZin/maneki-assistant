#!/usr/bin/env python3
"""Maneki 股票助手 — Claude SDK 管道

feishu webhook → 写入 inbox → Claude SDK 轮询 → 自主决策 → 回复

用法:
  python3 pipes/maneki/maneki_pipe.py
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
    config["feishu"]["app_id"] = os.getenv("FEISHU_APP_ID", "")
    config["feishu"]["app_secret"] = os.getenv("FEISHU_APP_SECRET", "")
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
        for line in f:
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


def build_feishu_mcp(feishu: FeishuAPI):
    @tool("send_feishu_text", "发送文本消息到飞书群聊", {
        "type": "object",
        "properties": {
            "chat_id": {"type": "string", "description": "群聊ID"},
            "content": {"type": "string", "description": "消息内容"},
        },
        "required": ["chat_id", "content"],
    })
    async def send_feishu_text(args):
        result = await feishu.send_text(args["chat_id"], args["content"])
        ok = result.get("code") == 0
        return {"content": [{"type": "text", "text": "已发送" if ok else f"发送失败: {result}"}]}

    @tool("send_feishu_markdown", "发送Markdown消息到飞书群聊", {
        "type": "object",
        "properties": {
            "chat_id": {"type": "string", "description": "群聊ID"},
            "content": {"type": "string", "description": "Markdown内容"},
        },
        "required": ["chat_id", "content"],
    })
    async def send_feishu_markdown(args):
        token = await feishu.get_token()
        import httpx
        import json
        async with httpx.AsyncClient(timeout=15) as c:
            resp = await c.post(
                "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
                headers={"Authorization": f"Bearer {token}"},
                json={"receive_id": args["chat_id"], "msg_type": "interactive",
                      "content": json.dumps({
                          "config": {"wide_screen_mode": True},
                          "elements": [{"tag": "markdown", "content": args["content"]}],
                      })},
            )
            result = resp.json()
            ok = result.get("code") == 0
            return {"content": [{"type": "text", "text": "已发送" if ok else f"发送失败: {result}"}]}

    @tool("send_feishu_card", "发送卡片消息到飞书群聊", {
        "type": "object",
        "properties": {
            "chat_id": {"type": "string", "description": "群聊ID"},
            "card": {"type": "object", "description": "飞书卡片 JSON"},
        },
        "required": ["chat_id", "card"],
    })
    async def send_feishu_card(args):
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

1. **分析股票** — 运行 `python plays/limit_up/pipeline.py --code CODE` 获取五维度评分
2. **盯盘管理** — 运行 `python plays/watchdog/watchdog.py` 管理盯盘
3. **知识查询** — 读取 `wiki/` 目录搜索A股概念和术语

## 回应方式

- 用 `send_feishu_text` 发送简短回复
- 用 `send_feishu_markdown` 发送格式化内容
- 用 `send_feishu_card` 发送评分结果卡片

## 群聊消息格式

每条消息格式: [chat_id:xxx] [用户] 消息内容

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
        log.error("未配置 FEISHU_APP_ID，请在 .env 中设置")
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

        poll = config.get("poll_interval", 2.0)
        log.info("maneki pipe started, polling inbox every %.1fs", poll)

        while True:
            for chat_id in list(known_chats):
                pos = progress.positions.get(chat_id, 0)
                messages, new_pos = read_inbox(chat_id, pos)

                for msg in messages:
                    text = msg.get("text", "")
                    sender = msg.get("sender", "unknown")
                    if not text:
                        continue

                    prompt = f"[chat_id:{chat_id}] [{sender}] {text}"
                    log.info("msg: %s", prompt[:120])

                    await client.query(prompt)
                    async for message in client.receive_response():
                        if isinstance(message, AssistantMessage):
                            for block in message.content:
                                if isinstance(block, TextBlock):
                                    log.info("claude: %s", block.text[:120])
                        elif isinstance(message, ResultMessage):
                            if not message.is_error:
                                progress.save(
                                    session_id=message.session_id,
                                    positions={chat_id: new_pos, **progress.positions},
                                )
                                log.info("done: turns=%d", message.num_turns)
                            else:
                                log.error("claude error: %s", message.subtype)

                if new_pos > pos:
                    progress.save(
                        positions={chat_id: new_pos, **progress.positions},
                    )

            await asyncio.sleep(poll)


if __name__ == "__main__":
    asyncio.run(main())