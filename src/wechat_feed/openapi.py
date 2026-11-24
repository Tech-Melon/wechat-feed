"""开放接口：让局域网里的其他程序把别的社交平台的群消息推进信息流。

- 单独监听一个端口（默认 0.0.0.0:8766），这个端口上只有 /open/v1/*；
  本地页和 /api/* 仍只在 127.0.0.1。
- 每个请求用密钥做 HMAC-SHA256 签名，带时间戳，±300 秒外的请求拒收；
  明文 HTTP 上也不会泄露密钥。
- 默认只接受私有网段来源，可在设置里改成 IP 白名单。
- 收到的消息先校验、去重，再进进程内队列，由单个协程按发送时间顺序写入，
  和微信消息共用同一条推送通路。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import ipaddress
import json
import math
import re
import secrets
import socket
import time
from collections import deque
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from loguru import logger

from .config import OPEN_PLATFORM, OPEN_RAW_ID, SettingsStore
from .hub import Hub
from .opendir import OpenDirectory, image_ref
from .store import KEEP_SECONDS
from .tokens import TokenIndex, extract_cas
from .upstream import _enrich_card, apply_display

API_VERSION = "v1"
MAX_BODY = 512 * 1024
MAX_BATCH = 100
MAX_TEXT = 2000
MAX_QUOTE = 500
MAX_NAME = 64
MAX_CHAT_NAME = 80
MAX_URL = 2048
SKEW_SECONDS = 300
# 未来时间最多容忍 5 秒（两台电脑的时钟差），再往后的按收到时间算，免得钉在信息流顶部。
FUTURE_SLACK = 5
QUEUE_MAX = 2000
# 每个来源 IP：每秒最多 20 个请求、每分钟最多 1200 条消息；签名错 20 次封 5 分钟。
RATE_REQUESTS_PER_SEC = 20
RATE_MESSAGES_PER_MIN = 1200
AUTH_FAIL_LIMIT = 20
AUTH_BLOCK_SECONDS = 300

_TYPES = {
    "text": "text",
    "image": "image",
    "sticker": "emoji",
    "video": "video",
    "voice": "voice",
    "file": "file",
    "link": "link",
}
_LABELS = {
    "image": "[图片]",
    "emoji": "[表情]",
    "video": "[视频]",
    "voice": "[语音]",
    "file": "[文件]",
    "link": "[链接]",
}
_PLATFORM_NAMES = {
    "telegram": "Telegram",
    "tg": "Telegram",
    "discord": "Discord",
    "qq": "QQ",
    "slack": "Slack",
    "whatsapp": "WhatsApp",
    "line": "LINE",
    "x": "X",
    "twitter": "X",
    "dingtalk": "钉钉",
    "feishu": "飞书",
    "lark": "Lark",
    "wecom": "企业微信",
    "matrix": "Matrix",
    "signal": "Signal",
    "kook": "KOOK",
}
_STAMP = re.compile(r"^[0-9]{1,12}$")
_SIGNATURE = re.compile(r"^[0-9a-f]{64}$")
_TICKER = re.compile(r"\$[A-Za-z][A-Za-z0-9]{1,15}")
# 去掉控制字符，保留换行和制表符。
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2066-\u2069]")


def platform_name(platform: str) -> str:
    return _PLATFORM_NAMES.get(
        platform, platform.upper() if len(platform) <= 4 else platform.title()
    )


def new_key() -> str:
    return "wfo_" + secrets.token_urlsafe(32)


def key_hint(key: str) -> str:
    return f"wfo_…{key[-4:]}" if key else ""


def sign(key: str, timestamp: int | str, method: str, path: str, body: bytes) -> str:
    """签名原文：时间戳\\n方法\\n路径\\n + 原始请求体字节。推送端照这个算。"""
    head = f"{timestamp}\n{method.upper()}\n{path}\n".encode()
    return hmac.new(key.encode("utf-8"), head + body, hashlib.sha256).hexdigest()


# ---- 校验与转换 ----


class Rejected(ValueError):
    pass


def _clean_text(value: Any, limit: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str | int | float) or isinstance(value, bool):
        raise Rejected("文本字段必须是字符串")
    text = _CONTROL.sub("", str(value)).replace("\r\n", "\n").strip()
    if len(text) > limit:
        text = text[:limit] + "…"
    return text


def _raw_id(value: Any, label: str) -> str:
    if isinstance(value, bool) or not isinstance(value, str | int):
        raise Rejected(f"{label} 必须是字符串或整数")
    text = str(value).strip()
    if not OPEN_RAW_ID.match(text):
        raise Rejected(f"{label} 只能用字母、数字和 _ . = + -，最长 64")
    return text


def _url(value: Any, label: str) -> str:
    if value in (None, ""):
        return ""
    if not isinstance(value, str) or len(value) > MAX_URL:
        raise Rejected(f"{label} 必须是不超过 {MAX_URL} 字符的地址")
    text = value.strip()
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise Rejected(f"{label} 只接受 http / https 地址")
    if parsed.username or parsed.password:
        raise Rejected(f"{label} 不能带账号密码")
    return text


def _obj(value: Any, label: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise Rejected(f"{label} 必须是对象")
    return value


def _time(value: Any, now: float) -> int:
    if value in (None, ""):
        return int(now)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise Rejected("time 必须是数字（毫秒或秒）")
    try:
        number = float(value)
    except OverflowError as exc:
        raise Rejected("time 数值过大") from exc
    # json.loads 接受 NaN / Infinity，这里必须拦住，否则 int() 会抛错导致整批 500。
    if not math.isfinite(number) or number < 0 or number > 1e15:
        raise Rejected("time 不是合法的时间戳")
    seconds = int(number // 1000 if number > 10_000_000_000 else number)
    if seconds > now + FUTURE_SLACK:
        return int(now)
    if seconds < now - KEEP_SECONDS:
        raise Rejected("time 早于 30 天，信息流不再保留")
    return seconds


def build_card(platform: str, item: Any, now: float | None = None) -> tuple[dict[str, Any], dict]:
    """把一条推送消息转成信息流卡片，同时返回要写进目录的群、人、图片。"""
    if not isinstance(item, dict):
        raise Rejected("每条消息必须是对象")
    clock = time.time() if now is None else now
    msg_id = _raw_id(item.get("id"), "id")
    chat = _obj(item.get("chat"), "chat")
    sender = _obj(item.get("sender"), "sender")
    chat_raw = _raw_id(chat.get("id"), "chat.id")
    sender_raw = _raw_id(sender.get("id"), "sender.id")
    conversation_id = f"{platform}:{chat_raw}"
    sender_id = f"{platform}:{sender_raw}"
    chat_name = _clean_text(chat.get("name"), MAX_CHAT_NAME)
    sender_name = _clean_text(sender.get("name"), MAX_NAME)
    kind = str(item.get("type") or "text")
    render_type = _TYPES.get(kind)
    if render_type is None:
        raise Rejected("type 只能是 " + " / ".join(_TYPES))
    text = _clean_text(item.get("text"), MAX_TEXT)
    image_url = _url(item.get("image"), "image")
    link = _obj(item.get("link"), "link")
    link_url = _url(link.get("url"), "link.url")
    link_title = _clean_text(link.get("title"), 200)
    if render_type == "link" and not text:
        text = f"{link_title}\n{link_url}".strip() if link_url not in link_title else link_title
    reply = _obj(item.get("reply"), "reply")
    quote_text = _clean_text(reply.get("text"), MAX_QUOTE)
    quote_name = _clean_text(reply.get("senderName"), MAX_NAME)
    quote_user = ""
    if reply.get("senderId") not in (None, ""):
        quote_user = f"{platform}:{_raw_id(reply.get('senderId'), 'reply.senderId')}"
    if not text:
        text = _LABELS.get(render_type, "")
    if not text and not quote_text and not image_url:
        raise Rejected("消息没有内容（text / image / link 至少一个）")
    created = _time(item.get("time"), clock)
    haystack = "\n".join(part for part in (text, quote_text, link_title) if part)
    cas = extract_cas(haystack)
    images: list[tuple[str, str]] = []
    media_url = ""
    if image_url:
        ref = image_ref(image_url)
        images.append((ref, image_url))
        media_url = "/api/openimg?ref=" + ref
        if render_type == "text":
            render_type = "image"
    is_self = item.get("isSelf", False)
    if not isinstance(is_self, bool):
        raise Rejected("isSelf 必须是布尔值")
    card = {
        "id": f"{conversation_id}:{msg_id}",
        "localId": 0,
        "conversationId": conversation_id,
        "conversationName": chat_name or conversation_id,
        "senderUsername": sender_id,
        "senderName": sender_name or sender_raw,
        "createTime": created,
        "renderType": render_type,
        "text": text,
        "quoteName": quote_name,
        "quoteText": quote_text,
        "quoteUsername": quote_user,
        "addresses": [entry["address"] for entry in cas],
        "cas": cas,
        "tickers": _TICKER.findall(haystack),
        "tokenCards": [],
        "mediaUrl": media_url,
        "mediaFullUrl": media_url,
        "linkUrl": link_url,
        "isSent": is_self,
        "demo": False,
        "platform": platform,
        "platformName": platform_name(platform),
        "receivedAt": int(clock * 1000),
    }
    directory_row = {
        "platform": platform,
        "chat": {
            "id": conversation_id,
            "name": chat_name,
            "avatar": _url(chat.get("avatar"), "chat.avatar"),
        },
        "sender": {
            "id": sender_id,
            "name": sender_name,
            "avatar": _url(sender.get("avatar"), "sender.avatar"),
        },
        "images": images,
    }
    return card, directory_row


# ---- 鉴权与限流 ----


def client_allowed(ip_text: str, allow: list[str]) -> bool:
    try:
        ip = ipaddress.ip_address(ip_text)
    except ValueError:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    if allow:
        return any(ip in ipaddress.ip_network(item, strict=False) for item in allow)
    # 没配白名单时只收局域网和本机来源，公网地址一律拒绝。
    return ip.is_private or ip.is_loopback


class Guard:
    """按来源 IP 的固定窗口限流和签名失败封禁。只在事件循环里用，不需要锁。"""

    def __init__(self) -> None:
        self._requests: dict[str, deque[float]] = {}
        self._messages: dict[str, deque[tuple[float, int]]] = {}
        self._fails: dict[str, deque[float]] = {}
        self._blocked: dict[str, float] = {}

    @staticmethod
    def _trim(queue: deque, horizon: float, key: Callable[[Any], float] = lambda x: x) -> None:
        while queue and key(queue[0]) < horizon:
            queue.popleft()

    def blocked(self, ip: str, now: float) -> bool:
        until = self._blocked.get(ip, 0.0)
        if until and until > now:
            return True
        self._blocked.pop(ip, None)
        return False

    def fail(self, ip: str, now: float) -> None:
        queue = self._fails.setdefault(ip, deque())
        self._trim(queue, now - 60)
        queue.append(now)
        if len(queue) >= AUTH_FAIL_LIMIT:
            self._blocked[ip] = now + AUTH_BLOCK_SECONDS
            queue.clear()
            logger.warning("开放接口：{} 签名连续出错，封禁 {} 秒", ip, AUTH_BLOCK_SECONDS)

    def hit(self, ip: str, now: float) -> bool:
        queue = self._requests.setdefault(ip, deque())
        self._trim(queue, now - 1)
        if len(queue) >= RATE_REQUESTS_PER_SEC:
            return False
        queue.append(now)
        return True

    def take_messages(self, ip: str, count: int, now: float) -> bool:
        queue = self._messages.setdefault(ip, deque())
        self._trim(queue, now - 60, key=lambda item: item[0])
        used = sum(item[1] for item in queue)
        if used + count > RATE_MESSAGES_PER_MIN:
            return False
        queue.append((now, count))
        return True


# ---- 入队与消费 ----


class OpenIntake:
    def __init__(
        self,
        store: SettingsStore,
        hub: Hub,
        tokens: TokenIndex,
        directory: OpenDirectory,
        maxsize: int = QUEUE_MAX,
    ) -> None:
        self.store = store
        self.hub = hub
        self.tokens = tokens
        self.directory = directory
        self.maxsize = maxsize
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._pending: set[str] = set()
        self.stats: dict[str, Any] = {
            "requests": 0,
            "accepted": 0,
            "duplicate": 0,
            "ignored": 0,
            "rejected": 0,
            "authFailed": 0,
            "lastAt": 0,
            "lastIp": "",
            "lastError": "",
        }

    def depth(self) -> int:
        return self.queue.qsize()

    def submit(self, body: Any, ip: str) -> dict[str, Any]:
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="请求体必须是对象")
        platform = str(body.get("platform") or "").strip()
        if not OPEN_PLATFORM.match(platform):
            raise HTTPException(
                status_code=400, detail="platform 必须是 2–16 位小写字母开头的 [a-z0-9_-]"
            )
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            raise HTTPException(status_code=400, detail="messages 必须是非空数组")
        if len(messages) > MAX_BATCH:
            raise HTTPException(status_code=413, detail=f"每次最多 {MAX_BATCH} 条")
        settings = self.store.get()
        watched = {str(item.get("id") or "") for item in settings.get("groups") or []}
        now = time.time()
        results: list[dict[str, Any]] = []
        rows: list[dict[str, Any]] = []
        ready: list[dict[str, Any]] = []
        counts = {"accepted": 0, "duplicate": 0, "ignored": 0, "rejected": 0}
        persist = self.hub.persist
        for item in messages:
            raw_id = str(item.get("id") or "")[:64] if isinstance(item, dict) else ""
            try:
                card, row = build_card(platform, item, now)
            except (Rejected, ValueError, TypeError, OverflowError) as exc:
                # 任何一条坏数据只拒这一条，不能让整批 500，否则前面已标记的 id 会被误判重复。
                counts["rejected"] += 1
                reason = str(exc) if isinstance(exc, Rejected) else "字段格式不正确"
                results.append({"id": raw_id, "status": "rejected", "reason": reason})
                continue
            rows.append(row)
            card_id = card["id"]
            if card["conversationId"] not in watched:
                status = "ignored"
            elif card_id in self._pending or (persist is not None and persist.exists(card_id)):
                status = "duplicate"
            else:
                status = "accepted"
                self._pending.add(card_id)
                ready.append(card)
            counts[status] += 1
            entry: dict[str, Any] = {"id": raw_id, "status": status}
            if status == "ignored":
                entry["reason"] = "这个群还没在「群聊选择管理」里加入监控"
            results.append(entry)
        if ready and self.queue.qsize() + len(ready) > self.maxsize:
            for card in ready:
                self._pending.discard(card["id"])
            raise HTTPException(
                status_code=429, detail="接收队列已满，稍后重试", headers={"Retry-After": "2"}
            )
        self.directory.note(rows)
        # 同一批按发送时间入队，单个消费者顺序写库，迟到的消息靠 createTime 落到正确位置。
        for card in sorted(ready, key=lambda c: (c["createTime"], c["receivedAt"], c["id"])):
            self.queue.put_nowait(card)
        self.stats["requests"] += 1
        for key, value in counts.items():
            self.stats[key] += value
        self.stats["lastAt"] = int(now)
        self.stats["lastIp"] = ip
        return {
            "ok": True,
            "platform": platform,
            **counts,
            "queue": self.queue.qsize(),
            "results": results,
        }

    def _publish(self, card: dict[str, Any]) -> None:
        card_id = str(card["id"])
        self._pending.discard(card_id)
        persist = self.hub.persist
        if persist is not None and persist.exists(card_id):
            return
        settings = self.store.get()
        for group in settings.get("groups") or []:
            if group.get("id") == card["conversationId"] and group.get("name"):
                card["conversationName"] = str(group["name"])
                break
        apply_display(card, settings)
        # 带 CA 的消息和微信消息一样走 TokenIndex 实时解析：每个新 CA 约 search 1 + info 1
        # = 2 权重，同一个币 2 秒内命中缓存；限频窗口统一记账，后台刷榜会自动让开。
        # 推送端按每分钟 20 个新 CA 估算约 0.7 权重/秒，仍在免费档 5 权重/秒以内。
        if self.hub.publish(card) and card.get("cas"):
            asyncio.create_task(_enrich_card(self.tokens, self.hub, card, list(card["cas"])))

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                card = await asyncio.wait_for(self.queue.get(), timeout=1.0)
            except TimeoutError:
                continue
            try:
                self._publish(card)
            except Exception as exc:
                logger.info("开放接口消息写入失败: {}", type(exc).__name__)
            if self.queue.empty():
                self.hub.flush()


# ---- 局域网端口上的应用 ----


async def _read_body(request: Request) -> bytes:
    declared = request.headers.get("content-length")
    if declared:
        try:
            if int(declared) > MAX_BODY:
                raise HTTPException(status_code=413, detail="请求体超过 512 KB")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Content-Length 不合法") from exc
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY:
            raise HTTPException(status_code=413, detail="请求体超过 512 KB")
        chunks.append(chunk)
    return b"".join(chunks)


def create_open_app(store: SettingsStore, intake: OpenIntake) -> FastAPI:
    app = FastAPI(title="技术瓜群流 开放接口", docs_url=None, redoc_url=None, openapi_url=None)
    guard = Guard()

    @app.middleware("http")
    async def gate(request: Request, call_next):
        ip = request.client.host if request.client else ""
        settings = store.get()
        if not client_allowed(ip, list(settings.get("open_api_allow_ips") or [])):
            return JSONResponse({"detail": "来源 IP 不在白名单"}, status_code=403)
        if not request.url.path.startswith(f"/open/{API_VERSION}/"):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        # 浏览器发来的请求一律拒绝：这个端口只给服务端程序调用，不开 CORS。
        if request.headers.get("origin"):
            return JSONResponse({"detail": "不接受浏览器跨源请求"}, status_code=403)
        now = time.time()
        if guard.blocked(ip, now):
            return JSONResponse({"detail": "签名错误次数过多，稍后再试"}, status_code=429)
        if not guard.hit(ip, now):
            return JSONResponse(
                {"detail": "请求太频繁"}, status_code=429, headers={"Retry-After": "1"}
            )
        return await call_next(request)

    def verify(request: Request, body: bytes) -> None:
        settings = store.get()
        key = str(settings.get("open_api_key") or "")
        if not settings.get("open_api_enabled") or not key:
            raise HTTPException(status_code=503, detail="开放接口没有开启")
        ip = request.client.host if request.client else ""
        stamp = str(request.headers.get("x-wf-timestamp") or "").strip()
        signature = str(request.headers.get("x-wf-signature") or "").strip().lower()
        reason = ""
        if not _STAMP.match(stamp) or not _SIGNATURE.match(signature):
            reason = "缺少或格式错误的 X-WF-Timestamp / X-WF-Signature"
        elif abs(time.time() - int(stamp)) > SKEW_SECONDS:
            reason = f"时间戳和服务器相差超过 {SKEW_SECONDS} 秒，请校准时钟"
        else:
            expected = sign(key, stamp, request.method, request.url.path, body)
            if not hmac.compare_digest(expected, signature):
                reason = "签名不正确"
        if reason:
            guard.fail(ip, time.time())
            intake.stats["authFailed"] += 1
            intake.stats["lastError"] = reason
            logger.info("开放接口鉴权失败 ip={} path={}: {}", ip, request.url.path, reason)
            raise HTTPException(status_code=401, detail=reason)

    @app.get(f"/open/{API_VERSION}/ping")
    async def ping(request: Request) -> dict[str, Any]:
        verify(request, b"")
        return {
            "ok": True,
            "version": API_VERSION,
            "time": int(time.time()),
            "queue": intake.depth(),
        }

    @app.post(f"/open/{API_VERSION}/messages")
    async def push_messages(request: Request) -> dict[str, Any]:
        kind = str(request.headers.get("content-type") or "").split(";")[0].strip().lower()
        if kind != "application/json":
            raise HTTPException(status_code=415, detail="只接受 application/json")
        body = await _read_body(request)
        verify(request, body)
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=400, detail="请求体不是合法 JSON") from exc
        ip = request.client.host if request.client else ""
        count = len(payload.get("messages") or []) if isinstance(payload, dict) else 0
        if count and not guard.take_messages(ip, min(count, MAX_BATCH), time.time()):
            raise HTTPException(
                status_code=429, detail="每分钟消息数超限", headers={"Retry-After": "5"}
            )
        out = intake.submit(payload, ip)
        logger.info(
            "开放接口收到 {} 条 platform={} ip={} 入队 {} 重复 {} 未监控 {} 拒收 {}",
            count,
            out["platform"],
            ip,
            out["accepted"],
            out["duplicate"],
            out["ignored"],
            out["rejected"],
        )
        return out

    return app


class _EmbeddedServer:
    """在主事件循环里再跑一个 uvicorn 实例。信号交给主服务处理，端口由我们自己先绑好。"""

    def __init__(self, app: FastAPI, sock: socket.socket) -> None:
        import uvicorn

        class _Quiet(uvicorn.Server):
            @contextlib.contextmanager
            def capture_signals(self):  # type: ignore[override]
                yield

        config = uvicorn.Config(
            app,
            log_config=None,
            access_log=False,
            lifespan="off",
            timeout_graceful_shutdown=2,
            proxy_headers=False,
            server_header=False,
        )
        self.server = _Quiet(config)
        self.sock = sock
        self.failed = False
        self.task = asyncio.create_task(self._serve())

    async def _serve(self) -> None:
        try:
            await self.server.serve(sockets=[self.sock])
        except SystemExit:
            # uvicorn 启动失败时会 sys.exit；这里拦住，不能让它带走主服务。
            self.failed = True
            logger.warning("开放接口端口启动失败")
        except Exception as exc:
            self.failed = True
            logger.warning("开放接口监听异常退出: {}", type(exc).__name__)

    async def stop(self) -> None:
        self.server.should_exit = True
        try:
            await asyncio.wait_for(asyncio.shield(self.task), timeout=5)
        except TimeoutError:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        with contextlib.suppress(OSError):
            self.sock.close()


class OpenListener:
    """按设置开关局域网端口。设置改了就重启监听，不影响 127.0.0.1:8765。"""

    def __init__(self, app_factory: Callable[[], FastAPI]) -> None:
        self._factory = app_factory
        self._app: FastAPI | None = None
        self._running: _EmbeddedServer | None = None
        self._signature: tuple[Any, ...] | None = None
        self._lock = asyncio.Lock()
        self.state: dict[str, Any] = {"listening": False, "address": "", "error": ""}

    async def apply(self, settings: dict[str, Any]) -> None:
        enabled = bool(settings.get("open_api_enabled") and settings.get("open_api_key"))
        host = str(settings.get("open_api_host") or "0.0.0.0")
        port = int(settings.get("open_api_port") or 0)
        signature = (enabled, host, port)
        async with self._lock:
            running = self._running
            if running is not None and running.task.done() and not running.server.should_exit:
                # 监听任务自己退出了（异常或启动失败）：清掉签名，下面重新绑定。
                self._running = None
                self._signature = None
            if signature == self._signature:
                return
            self._signature = signature
            await self._stop()
            if not enabled:
                self.state = {"listening": False, "address": "", "error": ""}
                return
            family = socket.AF_INET6 if ":" in host else socket.AF_INET
            sock = socket.socket(family, socket.SOCK_STREAM)
            try:
                # Windows 上 SO_EXCLUSIVEADDRUSE 防止别的程序抢同一端口偷听推送。
                exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
                if exclusive is not None:
                    sock.setsockopt(socket.SOL_SOCKET, exclusive, 1)
                sock.bind((host, port))
            except OSError as exc:
                sock.close()
                # 不记住这次签名：watch() 3 秒后会再试，旧进程释放端口后自动恢复。
                self._signature = None
                self.state = {
                    "listening": False,
                    "address": f"{host}:{port}",
                    "error": f"端口绑定失败：{exc.strerror or type(exc).__name__}",
                }
                logger.warning("开放接口绑定 {}:{} 失败: {}", host, port, exc)
                return
            if self._app is None:
                self._app = self._factory()
            self._running = _EmbeddedServer(self._app, sock)
            self.state = {"listening": True, "address": f"{host}:{port}", "error": ""}
            logger.info("开放接口已监听 http://{}:{}/open/{}/", host, port, API_VERSION)

    async def _stop(self) -> None:
        if self._running is None:
            return
        running, self._running = self._running, None
        await running.stop()
        logger.info("开放接口已停止监听")

    async def close(self) -> None:
        async with self._lock:
            self._signature = None
            await self._stop()
            self.state = {"listening": False, "address": "", "error": ""}

    async def watch(self, store: SettingsStore, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.apply(store.get())
            except Exception as exc:
                logger.warning("开放接口监听调整失败: {}", type(exc).__name__)
            try:
                await asyncio.wait_for(stop.wait(), timeout=3.0)
            except TimeoutError:
                continue


def lan_addresses() -> list[str]:
    found: list[str] = []
    with contextlib.suppress(OSError):
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = str(info[4][0])
            if ip not in found and not ip.startswith("127."):
                found.append(ip)
    return found
