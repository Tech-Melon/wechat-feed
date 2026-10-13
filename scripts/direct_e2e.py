"""在 8799 端口起一个临时实例跑直读模式，不碰正在运行的 8765 服务。

上游指向没人监听的端口，模拟 WDA 已关闭：uv run python scripts/direct_e2e.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
PORT = 8799
DEAD_UPSTREAM = "http://127.0.0.1:10399"
OUT = ROOT / "data" / "e2e_out.txt"


def get(path: str):
    with urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=30) as response:
        body = response.read()
        kind = response.headers.get("content-type", "")
    return json.loads(body) if "json" in kind else (kind, len(body))


def main() -> int:
    home = Path(tempfile.mkdtemp(prefix="wf-e2e-"))
    settings = json.loads((ROOT / "data" / "settings.json").read_text(encoding="utf-8"))
    settings["upstream_base"] = DEAD_UPSTREAM
    (home / "settings.json").write_text(json.dumps(settings, ensure_ascii=False), encoding="utf-8")
    shutil.copy(ROOT / "data" / "direct.json", home / "direct.json")
    env = {**os.environ, "WECHAT_FEED_HOME": str(home), "WECHAT_FEED_ROOT": str(ROOT)}
    code = (
        "import uvicorn; from wechat_feed.server import create_app; "
        f"uvicorn.run(create_app(), host='127.0.0.1', port={PORT}, log_config=None)"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", code], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )
    lines: list[str] = []
    try:
        for _ in range(60):
            time.sleep(0.5)
            try:
                health = get("/api/health")
                if health.get("source") == "direct" and health.get("stored"):
                    break
            except Exception:
                continue
        time.sleep(3)
        health = get("/api/health")
        lines.append(f"health source={health['source']} stored={health['stored']}")
        lines.append(f"status {health['runtime']['upstream']} {health['runtime']['detail']}")
        page = get("/api/messages?limit=5")
        for card in page["messages"]:
            lines.append(
                f"card {card['id'][:40]} {card['renderType']} {card['senderName']!r} "
                f"{card['displayGroup']!r} {card['text'][:30]!r} quote={card['quoteText'][:20]!r}"
            )
        groups = get("/api/groups")["groups"]
        lines.append(f"groups {len(groups)} first={groups[:3]}")
        sender = page["messages"][0]["senderUsername"] if page["messages"] else "wxid_x"
        lines.append(f"avatar {get('/api/avatar?username=' + sender)}")
        lines.append(f"key file exists {(home / 'direct.json').exists()}")

        # venv 的 python.exe 是启动器，真正干活的是监听端口的子进程。
        owner = (
            f"(Get-NetTCPConnection -LocalPort {PORT} -State Listen | Select-Object -First 1)"
            ".OwningProcess"
        )

        def probe(field: str) -> float:
            done = subprocess.run(
                ["powershell", "-NoProfile", "-Command", f"(Get-Process -Id {owner}).{field}"],
                capture_output=True,
                text=True,
            )
            return float(done.stdout.strip() or 0)

        before = probe("CPU")
        time.sleep(30)
        after = probe("CPU")
        lines.append(f"idle cpu over 30s: {(after - before) / 30 * 100:.2f}% of one core")
        lines.append(f"memory: {probe('WorkingSet64') / 1048576:.0f} MB")
        health = get("/api/health")
        lines.append(
            f"after 30s stored={health['stored']} lastEventAt={health['runtime']['lastEventAt']}"
        )
    finally:
        proc.terminate()
        try:
            out, _ = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate()
        log = out.decode("utf-8", "replace").splitlines()
        lines.append("--- server log tail")
        lines.extend(log[-25:])
        shutil.rmtree(home, ignore_errors=True)
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"结果写在 {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
