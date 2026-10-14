"""进程内复用的 httpx 客户端，避免每次请求重新握手 TLS。"""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import urljoin

import httpx

_LIMITS = httpx.Limits(
    max_keepalive_connections=20,
    max_connections=40,
    keepalive_expiry=60.0,
)
_REDIRECTS = {301, 302, 303, 307, 308}
_http: httpx.AsyncClient | None = None


class HopDenied(Exception):
    """跳转后的地址不在白名单里。"""

    def __init__(self, url: str) -> None:
        self.url = url
        super().__init__(url)


def client() -> httpx.AsyncClient:
    global _http
    if _http is None or _http.is_closed:
        _http = httpx.AsyncClient(
            timeout=httpx.Timeout(10.0, connect=3.0),
            follow_redirects=False,
            trust_env=False,
            limits=_LIMITS,
        )
    return _http


async def get_hops(
    url: str,
    allow: Callable[[str], bool],
    *,
    timeout: float = 8.0,
    headers: dict[str, str] | None = None,
    max_hops: int = 4,
) -> httpx.Response:
    """GET 远程资源；每一跳都要过 allow，避免开放跳转到内网。"""
    current = str(url or "").strip()
    for _ in range(max(1, max_hops) + 1):
        if not allow(current):
            raise HopDenied(current)
        response = await client().get(current, timeout=timeout, headers=headers)
        if response.status_code not in _REDIRECTS:
            return response
        location = str(response.headers.get("location") or "").strip()
        if not location:
            return response
        current = urljoin(current, location)
    raise HopDenied(current)


async def aclose() -> None:
    global _http
    if _http is None:
        return
    await _http.aclose()
    _http = None
