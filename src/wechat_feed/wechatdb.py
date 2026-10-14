"""直接只读打开本机微信 4.x 的 SQLCipher 4 库。

32 字节 db_key 先做 PBKDF2-HMAC-SHA512（256000 轮，盐为库文件前 16 字节）再作原始密钥。
所有方法都是同步的，调用方放进线程里跑，并用同一把锁串行化。
"""

from __future__ import annotations

import hashlib
import html
import re
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

_SHARD = re.compile(r"^message_(\d+)\.db$")
_WXID = re.compile(r"^(?:wxid_[A-Za-z0-9_-]{4,40}|[A-Za-z][A-Za-z0-9_-]{5,40})$")
_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
_SKIP_TYPES = {10000, 10002}
_SKIP_APP_SUBTYPES = {62}
_TABLE_REFRESH = 10.0
_NAME_TTL = 300.0

_BASE_RENDER = {
    1: "text",
    3: "image",
    34: "voice",
    43: "video",
    47: "emoji",
    48: "location",
}
_APP_RENDER = {
    5: "link",
    6: "file",
    19: "chatHistory",
    33: "link",
    36: "link",
    51: "link",
    57: "quote",
    2000: "transfer",
}
_QUOTE_LABELS = {
    3: "[图片]",
    34: "[语音]",
    43: "[视频]",
    47: "[表情]",
    48: "[位置]",
}
_EMOJI_MD5 = re.compile(r"^[0-9a-f]{32}$")
_STICKER_SOFT_MAX = 400_000


def derive_key(raw_key: bytes, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha512", raw_key, salt, 256000, 32)


def table_name(username: str) -> str:
    return "Msg_" + hashlib.md5(username.encode("utf-8")).hexdigest()


def decode_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, bytes):
        if content[:4] == _ZSTD_MAGIC:
            import zstandard

            content = zstandard.ZstdDecompressor().decompress(content, max_output_size=1 << 22)
        return content.decode("utf-8", "replace")
    return str(content)


def strip_sender(text: str, sender: str) -> str:
    """群消息正文带 `wxid:\\n` 前缀。"""
    if sender:
        return text[len(sender) + 2 :] if text.startswith(f"{sender}:\n") else text
    head, sep, rest = text.partition(":\n")
    if sep and _WXID.match(head):
        return rest
    return text


def _xml(text: str) -> ET.Element | None:
    start = text.find("<msg")
    if start < 0:
        return None
    try:
        return ET.fromstring(text[start:])
    except ET.ParseError:
        return None


def _find_text(node: ET.Element | None, path: str) -> str:
    if node is None:
        return ""
    found = node.find(path)
    return (found.text or "").strip() if found is not None and found.text else ""


def _appmsg_url(appmsg: ET.Element | None) -> str:
    for path in ("url", "weburl"):
        url = _find_text(appmsg, path)
        if url.startswith("http://") or url.startswith("https://"):
            return url
    return ""


def _parse_emoji(text: str) -> dict[str, Any]:
    node = _xml(text)
    el = node.find("emoji") if node is not None else None
    out: dict[str, Any] = {"renderType": "emoji", "content": ""}
    if el is None:
        return out
    md5 = (el.get("md5") or "").strip().lower()
    cdn = html.unescape((el.get("cdnurl") or "").strip())
    thumb = html.unescape((el.get("thumburl") or "").strip())
    try:
        length = int(el.get("len") or 0)
    except ValueError:
        length = 0
    url = thumb if thumb and length > _STICKER_SOFT_MAX else (cdn or thumb)
    if _EMOJI_MD5.fullmatch(md5) and url.startswith("http"):
        out["wxEmoji"] = md5
        out["emojiUrl"] = url
    return out


def _quote_content(refer: ET.Element) -> str:
    try:
        kind = int(_find_text(refer, "type") or 0)
    except ValueError:
        kind = 0
    content = _find_text(refer, "content")
    if kind == 1:
        return strip_sender(content, _find_text(refer, "chatusr"))
    if kind == 49:
        node = _xml(content) if content else None
        title = _find_text(node, "appmsg/title")
        url = _appmsg_url(node.find("appmsg") if node is not None else None)
        if title and url:
            return f"{title}\n{url}"
        if url:
            return url
        return f"[链接] {title}" if title else "[链接]"
    return _QUOTE_LABELS.get(kind, "[消息]")


def parse_row(
    *,
    local_type: int,
    sender: str,
    content: str,
) -> dict[str, Any] | None:
    """把一行消息转成 WDA /api/chat/messages 的字段，交给 normalize 处理。"""

    base = local_type & 0xFFFFFFFF
    sub = local_type >> 32
    if base in _SKIP_TYPES:
        return None
    text = strip_sender(content, sender)
    if base in _BASE_RENDER:
        render = _BASE_RENDER[base]
        if render == "text":
            return {"renderType": "text", "content": text}
        if render == "location":
            node = _xml(text)
            loc = node.find("location") if node is not None else None
            label = ""
            if loc is not None:
                label = loc.get("poiname") or loc.get("label") or ""
            return {"renderType": "location", "content": "", "locationLabel": label}
        if render == "voice":
            node = _xml(text)
            voice = node.find("voicemsg") if node is not None else None
            try:
                seconds = (
                    round(int(voice.get("voicelength") or 0) / 1000) if voice is not None else 0
                )
            except ValueError:
                seconds = 0
            return {"renderType": "voice", "content": f"[语音 {seconds}秒]" if seconds else ""}
        if render == "emoji":
            return _parse_emoji(text)
        return {"renderType": render, "content": ""}
    if base == 42:
        return {"renderType": "text", "content": "[名片]"}
    if base == 49:
        node = _xml(text)
        appmsg = node.find("appmsg") if node is not None else None
        if not sub and appmsg is not None:
            try:
                sub = int(_find_text(appmsg, "type") or 0)
            except ValueError:
                sub = 0
        if sub in _SKIP_APP_SUBTYPES:
            return None
        title = _find_text(appmsg, "title")
        render = _APP_RENDER.get(sub, "link")
        if render == "quote":
            refer = appmsg.find("refermsg") if appmsg is not None else None
            row: dict[str, Any] = {"renderType": "quote", "content": title}
            if refer is not None:
                row["quoteTitle"] = _find_text(refer, "displayname")
                row["quoteContent"] = _quote_content(refer)
                row["quoteUsername"] = _find_text(refer, "chatusr")
            return row
        if render == "chatHistory":
            lines = _find_text(appmsg, "des").replace("\r\n", "\n").strip()
            return {"renderType": "chatHistory", "content": lines or title, "title": title}
        if render == "transfer":
            amount = _find_text(appmsg, "wcpayinfo/feedesc")
            return {"renderType": "transfer", "content": "", "amount": amount}
        row = {"renderType": render, "content": "", "title": title}
        url = _appmsg_url(appmsg)
        if url:
            row["linkUrl"] = url
        return row
    return None


def _varint(buf: bytes, pos: int) -> tuple[int, int]:
    value = shift = 0
    while pos < len(buf):
        byte = buf[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7
    raise ValueError("varint")


def _proto_fields(buf: bytes):
    pos = 0
    while pos < len(buf):
        key, pos = _varint(buf, pos)
        wire = key & 7
        if wire == 0:
            value, pos = _varint(buf, pos)
        elif wire == 2:
            size, pos = _varint(buf, pos)
            value = buf[pos : pos + size]
            pos += size
        elif wire == 5:
            value, pos = buf[pos : pos + 4], pos + 4
        elif wire == 1:
            value, pos = buf[pos : pos + 8], pos + 8
        else:
            return
        yield key >> 3, wire, value


def image_file_md5(packed: bytes | None) -> str:
    """图片消息的 packed_info_data：field 3 里的 field 4 是本地 .dat 文件名。"""
    if not packed:
        return ""
    try:
        for number, wire, value in _proto_fields(bytes(packed)):
            if number != 3 or wire != 2:
                continue
            for inner, inner_wire, name in _proto_fields(value):
                if inner == 4 and inner_wire == 2:
                    text = name.decode("ascii", "replace")
                    if re.fullmatch(r"[0-9a-f]{32}", text):
                        return text
    except ValueError:
        return ""
    return ""


def room_nicknames(ext_buffer: bytes | None) -> dict[str, str]:
    """chat_room.ext_buffer 里 field 1 是成员列表，成员内 1=wxid，2=群昵称。"""
    members: dict[str, str] = {}
    if not ext_buffer:
        return members
    try:
        for number, wire, value in _proto_fields(ext_buffer):
            if number != 1 or wire != 2:
                continue
            member = {n: v for n, w, v in _proto_fields(value) if w == 2}
            ident = member.get(1, b"").decode("utf-8", "replace")
            nick = member.get(2, b"").decode("utf-8", "replace")
            if ident:
                members[ident] = nick
    except ValueError:
        return members
    return members


class WeChatDB:
    def __init__(self, db_dir: Path, raw_key: bytes, self_wxid: str = "") -> None:
        self.db_dir = Path(db_dir)
        self.self_wxid = self_wxid
        self._raw_key = raw_key
        self._lock = threading.Lock()
        self._conns: dict[Path, Any] = {}
        self._tables: dict[Path, dict[str, str]] = {}
        self._tables_at: dict[Path, float] = {}
        self._senders: dict[Path, dict[int, str]] = {}
        self._cursors: dict[tuple[Path, str], int] = {}
        self._rooms: dict[str, tuple[float, dict[str, str]]] = {}
        self._contacts: dict[str, tuple[float, str, str]] = {}
        self._primed = False

    def close(self) -> None:
        with self._lock:
            for conn in self._conns.values():
                conn.close()
            self._conns.clear()

    def _open(self, path: Path):
        conn = self._conns.get(path)
        if conn is not None:
            return conn
        from sqlcipher3 import dbapi2 as sqlcipher

        with path.open("rb") as handle:
            salt = handle.read(16)
        key = derive_key(self._raw_key, salt)
        conn = sqlcipher.connect(
            f"file:{path.as_posix()}?mode=ro", uri=True, check_same_thread=False
        )
        conn.execute(f"PRAGMA key = \"x'{key.hex()}'\"")
        conn.execute("PRAGMA cipher_page_size = 4096")
        conn.execute("PRAGMA cipher_hmac_algorithm = HMAC_SHA512")
        conn.execute("PRAGMA cipher_kdf_algorithm = PBKDF2_HMAC_SHA512")
        conn.execute("PRAGMA query_only = ON")
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        self._conns[path] = conn
        return conn

    def shards(self) -> list[Path]:
        folder = self.db_dir / "message"
        found = [path for path in folder.glob("message_*.db") if _SHARD.match(path.name)]
        return sorted(found, key=lambda path: int(_SHARD.match(path.name).group(1)))

    def signature(self) -> tuple[tuple[str, int, int], ...]:
        """只看聊天分片和会话库的 mtime，公众号 / 搜索索引 / 媒体库的写入不算。"""
        marks: list[tuple[str, int, int]] = []
        for path in self.shards():
            for item in (path, path.with_name(path.name + "-wal")):
                try:
                    stat = item.stat()
                except OSError:
                    continue
                marks.append((item.name, stat.st_mtime_ns, stat.st_size))
        return tuple(marks)

    def verify(self) -> None:
        with self._lock:
            self._open(self.db_dir / "session" / "session.db")

    def _shard_tables(self, path: Path, *, force: bool = False) -> dict[str, str]:
        now = time.time()
        if not force and now - self._tables_at.get(path, 0.0) < _TABLE_REFRESH:
            return self._tables.get(path, {})
        conn = self._open(path)
        names = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        mapping: dict[str, str] = {}
        for (username,) in conn.execute("SELECT user_name FROM Name2Id"):
            if not username or not str(username).endswith("@chatroom"):
                continue
            table = table_name(str(username))
            if table in names:
                mapping[table] = str(username)
        from_tail = path not in self._tables and not self._primed
        for table in mapping:
            key = (path, table)
            if key in self._cursors:
                continue
            if from_tail:
                row = conn.execute(f"SELECT MAX(local_id) FROM [{table}]").fetchone()
            else:
                row = conn.execute(
                    f"SELECT MAX(local_id) FROM [{table}] WHERE create_time < ?",
                    (int(now) - 60,),
                ).fetchone()
            self._cursors[key] = int(row[0] or 0)
        self._tables[path] = mapping
        self._tables_at[path] = now
        return mapping

    def _sender(self, path: Path, sender_id: int) -> str:
        cache = self._senders.setdefault(path, {})
        if sender_id in cache:
            return cache[sender_id]
        row = (
            self._open(path)
            .execute("SELECT user_name FROM Name2Id WHERE rowid = ?", (sender_id,))
            .fetchone()
        )
        name = str(row[0] or "") if row else ""
        cache[sender_id] = name
        return name

    def _contact(self, username: str) -> tuple[str, str]:
        cached = self._contacts.get(username)
        if cached and time.time() - cached[0] < _NAME_TTL:
            return cached[1], cached[2]
        conn = self._open(self.db_dir / "contact" / "contact.db")
        row = None
        for table in ("contact", "stranger"):
            row = conn.execute(
                f"SELECT remark, nick_name FROM {table} WHERE username = ?", (username,)
            ).fetchone()
            if row:
                break
        remark, nick = (str(row[0] or ""), str(row[1] or "")) if row else ("", "")
        self._contacts[username] = (time.time(), remark, nick)
        return remark, nick

    def _room(self, room: str) -> dict[str, str]:
        cached = self._rooms.get(room)
        if cached and time.time() - cached[0] < _NAME_TTL:
            return cached[1]
        conn = self._open(self.db_dir / "contact" / "contact.db")
        row = conn.execute(
            "SELECT ext_buffer FROM chat_room WHERE username = ?", (room,)
        ).fetchone()
        members = room_nicknames(row[0] if row else None)
        self._rooms[room] = (time.time(), members)
        return members

    def _display_name(self, room: str, username: str) -> str:
        nick_in_room = self._room(room).get(username, "")
        if nick_in_room:
            return nick_in_room
        remark, nick = self._contact(username)
        return remark or nick or username

    def _group_name(self, room: str) -> str:
        remark, nick = self._contact(room)
        return remark or nick

    def group_name(self, room: str) -> str:
        with self._lock:
            return self._group_name(room)

    def _message(self, path: Path, table: str, room: str, row: tuple) -> dict[str, Any] | None:
        local_id, created, local_type, sender_id, content, packed = row
        sender = self._sender(path, int(sender_id or 0))
        parsed = parse_row(
            local_type=int(local_type or 0),
            sender=sender,
            content=decode_content(content),
        )
        if parsed is None:
            return None
        if parsed["renderType"] == "image":
            name = image_file_md5(packed)
            if name:
                month = time.strftime("%Y-%m", time.localtime(int(created or 0)))
                parsed["wxImage"] = f"{table[4:]}/{month}/{name}"
        return {
            "id": f"{path.stem}:{table}:{local_id}",
            "localId": int(local_id),
            "createTime": int(created or 0),
            "senderUsername": sender,
            "senderDisplayName": self._display_name(room, sender) if sender else "",
            "isSent": bool(self.self_wxid) and sender == self.self_wxid,
            **parsed,
        }

    _COLUMNS = (
        "local_id, create_time, local_type, real_sender_id, message_content, packed_info_data"
    )

    @property
    def wx_dir(self) -> Path:
        return self.db_dir.parent

    def new_messages(self, rooms: set[str] | None) -> list[tuple[str, str, dict[str, Any]]]:
        """自上次调用以来的新消息 (群, 群名, 消息)。rooms 为 None 时看全部群。"""
        out: list[tuple[str, str, dict[str, Any]]] = []
        with self._lock:
            for path in self.shards():
                tables = self._shard_tables(path)
                conn = self._open(path)
                for table, room in tables.items():
                    if rooms is not None and room not in rooms:
                        continue
                    key = (path, table)
                    while True:
                        rows = conn.execute(
                            f"SELECT {self._COLUMNS} FROM [{table}] WHERE local_id > ? "
                            "ORDER BY local_id LIMIT 200",
                            (self._cursors.get(key, 0),),
                        ).fetchall()
                        for row in rows:
                            self._cursors[key] = int(row[0])
                            message = self._message(path, table, room, row)
                            if message is not None:
                                out.append((room, self._group_name(room), message))
                        if len(rows) < 200:
                            break
            self._primed = True
        return out

    def history(self, room: str, since: int, limit: int = 1500) -> list[dict[str, Any]]:
        """create_time >= since 的消息，从新到旧。"""
        table = table_name(room)
        out: list[dict[str, Any]] = []
        with self._lock:
            for path in reversed(self.shards()):
                tables = self._shard_tables(path)
                if table not in tables:
                    continue
                rows = (
                    self._open(path)
                    .execute(
                        f"SELECT {self._COLUMNS} FROM [{table}] WHERE create_time >= ? "
                        "ORDER BY create_time DESC, local_id DESC LIMIT ?",
                        (int(since), int(limit)),
                    )
                    .fetchall()
                )
                for row in rows:
                    message = self._message(path, table, room, row)
                    if message is not None:
                        out.append(message)
                if len(out) >= limit:
                    break
        out.sort(key=lambda item: (item["createTime"], item["localId"]), reverse=True)
        return out[:limit]

    def image_ref(self, message_id: str) -> str:
        """按直读消息 id（分片:表:local_id）重新算出图片引用。"""
        stem, _, rest = str(message_id).partition(":")
        table, _, local_id = rest.partition(":")
        if not _SHARD.match(f"{stem}.db") or not re.fullmatch(r"Msg_[0-9a-f]{32}", table):
            return ""
        if not local_id.isdigit():
            return ""
        path = self.db_dir / "message" / f"{stem}.db"
        if not path.exists():
            return ""
        with self._lock:
            row = (
                self._open(path)
                .execute(
                    f"SELECT create_time, packed_info_data FROM [{table}] WHERE local_id = ?",
                    (int(local_id),),
                )
                .fetchone()
            )
        if not row:
            return ""
        name = image_file_md5(row[1])
        if not name:
            return ""
        month = time.strftime("%Y-%m", time.localtime(int(row[0] or 0)))
        return f"{table[4:]}/{month}/{name}"

    def emoji_media(self, message_id: str) -> tuple[str, str]:
        """按直读消息 id 重新取出贴纸 md5 和 CDN。"""
        stem, _, rest = str(message_id).partition(":")
        table, _, local_id = rest.partition(":")
        if not _SHARD.match(f"{stem}.db") or not re.fullmatch(r"Msg_[0-9a-f]{32}", table):
            return "", ""
        if not local_id.isdigit():
            return "", ""
        path = self.db_dir / "message" / f"{stem}.db"
        if not path.exists():
            return "", ""
        with self._lock:
            row = (
                self._open(path)
                .execute(
                    f"SELECT real_sender_id, message_content FROM [{table}] WHERE local_id = ?",
                    (int(local_id),),
                )
                .fetchone()
            )
            if not row:
                return "", ""
            sender = self._sender(path, int(row[0] or 0))
            content = decode_content(row[1])
        parsed = parse_row(local_type=47, sender=sender, content=content)
        if not parsed:
            return "", ""
        return str(parsed.get("wxEmoji") or ""), str(parsed.get("emojiUrl") or "")

    def _contacts_many(self, usernames: list[str]) -> dict[str, tuple[str, str]]:
        now = time.time()
        found: dict[str, tuple[str, str]] = {}
        missing: list[str] = []
        for name in usernames:
            cached = self._contacts.get(name)
            if cached and now - cached[0] < _NAME_TTL:
                found[name] = (cached[1], cached[2])
            else:
                missing.append(name)
        if not missing:
            return found
        conn = self._open(self.db_dir / "contact" / "contact.db")
        fetched: dict[str, tuple[str, str]] = {}
        for table in ("contact", "stranger"):
            leftover = [item for item in missing if item not in fetched]
            if not leftover:
                break
            for start in range(0, len(leftover), 200):
                chunk = leftover[start : start + 200]
                placeholders = ",".join("?" * len(chunk))
                rows = conn.execute(
                    f"SELECT username, remark, nick_name FROM {table} "
                    f"WHERE username IN ({placeholders})",
                    chunk,
                ).fetchall()
                for row in rows:
                    fetched[str(row[0])] = (str(row[1] or ""), str(row[2] or ""))
        for name in missing:
            remark, nick = fetched.get(name, ("", ""))
            self._contacts[name] = (now, remark, nick)
            found[name] = (remark, nick)
        return found

    def _group_names(self, rooms: list[str]) -> dict[str, str]:
        contacts = self._contacts_many(rooms)
        names: dict[str, str] = {}
        for room in rooms:
            remark, nick = contacts.get(room, ("", ""))
            names[room] = remark or nick
        return names

    def groups(self, limit: int = 400) -> list[dict[str, str]]:
        with self._lock:
            conn = self._open(self.db_dir / "session" / "session.db")
            rows = conn.execute(
                "SELECT username FROM SessionTable WHERE username LIKE '%@chatroom' "
                "ORDER BY sort_timestamp DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
            ids = [str(row[0]) for row in rows]
            names = self._group_names(ids)
            return [{"id": item, "name": names.get(item, "")} for item in ids]

    def members(self, room: str) -> list[dict[str, str]]:
        """现场读 chat_room.ext_buffer，联系人名走短 TTL 缓存，不落盘。"""
        with self._lock:
            self._rooms.pop(room, None)
            nicks = self._room(room)
            contacts = self._contacts_many(list(nicks))
            out: list[dict[str, str]] = []
            for ident, room_nick in nicks.items():
                remark, nick = contacts.get(ident, ("", ""))
                name = room_nick or remark or nick or ident
                out.append(
                    {
                        "id": ident,
                        "name": name,
                        "roomNick": room_nick,
                        "remark": remark,
                        "nick": nick,
                    }
                )
            out.sort(key=lambda item: item["name"].casefold())
            return out

    def avatar(self, username: str) -> bytes | None:
        path = self.db_dir / "head_image" / "head_image.db"
        if not path.exists():
            return None
        with self._lock:
            row = (
                self._open(path)
                .execute("SELECT image_buffer FROM head_image WHERE username = ?", (username,))
                .fetchone()
            )
        blob = row[0] if row else None
        return bytes(blob) if blob else None
