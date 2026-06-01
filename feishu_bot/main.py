"""飞书 Bot 回调服务 — FastAPI 入口

只做一件事：接收飞书 webhook → 写入 inbox → 返回 200。
决策和回复由 pipes/maneki/maneki_pipe.py (Claude SDK) 接管。
"""

import json
import logging
import sys
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from pipes.maneki.maneki_pipe import write_inbox  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("feishu_bot")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("飞书Bot回调服务启动: inbox writer mode")
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def health():
    return {"status": "ok", "mode": "inbox_writer"}


@app.post("/feishu/callback")
async def feishu_callback(request: Request):
    try:
        event = await request.json()
    except Exception:
        return JSONResponse({"code": -1, "msg": "invalid json"}, status_code=400)

    # 飞书 URL 验证
    if event.get("type") == "url_verification":
        return JSONResponse({"challenge": event.get("challenge", "")})

    # 提取消息
    header = event.get("header", {})
    event_type = header.get("event_type", "")
    if event_type != "im.message.receive_v1":
        return JSONResponse({"code": 0})

    message = event.get("event", {}).get("message", {})
    chat_id = message.get("chat_id", "")
    content_raw = message.get("content", "{}")

    try:
        content = json.loads(content_raw)
    except json.JSONDecodeError:
        content = {}
    text = content.get("text", "") if isinstance(content, dict) else str(content)

    if not text.strip() or not chat_id:
        return JSONResponse({"code": 0})

    # 写入 inbox
    write_inbox(chat_id, {
        "text": text,
        "sender": event.get("event", {}).get("sender", {}).get("sender_id", {}).get("user_id", "unknown"),
        "timestamp": header.get("create_time", ""),
    })
    logger.info("inbox: chat=%s text=%s", chat_id, text[:80])

    return JSONResponse({"code": 0})