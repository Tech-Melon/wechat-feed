"""开放接口推送示例：在局域网另一台电脑上运行，把群消息推到技术瓜群流。

用法（推送端电脑，只依赖 httpx 和 loguru）：
    set WF_OPEN_URL=http://192.168.1.5:8766
    set WF_OPEN_KEY=wfo_xxx
    uv run --with httpx --with loguru python open_push_example.py

签名规则和字段说明见 docs/开放接口.md。接入自己的机器人时，复制 sign_headers() 和 push() 即可。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import time

import httpx
from loguru import logger

BASE = os.environ.get("WF_OPEN_URL", "http://127.0.0.1:8766").rstrip("/")
KEY = os.environ.get("WF_OPEN_KEY", "")
SAMPLE_CA = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"


def sign_headers(key: str, method: str, path: str, body: bytes) -> dict[str, str]:
    """签名原文 = 时间戳 \\n 方法 \\n 路径 \\n + 原始请求体字节，HMAC-SHA256 后取小写十六进制。"""
    stamp = str(int(time.time()))
    message = f"{stamp}\n{method.upper()}\n{path}\n".encode() + body
    signature = hmac.new(key.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return {"X-WF-Timestamp": stamp, "X-WF-Signature": signature}


def push(platform: str, messages: list[dict], *, retries: int = 3) -> dict:
    """推一批消息（最多 100 条）。网络错误或 429/5xx 时用同样的消息 id 重试，服务端按 id 去重。"""
    path = "/open/v1/messages"
    body = json.dumps({"platform": platform, "messages": messages}, ensure_ascii=False).encode(
        "utf-8"
    )
    delay = 1.0
    for attempt in range(1, retries + 1):
        # 每次重试都重新签名：时间戳超过 ±300 秒会被拒收。
        headers = {"Content-Type": "application/json", **sign_headers(KEY, "POST", path, body)}
        try:
            response = httpx.post(BASE + path, content=body, headers=headers, timeout=10.0)
        except httpx.HTTPError as exc:
            logger.warning("第 {} 次推送失败: {}", attempt, type(exc).__name__)
        else:
            if response.status_code == 200:
                return response.json()
            if response.status_code not in {429, 500, 502, 503}:
                raise RuntimeError(f"HTTP {response.status_code}: {response.text}")
            delay = float(response.headers.get("Retry-After") or delay)
            logger.warning("第 {} 次推送被拒 HTTP {}", attempt, response.status_code)
        time.sleep(delay)
        delay = min(delay * 2, 30.0)
    raise RuntimeError("重试多次仍未成功")


def ping() -> dict:
    path = "/open/v1/ping"
    response = httpx.get(BASE + path, headers=sign_headers(KEY, "GET", path, b""), timeout=10.0)
    response.raise_for_status()
    return response.json()


def sample(text: str) -> dict:
    now_ms = int(time.time() * 1000)
    return {
        "id": str(now_ms),
        "chat": {"id": "-1001234567", "name": "示例 TG 群"},
        "sender": {"id": "998877", "name": "Alice"},
        "time": now_ms,
        "type": "text",
        "text": text,
    }


def main() -> int:
    if not KEY:
        logger.error("先设置环境变量 WF_OPEN_KEY（在本地页「开放接口」里生成）")
        return 1
    menu = {
        "1": ("测试连通和签名（ping）", lambda: ping()),
        "2": ("推一条普通消息", lambda: push("telegram", [sample("来自开放接口的测试消息")])),
        "3": ("推一条带合约的消息", lambda: push("telegram", [sample(f"看下这个 {SAMPLE_CA}")])),
    }
    for number, (label, _action) in menu.items():
        logger.info("{}. {}", number, label)
    choice = input("选择序号：").strip()
    if choice not in menu:
        logger.error("没有这个选项")
        return 1
    try:
        result = menu[choice][1]()
    except (httpx.HTTPError, RuntimeError) as exc:
        logger.error("失败: {}", exc)
        return 1
    logger.info("返回: {}", json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
