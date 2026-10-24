"""进程内的消息缓冲和订阅。不落聊天正文。"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any

from .castore import CaStore
from .store import CardStore

# 榜单变化推送的最小间隔（秒）。几万个币时后台每秒都可能有变化，前端没必要跟着重算。
BOARD_PUSH_GAP = 5.0


class Hub:
    def __init__(
        self,
        persist: CardStore | None = None,
        cas: CaStore | None = None,
        limit: int = 400,
    ) -> None:
        self._persist = persist
        self.cas = cas
        self._limit = limit
        self._order: deque[str] = deque()
        self._cards: dict[str, dict[str, Any]] = {}
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        # 榜单变化推送节流：最多每 5 秒一次，期间的变化合并成一次补发。
        self._board_sent = 0.0
        self._board_timer: asyncio.TimerHandle | None = None
        self._status: dict[str, Any] = {
            "upstream": "idle",
            "detail": "还没有连接微信数据服务",
            "lastEventAt": None,
        }

    @property
    def persist(self):
        return self._persist

    def status(self) -> dict[str, Any]:
        return dict(self._status)

    def set_status(self, upstream: str, detail: str) -> None:
        self._status["upstream"] = upstream
        self._status["detail"] = detail
        self._broadcast({"type": "status", "status": self.status()})

    def mark_event(self, received_at: int) -> None:
        self._status["lastEventAt"] = received_at

    def messages(self, limit: int = 200) -> list[dict[str, Any]]:
        size = max(1, min(int(limit), self._limit))
        ids = list(self._order)[-size:]
        return [self._cards[item_id] for item_id in ids if item_id in self._cards]

    def clear(self) -> None:
        self._order.clear()
        self._cards.clear()
        self._broadcast({"type": "cleared"})

    def patch(self, card_id: str, fields: dict[str, Any], *, notify: bool = True) -> None:
        card = self._cards.get(card_id)
        if card is not None:
            card.update(fields)
            if self._persist is not None:
                self._persist.save(card)
            if notify:
                self._broadcast({"type": "patch", "id": card_id, "card": card})
            return
        if self._persist is None:
            return
        updated = self._persist.update_fields(card_id, fields)
        if updated is not None and notify:
            self._broadcast({"type": "patch", "id": card_id, "card": updated})

    def notify_settings(self, settings: dict[str, Any]) -> None:
        self._broadcast({"type": "settings", "settings": settings})

    def flush(self) -> None:
        if self._persist is not None:
            self._persist.flush()

    def stored_latest(self, conversation_id: str) -> int:
        if self._persist is None:
            return 0
        return self._persist.latest_time(conversation_id)

    def publish(self, card: dict[str, Any], *, notify: bool = True) -> bool:
        card_id = str(card.get("id") or "")
        if not card_id or card_id in self._cards:
            return False
        self._cards[card_id] = card
        self._order.append(card_id)
        if self._persist is not None:
            self._persist.save(card, replace=False)
        while len(self._order) > self._limit:
            oldest = self._order.popleft()
            self._cards.pop(oldest, None)
        self.mark_event(int(card.get("receivedAt") or 0))
        if notify:
            self._broadcast({"type": "message", "card": card})
        if self._persist is None:
            return True
        if self._persist.has_ca(card):
            self._persist.fill_context(card)
            self._persist.update_fields(
                card_id,
                {
                    "contextBefore": card.get("contextBefore") or [],
                    "contextAfter": card.get("contextAfter") or [],
                },
            )
            if notify:
                self._broadcast({"type": "patch", "id": card_id, "card": card})
        # 普通发言也要刷新同群同发送者更早 CA 卡的后三句。
        self._refresh_sender_ca_context(card)
        return True

    def _refresh_sender_ca_context(self, card: dict[str, Any]) -> None:
        if self._persist is None:
            return
        for older in self._persist.earlier_ca_cards(card):
            before, after = self._persist.around_sender(older)
            if older.get("contextBefore") == before and older.get("contextAfter") == after:
                continue
            self.patch(
                str(older.get("id") or ""),
                {"contextBefore": before, "contextAfter": after},
            )

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=800)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(queue)

    def notify_board(self) -> None:
        now = time.monotonic()
        wait = BOARD_PUSH_GAP - (now - self._board_sent)
        if wait <= 0:
            self._board_sent = now
            self._broadcast({"type": "ca-board"})
            return
        if self._board_timer is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._board_sent = now
            self._broadcast({"type": "ca-board"})
            return
        self._board_timer = loop.call_later(wait, self._flush_board)

    def _flush_board(self) -> None:
        self._board_timer = None
        self._board_sent = time.monotonic()
        self._broadcast({"type": "ca-board"})

    def _broadcast(self, event: dict[str, Any]) -> None:
        for queue in list(self._subscribers):
            self._offer(queue, event)

    @staticmethod
    def _offer(queue: asyncio.Queue[dict[str, Any]], event: dict[str, Any]) -> None:
        if queue.full():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                return
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            return
