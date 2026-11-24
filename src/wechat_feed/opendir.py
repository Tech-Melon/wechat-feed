"""开放接口推进来的群、用户、头像和图片：目录落在 data/open.db，图片缓存在接收端。

推送方只给图片地址，本服务在页面第一次要图时去下载并落盘，之后都读本机缓存。
浏览器永远只访问 127.0.0.1，不会直连对方平台的 CDN（地址里可能带 bot token）。
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

from loguru import logger

from . import http as feed_http
from .wxemoji import pick_mime

AVATAR_MAX = 2 * 1024 * 1024
IMAGE_MAX = 8 * 1024 * 1024
# 头像地址变了（换头像或签名地址过期）最多每小时重新下载一次；地址不变时缓存 3 天。
AVATAR_REFETCH_GAP = 3600
AVATAR_TTL = 3 * 24 * 3600
# 下载失败后记一个标记，10 分钟内不再重试，避免每次刷新页面都去打对方服务器。
MISS_TTL = 600
KEEP_SECONDS = 30 * 24 * 3600
_REDIRECTS = {301, 302, 303, 307, 308}
_UA = "Mozilla/5.0 (compatible; wechat-feed open-api)"


class FetchDenied(Exception):
    """图片地址不允许访问（协议不对、指向本机回环或链路本地等）。"""


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def image_ref(url: str) -> str:
    return _sha(url)[:32]


def valid_image_ref(ref: str) -> bool:
    text = str(ref or "")
    return len(text) == 32 and all(char in "0123456789abcdef" for char in text)


def _blocked_ip(raw: str) -> bool:
    try:
        ip = ipaddress.ip_address(raw.split("%", 1)[0])
    except ValueError:
        return True
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    # 允许局域网私有地址（推送端可能自己起一个图片服务），挡住本机回环和云厂商元数据这类地址。
    return bool(
        ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved
    )


async def _check_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise FetchDenied("只接受 http / https 图片地址")
    if parsed.username or parsed.password:
        raise FetchDenied("图片地址不能带账号密码")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(parsed.hostname, port)
    except OSError as exc:
        raise FetchDenied("图片域名解析失败") from exc
    if not infos or any(_blocked_ip(str(info[4][0])) for info in infos):
        raise FetchDenied("图片地址指向本机或保留地址")


async def fetch_image(url: str, limit: int) -> tuple[bytes, str]:
    """下载一张图：每一跳都检查目标地址，边下边数字节，超过上限立刻断开。"""
    current = str(url or "").strip()
    for _ in range(4):
        await _check_url(current)
        async with feed_http.client().stream(
            "GET", current, timeout=10.0, headers={"User-Agent": _UA}
        ) as response:
            if response.status_code in _REDIRECTS:
                location = str(response.headers.get("location") or "").strip()
                if not location:
                    raise FetchDenied("跳转没有目标地址")
                current = urljoin(current, location)
                continue
            if response.status_code != 200:
                raise FetchDenied(f"HTTP {response.status_code}")
            try:
                declared = int(response.headers.get("content-length") or 0)
            except ValueError:
                declared = 0
            if declared > limit:
                raise FetchDenied("图片太大")
            buffer = bytearray()
            async for chunk in response.aiter_bytes():
                buffer.extend(chunk)
                if len(buffer) > limit:
                    raise FetchDenied("图片太大")
        blob = bytes(buffer)
        mime = pick_mime(blob)
        if not mime:
            raise FetchDenied("不是 png / jpg / gif / webp 图片")
        return blob, mime
    raise FetchDenied("跳转次数过多")


class OpenDirectory:
    def __init__(self, path: Path, media_dir: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.media_dir = media_dir
        self._lock = threading.Lock()
        self._inflight: dict[str, asyncio.Future[tuple[bytes, str] | None]] = {}
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS chats (
                id TEXT PRIMARY KEY,
                platform TEXT NOT NULL,
                name TEXT NOT NULL DEFAULT '',
                avatar TEXT NOT NULL DEFAULT '',
                first_seen INTEGER NOT NULL,
                last_seen INTEGER NOT NULL,
                messages INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                platform TEXT NOT NULL,
                name TEXT NOT NULL DEFAULT '',
                avatar TEXT NOT NULL DEFAULT '',
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS members (
                chat_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                last_seen INTEGER NOT NULL,
                PRIMARY KEY (chat_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS images (
                ref TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            """
        )
        self._conn.commit()

    # ---- 目录 ----

    def note(self, rows: list[dict[str, Any]]) -> None:
        """一次请求里出现的群、发言人、图片，一个事务写完。名字和头像为空时保留旧值。"""
        if not rows:
            return
        now = int(time.time())
        with self._lock:
            for row in rows:
                chat = row["chat"]
                self._conn.execute(
                    """
                    INSERT INTO chats(id, platform, name, avatar, first_seen, last_seen, messages)
                    VALUES (?, ?, ?, ?, ?, ?, 1)
                    ON CONFLICT(id) DO UPDATE SET
                        name = CASE WHEN excluded.name != '' THEN excluded.name ELSE name END,
                        avatar = CASE WHEN excluded.avatar != ''
                            THEN excluded.avatar ELSE avatar END,
                        last_seen = MAX(last_seen, excluded.last_seen),
                        messages = messages + 1
                    """,
                    (chat["id"], row["platform"], chat["name"], chat["avatar"], now, now),
                )
                sender = row["sender"]
                self._conn.execute(
                    """
                    INSERT INTO users(id, platform, name, avatar, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        name = CASE WHEN excluded.name != '' THEN excluded.name ELSE name END,
                        avatar = CASE WHEN excluded.avatar != ''
                            THEN excluded.avatar ELSE avatar END,
                        updated_at = excluded.updated_at
                    """,
                    (sender["id"], row["platform"], sender["name"], sender["avatar"], now),
                )
                self._conn.execute(
                    """
                    INSERT INTO members(chat_id, user_id, last_seen) VALUES (?, ?, ?)
                    ON CONFLICT(chat_id, user_id) DO UPDATE SET last_seen = excluded.last_seen
                    """,
                    (chat["id"], sender["id"], now),
                )
                for ref, url in row.get("images") or []:
                    self._conn.execute(
                        "INSERT OR IGNORE INTO images(ref, url, created_at) VALUES (?, ?, ?)",
                        (ref, url, now),
                    )
            self._conn.commit()

    def chats(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, platform, name, first_seen, last_seen, messages FROM chats "
                "ORDER BY last_seen DESC LIMIT 500"
            ).fetchall()
        return [
            {
                "id": row[0],
                "platform": row[1],
                "name": row[2],
                "firstSeen": row[3],
                "lastSeen": row[4],
                "messages": row[5],
            }
            for row in rows
        ]

    def chat_name(self, chat_id: str) -> str:
        with self._lock:
            row = self._conn.execute("SELECT name FROM chats WHERE id = ?", (chat_id,)).fetchone()
        return str(row[0]) if row else ""

    def members(self, chat_id: str) -> list[dict[str, str]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT u.id, u.name FROM members m JOIN users u ON u.id = m.user_id "
                "WHERE m.chat_id = ? ORDER BY m.last_seen DESC LIMIT 1000",
                (chat_id,),
            ).fetchall()
        out = [
            {"id": row[0], "name": row[1] or row[0], "roomNick": "", "remark": "", "nick": row[1]}
            for row in rows
        ]
        out.sort(key=lambda item: item["name"].casefold())
        return out

    def forget_chat(self, chat_id: str) -> bool:
        with self._lock:
            deleted = self._conn.execute("DELETE FROM chats WHERE id = ?", (chat_id,)).rowcount
            self._conn.execute("DELETE FROM members WHERE chat_id = ?", (chat_id,))
            self._conn.commit()
        return bool(deleted)

    def avatar_source(self, ident: str) -> str:
        with self._lock:
            row = self._conn.execute(
                # 卡片头像都是发言人；群号和人号可能是同一个数（QQ、TG 私聊），人优先。
                "SELECT avatar FROM users WHERE id = ? AND avatar != '' UNION ALL "
                "SELECT avatar FROM chats WHERE id = ? LIMIT 1",
                (ident, ident),
            ).fetchone()
        return str(row[0]) if row else ""

    def image_source(self, ref: str) -> str:
        with self._lock:
            row = self._conn.execute("SELECT url FROM images WHERE ref = ?", (ref,)).fetchone()
        return str(row[0]) if row else ""

    def prune(self) -> None:
        cutoff = int(time.time()) - KEEP_SECONDS
        with self._lock:
            self._conn.execute("DELETE FROM images WHERE created_at < ?", (cutoff,))
            self._conn.commit()
        folder = self.media_dir / "images"
        if not folder.is_dir():
            return
        for path in folder.glob("*.bin"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
            except OSError:
                continue

    # ---- 图片缓存 ----

    def _paths(self, kind: str, key: str) -> tuple[Path, Path, Path]:
        folder = self.media_dir / kind
        folder.mkdir(parents=True, exist_ok=True)
        name = _sha(key)[:40]
        return folder / f"{name}.bin", folder / f"{name}.src", folder / f"{name}.miss"

    @staticmethod
    def _age(path: Path) -> float | None:
        try:
            return time.time() - path.stat().st_mtime
        except OSError:
            return None

    async def _download(self, key: str, url: str, limit: int) -> tuple[bytes, str] | None:
        # 同一张图同时被多个标签页请求时只下载一次。
        pending = self._inflight.get(key)
        if pending is not None:
            return await pending
        future: asyncio.Future[tuple[bytes, str] | None] = (
            asyncio.get_running_loop().create_future()
        )
        self._inflight[key] = future
        result: tuple[bytes, str] | None = None
        try:
            result = await fetch_image(url, limit)
        except FetchDenied as exc:
            # 只记原因，不记地址：Telegram 这类平台的文件地址里带 bot token。
            logger.info("开放接口图片不可用: {}", exc)
        except Exception as exc:
            logger.info("开放接口图片下载失败: {}", type(exc).__name__)
        except BaseException:
            future.cancel()
            raise
        finally:
            self._inflight.pop(key, None)
        future.set_result(result)
        return result

    async def avatar(self, ident: str) -> tuple[bytes, str] | None:
        url = await asyncio.to_thread(self.avatar_source, ident)
        blob_path, src_path, miss_path = self._paths("avatars", ident)
        cached: tuple[bytes, str] | None = None
        if blob_path.is_file():
            blob = blob_path.read_bytes()
            mime = pick_mime(blob)
            if mime:
                cached = (blob, mime)
        if not url:
            return cached
        url_hash = _sha(url)
        same = src_path.is_file() and src_path.read_text(encoding="utf-8").strip() == url_hash
        age = self._age(blob_path)
        if cached and age is not None:
            if (same and age < AVATAR_TTL) or (not same and age < AVATAR_REFETCH_GAP):
                return cached
        miss_age = self._age(miss_path)
        if miss_age is not None and miss_age < MISS_TTL:
            return cached
        fresh = await self._download("a:" + ident, url, AVATAR_MAX)
        if fresh is None:
            miss_path.touch()
            return cached
        blob_path.write_bytes(fresh[0])
        src_path.write_text(url_hash, encoding="utf-8")
        miss_path.unlink(missing_ok=True)
        return fresh

    async def image(self, ref: str) -> tuple[bytes, str] | None:
        blob_path, _src, miss_path = self._paths("images", ref)
        if blob_path.is_file():
            blob = blob_path.read_bytes()
            mime = pick_mime(blob)
            if mime:
                return blob, mime
        miss_age = self._age(miss_path)
        if miss_age is not None and miss_age < MISS_TTL:
            return None
        url = await asyncio.to_thread(self.image_source, ref)
        if not url:
            return None
        fresh = await self._download("i:" + ref, url, IMAGE_MAX)
        if fresh is None:
            miss_path.touch()
            return None
        blob_path.write_bytes(fresh[0])
        miss_path.unlink(missing_ok=True)
        return fresh

    def close(self) -> None:
        with self._lock:
            self._conn.close()
