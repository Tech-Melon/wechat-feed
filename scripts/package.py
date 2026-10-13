"""打一个可以发给别人的压缩包：uv run python scripts/package.py

只放源码、插件和文档；data/（聊天缓存、库密钥、Key）和 .venv 一律不进包。
"""

import json
import re
import sys
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ITEMS = (
    "src",
    "extension",
    "web",
    "scripts",
    "tests",
    "docs",
    "pyproject.toml",
    "uv.lock",
    "README.md",
    "wechat-feed.cmd",
    ".gitignore",
)
SKIP_DIRS = {"__pycache__", ".pytest_cache", ".ruff_cache"}
PRIVATE = {"direct.json", "settings.json", "feed.db", "feed.db-wal", "feed.db-shm"}


def versions() -> dict[str, str]:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    manifest = json.loads((ROOT / "extension" / "manifest.json").read_text(encoding="utf-8"))
    init = (ROOT / "src" / "wechat_feed" / "__init__.py").read_text(encoding="utf-8")
    index = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    return {
        "pyproject.toml": project["project"]["version"],
        "extension/manifest.json": manifest["version"],
        "__init__.py": re.search(r'__version__ = "([^"]+)"', init).group(1),
        "web/index.html": re.search(r"feed\.js\?v=([\w.]+)", index).group(1),
    }


def files() -> list[Path]:
    picked: list[Path] = []
    for item in ITEMS:
        path = ROOT / item
        if path.is_file():
            picked.append(path)
            continue
        for child in sorted(path.rglob("*")):
            if not child.is_file() or SKIP_DIRS & set(child.relative_to(ROOT).parts):
                continue
            if child.suffix == ".pyc" or child.name.startswith("_run_"):
                continue
            picked.append(child)
    return picked


def main() -> int:
    found = versions()
    if len(set(found.values())) != 1:
        print("版本号不一致：", found)
        return 1
    version = next(iter(found.values()))
    picked = files()
    leaks = [path for path in picked if path.name in PRIVATE]
    if leaks:
        print("包里混进了本机数据：", leaks)
        return 1
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    target = dist / f"wechat-feed-{version}.zip"
    base = f"wechat-feed-{version}"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in picked:
            archive.write(path, f"{base}/{path.relative_to(ROOT).as_posix()}")
    print(f"已生成 {target}（{len(picked)} 个文件，{target.stat().st_size // 1024} KB）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
