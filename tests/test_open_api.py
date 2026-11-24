import asyncio
import json
import socket
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from wechat_feed.config import SettingsStore, normalize_settings, wechat_groups
from wechat_feed.hub import Hub
from wechat_feed.openapi import (
    KEEP_SECONDS,
    OpenIntake,
    OpenListener,
    Rejected,
    build_card,
    client_allowed,
    create_open_app,
    new_key,
    sign,
)
from wechat_feed.opendir import FetchDenied, OpenDirectory, fetch_image
from wechat_feed.store import CardStore
from wechat_feed.tokens import TokenIndex
from wechat_feed.upstream import _allowed

ROOT = Path(__file__).resolve().parents[1]
PATH = "/open/v1/messages"
CA = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"


def _message(**extra: object) -> dict:
    item = {
        "id": "8812",
        "chat": {"id": "-1001234567", "name": "Alpha 冲狗群"},
        "sender": {"id": "998877", "name": "Alice"},
        "time": int(time.time() * 1000),
        "text": "大家好",
    }
    item.update(extra)
    return item


def _signed(key: str, body: dict, *, stamp: int | None = None, path: str = PATH):
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
    ts = int(time.time()) if stamp is None else stamp
    headers = {
        "Content-Type": "application/json",
        "X-WF-Timestamp": str(ts),
        "X-WF-Signature": sign(key, ts, "POST", path, raw),
    }
    return raw, headers


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("WECHAT_FEED_HOME", str(tmp_path))
    monkeypatch.setenv("WECHAT_FEED_ROOT", str(ROOT))
    monkeypatch.setenv("WECHAT_FEED_DISABLE_UPSTREAM", "1")
    monkeypatch.setenv("WECHAT_FEED_DISABLE_TOKEN", "1")
    monkeypatch.setenv("WECHAT_FEED_DISABLE_DIRECT", "1")
    monkeypatch.setenv("WECHAT_FEED_DISABLE_OPEN", "1")
    from wechat_feed.server import create_app

    app = create_app()
    with TestClient(app, base_url="http://127.0.0.1:8765") as main:
        store: SettingsStore = app.state.store
        hub: Hub = app.state.hub
        key = new_key()
        store.replace({"open_api_enabled": True, "open_api_key": key})
        # 和主服务共用同一个目录，/api/open-api 能看到测试推送的群。
        directory: OpenDirectory = app.state.open_dir
        intake = OpenIntake(store, hub, TokenIndex(lambda: ""), directory)
        lan = TestClient(create_open_app(store, intake), client=("192.168.1.20", 50000))
        yield {"main": main, "lan": lan, "store": store, "intake": intake, "key": key}


def _drain(intake: OpenIntake) -> None:
    while not intake.queue.empty():
        intake._publish(intake.queue.get_nowait())


def test_build_card_maps_ids_platform_and_ca() -> None:
    card, row = build_card(
        "telegram",
        _message(
            text=f"看下 {CA} $PEPE",
            reply={"senderId": "556", "senderName": "Bob", "text": "上一条"},
            image="https://cdn.example.com/a.jpg",
        ),
    )
    assert card["id"] == "telegram:-1001234567:8812"
    assert card["conversationId"] == "telegram:-1001234567"
    assert card["senderUsername"] == "telegram:998877"
    assert card["quoteUsername"] == "telegram:556"
    assert card["platformName"] == "Telegram"
    assert card["addresses"] == [CA]
    assert card["tickers"] == ["$PEPE"]
    assert card["renderType"] == "image"
    assert card["mediaUrl"].startswith("/api/openimg?ref=")
    assert row["images"][0][1] == "https://cdn.example.com/a.jpg"


def test_build_card_time_rules_and_rejections() -> None:
    now = time.time()
    future, _ = build_card("qq", _message(time=int((now + 3600) * 1000)), now)
    assert future["createTime"] == int(now)
    seconds, _ = build_card("qq", _message(time=int(now) - 60), now)
    assert seconds["createTime"] == int(now) - 60
    with pytest.raises(Rejected):
        build_card("qq", _message(time=int(now) - KEEP_SECONDS - 10), now)
    with pytest.raises(Rejected):
        build_card("qq", _message(id="a:b"))
    with pytest.raises(Rejected):
        build_card("qq", _message(text="", type="text"))
    with pytest.raises(Rejected):
        build_card("qq", _message(image="file:///etc/passwd"))
    with pytest.raises(Rejected):
        build_card("qq", _message(type="html"))
    clean, _ = build_card("qq", _message(text="a\u202eb\x00c"))
    assert clean["text"] == "abc"


def test_client_allowed_defaults_to_private_networks() -> None:
    assert client_allowed("192.168.1.20", [])
    assert client_allowed("127.0.0.1", [])
    assert not client_allowed("8.8.8.8", [])
    assert client_allowed("8.8.8.8", ["8.8.8.0/24"])
    assert not client_allowed("192.168.1.20", ["10.0.0.0/8"])
    assert not client_allowed("testclient", [])


def test_open_settings_validation() -> None:
    assert normalize_settings({"open_api_port": 9000})["open_api_port"] == 9000
    with pytest.raises(ValueError):
        normalize_settings({"open_api_port": 8765})
    with pytest.raises(ValueError):
        normalize_settings({"open_api_host": "evil.example"})
    with pytest.raises(ValueError):
        normalize_settings({"open_api_key": "short"})
    cleaned = normalize_settings({"open_api_allow_ips": ["192.168.1.5", "10.0.0.0/8"]})
    assert cleaned["open_api_allow_ips"] == ["192.168.1.5/32", "10.0.0.0/8"]
    groups = normalize_settings(
        {"groups": ["12345678@chatroom", {"id": "telegram:-100123", "name": "TG"}]}
    )
    assert [g["id"] for g in wechat_groups(groups)] == ["12345678@chatroom"]
    # 只加了开放接口的群时，微信侧仍按「没配监控群 = 全部群」处理。
    only_open = normalize_settings({"groups": ["telegram:-100123"]})
    assert _allowed(only_open, "99999999@chatroom")


def test_open_push_auth_failures(env) -> None:
    lan: TestClient = env["lan"]
    body = {"platform": "telegram", "messages": [_message()]}
    raw = json.dumps(body).encode()
    plain = {"Content-Type": "application/json"}
    assert lan.post(PATH, content=raw, headers=plain).status_code == 401
    _raw, bad = _signed("wfo_" + "x" * 43, body)
    assert lan.post(PATH, content=raw, headers=bad).status_code == 401
    _raw, stale = _signed(env["key"], body, stamp=int(time.time()) - 900)
    assert lan.post(PATH, content=_raw, headers=stale).status_code == 401
    good_raw, good = _signed(env["key"], body)
    assert (
        lan.post(PATH, content=good_raw, headers={**good, "Origin": "https://x.com"}).status_code
        == 403
    )
    # 签名对，但请求体被改过。
    assert lan.post(PATH, content=good_raw + b" ", headers=good).status_code == 401
    assert lan.get("/api/settings").status_code == 404
    public = TestClient(lan.app, client=("8.8.8.8", 50000))
    assert public.post(PATH, content=good_raw, headers=good).status_code == 403
    env["store"].replace({"open_api_enabled": False})
    assert lan.post(PATH, content=good_raw, headers=good).status_code == 503


def test_open_push_flow(env) -> None:
    lan: TestClient = env["lan"]
    main: TestClient = env["main"]
    store: SettingsStore = env["store"]
    intake: OpenIntake = env["intake"]
    body = {"platform": "telegram", "messages": [_message()]}

    raw, headers = _signed(env["key"], body)
    out = lan.post(PATH, content=raw, headers=headers)
    assert out.status_code == 200, out.text
    assert out.json()["ignored"] == 1
    assert intake.queue.empty()
    assert any(chat["id"] == "telegram:-1001234567" for chat in intake.directory.chats())

    store.replace({"groups": [{"id": "telegram:-1001234567", "name": ""}]})
    later = {"platform": "telegram", "messages": [_message(id="8813"), _message()]}
    raw, headers = _signed(env["key"], later)
    out = lan.post(PATH, content=raw, headers=headers).json()
    assert out["accepted"] == 2
    _drain(intake)

    page = main.get("/api/messages", params={"limit": 10}).json()["messages"]
    ids = [card["id"] for card in page]
    assert "telegram:-1001234567:8812" in ids
    card = next(card for card in page if card["id"].endswith(":8812"))
    assert card["platform"] == "telegram"
    assert card["displayGroup"] == "Alpha 冲狗群"
    assert card["avatarUrl"] == "/api/avatar?username=telegram%3A998877"

    raw, headers = _signed(env["key"], body)
    again = lan.post(PATH, content=raw, headers=headers).json()
    assert again["duplicate"] == 1 and again["accepted"] == 0

    # 移出监控后，已入库的消息也不再出现。
    store.replace({"groups": []})
    page = main.get("/api/messages", params={"limit": 10}).json()["messages"]
    assert not any(card["id"].startswith("telegram:") for card in page)


def test_main_app_open_key_and_host_guard(env) -> None:
    main: TestClient = env["main"]
    settings = main.get("/api/settings").json()
    assert "open_api_key" not in settings and settings["has_open_key"] is True
    lan: TestClient = env["lan"]
    raw, headers = _signed(env["key"], {"platform": "telegram", "messages": [_message()]})
    lan.post(PATH, content=raw, headers=headers)
    chat = main.get("/api/open-api").json()["chats"][0]
    assert chat["messages"] == 1 and chat["lastSeen"] and chat["platformName"] == "Telegram"
    main.put("/api/settings", json={"open_api_key": "wfo_" + "y" * 43})
    assert env["store"].get()["open_api_key"] == env["key"]
    rotated = main.post("/api/open-api/key", json={"action": "rotate"}).json()
    assert rotated["key"].startswith("wfo_") and rotated["key"] != env["key"]
    assert env["store"].get()["open_api_key"] == rotated["key"]
    assert main.post("/api/open-api/key", content=b"{}").status_code == 415
    state = main.get("/api/open-api").json()
    assert state["hasKey"] and "key" not in state
    assert main.get("/api/health", headers={"Host": "evil.example"}).status_code == 400
    assert main.get("/api/avatar", params={"username": "telegram:1"}).status_code == 404


def test_fetch_image_blocks_loopback() -> None:
    with pytest.raises(FetchDenied):
        asyncio.run(fetch_image("http://127.0.0.1:8765/api/settings", 1024))
    with pytest.raises(FetchDenied):
        asyncio.run(fetch_image("ftp://example.com/a.png", 1024))


def test_listener_serves_signed_ping(tmp_path: Path) -> None:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    store = SettingsStore(tmp_path / "settings.json")
    key = new_key()
    store.replace({"open_api_enabled": True, "open_api_key": key, "open_api_port": port})
    hub = Hub(persist=CardStore(tmp_path / "feed.db"))
    directory = OpenDirectory(tmp_path / "open.db", tmp_path / "media")
    intake = OpenIntake(store, hub, TokenIndex(lambda: ""), directory)

    async def scenario() -> tuple[int, int]:
        listener = OpenListener(lambda: create_open_app(store, intake))
        settings = {**store.get(), "open_api_host": "127.0.0.1"}
        await listener.apply(settings)
        assert listener.state["listening"], listener.state
        ts = int(time.time())
        headers = {
            "X-WF-Timestamp": str(ts),
            "X-WF-Signature": sign(key, ts, "GET", "/open/v1/ping", b""),
        }
        async with httpx.AsyncClient(trust_env=False) as client:
            for _ in range(50):
                try:
                    ok = await client.get(f"http://127.0.0.1:{port}/open/v1/ping", headers=headers)
                    break
                except httpx.ConnectError:
                    await asyncio.sleep(0.05)
            bad = await client.get(f"http://127.0.0.1:{port}/open/v1/ping")
        await listener.close()
        return ok.status_code, bad.status_code

    assert asyncio.run(scenario()) == (200, 401)
    directory.close()


def test_bad_item_in_batch_does_not_poison_others(env) -> None:
    lan: TestClient = env["lan"]
    env["store"].replace({"groups": [{"id": "telegram:-1001234567", "name": ""}]})
    raw = (
        '{"platform":"telegram","messages":['
        + json.dumps(_message(id="9001"))
        + ',{"id":"9002","chat":{"id":"-1001234567"},"sender":{"id":"1"},"time":NaN,"text":"x"}]}'
    ).encode()
    ts = int(time.time())
    headers = {
        "Content-Type": "application/json",
        "X-WF-Timestamp": str(ts),
        "X-WF-Signature": sign(env["key"], ts, "POST", PATH, raw),
    }
    out = lan.post(PATH, content=raw, headers=headers)
    assert out.status_code == 200, out.text
    assert [r["status"] for r in out.json()["results"]] == ["accepted", "rejected"]


def test_malformed_signature_headers_are_401(env) -> None:
    lan: TestClient = env["lan"]
    raw = b'{"platform":"telegram","messages":[]}'
    now = str(int(time.time())).encode()
    # 非 ASCII 的数字和签名字符以前会让 int() / compare_digest 抛错变成 500。
    for stamp, sig in (("\u00b2".encode(), b"a" * 64), (now, ("\u00e9" * 64).encode())):
        headers = {
            b"Content-Type": b"application/json",
            b"X-WF-Timestamp": stamp,
            b"X-WF-Signature": sig,
        }
        assert lan.post(PATH, content=raw, headers=headers).status_code == 401


def test_auto_watch_adds_only_new_chats(env) -> None:
    lan: TestClient = env["lan"]
    store: SettingsStore = env["store"]
    intake: OpenIntake = env["intake"]
    raw, headers = _signed(env["key"], {"platform": "telegram", "messages": [_message()]})
    assert lan.post(PATH, content=raw, headers=headers).json()["ignored"] == 1

    store.replace({"open_auto_watch": ["telegram"]})
    fresh = _message(id="1", chat={"id": "-1009", "name": "新群"})
    body = {"platform": "telegram", "messages": [fresh, _message(id="2")]}
    raw, headers = _signed(env["key"], body)
    out = lan.post(PATH, content=raw, headers=headers).json()
    # 新群自动加入并入库；之前见过、没加监控的群保持不显示。
    assert [r["status"] for r in out["results"]] == ["accepted", "ignored"]
    assert [g["id"] for g in store.get()["groups"]] == ["telegram:-1009"]
    _drain(intake)

    other = {"platform": "qq", "messages": [_message(id="3", chat={"id": "777"})]}
    raw, headers = _signed(env["key"], other)
    assert lan.post(PATH, content=raw, headers=headers).json()["ignored"] == 1
    with pytest.raises(ValueError):
        normalize_settings({"open_auto_watch": ["Bad Name"]})


def test_forgotten_chat_is_not_auto_watched(env) -> None:
    lan: TestClient = env["lan"]
    store: SettingsStore = env["store"]
    intake: OpenIntake = env["intake"]
    raw, headers = _signed(env["key"], {"platform": "telegram", "messages": [_message()]})
    lan.post(PATH, content=raw, headers=headers)
    assert intake.directory.forget_chat("telegram:-1001234567")
    assert not intake.directory.chats()
    store.replace({"open_auto_watch": ["telegram"]})
    raw, headers = _signed(env["key"], {"platform": "telegram", "messages": [_message(id="5")]})
    assert lan.post(PATH, content=raw, headers=headers).json()["ignored"] == 1
    assert store.get()["groups"] == []
    assert [chat["id"] for chat in intake.directory.chats()] == ["telegram:-1001234567"]

    # 新群第一批消息全被拒收，也已记进目录：移除后不会再被当成新群加回来。
    bad = _message(id="6", chat={"id": "-2002"}, type="html")
    raw, headers = _signed(env["key"], {"platform": "telegram", "messages": [bad]})
    assert lan.post(PATH, content=raw, headers=headers).json()["rejected"] == 1
    assert "telegram:-2002" in intake.directory.known(["telegram:-2002"])
