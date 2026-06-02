"""飞书 API 客户端 — token管理 + 消息发送"""

import json
import os
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_DIR / ".env")

# 机器人回调专用凭证（与推送信号的应用分离）
BOT_APP_ID = os.getenv("FEISHU_BOT_APP_ID", "")
BOT_APP_SECRET = os.getenv("FEISHU_BOT_APP_SECRET", "")
BOT_CHAT_ID = os.getenv("FEISHU_BOT_CHAT_ID", "")

FEISHU_BASE = "https://open.feishu.cn/open-apis"


class FeishuClient:
    """飞书 API 客户端，带 token 缓存自动刷新"""

    def __init__(self, app_id: str = "", app_secret: str = ""):
        self._app_id = app_id or BOT_APP_ID
        self._app_secret = app_secret or BOT_APP_SECRET
        self._token: str | None = None
        self._expires_at: float = 0.0

    async def get_token(self) -> str:
        """获取 tenant_access_token（缓存+自动刷新）"""
        if time.time() < self._expires_at and self._token:
            return self._token

        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{FEISHU_BASE}/auth/v3/tenant_access_token/internal",
                json={"app_id": self._app_id, "app_secret": self._app_secret},
            )
            data = resp.json()
            token = data.get("tenant_access_token")
            if not isinstance(token, str) or not token:
                raise ValueError("failed to get tenant_access_token from feishu response")
            self._token = token
            self._expires_at = time.time() + data.get("expire", 7200) - 60
            return token

    async def send_message(self, chat_id: str, msg_type: str, content: str) -> dict:
        token = await self.get_token()
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{FEISHU_BASE}/im/v1/messages?receive_id_type=chat_id",
                headers={"Authorization": f"Bearer {token}"},
                json={"receive_id": chat_id, "msg_type": msg_type, "content": content},
            )
            return resp.json()

    async def send_text(self, chat_id: str, text: str):
        return await self.send_message(chat_id, "text", json.dumps({"text": text}))

    async def send_card(self, chat_id: str, card: dict):
        return await self.send_message(chat_id, "interactive", json.dumps(card))

    async def reply_message(self, message_id: str, msg_type: str, content: str) -> dict:
        token = await self.get_token()
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{FEISHU_BASE}/im/v1/messages/{message_id}/reply",
                headers={"Authorization": f"Bearer {token}"},
                json={"msg_type": msg_type, "content": content},
            )
            return resp.json()

    async def reply_card(self, message_id: str, card: dict):
        return await self.reply_message(message_id, "interactive", json.dumps(card))

    async def reply_text(self, message_id: str, text: str):
        return await self.reply_message(message_id, "text", json.dumps({"text": text}))

    async def reply_markdown(self, message_id: str, md: str):
        """以飞书post富文本格式发送markdown内容"""
        content = _md_to_post_content(md)
        return await self.reply_message(message_id, "post", json.dumps(content))


# 全局单例（使用机器人回调凭证）
FEISHU_CLIENT = FeishuClient()


def _md_to_post_content(md: str) -> dict:
    """将简单markdown转为飞书post消息格式（支持 **bold** 和 \n 分段）"""
    import re
    paragraphs = []
    for line in md.split("\n"):
        if not line.strip():
            continue
        elements = []
        # 解析 **bold**
        parts = re.split(r"(\*\*[^*]+\*\*)", line)
        for part in parts:
            if part.startswith("**") and part.endswith("**"):
                elements.append({"tag": "text", "text": part[2:-2], "style": ["bold"]})
            elif part:
                elements.append({"tag": "text", "text": part})
        if elements:
            paragraphs.append(elements)
    return {"zh_cn": {"content": paragraphs}}
