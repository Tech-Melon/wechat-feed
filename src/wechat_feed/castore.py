"""单独记下 CA 首次发现与每人喊单，保留 1 年。"""

from __future__ import annotations

import math
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

KEEP_SECONDS = 365 * 24 * 3600
WIN_X = 1.5
# 超过一万倍当脏数据：历史回灌、错市值、单位错位都会出这种数。
MAX_X = 10_000.0
HOT_AGE = 6 * 3600
WARM_AGE = 3 * 86400
REFRESH_HOT = 15 * 60
REFRESH_WARM = 2 * 3600
REFRESH_COLD = 12 * 3600


def _num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _text(value: Any) -> str:
    return str(value or "").strip()


def format_x(value: float) -> str:
    number = _num(value)
    if number <= 0:
        return ""
    if number >= 100:
        return f"{number:.0f}x"
    if number >= 10:
        return f"{number:.1f}x".replace(".0x", "x")
    text = f"{number:.2f}".rstrip("0").rstrip(".")
    return f"{text}x"


def wilson_lower(wins: int, total: int, z: float = 1.96) -> float:
    if total <= 0:
        return 0.0
    p = wins / total
    z2 = z * z
    denom = 1 + z2 / total
    centre = p + z2 / (2 * total)
    spread = z * math.sqrt((p * (1 - p) + z2 / (4 * total)) / total)
    return max(0.0, (centre - spread) / denom)


def is_sane(first_mcap: float, ath_mcap: float = 0.0, last_mcap: float = 0.0) -> bool:
    first = _num(first_mcap)
    if first <= 0:
        return False
    peak = max(_num(ath_mcap), _num(last_mcap), first)
    return peak / first <= MAX_X


def _multiple(peak: float, base: float) -> float:
    if base <= 0 or peak <= 0:
        return 0.0
    value = peak / base
    if value > MAX_X:
        return 0.0
    if value > 500:
        return 500.0
    return value


class CaStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ca_tokens (
                chain TEXT NOT NULL,
                address TEXT NOT NULL,
                symbol TEXT,
                name TEXT,
                first_mcap REAL NOT NULL DEFAULT 0,
                ath_mcap REAL NOT NULL DEFAULT 0,
                last_mcap REAL NOT NULL DEFAULT 0,
                first_at INTEGER NOT NULL DEFAULT 0,
                last_at INTEGER NOT NULL DEFAULT 0,
                discoverer_id TEXT,
                discoverer_name TEXT,
                group_id TEXT,
                group_name TEXT,
                card_id TEXT,
                PRIMARY KEY (chain, address)
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ca_calls (
                chain TEXT NOT NULL,
                address TEXT NOT NULL,
                caller_id TEXT NOT NULL,
                caller_name TEXT,
                group_id TEXT,
                group_name TEXT,
                card_id TEXT,
                mcap REAL NOT NULL DEFAULT 0,
                called_at INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (chain, address, caller_id)
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ca_group_firsts (
                chain TEXT NOT NULL,
                address TEXT NOT NULL,
                group_id TEXT NOT NULL,
                group_name TEXT,
                caller_id TEXT,
                caller_name TEXT,
                card_id TEXT,
                mcap REAL NOT NULL DEFAULT 0,
                first_at INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (chain, address, group_id)
            )
            """
        )
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_ca_calls_time ON ca_calls(called_at)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_ca_tokens_time ON ca_tokens(first_at)")
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ca_group_firsts_time ON ca_group_firsts(first_at)"
        )
        self._ensure_columns()
        self._backfill_group_firsts()
        self._conn.commit()

    def _ensure_columns(self) -> None:
        cols = {row[1] for row in self._conn.execute("PRAGMA table_info(ca_tokens)")}
        if "refreshed_at" not in cols:
            self._conn.execute(
                "ALTER TABLE ca_tokens ADD COLUMN refreshed_at INTEGER NOT NULL DEFAULT 0"
            )

    def _backfill_group_firsts(self) -> None:
        self._conn.execute(
            """
            INSERT OR IGNORE INTO ca_group_firsts (
                chain, address, group_id, group_name, caller_id, caller_name,
                card_id, mcap, first_at
            )
            SELECT chain, address, group_id, group_name, discoverer_id, discoverer_name,
                   card_id, first_mcap, first_at
            FROM ca_tokens
            WHERE IFNULL(group_id, '') != '' AND first_mcap > 0
            """
        )
        self._conn.execute(
            """
            INSERT OR IGNORE INTO ca_group_firsts (
                chain, address, group_id, group_name, caller_id, caller_name,
                card_id, mcap, first_at
            )
            SELECT c.chain, c.address, c.group_id, c.group_name, c.caller_id, c.caller_name,
                   c.card_id, c.mcap, c.called_at
            FROM ca_calls c
            INNER JOIN (
                SELECT chain, address, group_id, MIN(called_at) AS first_at
                FROM ca_calls
                WHERE IFNULL(group_id, '') != '' AND mcap > 0
                GROUP BY chain, address, group_id
            ) x ON x.chain = c.chain AND x.address = c.address
               AND x.group_id = c.group_id AND x.first_at = c.called_at
            """
        )

    def cutoff(self) -> int:
        return int(time.time()) - KEEP_SECONDS

    def prune(self) -> None:
        cut = self.cutoff()
        with self._lock:
            self._conn.execute("DELETE FROM ca_calls WHERE called_at > 0 AND called_at < ?", (cut,))
            self._conn.execute(
                "DELETE FROM ca_group_firsts WHERE first_at > 0 AND first_at < ?", (cut,)
            )
            self._conn.execute("DELETE FROM ca_tokens WHERE first_at > 0 AND first_at < ?", (cut,))
            self._conn.commit()

    def prune_bad(self) -> int:
        """丢掉没有市值、地址空、倍数离谱的历史记录。"""
        dropped = 0
        with self._lock:
            rows = self._conn.execute(
                "SELECT chain, address, first_mcap, ath_mcap, last_mcap FROM ca_tokens"
            ).fetchall()
            for row in rows:
                address = _text(row["address"]).lower()
                if not address or not is_sane(row["first_mcap"], row["ath_mcap"], row["last_mcap"]):
                    self._conn.execute(
                        "DELETE FROM ca_calls WHERE chain = ? AND address = ?",
                        (row["chain"], row["address"]),
                    )
                    self._conn.execute(
                        "DELETE FROM ca_group_firsts WHERE chain = ? AND address = ?",
                        (row["chain"], row["address"]),
                    )
                    self._conn.execute(
                        "DELETE FROM ca_tokens WHERE chain = ? AND address = ?",
                        (row["chain"], row["address"]),
                    )
                    dropped += 1
            self._conn.execute(
                "DELETE FROM ca_calls WHERE IFNULL(address, '') = '' OR IFNULL(caller_id, '') = ''"
            )
            self._conn.execute(
                """
                DELETE FROM ca_calls WHERE NOT EXISTS (
                    SELECT 1 FROM ca_tokens t
                    WHERE t.chain = ca_calls.chain AND t.address = ca_calls.address
                )
                """
            )
            self._conn.execute(
                """
                DELETE FROM ca_group_firsts WHERE NOT EXISTS (
                    SELECT 1 FROM ca_tokens t
                    WHERE t.chain = ca_group_firsts.chain AND t.address = ca_group_firsts.address
                )
                """
            )
            self._conn.commit()
        return dropped

    def remember(self, card: dict[str, Any], tokens: list[dict[str, Any]]) -> list[dict[str, Any]]:
        now = int(card.get("createTime") or time.time())
        caller_id = _text(card.get("senderUsername"))
        caller_name = _text(card.get("senderName") or card.get("displayName") or caller_id)
        group_id = _text(card.get("conversationId"))
        group_name = _text(card.get("conversationName") or card.get("displayGroup") or group_id)
        card_id = _text(card.get("id"))
        out: list[dict[str, Any]] = []
        with self._lock:
            for token in tokens:
                kind = _text(token.get("kind"))
                source = _text(token.get("source"))
                if kind == "wallet" or source == "gmgn-wallet":
                    out.append(token)
                    continue
                chain = _text(token.get("chain")).lower()
                address = _text(token.get("address")).lower()
                if not address:
                    out.append(token)
                    continue
                if not chain:
                    chain = "_"
                mcap = _num(token.get("marketCap"))
                ath = max(_num(token.get("athMarketCap")), mcap)
                if mcap <= 0 and not self._has_token(chain, address):
                    out.append(token)
                    continue
                exists = self._has_token(chain, address)
                if mcap > 0 and not is_sane(mcap, ath, mcap) and not exists:
                    out.append(token)
                    continue
                self._upsert_token(
                    chain,
                    address,
                    token,
                    mcap,
                    ath,
                    now,
                    caller_id,
                    caller_name,
                    group_id,
                    group_name,
                    card_id,
                )
                if caller_id and mcap > 0:
                    self._upsert_call(
                        chain,
                        address,
                        caller_id,
                        caller_name,
                        group_id,
                        group_name,
                        card_id,
                        mcap,
                        now,
                    )
                if group_id and mcap > 0:
                    self._upsert_group_first(
                        chain,
                        address,
                        group_id,
                        group_name,
                        caller_id,
                        caller_name,
                        card_id,
                        mcap,
                        now,
                    )
                out.append(
                    self._decorate(token, chain, address, card_id, caller_id, now, group_id)
                )
            self._conn.commit()
        return out

    def _has_token(self, chain: str, address: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM ca_tokens WHERE chain = ? AND address = ?",
            (chain, address),
        ).fetchone()
        return row is not None

    def _upsert_token(
        self,
        chain: str,
        address: str,
        token: dict[str, Any],
        mcap: float,
        ath: float,
        now: int,
        caller_id: str,
        caller_name: str,
        group_id: str,
        group_name: str,
        card_id: str,
    ) -> None:
        row = self._conn.execute(
            "SELECT * FROM ca_tokens WHERE chain = ? AND address = ?",
            (chain, address),
        ).fetchone()
        symbol = _text(token.get("symbol"))
        name = _text(token.get("name"))
        if row is None:
            if mcap <= 0 or not is_sane(mcap, ath, mcap):
                return
            self._conn.execute(
                """
                INSERT INTO ca_tokens (
                    chain, address, symbol, name, first_mcap, ath_mcap, last_mcap,
                    first_at, last_at, discoverer_id, discoverer_name,
                    group_id, group_name, card_id,
                    refreshed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chain,
                    address,
                    symbol,
                    name,
                    mcap,
                    max(ath, mcap),
                    mcap,
                    now,
                    now,
                    caller_id,
                    caller_name,
                    group_id,
                    group_name,
                    card_id,
                    now,
                ),
            )
            return
        first_mcap = _num(row["first_mcap"])
        if first_mcap <= 0 and mcap > 0 and is_sane(mcap, ath, mcap):
            first_mcap = mcap
        ath_mcap = _num(row["ath_mcap"])
        if ath > 0 and (first_mcap <= 0 or ath / first_mcap <= MAX_X):
            ath_mcap = max(ath_mcap, ath, mcap)
        last_mcap = _num(row["last_mcap"])
        if mcap > 0 and (first_mcap <= 0 or mcap / first_mcap <= MAX_X):
            last_mcap = mcap
        self._conn.execute(
            """
            UPDATE ca_tokens SET
                symbol = COALESCE(NULLIF(?, ''), symbol),
                name = COALESCE(NULLIF(?, ''), name),
                first_mcap = ?,
                ath_mcap = ?,
                last_mcap = ?,
                last_at = ?
            WHERE chain = ? AND address = ?
            """,
            (symbol, name, first_mcap, ath_mcap, last_mcap, now, chain, address),
        )

    def _upsert_call(
        self,
        chain: str,
        address: str,
        caller_id: str,
        caller_name: str,
        group_id: str,
        group_name: str,
        card_id: str,
        mcap: float,
        now: int,
    ) -> None:
        row = self._conn.execute(
            "SELECT mcap FROM ca_calls WHERE chain = ? AND address = ? AND caller_id = ?",
            (chain, address, caller_id),
        ).fetchone()
        if row is None:
            self._conn.execute(
                """
                INSERT INTO ca_calls (
                    chain, address, caller_id, caller_name,
                    group_id, group_name, card_id, mcap, called_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chain,
                    address,
                    caller_id,
                    caller_name,
                    group_id,
                    group_name,
                    card_id,
                    mcap,
                    now,
                ),
            )
            return
        if _num(row["mcap"]) <= 0 and mcap > 0:
            self._conn.execute(
                "UPDATE ca_calls SET mcap = ?, caller_name = COALESCE(NULLIF(?, ''), caller_name) "
                "WHERE chain = ? AND address = ? AND caller_id = ?",
                (mcap, caller_name, chain, address, caller_id),
            )

    def _upsert_group_first(
        self,
        chain: str,
        address: str,
        group_id: str,
        group_name: str,
        caller_id: str,
        caller_name: str,
        card_id: str,
        mcap: float,
        now: int,
    ) -> None:
        row = self._conn.execute(
            """
            SELECT mcap FROM ca_group_firsts
            WHERE chain = ? AND address = ? AND group_id = ?
            """,
            (chain, address, group_id),
        ).fetchone()
        if row is None:
            if mcap <= 0:
                return
            self._conn.execute(
                """
                INSERT INTO ca_group_firsts (
                    chain, address, group_id, group_name, caller_id, caller_name,
                    card_id, mcap, first_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chain,
                    address,
                    group_id,
                    group_name,
                    caller_id,
                    caller_name,
                    card_id,
                    mcap,
                    now,
                ),
            )
            return
        if _num(row["mcap"]) <= 0 and mcap > 0:
            self._conn.execute(
                """
                UPDATE ca_group_firsts SET
                    mcap = ?,
                    caller_name = COALESCE(NULLIF(?, ''), caller_name),
                    group_name = COALESCE(NULLIF(?, ''), group_name)
                WHERE chain = ? AND address = ? AND group_id = ?
                """,
                (mcap, caller_name, group_name, chain, address, group_id),
            )

    @staticmethod
    def _same_first(
        card_id: str,
        sender_id: str,
        created_at: int,
        found_card: str,
        found_by: str,
        found_at: int,
    ) -> bool:
        if card_id and found_card and card_id == found_card:
            return True
        return (
            bool(sender_id)
            and sender_id == found_by
            and created_at > 0
            and found_at > 0
            and abs(created_at - found_at) <= 2
        )

    def _decorate(
        self,
        token: dict[str, Any],
        chain: str,
        address: str,
        card_id: str = "",
        sender_id: str = "",
        created_at: int = 0,
        group_id: str = "",
    ) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT * FROM ca_tokens WHERE chain = ? AND address = ?",
            (chain, address),
        ).fetchone()
        if row is None:
            return token
        first = _num(row["first_mcap"])
        ath = _num(row["ath_mcap"])
        last = _num(row["last_mcap"])
        token["firstMcap"] = first
        token["athMarketCap"] = ath
        token["nowX"] = (last / first) if first > 0 and last > 0 else 0.0
        token["maxX"] = (ath / first) if first > 0 and ath > 0 else 0.0
        token["foundBy"] = row["discoverer_name"] or ""
        token["foundById"] = row["discoverer_id"] or ""
        token["foundGroup"] = row["group_name"] or ""
        token["foundGroupId"] = row["group_id"] or ""
        token["foundAt"] = int(row["first_at"] or 0)
        token["foundCardId"] = row["card_id"] or ""
        token["isFirstCall"] = self._same_first(
            card_id,
            sender_id,
            created_at,
            row["card_id"] or "",
            row["discoverer_id"] or "",
            int(row["first_at"] or 0),
        )
        grow = None
        if group_id:
            grow = self._conn.execute(
                """
                SELECT * FROM ca_group_firsts
                WHERE chain = ? AND address = ? AND group_id = ?
                """,
                (chain, address, group_id),
            ).fetchone()
        if grow is not None:
            group_first = _num(grow["mcap"])
            token["groupFirstMcap"] = group_first
            token["groupFoundBy"] = grow["caller_name"] or ""
            token["groupFoundById"] = grow["caller_id"] or ""
            token["groupFoundGroup"] = grow["group_name"] or ""
            token["groupFoundAt"] = int(grow["first_at"] or 0)
            token["groupFoundCardId"] = grow["card_id"] or ""
            token["groupNowX"] = (
                (last / group_first) if group_first > 0 and last > 0 else 0.0
            )
            token["groupMaxX"] = (
                (ath / group_first) if group_first > 0 and ath > 0 else 0.0
            )
            token["isGroupFirst"] = self._same_first(
                card_id,
                sender_id,
                created_at,
                grow["card_id"] or "",
                grow["caller_id"] or "",
                int(grow["first_at"] or 0),
            )
            token["sameGroupAsGlobal"] = group_id == (row["group_id"] or "")
        else:
            token["groupFirstMcap"] = 0.0
            token["groupFoundBy"] = ""
            token["groupFoundById"] = ""
            token["groupFoundGroup"] = ""
            token["groupFoundAt"] = 0
            token["groupFoundCardId"] = ""
            token["groupNowX"] = 0.0
            token["groupMaxX"] = 0.0
            token["isGroupFirst"] = False
            token["sameGroupAsGlobal"] = False
        return token

    def decorate_card(self, card: dict[str, Any]) -> dict[str, Any]:
        tokens = card.get("tokenCards")
        if not isinstance(tokens, list) or not tokens:
            return card
        card_id = _text(card.get("id"))
        sender_id = _text(card.get("senderUsername"))
        created_at = int(card.get("createTime") or 0)
        group_id = _text(card.get("conversationId"))
        with self._lock:
            card["tokenCards"] = [
                self._decorate(
                    dict(token),
                    _text(token.get("chain")).lower() or "_",
                    _text(token.get("address")).lower(),
                    card_id,
                    sender_id,
                    created_at,
                    group_id,
                )
                if isinstance(token, dict) and _text(token.get("address"))
                else token
                for token in tokens
            ]
        return card

    def _rank_rows(self, rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
        buckets: dict[str, dict[str, Any]] = {}
        for row in rows:
            ident = _text(row["caller_id"])
            if not ident:
                continue
            first = _num(row["first_mcap"])
            if not is_sane(first, row["ath_mcap"], row["last_mcap"]):
                continue
            call_mcap = _num(row["mcap"])
            peak = max(_num(row["ath_mcap"]), _num(row["last_mcap"]))
            multiple = _multiple(peak, call_mcap)
            item = buckets.setdefault(
                ident,
                {
                    "userId": ident,
                    "name": _text(row["caller_name"]) or ident,
                    "calls": 0,
                    "wins": 0,
                    "xs": [],
                },
            )
            if _text(row["caller_name"]):
                item["name"] = _text(row["caller_name"])
            item["calls"] += 1
            if multiple >= WIN_X:
                item["wins"] += 1
            if multiple > 0:
                item["xs"].append(multiple)
        ranked: list[dict[str, Any]] = []
        for item in buckets.values():
            n = int(item["calls"])
            wins = int(item["wins"])
            xs = item["xs"]
            avg_x = sum(xs) / len(xs) if xs else 0.0
            max_x = max(xs) if xs else 0.0
            rate = wins / n if n else 0.0
            wilson = wilson_lower(wins, n)
            score = wilson * math.log10(1 + n) * (1 + math.log1p(avg_x))
            ranked.append(
                {
                    "userId": item["userId"],
                    "name": item["name"],
                    "calls": n,
                    "wins": wins,
                    "winRate": rate,
                    "avgX": avg_x,
                    "maxX": max_x,
                    "score": score,
                    "avgXText": format_x(avg_x),
                    "maxXText": format_x(max_x),
                    "winRateText": f"{rate * 100:.0f}%",
                    "scoreText": f"{score:.2f}",
                }
            )
        ranked.sort(key=lambda row: (-row["score"], -row["avgX"], -row["calls"]))
        for index, row in enumerate(ranked, start=1):
            row["rank"] = index
        return ranked

    def _call_join_sql(self) -> str:
        return """
            SELECT c.caller_id, c.caller_name, c.mcap, t.ath_mcap, t.last_mcap, t.first_mcap
            FROM ca_calls c
            JOIN ca_tokens t ON t.chain = c.chain AND t.address = c.address
            WHERE c.called_at >= ?
        """

    def board(self) -> list[dict[str, Any]]:
        cut = self.cutoff()
        with self._lock:
            rows = self._conn.execute(self._call_join_sql(), (cut,)).fetchall()
        return self._rank_rows(rows)

    def user_stats(self, user_id: str) -> dict[str, Any] | None:
        ident = _text(user_id)
        if not ident:
            return None
        cut = self.cutoff()
        with self._lock:
            rows = self._conn.execute(
                self._call_join_sql() + " AND c.caller_id = ?",
                (cut, ident),
            ).fetchall()
        ranked = self._rank_rows(rows)
        return ranked[0] if ranked else None

    def user_calls(self, user_id: str) -> list[dict[str, Any]]:
        ident = _text(user_id)
        if not ident:
            return []
        cut = self.cutoff()
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT c.chain, c.address, c.caller_name, c.mcap, c.called_at,
                       c.group_id, c.group_name, c.card_id,
                       t.symbol, t.name, t.first_mcap, t.ath_mcap, t.last_mcap,
                       t.discoverer_id, t.first_at
                FROM ca_calls c
                JOIN ca_tokens t ON t.chain = c.chain AND t.address = c.address
                WHERE c.caller_id = ? AND c.called_at >= ?
                ORDER BY c.called_at DESC
                """,
                (ident, cut),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            first = _num(row["first_mcap"])
            if not is_sane(first, row["ath_mcap"], row["last_mcap"]):
                continue
            call_mcap = _num(row["mcap"])
            ath = _num(row["ath_mcap"])
            last = _num(row["last_mcap"])
            peak = max(ath, last)
            max_x = (peak / call_mcap) if call_mcap > 0 and peak > 0 else 0.0
            now_x = (last / call_mcap) if call_mcap > 0 and last > 0 else 0.0
            if max_x > MAX_X or now_x > MAX_X:
                continue
            out.append(
                {
                    "chain": _text(row["chain"]),
                    "address": _text(row["address"]),
                    "symbol": _text(row["symbol"]),
                    "name": _text(row["name"]),
                    "callerName": _text(row["caller_name"]),
                    "calledAt": int(row["called_at"] or 0),
                    "callMcap": call_mcap,
                    "firstMcap": first,
                    "athMcap": ath,
                    "lastMcap": last,
                    "nowX": now_x,
                    "maxX": max_x,
                    "nowXText": format_x(now_x),
                    "maxXText": format_x(max_x),
                    "win": max_x >= WIN_X,
                    "isFirst": _text(row["discoverer_id"]) == ident,
                    "groupId": _text(row["group_id"]),
                    "groupName": _text(row["group_name"]),
                    "cardId": _text(row["card_id"]),
                }
            )
        return out

    def user_profile(self, user_id: str) -> dict[str, Any]:
        ident = _text(user_id)
        calls = self.user_calls(ident)
        stats = next((row for row in self.board() if row["userId"] == ident), None)
        name = _text(stats.get("name") if stats else "")
        if not name and calls:
            name = _text(calls[0].get("callerName"))
        user = stats or {
            "userId": ident,
            "name": name or ident,
            "calls": len(calls),
            "wins": sum(1 for item in calls if item.get("win")),
            "winRate": 0.0,
            "avgX": 0.0,
            "maxX": 0.0,
            "score": 0.0,
            "avgXText": "",
            "maxXText": "",
            "winRateText": "0%",
            "scoreText": "0.00",
            "rank": 0,
        }
        if not stats and calls:
            wins = int(user["wins"])
            n = len(calls)
            user["winRate"] = wins / n if n else 0.0
            user["winRateText"] = f"{user['winRate'] * 100:.0f}%"
        return {"user": user, "calls": calls}

    def next_stale(self, now: int | None = None) -> dict[str, Any] | None:
        """按热度分层取下一条该刷的代币：新的 15 分钟，温的 2 小时，冷的 12 小时。"""
        stamp = int(now or time.time())
        bands = (
            (stamp - HOT_AGE, stamp + 1, stamp - REFRESH_HOT),
            (stamp - WARM_AGE, stamp - HOT_AGE, stamp - REFRESH_WARM),
            (0, stamp - WARM_AGE, stamp - REFRESH_COLD),
        )
        with self._lock:
            for min_last, max_last, max_refreshed in bands:
                row = self._conn.execute(
                    """
                    SELECT chain, address, last_at, refreshed_at, ath_mcap, first_mcap
                    FROM ca_tokens
                    WHERE first_mcap > 0
                      AND IFNULL(address, '') != ''
                      AND last_at >= ? AND last_at < ?
                      AND refreshed_at < ?
                    ORDER BY last_at DESC
                    LIMIT 1
                    """,
                    (min_last, max_last, max_refreshed),
                ).fetchone()
                if row is not None:
                    return dict(row)
        return None

    def apply_quote(
        self,
        chain: str,
        address: str,
        quote: dict[str, Any] | None,
        now: int | None = None,
    ) -> None:
        stamp = int(now or time.time())
        chain = _text(chain).lower() or "_"
        address = _text(address).lower()
        if not address:
            return
        with self._lock:
            row = self._conn.execute(
                """
                SELECT first_mcap, ath_mcap, last_mcap
                FROM ca_tokens WHERE chain = ? AND address = ?
                """,
                (chain, address),
            ).fetchone()
            if row is None:
                return
            first = _num(row["first_mcap"])
            ath = _num(row["ath_mcap"])
            last = _num(row["last_mcap"])
            symbol = ""
            name = ""
            if quote:
                mcap = _num(quote.get("marketCap"))
                new_ath = _num(quote.get("athMarketCap"))
                symbol = _text(quote.get("symbol"))
                name = _text(quote.get("name"))
                if mcap > 0 and (first <= 0 or mcap / first <= MAX_X):
                    last = mcap
                    if first <= 0 and is_sane(mcap, new_ath or mcap, mcap):
                        first = mcap
                if new_ath > 0 and (first <= 0 or new_ath / first <= MAX_X):
                    ath = max(ath, new_ath, last)
                elif last > ath:
                    ath = last
            self._conn.execute(
                """
                UPDATE ca_tokens SET
                    symbol = COALESCE(NULLIF(?, ''), symbol),
                    name = COALESCE(NULLIF(?, ''), name),
                    first_mcap = ?,
                    ath_mcap = ?,
                    last_mcap = ?,
                    refreshed_at = ?
                WHERE chain = ? AND address = ?
                """,
                (symbol, name, first, ath, last, stamp, chain, address),
            )
            self._conn.commit()

    def stats(self) -> dict[str, Any]:
        stamp = int(time.time())
        with self._lock:
            tokens = int(
                self._conn.execute(
                    "SELECT COUNT(*) FROM ca_tokens WHERE first_mcap > 0"
                ).fetchone()[0]
            )
            calls = int(self._conn.execute("SELECT COUNT(*) FROM ca_calls").fetchone()[0])
            users = int(
                self._conn.execute(
                    "SELECT COUNT(DISTINCT caller_id) FROM ca_calls "
                    "WHERE IFNULL(caller_id,'') != ''"
                ).fetchone()[0]
            )
            last_refresh = int(
                self._conn.execute(
                    "SELECT COALESCE(MAX(refreshed_at), 0) FROM ca_tokens"
                ).fetchone()[0]
            )
            stale = 0
            bands = (
                (stamp - HOT_AGE, stamp + 1, stamp - REFRESH_HOT),
                (stamp - WARM_AGE, stamp - HOT_AGE, stamp - REFRESH_WARM),
                (0, stamp - WARM_AGE, stamp - REFRESH_COLD),
            )
            for min_last, max_last, max_refreshed in bands:
                stale += int(
                    self._conn.execute(
                        """
                        SELECT COUNT(*) FROM ca_tokens
                        WHERE first_mcap > 0 AND IFNULL(address,'') != ''
                          AND last_at >= ? AND last_at < ?
                          AND refreshed_at < ?
                        """,
                        (min_last, max_last, max_refreshed),
                    ).fetchone()[0]
                )
        return {
            "tokens": tokens,
            "calls": calls,
            "users": users,
            "stale": stale,
            "lastRefresh": last_refresh,
        }
