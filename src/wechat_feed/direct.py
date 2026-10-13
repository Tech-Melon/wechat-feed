"""直读模式：db_key 向 WDA 取一次存到本机，之后不再依赖 WDA。"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from .wechatdb import WeChatDB

_RETRY_AFTER = 30.0
_POLL_SECONDS = 0.3
_IMAGE_FIELDS = ("image_aes_key", "image_xor_key")


def direct_disabled() -> bool:
    return os.environ.get("WECHAT_FEED_DISABLE_DIRECT") == "1"


async def fetch_key_from_wda(upstream_base: str) -> dict[str, str] | None:
    base = upstream_base.rstrip("/")
    timeout = httpx.Timeout(30.0, connect=3.0)
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        status = (await client.get(f"{base}/api/chat/realtime/status")).json()
        realtime = status.get("realtime") or {}
        account = str(status.get("account") or realtime.get("account") or "")
        db_dir = str(realtime.get("db_storage_dir") or "")
        if not account or not db_dir:
            return None
        payload = (
            await client.get(
                f"{base}/api/keys", params={"account": account, "db_storage_path": db_dir}
            )
        ).json()
    keys = payload.get("keys") or {}
    key = str(keys.get("db_key") or "")
    if len(key) != 64:
        return None
    info = {"account": account, "db_storage_dir": db_dir, "db_key": key}
    for name in _IMAGE_FIELDS:
        if keys.get(name):
            info[name] = str(keys[name])
    return info


def wda_key_file() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(base) / "wechat-data-analysis-desktop" / "output" / "account_keys.json"


def read_wda_image_keys(account: str, db_dir: str) -> dict[str, str]:
    """WDA 桌面版把图片密钥存在本机，WDA 没在跑也能读。"""
    try:
        entries = json.loads(wda_key_file().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(entries, dict):
        return {}
    wanted = os.path.normcase(db_dir)
    for name, entry in entries.items():
        if not isinstance(entry, dict):
            continue
        source = os.path.normcase(str(entry.get("db_key_source_db_storage_path") or ""))
        if name == account or (wanted and source == wanted):
            found = {key: str(entry.get(key) or "") for key in _IMAGE_FIELDS}
            if all(found.values()):
                return found
    return {}


def parse_image_keys(info: dict[str, str]) -> tuple[bytes, int] | None:
    aes = str(info.get("image_aes_key") or "")
    xor = str(info.get("image_xor_key") or "")
    if len(aes) != 16 or not xor:
        return None
    try:
        return aes.encode("ascii"), int(xor, 0) & 0xFF
    except ValueError:
        return None


class DirectSource:
    def __init__(self, key_file: Path) -> None:
        self._key_file = key_file
        self._reader: WeChatDB | None = None
        self._lock = asyncio.Lock()
        self._failed_at = 0.0
        self._image_keys: tuple[bytes, int] | None = None
        self._image_checked = 0.0
        self.detail = ""

    @property
    def active(self) -> bool:
        return self._reader is not None

    def _load(self) -> dict[str, str] | None:
        try:
            info = json.loads(self._key_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(info, dict) or len(str(info.get("db_key") or "")) != 64:
            return None
        loaded = {key: str(info.get(key) or "") for key in ("account", "db_storage_dir", "db_key")}
        loaded.update({key: str(info[key]) for key in _IMAGE_FIELDS if info.get(key)})
        return loaded

    def image_keys(self) -> tuple[bytes, int] | None:
        if self._image_keys is not None or time.time() - self._image_checked < _RETRY_AFTER:
            return self._image_keys
        self._image_checked = time.time()
        stored = self._load()
        if not stored:
            return None
        keys = parse_image_keys(stored)
        if keys is None:
            found = read_wda_image_keys(stored.get("account", ""), stored.get("db_storage_dir", ""))
            keys = parse_image_keys(found)
            if keys is not None:
                self._save({**stored, **found})
                logger.info("已从 WDA 本机配置读到图片密钥")
        self._image_keys = keys
        return keys

    def _save(self, info: dict[str, str]) -> None:
        self._key_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._key_file.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self._key_file)

    async def _try_open(self, info: dict[str, str]) -> WeChatDB | None:
        db_dir = Path(info["db_storage_dir"])
        if not (db_dir / "message").is_dir():
            self.detail = f"微信库目录不存在: {db_dir}"
            return None
        reader = WeChatDB(db_dir, bytes.fromhex(info["db_key"]), info.get("account", ""))
        try:
            await asyncio.to_thread(reader.verify)
        except Exception as exc:
            reader.close()
            self.detail = f"密钥打不开微信库（{type(exc).__name__}），会重新向 WDA 取"
            return None
        return reader

    async def reader(self, upstream_base: str) -> WeChatDB | None:
        if self._reader is not None or direct_disabled():
            return self._reader
        async with self._lock:
            if self._reader is not None:
                return self._reader
            if time.time() - self._failed_at < _RETRY_AFTER:
                return None
            stored = self._load()
            reader = await self._try_open(stored) if stored else None
            if reader is None:
                try:
                    fetched = await fetch_key_from_wda(upstream_base)
                except Exception as exc:
                    fetched = None
                    if not stored:
                        self.detail = f"还没有直读密钥，WDA 也连不上（{type(exc).__name__}）"
                if fetched and fetched != stored:
                    reader = await self._try_open(fetched)
                    if reader is not None:
                        image = {k: v for k, v in (stored or {}).items() if k in _IMAGE_FIELDS}
                        self._save({**image, **fetched})
                        logger.info("已从 WDA 取到直读密钥，之后可以关掉 WDA")
            if reader is None:
                self._failed_at = time.time()
                return None
            self.detail = "直读本机微信库，不经过 WDA"
            self._reader = reader
            return reader

    def drop(self, reason: str) -> None:
        if self._reader is not None:
            self._reader.close()
        self._reader = None
        self._failed_at = time.time()
        self.detail = reason


async def direct_backfill(
    reader: WeChatDB,
    rooms: list[str],
    since_for,
    ingest,
    stop: asyncio.Event,
) -> None:
    for room in rooms:
        if stop.is_set():
            return
        messages = await asyncio.to_thread(reader.history, room, since_for(room))
        name = await asyncio.to_thread(reader.group_name, room)
        for index, message in enumerate(reversed(messages)):
            ingest(message, room, name)
            if index % 50 == 0:
                await asyncio.sleep(0)


async def direct_stream(
    reader: WeChatDB,
    rooms_for,
    ingest,
    stop: asyncio.Event,
) -> None:
    last: Any = None
    while not stop.is_set():
        signature = await asyncio.to_thread(reader.signature)
        if signature != last:
            last = signature
            rows = await asyncio.to_thread(reader.new_messages, rooms_for())
            for room, name, message in rows:
                ingest(message, room, name)
        await asyncio.sleep(_POLL_SECONDS)
