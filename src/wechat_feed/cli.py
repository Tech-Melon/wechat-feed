"""交互菜单。默认不拼接命令行参数。"""

from __future__ import annotations

import sys

from loguru import logger

from .config import SettingsStore, data_dir, validate_group_id, validate_upstream_base
from .server import serve

logger.remove()
if sys.stderr is not None:
    logger.add(sys.stderr, format="<green>{time:HH:mm:ss}</green> | {message}", level="INFO")
else:
    # pythonw 后台启动时没有控制台。
    logger.add(
        data_dir() / "logs" / "wechat-feed.log",
        format="{time:MM-DD HH:mm:ss} | {message}",
        level="INFO",
        rotation="2 MB",
        retention=3,
        encoding="utf-8",
    )


def _store() -> SettingsStore:
    return SettingsStore(data_dir() / "settings.json")


def _print_settings(settings: dict) -> None:
    groups = settings.get("groups") or []
    overlay = "开" if settings.get("gmgn_overlay") else "关"
    print(f"上游: {settings['upstream_base']}")
    print(f"gmgn.ai 页面显示: {overlay}")
    if not groups:
        print("监控群: 未指定，接收全部群聊，不接收私聊")
        return
    print("监控群:")
    for index, group in enumerate(groups, start=1):
        name = group.get("name") or "未命名"
        print(f"  {index}. {name}  {group['id']}")


def _ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except EOFError:
        print()
        raise SystemExit(0) from None


def _add_group(store: SettingsStore) -> None:
    group_id = _ask("群 ID（例如 12345678@chatroom）: ")
    name = _ask("显示名称，可直接回车: ")
    try:
        group_id = validate_group_id(group_id)
    except ValueError as exc:
        print(exc)
        return
    settings = store.get()
    groups = [item for item in settings["groups"] if item["id"] != group_id]
    groups.append({"id": group_id, "name": name})
    try:
        store.replace({"groups": groups})
    except ValueError as exc:
        print(exc)
        return
    print("已添加。")


def _remove_group(store: SettingsStore) -> None:
    settings = store.get()
    groups = list(settings["groups"])
    if not groups:
        print("当前没有单独指定的群。")
        return
    _print_settings(settings)
    raw = _ask("要移除的序号: ")
    if not raw.isdigit() or not 1 <= int(raw) <= len(groups):
        print("序号不在范围内。")
        return
    del groups[int(raw) - 1]
    store.replace({"groups": groups})
    print("已移除。")


def _edit_upstream(store: SettingsStore) -> None:
    raw = _ask("本机上游地址（例如 http://127.0.0.1:10392）: ")
    try:
        store.replace({"upstream_base": validate_upstream_base(raw)})
    except ValueError as exc:
        print(exc)
        return
    print("已保存。")


def _toggle_overlay(store: SettingsStore) -> None:
    current = bool(store.get()["gmgn_overlay"])
    store.replace({"gmgn_overlay": not current})
    state = "开" if not current else "关"
    print(f"gmgn.ai 页面显示已改为: {state}")


def menu() -> None:
    store = _store()
    actions = {
        "1": lambda: serve(),
        "2": lambda: _print_settings(store.get()),
        "3": lambda: _edit_upstream(store),
        "4": lambda: _add_group(store),
        "5": lambda: _remove_group(store),
        "6": lambda: _toggle_overlay(store),
        "7": lambda: sys.exit(0),
    }
    while True:
        settings = store.get()
        overlay = "开" if settings["gmgn_overlay"] else "关"
        print()
        print("微信群信息流")
        print("1. 启动本地页面和监控")
        print("2. 查看当前配置")
        print("3. 设置上游地址")
        print("4. 添加监控群")
        print("5. 移除监控群")
        print(f"6. 切换 gmgn.ai 页面显示（当前：{overlay}）")
        print("7. 退出")
        choice = _ask("选择序号: ")
        action = actions.get(choice)
        if action is None:
            print("请输入 1 到 7。")
            continue
        action()


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "serve":
        serve()
        return
    menu()


if __name__ == "__main__":
    main()
