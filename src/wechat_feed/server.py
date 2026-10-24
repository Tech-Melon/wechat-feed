"""只监听本机的信息流接口和本地页面。"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

from . import http as feed_http
from .castore import BOARD_RANGES, CaStore
from .config import (
    SettingsStore,
    data_dir,
    display_group_ids,
    group_senders_by_id,
    project_root,
    validate_group_id,
)
from .direct import DirectSource
from .hub import Hub
from .normalize import normalize_message_event
from .store import CardStore
from .tokens import TokenIndex, allowed_chainicon_url, chain_favicon
from .upstream import apply_display, backfill_history, fetch_upstream_groups, supervise
from .wxemoji import (
    STICKER_MAX,
    allowed_emote_url,
    allowed_sticker_url,
    decode_query_url,
    emote_source,
    pick_mime,
    sticker_cache,
    valid_md5,
    within_limit,
)
from .wximage import load_image, parse_ref, ref_for

_AVATAR_USER = re.compile(r"^[A-Za-z0-9_@.\-]{1,80}$")
_AVATAR_TTL = 24 * 3600
_AVATAR_CACHE = "private, max-age=86400"

_LISTEN_HOST = "127.0.0.1"
_LISTEN_PORT = 8765
_PAGE_SIZE = 30
# 节奏交给限频窗口控制，这里只留很短的喘息；没有到期的币时 3 秒看一次。
_IDLE_GAP = 0.2
_IDLE_POLL = 3
_USER_ID = re.compile(r"^[A-Za-z0-9_@.\-]{1,80}$")


async def _idle_ca_refresh(
    stop: asyncio.Event,
    cas: CaStore,
    tokens: TokenIndex,
    store: SettingsStore,
    hub: Hub,
) -> None:
    """后台刷榜：先首结算新喊单，再按热度增量刷新。实时解析优先，后台只用剩余额度。"""
    try:
        await asyncio.wait_for(stop.wait(), timeout=15)
        return
    except TimeoutError:
        pass
    while not stop.is_set():
        try:
            settings = store.get()
            if not settings.get("ca_board", True):
                await asyncio.wait_for(stop.wait(), timeout=20)
                continue
            if not tokens.has_key():
                await asyncio.wait_for(stop.wait(), timeout=20)
                continue
            if not tokens.idle():
                # 实时解析在跑或后台被 429 冷却中：短等再看，不再一停 8 秒。
                await asyncio.wait_for(stop.wait(), timeout=1)
                continue
            row = cas.next_due(int(time.time()))
            if row is None:
                await asyncio.wait_for(stop.wait(), timeout=_IDLE_POLL)
                continue
            chain = str(row.get("chain") or "")
            address = str(row.get("address") or "")
            anchors = cas.plan_anchors(chain, address)
            # 只按锚点拉：水位线之后共用一份，未结算喊单补自己的细粒度段。
            quote = await tokens.quote(
                address,
                chain,
                last_mcap=float(row.get("last_mcap") or 0),
                last_price=float(row.get("last_price") or 0),
                anchors=anchors,
            )
            if quote is None:
                cas.mark_attempt(chain, address)
            elif cas.apply_quote(chain, address, quote, anchors=anchors):
                hub.notify_board()
            await asyncio.sleep(_IDLE_GAP)
        except TimeoutError:
            continue
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.info("空闲刷新 CA 失败: {}", type(exc).__name__)
            try:
                await asyncio.wait_for(stop.wait(), timeout=8)
            except TimeoutError:
                continue


def public_settings(settings: dict[str, Any]) -> dict[str, Any]:
    payload = dict(settings)
    payload["has_gmgn_key"] = bool(settings.get("gmgn_api_key"))
    payload["gmgn_api_key"] = ""
    return payload


def demo_event() -> dict[str, Any]:
    local_id = time.time_ns()
    address = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"
    return {
        "type": "message",
        "username": "10001@chatroom",
        "conversationName": "样例群",
        "message": {
            "id": f"demo:{local_id}",
            "localId": local_id,
            "createTime": int(time.time()),
            "senderUsername": "wxid_demo",
            "senderDisplayName": "样例发言",
            "isSent": False,
            "renderType": "quote",
            "content": f"群里看到合约 {address} 冲不冲",
            "quoteTitle": "群友",
            "quoteUsername": "wxid_quoted",
            "quoteContent": "按键加原始输入法，甚至不用看手机也能打字",
        },
    }


def _sse(payload: dict[str, Any]) -> str:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"data: {body}\n\n"


def create_app() -> FastAPI:
    store = SettingsStore(data_dir() / "settings.json")
    persist = CardStore(data_dir() / "feed.db")
    cas = CaStore(data_dir() / "ca.db")
    hub = Hub(persist=persist, cas=cas)
    token_index = TokenIndex(lambda: str(store.get().get("gmgn_api_key") or ""))
    direct = DirectSource(data_dir() / "direct.json")
    root = project_root()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.store = store
        app.state.hub = hub
        app.state.persist = persist
        app.state.cas = cas
        app.state.tokens = token_index
        persist.prune()
        cas.prune()
        for card in persist.token_cards():
            cas.remember(card, list(card.get("tokenCards") or []))
        dropped = cas.prune_bad()
        if dropped:
            logger.info("已丢弃异常历史 CA {} 条", dropped)
        stop = asyncio.Event()
        tasks: list[asyncio.Task[None]] = []
        if os.environ.get("WECHAT_FEED_DISABLE_UPSTREAM") != "1":
            tasks.append(asyncio.create_task(backfill_history(store, hub, stop, direct)))
            tasks.append(asyncio.create_task(supervise(store, hub, stop, token_index, direct)))
        if os.environ.get("WECHAT_FEED_DISABLE_TOKEN") != "1":
            tasks.append(asyncio.create_task(_idle_ca_refresh(stop, cas, token_index, store, hub)))
        yield
        stop.set()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await feed_http.aclose()

    app = FastAPI(title="技术瓜群流", lifespan=lifespan)
    # 网页不能跨源读群消息；只放行浏览器扩展，本地页是同源。
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"^chrome-extension://[a-p]{32}$",
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.store = store
    app.state.hub = hub
    app.state.persist = persist

    def _token_needs_gmgn(card: dict[str, Any]) -> bool:
        if not card.get("cas"):
            return False
        tokens = card.get("tokenCards") or []
        if not tokens:
            return True
        return any(str(item.get("source") or "") == "dexscreener" for item in tokens)

    async def enrich_missing(card: dict[str, Any]) -> None:
        if not _token_needs_gmgn(card):
            return
        card_id = str(card["id"])

        def push(tokens: list[dict[str, Any]]) -> None:
            if tokens:
                remembered = cas.remember(card, tokens)
                hub.patch(card_id, {"tokenCards": remembered})

        try:
            tokens = await token_index.resolve_many(list(card["cas"]), on_partial=push)
        except Exception as exc:
            logger.info("代币信息补全失败: {}", type(exc).__name__)
            return
        push(tokens)

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {
            "ok": True,
            "host": _LISTEN_HOST,
            "port": _LISTEN_PORT,
            "runtime": hub.status(),
            "source": "direct" if direct.active else "wda",
            "sourceDetail": direct.detail,
            "stored": persist.count(),
        }

    @app.get("/api/settings")
    def get_settings() -> dict[str, Any]:
        return public_settings(store.get())

    @app.put("/api/settings")
    async def put_settings(request: Request) -> dict[str, Any]:
        try:
            patch = await request.json()
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="配置不是合法 JSON") from exc
        if not isinstance(patch, dict):
            raise HTTPException(status_code=400, detail="配置必须是对象")
        try:
            settings = store.replace(patch)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        public = public_settings(settings)
        hub.notify_settings(public)
        return public

    @app.get("/api/messages")
    async def get_messages(
        limit: int = _PAGE_SIZE,
        before: int | None = Query(default=None),
        before_received: int | None = Query(default=None),
        conversation_id: str = Query(default=""),
        ca_only: bool = Query(default=False),
        include_all_groups: bool = Query(default=False),
    ) -> dict[str, Any]:
        settings = store.get()
        blocked = list(settings.get("blocked_groups") or [])
        blocked_senders = [str(item) for item in (settings.get("blocked_users") or []) if item]
        senders = group_senders_by_id(settings)
        allowed = None
        if not conversation_id and not include_all_groups:
            allowed = display_group_ids(settings, ca_only=ca_only) or None
        cards, has_more = persist.page(
            limit=limit,
            before=before,
            before_received=before_received,
            conversation_id=conversation_id or None,
            blocked=blocked,
            allowed=allowed,
            ca_only=ca_only,
            senders_by_group=senders or None,
            blocked_senders=blocked_senders or None,
        )
        shown = [apply_display(dict(card), settings) for card in cards]
        for card in shown:
            persist.fill_context(card)
            cas.decorate_card(card)
        pending = 0
        for card in shown:
            if pending >= 12:
                break
            if _token_needs_gmgn(card):
                asyncio.create_task(enrich_missing(card))
                pending += 1
        return {
            "messages": shown,
            "has_more": has_more,
            "stored": persist.count(),
        }

    def _avatar_files(ident: str) -> tuple[Path, Path]:
        folder = data_dir() / "avatars"
        folder.mkdir(parents=True, exist_ok=True)
        safe = ident.replace("/", "_").replace("\\", "_")
        return folder / f"{safe}.bin", folder / f"{safe}.mime"

    @app.get("/api/avatar")
    async def avatar(username: str) -> Response:
        ident = str(username or "").strip()
        if not _AVATAR_USER.match(ident):
            raise HTTPException(status_code=400, detail="用户名不合法")
        blob_path, mime_path = _avatar_files(ident)

        def cached_response() -> Response | None:
            if not blob_path.exists():
                return None
            media_type = "image/jpeg"
            if mime_path.exists():
                media_type = mime_path.read_text(encoding="utf-8").strip() or media_type
            return Response(
                content=blob_path.read_bytes(),
                media_type=media_type,
                headers={"Cache-Control": _AVATAR_CACHE},
            )

        cached = cached_response()
        if cached and time.time() - blob_path.stat().st_mtime < _AVATAR_TTL:
            return cached
        settings = store.get()
        reader = await direct.reader(str(settings["upstream_base"]))
        if reader is not None:
            try:
                blob = await asyncio.to_thread(reader.avatar, ident)
            except Exception as exc:
                logger.info("直读头像失败: {}", type(exc).__name__)
                blob = None
            if blob:
                media_type = "image/png" if blob[:4] == b"\x89PNG" else "image/jpeg"
                blob_path.write_bytes(blob)
                mime_path.write_text(media_type, encoding="utf-8")
                return Response(
                    content=blob,
                    media_type=media_type,
                    headers={"Cache-Control": _AVATAR_CACHE},
                )
        url = f"{settings['upstream_base']}/api/chat/avatar"
        try:
            response = await feed_http.client().get(
                url, params={"username": ident, "source": "auto"}, timeout=8.0
            )
        except Exception as exc:
            if cached is not None:
                return cached
            logger.info("头像读取失败: {}", type(exc).__name__)
            raise HTTPException(status_code=502, detail="头像读不到") from exc
        content_type = str(response.headers.get("content-type") or "")
        if response.status_code != 200 or not content_type.startswith("image/"):
            if cached is not None:
                return cached
            raise HTTPException(status_code=404, detail="没有头像")
        blob_path.write_bytes(response.content)
        mime_path.write_text(content_type, encoding="utf-8")
        return Response(
            content=response.content,
            media_type=content_type,
            headers={"Cache-Control": _AVATAR_CACHE},
        )

    async def local_media(query: str) -> Response | None:
        """WDA 关掉后，旧卡片里的 WDA 图片地址改从本机直接解。"""
        from urllib.parse import parse_qs

        params = parse_qs(query)
        name = (params.get("md5") or [""])[0]
        room = (params.get("username") or [""])[0]
        reader = await direct.reader(str(store.get()["upstream_base"]))
        if reader is None or not room.endswith("@chatroom"):
            return None
        ref = await asyncio.to_thread(ref_for, reader.wx_dir, room, name)
        if not ref:
            return None
        keys = direct.image_keys()
        aes_key, xor_key = keys if keys else (None, None)
        found = await asyncio.to_thread(load_image, reader.wx_dir, ref, aes_key, xor_key)
        if found is None:
            return None
        blob, media_type, _kind = found
        return Response(
            content=blob,
            media_type=media_type,
            headers={"Cache-Control": "private, max-age=604800, immutable"},
        )

    @app.get("/api/media")
    async def media(u: str) -> Response:
        from urllib.parse import unquote, urlsplit

        raw = unquote(str(u or ""))
        parsed = urlsplit(raw)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "http" or host not in {"127.0.0.1", "localhost"}:
            raise HTTPException(status_code=400, detail="只允许读取本机媒体")
        if parsed.port != 10392:
            raise HTTPException(status_code=400, detail="媒体端口不正确")
        try:
            response = await feed_http.client().get(raw, timeout=10.0)
        except Exception as exc:
            fallback = await local_media(parsed.query)
            if fallback is not None:
                return fallback
            logger.info("媒体读取失败: {}", type(exc).__name__)
            raise HTTPException(status_code=502, detail="媒体读不到") from exc
        content_type = str(response.headers.get("content-type") or "application/octet-stream")
        if response.status_code != 200:
            raise HTTPException(status_code=404, detail="没有这张图")
        return Response(
            content=response.content,
            media_type=content_type,
            headers={"Cache-Control": "private, max-age=86400"},
        )

    @app.get("/api/wximg")
    async def wx_image(ref: str, hd: bool = False) -> Response:
        if parse_ref(ref) is None:
            raise HTTPException(status_code=400, detail="图片引用不合法")
        reader = await direct.reader(str(store.get()["upstream_base"]))
        if reader is None:
            raise HTTPException(status_code=503, detail="直读模式没有打开")
        keys = direct.image_keys()
        aes_key, xor_key = keys if keys else (None, None)
        found = await asyncio.to_thread(load_image, reader.wx_dir, ref, aes_key, xor_key, hd=hd)
        if found is None:
            # 微信可能还没下载完缩略图，不能让浏览器缓存这次失败。
            raise HTTPException(
                status_code=404,
                detail="图片还没下载或解不开",
                headers={"Cache-Control": "no-store"},
            )
        blob, media_type, kind = found
        # 高清位要的是 _h / 原图。若微信还没下，这里只能拿出缩略图，不能当高清缓存一周。
        cache = "no-store" if hd and kind == "t" else "private, max-age=604800, immutable"
        return Response(
            content=blob,
            media_type=media_type,
            headers={"Cache-Control": cache, "X-Wx-Image": kind},
        )

    async def _cached_image(
        url: str,
        cache: Path,
        ceiling: int,
        allow: Callable[[str], bool],
        *,
        cache_header: str = "private, max-age=604800, immutable",
        timeout: float = 8.0,
    ) -> Response:
        if cache.is_file():
            blob = cache.read_bytes()
            mime = pick_mime(blob)
            if mime:
                return Response(
                    content=blob,
                    media_type=mime,
                    headers={"Cache-Control": cache_header},
                )
        try:
            remote = await feed_http.get_hops(
                url,
                allow,
                timeout=timeout,
                headers={"User-Agent": "Mozilla/5.0"},
            )
        except feed_http.HopDenied as exc:
            logger.info("远程图片跳转被拒: {}", exc.url)
            raise HTTPException(status_code=400, detail="图片地址不合法") from exc
        except Exception as exc:
            logger.info("远程图片下载失败: {}", type(exc).__name__)
            raise HTTPException(status_code=502, detail="图片读不到") from exc
        if remote.status_code != 200 or not within_limit(remote.content, ceiling):
            logger.info(
                "远程图片不合格 status={} bytes={} ceiling={}",
                remote.status_code,
                len(remote.content),
                ceiling,
            )
            raise HTTPException(status_code=404, detail="图片读不到")
        mime = pick_mime(remote.content)
        if not mime:
            raise HTTPException(status_code=404, detail="不是图片")
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(remote.content)
        return Response(
            content=remote.content,
            media_type=mime,
            headers={"Cache-Control": cache_header},
        )

    @app.get("/api/wxemoji")
    async def wx_sticker(md5: str, u: str = "") -> Response:
        if not valid_md5(md5):
            raise HTTPException(status_code=400, detail="表情引用不合法")
        url = decode_query_url(u)
        if not allowed_sticker_url(url):
            raise HTTPException(status_code=400, detail="表情地址不合法")
        return await _cached_image(
            url,
            sticker_cache(data_dir() / "stickers", md5),
            STICKER_MAX,
            allowed_sticker_url,
            timeout=20.0,
        )

    @app.get("/api/wxemote")
    async def wx_emote(n: str = "") -> Response:
        source = emote_source(n)
        if source is None:
            raise HTTPException(status_code=404, detail="没有这个表情")
        url, filename = source
        return await _cached_image(url, data_dir() / "emotes" / filename, 80_000, allowed_emote_url)

    @app.get("/api/chainicon")
    async def chain_icon(n: str = "") -> Response:
        source = chain_favicon(n)
        if source is None:
            raise HTTPException(status_code=404, detail="没有这个链图标")
        url, filename = source
        return await _cached_image(
            url,
            data_dir() / "chainicons" / filename,
            80_000,
            allowed_chainicon_url,
            cache_header="private, max-age=31536000, immutable",
        )

    @app.delete("/api/messages")
    def delete_messages() -> dict[str, bool]:
        hub.clear()
        persist.clear()
        return {"ok": True}

    @app.get("/api/groups")
    async def get_groups() -> dict[str, Any]:
        settings = store.get()
        started = time.perf_counter()
        try:
            reader = await direct.reader(str(settings["upstream_base"]))
            if reader is not None:
                groups = await asyncio.to_thread(reader.groups)
                source = "direct"
            else:
                groups = await fetch_upstream_groups(str(settings["upstream_base"]))
                source = "wda"
        except Exception as exc:
            logger.info("读取群列表失败: {}", type(exc).__name__)
            raise HTTPException(
                status_code=502,
                detail="读不到本机微信会话。请确认 WeChatDataAnalysis 已启动并完成解密。",
            ) from exc
        logger.info(
            "读取群列表 {} 个（{}）{:.0f}ms",
            len(groups),
            source,
            (time.perf_counter() - started) * 1000,
        )
        return {"groups": groups}

    @app.get("/api/groups/{group_id}/members")
    async def get_members(group_id: str) -> dict[str, Any]:
        try:
            room = validate_group_id(group_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        settings = store.get()
        started = time.perf_counter()
        reader = await direct.reader(str(settings["upstream_base"]))
        if reader is None:
            raise HTTPException(status_code=503, detail="直读模式没有打开，暂时读不到群成员")
        try:
            members = await asyncio.to_thread(reader.members, room)
        except Exception as exc:
            logger.info("读取群成员失败: {}", type(exc).__name__)
            raise HTTPException(status_code=502, detail="读不到这个群的成员") from exc
        logger.info(
            "读取群成员 {} 个（{}）{:.0f}ms",
            len(members),
            room,
            (time.perf_counter() - started) * 1000,
        )
        return {"groupId": room, "members": members}

    @app.post("/api/demo")
    async def post_demo() -> dict[str, Any]:
        card = normalize_message_event(demo_event(), demo=True)
        if card is None:
            raise HTTPException(status_code=500, detail="样例消息没有写入")
        apply_display(card, store.get())
        if not hub.publish(card):
            raise HTTPException(status_code=500, detail="样例消息没有写入")
        if card.get("cas"):
            tokens = await token_index.resolve_many(list(card["cas"]))
            if tokens:
                tokens = cas.remember(card, tokens)
                hub.patch(str(card["id"]), {"tokenCards": tokens})
                card["tokenCards"] = tokens
        return {"ok": True, "card": card}

    @app.get("/api/ca-board")
    def ca_board(range: str = "all") -> dict[str, Any]:  # noqa: A002 - 查询参数名
        enabled = bool(store.get().get("ca_board", True))
        if not enabled:
            return {"ok": True, "enabled": False, "board": [], "stats": {}}
        window = range if range in BOARD_RANGES else "all"
        return {
            "ok": True,
            "enabled": True,
            "range": window,
            "board": cas.board(window),
            "stats": cas.stats(),
        }

    @app.get("/api/ca-board/user/{user_id}")
    def ca_board_user(user_id: str) -> dict[str, Any]:
        ident = str(user_id or "").strip()
        if not _USER_ID.match(ident):
            raise HTTPException(status_code=400, detail="用户名不合法")
        payload = cas.user_profile(ident)
        return {"ok": True, **payload}

    @app.get("/api/events")
    async def events() -> StreamingResponse:
        queue = hub.subscribe()

        async def generate():
            try:
                yield _sse(
                    {
                        "type": "hello",
                        "status": hub.status(),
                        "settings": public_settings(store.get()),
                    }
                )
                while True:
                    try:
                        item = await asyncio.wait_for(queue.get(), timeout=15)
                    except TimeoutError:
                        yield ": ping\n\n"
                        continue
                    yield _sse(item)
            finally:
                hub.unsubscribe(queue)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(
            root / "web" / "index.html",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/shared/feed.js")
    def shared_feed() -> FileResponse:
        return FileResponse(
            root / "extension" / "feed.js",
            media_type="text/javascript",
            headers={"Cache-Control": "no-store"},
        )

    app.mount("/assets", StaticFiles(directory=root / "web"), name="assets")
    return app


def serve() -> None:
    import uvicorn

    logger.info("本地页面 http://{}:{}/", _LISTEN_HOST, _LISTEN_PORT)
    uvicorn.run(
        create_app(),
        host=_LISTEN_HOST,
        port=_LISTEN_PORT,
        log_config=None,
        access_log=False,
    )
