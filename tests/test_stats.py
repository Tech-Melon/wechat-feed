import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from wechat_feed.hub import Hub
from wechat_feed.store import KEEP_SECONDS, CardStore

ROOT = Path(__file__).resolve().parents[1]
CA = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"


def _card(card_id: str, conv: str, sender: str, at: int, **extra: object) -> dict:
    card = {
        "id": card_id,
        "conversationId": conv,
        "conversationName": f"群{conv[:4]}",
        "senderUsername": sender,
        "senderName": f"人{sender[-2:]}",
        "createTime": at,
        "receivedAt": at * 1000,
        "text": "hi",
        "addresses": [],
        "cas": [],
    }
    card.update(extra)
    return card


def test_retention_is_half_year() -> None:
    assert KEEP_SECONDS == 180 * 24 * 3600


def test_stats_count_new_inserts_only(tmp_path: Path) -> None:
    store = CardStore(tmp_path / "feed.db")
    now = int(time.time())
    assert store.save(_card("a", "11111111@chatroom", "wxid_a", now), replace=False)
    assert not store.save(_card("a", "11111111@chatroom", "wxid_a", now), replace=False)
    store.save(_card("b", "11111111@chatroom", "wxid_b", now, addresses=[CA]), replace=False)
    store.save(_card("c", "telegram:-1", "telegram:7", now), replace=False)
    store.save(_card("d", "10001@chatroom", "wxid_demo", now, demo=True), replace=False)
    # replace=True 是修改已有卡片，不能再计一次。
    store.save(_card("b", "11111111@chatroom", "wxid_b", now, addresses=[CA]))
    store.flush()
    out = store.stats.query(window="7d")
    assert out["totals"] == {"messages": 3, "ca": 1, "groups": 2, "senders": 3}
    assert sum(out["hours"]) == 3
    assert out["daily"][-1]["messages"] == 3
    names = {row["id"]: row["name"] for row in out["senders"]}
    assert names["telegram:7"] == "人:7"
    assert store.stats.query(window="7d", source="wechat")["totals"]["messages"] == 2
    assert store.stats.query(window="7d", source="telegram")["totals"]["messages"] == 1
    assert store.stats.query(window="7d", sender="wxid_b")["totals"]["groups"] == 1
    sources = {row["source"]: row["messages"] for row in out["sources"]}
    assert sources == {"wechat": 2, "telegram": 1}


def test_stats_backfill_purge_and_prune(tmp_path: Path) -> None:
    path = tmp_path / "feed.db"
    now = int(time.time())
    store = CardStore(path)
    store.save(
        _card("old", "11111111@chatroom", "wxid_a", now - KEEP_SECONDS - 7200), replace=False
    )
    store.save(_card("mid", "11111111@chatroom", "wxid_a", now - 3 * 86400), replace=False)
    store.save(_card("new", "11111111@chatroom", "wxid_a", now), replace=False)
    store.flush()

    # 模拟升级前的库：去掉统计版本号，重新打开时应按原始消息补算。
    conn = sqlite3.connect(path)
    conn.execute("DELETE FROM meta")
    conn.execute("DELETE FROM msg_stats")
    conn.commit()
    conn.close()
    store = CardStore(path)
    assert store.stats.query(window="180d")["totals"]["messages"] == 2

    store.prune()
    assert store.count() == 2
    preview = store.purge(now - 4 * 86400, now - 2 * 86400, dry_run=True)
    assert preview == {"messages": 1, "openMessages": 0, "wechatMessages": 1}
    store.purge(now - 4 * 86400, now - 2 * 86400, dry_run=False)
    assert store.count() == 1
    assert store.stats.query(window="180d")["totals"]["messages"] == 1
    summary = store.data_summary()
    assert summary["messages"] == 1 and summary["keepDays"] == 180


def test_sender_column_migrates_old_db(tmp_path: Path) -> None:
    path = tmp_path / "feed.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE cards (id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, "
        "create_time INTEGER NOT NULL, received_at INTEGER NOT NULL, payload TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO cards VALUES ('x', '11111111@chatroom', ?, 0, "
        '\'{"senderUsername": "wxid_old", "tokenCards": []}\')',
        (int(time.time()),),
    )
    conn.commit()
    conn.close()
    store = CardStore(path)
    assert store.stats.query(window="7d")["senders"][0]["id"] == "wxid_old"
    plan = " ".join(
        str(row)
        for row in store._conn.execute(
            "EXPLAIN QUERY PLAN SELECT payload FROM cards WHERE conversation_id = ? "
            "AND create_time >= ? AND sender = ? AND (create_time, received_at, id) < (?, ?, ?) "
            "ORDER BY create_time DESC, received_at DESC, id DESC LIMIT 3",
            ("11111111@chatroom", 0, "wxid_old", 2**40, 0, "z"),
        )
    )
    assert "idx_cards_conv_sender_cursor" in plan


def test_hub_does_not_rebroadcast_stored_cards(tmp_path: Path) -> None:
    store = CardStore(tmp_path / "feed.db")
    card = _card("k", "11111111@chatroom", "wxid_a", int(time.time()))
    assert Hub(persist=store).publish(dict(card))
    # 重启后内存是空的，库里已有这张卡：不能再当新消息推一次。
    assert not Hub(persist=store).publish(dict(card))


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("WECHAT_FEED_HOME", str(tmp_path))
    monkeypatch.setenv("WECHAT_FEED_ROOT", str(ROOT))
    for flag in ("UPSTREAM", "TOKEN", "DIRECT", "OPEN"):
        monkeypatch.setenv(f"WECHAT_FEED_DISABLE_{flag}", "1")
    from wechat_feed.server import create_app

    app = create_app()
    with TestClient(app, base_url="http://127.0.0.1:8765") as test_client:
        yield test_client, app


def test_stats_and_data_api(client) -> None:
    http, app = client
    now = int(time.time())
    app.state.store.replace({"group_remarks": {"11111111@chatroom": "备注群"}})
    app.state.persist.save(_card("s1", "11111111@chatroom", "wxid_a", now), replace=False)
    app.state.persist.save(_card("s2", "qq:5", "qq:9", now, addresses=[CA]), replace=False)
    out = http.get("/api/stats", params={"range": "today"}).json()
    assert out["totals"]["messages"] == 2
    groups = {row["id"]: row for row in out["groups"]}
    assert groups["11111111@chatroom"]["name"] == "备注群"
    assert groups["qq:5"]["watched"] is False
    assert http.get("/api/stats", params={"source": "qq"}).json()["totals"]["ca"] == 1
    assert http.get("/api/stats", params={"source": "Bad!"}).status_code == 400
    assert http.get("/api/stats", params={"group": "nope"}).status_code == 400
    assert http.get("/stats").status_code == 200
    data = http.get("/api/data").json()
    assert data["messages"]["messages"] == 2
    body = {"target": "messages", "start": now - 60, "end": now + 60, "dryRun": True}
    preview = http.post("/api/data/purge", json=body).json()
    assert preview["messages"]["openMessages"] == 1
    assert "board" not in preview

    hub = app.state.hub
    assert hub.publish(_card("s3", "11111111@chatroom", "wxid_b", now))
    done = http.post("/api/data/purge", json={**body, "dryRun": False}).json()
    assert done["messages"]["messages"] == 3
    assert http.get("/api/stats", params={"range": "today"}).json()["totals"]["messages"] == 0
    # 内存里的卡片也去掉了：之后再 patch 不会把已删的消息写回库。
    hub.patch("s3", {"text": "edited"})
    assert app.state.persist.count() == 0


def test_names_skip_latest_empty_name(tmp_path: Path) -> None:
    path = tmp_path / "feed.db"
    store = CardStore(path)
    now = int(time.time())
    store.save(_card("n1", "11111111@chatroom", "wxid_z", now - 60), replace=False)
    store.save(_card("n2", "11111111@chatroom", "wxid_z", now, senderName=""), replace=False)
    store.flush()
    conn = sqlite3.connect(path)
    conn.execute("DELETE FROM meta")
    conn.commit()
    conn.close()
    store = CardStore(path)
    names = {row["id"]: row["name"] for row in store.stats.query(window="7d")["senders"]}
    assert names["wxid_z"] == "人_z"
