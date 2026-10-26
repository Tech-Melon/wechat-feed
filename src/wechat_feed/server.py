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
from .castore import BOARD_RANGES, REFRESH_MAX, USER_RANGES, CaStore
from .config import (
    SettingsStore,
    data_dir,
    display_group_ids,
    group_senders_by_id,
    project_root,
    validate_group_id,
)
from .direct import DirectSource
from .gmgnstore import HISTORY_RANGES, GmgnStore
from .hub import Hub
from .normalize import normalize_message_event
from .store import CardStore
from .tokens import TokenIndex, allowed_chainicon_url, chain_favicon, manual_timeout
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
# 头像落盘缓存 3 天，期间不再读微信库；浏览器和插件也按 3 天缓存。
_AVATAR_TTL = 3 * 24 * 3600
_AVATAR_CACHE = "private, max-age=259200"
# 微信库里确实没有头像的人记一个标记，1 天内直接 404，不再每次查库、连 WDA。
_AVATAR_MISS_TTL = 24 * 3600
_AVATAR_MISS_CACHE = "private, max-age=3600"

_LISTEN_HOST = "127.0.0.1"
_LISTEN_PORT = 8765
_PAGE_SIZE = 30
# 节奏交给限频窗口控制，这里只留很短的喘息；没有到期的币时 3 秒看一次。
_IDLE_GAP = 0.2
_IDLE_POLL = 3
_USER_ID = re.compile(r"^[A-Za-z0-9_@.\-]{1,80}$")
# GMGN 计量每 5 秒批量落盘一次，每 6 小时清一次过期数据。
_METER_FLUSH = 5.0
_METER_PRUNE = 6 * 3600
_PURGE_TARGETS = {"board", "gmgn", "all"}


async def _flush_meter(stop: asyncio.Event, tokens: TokenIndex, gm: GmgnStore) -> None:
    last_prune = 0.0
    while True:
        try:
            await asyncio.wait_for(stop.wait(), timeout=_METER_FLUSH)
            stopping = True
        except TimeoutError:
            stopping = False
        batch, stats = tokens.meter.drain()
        if batch:
            try:
                await asyncio.to_thread(gm.write, batch, stats)
            except Exception as exc:
                tokens.meter.requeue(batch)
                logger.info("GMGN 计量落盘失败: {}", type(exc).__name__)
        if time.time() - last_prune > _METER_PRUNE:
            last_prune = time.time()
            try:
                await asyncio.to_thread(gm.prune)
            except Exception as exc:
                logger.info("GMGN 计量清理失败: {}", type(exc).__name__)
        if stopping:
            return


class RefreshJob:
    """用户详情页点「立即刷新」后的手动任务。同一时间只跑一个，跑的时候后台定时刷新让开。"""

    def __init__(self) -> None:
        self.task: asyncio.Task[None] | None = None
        self.state: dict[str, Any] = {"running": False}

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    def start(
        self,
        user_id: str,
        scope: str,
        window: str,
        targets: list[dict[str, Any]],
        cas: CaStore,
        tokens: TokenIndex,
        hub: Hub,
    ) -> dict[str, Any]:
        self.state = {
            "running": True,
            "userId": user_id,
            "scope": scope,
            "range": window,
            "total": len(targets),
            "done": 0,
            "updated": 0,
            "failed": 0,
            "startedAt": time.time(),
            "finishedAt": 0.0,
        }
        self.task = asyncio.create_task(self._run(targets, cas, tokens, hub))
        return dict(self.state)

    async def _run(
        self, targets: list[dict[str, Any]], cas: CaStore, tokens: TokenIndex, hub: Hub
    ) -> None:
        # 手动刷新期间定时刷新的 GMGN 请求全部暂停；超时按币数算，到点就停，后台自动恢复。
        limit = manual_timeout(len(targets))
        self.state["timeoutSec"] = limit
        tokens.manual_begin(limit)
        deadline = time.time() + limit
        try:
            for row in targets:
                if time.time() >= deadline:
                    self.state["timedOut"] = True
                    break
                chain = row["chain"]
                address = row["address"]
                anchors = cas.plan_anchors(chain, address)
                try:
                    # 单个币最多等 60 秒，网络卡住也不会把整个任务挂死。
                    quote = await asyncio.wait_for(
                        tokens.quote(
                            address,
                            chain,
                            last_mcap=float(row.get("last_mcap") or 0),
                            last_price=float(row.get("last_price") or 0),
                            anchors=anchors,
                            manual=True,
                        ),
                        timeout=max(1.0, min(60.0, deadline - time.time())),
                    )
                except Exception as exc:
                    logger.info("手动刷新 CA 失败: {}", type(exc).__name__)
                    quote = None
                if quote is None or not quote.get("klineOk"):
                    self.state["failed"] += 1
                    cas.mark_attempt(chain, address)
                elif cas.apply_quote(chain, address, quote, anchors=anchors):
                    self.state["updated"] += 1
                    hub.notify_board()
                self.state["done"] += 1
        finally:
            tokens.manual_end()
            self.state["running"] = False
            self.state["finishedAt"] = time.time()
            hub.notify_board()


async def _idle_ca_refresh(
    stop: asyncio.Event,
    cas: CaStore,
    tokens: TokenIndex,
    store: SettingsStore,
    hub: Hub,
    job: RefreshJob | None = None,
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
            if not tokens.idle() or (job is not None and job.running):
                # 实时解析在跑、后台被 429 冷却中，或手动刷新在跑：短等再看。
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
    gmgn_db = GmgnStore(data_dir() / "gmgn.db")
    refresh_job = RefreshJob()
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
            cas.remember(card, list(card.get("tokenCards") or []), with_stats=False)
        dropped = cas.prune_bad()
        if dropped:
            logger.info("已丢弃异常历史 CA {} 条", dropped)
        gmgn_db.prune()
        # 重启后把最近 1 小时的明细装回内存，用量页不会清零。
        token_index.meter.preload(gmgn_db.load_recent(3600))
        stop = asyncio.Event()
        tasks: list[asyncio.Task[None]] = [
            asyncio.create_task(_flush_meter(stop, token_index, gmgn_db))
        ]
        if os.environ.get("WECHAT_FEED_DISABLE_UPSTREAM") != "1":
            tasks.append(asyncio.create_task(backfill_history(store, hub, stop, direct)))
            tasks.append(asyncio.create_task(supervise(store, hub, stop, token_index, direct)))
        if os.environ.get("WECHAT_FEED_DISABLE_TOKEN") != "1":
            tasks.append(
                asyncio.create_task(
                    _idle_ca_refresh(stop, cas, token_index, store, hub, refresh_job)
                )
            )
        yield
        stop.set()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        # 退出前把最后一批计量写进库。
        batch, stats = token_index.meter.drain()
        if batch:
            gmgn_db.write(batch, stats)
        gmgn_db.close()
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

        def build() -> tuple[list[dict[str, Any]], bool, int]:
            cards, more = persist.page(
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
            out = [apply_display(dict(card), settings) for card in cards]
            for card in out:
                persist.fill_context(card)
                cas.decorate_card(card)
            return out, more, persist.count()

        # 查库和组装都是同步的，放线程里跑，避免慢查询卡住事件循环（榜单、头像、SSE 会一起排队）。
        shown, has_more, stored = await asyncio.to_thread(build)
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
            "stored": stored,
        }

    def _avatar_files(ident: str) -> tuple[Path, Path, Path]:
        folder = data_dir() / "avatars"
        folder.mkdir(parents=True, exist_ok=True)
        safe = ident.replace("/", "_").replace("\\", "_")
        return folder / f"{safe}.bin", folder / f"{safe}.mime", folder / f"{safe}.miss"

    def _fresh(path: Path, ttl: int) -> bool:
        try:
            return time.time() - path.stat().st_mtime < ttl
        except OSError:
            return False

    def _save_avatar(blob_path: Path, mime_path: Path, miss_path: Path, blob: bytes, mime: str):
        blob_path.write_bytes(blob)
        mime_path.write_text(mime, encoding="utf-8")
        miss_path.unlink(missing_ok=True)

    @app.get("/api/avatar")
    async def avatar(username: str) -> Response:
        ident = str(username or "").strip()
        if not _AVATAR_USER.match(ident):
            raise HTTPException(status_code=400, detail="用户名不合法")
        blob_path, mime_path, miss_path = _avatar_files(ident)
        missing = HTTPException(
            status_code=404, detail="没有头像", headers={"Cache-Control": _AVATAR_MISS_CACHE}
        )

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
        if cached and _fresh(blob_path, _AVATAR_TTL):
            return cached
        if cached is None and _fresh(miss_path, _AVATAR_MISS_TTL):
            raise missing

        def keep_stale() -> Response:
            # 刷新失败就继续用旧图，并把缓存时间往后顺延，不让每次请求都重新查库、连 WDA。
            os.utime(blob_path)
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
                _save_avatar(blob_path, mime_path, miss_path, blob, media_type)
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
                return keep_stale()
            if reader is not None:
                # 直读可用、库里没有这个人的头像、WDA 也没开：记为没有头像。
                miss_path.touch()
                raise missing from exc
            logger.info("头像读取失败: {}", type(exc).__name__)
            raise HTTPException(status_code=502, detail="头像读不到") from exc
        content_type = str(response.headers.get("content-type") or "")
        if response.status_code != 200 or not content_type.startswith("image/"):
            if cached is not None:
                return keep_stale()
            miss_path.touch()
            raise missing
        _save_avatar(blob_path, mime_path, miss_path, response.content, content_type)
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

    @app.post("/api/ca-quotes")
    async def ca_quotes(request: Request) -> dict[str, Any]:
        """浮窗「最近 CA」：读本地最新市值；watch=true 时把这些币排进后台加急刷新。"""
        try:
            body = await request.json()
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="请求不是合法 JSON") from exc
        items = body.get("items") if isinstance(body, dict) else None
        if not isinstance(items, list):
            raise HTTPException(status_code=400, detail="items 必须是数组")
        items = [item for item in items[:20] if isinstance(item, dict)]
        if body.get("watch") and store.get().get("ca_board", True):
            cas.watch(items)
        quotes = await asyncio.to_thread(cas.quotes, items)
        return {"ok": True, "quotes": quotes, "now": int(time.time())}

    @app.get("/api/gmgn-stats")
    def gmgn_stats() -> dict[str, Any]:
        """GMGN 调用计量：最近 1 小时实时面板（不含 Key 与请求参数）。"""
        return {"ok": True, "hasKey": token_index.has_key(), **token_index.meter_snapshot()}

    @app.get("/api/gmgn-history")
    async def gmgn_history(range: str = "24h") -> dict[str, Any]:  # noqa: A002 - 查询参数名
        window = range if range in HISTORY_RANGES else "24h"
        return {"ok": True, **(await asyncio.to_thread(gmgn_db.history, window))}

    @app.get("/api/data")
    async def data_summary() -> dict[str, Any]:
        board = await asyncio.to_thread(cas.data_summary)
        gmgn = await asyncio.to_thread(gmgn_db.summary)
        return {"ok": True, "board": board, "gmgn": gmgn}

    async def _json_body(request: Request) -> dict[str, Any]:
        # 只收 application/json：跨站表单 POST 发不了这个类型，必须先过 CORS 预检，网页删不了数据。
        kind = str(request.headers.get("content-type") or "").split(";")[0].strip().lower()
        if kind != "application/json":
            raise HTTPException(status_code=415, detail="只接受 application/json")
        try:
            body = await request.json()
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="不是合法 JSON") from exc
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="请求体必须是对象")
        return body

    @app.post("/api/data/purge")
    async def data_purge(request: Request) -> dict[str, Any]:
        body = await _json_body(request)
        target = str(body.get("target") or "")
        if target not in _PURGE_TARGETS:
            raise HTTPException(status_code=400, detail="target 只能是 board / gmgn / all")
        try:
            start = int(body.get("start"))
            end = int(body.get("end"))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="start / end 必须是秒级时间戳") from exc
        # 结束时间可以在未来（删「当前这个月」时月末就在未来），只拦离谱的时间戳。
        if start < 0 or end <= start or end > 4_102_444_800:
            raise HTTPException(status_code=400, detail="时间范围不合法")
        dry_run = body.get("dryRun", True) is not False
        out: dict[str, Any] = {"ok": True, "dryRun": dry_run, "start": start, "end": end}
        if target in {"board", "all"}:
            out["board"] = await asyncio.to_thread(cas.purge, start, end, dry_run)
        if target in {"gmgn", "all"}:
            if not dry_run:
                token_index.meter.forget(start, end)
            out["gmgn"] = await asyncio.to_thread(gmgn_db.purge, start, end, dry_run)
        if not dry_run:
            logger.info("手动删除数据 target={} {}~{}", target, start, end)
            if target in {"board", "all"}:
                hub.notify_board()
        return out

    @app.post("/api/data/vacuum")
    async def data_vacuum(request: Request) -> dict[str, Any]:
        await _json_body(request)
        board = await asyncio.to_thread(cas.vacuum)
        gmgn = await asyncio.to_thread(gmgn_db.vacuum)
        return {"ok": True, "boardBytes": board, "gmgnBytes": gmgn}

    @app.get("/data")
    def data_page() -> FileResponse:
        return FileResponse(root / "web" / "data.html", headers={"Cache-Control": "no-store"})

    @app.get("/gmgn")
    def gmgn_page() -> FileResponse:
        return FileResponse(
            root / "web" / "gmgn.html",
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/api/ca-board/user/{user_id}/refresh")
    async def ca_board_refresh(user_id: str, request: Request) -> dict[str, Any]:
        """立即刷新某个用户的喊单：scope=pending 只刷待定，scope=range 刷 range 内全部。"""
        ident = str(user_id or "").strip()
        if not _USER_ID.match(ident):
            raise HTTPException(status_code=400, detail="用户名不合法")
        body = await _json_body(request)
        scope = str(body.get("scope") or "pending")
        window = str(body.get("range") or "7d")
        if scope not in {"pending", "range"} or window not in USER_RANGES:
            raise HTTPException(status_code=400, detail="scope / range 不合法")
        if not store.get().get("ca_board", True):
            raise HTTPException(status_code=400, detail="排行榜已关闭")
        if not token_index.has_key():
            raise HTTPException(status_code=400, detail="还没有配置 GMGN Key")
        if refresh_job.running:
            raise HTTPException(status_code=409, detail="上一次刷新还没跑完")
        targets = await asyncio.to_thread(cas.refresh_targets, ident, scope, window)
        capped = len(targets) > REFRESH_MAX
        state = refresh_job.start(
            ident, scope, window, targets[:REFRESH_MAX], cas, token_index, hub
        )
        return {"ok": True, "capped": capped, "matched": len(targets), **state}

    @app.get("/api/ca-board/refresh")
    def ca_board_refresh_state() -> dict[str, Any]:
        return {"ok": True, **refresh_job.state}

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
