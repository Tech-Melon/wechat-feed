"""从文本里抽出合约，并用 DexScreener 定链、取 ticker / 市值 / 图标。"""

from __future__ import annotations

import asyncio
import contextvars
import os
import re
import time
import uuid
from collections import deque
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import httpx
from loguru import logger

from .gmgnmeter import GmgnMeter
from .http import client as http_client

_EVM = re.compile(r"0x[a-fA-F0-9]{40}(?![a-fA-F0-9])")
_SUI = re.compile(r"0x[a-fA-F0-9]{64}(?![a-fA-F0-9])")
_TRON = re.compile(r"\bT[1-9A-HJ-NP-Za-km-z]{33}\b")
_TON = re.compile(r"\b(?:EQ|UQ)[A-Za-z0-9_-]{46}\b")
_PUMP = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}pump\b")
_SOL = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")
_CHAIN_TOKEN = (
    r"sol|solana|bsc|bnb|base|eth|ethereum|arb|arbitrum|blast|polygon|matic|"
    r"monad|robinhood|tron|trx|ton|sui|avax|avalanche|op|optimism|linea|"
    r"scroll|zksync|hyperevm|plasma|ape|mantle|sei"
)
_GMGN_URL = re.compile(
    rf"https?://(?:www\.)?gmgn\.ai/({_CHAIN_TOKEN})/token/"
    r"(?:[^\s/]*?_?)?(0x[a-fA-F0-9]{40,64}|T[1-9A-HJ-NP-Za-km-z]{33}|"
    r"(?:EQ|UQ)[A-Za-z0-9_-]{46}|[1-9A-HJ-NP-Za-km-z]{32,44})",
    re.IGNORECASE,
)
_DEBOT_URL = re.compile(
    rf"https?://(?:www\.)?debot\.ai/token/({_CHAIN_TOKEN})/[^\s]*?"
    r"(0x[a-fA-F0-9]{40,64}|T[1-9A-HJ-NP-Za-km-z]{33}|[1-9A-HJ-NP-Za-km-z]{32,44}(?:pump)?)",
    re.IGNORECASE,
)
_DEX_URL = re.compile(
    rf"https?://(?:www\.)?dexscreener\.com/({_CHAIN_TOKEN})/"
    r"(0x[a-fA-F0-9]{40,64}|T[1-9A-HJ-NP-Za-km-z]{33}|[1-9A-HJ-NP-Za-km-z]{32,44})",
    re.IGNORECASE,
)
_BIRDEYE_URL = re.compile(
    r"https?://(?:www\.)?birdeye\.so/token/"
    r"(0x[a-fA-F0-9]{40,64}|[1-9A-HJ-NP-Za-km-z]{32,44})",
    re.IGNORECASE,
)
_SOLSCAN_URL = re.compile(
    r"https?://(?:www\.)?solscan\.io/token/([1-9A-HJ-NP-Za-km-z]{32,44})",
    re.IGNORECASE,
)
_TRONSCAN_URL = re.compile(
    r"https?://(?:www\.)?tronscan\.org/#/token20/(T[1-9A-HJ-NP-Za-km-z]{33})",
    re.IGNORECASE,
)

_CHAIN_ALIAS = {
    "bnb": "bsc",
    "binance": "bsc",
    "binance-smart-chain": "bsc",
    "ethereum": "eth",
    "solana": "sol",
    "solana-mainnet": "sol",
    "matic": "polygon",
    "trx": "tron",
    "avalanche": "avax",
    "optimism": "op",
    "arbitrum": "arb",
    "x-layer": "xlayer",
}

# 只把官网域名交给谷歌 s2 拉图标，避免直连官网被墙；下过一次就落在 data/chainicons/。
_CHAIN_SITES = {
    "sol": "solana.com",
    "base": "base.org",
    "bsc": "bnbchain.org",
    "eth": "ethereum.org",
    "robinhood": "robinhood.com",
    "arc": "arc.network",
    "tron": "tron.network",
    "ton": "ton.org",
    "sui": "sui.io",
    "polygon": "polygon.technology",
    "arb": "arbitrum.io",
    "avax": "avax.network",
    "op": "optimism.io",
    "blast": "blast.io",
    "monad": "monad.xyz",
    "linea": "linea.build",
    "scroll": "scroll.io",
    "zksync": "zksync.io",
    "sei": "sei.io",
    "mantle": "mantle.xyz",
    "ape": "apechain.com",
    "hyperevm": "hyperliquid.xyz",
    "plasma": "plasma.to",
    "xlayer": "www.okx.com",
}

# OpenAPI token/info 用全称；页面 URL 仍用短链名。
_GMGN_API_CHAIN = {
    "arb": "arbitrum",
    "arbitrum": "arbitrum",
    "eth": "eth",
    "ethereum": "eth",
    "bnb": "bsc",
    "binance": "bsc",
    "solana": "sol",
}

# 无链 search 没命中时，才按这三条补搜。
_EVM_SEARCH_CHAINS = ("bsc", "eth", "robinhood")


def _norm_chain(value: str | None) -> str:
    text = str(value or "").strip().lower()
    return _CHAIN_ALIAS.get(text, text)


def chain_favicon(name: str) -> tuple[str, str] | None:
    """谷歌网站图标接口。只放行表内链，避免做成开放代理；直连官网可能访问不到。"""
    key = _norm_chain(name)
    domain = _CHAIN_SITES.get(key)
    if not domain:
        return None
    url = f"https://www.google.com/s2/favicons?sz=64&domain={domain}"
    return url, f"{key}.png"


def allowed_chainicon_url(url: str) -> bool:
    parsed = urlsplit(str(url or ""))
    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").lower()
    if host in {"www.google.com", "google.com"}:
        return parsed.path.startswith("/s2/favicons")
    return host == "gstatic.com" or host.endswith(".gstatic.com")


def gmgn_api_chain(value: str | None) -> str:
    text = _norm_chain(value)
    return _GMGN_API_CHAIN.get(text, text)


def candidate_chains(
    address: str,
    hint: str = "",
    *,
    resolved: bool = False,
    dex_chain: str = "",
) -> list[str]:
    """按地址形态给出 GMGN 探测顺序：已定链只留一条，EVM 只走 bsc / eth / robinhood。"""
    kind = _kind_of(address, hint or dex_chain)
    ordered: list[str] = []

    def add(chain: str) -> None:
        mapped = gmgn_api_chain(chain)
        if mapped and mapped not in ordered:
            ordered.append(mapped)

    add(dex_chain)
    add(hint)
    if kind == "sol":
        add("sol")
        return ordered
    if kind == "tron":
        add("tron")
        return ordered
    if kind == "sui":
        add("sui")
        return ordered
    if kind == "ton":
        add("ton")
        return ordered
    if kind == "evm" and not resolved:
        for item in _EVM_SEARCH_CHAINS:
            add(item)
    return ordered


def _search_buckets(
    payload: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not isinstance(payload, dict):
        return [], []
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if not isinstance(data, dict):
        return [], []
    coins = data.get("coins") or data.get("tokens") or data.get("list") or []
    wallets = data.get("wallets") or []
    return (
        [row for row in coins if isinstance(row, dict)],
        [row for row in wallets if isinstance(row, dict)],
    )


def parse_search(
    payload: dict[str, Any] | None, address: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """只认地址完全一致的 coin / wallet，避免把同名垃圾盘或钱包误当成代币。"""
    needle = str(address or "").lower()
    coins, wallets = _search_buckets(payload)
    if not needle:
        return [], []
    matched_coins = [
        row
        for row in coins
        if str(row.get("address") or row.get("token_address") or "").lower() == needle
    ]
    matched_wallets = [row for row in wallets if str(row.get("address") or "").lower() == needle]
    return matched_coins, matched_wallets


def chain_from_search(payload: dict[str, Any] | None, address: str) -> str:
    coins, _wallets = parse_search(payload, address)
    if not coins:
        return ""
    return gmgn_api_chain(str(coins[0].get("chain") or coins[0].get("network") or ""))


def gmgn_address_url(chain: str, address: str) -> str:
    mapped = _norm_chain(chain)
    if mapped == "solana":
        mapped = "sol"
    if mapped in {"binance", "bnb"}:
        mapped = "bsc"
    if mapped:
        return f"https://gmgn.ai/{mapped}/address/{address}"
    return f"https://gmgn.ai/address/{address}"


def gmgn_token_url(chain: str, address: str) -> str:
    mapped = _norm_chain(chain) or "bsc"
    if mapped == "solana":
        mapped = "sol"
    if mapped in {"binance", "bnb"}:
        mapped = "bsc"
    return f"https://gmgn.ai/{mapped}/token/{address}"


def _kind_of(address: str, chain: str = "") -> str:
    text = str(address or "")
    mapped = _norm_chain(chain)
    if mapped in {"sol", "solana"} or text.endswith("pump"):
        return "sol"
    if mapped in {"tron", "trx"} or (text.startswith("T") and len(text) == 34):
        return "tron"
    if mapped == "sui" or (text.startswith("0x") and len(text) == 66):
        return "sui"
    if mapped == "ton" or text.startswith("EQ") or text.startswith("UQ"):
        return "ton"
    if text.startswith("0x"):
        return "evm"
    if mapped:
        return mapped
    return "sol"


def format_tax(value: Any) -> str:
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return "0%"
    if 0 < number <= 1:
        number *= 100
    text = f"{number:.1f}%".replace(".0%", "%")
    return text


def format_mcap(value: float) -> str:
    number = float(value)
    if number >= 100_000_000:
        return f"{number / 100_000_000:.2f}亿"
    if number >= 10_000:
        return f"{number / 10_000:.1f}万"
    if number >= 1:
        return f"{number:.0f}"
    return f"{number:.4f}"


def extract_cas(text: str) -> list[dict[str, Any]]:
    """链接里的链作准；裸 EVM / Sui 先不定链。

    pump / 混排 base58 视为 Solana；T 开头 34 位视为 Tron。
    """

    source = str(text or "")
    hits: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(address: str, *, kind: str = "", chain: str = "", resolved: bool = False) -> None:
        text_addr = str(address or "").strip()
        if not text_addr:
            return
        kind = kind or _kind_of(text_addr, chain)
        if kind in {"evm", "sui"}:
            stored = text_addr.lower()
        elif kind == "sol":
            stored = text_addr
        else:
            stored = text_addr
        key = stored.lower()
        if key in seen:
            return
        seen.add(key)
        hits.append(
            {
                "address": stored,
                "kind": kind,
                "chain": _norm_chain(chain),
                "resolved": resolved,
            }
        )

    for match in _GMGN_URL.finditer(source):
        add(match.group(2), chain=_norm_chain(match.group(1)), resolved=True)
    for match in _DEBOT_URL.finditer(source):
        add(match.group(2), chain=_norm_chain(match.group(1)), resolved=True)
    for match in _DEX_URL.finditer(source):
        add(match.group(2), chain=_norm_chain(match.group(1)), resolved=True)
    for match in _BIRDEYE_URL.finditer(source):
        address = match.group(1)
        chain = "sol" if not address.startswith("0x") else ""
        add(address, chain=chain, resolved=bool(chain))
    for match in _SOLSCAN_URL.finditer(source):
        add(match.group(1), kind="sol", chain="sol", resolved=True)
    for match in _TRONSCAN_URL.finditer(source):
        add(match.group(1), kind="tron", chain="tron", resolved=True)
    for match in _SUI.finditer(source):
        add(match.group(0), kind="sui", chain="sui", resolved=True)
    for match in _EVM.finditer(source):
        add(match.group(0), kind="evm", chain="", resolved=False)
    for match in _TRON.finditer(source):
        add(match.group(0), kind="tron", chain="tron", resolved=True)
    for match in _TON.finditer(source):
        add(match.group(0), kind="ton", chain="ton", resolved=True)
    for match in _PUMP.finditer(source):
        add(match.group(0), kind="sol", chain="sol", resolved=True)
    for match in _SOL.finditer(source):
        address = match.group(0)
        if address.endswith("pump") or address.startswith("T"):
            continue
        if not re.search(r"[A-Z]", address) or not re.search(r"[a-z]", address):
            continue
        if not re.search(r"\d", address):
            continue
        add(address, kind="sol", chain="sol", resolved=True)
    return hits


def _from_pair(pair: dict[str, Any], address: str, hint: str) -> dict[str, Any]:
    chain = _norm_chain(str(pair.get("chainId") or hint or ""))
    base = pair.get("baseToken") if isinstance(pair.get("baseToken"), dict) else {}
    info = pair.get("info") if isinstance(pair.get("info"), dict) else {}
    quote = pair.get("quoteToken") if isinstance(pair.get("quoteToken"), dict) else {}
    token = base if str(base.get("address") or "").lower() == address.lower() else quote
    if not token:
        token = base
    symbol = str(token.get("symbol") or "").strip() or "TOKEN"
    name = str(token.get("name") or symbol).strip()
    logo = str(info.get("imageUrl") or "").strip()
    market_cap = 0.0
    for key in ("marketCap", "fdv"):
        try:
            market_cap = float(pair.get(key) or 0)
        except (TypeError, ValueError):
            market_cap = 0.0
        if market_cap > 0:
            break
    liq_obj = pair.get("liquidity")
    liquidity = _num(liq_obj.get("usd") if isinstance(liq_obj, dict) else liq_obj)
    created = _unix_seconds(pair.get("pairCreatedAt"))
    return {
        "address": address,
        "chain": chain or hint or "",
        "symbol": symbol,
        "name": name,
        "logo": logo,
        "marketCap": market_cap,
        "marketCapText": format_mcap(market_cap) if market_cap else "—",
        "priceUsd": str(pair.get("priceUsd") or ""),
        "url": gmgn_token_url(chain or hint or "bsc", address),
        "source": "dexscreener",
        "twitter": "",
        "website": "",
        "telegram": "",
        "buyTax": "",
        "sellTax": "",
        "honeypot": "",
        "openSource": "",
        "renounced": "",
        "top10": "",
        "launchpad": "",
        "liquidity": liquidity,
        "liquidityText": format_mcap(liquidity) if liquidity else "—",
        "holders": 0,
        "createdAt": created,
    }


def _gmgn_headers(api_key: str) -> dict[str, str]:
    return {
        "X-APIKEY": api_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _unwrap(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    if payload.get("code") in (0, "0"):
        data = payload.get("data")
        return data if isinstance(data, dict) else None
    if "symbol" in payload or "logo" in payload:
        return payload
    return None


def _from_gmgn(
    info: dict[str, Any],
    security: dict[str, Any] | None,
    chain: str,
    address: str,
) -> dict[str, Any]:
    price_obj = info.get("price") if isinstance(info.get("price"), dict) else {}
    link = info.get("link") if isinstance(info.get("link"), dict) else {}
    try:
        price = float(price_obj.get("price") or 0)
    except (TypeError, ValueError):
        price = 0.0
    try:
        supply = float(info.get("circulating_supply") or info.get("total_supply") or 0)
    except (TypeError, ValueError):
        supply = 0.0
    market_cap = price * supply if price and supply else 0.0
    ath_price = _num(price_obj.get("ath"))
    if not ath_price:
        ath_price = _num(info.get("ath_price"))
    ath_mcap = ath_price * supply if ath_price and supply else 0.0
    if not ath_mcap:
        ath_mcap = _num(info.get("ath_market_cap") or info.get("history_highest_market_cap"))
    try:
        liquidity = float(info.get("liquidity") or 0)
    except (TypeError, ValueError):
        liquidity = 0.0
    security = security or {}
    created = 0
    try:
        created = int(info.get("creation_timestamp") or info.get("open_timestamp") or 0)
    except (TypeError, ValueError):
        created = 0
    holders = 0
    try:
        holders = int(info.get("holder_count") or 0)
    except (TypeError, ValueError):
        holders = 0
    top10 = security.get("top_10_holder_rate")
    top10_text = ""
    try:
        if top10 is not None and str(top10) != "":
            rate = float(top10)
            if rate <= 1:
                rate *= 100
            top10_text = f"{rate:.1f}%".replace(".0%", "%")
    except (TypeError, ValueError):
        top10_text = ""
    logo = str(info.get("logo") or "").strip()
    return {
        "address": address,
        "chain": chain,
        "symbol": str(info.get("symbol") or "").strip() or "TOKEN",
        "name": str(info.get("name") or info.get("symbol") or "").strip(),
        "logo": logo,
        "marketCap": market_cap,
        "marketCapText": format_mcap(market_cap) if market_cap else "—",
        "athMarketCap": ath_mcap,
        "priceUsd": str(price_obj.get("price") or ""),
        "url": gmgn_token_url(chain or "bsc", address),
        "source": "gmgn",
        "twitter": str(link.get("twitter_username") or "").strip(),
        "website": str(link.get("website") or "").strip(),
        "telegram": str(link.get("telegram") or "").strip(),
        "buyTax": format_tax(security.get("buy_tax")),
        "sellTax": format_tax(security.get("sell_tax")),
        "honeypot": str(security.get("is_honeypot") or ""),
        "openSource": str(security.get("open_source") or ""),
        "renounced": str(security.get("owner_renounced") or ""),
        "top10": top10_text,
        "launchpad": str(info.get("launchpad_platform") or info.get("launchpad") or "").strip(),
        "liquidity": liquidity,
        "liquidityText": format_mcap(liquidity) if liquidity else "—",
        "holders": holders,
        "createdAt": created,
    }


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return default


_KLINE_SPAN = {"1m": 60, "5m": 300, "1h": 3600, "4h": 14400, "1d": 86400}


def kline_resolution(created_at: int, now: int | None = None) -> str:
    """Ave：<10m 用 1m，否则 5m/1h/4h/1d。粗柱会把喊单前的高点算进当根，近处必须细。"""
    stamp = int(now or time.time())
    age = max(0, stamp - int(created_at or 0)) if created_at else 7 * 86400
    if age < 1800:
        return "1m"
    if age < 3600:
        return "5m"
    if age < 43200:
        return "1h"
    if age < 86400:
        return "4h"
    return "1d"


_HOUR = 3600
_DAY = 86400
# GMGN 单次最多回 100 根，且是窗口内最新的 100 根；日线按 90 天一段拉。
_DAY_CHUNK = 90 * _DAY
_MAX_ANCHORS = 4


def _ceil_to(ts: int, span: int) -> int:
    return -(-int(ts) // span) * span


def kline_plan(anchors: list[int], now: int) -> list[tuple[str, int, int]]:
    """喊单后 K 线分段：喊单→整点 1m，整点→UTC 0 点 1h，之后 1d。

    GMGN 的柱按 UTC 对齐，粗柱只从喊单后的整点/0 点开始拉，
    这样不会有任何一根粗柱跨过喊单时间、把喊单前的高点带进来。
    """
    stamp = int(now)
    points = sorted({int(a) for a in anchors if 0 < int(a or 0) < stamp})
    if not points:
        return []
    if len(points) > _MAX_ANCHORS:
        points = points[:1] + points[-(_MAX_ANCHORS - 1) :]
    segments: list[tuple[str, int, int]] = []
    for anchor in points:
        hour = _ceil_to(anchor, _HOUR)
        day = _ceil_to(anchor, _DAY)
        # 往前多拉 60 秒，确保喊单所在那根 1m 在窗口里。
        segments.append(("1m", anchor - 60, min(hour, stamp)))
        segments.append(("1h", hour, min(day, stamp)))
    start = _ceil_to(points[0], _DAY)
    while start < stamp:
        end = min(start + _DAY_CHUNK, stamp)
        segments.append(("1d", start, end))
        start = end
    out: list[tuple[str, int, int]] = []
    for seg in segments:
        res, lo, hi = seg
        if lo >= hi or seg in out:
            continue
        # 同粒度被别段完全覆盖的不再重复拉。
        if any(r == res and a <= lo and hi <= b and (a, b) != (lo, hi) for r, a, b in segments):
            continue
        out.append(seg)
    return out


def kline_bars(
    payload: dict[str, Any] | None,
    current_mcap: float = 0.0,
    current_price: float = 0.0,
) -> list[tuple[int, float]]:
    """K 线 → [(时间, 市值高点)]。

    GMGN 的 high 是价格：同 Ave，市值 = 高点价格 × 供应量，供应量 = 现市值 / 现价。
    没有价格锚点时才把 high 当市值。
    """
    if not isinstance(payload, dict):
        return []
    raw_data = payload.get("data")
    data = raw_data if isinstance(raw_data, (dict, list)) else payload
    rows: list[Any] = []
    if isinstance(data, dict):
        raw = (
            data.get("list") or data.get("klines") or data.get("items") or data.get("points") or []
        )
        rows = raw if isinstance(raw, list) else []
    elif isinstance(data, list):
        rows = data
    out: list[tuple[int, float]] = []
    for row in rows:
        bar_time = 0
        mcap = 0.0
        high = 0.0
        if isinstance(row, dict):
            bar_time = _unix_seconds(
                row.get("time") or row.get("timestamp") or row.get("t") or row.get("open_time")
            )
            mcap = _num(row.get("market_cap") or row.get("mcp") or row.get("usd_market_cap"))
            high = _num(row.get("high") or row.get("h") or row.get("max"))
        elif isinstance(row, (list, tuple)) and len(row) >= 3:
            bar_time = _unix_seconds(row[0])
            high = _num(row[2])
        value = mcap
        if value <= 0 and high > 0 and current_price > 0 and current_mcap > 0:
            value = current_mcap * (high / current_price)
        elif value <= 0 and high >= 1:
            value = high
        if value <= 0:
            continue
        if value < 1 and current_mcap >= 100:
            continue
        out.append((bar_time, value))
    out.sort(key=lambda item: item[0])
    return out


def kline_last_close(payload: dict[str, Any] | None) -> float:
    if not isinstance(payload, dict):
        return 0.0
    raw_data = payload.get("data")
    data = raw_data if isinstance(raw_data, (dict, list)) else payload
    rows: list[Any] = []
    if isinstance(data, dict):
        raw = (
            data.get("list") or data.get("klines") or data.get("items") or data.get("points") or []
        )
        rows = raw if isinstance(raw, list) else []
    elif isinstance(data, list):
        rows = data
    close = 0.0
    for row in rows:
        if isinstance(row, dict):
            value = _num(row.get("close") or row.get("c"))
            if value > 0:
                close = value
        elif isinstance(row, (list, tuple)) and len(row) >= 5:
            value = _num(row[4])
            if value > 0:
                close = value
    return close


def peak_since(bars: list[tuple[int, float]], since_ts: int = 0) -> float:
    """喊单之后的最高（同 Ave：喊单所在那根 1m 也算）。

    只认开盘时间 >= 喊单前 60 秒的柱：喊单所在的 1m 柱算进来，
    跨过喊单的 1h/1d 粗柱一律不算，避免把开盘拉盘的高点记到喊单人头上。
    """
    peak = 0.0
    since = int(since_ts or 0)
    floor = since - 60 if since else 0
    for bar_time, value in bars:
        if value <= 0:
            continue
        if floor and bar_time < floor:
            continue
        peak = max(peak, value)
    return peak


def ath_from_kline(
    payload: dict[str, Any] | None,
    current_mcap: float = 0.0,
    current_price: float = 0.0,
    since: int = 0,
) -> float:
    return peak_since(kline_bars(payload, current_mcap, current_price), since)


def merge_bars(*groups: list[tuple[int, float]]) -> list[tuple[int, float]]:
    """多段多粒度的柱合并：同一开盘时间取高者，按时间排序。"""
    merged: dict[int, float] = {}
    for bars in groups:
        for bar_time, value in bars:
            if value > merged.get(bar_time, 0.0):
                merged[bar_time] = value
    return sorted(merged.items())


def _unix_seconds(value: Any) -> int:
    number = _num(value)
    if number >= 1_000_000_000_000:
        number /= 1000
    return int(number) if number > 0 else 0


def _best_coin(coins: list[dict[str, Any]]) -> dict[str, Any]:
    return max(coins, key=lambda row: _num(row.get("liquidity")))


def _from_search(row: dict[str, Any], address: str) -> dict[str, Any]:
    chain = gmgn_api_chain(str(row.get("chain") or row.get("network") or ""))
    price = _num(row.get("price"))
    liquidity = _num(row.get("liquidity"))
    market_cap = _num(row.get("market_cap") or row.get("usd_market_cap") or row.get("mcp"))
    ath_mcap = _num(row.get("ath_market_cap") or row.get("history_highest_market_cap"))
    holders = int(_num(row.get("holder_count")))
    created = int(
        _num(row.get("created_at") or row.get("creation_timestamp") or row.get("open_timestamp"))
    )
    honeypot = row.get("is_honeypot")
    if honeypot is True or honeypot in {1, "1", "yes", "true"}:
        honeypot_text = "yes"
    elif honeypot is False or honeypot in {0, "0", "no", "false"}:
        honeypot_text = "no"
    else:
        honeypot_text = str(honeypot or "")
    logo = str(row.get("logo") or "").strip()
    link = row.get("token_link") if isinstance(row.get("token_link"), dict) else {}
    twitter = str(
        row.get("twitter") or row.get("twitter_username") or link.get("twitter_username") or ""
    ).strip()
    website = str(row.get("website") or link.get("website") or "").strip()
    telegram = str(row.get("telegram") or link.get("telegram") or "").strip()
    return {
        "address": address,
        "chain": chain,
        "symbol": str(row.get("symbol") or "").strip() or "TOKEN",
        "name": str(row.get("name") or row.get("symbol") or "").strip(),
        "logo": logo,
        "marketCap": market_cap,
        "marketCapText": format_mcap(market_cap) if market_cap else "—",
        "athMarketCap": ath_mcap,
        "priceUsd": str(row.get("price") or "") if price else "",
        "url": gmgn_token_url(chain or "bsc", address),
        "source": "gmgn-search",
        "twitter": twitter,
        "website": website,
        "telegram": telegram,
        "buyTax": format_tax(row.get("buy_tax")) if row.get("buy_tax") not in (None, "") else "",
        "sellTax": format_tax(row.get("sell_tax")) if row.get("sell_tax") not in (None, "") else "",
        "honeypot": honeypot_text,
        "openSource": "",
        "renounced": "",
        "top10": "",
        "launchpad": str(row.get("launchpad_platform") or row.get("launchpad") or "").strip(),
        "liquidity": liquidity,
        "liquidityText": format_mcap(liquidity) if liquidity else "—",
        "holders": holders,
        "createdAt": created,
    }


def _from_wallet(address: str, wallets: list[dict[str, Any]]) -> dict[str, Any]:
    by_chain: dict[str, dict[str, Any]] = {}
    for row in wallets:
        chain = gmgn_api_chain(str(row.get("chain") or ""))
        if chain and chain not in by_chain:
            by_chain[chain] = row
    ordered: list[dict[str, Any]] = []
    for chain in ("eth", "bsc", "robinhood"):
        if chain in by_chain:
            ordered.append(by_chain[chain])
    for row in wallets:
        if row not in ordered:
            ordered.append(row)
    top = ordered[0] if ordered else {}
    chain = gmgn_api_chain(str(top.get("chain") or ""))
    chains: list[str] = []
    for row in wallets:
        item = gmgn_api_chain(str(row.get("chain") or ""))
        if item and item not in chains:
            chains.append(item)
    name = str(top.get("twitter_name") or top.get("ens") or top.get("remark_name") or "").strip()
    twitter = str(top.get("twitter_username") or "").strip()
    return {
        "address": address,
        "chain": chain,
        "chains": chains,
        "symbol": "",
        "name": name or "钱包地址",
        "kind": "wallet",
        "note": "这是钱包地址，不是代币合约",
        "logo": str(top.get("avatar") or "").strip(),
        "marketCap": 0,
        "marketCapText": "—",
        "priceUsd": "",
        "url": gmgn_address_url(chain, address),
        "source": "gmgn-wallet",
        "twitter": twitter,
        "website": "",
        "telegram": "",
        "buyTax": "",
        "sellTax": "",
        "honeypot": "",
        "openSource": "",
        "renounced": "",
        "top10": "",
        "launchpad": "",
        "liquidity": 0,
        "liquidityText": "—",
        "holders": 0,
        "createdAt": 0,
    }


def _unresolved(address: str, hint: str) -> dict[str, Any]:
    return {
        "address": address,
        "chain": hint,
        "symbol": "",
        "name": "",
        "logo": "",
        "marketCap": 0,
        "marketCapText": "—",
        "priceUsd": "",
        "url": gmgn_token_url(hint or "bsc", address),
        "source": "unresolved",
        "twitter": "",
        "website": "",
        "telegram": "",
        "buyTax": "",
        "sellTax": "",
        "honeypot": "",
        "openSource": "",
        "renounced": "",
        "top10": "",
        "launchpad": "",
        "liquidity": 0,
        "liquidityText": "—",
        "holders": 0,
        "createdAt": 0,
    }


# 免费套餐每秒可用权重 5，每次调用按接口权重扣，见 GMGN OpenAPI 限频规则。
_GMGN_WEIGHTS = {
    "/v1/token/info": 1,
    "/v1/token/security": 1,
    "/v1/token/pool": 1,
    "/v1/token/holders": 5,
    "/v1/token/traders": 5,
    "/v1/market/search": 1,
    # 官方表未列，实测 x-ratelimit-remaining 每次扣 3。
    "/v1/market/token_kline": 3,
}
_CACHE_TTL = 2.0
_CACHE_MAX = 2000


def _search_covers_card(token: dict[str, Any] | None) -> bool:
    """search 已有链、代号和市值时，不再打 token/info。"""
    if not token:
        return False
    if not str(token.get("chain") or "").strip():
        return False
    if not str(token.get("symbol") or "").strip():
        return False
    return _num(token.get("marketCap")) > 0


def _plan_weight() -> int:
    try:
        return max(1, int(os.environ.get("WECHAT_FEED_GMGN_RATE") or 5))
    except ValueError:
        return 5


# 限频预算（免费档每秒权重 5）：
# - 本地按 1.2 秒滑动窗口记账：请求在路上的延迟抖动会让服务端看到的间隔变短，留 20% 余量防 429。
#   总吞吐上限约 5 / 1.2 ≈ 4.2 权重/秒。
# - 后台刷榜每个窗口必须给实时留 2：K 线权重 3，后台最多每窗口 1 条 ≈ 2.5 权重/秒，
#   实时的 search(1)+info(1) 永远随到随走。
_RATE_WINDOW = 1.2
_REALTIME_RESERVE = 2
# 当前协程是否在后台刷榜：只影响限频优先级和 429 处理。
_BACKGROUND: contextvars.ContextVar[bool] = contextvars.ContextVar("gmgn_bg", default=False)


class RateWindow:
    """1 秒滑动窗口内已用权重不超过上限；用响应头的剩余额度校准本地估算。"""

    def __init__(self, limit: int, window: float = 1.0) -> None:
        self.limit = limit
        self.window = window
        self._used: deque[tuple[float, int]] = deque()
        self._lock = asyncio.Lock()

    def _expire(self, now: float) -> int:
        while self._used and now - self._used[0][0] >= self.window:
            self._used.popleft()
        return sum(item[1] for item in self._used)

    async def acquire(self, weight: int, reserve: int = 0) -> None:
        """reserve：本次必须给别人留出的权重。等待在锁外进行，后台排队不挡实时。"""
        weight = min(weight, self.limit)
        reserve = max(0, min(reserve, self.limit - weight))
        while True:
            async with self._lock:
                now = time.monotonic()
                used = self._expire(now)
                if used + weight + reserve <= self.limit:
                    self._used.append((now, weight))
                    return
                wait = self.window - (now - self._used[0][0]) + 0.01 if self._used else 0.05
            await asyncio.sleep(max(0.02, wait))

    def sync(self, remaining: int | None) -> None:
        """服务端说本秒只剩 remaining，本地记账少了就补齐，避免撞 429。"""
        if remaining is None:
            return
        now = time.monotonic()
        used = self._expire(now)
        server_used = self.limit - max(0, int(remaining))
        if server_used > used:
            self._used.append((now, server_used - used))

    def penalize(self, seconds: float) -> None:
        """被 429 后把窗口占满一段时间。"""
        self._used.append((time.monotonic() + seconds - self.window, self.limit))


def _remaining(response: httpx.Response) -> int | None:
    raw = response.headers.get("x-ratelimit-remaining")
    try:
        return int(raw) if raw is not None else None
    except ValueError:
        return None


class TokenIndex:
    def __init__(self, key_provider: Callable[[], str] | None = None) -> None:
        self._key_provider = key_provider or (lambda: "")
        self._cache: dict[str, tuple[float, dict[str, Any] | None]] = {}
        self._lock = asyncio.Lock()
        self._rate = RateWindow(_plan_weight(), window=_RATE_WINDOW)
        self.meter = GmgnMeter(self._rate.limit, _RATE_WINDOW, _REALTIME_RESERVE)
        # 鉴权失败时全部停；限频只停后台，实时退避 1 秒重试。
        self._gmgn_cooldown = 0.0
        self._bg_cooldown = 0.0
        self._live = 0

    def meter_snapshot(self) -> dict[str, Any]:
        return self.meter.snapshot({"background": self._bg_cooldown, "auth": self._gmgn_cooldown})

    def idle(self) -> bool:
        now = time.time()
        return self._live <= 0 and now >= max(self._gmgn_cooldown, self._bg_cooldown)

    def has_key(self) -> bool:
        return bool(str(self._key_provider() or "").strip())

    def _client(self) -> httpx.AsyncClient:
        return http_client()

    def _remember(self, key: str, value: dict[str, Any] | None) -> None:
        now = time.time()
        self._cache[key] = (now, value)
        if len(self._cache) <= _CACHE_MAX:
            return
        for stale in [k for k, (at, _) in self._cache.items() if now - at >= _CACHE_TTL]:
            del self._cache[stale]
        while len(self._cache) > _CACHE_MAX:
            del self._cache[next(iter(self._cache))]

    async def resolve_many(
        self,
        cas: list[dict[str, Any]],
        on_partial: Callable[[list[dict[str, Any]]], None] | None = None,
    ) -> list[dict[str, Any]]:
        if os.environ.get("WECHAT_FEED_DISABLE_TOKEN") == "1":
            return []
        self._live += 1
        try:
            return await self._resolve_many(cas, on_partial)
        finally:
            self._live = max(0, self._live - 1)

    async def _resolve_many(
        self,
        cas: list[dict[str, Any]],
        on_partial: Callable[[list[dict[str, Any]]], None] | None = None,
    ) -> list[dict[str, Any]]:
        slots: list[dict[str, Any] | None] = [None] * len(cas)

        def emit() -> None:
            if on_partial is None:
                return
            ready = [item for item in slots if item is not None]
            if ready:
                on_partial(ready)

        async def one(index: int, item: dict[str, Any]) -> None:
            def partial(token: dict[str, Any]) -> None:
                slots[index] = token
                emit()

            slots[index] = await self.resolve_one(item, on_partial=partial)
            emit()

        await asyncio.gather(*[one(index, item) for index, item in enumerate(cas)])
        return [item for item in slots if item is not None]

    async def quote(
        self,
        address: str,
        chain: str = "",
        since: int = 0,
        last_mcap: float = 0.0,
        last_price: float = 0.0,
        anchors: list[int] | None = None,
    ) -> dict[str, Any] | None:
        """空闲刷新。已定链且有价格锚点时只打 K 线；否则 search 一次把价格写下。

        anchors 是各喊单/各群首喊时间，K 线按它们分段，保证每个喊单人都只算喊单之后。
        """
        if os.environ.get("WECHAT_FEED_DISABLE_TOKEN") == "1":
            return None
        address = str(address or "").strip()
        if not address:
            return None
        # 后台任务：不计入 _live（那是实时解析的计数），限频时给实时让路。
        flag = _BACKGROUND.set(True)
        try:
            chain_norm = _norm_chain(chain)
            if chain_norm and last_price > 0 and last_mcap > 0:
                token: dict[str, Any] = {
                    "address": address,
                    "chain": gmgn_api_chain(chain_norm),
                    "marketCap": last_mcap,
                    "priceUsd": last_price,
                }
                return await self._fill_ath(token, force=True, since=since, anchors=anchors)
            coins, _wallets = await self._search_hits(address, chain_norm)
            if not coins:
                return None
            picked = _best_coin(coins) if len(coins) > 1 else coins[0]
            token = _from_search(picked, address)
            if token:
                token = await self._fill_ath(token, force=True, since=since, anchors=anchors)
            return token
        finally:
            _BACKGROUND.reset(flag)

    async def resolve_one(
        self,
        item: dict[str, Any],
        on_partial: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any] | None:
        address = str(item.get("address") or "").strip()
        if not address:
            return None
        cache_key = address.lower()
        async with self._lock:
            cached = self._cache.get(cache_key)
            if cached and time.time() - cached[0] < _CACHE_TTL:
                return cached[1]
        looked = await self._fetch(
            address,
            str(item.get("chain") or ""),
            bool(item.get("resolved")),
            on_partial=on_partial,
        )
        async with self._lock:
            self._remember(cache_key, looked)
        return looked

    async def _gmgn_call(self, path: str, extra: dict[str, str]) -> dict[str, Any] | None:
        api_key = str(self._key_provider() or "").strip()
        if not api_key:
            return None
        background = _BACKGROUND.get()
        weight = _GMGN_WEIGHTS.get(path, 1)

        def note(outcome: str, **extra: Any) -> None:
            self.meter.record(path, weight, background=background, outcome=outcome, **extra)

        if time.time() < self._gmgn_cooldown:
            note("skipped")
            return None
        last_status = 0
        for attempt in range(2):
            queued = time.monotonic()
            if background:
                # 实时解析在跑就先让开；后台被 429 过的冷却期内不再打。
                while self._live > 0:
                    await asyncio.sleep(0.2)
                if time.time() < self._bg_cooldown:
                    note("skipped")
                    return None
                await self._rate.acquire(weight, reserve=_REALTIME_RESERVE)
            else:
                await self._rate.acquire(weight)
            wait_ms = (time.monotonic() - queued) * 1000
            sent_at = time.monotonic()
            sent_wall = time.time()
            params = {
                "timestamp": str(int(time.time())),
                "client_id": str(uuid.uuid4()),
                **extra,
            }
            try:
                response = await self._client().get(
                    f"https://openapi.gmgn.ai{path}",
                    params=params,
                    headers=_gmgn_headers(api_key),
                )
                last_status = response.status_code
                left = _remaining(response)
                self._rate.sync(left)
                timing = {
                    "status": response.status_code,
                    "latency_ms": (time.monotonic() - sent_at) * 1000,
                    "wait_ms": wait_ms,
                    "remaining": left,
                    "at": sent_wall,
                }
                if response.status_code in {429, 408}:
                    note("limited", **timing)
                    logger.info(
                        "GMGN 限频 {} status={} {}",
                        path,
                        response.status_code,
                        "后台" if background else "实时",
                    )
                    if background:
                        # 后台停 8 秒，实时不受影响。
                        self._rate.penalize(2.0)
                        self._bg_cooldown = time.time() + 8
                        return None
                    # 实时：退避 1 秒重试一次，不让整条链路停摆。
                    self._rate.penalize(1.0)
                    if attempt == 0:
                        await asyncio.sleep(1.0)
                        continue
                    return None
                if response.status_code in {401, 403}:
                    note("auth", **timing)
                    self._gmgn_cooldown = time.time() + 90
                    logger.info("GMGN 鉴权失败 status={}", response.status_code)
                    return None
                if response.status_code >= 400:
                    detail = ""
                    try:
                        payload = response.json()
                        if isinstance(payload, dict):
                            detail = str(payload.get("message") or payload.get("msg") or "")[:80]
                    except Exception:
                        detail = response.text[:80]
                    note("error", **timing)
                    logger.info("GMGN {} status={} {}", path, response.status_code, detail)
                    if "timestamp" in detail.lower():
                        await asyncio.sleep(0.2)
                        continue
                    return None
                loaded = response.json()
                code = loaded.get("code") if isinstance(loaded, dict) else None
                note("ok" if code in (None, 0, "0") else "biz", **timing)
                unwrapped = _unwrap(loaded)
                return (
                    unwrapped
                    if unwrapped is not None
                    else (loaded if isinstance(loaded, dict) else None)
                )
            except Exception as exc:
                note(
                    "exception",
                    latency_ms=(time.monotonic() - sent_at) * 1000,
                    wait_ms=wait_ms,
                    at=sent_wall,
                )
                logger.info("GMGN {} 失败: {}", path, type(exc).__name__)
                return None
        if last_status:
            logger.info("GMGN {} 最终失败 status={}", path, last_status)
        return None

    async def _gmgn_get(self, path: str, chain: str, address: str) -> dict[str, Any] | None:
        return await self._gmgn_call(path, {"chain": gmgn_api_chain(chain), "address": address})

    async def _gmgn_search(self, address: str, chain: str = "") -> dict[str, Any] | None:
        extra = {"q": address}
        if chain:
            extra["chain"] = gmgn_api_chain(chain)
        return await self._gmgn_call("/v1/market/search", extra)

    async def _search_hits(
        self, address: str, hint: str = ""
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        hint_norm = _norm_chain(hint)
        coins: list[dict[str, Any]] = []
        wallets: list[dict[str, Any]] = []
        seen_coins: set[tuple[str, str]] = set()
        seen_wallets: set[tuple[str, str]] = set()

        def absorb(payload: object) -> None:
            if not isinstance(payload, dict):
                return
            more_coins, more_wallets = parse_search(payload, address)
            for row in more_coins:
                key = (
                    gmgn_api_chain(str(row.get("chain") or row.get("network") or "")),
                    str(row.get("address") or "").lower(),
                )
                if key in seen_coins:
                    continue
                seen_coins.add(key)
                coins.append(row)
            for row in more_wallets:
                key = (
                    gmgn_api_chain(str(row.get("chain") or "")),
                    str(row.get("address") or "").lower(),
                )
                if key in seen_wallets:
                    continue
                seen_wallets.add(key)
                wallets.append(row)

        # 官方 OpenAPI：不带 chain 的 search 会全链反查，coins[].chain 就是链。
        first = await self._gmgn_search(address, hint_norm)
        absorb(first)
        if coins or wallets or hint_norm or _kind_of(address) != "evm":
            return coins, wallets
        gathered = await asyncio.gather(
            *[self._gmgn_search(address, chain) for chain in _EVM_SEARCH_CHAINS],
            return_exceptions=True,
        )
        for item in gathered:
            absorb(item)
        return coins, wallets

    async def _gmgn_pair(self, chain: str, address: str) -> dict[str, Any] | None:
        info, security = await asyncio.gather(
            self._gmgn_get("/v1/token/info", chain, address),
            self._gmgn_get("/v1/token/security", chain, address),
        )
        if not isinstance(info, dict) or not (info.get("symbol") or info.get("address")):
            return None
        return _from_gmgn(info, security if isinstance(security, dict) else None, chain, address)

    async def _kline_fetch(
        self,
        chain: str,
        address: str,
        resolution: str,
        start: int,
        now: int,
        current_mcap: float,
        current_price: float,
    ) -> tuple[list[tuple[int, float]], float, dict[str, Any] | None]:
        payload = await self._gmgn_call(
            "/v1/market/token_kline",
            {
                "chain": gmgn_api_chain(chain),
                "address": address,
                "resolution": resolution,
                "from": str(start * 1000),
                "to": str(now * 1000),
            },
        )
        bars = kline_bars(payload, current_mcap, current_price)
        return bars, kline_last_close(payload), payload

    async def _since_bars(
        self,
        chain: str,
        address: str,
        anchors: list[int],
        now: int,
        current_mcap: float,
        current_price: float,
    ) -> tuple[list[tuple[int, float]], float, bool]:
        """按锚点分段拉 K 线，合并成一份柱，顺带给出最新收盘价。

        第三项表示每段都拿到了响应（空列表也算拿到，说明这段时间没成交）。
        """
        groups: list[list[tuple[int, float]]] = []
        last_close = 0.0
        last_end = 0
        for resolution, lo, hi in kline_plan(anchors, now):
            bars, close, payload = await self._kline_fetch(
                chain, address, resolution, lo, hi, current_mcap, current_price
            )
            if payload is None:
                # 任一段失败（限频/鉴权）就整份作废：不推进水位线，下次重拉。
                return [], 0.0, False
            groups.append(bars)
            if close > 0 and hi >= last_end:
                last_close = close
                last_end = hi
        return merge_bars(*groups), last_close, True

    async def _fill_ath(
        self,
        token: dict[str, Any],
        *,
        force: bool = False,
        since: int = 0,
        anchors: list[int] | None = None,
    ) -> dict[str, Any]:
        existing = _num(token.get("athMarketCap"))
        if existing > 0 and not force:
            return token
        chain = str(token.get("chain") or "")
        address = str(token.get("address") or "")
        if not chain or not address or chain == "_":
            return token
        created = int(token.get("createdAt") or 0)
        now = int(time.time())
        current_mcap = _num(token.get("marketCap"))
        current_price = _num(token.get("priceUsd") or token.get("price"))
        points = [int(a) for a in (anchors or []) if int(a or 0) > 0]
        if since:
            points.append(int(since))
        if points:
            # 喊单后的高点：分段细粒度，粗柱不跨喊单时间。
            start = min(points)
            bars, last_close, ok = await self._since_bars(
                chain, address, points, now, current_mcap, current_price
            )
            token["klineBars"] = bars
            # klineOk/klineTo 给增量刷新推进水位线用。
            token["klineOk"] = ok
            token["klineTo"] = now
        else:
            # 新币首次解析只要历史 ATH 展示，一次请求即可。
            start = created if created > 0 else now - 180 * 86400
            resolution = kline_resolution(start, now)
            span = _KLINE_SPAN.get(resolution, 0)
            bars, last_close, _payload = await self._kline_fetch(
                chain, address, resolution, max(0, start - span), now, current_mcap, current_price
            )
            start = 0
        peak = peak_since(bars, start)
        if last_close > 0 and current_price > 0 and current_mcap > 0:
            token["marketCap"] = current_mcap * (last_close / current_price)
            token["priceUsd"] = last_close
        if force:
            chosen = max(peak, _num(token.get("marketCap")), current_mcap)
        else:
            chosen = max(existing, peak, _num(token.get("marketCap")), current_mcap)
        if chosen > 0:
            token["athMarketCap"] = chosen
        return token

    async def _dexscreener(self, address: str, hint: str) -> dict[str, Any] | None:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{address}"
        try:
            response = await self._client().get(
                url,
                headers={"user-agent": "wechat-feed/0.2"},
                timeout=2.5,
            )
            response.raise_for_status()
            loaded = response.json()
        except Exception as exc:
            logger.info("DexScreener 查询失败: {}", type(exc).__name__)
            return None
        payload = loaded if isinstance(loaded, dict) else {}
        pairs = [row for row in (payload.get("pairs") or []) if isinstance(row, dict)]
        hint_norm = _norm_chain(hint)
        if hint_norm:
            filtered = [
                row for row in pairs if _norm_chain(str(row.get("chainId") or "")) == hint_norm
            ]
            if filtered:
                pairs = filtered
        pairs.sort(
            key=lambda row: float((row.get("liquidity") or {}).get("usd") or 0),
            reverse=True,
        )
        if not pairs:
            return None
        return _from_pair(pairs[0], address, hint_norm)

    async def _fetch(
        self,
        address: str,
        hint: str,
        resolved: bool,  # 保留入参，定链改走 search
        on_partial: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any] | None:
        hint_norm = _norm_chain(hint)

        async def enrich(chain: str, seed: dict[str, Any] | None) -> dict[str, Any] | None:
            if seed and on_partial:
                on_partial(seed)
            if seed and _search_covers_card(seed):
                return seed
            if not chain:
                return seed
            gmgn = await self._gmgn_pair(chain, address)
            if gmgn and seed:
                if not gmgn.get("marketCap") and seed.get("marketCap"):
                    gmgn["marketCap"] = seed["marketCap"]
                    gmgn["marketCapText"] = seed.get("marketCapText") or format_mcap(
                        float(seed["marketCap"] or 0)
                    )
            return gmgn or seed

        coins, wallets = await self._search_hits(address, hint_norm)
        if coins:
            coin = _best_coin(coins)
            chain = gmgn_api_chain(str(coin.get("chain") or coin.get("network") or hint_norm))
            logger.info("GMGN search 命中 {} -> {}", address[:10], chain or "?")
            found = await enrich(chain, _from_search(coin, address))
            if found:
                if not _num(found.get("athMarketCap")):
                    found = await self._fill_ath(found)
                return found
        if wallets:
            card = _from_wallet(address, wallets)
            logger.info(
                "GMGN search 钱包 {} chains={}",
                address[:10],
                ",".join(card.get("chains") or []),
            )
            if on_partial:
                on_partial(card)
            return card

        dex = await self._dexscreener(address, hint_norm)
        if dex:
            chain = str(dex.get("chain") or "")
            found = await enrich(chain, dex)
            if found:
                if not _num(found.get("athMarketCap")):
                    found = await self._fill_ath(found)
                return found
        return _unresolved(address, hint)
