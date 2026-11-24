"""群消息统计：按小时汇总到 feed.db 的 msg_stats，查半年统计只读这张小表。

- 一行 = 某个小时 × 某个群 × 某个发言人：消息数、含合约的消息数。
- 新消息入库时在同一个事务里加一；删消息（过期、手动删除、清空）时同步删或按原始消息重算。
- 样例消息（demo）不计入。
"""

from __future__ import annotations

import sqlite3
import threading
import time
from typing import Any

STATS_VERSION = "2"
HOUR = 3600
TOP_LIMIT = 50
RANGES = {"today": 0, "7d": 7, "30d": 30, "180d": 180}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS msg_stats (
    bucket INTEGER NOT NULL,
    conversation_id TEXT NOT NULL,
    sender TEXT NOT NULL,
    messages INTEGER NOT NULL DEFAULT 0,
    ca INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (bucket, conversation_id, sender)
);
CREATE INDEX IF NOT EXISTS idx_msg_stats_conv ON msg_stats(conversation_id, bucket);
CREATE INDEX IF NOT EXISTS idx_msg_stats_sender ON msg_stats(sender, bucket);
CREATE TABLE IF NOT EXISTS stat_names (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# 从 cards 原始消息重算统计用的聚合（样例消息不算）。
_AGGREGATE = """
SELECT (create_time / 3600) * 3600 AS bucket, conversation_id, sender,
       COUNT(*),
       SUM(CASE WHEN json_array_length(COALESCE(json_extract(payload, '$.addresses'), '[]')) > 0
                  OR json_array_length(COALESCE(json_extract(payload, '$.cas'), '[]')) > 0
                THEN 1 ELSE 0 END)
FROM cards
WHERE create_time >= ? AND create_time < ?
  AND COALESCE(json_extract(payload, '$.demo'), 0) = 0
GROUP BY bucket, conversation_id, sender
"""


def bucket_of(create_time: int) -> int:
    return int(create_time) // HOUR * HOUR


def _source_sql(source: str) -> tuple[str, list[Any]]:
    """来源筛选：all / wechat / 某个平台标识。开放接口的群 ID 带「平台:」前缀，微信群不带冒号。"""
    if source == "wechat":
        return " AND instr(conversation_id, ':') = 0", []
    if source and source != "all":
        return " AND substr(conversation_id, 1, ?) = ?", [len(source) + 1, f"{source}:"]
    return "", []


class MessageStats:
    def __init__(self, conn: sqlite3.Connection, lock: threading.Lock) -> None:
        self._conn = conn
        self._lock = lock
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # ---- 写入（调用方已持有锁，和插入 cards 同一个事务） ----

    def add_locked(self, card: dict[str, Any], sender: str) -> None:
        if card.get("demo"):
            return
        conversation_id = str(card.get("conversationId") or "")
        has_ca = 1 if (card.get("addresses") or card.get("cas")) else 0
        self._conn.execute(
            """
            INSERT INTO msg_stats(bucket, conversation_id, sender, messages, ca)
            VALUES (?, ?, ?, 1, ?)
            ON CONFLICT(bucket, conversation_id, sender) DO UPDATE SET
                messages = messages + 1, ca = ca + excluded.ca
            """,
            (bucket_of(int(card.get("createTime") or 0)), conversation_id, sender, has_ca),
        )
        now = int(time.time())
        group_name = str(card.get("conversationName") or "")
        if group_name and group_name != conversation_id:
            self._name_locked(conversation_id, group_name, now)
        sender_name = str(card.get("senderName") or "")
        if sender and sender_name and sender_name != sender:
            self._name_locked(sender, sender_name, now)

    def _name_locked(self, ident: str, name: str, now: int) -> None:
        self._conn.execute(
            """
            INSERT INTO stat_names(id, name, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET name = excluded.name, updated_at = excluded.updated_at
            WHERE excluded.name != stat_names.name
            """,
            (ident, name[:80], now),
        )

    def prune_names_locked(self) -> None:
        """统计里已经没有的群和人，名字一起删掉，不超出消息保留期。"""
        self._conn.execute(
            """
            DELETE FROM stat_names
            WHERE id NOT IN (SELECT conversation_id FROM msg_stats)
              AND id NOT IN (SELECT sender FROM msg_stats)
            """
        )

    def rebuild_locked(self, start: int, end: int) -> None:
        """按 cards 重算 [start, end) 覆盖到的整点桶。手动删除一段消息后调用。"""
        low = bucket_of(start)
        high = bucket_of(end - 1) + HOUR if end > start else low
        self._conn.execute("DELETE FROM msg_stats WHERE bucket >= ? AND bucket < ?", (low, high))
        self._conn.execute(
            "INSERT INTO msg_stats(bucket, conversation_id, sender, messages, ca) " + _AGGREGATE,
            (low, high),
        )

    def ensure_built(self) -> bool:
        """第一次启用统计时用已有消息补算一遍。返回是否做了补算。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key = 'stats_version'"
            ).fetchone()
            if row and row[0] == STATS_VERSION:
                return False
            self._conn.execute("DELETE FROM msg_stats")
            self._conn.execute(
                "INSERT INTO msg_stats(bucket, conversation_id, sender, messages, ca) "
                + _AGGREGATE,
                (0, 2**62),
            )
            # 名字取每个人 / 每个群最近一条「带名字」的消息，先筛掉空名字再取最新。
            self._conn.execute("DELETE FROM stat_names")
            self._conn.execute(
                """
                INSERT OR REPLACE INTO stat_names(id, name, updated_at)
                SELECT sender, name, MAX(create_time) FROM (
                    SELECT sender, create_time, json_extract(payload, '$.senderName') AS name
                    FROM cards WHERE sender != ''
                ) WHERE COALESCE(name, '') != '' AND name != sender GROUP BY sender
                """
            )
            self._conn.execute(
                """
                INSERT OR REPLACE INTO stat_names(id, name, updated_at)
                SELECT conversation_id, name, MAX(create_time) FROM (
                    SELECT conversation_id, create_time,
                           json_extract(payload, '$.conversationName') AS name
                    FROM cards
                ) WHERE COALESCE(name, '') != '' AND name != conversation_id
                GROUP BY conversation_id
                """
            )
            self._conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('stats_version', ?)",
                (STATS_VERSION,),
            )
            self._conn.commit()
        return True

    # ---- 查询 ----

    def query(
        self,
        *,
        window: str = "7d",
        source: str = "all",
        conversation_id: str = "",
        sender: str = "",
        now: float | None = None,
    ) -> dict[str, Any]:
        clock = time.time() if now is None else now
        days = RANGES.get(window, 7)
        local = time.localtime(clock)
        midnight = int(time.mktime((local.tm_year, local.tm_mon, local.tm_mday, 0, 0, 0, 0, 0, -1)))
        since = midnight if days == 0 else midnight - (days - 1) * 86400
        where = " WHERE bucket >= ?"
        args: list[Any] = [bucket_of(since)]
        part, extra = _source_sql(source)
        where += part
        args.extend(extra)
        if conversation_id:
            where += " AND conversation_id = ?"
            args.append(conversation_id)
        if sender:
            where += " AND sender = ?"
            args.append(sender)
        base = f"FROM msg_stats{where}"
        with self._lock:
            total = self._conn.execute(
                f"SELECT COALESCE(SUM(messages), 0), COALESCE(SUM(ca), 0), "
                f"COUNT(DISTINCT conversation_id), "
                f"COUNT(DISTINCT CASE WHEN sender != '' THEN sender END) {base}",
                args,
            ).fetchone()
            daily = self._conn.execute(
                "SELECT date(bucket, 'unixepoch', 'localtime') AS day, SUM(messages), SUM(ca) "
                f"{base} GROUP BY day ORDER BY day",
                args,
            ).fetchall()
            hours = self._conn.execute(
                "SELECT CAST(strftime('%H', bucket, 'unixepoch', 'localtime') AS INTEGER) AS h, "
                f"SUM(messages) {base} GROUP BY h",
                args,
            ).fetchall()
            groups = self._conn.execute(
                "SELECT s.conversation_id, SUM(s.messages) AS m, SUM(s.ca), "
                "COUNT(DISTINCT CASE WHEN s.sender != '' THEN s.sender END), MAX(s.bucket), "
                "n.name "
                f"FROM msg_stats s LEFT JOIN stat_names n ON n.id = s.conversation_id"
                f"{where} "
                "GROUP BY s.conversation_id ORDER BY m DESC LIMIT ?",
                [*args, TOP_LIMIT],
            ).fetchall()
            senders = self._conn.execute(
                "SELECT s.sender, SUM(s.messages) AS m, SUM(s.ca), "
                "COUNT(DISTINCT s.conversation_id), MAX(s.bucket), n.name "
                f"FROM msg_stats s LEFT JOIN stat_names n ON n.id = s.sender"
                f"{where} AND s.sender != '' "
                "GROUP BY s.sender ORDER BY m DESC LIMIT ?",
                [*args, TOP_LIMIT],
            ).fetchall()
            sources = self._conn.execute(
                "SELECT CASE WHEN instr(conversation_id, ':') = 0 THEN 'wechat' "
                "ELSE substr(conversation_id, 1, instr(conversation_id, ':') - 1) END AS src, "
                "SUM(messages) FROM msg_stats WHERE bucket >= ? GROUP BY src ORDER BY 2 DESC",
                (bucket_of(since),),
            ).fetchall()
        hour_counts = [0] * 24
        for hour, count in hours:
            if hour is not None and 0 <= int(hour) < 24:
                hour_counts[int(hour)] = int(count or 0)
        return {
            "range": window if window in RANGES else "7d",
            "since": since,
            "totals": {
                "messages": int(total[0]),
                "ca": int(total[1]),
                "groups": int(total[2]),
                "senders": int(total[3]),
            },
            "daily": [
                {"day": row[0], "messages": int(row[1] or 0), "ca": int(row[2] or 0)}
                for row in daily
            ],
            "hours": hour_counts,
            "groups": [
                {
                    "id": row[0],
                    "messages": int(row[1] or 0),
                    "ca": int(row[2] or 0),
                    "senders": int(row[3] or 0),
                    "lastAt": int(row[4] or 0),
                    "name": row[5] or "",
                }
                for row in groups
            ],
            "senders": [
                {
                    "id": row[0],
                    "messages": int(row[1] or 0),
                    "ca": int(row[2] or 0),
                    "groups": int(row[3] or 0),
                    "lastAt": int(row[4] or 0),
                    "name": row[5] or "",
                }
                for row in senders
            ],
            "sources": [{"source": row[0], "messages": int(row[1] or 0)} for row in sources],
        }
