"""飞书 Bot 回调服务 — FastAPI 入口

接收飞书 webhook → 即时回复"请稍候"（已读确认）→ 写入 inbox → Claude 接管回复。
"""

import asyncio
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

from pipelines.maneki.maneki_pipe import write_inbox  # noqa: E402
from feishu_bot.feishu_client import FEISHU_CLIENT  # noqa: E402

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
    message_id = message.get("message_id", "")
    chat_type = message.get("chat_type", "")
    content_raw = message.get("content", "{}")

    # @检测: 群聊中非@机器人的消息不处理
    if chat_type == "group":
        mentions = message.get("mentions", [])
        if not mentions:
            logger.debug("skip non-at message in group chat: %s", message_id[:12])
            return JSONResponse({"code": 0})
    # p2p 私聊: 始终处理

    try:
        content = json.loads(content_raw)
    except json.JSONDecodeError:
        content = {}
    text = content.get("text", "") if isinstance(content, dict) else str(content)

    if not text.strip() or not chat_id:
        return JSONResponse({"code": 0})

    # 去重：检查持久化去重文件 + inbox
    dedup_file = Path(PROJECT_DIR) / "pipelines" / "maneki" / "inbox" / "dedup.json"
    seen_ids: set = set()
    if dedup_file.exists():
        try:
            seen_ids = set(json.loads(dedup_file.read_text()))
        except Exception:
            pass
    # 也检查 inbox 文件（兼容旧数据）
    inbox_file = Path(PROJECT_DIR) / "pipelines" / "maneki" / "inbox" / f"{chat_id}.jsonl"
    if inbox_file.exists():
        for line in inbox_file.read_text().split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                existing = json.loads(line)
                mid = existing.get("message_id", "")
                if mid:
                    seen_ids.add(mid)
            except json.JSONDecodeError:
                pass
    if message_id in seen_ids:
        logger.info("dedup: skip duplicate msg %s", message_id[:12])
        return JSONResponse({"code": 0})
    # 记录到持久化去重文件
    seen_ids.add(message_id)
    dedup_file.parent.mkdir(parents=True, exist_ok=True)
    dedup_file.write_text(json.dumps(list(seen_ids), ensure_ascii=False))

    # 异步发送"请稍候"（不阻塞 webhook 响应，防止 Feishu 超时重试）
    async def _send_ack():
        try:
            await FEISHU_CLIENT.reply_text(message_id, "分析中，请稍候...")
        except Exception as e:
            logger.warning("reply_text failed: %s", e)
    asyncio.create_task(_send_ack())

    # 写入 inbox（同步，直接写文件不涉及网络）
    write_inbox(chat_id, {
        "text": text,
        "message_id": message_id,
        "chat_id": chat_id,
        "sender": event.get("event", {}).get("sender", {}).get("sender_id", {}).get("user_id", "unknown"),
        "timestamp": header.get("create_time", ""),
    })
    logger.info("inbox: chat=%s msg=%s text=%s", chat_id, message_id[:12], text[:80])

    return JSONResponse({"code": 0})
