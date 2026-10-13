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

import httpx
from loguru import logger

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

_GMGN_FALLBACK = (
    "bsc",
    "base",
    "eth",
    "sol",
    "arbitrum",
    "hyperevm",
    "tron",
    "monad",
    "robinhood",
)


def _norm_chain(value: str | None) -> str:
    text = str(value or "").strip().lower()
    return _CHAIN_ALIAS.get(text, text)


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
    """按地址形态给出 GMGN 探测顺序：Dex 定链优先，再按 kind 收窄。"""
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
    if not resolved:
        for item in _GMGN_FALLBACK:
            add(item)
    return ordered


def chain_from_search(payload: dict[str, Any] | None, address: str) -> str:
    if not isinstance(payload, dict):
        return ""
    coins: Any = payload.get("coins") or payload.get("tokens") or payload.get("list")
    data = payload.get("data")
    if isinstance(data, dict):
        coins = data.get("coins") or data.get("tokens") or data.get("list") or coins
    elif isinstance(data, list):
        coins = data
    if not isinstance(coins, list):
        return ""
    needle = str(address or "").lower()
    fallback = ""
    for row in coins:
        if not isinstance(row, dict):
            continue
        chain = gmgn_api_chain(str(row.get("chain") or row.get("network") or ""))
        if not chain:
            continue
        row_addr = str(row.get("address") or row.get("token_address") or "").lower()
        if needle and row_addr == needle:
            return chain
        if not fallback:
            fallback = chain
    return fallback


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
    logo = ""
    if chain:
        logo = f"https://dd.dexscreener.com/ds-data/tokens/{chain}/{address}.png"
    elif str(info.get("imageUrl") or "").strip():
        logo = str(info.get("imageUrl") or "").strip()
    market_cap = 0.0
    for key in ("marketCap", "fdv"):
        try:
            market_cap = float(pair.get(key) or 0)
        except (TypeError, ValueError):
            market_cap = 0.0
        if market_cap > 0:
            break
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
        "liquidity": 0,
        "liquidityText": "—",
        "holders": 0,
        "createdAt": 0,
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
    if not logo and chain:
        logo = f"https://dd.dexscreener.com/ds-data/tokens/{chain}/{address}.png"
    return {
        "address": address,
        "chain": chain,
        "symbol": str(info.get("symbol") or "").strip() or "TOKEN",
        "name": str(info.get("name") or info.get("symbol") or "").strip(),
        "logo": logo,
        "marketCap": market_cap,
        "marketCapText": format_mcap(market_cap) if market_cap else "—",
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


# 免费套餐每秒可用权重 5，每次调用按接口权重扣，见 GMGN OpenAPI 限频规则。
_GMGN_WEIGHTS = {
    "/v1/token/info": 1,
    "/v1/token/security": 1,
    "/v1/token/pool": 1,
    "/v1/token/holders": 5,
    "/v1/token/traders": 5,
    "/v1/market/search": 1,
}
_CACHE_TTL = 300.0
_CACHE_MAX = 2000


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

    def _remember(self, key: str, value: dict[str, Any] | None) -> None:
        now = time.time()
        self._cache[key] = (now, value)
        if len(self._cache) <= _CACHE_MAX:
            return
        for stale in [k for k, (at, _) in self._cache.items() if now - at >= _CACHE_TTL]:
            del self._cache[stale]
        while len(self._cache) > _CACHE_MAX:
            del self._cache[next(iter(self._cache))]

    async def resolve_many(self, cas: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if os.environ.get("WECHAT_FEED_DISABLE_TOKEN") == "1":
            return []
        result: list[dict[str, Any]] = []
        for item in cas:
            resolved = await self.resolve_one(item)
            if resolved is not None:
                result.append(resolved)
        return result

    async def resolve_one(self, item: dict[str, Any]) -> dict[str, Any] | None:
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
                async with httpx.AsyncClient(
                    timeout=8.0,
                    follow_redirects=True,
                    trust_env=False,
                ) as client:
                    response = await client.get(
                        f"https://openapi.gmgn.ai{path}",
                        params=params,
                        headers=_gmgn_headers(api_key),
                    )
                last_status = response.status_code
                if response.status_code in {429, 408}:
                    self._rate.penalize(1.0)
                    continue
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

    async def _gmgn_search_chain(self, address: str) -> str:
        payload = await self._gmgn_call("/v1/market/search", {"q": address})
        chain = chain_from_search(payload, address)
        if chain:
            logger.info("GMGN search 定链 {} -> {}", address[:10], chain)
        return chain

    async def _fetch(self, address: str, hint: str, resolved: bool) -> dict[str, Any] | None:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{address}"
        payload: dict[str, Any] = {}
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                response = await client.get(url, headers={"user-agent": "wechat-feed/0.2"})
                response.raise_for_status()
                loaded = response.json()
                if isinstance(loaded, dict):
                    payload = loaded
        except Exception as exc:
            logger.info("DexScreener 查询失败: {}", type(exc).__name__)
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
        dex = None
        dex_chain = ""
        if pairs:
            dex = _from_pair(pairs[0], address, hint_norm)
            dex_chain = str(dex.get("chain") or "")
            if dex_chain:
                hint_norm = dex_chain
        chains = candidate_chains(
            address,
            hint_norm,
            resolved=resolved,
            dex_chain=dex_chain,
        )
        if not resolved and not dex_chain:
            searched = await self._gmgn_search_chain(address)
            if searched:
                chains = [searched, *[item for item in chains if item != searched]]
        gmgn_info = None
        used_chain = hint_norm
        for chain in chains[:4]:
            gmgn_info = await self._gmgn_get("/v1/token/info", chain, address)
            if gmgn_info and (gmgn_info.get("symbol") or gmgn_info.get("address")):
                used_chain = chain
                break
            gmgn_info = None
        if gmgn_info:
            security = await self._gmgn_get("/v1/token/security", used_chain, address)
            return _from_gmgn(gmgn_info, security, used_chain, address)
        if dex:
            return dex
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
