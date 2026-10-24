"""单独记下 CA 首次发现与每人喊单，保留 1 年。"""

from __future__ import annotations

import math
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .gmgnstore import file_bytes
from .tokens import peak_since

KEEP_SECONDS = 365 * 24 * 3600
WIN_X = 1.5
# 超过一万倍当脏数据：历史回灌、错市值、单位错位都会出这种数。
MAX_X = 10_000.0
# 刷新分层按「最后一次被喊」算：热 6 小时内、温 3 天内、其余为冷。
HOT_AGE = 6 * 3600
WARM_AGE = 3 * 86400
# 这是「最短」间隔：额度够时按它刷，到期的币多了就由限频窗口自然排队、间隔拉长。
# 稳态约 1.6 权重/秒（免费档 5 的三成），后台上限 2.5 权重/秒，见 tokens 的限频说明。
REFRESH_HOT = 30
REFRESH_WARM = 5 * 60
REFRESH_COLD = 60 * 60
# 新喊单 60 秒后首结算（那根 1m 才有数），失败 60 秒后重试；超过 6 小时回落到普通分层。
SETTLE_DELAY = 60
SETTLE_RETRY = 60
SETTLE_WINDOW = 6 * 3600
# 超过 24 小时没人再喊、且现市值低于 1.5 万的币暂停拉 K 线，下次被喊到再恢复。
DORMANT_AGE = 24 * 3600
DORMANT_MCAP = 15_000.0
# 水位线往回留 2 分钟重叠，兜住 GMGN 迟到入库的柱；重叠部分取 max，不会重复计数。
WATERMARK_OVERLAP = 120
# 一次刷新最多带几个未结算喊单（另加水位线锚点），剩下的下一轮再结算。
SETTLE_BATCH = 3
# 喊单不满 24 小时且还没到 1.5 倍的记为待定，不进胜率分母。
PENDING_AGE = 24 * 3600
# 均倍按单条 20 倍封顶，避免一个百倍单主导得分；最高倍仍显示真实值。
AVG_CAP = 20.0
BOARD_RANGES = {"all": KEEP_SECONDS, "30d": 30 * 86400, "7d": 7 * 86400}
# 用户详情页的日期筛选，today 另按本地 0 点算。
USER_RANGES = {"all": KEEP_SECONDS, "30d": 30 * 86400, "7d": 7 * 86400, "today": 86400}
# 手动刷新一次最多刷这么多个币（后台约 1.2 秒一个，100 个约 2 分钟）。
REFRESH_MAX = 100


def _num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _text(value: Any) -> str:
    return str(value or "").strip()


def _addr(value: Any) -> str:
    """EVM/Sui 的 0x 地址不分大小写，统一小写；Solana、Tron、TON 区分大小写，原样保留。"""
    text = _text(value)
    return text.lower() if text[:2].lower() == "0x" else text


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


def call_status(multiple: float, called_at: int, now: int) -> str:
    """win：已到 1.5 倍；pending：不满 24 小时且还没到；lose：满 24 小时仍没到。"""
    if multiple >= WIN_X:
        return "win"
    if called_at and now - int(called_at) < PENDING_AGE:
        return "pending"
    return "lose"


class CaStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
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
        # 手动删除过的时间段：启动时从 feed.db 回灌卡片，落在这些段里的不再写回榜单。
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ca_purged (
                start INTEGER NOT NULL,
                end INTEGER NOT NULL,
                purged_at INTEGER NOT NULL,
                PRIMARY KEY (start, end)
            )
            """
        )
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
        call_cols = {row[1] for row in self._conn.execute("PRAGMA table_info(ca_calls)")}
        if "ath_mcap" not in call_cols:
            self._conn.execute("ALTER TABLE ca_calls ADD COLUMN ath_mcap REAL NOT NULL DEFAULT 0")
        if "call_price" not in call_cols:
            self._conn.execute("ALTER TABLE ca_calls ADD COLUMN call_price REAL NOT NULL DEFAULT 0")
        group_cols = {row[1] for row in self._conn.execute("PRAGMA table_info(ca_group_firsts)")}
        if "ath_mcap" not in group_cols:
            self._conn.execute(
                "ALTER TABLE ca_group_firsts ADD COLUMN ath_mcap REAL NOT NULL DEFAULT 0"
            )
        if "last_price" not in cols:
            self._conn.execute(
                "ALTER TABLE ca_tokens ADD COLUMN last_price REAL NOT NULL DEFAULT 0"
            )
        if "kline_until" not in cols:
            # 增量刷新的水位线。旧库按上次全量刷新时间迁移：那次已按喊单后口径算过。
            self._conn.execute(
                "ALTER TABLE ca_tokens ADD COLUMN kline_until INTEGER NOT NULL DEFAULT 0"
            )
            self._conn.execute(
                "UPDATE ca_tokens SET kline_until = refreshed_at - ? WHERE refreshed_at > 0",
                (WATERMARK_OVERLAP,),
            )
        for table, at in (("ca_calls", "called_at"), ("ca_group_firsts", "first_at")):
            have = {row[1] for row in self._conn.execute(f"PRAGMA table_info({table})")}
            if "settled" in have:
                continue
            # 每条喊单只做一次首结算；旧库里刷新时已存在的喊单视为已结算。
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN settled INTEGER NOT NULL DEFAULT 0")
            self._conn.execute(
                f"""
                UPDATE {table} SET settled = 1
                WHERE EXISTS (
                    SELECT 1 FROM ca_tokens t
                    WHERE t.chain = {table}.chain AND t.address = {table}.address
                      AND t.refreshed_at > 0 AND {table}.{at} <= t.refreshed_at
                )
                """
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
                address = _addr(row["address"])
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
        if self._is_purged(now):
            return tokens
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
                address = _addr(token.get("address"))
                if not address:
                    out.append(token)
                    continue
                if not chain:
                    chain = "_"
                self._heal_case(chain, address)
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
                        _num(token.get("priceUsd")),
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
                out.append(self._decorate(token, chain, address, card_id, caller_id, now, group_id))
            self._conn.commit()
        return out

    def _heal_case(self, chain: str, address: str) -> None:
        """旧版把 Solana 等 base58 地址存成了小写，GMGN 不认；见到原大小写就原地改回来。"""
        low = address.lower()
        if low == address or self._has_token(chain, address) or not self._has_token(chain, low):
            return
        for table in ("ca_tokens", "ca_calls", "ca_group_firsts"):
            self._conn.execute(
                f"UPDATE {table} SET address = ? WHERE chain = ? AND address = ?",
                (address, chain, low),
            )

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
            if mcap <= 0 or not is_sane(mcap, mcap, mcap):
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
                    mcap,
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
            price = _num(token.get("priceUsd"))
            if price > 0:
                self._conn.execute(
                    "UPDATE ca_tokens SET last_price = ? WHERE chain = ? AND address = ?",
                    (price, chain, address),
                )
            return
        first_mcap = _num(row["first_mcap"])
        if first_mcap <= 0 and mcap > 0 and is_sane(mcap, mcap, mcap):
            first_mcap = mcap
        ath_mcap = _num(row["ath_mcap"])
        if mcap > 0 and (first_mcap <= 0 or mcap / first_mcap <= MAX_X):
            ath_mcap = max(ath_mcap, mcap)
        last_mcap = _num(row["last_mcap"])
        if mcap > 0 and (first_mcap <= 0 or mcap / first_mcap <= MAX_X):
            last_mcap = mcap
        price = _num(token.get("priceUsd"))
        self._conn.execute(
            """
            UPDATE ca_tokens SET
                symbol = COALESCE(NULLIF(?, ''), symbol),
                name = COALESCE(NULLIF(?, ''), name),
                first_mcap = ?,
                ath_mcap = ?,
                last_mcap = ?,
                last_at = ?,
                last_price = CASE WHEN ? > 0 THEN ? ELSE last_price END
            WHERE chain = ? AND address = ?
            """,
            (
                symbol,
                name,
                first_mcap,
                ath_mcap,
                last_mcap,
                now,
                price,
                price,
                chain,
                address,
            ),
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
        price: float = 0.0,
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
                    group_id, group_name, card_id, mcap, called_at, ath_mcap, call_price
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    mcap,
                    price,
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
                    card_id, mcap, first_at, ath_mcap
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    mcap,
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
        since_ath = ath if ath > 0 else last
        token["maxX"] = (since_ath / first) if first > 0 and since_ath > 0 else 0.0
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
            token["groupNowX"] = (last / group_first) if group_first > 0 and last > 0 else 0.0
            group_ath = _num(grow["ath_mcap"] if "ath_mcap" in grow.keys() else 0)
            if group_ath <= 0:
                group_ath = last
            token["groupMaxX"] = (
                (group_ath / group_first) if group_first > 0 and group_ath > 0 else 0.0
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
                    _addr(token.get("address")),
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

    def _rank_rows(self, rows: list[sqlite3.Row], now: int | None = None) -> list[dict[str, Any]]:
        stamp = int(now or time.time())
        buckets: dict[str, dict[str, Any]] = {}
        for row in rows:
            ident = _text(row["caller_id"])
            if not ident:
                continue
            first = _num(row["first_mcap"])
            last = _num(row["last_mcap"])
            if not is_sane(first, last, last):
                continue
            call_mcap = _num(row["mcap"])
            # 榜用喊单后的 K 线高点；还没刷到 K 线时用现市值，不用 GMGN 历史总 ATH。
            call_ath = _num(row["call_ath"] if "call_ath" in row.keys() else 0)
            peak = max(call_ath, last, call_mcap)
            multiple = _multiple(peak, call_mcap)
            item = buckets.setdefault(
                ident,
                {
                    "userId": ident,
                    "name": _text(row["caller_name"]) or ident,
                    "calls": 0,
                    "wins": 0,
                    "pending": 0,
                    "xs": [],
                    "max": 0.0,
                },
            )
            if _text(row["caller_name"]):
                item["name"] = _text(row["caller_name"])
            item["calls"] += 1
            item["max"] = max(item["max"], multiple)
            status = call_status(multiple, int(row["called_at"] or 0), stamp)
            if status == "pending":
                # 还没走完 24 小时的单不进分母，也不拉低均倍。
                item["pending"] += 1
                continue
            if status == "win":
                item["wins"] += 1
            if multiple > 0:
                item["xs"].append(min(multiple, AVG_CAP))
        ranked: list[dict[str, Any]] = []
        for item in buckets.values():
            calls = int(item["calls"])
            pending = int(item["pending"])
            n = calls - pending
            wins = int(item["wins"])
            xs = item["xs"]
            avg_x = sum(xs) / len(xs) if xs else 0.0
            max_x = float(item["max"])
            rate = wins / n if n else 0.0
            wilson = wilson_lower(wins, n)
            score = wilson * math.log10(1 + n) * (1 + math.log1p(avg_x))
            ranked.append(
                {
                    "userId": item["userId"],
                    "name": item["name"],
                    "calls": calls,
                    "settled": n,
                    "pending": pending,
                    "wins": wins,
                    "winRate": rate,
                    "avgX": avg_x,
                    "maxX": max_x,
                    "score": score,
                    "avgXText": format_x(avg_x),
                    "maxXText": format_x(max_x),
                    "winRateText": f"{rate * 100:.0f}%" if n else "待定",
                    "scoreText": f"{score:.2f}",
                }
            )
        ranked.sort(key=lambda row: (-row["score"], -row["avgX"], -row["calls"]))
        for index, row in enumerate(ranked, start=1):
            row["rank"] = index
        return ranked

    def _call_join_sql(self) -> str:
        return """
            SELECT c.caller_id, c.caller_name, c.mcap, c.called_at,
                   COALESCE(NULLIF(c.ath_mcap, 0), t.last_mcap) AS call_ath,
                   t.last_mcap, t.first_mcap
            FROM ca_calls c
            JOIN ca_tokens t ON t.chain = c.chain AND t.address = c.address
            WHERE c.called_at >= ?
        """

    def board(self, window: str = "all", now: int | None = None) -> list[dict[str, Any]]:
        """window：all / 30d / 7d，按喊单时间截取。"""
        stamp = int(now or time.time())
        span = BOARD_RANGES.get(window, KEEP_SECONDS)
        with self._lock:
            rows = self._conn.execute(self._call_join_sql(), (stamp - span,)).fetchall()
        return self._rank_rows(rows, stamp)

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
                       c.group_id, c.group_name, c.card_id, c.ath_mcap AS call_ath,
                       t.symbol, t.name, t.first_mcap, t.last_mcap,
                       t.discoverer_id, t.first_at
                FROM ca_calls c
                JOIN ca_tokens t ON t.chain = c.chain AND t.address = c.address
                WHERE c.caller_id = ? AND c.called_at >= ?
                ORDER BY c.called_at DESC
                """,
                (ident, cut),
            ).fetchall()
        stamp = int(time.time())
        out: list[dict[str, Any]] = []
        for row in rows:
            first = _num(row["first_mcap"])
            last = _num(row["last_mcap"])
            if not is_sane(first, last, last):
                continue
            call_mcap = _num(row["mcap"])
            ath = _num(row["call_ath"])
            peak = max(ath, last, call_mcap)
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
                    "status": call_status(max_x, int(row["called_at"] or 0), stamp),
                    "isFirst": _text(row["discoverer_id"]) == ident,
                    "groupId": _text(row["group_id"]),
                    "groupName": _text(row["group_name"]),
                    "cardId": _text(row["card_id"]),
                }
            )
        return out

    def refresh_targets(
        self, user_id: str, scope: str = "pending", window: str = "7d", now: int | None = None
    ) -> list[dict[str, Any]]:
        """某个用户要立即刷新的币（同一个币只刷一次，新喊的在前）。

        scope=pending：只刷待定（不满 24 小时且还没到 1.5 倍）；
        scope=range：刷 window（today / 7d / 30d / all）内喊过的全部。
        """
        stamp = int(now or time.time())
        if scope == "pending":
            since = stamp - PENDING_AGE
        else:
            since = stamp - USER_RANGES.get(window, KEEP_SECONDS)
            if window == "today":
                local = time.localtime(stamp)
                since = int(
                    time.mktime((local.tm_year, local.tm_mon, local.tm_mday, 0, 0, 0, 0, 0, -1))
                )
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT c.chain, c.address, c.mcap, c.called_at,
                       COALESCE(NULLIF(c.ath_mcap, 0), t.last_mcap) AS call_ath,
                       t.last_mcap, t.last_price
                FROM ca_calls c
                JOIN ca_tokens t ON t.chain = c.chain AND t.address = c.address
                WHERE c.caller_id = ? AND c.called_at >= ? AND t.first_mcap > 0
                ORDER BY c.called_at DESC
                """,
                (_text(user_id), max(since, self.cutoff())),
            ).fetchall()
        out: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for row in rows:
            key = (_text(row["chain"]), _text(row["address"]))
            if key in seen or not key[1]:
                continue
            call_mcap = _num(row["mcap"])
            peak = max(_num(row["call_ath"]), _num(row["last_mcap"]), call_mcap)
            if scope == "pending":
                multiple = _multiple(peak, call_mcap)
                if call_status(multiple, int(row["called_at"] or 0), stamp) != "pending":
                    continue
            seen.add(key)
            out.append(
                {
                    "chain": key[0],
                    "address": key[1],
                    "last_mcap": _num(row["last_mcap"]),
                    "last_price": _num(row["last_price"]),
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

    def _unsettled_sql(self, extra_calls: str = "", extra_groups: str = "") -> str:
        return f"""(
            EXISTS (
                SELECT 1 FROM ca_calls c
                WHERE c.chain = t.chain AND c.address = t.address AND c.settled = 0 {extra_calls}
            )
            OR EXISTS (
                SELECT 1 FROM ca_group_firsts g
                WHERE g.chain = t.chain AND g.address = t.address AND g.settled = 0 {extra_groups}
            )
        )"""

    def _dormant_sql(self) -> str:
        return "(t.last_at < ? AND t.last_mcap < ?)"

    def next_due(self, now: int | None = None) -> dict[str, Any] | None:
        """下一条该刷的代币。

        1. 6 小时内的新喊单还没首结算的，喊单 60 秒后优先；
        2. 其余按最后被喊时间分层：热 2 分钟、温 30 分钟、冷 6 小时；
        3. 超过 24 小时没人喊、现市值不到 1.5 万的休眠，直到再被喊到。
        """
        stamp = int(now or time.time())
        cols = "t.chain, t.address, t.first_at, t.last_at, t.last_mcap, t.last_price"
        base = "t.first_mcap > 0 AND IFNULL(t.address, '') != ''"
        fresh = self._unsettled_sql(
            "AND c.called_at <= ? AND c.called_at >= ?",
            "AND g.first_at <= ? AND g.first_at >= ?",
        )
        window = (stamp - SETTLE_DELAY, stamp - SETTLE_WINDOW)
        with self._lock:
            row = self._conn.execute(
                f"""
                SELECT {cols} FROM ca_tokens t
                WHERE {base} AND t.refreshed_at <= ? AND {fresh}
                ORDER BY t.last_at DESC LIMIT 1
                """,
                (stamp - SETTLE_RETRY, *window, *window),
            ).fetchone()
            if row is not None:
                return dict(row)
            bands = (
                (stamp - HOT_AGE, stamp + 1, stamp - REFRESH_HOT),
                (stamp - WARM_AGE, stamp - HOT_AGE, stamp - REFRESH_WARM),
                (0, stamp - WARM_AGE, stamp - REFRESH_COLD),
            )
            for min_last, max_last, max_refreshed in bands:
                row = self._conn.execute(
                    f"""
                    SELECT {cols} FROM ca_tokens t
                    WHERE {base}
                      AND t.last_at >= ? AND t.last_at < ?
                      AND t.refreshed_at < ?
                      AND NOT {self._dormant_sql()}
                    ORDER BY t.refreshed_at ASC LIMIT 1
                    """,
                    (min_last, max_last, max_refreshed, stamp - DORMANT_AGE, DORMANT_MCAP),
                ).fetchone()
                if row is not None:
                    return dict(row)
        return None

    def plan_anchors(self, chain: str, address: str) -> list[int]:
        """这次刷新要拉的锚点：水位线 + 至多 3 条未结算喊单的时间。

        水位线之后的柱对所有已结算的喊单都是「喊单之后」，一份数据大家共用；
        只有未结算的喊单需要在自己的喊单时间附近补细粒度段。
        """
        chain = _text(chain).lower() or "_"
        address = _addr(address)
        with self._lock:
            row = self._conn.execute(
                "SELECT first_at, kline_until FROM ca_tokens WHERE chain = ? AND address = ?",
                (chain, address),
            ).fetchone()
            if row is None:
                return []
            pending = [
                int(r["t"])
                for r in self._conn.execute(
                    """
                    SELECT called_at AS t FROM ca_calls
                    WHERE chain = ? AND address = ? AND settled = 0 AND called_at > 0
                    UNION
                    SELECT first_at AS t FROM ca_group_firsts
                    WHERE chain = ? AND address = ? AND settled = 0 AND first_at > 0
                    ORDER BY t ASC
                    """,
                    (chain, address, chain, address),
                ).fetchall()
            ]
        mark = int(row["kline_until"] or 0)
        head = mark if mark > 0 else int(row["first_at"] or 0)
        points = {p for p in [head, *pending[:SETTLE_BATCH]] if p > 0}
        return sorted(points)

    def mark_attempt(self, chain: str, address: str, now: int | None = None) -> None:
        """拉取失败也记一次尝试时间，避免同一个币被立刻反复重试。"""
        with self._lock:
            self._conn.execute(
                "UPDATE ca_tokens SET refreshed_at = ? WHERE chain = ? AND address = ?",
                (int(now or time.time()), _text(chain).lower() or "_", _addr(address)),
            )
            self._conn.commit()

    def apply_quote(
        self,
        chain: str,
        address: str,
        quote: dict[str, Any] | None,
        now: int | None = None,
        anchors: list[int] | None = None,
    ) -> bool:
        """写回一次刷新结果。高点只增不减；K 线完整拉到时推进水位线、标记本批喊单已结算。

        返回榜单数据是否有变化，没变就不必推送刷新。
        """
        stamp = int(now or time.time())
        chain = _text(chain).lower() or "_"
        address = _addr(address)
        if not address:
            return False
        with self._lock:
            row = self._conn.execute(
                """
                SELECT first_mcap, ath_mcap, last_mcap, first_at, kline_until
                FROM ca_tokens WHERE chain = ? AND address = ?
                """,
                (chain, address),
            ).fetchone()
            if row is None:
                return False
            first = _num(row["first_mcap"])
            ath = _num(row["ath_mcap"])
            last = _num(row["last_mcap"])
            before = (first, ath, last)
            first_at = int(row["first_at"] or 0)
            mark = int(row["kline_until"] or 0)
            symbol = ""
            name = ""
            bars = _quote_bars(quote)
            price = 0.0
            if quote:
                mcap = _num(quote.get("marketCap"))
                price = _num(quote.get("priceUsd"))
                symbol = _text(quote.get("symbol"))
                name = _text(quote.get("name"))
                if mcap > 0 and (first <= 0 or mcap / first <= MAX_X):
                    last = mcap
                    if first <= 0 and is_sane(mcap, mcap, mcap):
                        first = mcap
                if last > 0 and (first <= 0 or last / first <= MAX_X):
                    ath = max(ath, last)
            changed = False
            if bars:
                since_ath = peak_since(bars, first_at)
                if since_ath > 0 and (first <= 0 or since_ath / first <= MAX_X):
                    ath = max(ath, since_ath)
                changed = self._apply_since_ath(chain, address, bars)
            if quote and quote.get("klineOk"):
                fetched_to = int(quote.get("klineTo") or stamp)
                mark = max(mark, fetched_to - WATERMARK_OVERLAP)
                points = sorted({int(a) for a in (anchors or []) if int(a or 0) > 0})
                if points:
                    marks = ",".join("?" * len(points))
                    for table, at in (("ca_calls", "called_at"), ("ca_group_firsts", "first_at")):
                        self._conn.execute(
                            f"""
                            UPDATE {table} SET settled = 1
                            WHERE chain = ? AND address = ? AND {at} IN ({marks})
                            """,
                            (chain, address, *points),
                        )
            self._conn.execute(
                """
                UPDATE ca_tokens SET
                    symbol = COALESCE(NULLIF(?, ''), symbol),
                    name = COALESCE(NULLIF(?, ''), name),
                    first_mcap = ?,
                    ath_mcap = ?,
                    last_mcap = ?,
                    refreshed_at = ?,
                    kline_until = ?,
                    last_price = CASE WHEN ? > 0 THEN ? ELSE last_price END
                WHERE chain = ? AND address = ?
                """,
                (symbol, name, first, ath, last, stamp, mark, price, price, chain, address),
            )
            self._conn.commit()
        return changed or (first, ath, last) != before

    def _apply_since_ath(
        self,
        chain: str,
        address: str,
        bars: list[tuple[int, float]],
    ) -> bool:
        """一份共用 K 线，按各用户/各群的喊单时间切片；高点只增不减。"""
        changed = False
        for call in self._conn.execute(
            "SELECT caller_id, called_at, mcap FROM ca_calls WHERE chain = ? AND address = ?",
            (chain, address),
        ).fetchall():
            peak = peak_since(bars, int(call["called_at"] or 0))
            call_mcap = _num(call["mcap"])
            if peak <= 0 or (call_mcap > 0 and peak / call_mcap > MAX_X):
                continue
            cur = self._conn.execute(
                """
                UPDATE ca_calls SET ath_mcap = ?
                WHERE chain = ? AND address = ? AND caller_id = ? AND ath_mcap < ?
                """,
                (peak, chain, address, call["caller_id"], peak),
            )
            changed = changed or cur.rowcount > 0
        for group in self._conn.execute(
            "SELECT group_id, first_at, mcap FROM ca_group_firsts WHERE chain = ? AND address = ?",
            (chain, address),
        ).fetchall():
            peak = peak_since(bars, int(group["first_at"] or 0))
            base = _num(group["mcap"])
            if peak <= 0 or (base > 0 and peak / base > MAX_X):
                continue
            cur = self._conn.execute(
                """
                UPDATE ca_group_firsts SET ath_mcap = ?
                WHERE chain = ? AND address = ? AND group_id = ? AND ath_mcap < ?
                """,
                (peak, chain, address, group["group_id"], peak),
            )
            changed = changed or cur.rowcount > 0
        return changed

    def anchors(self, chain: str, address: str) -> list[int]:
        """该币所有喊单时间与各群首喊时间。"""
        chain = _text(chain).lower() or "_"
        address = _addr(address)
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT called_at AS t FROM ca_calls WHERE chain = ? AND address = ?
                UNION
                SELECT first_at AS t FROM ca_group_firsts WHERE chain = ? AND address = ?
                UNION
                SELECT first_at AS t FROM ca_tokens WHERE chain = ? AND address = ?
                """,
                (chain, address, chain, address, chain, address),
            ).fetchall()
        return sorted({int(row["t"] or 0) for row in rows if int(row["t"] or 0) > 0})

    # ---- 数据管理 ----

    def _is_purged(self, at: int) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM ca_purged WHERE start <= ? AND ? < end LIMIT 1", (at, at)
            ).fetchone()
        return row is not None

    def data_summary(self) -> dict[str, Any]:
        with self._lock:
            span = self._conn.execute(
                "SELECT MIN(called_at), MAX(called_at), COUNT(*) FROM ca_calls WHERE called_at > 0"
            ).fetchone()
            months = self._conn.execute(
                """
                SELECT strftime('%Y-%m', called_at, 'unixepoch', 'localtime') AS month,
                       COUNT(*) AS calls,
                       COUNT(DISTINCT address) AS tokens,
                       COUNT(DISTINCT caller_id) AS users
                FROM ca_calls WHERE called_at > 0
                GROUP BY month ORDER BY month DESC
                """
            ).fetchall()
            tokens = self._count("SELECT COUNT(*) FROM ca_tokens")
            purged = self._conn.execute(
                "SELECT start, end, purged_at FROM ca_purged ORDER BY start DESC"
            ).fetchall()
        return {
            "oldest": span[0],
            "newest": span[1],
            "calls": int(span[2] or 0),
            "tokens": tokens,
            "months": [
                {
                    "month": m["month"],
                    "calls": int(m["calls"] or 0),
                    "tokens": int(m["tokens"] or 0),
                    "users": int(m["users"] or 0),
                }
                for m in months
            ],
            "purged": [dict(row) for row in purged],
            "keepDays": KEEP_SECONDS // 86400,
            "bytes": file_bytes(self._path),
        }

    def purge(self, start: int, end: int, dry_run: bool = True) -> dict[str, int]:
        """删除喊单时间落在 [start, end) 的榜单记录。dry_run 只数不删。

        喊单、群首喊按各自时间删；删完后没有任何喊单和群首喊的代币一起删掉。
        同时记下这一段，启动回灌 feed.db 时不再把这段写回来。
        """
        with self._lock:
            calls = self._count(
                "SELECT COUNT(*) FROM ca_calls WHERE called_at >= ? AND called_at < ?",
                (start, end),
            )
            groups = self._count(
                "SELECT COUNT(*) FROM ca_group_firsts WHERE first_at >= ? AND first_at < ?",
                (start, end),
            )
            orphan_sql = """
                FROM ca_tokens t
                WHERE NOT EXISTS (
                    SELECT 1 FROM ca_calls c
                    WHERE c.chain = t.chain AND c.address = t.address
                      AND NOT (c.called_at >= ? AND c.called_at < ?)
                )
                AND NOT EXISTS (
                    SELECT 1 FROM ca_group_firsts g
                    WHERE g.chain = t.chain AND g.address = t.address
                      AND NOT (g.first_at >= ? AND g.first_at < ?)
                )
                AND t.first_at >= ? AND t.first_at < ?
            """
            span = (start, end, start, end, start, end)
            tokens = self._count(f"SELECT COUNT(*) {orphan_sql}", span)
            if not dry_run:
                self._conn.execute(
                    f"DELETE FROM ca_tokens WHERE rowid IN (SELECT t.rowid {orphan_sql})", span
                )
                self._conn.execute(
                    "DELETE FROM ca_calls WHERE called_at >= ? AND called_at < ?", (start, end)
                )
                self._conn.execute(
                    "DELETE FROM ca_group_firsts WHERE first_at >= ? AND first_at < ?",
                    (start, end),
                )
                self._conn.execute(
                    "INSERT OR REPLACE INTO ca_purged (start, end, purged_at) VALUES (?, ?, ?)",
                    (int(start), int(end), int(time.time())),
                )
                self._conn.commit()
        return {"calls": calls, "groupFirsts": groups, "tokens": tokens}

    def vacuum(self) -> int:
        with self._lock:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._conn.execute("VACUUM")
        return file_bytes(self._path)

    def _count(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        return int(self._conn.execute(sql, params).fetchone()[0] or 0)

    def stats(self, now: int | None = None) -> dict[str, Any]:
        stamp = int(now or time.time())
        live = "SELECT COUNT(*) FROM ca_tokens t WHERE t.first_mcap > 0"
        with self._lock:
            tokens = self._count(live)
            calls = self._count("SELECT COUNT(*) FROM ca_calls")
            users = self._count(
                """
                SELECT COUNT(DISTINCT caller_id) FROM ca_calls
                WHERE IFNULL(caller_id, '') != ''
                """
            )
            last_refresh = self._count("SELECT COALESCE(MAX(refreshed_at), 0) FROM ca_tokens")
            settling = self._count(f"{live} AND {self._unsettled_sql()}")
            dormant = self._count(
                f"{live} AND {self._dormant_sql()}", (stamp - DORMANT_AGE, DORMANT_MCAP)
            )
        return {
            "tokens": tokens,
            "calls": calls,
            "users": users,
            "lastRefresh": last_refresh,
            "settling": settling,
            "dormant": dormant,
            "dormantMcap": DORMANT_MCAP,
        }


def _quote_bars(quote: dict[str, Any] | None) -> list[tuple[int, float]]:
    if not quote:
        return []
    raw = quote.get("klineBars")
    if not isinstance(raw, list):
        return []
    out: list[tuple[int, float]] = []
    for item in raw:
        try:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                out.append((int(item[0] or 0), float(item[1] or 0)))
            elif isinstance(item, dict):
                out.append(
                    (
                        int(item.get("t") or item.get("time") or 0),
                        float(item.get("mcap") or item.get("v") or 0),
                    )
                )
        except (TypeError, ValueError):
            continue
    return [(t, v) for t, v in out if v > 0]
