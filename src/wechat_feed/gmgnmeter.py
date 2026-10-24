"""GMGN OpenAPI 调用计量：每次请求记一条，供本地「GMGN 用量」页查看是否逼近限频。

只在内存里留最近 1 小时，服务重启后清零；累计数从本次启动算起。不记录 Key 和请求参数。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

KEEP_SECONDS = 3600
MAX_EVENTS = 50_000
TIMELINE_SECONDS = 600
TIMELINE_BUCKET = 10
RECENT = 40
# GMGN 按每秒权重限频；本地 1.2 秒记账窗口只是余量，峰值按服务端口径 1 秒算。
SERVER_WINDOW = 1.0

# ok：拿到数据；biz：HTTP 200 但业务码非 0（如地址不合法）；limited：429/408；
# auth：401/403；error：其他 HTTP 错误；exception：网络异常/超时；skipped：冷却中没发出去。
OUTCOMES = ("ok", "biz", "limited", "auth", "error", "exception", "skipped")


@dataclass(slots=True)
class Event:
    at: float
    path: str
    weight: int
    background: bool
    outcome: str
    status: int
    latency_ms: float
    wait_ms: float
    remaining: int | None
    # 后台为实时解析让路的时间，和限速等待分开记。
    yield_ms: float = 0.0
    # 用户点的立即刷新（也算后台额度，单独标出来）。
    manual: bool = False


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[index]


class GmgnMeter:
    def __init__(self, limit: int, window: float, reserve: int) -> None:
        self.limit = int(limit)
        self.window = float(window)
        self.reserve = int(reserve)
        self.started_at = time.time()
        # 有数据的起点：重启后装回历史就往前挪，速率按真实跨度算，不按本次运行时长。
        self.data_since = self.started_at
        self._events: deque[Event] = deque(maxlen=MAX_EVENTS)
        self._totals: dict[str, int] = {key: 0 for key in OUTCOMES}
        self._total_weight = 0
        # 还没落盘的事件，由服务端定时 drain() 批量写进 SQLite。
        self._pending: list[Event] = []
        self._lock = threading.Lock()

    def preload(self, events: list[Event]) -> None:
        """重启后把最近 1 小时的明细从库里装回内存，实时面板不会清零（不计入本次启动累计）。"""
        with self._lock:
            merged = sorted([*events, *self._events], key=lambda e: e.at)
            if events:
                self.data_since = min(self.data_since, events[0].at)
            self._events = deque(merged[-MAX_EVENTS:], maxlen=MAX_EVENTS)

    def drain(self) -> tuple[list[Event], dict[int, tuple[int, int | None]]]:
        """取出待落盘的事件，并给出涉及到的每分钟的 (1 秒峰值, 最低剩余)。"""
        with self._lock:
            batch, self._pending = self._pending, []
            minutes = {int(e.at // 60) * 60 for e in batch}
            window = [e for e in self._events if int(e.at // 60) * 60 in minutes]
        stats: dict[int, tuple[int, int | None]] = {}
        for minute in minutes:
            rows = [e for e in window if int(e.at // 60) * 60 == minute]
            left = [e.remaining for e in rows if e.remaining is not None]
            stats[minute] = (int(self._peak(rows)), min(left) if left else None)
        return batch, stats

    def requeue(self, batch: list[Event]) -> None:
        """落盘失败时放回去，下次再写。"""
        with self._lock:
            self._pending = [*batch, *self._pending][-MAX_EVENTS:]

    def forget(self, start: float, end: float) -> None:
        """数据管理删掉某段时间后，内存里同一段也清掉，避免下次落盘又写回去。"""
        with self._lock:
            self._events = deque(
                (e for e in self._events if not start <= e.at < end), maxlen=MAX_EVENTS
            )
            self._pending = [e for e in self._pending if not start <= e.at < end]

    def record(
        self,
        path: str,
        weight: int,
        *,
        background: bool,
        outcome: str,
        status: int = 0,
        latency_ms: float = 0.0,
        wait_ms: float = 0.0,
        yield_ms: float = 0.0,
        manual: bool = False,
        remaining: int | None = None,
        at: float | None = None,
    ) -> None:
        """at：请求发出的时刻（time.time()）。不传就用当前时间（冷却跳过时）。"""
        sent = outcome != "skipped"
        event = Event(
            at=float(at) if at else time.time(),
            path=path,
            weight=int(weight) if sent else 0,
            background=background,
            outcome=outcome if outcome in OUTCOMES else "error",
            status=int(status or 0),
            latency_ms=float(latency_ms),
            wait_ms=float(wait_ms),
            remaining=remaining,
            yield_ms=float(yield_ms),
            manual=bool(manual),
        )
        with self._lock:
            self._events.append(event)
            self._pending.append(event)
            self._totals[event.outcome] += 1
            self._total_weight += event.weight
            cut = event.at - KEEP_SECONDS
            while self._events and self._events[0].at < cut:
                self._events.popleft()

    def _peak(self, events: list[Event]) -> float:
        """任意 1 秒内发出的最大权重（服务端限频口径），直接对比套餐上限。"""
        # 并发请求按响应先后入队，发出时间可能乱序，先排好再滑窗。
        sent = sorted((e for e in events if e.weight > 0), key=lambda e: e.at)
        best = 0
        total = 0
        left = 0
        for event in sent:
            total += event.weight
            while sent[left].at <= event.at - SERVER_WINDOW:
                total -= sent[left].weight
                left += 1
            best = max(best, total)
        return float(best)

    def _summary(self, events: list[Event], span: float) -> dict[str, Any]:
        counts = {key: 0 for key in OUTCOMES}
        weight = 0
        paths: dict[str, dict[str, Any]] = {}
        for event in events:
            counts[event.outcome] += 1
            weight += event.weight
            row = paths.setdefault(
                event.path,
                {
                    "path": event.path,
                    "calls": 0,
                    "weight": 0,
                    "realtime": 0,
                    "background": 0,
                    "latencies": [],
                    "waits": [],
                    "yields": [],
                    **{key: 0 for key in OUTCOMES},
                },
            )
            row[event.outcome] += 1
            if event.outcome == "skipped":
                continue
            row["calls"] += 1
            row["weight"] += event.weight
            row["background" if event.background else "realtime"] += 1
            if event.latency_ms > 0:
                row["latencies"].append(event.latency_ms)
            row["waits"].append(event.wait_ms)
            row["yields"].append(event.yield_ms)
        out_paths = []
        for row in paths.values():
            lat = row.pop("latencies")
            waits = row.pop("waits")
            yields = row.pop("yields")
            row["avgMs"] = round(sum(lat) / len(lat), 1) if lat else 0.0
            row["p95Ms"] = round(_pct(lat, 0.95), 1)
            row["avgWaitMs"] = round(sum(waits) / len(waits), 1) if waits else 0.0
            row["avgYieldMs"] = round(sum(yields) / len(yields), 1) if yields else 0.0
            row["share"] = round(row["weight"] / weight, 4) if weight else 0.0
            out_paths.append(row)
        out_paths.sort(key=lambda r: (-r["weight"], -r["calls"]))
        sent = sum(counts[key] for key in OUTCOMES if key != "skipped")
        return {
            "seconds": span,
            "calls": sent,
            "weight": weight,
            "weightPerSec": round(weight / span, 3) if span else 0.0,
            "usage": round(weight / span / self.limit, 4) if span and self.limit else 0.0,
            "peak": self._peak(events),
            "outcomes": counts,
            "successRate": round(counts["ok"] / sent, 4) if sent else 1.0,
            "paths": out_paths,
        }

    def snapshot(self, cooldowns: dict[str, float] | None = None) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            events = list(self._events)
            totals = dict(self._totals)
            total_weight = self._total_weight
        uptime = max(1.0, now - self.started_at)
        covered = max(1.0, now - self.data_since)

        def since(seconds: float) -> list[Event]:
            cut = now - seconds
            return [e for e in events if e.at >= cut]

        windows = {}
        for key, span in (("1m", 60), ("5m", 300), ("1h", KEEP_SECONDS)):
            windows[key] = self._summary(since(span), min(float(span), covered))
        buckets = TIMELINE_SECONDS // TIMELINE_BUCKET
        start = now - TIMELINE_SECONDS
        timeline = [
            {
                "at": int(start + i * TIMELINE_BUCKET),
                "realtime": 0,
                "background": 0,
                "limited": 0,
                "failed": 0,
            }
            for i in range(buckets)
        ]
        for event in events:
            if event.at < start:
                continue
            index = min(buckets - 1, int((event.at - start) // TIMELINE_BUCKET))
            slot = timeline[index]
            slot["background" if event.background else "realtime"] += event.weight
            if event.outcome == "limited":
                slot["limited"] += 1
            elif event.outcome in {"auth", "error", "exception"}:
                slot["failed"] += 1
        current = sum(e.weight for e in events if e.at >= now - self.window)
        remaining = [e.remaining for e in since(300) if e.remaining is not None]
        recent = [
            {
                "at": round(e.at, 3),
                "path": e.path,
                "weight": e.weight,
                "kind": "manual" if e.manual else ("background" if e.background else "realtime"),
                "outcome": e.outcome,
                "status": e.status,
                "latencyMs": round(e.latency_ms, 1),
                "waitMs": round(e.wait_ms, 1),
                "yieldMs": round(e.yield_ms, 1),
                "remaining": e.remaining,
            }
            for e in reversed(events[-RECENT:])
        ]
        return {
            "now": now,
            "startedAt": self.started_at,
            "uptime": round(uptime, 1),
            "config": {
                "limit": self.limit,
                "window": self.window,
                "reserve": self.reserve,
                # 本地记账后的理论上限：总吞吐与后台吞吐（权重/秒）。
                "safeCap": round(self.limit / self.window, 3),
                "backgroundCap": round((self.limit - self.reserve) / self.window, 3),
                "bucket": TIMELINE_BUCKET,
                "serverWindow": SERVER_WINDOW,
            },
            "current": {
                "windowWeight": current,
                "lastRemaining": next(
                    (e.remaining for e in reversed(events) if e.remaining is not None), None
                ),
                "minRemaining5m": min(remaining) if remaining else None,
                "cooldowns": {k: max(0.0, round(v - now, 1)) for k, v in (cooldowns or {}).items()},
            },
            "totals": {
                "outcomes": totals,
                "calls": sum(v for k, v in totals.items() if k != "skipped"),
                "weight": total_weight,
            },
            "windows": windows,
            "timeline": timeline,
            "recent": recent,
        }
