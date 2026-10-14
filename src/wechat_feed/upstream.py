"""连接本机 WeChatDataAnalysis 的实时消息流。"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from urllib.parse import quote, urlencode

import httpx
from loguru import logger

from .config import SettingsStore
from .direct import DirectSource, direct_backfill, direct_disabled, direct_stream
from .http import client as http_client
from .hub import Hub
from .normalize import extract_groups, normalize_message_event
from .store import KEEP_SECONDS
from .tokens import TokenIndex
from .wechatdb import WeChatDB

_SESSION_TIMEOUT = httpx.Timeout(8.0, connect=3.0)
_POLL_MS = 300
# WDA 每轮拉取约 1.8 CPU 秒，库写入密集时不限频会持续占满一个多核。
_PULL_MIN_GAP = 1.5
_DIRECT_RETRY = 30.0


def parse_sse_block(block: str) -> dict[str, Any] | None:
    data_lines: list[str] = []
    for line in block.splitlines():
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    if not data_lines:
        return None
    try:
        payload = json.loads("\n".join(data_lines))
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _message_stream_url(settings: dict[str, Any]) -> str:
    base = str(settings["upstream_base"]).rstrip("/")
    query = {"include_sent": "false", "interval_ms": str(_POLL_MS)}
    return f"{base}/api/chat/realtime/messages?{urlencode(query)}"


def _change_stream_url(settings: dict[str, Any]) -> str:
    base = str(settings["upstream_base"]).rstrip("/")
    query = {"interval_ms": str(_POLL_MS), "scope": "chat"}
    return f"{base}/api/chat/realtime/stream?{urlencode(query)}"


def _stream_urls(settings: dict[str, Any]) -> list[str]:
    return [_message_stream_url(settings)]


def _describe_failure(exc: BaseException) -> str:
    if isinstance(exc, httpx.ConnectError):
        return "本机 10392 端口没有服务。请确认 WeChatDataAnalysis 窗口还开着。"
    if isinstance(exc, httpx.HTTPStatusError):
        status = int(exc.response.status_code)
        if status == 404:
            return "账号已检测到，密钥还没提取。请点当前账号旁边的「解密提取」。"
        return f"微信数据服务返回 {status}，正在重试。"
    return "微信数据服务暂时连不上，正在重试。"


def _allowed(settings: dict[str, Any], conversation_id: str) -> bool:
    groups = list(settings.get("groups") or [])
    if not groups:
        return conversation_id.endswith("@chatroom")
    return conversation_id in {str(item["id"]) for item in groups}


def apply_display(card: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    conversation_id = str(card.get("conversationId") or "")
    sender = str(card.get("senderUsername") or "")
    original_name = str(card.get("senderName") or "群成员")
    original_group = str(card.get("conversationName") or "")
    if original_group.endswith("@chatroom"):
        original_group = ""
    remark_name = str((settings.get("user_remarks") or {}).get(sender) or "")
    remark_group = str((settings.get("group_remarks") or {}).get(conversation_id) or "")
    watched_name = ""
    for item in settings.get("groups") or []:
        if item["id"] == conversation_id and item.get("name"):
            watched_name = str(item["name"])
            break
    display_group = remark_group or watched_name or original_group or "未命名群"
    card["displayName"] = remark_name or original_name
    card["displayGroup"] = display_group
    card["nameRemarked"] = bool(remark_name)
    card["groupRemarked"] = bool(remark_group)
    card["originalName"] = original_name if remark_name and remark_name != original_name else ""
    card["originalGroup"] = (
        original_group if remark_group and remark_group != original_group else ""
    )
    card["starred"] = sender in {str(item) for item in settings.get("starred_users") or []}
    if sender:
        card["avatarUrl"] = f"/api/avatar?username={quote(sender)}"
    else:
        card["avatarUrl"] = ""
    quote_user = str(card.get("quoteUsername") or "")
    if quote_user:
        card["quoteAvatarUrl"] = f"/api/avatar?username={quote(quote_user)}"
    else:
        card["quoteAvatarUrl"] = ""
    return card


async def fetch_upstream_groups(upstream_base: str) -> list[dict[str, str]]:
    url = f"{upstream_base.rstrip('/')}/api/chat/sessions"
    response = await http_client().get(
        url, params={"source": "realtime"}, timeout=_SESSION_TIMEOUT
    )
    response.raise_for_status()
    return extract_groups(response.json())


async def _enrich_card(
    index: TokenIndex,
    hub: Hub,
    card_id: str,
    cas: list[dict[str, Any]],
) -> None:
    def push(tokens: list[dict[str, Any]]) -> None:
        if tokens:
            hub.patch(card_id, {"tokenCards": tokens})

    try:
        tokens = await index.resolve_many(cas, on_partial=push)
    except Exception as exc:
        logger.info("代币信息补全失败: {}", type(exc).__name__)
        return
    push(tokens)


def _ingest_card(
    card: dict[str, Any] | None,
    settings_store: SettingsStore,
    hub: Hub,
    token_index: TokenIndex,
) -> None:
    if card is None:
        return
    settings = settings_store.get()
    if not _allowed(settings, str(card["conversationId"])):
        return
    known = {item["id"]: item.get("name") or "" for item in settings.get("groups") or []}
    custom_name = known.get(str(card["conversationId"]))
    if custom_name:
        card["conversationName"] = custom_name
    apply_display(card, settings)
    if hub.publish(card) and card.get("cas"):
        asyncio.create_task(_enrich_card(token_index, hub, str(card["id"]), list(card["cas"])))


def _ingest_raw_message(
    message: dict[str, Any],
    username: str,
    conversation_name: str,
    settings_store: SettingsStore,
    hub: Hub,
    token_index: TokenIndex,
) -> None:
    card = normalize_message_event(
        {
            "type": "message",
            "username": username,
            "conversationName": conversation_name,
            "message": message,
        }
    )
    _ingest_card(card, settings_store, hub, token_index)


async def _pull_watched_groups(
    client: httpx.AsyncClient,
    settings: dict[str, Any],
    settings_store: SettingsStore,
    hub: Hub,
    token_index: TokenIndex,
) -> None:
    groups = list(settings.get("groups") or [])
    if not groups:
        return
    base = str(settings["upstream_base"]).rstrip("/")
    for group in groups:
        username = str(group.get("id") or "")
        if not username.endswith("@chatroom"):
            continue
        try:
            response = await client.get(
                f"{base}/api/chat/messages",
                params={
                    "username": username,
                    "limit": 20,
                    "offset": 0,
                    "order": "desc",
                    "source": "realtime",
                },
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            logger.info("增量拉取失败: {}", type(exc).__name__)
            continue
        name = str(group.get("name") or "")
        for item in payload.get("messages") or []:
            if isinstance(item, dict):
                _ingest_raw_message(item, username, name, settings_store, hub, token_index)


async def _read_sse(
    client: httpx.AsyncClient,
    url: str,
    stop: asyncio.Event,
    on_payload,
) -> None:
    async with client.stream("GET", url) as response:
        if response.status_code >= 400:
            await response.aread()
        response.raise_for_status()
        buffer = ""
        async for chunk in response.aiter_text():
            if stop.is_set():
                return
            buffer += chunk
            while "\n\n" in buffer:
                block, buffer = buffer.split("\n\n", 1)
                payload = parse_sse_block(block)
                if payload is None:
                    continue
                await on_payload(payload)


async def _consume_stream(
    client: httpx.AsyncClient,
    url: str,
    hub: Hub,
    settings_store: SettingsStore,
    stop: asyncio.Event,
    token_index: TokenIndex,
) -> None:
    hub.set_status("live", "已订阅本机微信消息（SSE），300ms 扫描会话游标")

    async def on_payload(payload: dict[str, Any]) -> None:
        if payload.get("type") != "message":
            return
        _ingest_card(normalize_message_event(payload), settings_store, hub, token_index)

    await _read_sse(client, url, stop, on_payload)


async def fetch_group_history(
    upstream_base: str,
    username: str,
    cutoff: int,
    limit_total: int = 1500,
) -> list[dict[str, Any]]:
    collected: list[dict[str, Any]] = []
    offset = 0
    timeout = httpx.Timeout(20.0, connect=5.0)
    client = http_client()
    while len(collected) < limit_total:
        response = await client.get(
            f"{upstream_base.rstrip('/')}/api/chat/messages",
            params={
                "username": username,
                "limit": 200,
                "offset": offset,
                "order": "desc",
                "source": "realtime",
            },
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
        batch = [item for item in (payload.get("messages") or []) if isinstance(item, dict)]
        if not batch:
            break
        stop_page = False
        for item in batch:
            try:
                created = int(item.get("createTime") or 0)
            except (TypeError, ValueError):
                created = 0
            if created and created < cutoff:
                stop_page = True
                break
            collected.append(item)
        if stop_page or not payload.get("hasMore"):
            break
        offset += len(batch)
    return collected


def _backfill_card(
    message: dict[str, Any],
    username: str,
    name: str,
    settings: dict[str, Any],
    hub: Hub,
) -> None:
    card = normalize_message_event(
        {
            "type": "message",
            "username": username,
            "conversationName": name,
            "message": message,
        }
    )
    if card is None or not _allowed(settings, username):
        return
    known = {item["id"]: item.get("name") or "" for item in settings.get("groups") or []}
    if known.get(username):
        card["conversationName"] = known[username]
    apply_display(card, settings)
    hub.publish(card, notify=False)


def _backfill_cutoff(hub: Hub, username: str) -> int:
    # 同一秒可能有多条，往回多留 1 秒，重复的由 id 去重。
    return max(int(time.time()) - KEEP_SECONDS, hub.stored_latest(username) - 1)


async def _repair_images(reader: WeChatDB, hub: Hub) -> None:
    persist = hub.persist
    if persist is None:
        return
    fixed = 0
    for card_id in await asyncio.to_thread(persist.images_without_media):
        ref = await asyncio.to_thread(reader.image_ref, card_id)
        if not ref:
            continue
        media = "/api/wximg?ref=" + quote(ref, safe="")
        hub.patch(card_id, {"mediaUrl": media, "mediaFullUrl": media + "&hd=1"}, notify=False)
        fixed += 1
        if fixed % 50 == 0:
            await asyncio.sleep(0)
    if fixed:
        logger.info("已给 {} 张旧图片卡片补上图片地址", fixed)
    stickers = 0
    for card_id in await asyncio.to_thread(persist.emoji_without_media):
        md5, url = await asyncio.to_thread(reader.emoji_media, card_id)
        if not md5 or not url:
            continue
        media = "/api/wxemoji?md5=" + quote(md5, safe="") + "&u=" + quote(url, safe="")
        hub.patch(card_id, {"mediaUrl": media}, notify=False)
        stickers += 1
        if stickers % 50 == 0:
            await asyncio.sleep(0)
    if stickers:
        logger.info("已给 {} 条旧表情卡片补上贴纸地址", stickers)


async def backfill_history(
    settings_store: SettingsStore,
    hub: Hub,
    stop: asyncio.Event,
    direct: DirectSource | None = None,
) -> None:
    settings = settings_store.get()
    groups = list(settings.get("groups") or [])
    reader = await direct.reader(str(settings["upstream_base"])) if direct else None
    if not groups:
        try:
            if reader is not None:
                groups = await asyncio.to_thread(reader.groups)
            else:
                groups = await fetch_upstream_groups(str(settings["upstream_base"]))
        except Exception as exc:
            logger.info("回拉会话列表失败: {}", type(exc).__name__)
            return
    rooms = [str(item.get("id") or "") for item in groups]
    rooms = [room for room in rooms if room.endswith("@chatroom")]
    if reader is not None:
        hub.set_status("connecting", "正在从本机微信库回拉近 30 天记录")
        try:
            await direct_backfill(
                reader,
                rooms,
                lambda room: _backfill_cutoff(hub, room),
                lambda message, room, name: _backfill_card(message, room, name, settings, hub),
                stop,
            )
        except Exception as exc:
            logger.info("直读回拉失败: {}", type(exc).__name__)
        try:
            await _repair_images(reader, hub)
        except Exception as exc:
            logger.info("补图片地址失败: {}", type(exc).__name__)
        hub.flush()
        logger.info("历史回拉完成（直读）")
        return
    hub.set_status("connecting", "正在从微信数据服务回拉近 30 天记录")
    names = {str(item.get("id") or ""): str(item.get("name") or "") for item in groups}
    for username in rooms:
        if stop.is_set():
            return
        try:
            raw_messages = await fetch_group_history(
                str(settings["upstream_base"]), username, _backfill_cutoff(hub, username)
            )
        except Exception as exc:
            logger.info("回拉群历史失败: {}", type(exc).__name__)
            continue
        for index, message in enumerate(reversed(raw_messages)):
            _backfill_card(message, username, names.get(username, ""), settings, hub)
            if index % 20 == 0:
                await asyncio.sleep(0)
        hub.flush()
    logger.info("历史回拉完成")


async def _keep_loop(
    name: str,
    hub: Hub,
    stop: asyncio.Event,
    live: set[str],
    worker,
) -> None:
    delay = 1.0
    timeout = httpx.Timeout(None, connect=5.0)
    while not stop.is_set():
        try:
            async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
                live.add(name)
                hub.set_status("live", "已订阅微信库文件变更，有写入才拉最新群消息")
                await worker(client)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("实时通道中断 {}: {}", name, type(exc).__name__)
            live.discard(name)
            if not live:
                hub.set_status("error", _describe_failure(exc))
        finally:
            live.discard(name)
        if stop.is_set():
            return
        await _pause(delay, stop)
        delay = min(delay * 2, 15.0)


async def _stream_round(
    hub: Hub,
    settings_store: SettingsStore,
    stop: asyncio.Event,
    token_index: TokenIndex,
) -> None:
    live: set[str] = set()
    pull_event = asyncio.Event()
    pull_event.set()

    async def messages_worker(client: httpx.AsyncClient) -> None:
        settings = settings_store.get()
        await _consume_stream(
            client,
            _message_stream_url(settings),
            hub,
            settings_store,
            stop,
            token_index,
        )

    async def changes_worker(client: httpx.AsyncClient) -> None:
        settings = settings_store.get()

        async def on_payload(payload: dict[str, Any]) -> None:
            if payload.get("type") in {"ready", "change"}:
                pull_event.set()

        await _read_sse(client, _change_stream_url(settings), stop, on_payload)

    async def pull_worker() -> None:
        last = 0.0
        client = http_client()
        while not stop.is_set():
            try:
                await asyncio.wait_for(pull_event.wait(), timeout=1.0)
            except TimeoutError:
                continue
            except asyncio.CancelledError:
                raise
            wait = last + _PULL_MIN_GAP - time.time()
            if wait > 0:
                await _pause(wait, stop)
            pull_event.clear()
            last = time.time()
            settings = settings_store.get()
            if not settings.get("groups"):
                continue
            try:
                await _pull_watched_groups(client, settings, settings_store, hub, token_index)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.info("变更拉取失败: {}", type(exc).__name__)

    settings = settings_store.get()
    tasks = [
        asyncio.create_task(_keep_loop("changes", hub, stop, live, changes_worker)),
        asyncio.create_task(pull_worker()),
    ]
    if not settings.get("groups"):
        tasks.append(asyncio.create_task(_keep_loop("messages", hub, stop, live, messages_worker)))
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _pause(seconds: float, stop: asyncio.Event) -> None:
    wake_at = time.time() + seconds
    while time.time() < wake_at and not stop.is_set():
        await asyncio.sleep(min(0.4, wake_at - time.time()))


def _watched_rooms(settings: dict[str, Any]) -> set[str] | None:
    groups = settings.get("groups") or []
    if not groups:
        return None
    return {str(item["id"]) for item in groups if str(item.get("id") or "").endswith("@chatroom")}


async def _direct_round(
    reader: WeChatDB,
    hub: Hub,
    settings_store: SettingsStore,
    stop: asyncio.Event,
    token_index: TokenIndex,
) -> None:
    hub.set_status("live", "直读本机微信库（不经过 WDA），库文件有写入就读新消息")
    await direct_stream(
        reader,
        lambda: _watched_rooms(settings_store.get()),
        lambda message, room, name: _ingest_raw_message(
            message, room, name, settings_store, hub, token_index
        ),
        stop,
    )


async def _wda_until_direct(
    hub: Hub,
    settings_store: SettingsStore,
    stop: asyncio.Event,
    token_index: TokenIndex,
    direct: DirectSource | None,
) -> bool:
    """跑 WDA 通道；直读可用时提前结束并返回 True。"""
    round_task = asyncio.create_task(_stream_round(hub, settings_store, stop, token_index))
    if direct is None or direct_disabled():
        await round_task
        return False

    async def wait_direct() -> None:
        while not stop.is_set():
            await _pause(_DIRECT_RETRY, stop)
            if await direct.reader(str(settings_store.get()["upstream_base"])) is not None:
                return

    watch = asyncio.create_task(wait_direct())
    try:
        done, _pending = await asyncio.wait(
            {round_task, watch}, return_when=asyncio.FIRST_COMPLETED
        )
    finally:
        for task in (round_task, watch):
            if not task.done():
                task.cancel()
        await asyncio.gather(round_task, watch, return_exceptions=True)
    if round_task in done:
        round_task.result()
        return False
    logger.info("直读可用，WDA 通道停用")
    return True


async def stream_until_stopped(
    settings_store: SettingsStore,
    hub: Hub,
    stop: asyncio.Event,
    token_index: TokenIndex,
    direct: DirectSource | None = None,
) -> None:
    delay = 1.0
    while not stop.is_set():
        settings = settings_store.get()
        urls = _stream_urls(settings)
        if not urls:
            hub.set_status("error", "当前没有可监控的群。把屏蔽去掉或添加群后再看。")
            await _pause(2.0, stop)
            continue
        reader = await direct.reader(str(settings["upstream_base"])) if direct else None
        if reader is not None:
            try:
                await _direct_round(reader, hub, settings_store, stop, token_index)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("直读中断，改走 WDA: {}", type(exc).__name__)
                direct.drop(f"直读中断（{type(exc).__name__}），暂时改走 WDA")
            continue
        hub.set_status("connecting", "正在连接本机微信数据服务")
        try:
            if await _wda_until_direct(hub, settings_store, stop, token_index, direct):
                continue
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("微信数据服务连接中断: {}", type(exc).__name__)
            hub.set_status("error", _describe_failure(exc))
        if stop.is_set():
            return
        await _pause(delay, stop)
        delay = min(delay * 2, 30.0)


async def supervise(
    settings_store: SettingsStore,
    hub: Hub,
    stop: asyncio.Event,
    token_index: TokenIndex,
    direct: DirectSource | None = None,
) -> None:
    def start() -> asyncio.Task[None]:
        return asyncio.create_task(
            stream_until_stopped(settings_store, hub, stop, token_index, direct)
        )

    task: asyncio.Task[None] | None = None
    signature: tuple[Any, ...] | None = None
    while not stop.is_set():
        settings = settings_store.get()
        current = (
            settings["upstream_base"],
            tuple(item["id"] for item in settings["groups"]),
        )
        if current != signature:
            signature = current
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            task = start()
        elif task is not None and task.done():
            task = start()
        try:
            await asyncio.wait_for(stop.wait(), timeout=1.0)
        except TimeoutError:
            continue
    if task is not None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
