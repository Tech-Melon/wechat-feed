"""GMGN 调用计量落盘：逐条明细留 7 天，按分钟汇总留 1 年。

写入由服务端每 5 秒批量调用一次（在线程里执行），不在请求路径上同步写库。
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .gmgnmeter import Event

RAW_KEEP = 7 * 86400
MINUTE_KEEP = 365 * 86400
# 历史曲线的范围 → 每个桶的秒数。
HISTORY_RANGES = {
    "24h": (86400, 900),
    "7d": (7 * 86400, 3600),
    "30d": (30 * 86400, 6 * 3600),
    "1y": (365 * 86400, 86400),
}
FAILED = ("auth", "error", "exception")


class GmgnStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS gmgn_events (
                at REAL NOT NULL,
                path TEXT NOT NULL,
                weight INTEGER NOT NULL,
                kind TEXT NOT NULL,
                outcome TEXT NOT NULL,
                status INTEGER NOT NULL DEFAULT 0,
                latency_ms REAL NOT NULL DEFAULT 0,
                wait_ms REAL NOT NULL DEFAULT 0,
                remaining INTEGER,
                yield_ms REAL NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_gmgn_events_at ON gmgn_events(at);
            CREATE TABLE IF NOT EXISTS gmgn_minute (
                minute INTEGER NOT NULL,
                path TEXT NOT NULL,
                kind TEXT NOT NULL,
                outcome TEXT NOT NULL,
                calls INTEGER NOT NULL DEFAULT 0,
                weight INTEGER NOT NULL DEFAULT 0,
                latency_sum REAL NOT NULL DEFAULT 0,
                latency_max REAL NOT NULL DEFAULT 0,
                wait_sum REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (minute, path, kind, outcome)
            );
            CREATE TABLE IF NOT EXISTS gmgn_minute_stat (
                minute INTEGER PRIMARY KEY,
                peak INTEGER NOT NULL DEFAULT 0,
                min_remaining INTEGER
            );
            """
        )
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(gmgn_events)")}
        if "yield_ms" not in cols:
            self._conn.execute(
                "ALTER TABLE gmgn_events ADD COLUMN yield_ms REAL NOT NULL DEFAULT 0"
            )
        self._conn.commit()

    def write(self, events: list[Event], minute_stats: dict[int, tuple[int, int | None]]) -> None:
        if not events and not minute_stats:
            return
        rollup: dict[tuple[int, str, str, str], list[float]] = {}
        for e in events:
            key = (
                int(e.at // 60) * 60,
                e.path,
                _kind(e),
                e.outcome,
            )
            row = rollup.setdefault(key, [0, 0, 0.0, 0.0, 0.0])
            row[0] += 1
            row[1] += e.weight
            row[2] += e.latency_ms
            row[3] = max(row[3], e.latency_ms)
            row[4] += e.wait_ms
        with self._lock:
            self._conn.executemany(
                """
                INSERT INTO gmgn_events
                    (at, path, weight, kind, outcome, status,
                     latency_ms, wait_ms, remaining, yield_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        e.at,
                        e.path,
                        e.weight,
                        _kind(e),
                        e.outcome,
                        e.status,
                        e.latency_ms,
                        e.wait_ms,
                        e.remaining,
                        e.yield_ms,
                    )
                    for e in events
                ],
            )
            self._conn.executemany(
                """
                INSERT INTO gmgn_minute
                    (minute, path, kind, outcome, calls, weight, latency_sum, latency_max, wait_sum)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (minute, path, kind, outcome) DO UPDATE SET
                    calls = calls + excluded.calls,
                    weight = weight + excluded.weight,
                    latency_sum = latency_sum + excluded.latency_sum,
                    latency_max = MAX(latency_max, excluded.latency_max),
                    wait_sum = wait_sum + excluded.wait_sum
                """,
                [(*key, *vals) for key, vals in rollup.items()],
            )
            self._conn.executemany(
                """
                INSERT INTO gmgn_minute_stat (minute, peak, min_remaining) VALUES (?, ?, ?)
                ON CONFLICT (minute) DO UPDATE SET
                    peak = MAX(peak, excluded.peak),
                    min_remaining = CASE
                        WHEN excluded.min_remaining IS NULL THEN min_remaining
                        WHEN min_remaining IS NULL THEN excluded.min_remaining
                        ELSE MIN(min_remaining, excluded.min_remaining)
                    END
                """,
                [(minute, peak, left) for minute, (peak, left) in minute_stats.items()],
            )
            self._conn.commit()

    def load_recent(self, seconds: float, now: float | None = None) -> list[Event]:
        cut = float(now or time.time()) - seconds
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM gmgn_events WHERE at >= ? ORDER BY at", (cut,)
            ).fetchall()
        return [
            Event(
                at=float(r["at"]),
                path=str(r["path"]),
                weight=int(r["weight"]),
                background=r["kind"] != "realtime",
                manual=r["kind"] == "manual",
                outcome=str(r["outcome"]),
                status=int(r["status"] or 0),
                latency_ms=float(r["latency_ms"] or 0),
                wait_ms=float(r["wait_ms"] or 0),
                remaining=r["remaining"],
                yield_ms=float(r["yield_ms"] or 0),
            )
            for r in rows
        ]

    def history(self, window: str = "24h", now: float | None = None) -> dict[str, Any]:
        span, bucket = HISTORY_RANGES.get(window, HISTORY_RANGES["24h"])
        stamp = int(now or time.time())
        end = (stamp // bucket + 1) * bucket
        start = end - span
        failed = ",".join(f"'{o}'" for o in FAILED)
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT (minute - ?) / ? AS slot,
                       SUM(CASE WHEN outcome != 'skipped' THEN calls ELSE 0 END) AS calls,
                       SUM(weight) AS weight,
                       SUM(CASE WHEN kind != 'realtime' THEN weight ELSE 0 END) AS bg_weight,
                       SUM(CASE WHEN outcome = 'limited' THEN calls ELSE 0 END) AS limited,
                       SUM(CASE WHEN outcome IN ({failed}) THEN calls ELSE 0 END) AS failed
                FROM gmgn_minute WHERE minute >= ? AND minute < ?
                GROUP BY slot
                """,
                (start, bucket, start, end),
            ).fetchall()
            peaks = self._conn.execute(
                """
                SELECT (minute - ?) / ? AS slot, MAX(peak) AS peak, MIN(min_remaining) AS low
                FROM gmgn_minute_stat WHERE minute >= ? AND minute < ?
                GROUP BY slot
                """,
                (start, bucket, start, end),
            ).fetchall()
            paths = self._conn.execute(
                f"""
                SELECT path,
                       SUM(CASE WHEN outcome != 'skipped' THEN calls ELSE 0 END) AS calls,
                       SUM(weight) AS weight,
                       SUM(CASE WHEN outcome = 'ok' THEN calls ELSE 0 END) AS ok,
                       SUM(CASE WHEN outcome = 'biz' THEN calls ELSE 0 END) AS biz,
                       SUM(CASE WHEN outcome = 'limited' THEN calls ELSE 0 END) AS limited,
                       SUM(CASE WHEN outcome IN ({failed}) THEN calls ELSE 0 END) AS failed,
                       SUM(CASE WHEN outcome = 'skipped' THEN calls ELSE 0 END) AS skipped,
                       SUM(latency_sum) AS latency_sum,
                       MAX(latency_max) AS latency_max
                FROM gmgn_minute WHERE minute >= ? AND minute < ?
                GROUP BY path ORDER BY weight DESC
                """,
                (start, end),
            ).fetchall()
        slots = int(span // bucket)
        series = [
            {
                "at": start + i * bucket,
                "calls": 0,
                "weight": 0,
                "bgWeight": 0,
                "limited": 0,
                "failed": 0,
                "peak": 0,
                "low": None,
            }
            for i in range(slots)
        ]
        for r in rows:
            i = int(r["slot"])
            if 0 <= i < slots:
                series[i].update(
                    calls=int(r["calls"] or 0),
                    weight=int(r["weight"] or 0),
                    bgWeight=int(r["bg_weight"] or 0),
                    limited=int(r["limited"] or 0),
                    failed=int(r["failed"] or 0),
                )
        for r in peaks:
            i = int(r["slot"])
            if 0 <= i < slots:
                series[i]["peak"] = int(r["peak"] or 0)
                series[i]["low"] = r["low"]
        total_weight = sum(s["weight"] for s in series)
        out_paths = []
        for r in paths:
            calls = int(r["calls"] or 0)
            out_paths.append(
                {
                    "path": r["path"],
                    "calls": calls,
                    "weight": int(r["weight"] or 0),
                    "share": round(int(r["weight"] or 0) / total_weight, 4)
                    if total_weight
                    else 0.0,
                    "ok": int(r["ok"] or 0),
                    "biz": int(r["biz"] or 0),
                    "limited": int(r["limited"] or 0),
                    "failed": int(r["failed"] or 0),
                    "skipped": int(r["skipped"] or 0),
                    "avgMs": round(float(r["latency_sum"] or 0) / calls, 1) if calls else 0.0,
                    "maxMs": round(float(r["latency_max"] or 0), 1),
                }
            )
        return {
            "range": window if window in HISTORY_RANGES else "24h",
            "bucket": bucket,
            "start": start,
            "end": end,
            "series": series,
            "paths": out_paths,
            "totals": {
                "calls": sum(s["calls"] for s in series),
                "weight": total_weight,
                "limited": sum(s["limited"] for s in series),
                "failed": sum(s["failed"] for s in series),
                "peak": max((s["peak"] for s in series), default=0),
                "weightPerSec": round(total_weight / span, 4),
            },
        }

    def prune(self, now: float | None = None) -> None:
        stamp = float(now or time.time())
        with self._lock:
            self._conn.execute("DELETE FROM gmgn_events WHERE at < ?", (stamp - RAW_KEEP,))
            self._conn.execute("DELETE FROM gmgn_minute WHERE minute < ?", (stamp - MINUTE_KEEP,))
            self._conn.execute(
                "DELETE FROM gmgn_minute_stat WHERE minute < ?", (stamp - MINUTE_KEEP,)
            )
            self._conn.commit()

    # ---- 数据管理 ----

    def summary(self) -> dict[str, Any]:
        with self._lock:
            span = self._conn.execute(
                "SELECT MIN(minute), MAX(minute), COUNT(*) FROM gmgn_minute"
            ).fetchone()
            raw = self._conn.execute("SELECT MIN(at), COUNT(*) FROM gmgn_events").fetchone()
            months = self._conn.execute(
                """
                SELECT strftime('%Y-%m', minute, 'unixepoch', 'localtime') AS month,
                       SUM(CASE WHEN outcome != 'skipped' THEN calls ELSE 0 END) AS calls,
                       SUM(weight) AS weight, COUNT(*) AS rows
                FROM gmgn_minute GROUP BY month ORDER BY month DESC
                """
            ).fetchall()
        return {
            "oldest": span[0],
            "newest": span[1],
            "minuteRows": int(span[2] or 0),
            "rawOldest": raw[0],
            "rawRows": int(raw[1] or 0),
            "months": [
                {
                    "month": m["month"],
                    "calls": int(m["calls"] or 0),
                    "weight": int(m["weight"] or 0),
                    "rows": int(m["rows"] or 0),
                }
                for m in months
            ],
            "keepDays": MINUTE_KEEP // 86400,
            "rawKeepDays": RAW_KEEP // 86400,
            "bytes": file_bytes(self.path),
        }

    def purge(self, start: float, end: float, dry_run: bool = True) -> dict[str, int]:
        """删除 [start, end) 内的计量（明细与分钟汇总）。dry_run 只数不删。"""
        with self._lock:
            counts = {
                "rawRows": int(
                    self._conn.execute(
                        "SELECT COUNT(*) FROM gmgn_events WHERE at >= ? AND at < ?", (start, end)
                    ).fetchone()[0]
                ),
                "minuteRows": int(
                    self._conn.execute(
                        "SELECT COUNT(*) FROM gmgn_minute WHERE minute >= ? AND minute < ?",
                        (start, end),
                    ).fetchone()[0]
                ),
            }
            if not dry_run:
                self._conn.execute("DELETE FROM gmgn_events WHERE at >= ? AND at < ?", (start, end))
                for table in ("gmgn_minute", "gmgn_minute_stat"):
                    self._conn.execute(
                        f"DELETE FROM {table} WHERE minute >= ? AND minute < ?", (start, end)
                    )
                self._conn.commit()
        return counts

    def vacuum(self) -> int:
        with self._lock:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._conn.execute("VACUUM")
        return file_bytes(self.path)

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _kind(e: Event) -> str:
    return "manual" if e.manual else ("background" if e.background else "realtime")


def file_bytes(path: Path) -> int:
    total = 0
    for item in (path, Path(f"{path}-wal")):
        try:
            total += item.stat().st_size
        except OSError:
            continue
    return total
