import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from wechat_feed.castore import (
    DORMANT_AGE,
    PENDING_AGE,
    REFRESH_HOT,
    SETTLE_DELAY,
    WATERMARK_OVERLAP,
    CaStore,
    call_status,
    format_x,
    wilson_lower,
)
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
    _from_search,
    _search_covers_card,
    allowed_chainicon_url,
    ath_from_kline,
    candidate_chains,
    chain_from_search,
    extract_cas,
    format_mcap,
    format_tax,
    gmgn_api_chain,
    gmgn_token_url,
    kline_bars,
    kline_plan,
    kline_resolution,
    merge_bars,
    parse_search,
    peak_since,
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
    assert "不带链的 GMGN" in guide


def test_ca_board_api(client: TestClient) -> None:
    listed = client.get("/api/ca-board")
    assert listed.status_code == 200
    assert listed.json()["board"] == []
    assert listed.json()["enabled"] is True
    assert "tokens" in listed.json()["stats"]
    missing = client.get("/api/ca-board/user/wxid_nobody")
    assert missing.status_code == 200
    assert missing.json()["calls"] == []
    bad = client.get("/api/ca-board/user/not a user")
    assert bad.status_code == 400


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
    assert settings.json()["ca_board"] is True
    assert "ca_daily_hour" not in settings.json()
    assert "ca_daily_hour" not in normalize_settings({"ca_daily_hour": 22})
    patched = client.put("/api/settings", json={"mcap_format": "en"})
    assert patched.status_code == 200
    assert patched.json()["mcap_format"] == "en"
    assert normalize_settings({"mcap_format": "en"})["mcap_format"] == "en"
    with pytest.raises(ValueError):
        normalize_settings({"mcap_format": "usd"})
    closed = client.put("/api/settings", json={"ca_board": False})
    assert closed.status_code == 200
    assert closed.json()["ca_board"] is False
    listed = client.get("/api/ca-board")
    assert listed.status_code == 200
    assert listed.json()["enabled"] is False
    assert listed.json()["board"] == []
    with pytest.raises(ValueError):
        normalize_settings({"ca_board": "off"})

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
    covered = _from_search(
        {
            "address": evm,
            "chain": "robinhood",
            "symbol": "MJ",
            "mcp": "27749.795",
            "liquidity": "12343",
            "holder_count": 135,
            "is_honeypot": False,
            "ath_market_cap": "39329.659",
        },
        evm,
    )
    assert covered["marketCap"] == 27749.795
    assert covered["athMarketCap"] == 39329.659
    assert _search_covers_card(covered)
    assert not _search_covers_card(_from_search({"chain": "bsc", "symbol": "X"}, evm))
    assert format_x(5.93) == "5.93x"
    assert wilson_lower(10, 12) > wilson_lower(1, 1)
    assert ath_from_kline({"data": {"list": [{"high": 12}, {"h": 20}]}}) == 20
    assert kline_resolution(int(__import__("time").time()) - 120) == "1m"
    assert kline_resolution(int(__import__("time").time()) - 2400) == "5m"
    assert kline_resolution(int(__import__("time").time()) - 80000) == "4h"
    assert (
        ath_from_kline(
            {"data": {"list": [{"high": 0.002}, {"high": 0.005}]}},
            current_mcap=10_000,
            current_price=0.001,
        )
        == 50_000
    )
    assert peak_since([(100, 10), (200, 50), (300, 20)], 200) == 50
    # 喊单所在那根 1m（开盘早于喊单不到 60 秒）算，更早的不算。
    assert peak_since([(100, 10), (200, 50), (300, 20)], 250) == 50
    assert peak_since([(100, 10), (200, 50), (300, 20)], 270) == 20


def test_kline_plan_never_lets_coarse_bar_cross_call() -> None:
    # AIPRO：10-21 20:20:17 喊单，开盘拉盘发生在喊单前的同一小时/同一天。
    call = 1792585217
    now = call + 3 * 86400
    plan = kline_plan([call], now)
    assert plan[0] == ("1m", call - 60, 1792587600)
    assert plan[1] == ("1h", 1792587600, 1792627200)
    assert plan[2] == ("1d", 1792627200, now)
    for resolution, lo, _hi in plan:
        if resolution != "1m":
            assert lo >= call
    # 开盘 258K 的 1h/1d 柱都在喊单前开盘，不能计入；喊单后 1m 峰值 96K 才是答案。
    bars = merge_bars(
        [(1792584000, 258_352.0)],  # 20:00 的 1h 柱
        [(1792540800, 258_352.0)],  # 08:00 的 1d 柱
        [(1792585200, 36_633.0), (1792586820, 96_438.0)],  # 1m
        [(1792627200, 41_825.0)],
    )
    assert peak_since(bars, call) == 96_438.0
    # 多个喊单人：每个锚点都有自己的 1m/1h 段，日线只拉一份。
    late = call + 2 * 86400 + 777
    multi = kline_plan([call, late], now)
    assert ("1m", late - 60, _ceil_hour(late)) in multi
    assert sum(1 for row in multi if row[0] == "1d") == 1
    assert kline_plan([], now) == []
    long_plan = kline_plan([call], call + 200 * 86400)
    assert sum(1 for row in long_plan if row[0] == "1d") == 3


def _ceil_hour(ts: int) -> int:
    return -(-ts // 3600) * 3600


def test_kline_bars_scales_price_high_to_mcap() -> None:
    # GMGN 的 high 是价格，按 现市值/现价 的供应量换算市值。
    payload = {"data": {"list": [{"time": 1792585260000, "high": "0.000070183418"}]}}
    bars = kline_bars(payload, current_mcap=17_557.467, current_price=1.7557467e-05)
    assert bars[0][0] == 1792585260
    assert round(bars[0][1]) == 70_183


def test_ca_store_tracks_first_call_and_board(tmp_path: Path) -> None:
    store = CaStore(tmp_path / "ca.db")
    address = "0x" + "ab" * 20
    first = {
        "id": "c1",
        "senderUsername": "wxid_a",
        "senderName": "大白",
        "conversationId": "12345678@chatroom",
        "conversationName": "奶牛群",
        "createTime": 1_800_000_000,
    }
    token = {
        "address": address,
        "chain": "bsc",
        "symbol": "MJ",
        "marketCap": 1000,
        "athMarketCap": 1000,
    }
    skipped = store.remember(
        first,
        [{"address": address, "chain": "bsc", "marketCap": 0, "athMarketCap": 0}],
    )
    assert skipped[0].get("foundBy") in (None, "")
    assert store.board() == []
    store.remember(
        first,
        [{"address": address, "chain": "bsc", "marketCap": 1, "athMarketCap": 50_000}],
    )
    assert store.board() == []
    out = store.remember(first, [token])
    assert out[0]["foundBy"] == "大白"
    assert out[0]["foundById"] == "wxid_a"
    assert out[0]["foundGroup"] == "奶牛群"
    assert out[0]["isFirstCall"] is True
    assert out[0]["isGroupFirst"] is True
    assert out[0]["sameGroupAsGlobal"] is True
    assert out[0]["nowX"] == 1
    assert out[0]["maxX"] == 1
    assert out[0]["groupNowX"] == 1
    store.remember(
        {
            **first,
            "id": "c2",
            "senderUsername": "wxid_b",
            "senderName": "莲花",
            "createTime": 1_800_000_100,
        },
        [{"address": address, "chain": "bsc", "marketCap": 4000, "athMarketCap": 8000}],
    )
    again = store.remember(
        first,
        [{"address": address, "chain": "bsc", "marketCap": 4000, "athMarketCap": 8000}],
    )
    assert again[0]["foundBy"] == "大白"
    assert again[0]["foundById"] == "wxid_a"
    assert again[0]["isFirstCall"] is True
    assert again[0]["maxX"] == 4
    assert again[0]["nowX"] == 4
    board = store.board()
    by_id = {row["userId"]: row for row in board}
    assert by_id["wxid_a"]["calls"] == 1
    assert by_id["wxid_b"]["calls"] == 1
    assert by_id["wxid_a"]["wins"] == 1
    assert by_id["wxid_b"]["wins"] == 0
    other = store.remember(
        {
            "id": "c3",
            "senderUsername": "wxid_c",
            "senderName": "Flyhoo",
            "conversationId": "87654321@chatroom",
            "conversationName": "YY",
            "createTime": 1_800_000_200,
        },
        [{"address": address, "chain": "bsc", "marketCap": 4000, "athMarketCap": 8000}],
    )
    assert other[0]["isFirstCall"] is False
    assert other[0]["isGroupFirst"] is True
    assert other[0]["sameGroupAsGlobal"] is False
    assert other[0]["foundBy"] == "大白"
    assert other[0]["groupFoundBy"] == "Flyhoo"
    assert other[0]["nowX"] == 4
    assert other[0]["maxX"] == 4
    assert other[0]["groupNowX"] == 1
    assert other[0]["groupMaxX"] == 1
    store.apply_quote(
        "bsc",
        address,
        {
            "marketCap": 4000,
            "klineBars": [
                [1_800_000_000, 1000],
                [1_800_000_050, 8000],
                [1_800_000_120, 6000],
                [1_800_000_200, 4000],
            ],
        },
        now=1_800_000_200,
    )
    after_kline = store.decorate_card(
        {
            **first,
            "tokenCards": [{"address": address, "chain": "bsc"}],
        }
    )
    assert after_kline["tokenCards"][0]["maxX"] == 8
    assert after_kline["tokenCards"][0]["nowX"] == 4
    yy = store.decorate_card(
        {
            "id": "c3",
            "senderUsername": "wxid_c",
            "conversationId": "87654321@chatroom",
            "createTime": 1_800_000_200,
            "tokenCards": [{"address": address, "chain": "bsc"}],
        }
    )
    assert yy["tokenCards"][0]["groupMaxX"] == 1
    by_id = {row["userId"]: row for row in store.board()}
    assert by_id["wxid_b"]["wins"] == 1
    later = store.decorate_card(
        {
            "id": "c4",
            "senderUsername": "wxid_d",
            "conversationId": "87654321@chatroom",
            "createTime": 1_800_000_300,
            "tokenCards": [{"address": address, "chain": "bsc"}],
        }
    )
    assert later["tokenCards"][0]["isGroupFirst"] is False
    assert later["tokenCards"][0]["groupFoundBy"] == "Flyhoo"
    assert later["tokenCards"][0]["groupNowX"] == 1
    profile = store.user_profile("wxid_a")
    assert profile["user"]["calls"] == 1
    assert profile["calls"][0]["isFirst"] is True
    assert profile["calls"][0]["win"] is True
    store._conn.execute(
        "INSERT INTO ca_tokens(chain, address, first_mcap, ath_mcap, last_mcap, first_at, last_at) "
        "VALUES ('bsc', '0xdead', 0, 0, 0, 1, 1)"
    )
    store._conn.commit()
    assert store.prune_bad() >= 1
    store._conn.execute("UPDATE ca_tokens SET refreshed_at = 0")
    store._conn.commit()
    stale = store.next_due(now=1_800_000_200)
    assert stale is not None
    store.apply_quote(
        "bsc",
        address,
        {
            "marketCap": 2000,
            "klineBars": [[1_800_000_000, 9000], [1_800_000_200, 2000]],
        },
        now=1_800_000_200,
    )
    decorated = store.decorate_card(
        {
            "id": "c2",
            "tokenCards": [{"address": address, "chain": "bsc"}],
        }
    )
    assert decorated["tokenCards"][0]["isFirstCall"] is False
    assert decorated["tokenCards"][0]["nowX"] == 2.0
    first_again = store.decorate_card(
        {
            "id": "other",
            "senderUsername": "wxid_a",
            "createTime": 1_800_000_000,
            "tokenCards": [{"address": address, "chain": "bsc"}],
        }
    )
    assert first_again["tokenCards"][0]["isFirstCall"] is True


def _call_card(card_id: str, user: str, at: int, room: str = "11111111111@chatroom") -> dict:
    return {
        "id": card_id,
        "senderUsername": user,
        "senderName": user,
        "conversationId": room,
        "conversationName": "测群",
        "createTime": at,
    }


def test_incremental_refresh_shares_one_kline(tmp_path: Path) -> None:
    store = CaStore(tmp_path / "ca.db")
    address = "0x" + "cd" * 20
    t0 = 1_800_000_000
    store.remember(
        _call_card("d1", "wxid_a", t0), [{"address": address, "chain": "bsc", "marketCap": 1000}]
    )
    # 新币：第一次只按首喊时间拉，没有水位线。
    assert store.plan_anchors("bsc", address) == [t0]
    # 刚喊不到 60 秒不首结算；60 秒后进入首结算队列。
    assert store.next_due(now=t0 + 10) is None
    assert store.next_due(now=t0 + SETTLE_DELAY)["address"] == address
    first = t0 + 600
    assert store.apply_quote(
        "bsc",
        address,
        {
            "marketCap": 1200,
            "klineBars": [[t0, 1000], [t0 + 300, 3000]],
            "klineOk": True,
            "klineTo": first,
        },
        now=first,
        anchors=[t0],
    )
    row = store._conn.execute("SELECT kline_until FROM ca_tokens").fetchone()
    assert row["kline_until"] == first - WATERMARK_OVERLAP
    assert store._conn.execute("SELECT settled FROM ca_calls").fetchone()[0] == 1
    # 已结算后只从水位线续拉一段，所有喊单共用。
    assert store.plan_anchors("bsc", address) == [first - WATERMARK_OVERLAP]
    # 第二个人在水位线之后喊：只为他补自己的锚点。
    t1 = first + 900
    store.remember(
        _call_card("d2", "wxid_b", t1), [{"address": address, "chain": "bsc", "marketCap": 2000}]
    )
    assert store.plan_anchors("bsc", address) == [first - WATERMARK_OVERLAP, t1]
    # 增量柱：t1 之前的 2500 只算给 A，t1 之后的 5000 两人都算。
    second = t1 + 600
    store.apply_quote(
        "bsc",
        address,
        {
            "marketCap": 2400,
            "klineBars": [[first - 60, 2500], [t1, 2100], [t1 + 120, 5000]],
            "klineOk": True,
            "klineTo": second,
        },
        now=second,
        anchors=store.plan_anchors("bsc", address),
    )
    aths = dict(store._conn.execute("SELECT caller_id, ath_mcap FROM ca_calls").fetchall())
    assert aths == {"wxid_a": 5000, "wxid_b": 5000}
    # 只增不减：空列表、更低的柱都不会把高点改小。
    assert not store.apply_quote(
        "bsc",
        address,
        {"marketCap": 2400, "klineBars": [], "klineOk": True, "klineTo": second + 120},
        now=second + 120,
    )
    store.apply_quote(
        "bsc",
        address,
        {"marketCap": 2400, "klineBars": [[second, 100]], "klineOk": True, "klineTo": second + 240},
        now=second + 240,
    )
    aths = dict(store._conn.execute("SELECT caller_id, ath_mcap FROM ca_calls").fetchall())
    assert aths == {"wxid_a": 5000, "wxid_b": 5000}
    # 拉取失败：不推进水位线、不标结算。
    before = store._conn.execute("SELECT kline_until FROM ca_tokens").fetchone()[0]
    store.remember(
        _call_card("d3", "wxid_c", second + 300),
        [{"address": address, "chain": "bsc", "marketCap": 2400}],
    )
    store.apply_quote(
        "bsc",
        address,
        {"marketCap": 2400, "klineBars": [], "klineOk": False},
        now=second + 400,
        anchors=[second + 300],
    )
    assert store._conn.execute("SELECT kline_until FROM ca_tokens").fetchone()[0] == before
    assert (
        store._conn.execute("SELECT settled FROM ca_calls WHERE caller_id = 'wxid_c'").fetchone()[0]
        == 0
    )


def test_late_recorded_call_still_gets_own_segment(tmp_path: Path) -> None:
    # 卡片晚到（回灌历史）：喊单时间早于水位线，也必须按它自己的时间补段，不能当已结算。
    store = CaStore(tmp_path / "ca.db")
    address = "0x" + "ef" * 20
    t0 = 1_800_000_000
    store.remember(
        _call_card("e1", "wxid_a", t0), [{"address": address, "chain": "bsc", "marketCap": 1000}]
    )
    mark_to = t0 + 7200
    store.apply_quote(
        "bsc",
        address,
        {"marketCap": 1000, "klineBars": [], "klineOk": True, "klineTo": mark_to},
        now=mark_to,
        anchors=[t0],
    )
    late = t0 + 1800
    store.remember(
        _call_card("e2", "wxid_b", late), [{"address": address, "chain": "bsc", "marketCap": 1500}]
    )
    assert store.plan_anchors("bsc", address) == [late, mark_to - WATERMARK_OVERLAP]


def test_next_due_tiers_and_dormant(tmp_path: Path) -> None:
    store = CaStore(tmp_path / "ca.db")
    now = 1_800_100_000
    hot = "0x" + "11" * 20
    dead = "0x" + "22" * 20
    alive = "0x" + "33" * 20
    for card, addr, at, mcap in (
        ("h", hot, now - 3600, 50_000),
        ("d", dead, now - DORMANT_AGE - 3600, 9_000),
        ("a", alive, now - DORMANT_AGE - 3600, 80_000),
    ):
        store.remember(
            _call_card(card, f"wxid_{card}", at),
            [{"address": addr, "chain": "bsc", "marketCap": mcap}],
        )
    store._conn.execute("UPDATE ca_calls SET settled = 1")
    store._conn.execute("UPDATE ca_group_firsts SET settled = 1")
    store._conn.execute("UPDATE ca_tokens SET refreshed_at = ?", (now - 1,))
    store._conn.commit()
    assert store.next_due(now=now) is None
    # 热币 2 分钟一刷。
    assert store.next_due(now=now + REFRESH_HOT)["address"] == hot
    store._conn.execute("UPDATE ca_tokens SET refreshed_at = 0")
    store._conn.commit()
    due = []
    while (row := store.next_due(now=now)) is not None:
        due.append(row["address"])
        store.mark_attempt(row["chain"], row["address"], now=now)
    # 24 小时没人喊且不到 1.5 万的休眠币不刷。
    assert dead not in due
    assert set(due) == {hot, alive}
    assert store.stats(now=now)["dormant"] == 1
    # 再被喊到就恢复。
    store.remember(
        _call_card("d2", "wxid_x", now), [{"address": dead, "chain": "bsc", "marketCap": 9_500}]
    )
    assert store.next_due(now=now + SETTLE_DELAY)["address"] == dead


def test_board_pending_cap_and_range(tmp_path: Path) -> None:
    store = CaStore(tmp_path / "ca.db")
    now = 1_800_000_000 + 40 * 86400
    rows = [
        # (用户, 币, 喊单时间, 喊单市值, 喊后最高)
        ("wxid_a", "0x" + "a1" * 20, now - 35 * 86400, 1000, 100_000),  # 100x，均倍按 20x 封顶
        ("wxid_a", "0x" + "a2" * 20, now - 2 * 86400, 1000, 1000),  # 已满 24h 未到 → 负
        ("wxid_a", "0x" + "a3" * 20, now - 3600, 1000, 1100),  # 不满 24h 未到 → 待定
        ("wxid_a", "0x" + "a4" * 20, now - 1800, 1000, 2000),  # 不满 24h 已到 → 胜
    ]
    for index, (user, addr, at, mcap, peak) in enumerate(rows):
        store.remember(
            _call_card(f"p{index}", user, at),
            [{"address": addr, "chain": "bsc", "marketCap": mcap}],
        )
        store._conn.execute("UPDATE ca_calls SET ath_mcap = ? WHERE address = ?", (peak, addr))
    store._conn.commit()
    user = store.board(now=now)[0]
    assert user["calls"] == 4
    assert user["pending"] == 1
    assert user["settled"] == 3
    assert user["wins"] == 2
    assert user["maxX"] == 100
    assert round(user["avgX"], 4) == round((20 + 1 + 2) / 3, 4)
    recent = store.board("7d", now=now)[0]
    assert recent["calls"] == 3
    assert recent["maxX"] == 2
    assert call_status(1.2, now - 100, now) == "pending"
    assert call_status(1.2, now - PENDING_AGE, now) == "lose"
    assert call_status(1.5, now - 100, now) == "win"
    only_pending = CaStore(tmp_path / "p.db")
    only_pending.remember(
        _call_card("q", "wxid_q", now - 60),
        [{"address": "0x" + "b1" * 20, "chain": "bsc", "marketCap": 1000}],
    )
    solo = only_pending.board(now=now)[0]
    assert solo["winRateText"] == "待定"
    assert solo["score"] == 0


def test_rate_window_reserve_and_sync() -> None:
    from wechat_feed.tokens import RateWindow

    async def run() -> None:
        window = RateWindow(5, window=0.3)
        # 后台要给实时留 2：窗口里已有 1 时，权重 3 的后台请求得等。
        await window.acquire(1)
        task = asyncio.create_task(window.acquire(3, reserve=2))
        await asyncio.sleep(0.05)
        assert not task.done()
        # 实时请求不受后台排队影响。
        await asyncio.wait_for(window.acquire(1), timeout=0.05)
        await asyncio.wait_for(task, timeout=1.0)
        # 服务端说只剩 0：本地补记到满额。
        fresh = RateWindow(5, window=0.3)
        fresh.sync(0)
        later = asyncio.create_task(fresh.acquire(1))
        await asyncio.sleep(0.05)
        assert not later.done()
        await asyncio.wait_for(later, timeout=1.0)

    asyncio.run(run())


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
    assert allowed_emote_url(
        "https://cdn.jsdelivr.net/gh/YiJio/emoji-chinese@1.1.0/wx/wx-new/default/wl.png"
    )
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
    assert emote_source("微笑")[1] == "0.gif"
    assert emote_source("强")[1] == "79.gif"
    assert emote_source("弱")[1] == "80.gif"
    assert emote_source("OK")[1] == "89.gif"
    assert emote_source("磕头")[1] == "96.gif"
    assert emote_source("跳绳")[1] == "98.gif"
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
    manifest = json.loads((ROOT / "extension" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["manifest_version"] == 3
    assert manifest["version"]
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
    assert "function tokenStamp" in content_js
    assert "token.marketCap" in content_js
    assert "微信群" not in feed
    assert "wf-origin-inline" not in feed
    assert 'class="wf-badge"' not in feed
    assert "原群名" not in feed
    assert "原名 " not in feed
    assert "wf-copy-btn" in feed
    assert "wf-metrics" in feed
    assert "wf-ca-addr" in feed
    assert "function chainBadge" in feed
    assert "chainBadge," in feed
    assert "function restoreHref" in feed
    assert "function formatUsd" in feed
    assert "wf-mcap" in feed
    assert "data-mcap-format" in feed
    assert "wf-tag-ico" in feed
    background = (ROOT / "extension" / "background.js").read_text(encoding="utf-8")
    assert "include_all_groups" in background
    assert "AVATAR_TTL_MS" in background
    assert "wf-avatar-v2" in background
    server = (ROOT / "src" / "wechat_feed" / "server.py").read_text(encoding="utf-8")
    assert "_AVATAR_TTL = 24 * 3600" in server
    assert "max-age=86400" in server
    app_js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    assert "await reloadFeeds()" in app_js
    assert "function setListenUsers" in app_js
    assert "function renderBoard" in app_js
    assert "ca-board" in app_js
    assert "notify_board" in (ROOT / "src" / "wechat_feed" / "hub.py").read_text(encoding="utf-8")
    assert 'from": str(start * 1000)' in (ROOT / "src" / "wechat_feed" / "tokens.py").read_text(
        encoding="utf-8"
    )
    assert "_CACHE_TTL = 2.0" in (ROOT / "src" / "wechat_feed" / "tokens.py").read_text(
        encoding="utf-8"
    )
    assert "WechatFeed.chainBadge" in app_js
    assert "function medalSvg" in app_js
    assert "function filterUserCalls" in app_js
    assert "function sortUserCalls" in app_js
    assert "data-user-sort" in app_js
    assert "data-user-range" in app_js
    assert "function formatFresh" in feed
    assert "function formatDateTime" in feed
    assert "force=True" in (ROOT / "src" / "wechat_feed" / "tokens.py").read_text(encoding="utf-8")
    assert "ca-pill score" in app_js
    assert "peak_since" in (ROOT / "src" / "wechat_feed" / "tokens.py").read_text(encoding="utf-8")
    assert "klineBars" in (ROOT / "src" / "wechat_feed" / "castore.py").read_text(encoding="utf-8")
    assert "plan_anchors" in (ROOT / "src" / "wechat_feed" / "castore.py").read_text(
        encoding="utf-8"
    )
    assert "since=since" in (ROOT / "src" / "wechat_feed" / "tokens.py").read_text(encoding="utf-8")
    assert "_KLINE_SPAN" in (ROOT / "src" / "wechat_feed" / "tokens.py").read_text(encoding="utf-8")
    castore_py = (ROOT / "src" / "wechat_feed" / "castore.py").read_text(encoding="utf-8")
    assert "WIN_X = 1.5" in castore_py
    assert "涨幅 50%" in app_js
    assert "ca-win-rule" in app_js
    assert "刚刚" in feed
    assert "分钟前" in feed
    assert "wf-debut" in feed
    assert "所有群首发" in feed
    assert "本群首发" in feed
    assert "wf-xlab" in feed
    assert "is-debut" in feed
    assert "ca_board" in (ROOT / "src" / "wechat_feed" / "config.py").read_text(encoding="utf-8")
    assert 'id="ca-board-on"' in (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    assert "CA 发现榜" in (ROOT / "extension" / "popup.html").read_text(encoding="utf-8")
    assert "setCaBoard" in background
    assert "wf-fresh" in feed
    assert "isFirstCall" in feed
    assert "wf-xbar" in feed
    assert "wf-discover" in feed
    assert "foundById" in (ROOT / "src" / "wechat_feed" / "castore.py").read_text(encoding="utf-8")
    assert "next_due" in (ROOT / "src" / "wechat_feed" / "castore.py").read_text(encoding="utf-8")
    assert "ca-strip" in (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    assert "data-open-board" in (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    assert "ca-page" in (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    css = (ROOT / "web" / "app.css").read_text(encoding="utf-8")
    assert ".member-row[hidden]" in css
    assert "ca-medal" in css
    assert "ca-pill.score" in css
    assert "ca-main" in css
    assert "ca-stats" in css
    assert 'headers={"Cache-Control": "no-store"}' in (
        ROOT / "src" / "wechat_feed" / "server.py"
    ).read_text(encoding="utf-8")
    popup_js = (ROOT / "extension" / "popup.js").read_text(encoding="utf-8")
    assert "getManifest().version" in popup_js
    popup_css = (ROOT / "extension" / "popup.css").read_text(encoding="utf-8")
    assert ".member-row[hidden]" in popup_css
    assert "dataset.hay" in app_js
    assert "function applyListen" in (ROOT / "extension" / "popup.js").read_text(encoding="utf-8")
    assert "writeTail" in background
    assert "wf-text-link" in feed
    assert "点點" in feed
    assert "hxxp" in feed
    assert "URL_GAP" in feed
    assert "URL_HOST" in feed
    assert r"^(https?:\/\/)\.+" in feed
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


def test_ca_store_keeps_solana_case(tmp_path: Path) -> None:
    # base58 区分大小写，转小写后 GMGN 会报 invalid token_address。
    store = CaStore(tmp_path / "ca.db")
    sol = "4BcyEfuXVhMmQfRaEvPx7ZsKngoMeLCkHRq4AYe2YDgn"
    evm = "0x" + "AB" * 20
    card = {
        "id": "s1",
        "senderUsername": "wxid_s",
        "senderName": "测",
        "conversationId": "12345678@chatroom",
        "createTime": 1_800_000_000,
    }
    store.remember(
        card,
        [
            {"address": sol, "chain": "sol", "marketCap": 1000},
            {"address": evm, "chain": "bsc", "marketCap": 1000},
        ],
    )
    stored = {row[0] for row in store._conn.execute("SELECT address FROM ca_tokens")}
    assert stored == {sol, evm.lower()}
    assert store.anchors("sol", sol) == [1_800_000_000]
    decorated = store.decorate_card({**card, "tokenCards": [{"address": sol, "chain": "sol"}]})
    assert decorated["tokenCards"][0]["foundBy"] == "测"


def test_ca_store_heals_lowercased_solana_rows(tmp_path: Path) -> None:
    # 旧版存成小写的 sol 行，再次喊到原大小写时迁移，而不是另起一行重复计数。
    store = CaStore(tmp_path / "ca.db")
    sol = "4BcyEfuXVhMmQfRaEvPx7ZsKngoMeLCkHRq4AYe2YDgn"
    card = {
        "id": "h1",
        "senderUsername": "wxid_h",
        "senderName": "旧",
        "conversationId": "12345678@chatroom",
        "createTime": 1_800_000_000,
    }
    store.remember(card, [{"address": sol, "chain": "sol", "marketCap": 1000}])
    for table in ("ca_tokens", "ca_calls", "ca_group_firsts"):
        store._conn.execute(f"UPDATE {table} SET address = lower(address)")
    store._conn.commit()
    store.remember(
        {**card, "id": "h2", "createTime": 1_800_000_500},
        [{"address": sol, "chain": "sol", "marketCap": 2000}],
    )
    for table in ("ca_tokens", "ca_calls", "ca_group_firsts"):
        rows = [r[0] for r in store._conn.execute(f"SELECT address FROM {table}")]
        assert rows == [sol], table
    by_id = {row["userId"]: row for row in store.board()}
    assert by_id["wxid_h"]["calls"] == 1


def test_background_budget_leaves_room_for_realtime() -> None:
    # 后台 K 线（权重 3、留 2）每个窗口最多 1 条；同一窗口里实时请求仍能立刻拿到 2。
    import time

    from wechat_feed.tokens import RateWindow

    async def run() -> tuple[float, float]:
        window = RateWindow(5, window=0.2)
        start = time.monotonic()
        for _ in range(4):
            await window.acquire(3, reserve=2)
        bg_elapsed = time.monotonic() - start
        realtime = time.monotonic()
        await window.acquire(1)
        await window.acquire(1)
        return bg_elapsed, time.monotonic() - realtime

    bg_elapsed, realtime_wait = asyncio.run(run())
    assert bg_elapsed >= 0.55  # 4 条至少跨 3 个完整窗口
    assert realtime_wait < 0.05


def test_gmgn_meter_summary_peak_and_timeline() -> None:
    from wechat_feed.gmgnmeter import GmgnMeter

    meter = GmgnMeter(5, 1.2, 2)
    kline = "/v1/market/token_kline"
    search = "/v1/market/search"
    meter.record(search, 1, background=False, outcome="ok", status=200, latency_ms=120, remaining=4)
    meter.record(kline, 3, background=True, outcome="ok", status=200, latency_ms=300, remaining=1)
    meter.record(kline, 3, background=True, outcome="limited", status=429, latency_ms=80)
    meter.record(kline, 3, background=True, outcome="skipped")
    meter.record(search, 1, background=False, outcome="biz", status=200, latency_ms=90)
    snap = meter.snapshot({"background": 0.0})
    five = snap["windows"]["5m"]
    # 跳过的请求不算发出、不占权重。
    assert five["calls"] == 4
    assert five["weight"] == 1 + 3 + 3 + 1
    assert five["outcomes"]["limited"] == 1
    assert five["outcomes"]["skipped"] == 1
    assert five["successRate"] == 0.5
    # 同一个 1.2 秒窗口内连发，峰值就是总和，超过套餐 5 一眼可见。
    assert five["peak"] == 8
    # 峰值按发出时间算：间隔超过一个窗口发出的两条不叠加。
    spaced = GmgnMeter(5, 1.2, 2)
    base = __import__("time").time()
    spaced.record(kline, 3, background=True, outcome="ok", at=base - 3.0)
    spaced.record(kline, 3, background=True, outcome="ok", at=base - 1.7)
    assert spaced.snapshot()["windows"]["1m"]["peak"] == 3
    by_path = {row["path"]: row for row in five["paths"]}
    assert by_path[kline]["background"] == 2
    assert by_path[kline]["limited"] == 1
    assert by_path[search]["realtime"] == 2
    assert by_path[kline]["share"] == 0.75
    assert snap["current"]["minRemaining5m"] == 1
    assert snap["current"]["windowWeight"] == 8
    assert sum(b["background"] + b["realtime"] for b in snap["timeline"]) == 8
    assert sum(b["limited"] for b in snap["timeline"]) == 1
    assert snap["recent"][0]["outcome"] == "biz"
    meter.record(kline, 3, background=True, outcome="ok", wait_ms=700, yield_ms=400)
    split = meter.snapshot()["recent"][0]
    assert (split["waitMs"], split["yieldMs"]) == (700, 400)
    assert snap["config"]["backgroundCap"] == 2.5
    assert snap["totals"]["calls"] == 4


def test_gmgn_stats_api_and_page(client: TestClient) -> None:
    stats = client.get("/api/gmgn-stats")
    assert stats.status_code == 200
    body = stats.json()
    assert body["ok"] is True
    assert body["config"]["limit"] == 5
    assert set(body["windows"]) == {"1m", "5m", "1h"}
    assert "gmgn_" not in stats.text
    page = client.get("/gmgn")
    assert page.status_code == 200
    assert "GMGN 用量" in page.text
    assert client.get("/assets/gmgn.js").status_code == 200
    assert 'href="/gmgn"' in (ROOT / "web" / "index.html").read_text(encoding="utf-8")


def test_gmgn_store_persists_history_and_purges(tmp_path: Path) -> None:
    import time

    from wechat_feed.gmgnmeter import GmgnMeter
    from wechat_feed.gmgnstore import GmgnStore

    kline = "/v1/market/token_kline"
    db = GmgnStore(tmp_path / "gmgn.db")
    meter = GmgnMeter(5, 1.2, 2)
    now = time.time()
    meter.record(kline, 3, background=True, outcome="ok", latency_ms=200, remaining=2, at=now - 30)
    meter.record(
        kline, 3, background=True, outcome="ok", latency_ms=300, remaining=1, at=now - 29.5
    )
    meter.record("/v1/market/search", 1, background=False, outcome="limited", at=now - 20)
    batch, stats = meter.drain()
    assert len(batch) == 3
    db.write(batch, stats)
    # 已经落盘的不会重复写。
    assert meter.drain()[0] == []
    hist = db.history("24h", now=now)
    assert hist["totals"]["calls"] == 3
    assert hist["totals"]["weight"] == 7
    assert hist["totals"]["limited"] == 1
    assert hist["totals"]["peak"] == 6
    assert {row["path"] for row in hist["paths"]} == {kline, "/v1/market/search"}
    # 重启：明细能装回新的 meter。
    again = GmgnMeter(5, 1.2, 2)
    again.preload(db.load_recent(3600, now=now))
    restored = again.snapshot()["windows"]["5m"]
    assert restored["calls"] == 3
    # 速率按装回数据的真实跨度算（约 30 秒），不按刚启动的几毫秒算。
    assert restored["weightPerSec"] < 0.5
    summary = db.summary()
    assert summary["rawRows"] == 3
    assert summary["keepDays"] == 365
    # 预览不删，确认才删。
    preview = db.purge(now - 3600, now + 60, dry_run=True)
    assert preview["rawRows"] == 3
    assert db.summary()["rawRows"] == 3
    db.purge(now - 3600, now + 60, dry_run=False)
    assert db.summary()["rawRows"] == 0
    assert db.history("24h", now=now)["totals"]["calls"] == 0
    # 一年前的分钟汇总会被清掉。
    old = GmgnMeter(5, 1.2, 2)
    old.record(kline, 3, background=True, outcome="ok", at=now - 400 * 86400)
    db.write(*old.drain())
    db.prune(now=now)
    assert db.summary()["minuteRows"] == 0
    db.close()


def test_ca_store_purge_period_survives_reimport(tmp_path: Path) -> None:
    store = CaStore(tmp_path / "ca.db")
    keep = "0x" + "c1" * 20
    drop = "0x" + "c2" * 20
    t_keep = 1_800_000_000
    t_drop = t_keep + 40 * 86400
    store.remember(
        _call_card("k", "wxid_k", t_keep), [{"address": keep, "chain": "bsc", "marketCap": 1000}]
    )
    store.remember(
        _call_card("d", "wxid_d", t_drop), [{"address": drop, "chain": "bsc", "marketCap": 1000}]
    )
    # 老币在删除段内又被喊过一次：只删那条喊单，币保留。
    store.remember(
        _call_card("k2", "wxid_x", t_drop + 60),
        [{"address": keep, "chain": "bsc", "marketCap": 1200}],
    )
    preview = store.purge(t_drop - 10, t_drop + 3600, dry_run=True)
    assert preview == {"calls": 2, "groupFirsts": 1, "tokens": 1}
    assert len(store.board(now=t_drop + 7200)) == 3
    store.purge(t_drop - 10, t_drop + 3600, dry_run=False)
    tokens = {row[0] for row in store._conn.execute("SELECT address FROM ca_tokens")}
    assert tokens == {keep}
    assert [r["userId"] for r in store.board(now=t_drop + 7200)] == ["wxid_k"]
    # 启动时从 feed.db 回灌同一张卡片，被删的那段不会写回来。
    store.remember(
        _call_card("d", "wxid_d", t_drop), [{"address": drop, "chain": "bsc", "marketCap": 1000}]
    )
    assert store._count("SELECT COUNT(*) FROM ca_calls WHERE called_at = ?", (t_drop,)) == 0
    summary = store.data_summary()
    assert summary["calls"] == 1
    assert summary["purged"][0]["start"] == t_drop - 10
    assert summary["keepDays"] == 365


def test_data_api_requires_json_and_valid_range(client: TestClient) -> None:
    summary = client.get("/api/data")
    assert summary.status_code == 200
    assert {"board", "gmgn"} <= set(summary.json())
    # 跨站表单只能发 text/plain，不经过 CORS 预检，必须拒绝。
    form = client.post(
        "/api/data/purge",
        content='{"target":"all","start":0,"end":1,"dryRun":false}',
        headers={"Content-Type": "text/plain"},
    )
    assert form.status_code == 415
    assert (
        client.post("/api/data/purge", json={"target": "x", "start": 0, "end": 1}).status_code
        == 400
    )
    assert (
        client.post("/api/data/purge", json={"target": "all", "start": 5, "end": 5}).status_code
        == 400
    )
    # 当前月：月末在未来，也要能预览。
    future = int(__import__("time").time()) + 20 * 86400
    ok = client.post("/api/data/purge", json={"target": "board", "start": 0, "end": future})
    assert ok.status_code == 200
    far = {"target": "board", "start": 0, "end": 9_999_999_999}
    assert client.post("/api/data/purge", json=far).status_code == 400
    dry = client.post("/api/data/purge", json={"target": "all", "start": 0, "end": 1000})
    assert dry.status_code == 200
    assert dry.json()["dryRun"] is True
    assert client.post("/api/data/vacuum", json={}).status_code == 200
    assert client.get("/data").status_code == 200
    history = client.get("/api/gmgn-history?range=1y")
    assert history.status_code == 200
    assert history.json()["range"] == "1y"
    assert len(history.json()["series"]) == 365
