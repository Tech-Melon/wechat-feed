from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from wechat_feed.config import SettingsStore, normalize_settings, validate_upstream_base
from wechat_feed.normalize import extract_groups, normalize_message_event
from wechat_feed.store import CardStore
from wechat_feed.tokens import (
    candidate_chains,
    chain_from_search,
    extract_cas,
    format_mcap,
    format_tax,
    gmgn_api_chain,
    gmgn_token_url,
)
from wechat_feed.upstream import _allowed, _stream_urls, parse_sse_block
from wechat_feed.wechatdb import decode_content, parse_row, room_nicknames, strip_sender, table_name

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("WECHAT_FEED_HOME", str(tmp_path))
    monkeypatch.setenv("WECHAT_FEED_ROOT", str(ROOT))
    monkeypatch.setenv("WECHAT_FEED_DISABLE_UPSTREAM", "1")
    monkeypatch.setenv("WECHAT_FEED_DISABLE_TOKEN", "1")
    monkeypatch.setenv("WECHAT_FEED_DISABLE_DIRECT", "1")
    from wechat_feed.server import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


def _event(username: str = "12345678@chatroom", **message: object) -> dict:
    payload = {
        "type": "message",
        "username": username,
        "conversationName": "信号群",
        "message": {
            "id": "msg-1",
            "localId": 7,
            "createTime": 1_700_000_000,
            "senderUsername": "wxid_alpha",
            "senderDisplayName": "蓝点网",
            "isSent": False,
            "renderType": "text",
            "content": "看一下 0x" + "ab" * 20 + " 和 $BTC",
        },
    }
    payload["message"].update(message)
    return payload


def test_private_and_system_messages_are_dropped() -> None:
    assert normalize_message_event(_event("wxid_friend")) is None
    assert normalize_message_event(_event(renderType="system", content="邀请加入了群聊")) is None


def test_group_message_keeps_address_quote_and_name() -> None:
    card = normalize_message_event(
        _event(
            renderType="quote",
            quoteTitle="群友",
            quoteUsername="wxid_quoted",
            quoteContent="按键加原始输入法",
        )
    )
    assert card is not None
    assert card["conversationId"] == "12345678@chatroom"
    assert card["senderName"] == "蓝点网"
    assert card["quoteName"] == "群友"
    assert card["quoteUsername"] == "wxid_quoted"
    assert card["quoteText"] == "按键加原始输入法"
    assert len(card["addresses"]) == 1
    assert card["tickers"] == ["$BTC"]
    assert card["cas"]
    assert card["demo"] is False


def test_xml_blob_is_not_shown_raw() -> None:
    card = normalize_message_event(_event(renderType="image", content="<msg><img /></msg>"))
    assert card is not None
    assert card["text"] == "[图片]"


def test_sse_block_and_stream_target() -> None:
    assert parse_sse_block('id: 1\ndata: {"type":"ready"}\n') == {"type": "ready"}
    assert parse_sse_block(": ping") is None
    urls = _stream_urls(
        {
            "upstream_base": "http://127.0.0.1:10392",
            "groups": [{"id": "12345678@chatroom", "name": ""}],
        }
    )
    assert len(urls) == 1
    assert "interval_ms=300" in urls[0]
    assert "username=" not in urls[0]
    all_groups = _stream_urls({"upstream_base": "http://127.0.0.1:10392", "groups": []})
    assert urls == all_groups


def test_extract_groups_ignores_private_chats() -> None:
    groups = extract_groups(
        {
            "sessions": [
                {"username": "wxid_me", "displayName": "私聊"},
                {"userName": "999@chatroom", "nickName": "Alpha"},
            ]
        }
    )
    assert groups == [{"id": "999@chatroom", "name": "Alpha"}]


def test_upstream_must_stay_on_loopback() -> None:
    assert validate_upstream_base("http://localhost:10392/") == "http://localhost:10392"
    with pytest.raises(ValueError):
        validate_upstream_base("https://127.0.0.1:10392")
    with pytest.raises(ValueError):
        validate_upstream_base("http://10.0.0.8:10392")
    with pytest.raises(ValueError):
        normalize_settings({"gmgn_overlay": "yes"})


def test_api_defaults_and_two_paths(client: TestClient) -> None:
    settings = client.get("/api/settings")
    assert settings.status_code == 200
    assert settings.json()["gmgn_overlay"] is False
    assert settings.json()["float_panel"] is False
    assert settings.json()["ca_only"] is False
    assert settings.json()["gmgn_api_key"] == ""
    assert settings.json()["starred_users"] == []
    assert settings.json()["star_colors"] == {}
    assert settings.json()["blocked_groups"] == []
    assert settings.json()["groups"] == []

    page = client.get("/")
    assert page.status_code == 200
    assert "插入 gmgn 推特信息流" in page.text
    assert "只看 CA" in page.text
    assert 'id="gmgn"' in page.text
    assert "checked" not in page.text
    assert settings.json()["has_gmgn_key"] is False

    rejected = client.put("/api/settings", json={"upstream_base": "http://example.com:10392"})
    assert rejected.status_code == 400

    enabled = client.put("/api/settings", json={"gmgn_overlay": True})
    assert enabled.json()["gmgn_overlay"] is True
    assert client.get("/api/settings").json()["gmgn_overlay"] is True

    demo = client.post("/api/demo")
    assert demo.status_code == 200
    card = demo.json()["card"]
    assert card["demo"] is True
    assert card["quoteUsername"] == "wxid_quoted"
    assert card["quoteAvatarUrl"]
    assert card["addresses"]
    listed = client.get("/api/messages").json()
    assert listed["messages"][0]["id"] == card["id"]
    assert "has_more" in listed

    assert client.delete("/api/messages").json()["ok"] is True
    assert client.get("/api/messages").json()["messages"] == []

    saved_key = client.put("/api/settings", json={"gmgn_api_key": "gmgn_abc12345"}).json()
    assert saved_key["has_gmgn_key"] is True
    assert saved_key["gmgn_api_key"] == ""
    loaded_key = client.get("/api/settings").json()
    assert loaded_key["has_gmgn_key"] is True
    assert loaded_key["gmgn_api_key"] == ""


def test_extract_cas_follows_flap_rules() -> None:
    flap = "0x98815a15f314395f3dc4a20abe845c668df77777"
    hits = extract_cas(f"看这个 {flap}")
    assert hits[0]["address"] == flap.lower()
    assert hits[0]["resolved"] is False
    url_hits = extract_cas(f"https://gmgn.ai/base/token/{flap}")
    assert url_hits[0]["chain"] == "base"
    assert url_hits[0]["resolved"] is True
    pump = extract_cas("9jXsou9R8LxH64Tv5636sJDtFG1kGPBpvennW2g5pump")
    assert pump[0]["chain"] == "sol"
    tron = extract_cas("看这个 TQn9Y2khEsLJW1ChVWFMSMeRDow5KcbLSE")
    assert tron[0]["kind"] == "tron"
    assert tron[0]["chain"] == "tron"
    sui = extract_cas("0x" + "ab" * 32)
    assert sui[0]["kind"] == "sui"
    sol = extract_cas("看这个 7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU")
    assert sol[0]["kind"] == "sol"
    assert sol[0]["chain"] == "sol"
    tron_url = extract_cas("https://tronscan.org/#/token20/TQn9Y2khEsLJW1ChVWFMSMeRDow5KcbLSE")
    assert tron_url[0]["chain"] == "tron"
    ton = extract_cas("EQ" + "A" * 46)
    assert ton[0]["kind"] == "ton"
    assert format_mcap(12_300) == "1.2万"
    assert format_tax(0.02) == "2%"
    assert format_tax(2) == "2%"
    assert "bsc/token" in gmgn_token_url("bsc", flap)


def test_candidate_chains_prefers_dex_then_kind() -> None:
    evm = "0x" + "ab" * 20
    assert candidate_chains(evm, resolved=False)[:4] == ["bsc", "base", "eth", "sol"]
    assert candidate_chains(evm, "eth", resolved=True) == ["eth"]
    assert candidate_chains(evm, dex_chain="base", resolved=False)[0] == "base"
    assert gmgn_api_chain("arb") == "arbitrum"
    assert gmgn_api_chain("ethereum") == "eth"
    sol = "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"
    assert candidate_chains(sol) == ["sol"]
    found = chain_from_search(
        {"data": {"coins": [{"address": evm, "chain": "eth", "symbol": "X"}]}},
        evm,
    )
    assert found == "eth"
    unwrapped = chain_from_search(
        {"coins": [{"address": evm, "chain": "base", "symbol": "X"}], "wallets": []},
        evm,
    )
    assert unwrapped == "base"


def test_card_store_round_trip(tmp_path: Path) -> None:
    store = CardStore(tmp_path / "feed.db")
    store.save(
        {
            "id": "a1",
            "conversationId": "12345678@chatroom",
            "createTime": int(__import__("time").time()),
            "receivedAt": 1,
            "text": "hello",
        }
    )
    rows = store.recent(limit=10)
    assert len(rows) == 1
    assert rows[0]["text"] == "hello"
    store.clear()
    assert store.recent() == []


def test_blocked_group_is_filtered() -> None:
    settings = {
        "groups": [],
        "blocked_groups": ["12345678@chatroom"],
    }
    assert _allowed(settings, "12345678@chatroom") is False
    assert _allowed(settings, "99999999@chatroom") is True


def test_settings_round_trip(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    saved = store.replace(
        {"groups": [{"id": "12345678@chatroom", "name": "信号群"}], "gmgn_overlay": False}
    )
    assert saved["gmgn_overlay"] is False
    again = SettingsStore(tmp_path / "settings.json").get()
    assert again["groups"][0]["name"] == "信号群"
    blocked = store.replace(
        {
            "blocked_groups": ["12345678@chatroom"],
            "ca_only": True,
            "gmgn_api_key": "gmgn_abc12345",
        }
    )
    assert blocked["blocked_groups"] == ["12345678@chatroom"]
    assert blocked["ca_only"] is True
    assert blocked["gmgn_api_key"] == "gmgn_abc12345"
    starred = store.replace({"starred_users": ["wxid_alpha"], "float_panel": True})
    assert starred["starred_users"] == ["wxid_alpha"]
    colored = store.replace(
        {"starred_users": ["wxid_alpha"], "star_colors": {"wxid_alpha": "rose"}}
    )
    assert colored["star_colors"] == {"wxid_alpha": "rose"}
    assert starred["float_panel"] is True


def test_messages_are_paginated(client: TestClient) -> None:
    persist = client.app.state.persist
    now = 1_800_000_000
    for index in range(35):
        persist.save(
            {
                "id": f"page-{index}",
                "conversationId": "12345678@chatroom",
                "createTime": now - index,
                "receivedAt": now * 1000 - index,
                "text": f"hello {index}",
                "addresses": [],
                "cas": [],
            }
        )
    first = client.get("/api/messages?limit=30").json()
    assert len(first["messages"]) == 30
    assert first["has_more"] is True
    assert first["messages"][0]["id"] == "page-0"
    last = first["messages"][-1]
    second = client.get(
        "/api/messages",
        params={
            "limit": 30,
            "before": last["createTime"],
            "before_received": last["receivedAt"],
        },
    ).json()
    assert len(second["messages"]) == 5
    assert second["has_more"] is False
    assert second["messages"][-1]["id"] == "page-34"


def test_web_pages_cannot_read_messages_cross_origin(client: TestClient) -> None:
    site = client.get("/api/messages", headers={"Origin": "https://gmgn.ai"})
    assert "access-control-allow-origin" not in site.headers
    extension = "chrome-extension://" + "a" * 32
    allowed = client.get("/api/messages", headers={"Origin": extension})
    assert allowed.headers["access-control-allow-origin"] == extension


def test_card_store_latest_time(tmp_path: Path) -> None:
    store = CardStore(tmp_path / "feed.db")
    now = int(__import__("time").time())
    for index, created in enumerate([now - 60, now - 5, now - 30]):
        store.save(
            {
                "id": f"t{index}",
                "conversationId": "12345678@chatroom",
                "createTime": created,
                "receivedAt": created * 1000,
            }
        )
    assert store.latest_time("12345678@chatroom") == now - 5
    assert store.latest_time("99999999@chatroom") == 0


def test_ca_card_includes_sender_neighbors(tmp_path: Path) -> None:
    store = CardStore(tmp_path / "feed.db")
    now = 1_800_000_100
    for index, text in enumerate(
        ["前文1", "前文2", "前文3", "看这个合约", "后文1", "后文2", "后文3"]
    ):
        store.save(
            {
                "id": f"n{index}",
                "conversationId": "12345678@chatroom",
                "senderUsername": "wxid_alpha",
                "createTime": now + index,
                "receivedAt": now * 1000 + index,
                "text": text,
                "addresses": ["0x" + "ab" * 20] if index == 3 else [],
                "cas": [{"address": "0x" + "ab" * 20}] if index == 3 else [],
            }
        )
    middle = store.recent(limit=10)[3]
    before, after = store.around_sender(middle)
    assert [item["text"] for item in before] == ["前文1", "前文2", "前文3"]
    assert [item["text"] for item in after] == ["后文1", "后文2", "后文3"]
    filled = store.fill_context(dict(middle))
    assert len(filled["contextBefore"]) == 3
    assert len(filled["contextAfter"]) == 3


def test_direct_text_strips_sender_prefix() -> None:
    assert strip_sender("wxid_abc123:\n冲不冲", "wxid_abc123") == "冲不冲"
    assert strip_sender("注意:\n别追高", "wxid_abc123") == "注意:\n别追高"
    assert strip_sender("wxid_abc123:\n冲", "") == "冲"
    assert strip_sender("注意:\n别追高", "") == "注意:\n别追高"
    row = parse_row(local_type=1, sender="wxid_abc123", content="wxid_abc123:\n看 0xabc")
    assert row == {"renderType": "text", "content": "看 0xabc"}


def test_direct_quote_and_skipped_types() -> None:
    xml = (
        'wxid_me01:\n<?xml version="1.0"?><msg><appmsg><title>射了射了</title><type>57</type>'
        "<refermsg><type>1</type><chatusr>wxid_other</chatusr><displayname>群友</displayname>"
        "<content>原话</content></refermsg></appmsg></msg>"
    )
    row = parse_row(local_type=(57 << 32) | 49, sender="wxid_me01", content=xml)
    assert row["renderType"] == "quote"
    assert row["content"] == "射了射了"
    assert row["quoteTitle"] == "群友"
    assert row["quoteContent"] == "原话"
    assert row["quoteUsername"] == "wxid_other"
    card = normalize_message_event(
        {"type": "message", "username": "12345678@chatroom", "message": {"id": "x", **row}}
    )
    assert card is not None and card["quoteText"] == "原话"
    voice = 'wxid_me01:\n<msg><voicemsg voicelength="9230" /></msg>'
    assert parse_row(local_type=34, sender="wxid_me01", content=voice)["content"] == "[语音 9秒]"
    record = (
        "<msg><appmsg><title>群聊的聊天记录</title><des>大白:[图片]&#x0D;&#x0A;大白:冲</des>"
        "<type>19</type></appmsg></msg>"
    )
    row = parse_row(local_type=(19 << 32) | 49, sender="", content=record)
    assert row["renderType"] == "chatHistory"
    assert row["content"] == "大白:[图片]\n大白:冲"
    assert parse_row(local_type=10000, sender="", content="<sysmsg/>") is None
    pat = "<msg><appmsg><title>拍了拍</title><type>62</type></appmsg></msg>"
    assert parse_row(local_type=(62 << 32) | 49, sender="", content=pat) is None
    assert parse_row(local_type=3, sender="wxid_me01", content="wxid_me01:\n<msg/>") == {
        "renderType": "image",
        "content": "",
    }


def test_direct_room_nicknames_and_zstd() -> None:
    import zstandard

    def field(number: int, payload: bytes) -> bytes:
        return bytes([number << 3 | 2, len(payload)]) + payload

    member = field(1, b"wxid_abc123") + field(2, "史迪仔".encode())
    buffer = field(1, member) + bytes([3 << 3, 5])
    assert room_nicknames(buffer) == {"wxid_abc123": "史迪仔"}
    assert room_nicknames(b"\xff") == {}
    packed = zstandard.ZstdCompressor().compress("你好".encode())
    assert decode_content(packed) == "你好"
    assert table_name("12345678@chatroom").startswith("Msg_")


def test_gmgn_rate_window_respects_weight() -> None:
    import asyncio
    import time

    from wechat_feed.tokens import RateWindow

    async def run() -> tuple[float, float]:
        window = RateWindow(5, window=0.3)
        start = time.monotonic()
        for _ in range(5):
            await window.acquire(1)
        burst = time.monotonic() - start
        await window.acquire(5)
        return burst, time.monotonic() - start

    burst, total = asyncio.run(run())
    assert burst < 0.1
    assert total >= 0.29


def _v2_dat(plain: bytes, aes_key: bytes, xor_key: int, aes_size: int = 1024) -> bytes:
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    padder = padding.PKCS7(128).padder()
    head = padder.update(plain[:aes_size]) + padder.finalize()
    encryptor = Cipher(algorithms.AES(aes_key), modes.ECB()).encryptor()
    head = encryptor.update(head) + encryptor.finalize()
    tail = bytes(value ^ xor_key for value in plain[aes_size:])
    sizes = aes_size.to_bytes(4, "little") + len(tail).to_bytes(4, "little")
    header = b"\x07\x08V2\x08\x07" + sizes + b"\x01"
    return header + head + tail


def test_decode_v2_image_and_skip_wxgf(tmp_path: Path) -> None:
    from wechat_feed.wximage import decode_dat, load_image

    key = b"0123456789abcdef"
    jpeg = b"\xff\xd8\xff\xe0" + bytes(range(256)) * 12
    assert decode_dat(_v2_dat(jpeg, key, 0x5A), key, 0x5A) == jpeg

    room, name = "a" * 32, "b" * 32
    folder = tmp_path / "msg" / "attach" / room / "2026-10" / "Img"
    folder.mkdir(parents=True)
    (folder / f"{name}.dat").write_bytes(_v2_dat(b"wxgf" + bytes(2000), key, 0x5A))
    (folder / f"{name}_t.dat").write_bytes(_v2_dat(jpeg, key, 0x5A))
    ref = f"{room}/2026-10/{name}"
    assert load_image(tmp_path, ref, key, 0x5A, hd=True) == (jpeg, "image/jpeg")
    assert load_image(tmp_path, "../etc/passwd", key, 0x5A) is None


def test_image_message_carries_file_reference() -> None:
    from wechat_feed.wechatdb import image_file_md5

    name = "8d2a69a312219787902e2cbd8e5641b0"
    inner = b"\x22" + bytes([len(name)]) + name.encode()
    packed = b"\x08\x07\x10\x02\x1a" + bytes([len(inner)]) + inner + b"X\x00"
    assert image_file_md5(packed) == name
    assert image_file_md5(b"") == ""

    card = normalize_message_event(
        _event(renderType="image", content="", wxImage=f"{'c' * 32}/2026-10/{name}")
    )
    assert card["text"] == "[图片]"
    assert card["mediaUrl"].startswith("/api/wximg?ref=")
    assert card["mediaFullUrl"].endswith("&hd=1")
