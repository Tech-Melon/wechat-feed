"""进程内复用的 httpx 客户端，避免每次请求重新握手 TLS。"""

from __future__ import annotations

import httpx

_LIMITS = httpx.Limits(
    max_keepalive_connections=20,
    max_connections=40,
    keepalive_expiry=60.0,
)
_http: httpx.AsyncClient | None = None


def client() -> httpx.AsyncClient:
    global _http
    if _http is None or _http.is_closed:
        _http = httpx.AsyncClient(
            timeout=httpx.Timeout(10.0, connect=3.0),
            follow_redirects=True,
            trust_env=False,
            limits=_LIMITS,
        )
    return _http


async def aclose() -> None:
    global _http
    if _http is None:
        return
    await _http.aclose()
    _http = None
