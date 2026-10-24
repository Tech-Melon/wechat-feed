"""本机配置。只允许回环地址上的上游，避免把群消息接到外网服务。"""

from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

GROUP_LIMIT = 50
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost"}


def project_root() -> Path:
    override = os.environ.get("WECHAT_FEED_ROOT", "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2]


def data_dir() -> Path:
    override = os.environ.get("WECHAT_FEED_HOME", "").strip()
    if override:
        return Path(override)
    return project_root() / "data"


def default_settings() -> dict[str, Any]:
    return {
        "upstream_base": "http://127.0.0.1:10392",
        "groups": [],
        "gmgn_overlay": False,
        "float_panel": False,
        "blocked_groups": [],
        "blocked_users": [],
        "user_remarks": {},
        "group_remarks": {},
        "starred_users": [],
        "star_colors": {},
        "group_users": {},
        "ca_only": False,
        "gmgn_ca_only": False,
        "float_ca_only": False,
        "all_feed_groups": [],
        "ca_feed_groups": [],
        "gmgn_api_key": "",
        "mcap_format": "cn",
        "ca_board": True,
    }


def validate_upstream_base(value: str) -> str:
    text = str(value or "").strip().rstrip("/")
    parsed = urlsplit(text)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "http" or host not in _LOOPBACK_HOSTS:
        raise ValueError("上游只允许 http://127.0.0.1 或 http://localhost")
    if parsed.username or parsed.password or parsed.path not in {"", "/"}:
        raise ValueError("上游地址只能是本机源，不能带路径、账号或查询参数")
    if parsed.port is None:
        raise ValueError("上游地址需要带端口，例如 http://127.0.0.1:10392")
    return f"http://{host}:{parsed.port}"


def validate_group_id(value: str) -> str:
    text = str(value or "").strip()
    if len(text) < 12 or len(text) > 96 or not text.endswith("@chatroom"):
        raise ValueError("群 ID 需要是微信群会话，形如 12345678@chatroom")
    local = text[: -len("@chatroom")]
    if not local or any(char.isspace() or char in "@/\\#" for char in local):
        raise ValueError("群 ID 格式不正确")
    return text


def _clean_id_list(value: Any, *, limit: int, label: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{label}必须是数组")
    if len(value) > limit:
        raise ValueError(f"{label}最多 {limit} 条")
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in value:
        group_id = validate_group_id(str(item))
        if group_id in seen:
            continue
        seen.add(group_id)
        cleaned.append(group_id)
    return cleaned


def _clean_user_list(value: Any, *, limit: int, label: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{label}必须是数组")
    if len(value) > limit:
        raise ValueError(f"{label}最多 {limit} 条")
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in value:
        ident = str(item or "").strip()
        if not ident or len(ident) > 80 or any(char in ident for char in "/\\?#"):
            raise ValueError(f"{label}的用户名不合法")
        if ident in seen:
            continue
        seen.add(ident)
        cleaned.append(ident)
    return cleaned


_STAR_COLORS = {
    "amber",
    "red",
    "rose",
    "violet",
    "indigo",
    "cyan",
    "green",
    "lime",
    "orange",
    "slate",
}


def _clean_group_users(value: Any) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        raise ValueError("群成员监听必须是对象")
    if len(value) > GROUP_LIMIT:
        raise ValueError(f"群成员监听最多 {GROUP_LIMIT} 个群")
    cleaned: dict[str, list[str]] = {}
    for key, users in value.items():
        group_id = validate_group_id(str(key))
        cleaned[group_id] = _clean_user_list(users, limit=200, label="群监听用户")
    return cleaned


def _clean_star_colors(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError("星标颜色必须是对象")
    if len(value) > 80:
        raise ValueError("星标颜色最多 80 条")
    cleaned: dict[str, str] = {}
    for key, color in value.items():
        ident = str(key or "").strip()
        name = str(color or "").strip()
        if not ident or len(ident) > 80 or any(char in ident for char in "/\\?#"):
            raise ValueError("星标颜色的用户名不合法")
        if name not in _STAR_COLORS:
            raise ValueError("星标颜色不在预设里")
        cleaned[ident] = name
    return cleaned


def _clean_remarks(value: Any, *, label: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError(f"{label}必须是对象")
    if len(value) > 300:
        raise ValueError(f"{label}条数过多")
    cleaned: dict[str, str] = {}
    for key, name in value.items():
        ident = str(key or "").strip()
        text = str(name or "").strip()
        if not ident or len(ident) > 96 or any(char in ident for char in "/\\?#"):
            raise ValueError(f"{label}的键不合法")
        if len(text) > 40:
            raise ValueError("备注最多 40 个字")
        if text:
            cleaned[ident] = text
    return cleaned


def _clean_group(item: Any) -> dict[str, str]:
    if isinstance(item, str):
        return {"id": validate_group_id(item), "name": ""}
    if not isinstance(item, dict):
        raise ValueError("群配置必须是对象或群 ID")
    name = str(item.get("name") or "").strip()
    if len(name) > 80:
        raise ValueError("群名称过长")
    return {"id": validate_group_id(str(item.get("id") or "")), "name": name}


def normalize_settings(raw: dict[str, Any]) -> dict[str, Any]:
    merged = default_settings()
    if not isinstance(raw, dict):
        raise ValueError("配置必须是对象")
    if "upstream_base" in raw:
        merged["upstream_base"] = validate_upstream_base(str(raw["upstream_base"]))
    if "gmgn_overlay" in raw:
        if not isinstance(raw["gmgn_overlay"], bool):
            raise ValueError("gmgn.ai 显示开关必须是布尔值")
        merged["gmgn_overlay"] = raw["gmgn_overlay"]
    if "float_panel" in raw:
        if not isinstance(raw["float_panel"], bool):
            raise ValueError("网页浮窗开关必须是布尔值")
        merged["float_panel"] = raw["float_panel"]
    if "ca_only" in raw:
        if not isinstance(raw["ca_only"], bool):
            raise ValueError("只看 CA 开关必须是布尔值")
        merged["ca_only"] = raw["ca_only"]
    if "gmgn_ca_only" in raw:
        if not isinstance(raw["gmgn_ca_only"], bool):
            raise ValueError("gmgn 只看 CA 开关必须是布尔值")
        merged["gmgn_ca_only"] = raw["gmgn_ca_only"]
    elif "ca_only" in raw:
        merged["gmgn_ca_only"] = merged["ca_only"]
    if "float_ca_only" in raw:
        if not isinstance(raw["float_ca_only"], bool):
            raise ValueError("浮窗只看 CA 开关必须是布尔值")
        merged["float_ca_only"] = raw["float_ca_only"]
    elif "ca_only" in raw:
        merged["float_ca_only"] = merged["ca_only"]
    if "all_feed_groups" in raw:
        merged["all_feed_groups"] = _clean_id_list(
            raw["all_feed_groups"], limit=GROUP_LIMIT, label="全部消息群"
        )
    if "ca_feed_groups" in raw:
        merged["ca_feed_groups"] = _clean_id_list(
            raw["ca_feed_groups"], limit=GROUP_LIMIT, label="只看 CA 群"
        )
    if "gmgn_api_key" in raw:
        key = str(raw.get("gmgn_api_key") or "").strip()
        if key and (not key.startswith("gmgn_") or len(key) > 256):
            raise ValueError("GMGN API Key 格式不正确")
        merged["gmgn_api_key"] = key
    if "mcap_format" in raw:
        fmt = str(raw.get("mcap_format") or "cn").strip().lower()
        if fmt not in {"cn", "en"}:
            raise ValueError("市值格式只能是 cn 或 en")
        merged["mcap_format"] = fmt
    if "ca_board" in raw:
        if not isinstance(raw["ca_board"], bool):
            raise ValueError("CA 发现榜开关必须是布尔值")
        merged["ca_board"] = raw["ca_board"]
    # 旧版的日结算时刻：增量刷新后不再需要，读到就丢掉。
    merged.pop("ca_daily_hour", None)
    if "blocked_groups" in raw:
        merged["blocked_groups"] = _clean_id_list(raw["blocked_groups"], limit=200, label="屏蔽群")
    if "blocked_users" in raw:
        merged["blocked_users"] = _clean_user_list(
            raw["blocked_users"], limit=200, label="屏蔽用户"
        )
    if "user_remarks" in raw:
        merged["user_remarks"] = _clean_remarks(raw["user_remarks"], label="用户备注")
    if "group_remarks" in raw:
        merged["group_remarks"] = _clean_remarks(raw["group_remarks"], label="群备注")
    if "starred_users" in raw:
        merged["starred_users"] = _clean_user_list(raw["starred_users"], limit=80, label="星标用户")
    if "star_colors" in raw:
        merged["star_colors"] = _clean_star_colors(raw["star_colors"])
        starred = set(merged.get("starred_users") or [])
        merged["star_colors"] = {
            user: color for user, color in merged["star_colors"].items() if user in starred
        }
    if "group_users" in raw:
        merged["group_users"] = _clean_group_users(raw["group_users"])
    if "groups" in raw:
        groups = raw["groups"]
        if not isinstance(groups, list):
            raise ValueError("群列表必须是数组")
        if len(groups) > GROUP_LIMIT:
            raise ValueError(f"最多监控 {GROUP_LIMIT} 个群")
        cleaned: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in groups:
            group = _clean_group(item)
            if group["id"] in seen:
                continue
            seen.add(group["id"])
            cleaned.append(group)
        merged["groups"] = cleaned
    unknown = set(raw) - {
        "upstream_base",
        "groups",
        "gmgn_overlay",
        "float_panel",
        "blocked_groups",
        "blocked_users",
        "user_remarks",
        "group_remarks",
        "starred_users",
        "star_colors",
        "group_users",
        "ca_only",
        "gmgn_ca_only",
        "float_ca_only",
        "all_feed_groups",
        "ca_feed_groups",
        "gmgn_api_key",
        "mcap_format",
        "ca_board",
        "ca_daily_hour",
    }
    if unknown:
        raise ValueError("含有不支持的配置项")
    watched = {str(item["id"]) for item in merged.get("groups") or [] if item.get("id")}
    if watched:
        merged["group_users"] = {
            key: users for key, users in (merged.get("group_users") or {}).items() if key in watched
        }
    return merged


def display_group_ids(settings: dict[str, Any], *, ca_only: bool) -> list[str]:
    """当前显示模式勾选的群。空列表表示看监控里的全部群。"""
    key = "ca_feed_groups" if ca_only else "all_feed_groups"
    return [str(item) for item in (settings.get(key) or []) if item]


def group_senders_by_id(settings: dict[str, Any]) -> dict[str, list[str]]:
    """只监听部分用户的群。缺这个键表示全员；空数组表示这个群暂时谁都不显示。"""
    mapping = settings.get("group_users") or {}
    if not isinstance(mapping, dict):
        return {}
    cleaned: dict[str, list[str]] = {}
    for key, users in mapping.items():
        group_id = str(key or "")
        if not group_id:
            continue
        cleaned[group_id] = [str(item) for item in (users or []) if item]
    return cleaned


class SettingsStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._cache = self._load_file()

    def _load_file(self) -> dict[str, Any]:
        if not self.path.exists():
            return default_settings()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("配置文件无法读取") from exc
        return normalize_settings(raw)

    def get(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._cache)

    def replace(self, patch: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            updated = normalize_settings({**self._cache, **patch})
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(updated, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary.replace(self.path)
            self._cache = updated
            return copy.deepcopy(updated)
