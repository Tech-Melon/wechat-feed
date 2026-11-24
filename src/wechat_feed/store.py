"""把信息流卡片落到本机 SQLite，保留近半年（180 天）。"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .msgstats import MessageStats

# 微信和开放接口的消息都保留 180 天；统计（msg_stats）同步保留。
KEEP_SECONDS = 180 * 24 * 3600


class CardStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._dirty = 0
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cards (
                id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                create_time INTEGER NOT NULL,
                received_at INTEGER NOT NULL,
                payload TEXT NOT NULL
            )
            """
        )
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_cards_time ON cards(create_time)")
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_cards_conv_time ON cards(conversation_id, create_time)"
        )
        # 翻页游标按 (create_time, received_at[, id]) 行值比较，配复合索引才能免排序、免 OR 拆分。
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_cards_time_recv ON cards(create_time, received_at)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_cards_conv_cursor "
            "ON cards(conversation_id, create_time, received_at, id)"
        )
        self._migrate_sender()
        self._conn.commit()
        self.stats = MessageStats(self._conn, self._lock)
        self.stats.ensure_built()

    def _migrate_sender(self) -> None:
        """0.9.40：发言人单独成列并建索引。保留半年后，按人找上下文不能再靠 json_extract 扫全群。"""
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(cards)")}
        if "sender" not in columns:
            # 加列和回填放进同一个事务：中途被杀就整体回滚，下次启动重做，不会留下一列空值。
            self._conn.commit()
            self._conn.execute("BEGIN")
            try:
                self._conn.execute("ALTER TABLE cards ADD COLUMN sender TEXT NOT NULL DEFAULT ''")
                self._conn.execute(
                    "UPDATE cards SET sender = "
                    "COALESCE(json_extract(payload, '$.senderUsername'), '')"
                )
                self._conn.execute("COMMIT")
            except sqlite3.Error:
                self._conn.execute("ROLLBACK")
                raise
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_cards_conv_sender_cursor "
            "ON cards(conversation_id, sender, create_time, received_at, id)"
        )

    def cutoff(self) -> int:
        return int(time.time()) - KEEP_SECONDS

    def token_cards(self) -> list[dict[str, Any]]:
        # 先用子串粗筛掉 tokenCards 为空的卡片，半年数据量下不必逐条解析 JSON。
        rows = self._conn.execute(
            "SELECT payload FROM cards WHERE instr(payload, '\"tokenCards\": []') = 0 "
            "ORDER BY create_time ASC, received_at ASC, id ASC"
        ).fetchall()
        out: list[dict[str, Any]] = []
        for (raw,) in rows:
            try:
                card = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(card, dict) and card.get("tokenCards"):
                out.append(card)
        return out

    def save(self, card: dict[str, Any], *, replace: bool = True) -> bool:
        """写入一张卡片。replace=False 时返回这次是否真的新插入（库里已有则 False）。"""
        card_id = str(card.get("id") or "")
        if not card_id:
            return False
        conversation_id = str(card.get("conversationId") or "")
        sender = str(card.get("senderUsername") or "")
        create_time = int(card.get("createTime") or 0)
        received_at = int(card.get("receivedAt") or 0)
        payload = json.dumps(card, ensure_ascii=False)
        conflict = (
            "ON CONFLICT(id) DO UPDATE SET "
            "payload=excluded.payload, received_at=excluded.received_at"
            if replace
            else "ON CONFLICT(id) DO NOTHING"
        )
        with self._lock:
            cursor = self._conn.execute(
                f"""
                INSERT INTO cards(id, conversation_id, create_time, received_at, sender, payload)
                VALUES (?, ?, ?, ?, ?, ?)
                {conflict}
                """,
                (card_id, conversation_id, create_time, received_at, sender, payload),
            )
            # 新卡片都走 replace=False（Hub.publish），这时 rowcount 能区分新插入和重复。
            inserted = not replace and cursor.rowcount == 1
            if inserted:
                self.stats.add_locked(card, sender)
            self._dirty += 1
            if replace or self._dirty >= 40:
                self._conn.commit()
                self._dirty = 0
        return inserted

    def exists(self, card_id: str) -> bool:
        if not card_id:
            return False
        with self._lock:
            row = self._conn.execute("SELECT 1 FROM cards WHERE id = ?", (card_id,)).fetchone()
        return row is not None

    def flush(self) -> None:
        with self._lock:
            if self._dirty:
                self._conn.commit()
                self._dirty = 0

    def update_fields(self, card_id: str, fields: dict[str, Any]) -> dict[str, Any] | None:
        if not card_id:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM cards WHERE id = ?",
                (card_id,),
            ).fetchone()
            if not row:
                return None
            try:
                card = json.loads(row[0])
            except json.JSONDecodeError:
                return None
            if not isinstance(card, dict):
                return None
            card.update(fields)
            self._conn.execute(
                "UPDATE cards SET payload = ? WHERE id = ?",
                (json.dumps(card, ensure_ascii=False), card_id),
            )
            self._conn.commit()
            self._dirty = 0
            return card

    def page(
        self,
        *,
        limit: int = 30,
        before: int | None = None,
        before_received: int | None = None,
        conversation_id: str | None = None,
        blocked: list[str] | None = None,
        allowed: list[str] | None = None,
        ca_only: bool = False,
        senders_by_group: dict[str, list[str]] | None = None,
        blocked_senders: list[str] | None = None,
        open_watched: list[str] | None = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        cutoff = self.cutoff()
        size = max(1, min(int(limit), 80))
        collected: list[dict[str, Any]] = []
        has_more = False
        cursor = before
        cursor_received = before_received
        guard = 0
        blocked_set = {str(item) for item in (blocked_senders or []) if item}
        scanning = bool(ca_only or senders_by_group or blocked_set)
        while len(collected) < size and guard < (24 if scanning else 8):
            guard += 1
            batch, more = self._fetch_newest(
                limit=120 if scanning else size - len(collected) + 1,
                before=cursor,
                before_received=cursor_received,
                conversation_id=conversation_id,
                blocked=blocked,
                allowed=allowed,
                cutoff=cutoff,
                open_watched=open_watched,
            )
            if not batch:
                has_more = False
                break
            last = batch[-1]
            cursor = int(last.get("createTime") or 0) or None
            cursor_received = int(last.get("receivedAt") or 0) or None
            for index, item in enumerate(batch):
                if ca_only and not (item.get("addresses") or item.get("cas")):
                    continue
                if blocked_set and str(item.get("senderUsername") or "") in blocked_set:
                    continue
                if not self._sender_allowed(item, senders_by_group):
                    continue
                collected.append(item)
                if len(collected) >= size:
                    has_more = index + 1 < len(batch) or more
                    return collected[:size], has_more
            has_more = more
            if not more:
                break
        return collected[:size], has_more

    @staticmethod
    def _sender_allowed(
        item: dict[str, Any],
        senders_by_group: dict[str, list[str]] | None,
    ) -> bool:
        if not senders_by_group:
            return True
        conversation_id = str(item.get("conversationId") or "")
        if conversation_id not in senders_by_group:
            return True
        sender = str(item.get("senderUsername") or "")
        return sender in set(senders_by_group[conversation_id])

    def _fetch_newest(
        self,
        *,
        limit: int,
        before: int | None,
        before_received: int | None,
        conversation_id: str | None,
        blocked: list[str] | None,
        allowed: list[str] | None,
        cutoff: int,
        open_watched: list[str] | None = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        blocked = blocked or []
        allowed = [item for item in (allowed or []) if item]
        want = max(1, min(int(limit), 120))
        sql = "SELECT payload FROM cards WHERE create_time >= ?"
        args: list[Any] = [cutoff]
        if before:
            if before_received:
                # 行值比较能直接走 idx_cards_time_recv；OR 写法会被拆成 MULTI-INDEX OR + 临时排序。
                sql += " AND (create_time, received_at) < (?, ?)"
                args.extend([int(before), int(before_received)])
            else:
                sql += " AND create_time < ?"
                args.append(int(before))
        if conversation_id:
            sql += " AND conversation_id = ?"
            args.append(conversation_id)
        elif allowed:
            placeholders = ",".join("?" for _ in allowed)
            sql += f" AND conversation_id IN ({placeholders})"
            args.extend(allowed)
        elif open_watched is not None:
            # 没勾具体群时微信群全显示；开放接口的群（ID 带冒号）只显示已加入监控的。
            watched = [item for item in open_watched if item]
            if watched:
                placeholders = ",".join("?" for _ in watched)
                sql += (
                    f" AND (instr(conversation_id, ':') = 0 OR conversation_id IN ({placeholders}))"
                )
                args.extend(watched)
            else:
                sql += " AND instr(conversation_id, ':') = 0"
        if blocked:
            placeholders = ",".join("?" for _ in blocked)
            sql += f" AND conversation_id NOT IN ({placeholders})"
            args.extend(blocked)
        sql += " ORDER BY create_time DESC, received_at DESC LIMIT ?"
        args.append(want + 1)
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        cards: list[dict[str, Any]] = []
        for (payload,) in rows[:want]:
            try:
                item = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                cards.append(item)
        return cards, len(rows) > want

    def recent(
        self,
        *,
        limit: int = 200,
        conversation_id: str | None = None,
        blocked: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        cards, _has_more = self.page(
            limit=limit,
            conversation_id=conversation_id,
            blocked=blocked,
        )
        cards.reverse()
        return cards

    def clear(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM cards")
            self._conn.execute("DELETE FROM msg_stats")
            self._conn.execute("DELETE FROM stat_names")
            self._conn.commit()

    def prune(self) -> None:
        cutoff = self.cutoff()
        with self._lock:
            self._conn.execute("DELETE FROM cards WHERE create_time < ?", (cutoff,))
            # 统计按整点桶存，桶整段过期才删，和消息保留期最多差 1 小时。
            self._conn.execute("DELETE FROM msg_stats WHERE bucket + 3600 <= ?", (cutoff,))
            self.stats.prune_names_locked()
            self._conn.commit()

    def purge(self, start: int, end: int, dry_run: bool = True) -> dict[str, int]:
        """手动删除发送时间在 [start, end) 的群消息，并按剩下的消息重算这段统计。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(instr(conversation_id, ':') > 0), 0) "
                "FROM cards WHERE create_time >= ? AND create_time < ?",
                (start, end),
            ).fetchone()
            if not dry_run:
                self._conn.execute(
                    "DELETE FROM cards WHERE create_time >= ? AND create_time < ?", (start, end)
                )
                self.stats.rebuild_locked(start, end)
                self.stats.prune_names_locked()
                self._conn.commit()
                self._dirty = 0
        total = int(row[0] or 0)
        opened = int(row[1] or 0)
        return {"messages": total, "openMessages": opened, "wechatMessages": total - opened}

    def data_summary(self) -> dict[str, Any]:
        with self._lock:
            span = self._conn.execute(
                "SELECT MIN(create_time), MAX(create_time), COUNT(*) FROM cards"
            ).fetchone()
            months = self._conn.execute(
                """
                SELECT strftime('%Y-%m', create_time, 'unixepoch', 'localtime') AS month,
                       COUNT(*), COALESCE(SUM(instr(conversation_id, ':') > 0), 0),
                       COUNT(DISTINCT conversation_id)
                FROM cards GROUP BY month ORDER BY month DESC
                """
            ).fetchall()
            pages = self._conn.execute("PRAGMA page_count").fetchone()[0]
            page_size = self._conn.execute("PRAGMA page_size").fetchone()[0]
        return {
            "oldest": span[0],
            "newest": span[1],
            "messages": int(span[2] or 0),
            "keepDays": KEEP_SECONDS // 86400,
            "bytes": int(pages or 0) * int(page_size or 0),
            "months": [
                {
                    "month": row[0],
                    "messages": int(row[1] or 0),
                    "openMessages": int(row[2] or 0),
                    "wechatMessages": int(row[1] or 0) - int(row[2] or 0),
                    "groups": int(row[3] or 0),
                }
                for row in months
            ],
        }

    def vacuum(self) -> int:
        with self._lock:
            self._conn.commit()
            self._dirty = 0
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._conn.execute("VACUUM")
            pages = self._conn.execute("PRAGMA page_count").fetchone()[0]
            page_size = self._conn.execute("PRAGMA page_size").fetchone()[0]
        return int(pages or 0) * int(page_size or 0)

    def images_without_media(self) -> list[str]:
        """直读模式早期存下的图片卡片没有图片地址。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM cards WHERE create_time >= ? "
                "AND json_extract(payload, '$.renderType') = 'image' "
                "AND COALESCE(json_extract(payload, '$.mediaUrl'), '') = ''",
                (self.cutoff(),),
            ).fetchall()
        return [str(row[0]) for row in rows]

    def emoji_without_media(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM cards WHERE create_time >= ? "
                "AND json_extract(payload, '$.renderType') = 'emoji' "
                "AND COALESCE(json_extract(payload, '$.mediaUrl'), '') = ''",
                (self.cutoff(),),
            ).fetchall()
        return [str(row[0]) for row in rows]

    def latest_time(self, conversation_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(create_time) FROM cards WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
        return int(row[0] or 0) if row else 0

    def count(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM cards WHERE create_time >= ?",
                (self.cutoff(),),
            ).fetchone()
        return int(row[0] if row else 0)

    @staticmethod
    def has_ca(card: dict[str, Any]) -> bool:
        return bool(card.get("cas") or card.get("addresses"))

    @staticmethod
    def slim_context(card: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": str(card.get("id") or ""),
            "text": str(card.get("text") or "")[:280],
            "createTime": int(card.get("createTime") or 0),
            "quoteText": str(card.get("quoteText") or "")[:160],
        }

    def _parse_payload(self, raw: str) -> dict[str, Any] | None:
        try:
            item = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return item if isinstance(item, dict) else None

    def _fetch_side(
        self,
        *,
        conversation_id: str,
        create_time: int,
        received_at: int,
        card_id: str,
        newer: bool,
        batch: int = 40,
        sender: str | None = None,
    ) -> list[dict[str, Any]]:
        cutoff = self.cutoff()
        sender_sql = ""
        extra: list[Any] = []
        if sender:
            sender_sql = "AND sender = ? "
            extra = [sender]
        # 行值游标配 idx_cards_conv_cursor，按索引顺序取，不再做临时排序。
        if newer:
            sql = (
                "SELECT payload FROM cards WHERE conversation_id = ? AND create_time >= ? "
                f"{sender_sql}"
                "AND (create_time, received_at, id) > (?, ?, ?) "
                "ORDER BY create_time ASC, received_at ASC, id ASC LIMIT ?"
            )
        else:
            sql = (
                "SELECT payload FROM cards WHERE conversation_id = ? AND create_time >= ? "
                f"{sender_sql}"
                "AND (create_time, received_at, id) < (?, ?, ?) "
                "ORDER BY create_time DESC, received_at DESC, id DESC LIMIT ?"
            )
        args: list[Any] = [
            conversation_id,
            cutoff,
            *extra,
            int(create_time),
            int(received_at),
            str(card_id),
            max(1, min(int(batch), 80)),
        ]
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        cards: list[dict[str, Any]] = []
        for (payload,) in rows:
            item = self._parse_payload(payload)
            if item is not None:
                cards.append(item)
        return cards

    def around_sender(
        self,
        card: dict[str, Any],
        *,
        limit: int = 3,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        conversation_id = str(card.get("conversationId") or "")
        sender = str(card.get("senderUsername") or "")
        card_id = str(card.get("id") or "")
        if not conversation_id or not sender:
            return [], []
        create_time = int(card.get("createTime") or 0)
        received_at = int(card.get("receivedAt") or 0)
        size = max(1, min(int(limit), 6))
        older = self._fetch_side(
            conversation_id=conversation_id,
            create_time=create_time,
            received_at=received_at,
            card_id=card_id,
            newer=False,
            batch=size,
            sender=sender,
        )
        newer = self._fetch_side(
            conversation_id=conversation_id,
            create_time=create_time,
            received_at=received_at,
            card_id=card_id,
            newer=True,
            batch=size,
            sender=sender,
        )
        before = [
            self.slim_context(item) for item in older if str(item.get("id") or "") != card_id
        ][:size]
        before.reverse()
        after = [self.slim_context(item) for item in newer if str(item.get("id") or "") != card_id][
            :size
        ]
        return before, after

    def earlier_ca_cards(self, card: dict[str, Any], *, limit: int = 8) -> list[dict[str, Any]]:
        conversation_id = str(card.get("conversationId") or "")
        sender = str(card.get("senderUsername") or "")
        if not conversation_id or not sender:
            return []
        create_time = int(card.get("createTime") or 0)
        received_at = int(card.get("receivedAt") or 0)
        card_id = str(card.get("id") or "")
        want = max(1, min(int(limit), 12))
        found: list[dict[str, Any]] = []
        for _ in range(8):
            older = self._fetch_side(
                conversation_id=conversation_id,
                create_time=create_time,
                received_at=received_at,
                card_id=card_id,
                newer=False,
                batch=80,
                sender=sender,
            )
            if not older:
                break
            for item in older:
                item_id = str(item.get("id") or "")
                if item_id == card_id or not self.has_ca(item):
                    continue
                found.append(item)
                if len(found) >= want:
                    return found
            last = older[-1]
            create_time = int(last.get("createTime") or 0)
            received_at = int(last.get("receivedAt") or 0)
            card_id = str(last.get("id") or "")
        return found

    def fill_context(self, card: dict[str, Any], *, limit: int = 3) -> dict[str, Any]:
        if not self.has_ca(card):
            return card
        before, after = self.around_sender(card, limit=limit)
        card["contextBefore"] = before
        card["contextAfter"] = after
        return card
