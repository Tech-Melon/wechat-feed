from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from wechat_feed.config import (
    SettingsStore,
    display_group_ids,
    group_senders_by_id,
    normalize_settings,
    validate_upstream_base,
)
from wechat_feed.hub import Hub
from wechat_feed.normalize import extract_groups, normalize_message_event
from wechat_feed.store import CardStore
from wechat_feed.tokens import (
    _from_pair,
    allowed_chainicon_url,
    candidate_chains,
    chain_from_search,
    extract_cas,
    format_mcap,
    format_tax,
    gmgn_api_chain,
    gmgn_token_url,
    parse_search,
)
from wechat_feed.upstream import _allowed, _stream_urls, parse_sse_block
from wechat_feed.wechatdb import decode_content, parse_row, room_nicknames, strip_sender, table_name
from wechat_feed.wxemoji import allowed_emote_url, allowed_sticker_url

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


def test_public_docs_cover_wda_and_unpacked_extension() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "[MIT](LICENSE)" in readme
    assert "不上架" in readme
    assert "加载已解压的扩展程序" in readme
    assert "WeChatDataAnalysis" in readme
    assert "data/direct.json" in readme
    assert (ROOT / "LICENSE").is_file()
    guide = (ROOT / "docs" / "使用指南.md").read_text(encoding="utf-8")
    assert "不在监控群的不入库" in guide
    assert "只过滤显示" in guide
    assert "补 search" in guide


def test_api_defaults_and_two_paths(client: TestClient) -> None:
    settings = client.get("/api/settings")
    assert settings.status_code == 200
    assert settings.json()["gmgn_overlay"] is False
    assert settings.json()["float_panel"] is False
    assert settings.json()["ca_only"] is False
    assert settings.json()["gmgn_ca_only"] is False
    assert settings.json()["float_ca_only"] is False
    assert settings.json()["all_feed_groups"] == []
    assert settings.json()["ca_feed_groups"] == []
    assert settings.json()["gmgn_api_key"] == ""
    assert settings.json()["starred_users"] == []
    assert settings.json()["star_colors"] == {}
    assert settings.json()["group_users"] == {}
    assert settings.json()["blocked_groups"] == []
    assert settings.json()["blocked_users"] == []
    assert settings.json()["groups"] == []
    assert settings.json()["mcap_format"] == "cn"
    patched = client.put("/api/settings", json={"mcap_format": "en"})
    assert patched.status_code == 200
    assert patched.json()["mcap_format"] == "en"
    assert normalize_settings({"mcap_format": "en"})["mcap_format"] == "en"
    with pytest.raises(ValueError):
        normalize_settings({"mcap_format": "usd"})

    page = client.get("/")
    assert page.status_code == 200
    assert "技术瓜群流" in page.text
    assert "/assets/favicon.svg" in page.text
    assert "插入 gmgn 推特信息流" in page.text
    assert "只看 CA" in page.text
    assert "群聊选择管理" in page.text
    assert "监听全员" in page.text
    assert 'data-surface="gmgn"' in page.text
    assert 'data-surface="float"' in page.text
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
    pair = _from_pair(
        {
            "chainId": "bsc",
            "baseToken": {
                "address": "0x" + "ab" * 20,
                "symbol": "Puffs",
                "name": "Gemini Puffs",
            },
            "quoteToken": {},
            "marketCap": 7020,
            "liquidity": {"usd": 10012.09},
            "info": {},
            "pairCreatedAt": 1_791_955_000_000,
        },
        "0x" + "ab" * 20,
        "bsc",
    )
    assert pair["logo"] == ""
    assert pair["liquidityText"] == "1.0万"
    assert pair["createdAt"] == 1_791_955_000
    assert format_tax(0.02) == "2%"
    assert format_tax(2) == "2%"
    assert "bsc/token" in gmgn_token_url("bsc", flap)


def test_chain_favicon_uses_google_site_icon() -> None:
    from wechat_feed.tokens import chain_favicon

    url, name = chain_favicon("bsc")
    assert name == "bsc.png"
    assert "google.com/s2/favicons" in url
    assert "bnbchain.org" in url
    assert chain_favicon("binance")[1] == "bsc.png"
    assert chain_favicon("solana")[1] == "sol.png"
    assert chain_favicon("不是链") is None


def test_candidate_chains_prefers_dex_then_kind() -> None:
    evm = "0x" + "ab" * 20
    assert candidate_chains(evm, resolved=False) == ["bsc", "eth", "robinhood"]
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
    decoy = "0x" + "cd" * 20
    coins, wallets = parse_search(
        {
            "coins": [{"address": decoy, "chain": "base", "symbol": "FAKE"}],
            "wallets": [{"address": evm, "chain": "eth", "ens": "vitalik.eth"}],
        },
        evm,
    )
    assert coins == []
    assert wallets[0]["chain"] == "eth"
    assert chain_from_search({"coins": [{"address": decoy, "chain": "base"}]}, evm) == ""


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


def test_display_group_filters_are_independent() -> None:
    room_a = "111111111111@chatroom"
    room_b = "222222222222@chatroom"
    settings = normalize_settings(
        {
            "all_feed_groups": [room_a],
            "ca_feed_groups": [room_b],
        }
    )
    assert display_group_ids(settings, ca_only=False) == [room_a]
    assert display_group_ids(settings, ca_only=True) == [room_b]
    assert display_group_ids(normalize_settings({}), ca_only=False) == []
    assert display_group_ids(normalize_settings({}), ca_only=True) == []
    split = normalize_settings({"ca_only": True})
    assert split["ca_only"] is True
    assert split["gmgn_ca_only"] is True
    assert split["float_ca_only"] is True
    independent = normalize_settings(
        {"ca_only": True, "gmgn_ca_only": True, "float_ca_only": False}
    )
    assert independent["gmgn_ca_only"] is True
    assert independent["float_ca_only"] is False


def test_page_respects_allowed_groups(tmp_path: Path) -> None:
    store = CardStore(tmp_path / "feed.db")
    now = int(__import__("time").time())
    room_a = "111111111111@chatroom"
    room_b = "222222222222@chatroom"
    store.save(
        {
            "id": "a",
            "conversationId": room_a,
            "createTime": now,
            "receivedAt": 2,
            "text": "all-mode",
        }
    )
    store.save(
        {
            "id": "b",
            "conversationId": room_b,
            "createTime": now,
            "receivedAt": 1,
            "text": "ca-mode",
            "addresses": ["0xabc"],
        }
    )
    only_a, _ = store.page(limit=10, allowed=[room_a])
    assert [item["id"] for item in only_a] == ["a"]
    only_b, _ = store.page(limit=10, allowed=[room_b], ca_only=True)
    assert [item["id"] for item in only_b] == ["b"]
    both, _ = store.page(limit=10, allowed=[])
    assert {item["id"] for item in both} == {"a", "b"}


def test_messages_api_respects_feed_groups(client: TestClient) -> None:
    room_a = "111111111111@chatroom"
    room_b = "222222222222@chatroom"
    saved = client.put(
        "/api/settings",
        json={"all_feed_groups": [room_a], "ca_feed_groups": [room_b]},
    )
    assert saved.status_code == 200
    persist = client.app.state.persist
    now = 1_800_000_000
    persist.save(
        {
            "id": "all-mode",
            "conversationId": room_a,
            "createTime": now,
            "receivedAt": 2,
            "text": "all",
            "addresses": [],
            "cas": [],
        }
    )
    persist.save(
        {
            "id": "ca-mode",
            "conversationId": room_b,
            "createTime": now,
            "receivedAt": 1,
            "text": "ca",
            "addresses": ["0xabc"],
            "cas": [{"address": "0xabc"}],
        }
    )
    first = client.get("/api/messages?limit=10").json()["messages"]
    assert [item["id"] for item in first] == ["all-mode"]
    only_ca = client.get("/api/messages?limit=10&ca_only=true").json()["messages"]
    assert [item["id"] for item in only_ca] == ["ca-mode"]
    mixed = client.get("/api/messages?limit=10&include_all_groups=true").json()["messages"]
    assert {item["id"] for item in mixed} == {"all-mode", "ca-mode"}


def test_remote_image_hosts_are_allowlisted() -> None:
    assert allowed_sticker_url("https://emoji.qpic.cn/sticker.png")
    assert allowed_sticker_url("https://szextshort.weixin.qq.com/x")
    assert not allowed_sticker_url("http://127.0.0.1:8765/secret")
    assert not allowed_sticker_url("https://evil.example/x")
    assert allowed_emote_url("https://cdn.jsdelivr.net/gh/YiJio/emoji-chinese@1.1.0/wx/wx-new/default/wl.png")
    assert allowed_emote_url("https://res.wx.qq.com/mpres/htmledition/images/icon/emotion/1.gif")
    assert not allowed_emote_url("http://res.wx.qq.com/x.gif")
    assert allowed_chainicon_url("https://www.google.com/s2/favicons?sz=64&domain=solana.com")
    assert allowed_chainicon_url("https://t2.gstatic.com/faviconV2")
    assert not allowed_chainicon_url("https://www.google.com/search")
    assert not allowed_chainicon_url("http://127.0.0.1:10392/x")


def test_ingest_keeps_blocked_and_user_filters() -> None:
    room = "12345678@chatroom"
    settings = {
        "groups": [{"id": room, "name": "信号群"}],
        "blocked_groups": [room],
        "group_users": {room: ["wxid_alpha"]},
    }
    assert _allowed(settings, room) is True
    assert _allowed(settings, "99999999@chatroom") is False
    open_all = {"groups": [], "blocked_groups": [room]}
    assert _allowed(open_all, room) is True
    assert _allowed(open_all, "wxid_friend") is False


def test_group_users_settings_and_page_filter(tmp_path: Path) -> None:
    room = "12345678@chatroom"
    saved = normalize_settings(
        {
            "groups": [{"id": room, "name": "信号群"}],
            "group_users": {room: ["wxid_alpha", "wxid_alpha", "wxid_beta"]},
        }
    )
    assert saved["group_users"][room] == ["wxid_alpha", "wxid_beta"]
    assert group_senders_by_id(saved)[room] == ["wxid_alpha", "wxid_beta"]
    empty_all = normalize_settings({})
    assert empty_all["group_users"] == {}
    with pytest.raises(ValueError):
        normalize_settings({"group_users": "nope"})

    store = CardStore(tmp_path / "feed.db")
    now = int(__import__("time").time())
    store.save(
        {
            "id": "keep",
            "conversationId": room,
            "senderUsername": "wxid_alpha",
            "createTime": now,
            "receivedAt": 2,
            "text": "keep",
        }
    )
    store.save(
        {
            "id": "drop",
            "conversationId": room,
            "senderUsername": "wxid_other",
            "createTime": now,
            "receivedAt": 1,
            "text": "drop",
        }
    )
    only, _ = store.page(limit=10, senders_by_group={room: ["wxid_alpha"]})
    assert [item["id"] for item in only] == ["keep"]
    nobody, _ = store.page(limit=10, senders_by_group={room: []})
    assert nobody == []
    hidden, _ = store.page(limit=10, blocked_senders=["wxid_other"])
    assert [item["id"] for item in hidden] == ["keep"]


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
    muted = store.replace({"blocked_users": ["wxid_spam", "wxid_spam"]})
    assert muted["blocked_users"] == ["wxid_spam"]
    users = store.replace({"group_users": {"12345678@chatroom": ["wxid_alpha"]}})
    assert users["group_users"] == {"12345678@chatroom": ["wxid_alpha"]}
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


def test_api_messages_filter_group_users(client: TestClient) -> None:
    room = "12345678@chatroom"
    saved = client.put(
        "/api/settings",
        json={"groups": [{"id": room, "name": ""}], "group_users": {room: ["wxid_alpha"]}},
    )
    assert saved.status_code == 200
    assert saved.json()["group_users"] == {room: ["wxid_alpha"]}
    persist = client.app.state.persist
    now = 1_800_000_000
    persist.save(
        {
            "id": "keep",
            "conversationId": room,
            "senderUsername": "wxid_alpha",
            "createTime": now,
            "receivedAt": 2,
            "text": "keep",
            "addresses": [],
            "cas": [],
        }
    )
    persist.save(
        {
            "id": "drop",
            "conversationId": room,
            "senderUsername": "wxid_other",
            "createTime": now,
            "receivedAt": 1,
            "text": "drop",
            "addresses": [],
            "cas": [],
        }
    )
    listed = client.get("/api/messages").json()["messages"]
    assert [item["id"] for item in listed] == ["keep"]


def test_api_messages_filter_blocked_users(client: TestClient) -> None:
    room = "12345678@chatroom"
    saved = client.put(
        "/api/settings",
        json={"groups": [{"id": room, "name": ""}], "blocked_users": ["wxid_spam"]},
    )
    assert saved.status_code == 200
    assert saved.json()["blocked_users"] == ["wxid_spam"]
    persist = client.app.state.persist
    now = 1_800_000_000
    persist.save(
        {
            "id": "keep",
            "conversationId": room,
            "senderUsername": "wxid_alpha",
            "createTime": now,
            "receivedAt": 2,
            "text": "keep",
            "addresses": [],
            "cas": [],
        }
    )
    persist.save(
        {
            "id": "drop",
            "conversationId": room,
            "senderUsername": "wxid_spam",
            "createTime": now,
            "receivedAt": 1,
            "text": "drop",
            "addresses": [],
            "cas": [],
        }
    )
    listed = client.get("/api/messages").json()["messages"]
    assert [item["id"] for item in listed] == ["keep"]


def test_messages_ca_only_follows_query_not_settings(client: TestClient) -> None:
    persist = client.app.state.persist
    persist.save(
        {
            "id": "plain",
            "conversationId": "12345678@chatroom",
            "createTime": 1_800_000_100,
            "receivedAt": 1,
            "text": "hello",
            "addresses": [],
            "cas": [],
        }
    )
    persist.save(
        {
            "id": "ca",
            "conversationId": "12345678@chatroom",
            "createTime": 1_800_000_101,
            "receivedAt": 2,
            "text": "token",
            "addresses": ["0xabc"],
            "cas": [{"address": "0xabc"}],
        }
    )
    client.put(
        "/api/settings",
        json={"ca_only": True, "gmgn_ca_only": True, "float_ca_only": False},
    )
    mixed = client.get("/api/messages?limit=10").json()["messages"]
    assert {item["id"] for item in mixed} == {"plain", "ca"}
    only = client.get("/api/messages?limit=10&ca_only=true").json()["messages"]
    assert [item["id"] for item in only] == ["ca"]


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


def test_around_sender_finds_user_across_busy_room(tmp_path: Path) -> None:
    store = CardStore(tmp_path / "feed.db")
    now = 1_800_000_400
    room = "12345678@chatroom"
    texts = ["前文1", "前文2", "前文3"]
    for index, text in enumerate(texts):
        store.save(
            {
                "id": f"me-{index}",
                "conversationId": room,
                "senderUsername": "wxid_alpha",
                "createTime": now + index,
                "receivedAt": now * 1000 + index,
                "text": text,
            }
        )
    for index in range(90):
        store.save(
            {
                "id": f"other-{index}",
                "conversationId": room,
                "senderUsername": "wxid_other",
                "createTime": now + 10 + index,
                "receivedAt": now * 1000 + 10 + index,
                "text": f"灌水{index}",
            }
        )
    store.save(
        {
            "id": "ca",
            "conversationId": room,
            "senderUsername": "wxid_alpha",
            "createTime": now + 200,
            "receivedAt": now * 1000 + 200,
            "text": "看这个合约",
            "addresses": ["0x" + "ab" * 20],
            "cas": [{"address": "0x" + "ab" * 20}],
        }
    )
    for index, text in enumerate(["后文1", "后文2", "后文3"]):
        store.save(
            {
                "id": f"after-{index}",
                "conversationId": room,
                "senderUsername": "wxid_alpha",
                "createTime": now + 300 + index,
                "receivedAt": now * 1000 + 300 + index,
                "text": text,
            }
        )
    ca = {
        "id": "ca",
        "conversationId": room,
        "senderUsername": "wxid_alpha",
        "createTime": now + 200,
        "receivedAt": now * 1000 + 200,
        "text": "看这个合约",
        "addresses": ["0x" + "ab" * 20],
        "cas": [{"address": "0x" + "ab" * 20}],
    }
    before, after = store.around_sender(ca)
    assert [item["text"] for item in before] == texts
    assert [item["text"] for item in after] == ["后文1", "后文2", "后文3"]


def test_later_messages_refresh_ca_context_after(tmp_path: Path) -> None:
    store = CardStore(tmp_path / "feed.db")
    hub = Hub(persist=store)
    now = 1_800_000_200
    hub.publish(
        {
            "id": "ca1",
            "conversationId": "12345678@chatroom",
            "senderUsername": "wxid_alpha",
            "createTime": now,
            "receivedAt": now * 1000,
            "text": "看这个合约",
            "addresses": ["0x" + "ab" * 20],
            "cas": [{"address": "0x" + "ab" * 20}],
        },
        notify=False,
    )
    assert hub.messages()[0].get("contextAfter") == []
    for index, text in enumerate(["后文1", "后文2", "后文3"], start=1):
        hub.publish(
            {
                "id": f"after{index}",
                "conversationId": "12345678@chatroom",
                "senderUsername": "wxid_alpha",
                "createTime": now + index,
                "receivedAt": now * 1000 + index,
                "text": text,
                "addresses": [],
                "cas": [],
            },
            notify=False,
        )
    cards = {item["id"]: item for item in hub.messages()}
    assert [item["text"] for item in cards["ca1"]["contextAfter"]] == ["后文1", "后文2", "后文3"]


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


def test_direct_share_link_keeps_url() -> None:
    xml = (
        'wxid_me01:\n<?xml version="1.0"?><msg><appmsg>'
        "<title>播客笔记</title><type>5</type>"
        "<url>https://mp.weixin.qq.com/s/abc</url>"
        "</appmsg></msg>"
    )
    row = parse_row(local_type=(5 << 32) | 49, sender="wxid_me01", content=xml)
    assert row["renderType"] == "link"
    assert row["title"] == "播客笔记"
    assert row["linkUrl"] == "https://mp.weixin.qq.com/s/abc"
    card = normalize_message_event(
        {"type": "message", "username": "12345678@chatroom", "message": {"id": "link1", **row}}
    )
    assert card is not None
    assert card["text"] == "播客笔记\nhttps://mp.weixin.qq.com/s/abc"
    assert card["linkUrl"] == "https://mp.weixin.qq.com/s/abc"
    quoted = (
        'wxid_me01:\n<?xml version="1.0"?><msg><appmsg><title>看看这个</title><type>57</type>'
        "<refermsg><type>49</type><chatusr>wxid_other</chatusr><displayname>群友</displayname>"
        "<content>&lt;msg&gt;&lt;appmsg&gt;&lt;title&gt;文章&lt;/title&gt;"
        "&lt;url&gt;https://x.com/a&lt;/url&gt;&lt;/appmsg&gt;&lt;/msg&gt;</content>"
        "</refermsg></appmsg></msg>"
    )
    qrow = parse_row(local_type=(57 << 32) | 49, sender="wxid_me01", content=quoted)
    assert qrow["quoteContent"] == "文章\nhttps://x.com/a"


def test_direct_emoji_sticker_and_system_emote() -> None:
    from wechat_feed.wxemoji import STICKER_MAX, allowed_sticker_url, emote_source, within_limit

    xml = (
        'wxid_me01:\n<msg><emoji fromusername="wxid_me01" type="2" '
        'md5="1edd258f8470085c593f3bdf52170a7b" len="227011" '
        'cdnurl="http://vweixinf.tc.qq.com/110/20401/stodownload'
        '?m=1edd258f8470085c593f3bdf52170a7b" '
        'thumburl="" /></msg>'
    )
    row = parse_row(local_type=47, sender="wxid_me01", content=xml)
    assert row["renderType"] == "emoji"
    assert row["wxEmoji"] == "1edd258f8470085c593f3bdf52170a7b"
    assert row["emojiUrl"].startswith("http://vweixinf.tc.qq.com/")
    card = normalize_message_event(
        {"type": "message", "username": "12345678@chatroom", "message": {"id": "e1", **row}}
    )
    assert card is not None
    assert card["text"] == "[表情]"
    assert card["mediaUrl"].startswith("/api/wxemoji?md5=")
    assert "u=" in card["mediaUrl"]
    assert allowed_sticker_url(row["emojiUrl"])
    assert allowed_sticker_url("https://szextshort.weixin.qq.com/stodownload?m=abc")
    assert not allowed_sticker_url("https://evil.example/x")
    assert STICKER_MAX >= 4_000_000
    assert within_limit(b"0" * 2_335_236, STICKER_MAX)
    assert not within_limit(b"0" * (STICKER_MAX + 1), STICKER_MAX)
    png = emote_source("捂脸")
    assert png is not None and png[1] == "wl.png"
    gif = emote_source("冷汗")
    assert gif is not None and gif[1] == "17.gif"
    assert emote_source("不是表情") is None


def test_direct_room_nicknames_and_zstd() -> None:
    import zstandard

    def field(number: int, payload: bytes) -> bytes:
        return bytes([number << 3 | 2, len(payload)]) + payload

    member = field(1, b"wxid_abc123") + field(2, "史迪仔".encode())
    silent = field(1, b"wxid_only")
    buffer = field(1, member) + field(1, silent) + bytes([3 << 3, 5])
    assert room_nicknames(buffer) == {"wxid_abc123": "史迪仔", "wxid_only": ""}
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
    assert load_image(tmp_path, ref, key, 0x5A, hd=True) == (jpeg, "image/jpeg", "t")
    assert load_image(tmp_path, "../etc/passwd", key, 0x5A) is None
    hd_jpeg = b"\xff\xd8\xff\xe0" + bytes(range(256)) * 40
    (folder / f"{name}_h.dat").write_bytes(_v2_dat(hd_jpeg, key, 0x5A))
    assert load_image(tmp_path, ref, key, 0x5A, hd=True) == (hd_jpeg, "image/jpeg", "h")
    assert load_image(tmp_path, ref, key, 0x5A, hd=False) == (jpeg, "image/jpeg", "t")


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


def test_package_omits_local_data_and_api_key() -> None:
    import importlib.util
    import json

    spec = importlib.util.spec_from_file_location("pack", ROOT / "scripts" / "package.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    files = module.files()
    picked = [path.relative_to(ROOT).as_posix() for path in files]
    assert all(not item.startswith("data/") for item in picked)
    assert "data/settings.json" not in picked
    assert not any(Path(item).name in module.PRIVATE for item in picked)
    settings_path = ROOT / "data" / "settings.json"
    if settings_path.is_file():
        payload = json.loads(settings_path.read_text(encoding="utf-8"))
        local_key = str(payload.get("gmgn_api_key") or "").strip()
        if local_key:
            hits = [path for path in files if local_key.encode("utf-8") in path.read_bytes()]
            assert hits == []
    page = (ROOT / "extension" / "popup.html").read_text(encoding="utf-8")
    assert "卡片水印" not in page
    assert 'id="watermark"' not in page
    assert "𝕏 @0xTechMelon" in page
    assert "关注 @0xTechMelon" not in page
    feed = (ROOT / "extension" / "feed.js").read_text(encoding="utf-8")
    assert 'DEFAULT_WATERMARK = "𝕏 @0xTechMelon"' in feed
    assert "关注 X:" not in feed
    assert "white-space:nowrap" in feed
    assert "min-width:118px" not in feed
    assert "function stackMeta" in feed
    assert "STACK_GAP_SEC" in feed
    assert "is-stack-" in feed
    assert "const NAME_MAX = 10" in feed
    assert "const GROUP_MAX = 8" in feed
    assert "function clipText" in feed
    assert "avatarHtml(card, 22)" in feed
    assert "avatarHtml(card, stacked ? 36 : undefined)" in feed
    assert "const EMOTE_SLUGS" in feed
    assert '"捂脸": "wl"' in feed
    assert "wf-emote" in feed
    assert "wf-emoji" in feed
    assert "/api/wxemote?n=" in feed
    assert "wf-avatar-slot" not in feed
    assert "max-height:280px" in feed
    content_js = (ROOT / "extension" / "content.js").read_text(encoding="utf-8")
    assert "img.dataset.wfFull" in content_js
    assert "shouldFillMore" in content_js
    assert "FILL_VISIBLE" in content_js
    assert "微信群" not in feed
    assert "wf-origin-inline" not in feed
    assert 'class="wf-badge"' not in feed
    assert "原群名" not in feed
    assert "原名 " not in feed
    assert "wf-copy-btn" in feed
    assert "wf-metrics" in feed
    assert "wf-ca-addr" in feed
    assert "function chainBadge" in feed
    assert "function restoreHref" in feed
    assert "function formatUsd" in feed
    assert "wf-mcap" in feed
    assert "data-mcap-format" in feed
    assert "wf-tag-ico" in feed
    background = (ROOT / "extension" / "background.js").read_text(encoding="utf-8")
    assert "include_all_groups" in background
    app_js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    assert "await reloadFeeds()" in app_js
    assert "wf-text-link" in feed
    assert "点點" in feed
    assert "hxxp" in feed
    assert "wf-chain-ico" in feed
    assert "/api/chainicon?n=" in feed
    assert "google.com/s2/favicons" not in feed
    assert "const ico =" not in feed
    assert 'label: "Solana"' in feed
    assert 'label: "BNB"' in feed
    assert 'label: "Robinhood"' in feed
    assert 'label: "X Layer"' in feed
    assert ".wf-chain.is-bsc" in feed
    assert 'img.classList.contains("wf-chain-ico")' in content_js
    background = (ROOT / "extension" / "background.js").read_text(encoding="utf-8")
    assert 'importScripts("feed.js")' in background
    assert "WechatFeed.cardInFeed(card, settings, false)" in background
    assert "WechatFeed.cardInFeed(card, settings, true)" in background
    assert "voice.normalEnabled && inAll" in background
    assert "groupListenUsers" in feed
    assert "group_users" in feed
    assert "blocked_users" in feed
    assert "data-mute-user" in feed
    assert "muteUser" in background
    popup = (ROOT / "extension" / "popup.html").read_text(encoding="utf-8")
    assert "只勾了「只看 CA」的群" in popup
    assert "监听全员" in popup
    assert "屏蔽某人" in popup
    assert "只听部分人" in popup


def test_members_requires_direct(client: TestClient) -> None:
    missing = client.get("/api/groups/not-a-room/members")
    assert missing.status_code == 400
    response = client.get("/api/groups/12345678@chatroom/members")
    assert response.status_code == 503
