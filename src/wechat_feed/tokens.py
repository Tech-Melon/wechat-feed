"""从文本里抽出合约，并用 DexScreener 定链、取 ticker / 市值 / 图标。"""

from __future__ import annotations

import asyncio
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
    matched_wallets = [
        row for row in wallets if str(row.get("address") or "").lower() == needle
    ]
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


def ath_from_kline(payload: dict[str, Any] | None, current_mcap: float = 0.0) -> float:
    if not isinstance(payload, dict):
        return 0.0
    raw_data = payload.get("data")
    data = raw_data if isinstance(raw_data, (dict, list)) else payload
    rows: list[Any] = []
    if isinstance(data, dict):
        raw = data.get("list") or data.get("klines") or data.get("items") or []
        rows = raw if isinstance(raw, list) else []
    elif isinstance(data, list):
        rows = data
    highs: list[float] = []
    for row in rows:
        if isinstance(row, dict):
            mcap = _num(row.get("market_cap") or row.get("mcp") or row.get("usd_market_cap"))
            high = _num(row.get("high") or row.get("h") or row.get("max"))
            highs.append(mcap if mcap > 0 else high)
        elif isinstance(row, (list, tuple)) and len(row) >= 3:
            highs.append(_num(row[2]))
    peak = max(highs) if highs else 0.0
    if peak <= 0:
        return 0.0
    if peak < 1 and current_mcap >= 100:
        return 0.0
    return peak


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
    market_cap = _num(
        row.get("market_cap") or row.get("usd_market_cap") or row.get("mcp")
    )
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
    "/v1/market/token_kline": 2,
}
_CACHE_TTL = 60.0
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


class RateWindow:
    """1 秒滑动窗口内已用权重不超过上限。"""

    def __init__(self, limit: int, window: float = 1.0) -> None:
        self.limit = limit
        self.window = window
        self._used: deque[tuple[float, int]] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self, weight: int) -> None:
        weight = min(weight, self.limit)
        async with self._lock:
            while True:
                now = time.monotonic()
                while self._used and now - self._used[0][0] >= self.window:
                    self._used.popleft()
                if sum(item[1] for item in self._used) + weight <= self.limit:
                    self._used.append((now, weight))
                    return
                await asyncio.sleep(self.window - (now - self._used[0][0]) + 0.01)

    def penalize(self, seconds: float) -> None:
        """被 429 后把窗口占满一段时间。"""
        self._used.append((time.monotonic() + seconds - self.window, self.limit))


class TokenIndex:
    def __init__(self, key_provider: Callable[[], str] | None = None) -> None:
        self._key_provider = key_provider or (lambda: "")
        self._cache: dict[str, tuple[float, dict[str, Any] | None]] = {}
        self._lock = asyncio.Lock()
        self._rate = RateWindow(_plan_weight())
        self._gmgn_cooldown = 0.0
        self._live = 0

    def idle(self) -> bool:
        return self._live <= 0 and time.time() >= self._gmgn_cooldown

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

    async def quote(self, address: str, chain: str = "") -> dict[str, Any] | None:
        """空闲刷新只用 search（权重 1）。缺 ATH 才补 4h K 线。"""
        if os.environ.get("WECHAT_FEED_DISABLE_TOKEN") == "1":
            return None
        address = str(address or "").strip()
        if not address:
            return None
        coins, _wallets = await self._search_hits(address, chain)
        if not coins:
            return None
        picked = _best_coin(coins) if len(coins) > 1 else coins[0]
        token = _from_search(picked, address)
        if token and _num(token.get("athMarketCap")) <= 0:
            token = await self._fill_ath(token)
        return token

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
        if time.time() < self._gmgn_cooldown:
            return None
        last_status = 0
        weight = _GMGN_WEIGHTS.get(path, 1)
        for _attempt in range(2):
            await self._rate.acquire(weight)
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
                if response.status_code in {429, 408}:
                    self._rate.penalize(8.0)
                    self._gmgn_cooldown = max(self._gmgn_cooldown, time.time() + 8)
                    logger.info("GMGN 限频 {} status={}", path, response.status_code)
                    return None
                if response.status_code in {401, 403}:
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
                    logger.info("GMGN {} status={} {}", path, response.status_code, detail)
                    if "timestamp" in detail.lower():
                        await asyncio.sleep(0.2)
                        continue
                    return None
                loaded = response.json()
                unwrapped = _unwrap(loaded)
                return (
                    unwrapped
                    if unwrapped is not None
                    else (loaded if isinstance(loaded, dict) else None)
                )
            except Exception as exc:
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

    async def _fill_ath(self, token: dict[str, Any]) -> dict[str, Any]:
        if _num(token.get("athMarketCap")) > 0:
            return token
        chain = str(token.get("chain") or "")
        address = str(token.get("address") or "")
        if not chain or not address or chain == "_":
            return token
        created = int(token.get("createdAt") or 0)
        now = int(time.time())
        start = created if created > 0 else now - 180 * 86400
        payload = await self._gmgn_call(
            "/v1/market/token_kline",
            {
                "chain": gmgn_api_chain(chain),
                "address": address,
                "resolution": "4h",
                "from": str(start),
                "to": str(now),
            },
        )
        peak = ath_from_kline(payload, _num(token.get("marketCap")))
        if peak <= 0:
            payload = await self._gmgn_call(
                "/v1/market/token_kline",
                {
                    "chain": gmgn_api_chain(chain),
                    "address": address,
                    "resolution": "4h",
                    "from": str(start * 1000),
                    "to": str(now * 1000),
                },
            )
            peak = ath_from_kline(payload, _num(token.get("marketCap")))
        if peak > 0:
            token["athMarketCap"] = peak
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
