"""把 WeChatDataAnalysis 的实时事件收成信息流卡片。"""

from __future__ import annotations

import re
import time
from typing import Any
from urllib.parse import quote

from .tokens import extract_cas

_TOKEN = re.compile(r"\$[A-Za-z][A-Za-z0-9]{1,15}")
_XML_PREFIX = re.compile(r"^\s*<")

_MEDIA_LABELS = {
    "image": "[图片]",
    "video": "[视频]",
    "voice": "[语音]",
    "emoji": "[表情]",
    "location": "[位置]",
    "file": "[文件]",
    "transfer": "[转账]",
    "link": "[链接]",
    "quote": "",
    "system": "",
}

_MAX_TEXT = 2000


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _create_time_seconds(value: Any) -> int:
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        return 0
    if number > 10_000_000_000:
        return number // 1000
    return number


def _visible_text(message: dict[str, Any]) -> str:
    render_type = _as_text(message.get("renderType")) or "text"
    content = _as_text(message.get("content"))
    title = _as_text(message.get("title"))
    transcript = _as_text(message.get("voiceTranscript"))
    link_url = _as_text(message.get("linkUrl") or message.get("url"))
    if render_type == "voice" and transcript:
        content = transcript
    elif render_type == "transfer":
        amount = _as_text(message.get("amount"))
        status = _as_text(message.get("transferStatus"))
        content = " ".join(part for part in ("[转账]", amount, status) if part)
    elif render_type == "location":
        label = _as_text(message.get("locationLabel")) or _as_text(message.get("locationPoiname"))
        content = f"[位置] {label}".strip()
    elif not content and render_type in {"link", "file"}:
        if title and link_url and link_url not in title:
            content = f"{title}\n{link_url}"
        else:
            content = title or link_url
    if not content:
        content = _MEDIA_LABELS.get(render_type, "")
    if _XML_PREFIX.match(content):
        content = _MEDIA_LABELS.get(render_type) or "[消息]"
    if len(content) > _MAX_TEXT:
        content = content[:_MAX_TEXT] + "…"
    return content


def _quote(message: dict[str, Any]) -> tuple[str, str]:
    title = _as_text(message.get("quoteTitle"))
    content = _as_text(message.get("quoteContent"))
    if title and content:
        return title, content
    if content:
        return "", content
    return "", title


def _quote_username(message: dict[str, Any]) -> str:
    for key in (
        "quoteUsername",
        "quotedUsername",
        "quoteSenderUsername",
        "quote_username",
        "referUsername",
        "quotedWxid",
    ):
        value = _as_text(message.get(key))
        if value:
            return value
    return ""


def normalize_message_event(event: dict[str, Any], *, demo: bool = False) -> dict[str, Any] | None:
    """只保留群消息。私聊和空系统提示不会进入信息流。"""

    if event.get("type") != "message":
        return None
    message = event.get("message")
    if not isinstance(message, dict):
        return None
    conversation_id = _as_text(event.get("username") or event.get("conversationId"))
    if not conversation_id.endswith("@chatroom"):
        return None
    render_type = _as_text(message.get("renderType")) or "text"
    if render_type == "system":
        return None
    text = _visible_text(message)
    quote_name, quote_text = _quote(message)
    quote_username = _quote_username(message)
    if not text and not quote_text:
        return None
    sender_name = _as_text(message.get("senderDisplayName")) or _as_text(
        message.get("senderUsername")
    )
    sender_username = _as_text(message.get("senderUsername"))
    local_id = 0
    try:
        local_id = int(message.get("localId") or 0)
    except (TypeError, ValueError):
        local_id = 0
    message_id = _as_text(message.get("id")) or f"{conversation_id}:{local_id}"
    haystack = "\n".join(
        part for part in (text, quote_text, _as_text(message.get("title"))) if part
    )
    created = _create_time_seconds(message.get("createTime")) or int(time.time())
    cas = extract_cas(haystack)
    media_url = ""
    raw_media = _as_text(
        message.get("imageUrl") or message.get("thumbUrl") or message.get("videoThumbUrl")
    )
    media_full = ""
    wx_image = _as_text(message.get("wxImage"))
    wx_emoji = _as_text(message.get("wxEmoji"))
    emoji_url = _as_text(message.get("emojiUrl"))
    if wx_image:
        media_url = "/api/wximg?ref=" + quote(wx_image, safe="")
        media_full = media_url + "&hd=1"
    elif wx_emoji and emoji_url:
        media_url = (
            "/api/wxemoji?md5=" + quote(wx_emoji, safe="") + "&u=" + quote(emoji_url, safe="")
        )
    elif raw_media.startswith("http://127.0.0.1:") or raw_media.startswith("http://localhost:"):
        media_url = "/api/media?u=" + quote(raw_media, safe="")
    link_url = _as_text(message.get("linkUrl") or message.get("url"))
    return {
        "id": message_id,
        "localId": local_id,
        "conversationId": conversation_id,
        "conversationName": _as_text(event.get("conversationName")) or conversation_id,
        "senderUsername": sender_username,
        "senderName": sender_name or "群成员",
        "createTime": created,
        "renderType": render_type,
        "text": text,
        "quoteName": quote_name,
        "quoteText": quote_text,
        "quoteUsername": quote_username,
        "addresses": [item["address"] for item in cas],
        "cas": cas,
        "tickers": _TOKEN.findall(haystack),
        "tokenCards": [],
        "mediaUrl": media_url,
        "mediaFullUrl": media_full,
        "linkUrl": link_url,
        "isSent": bool(message.get("isSent")),
        "demo": demo,
        "receivedAt": int(time.time() * 1000),
    }


def extract_groups(payload: Any) -> list[dict[str, str]]:
    """从会话列表响应里取出群。字段名按上游实际返回做兼容。"""

    rows: Any = payload
    if isinstance(payload, dict):
        for key in ("sessions", "data", "items", "list"):
            candidate = payload.get(key)
            if isinstance(candidate, list):
                rows = candidate
                break
        else:
            rows = []
    if not isinstance(rows, list):
        return []
    groups: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        username = ""
        for key in ("username", "userName", "user_name", "conversationId"):
            username = _as_text(row.get(key))
            if username:
                break
        if not username.endswith("@chatroom") or username in seen:
            continue
        name = ""
        for key in ("displayName", "conversationName", "nickName", "nickname", "name", "remark"):
            name = _as_text(row.get(key))
            if name and name != username:
                break
        seen.add(username)
        groups.append({"id": username, "name": name if name != username else ""})
    return groups
