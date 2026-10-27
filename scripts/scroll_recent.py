# ruff: noqa: E501, E741  (嵌入的 JS 片段保持单行，方便和 DevTools 对照)
"""加载真实插件，打开「最近 CA」面板，在 .rc-list 里滚动：
1) 滚动瓶颈截图；2) 滚动期间主线程绘制量；3) 每个滚动容器的背景是否不透明。
用法：uv run --with playwright python scripts/scroll_recent.py [dpr] [注入 CSS]
"""

import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
EXT = str(ROOT / "extension")
URL = "https://gmgn.ai/follow?target=xTracker"
SHOTS = ROOT / ".scratch" / "shots"


def shadow_call(cdp, fn, arg=None):
    doc = cdp.send("DOM.getDocument", {"depth": -1, "pierce": True})
    roots: list[dict] = []

    def collect(node):
        roots.extend(node.get("shadowRoots", []) or [])
        for child in (node.get("children", []) or []) + (node.get("shadowRoots", []) or []):
            collect(child)

    collect(doc["root"])
    for shadow in roots:
        obj = cdp.send("DOM.resolveNode", {"nodeId": shadow["nodeId"]})["object"]
        ok = cdp.send(
            "Runtime.callFunctionOn",
            {
                "objectId": obj["objectId"],
                "functionDeclaration": "function(){return Boolean(this.querySelector('.rc-panel'))}",
                "returnByValue": True,
            },
        )["result"].get("value")
        if not ok:
            continue
        return cdp.send(
            "Runtime.callFunctionOn",
            {
                "objectId": obj["objectId"],
                "functionDeclaration": fn,
                "arguments": [{"value": arg}],
                "returnByValue": True,
            },
        )["result"].get("value")
    raise RuntimeError("没找到浮窗 shadow root")


def run() -> int:
    dpr = float(sys.argv[1]) if len(sys.argv) > 1 else 1.0
    css = sys.argv[2] if len(sys.argv) > 2 else ""
    tag = f"rc-dpr{dpr}" + ("-css" if css else "")
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            tempfile.mkdtemp(),
            headless=False,
            viewport={"width": 1440, "height": 900},
            device_scale_factor=dpr,
            args=[f"--disable-extensions-except={EXT}", f"--load-extension={EXT}"],
        )
        worker = (
            ctx.service_workers[0] if ctx.service_workers else ctx.wait_for_event("serviceworker")
        )
        worker.evaluate(
            "() => chrome.storage.local.set({ floatPanel: true, floatRect: { width: 420, height: 760, right: 16, top: 70 } })"
        )
        time.sleep(1)
        page = ctx.new_page()
        page.goto(URL, wait_until="domcontentloaded")
        page.wait_for_timeout(10000)
        cdp = ctx.new_cdp_session(page)
        if css:
            shadow_call(
                cdp,
                "function(css){const s=document.createElement('style');s.textContent=css;this.append(s);}",
                css,
            )
        shadow_call(cdp, "function(){this.querySelector('[data-recent]').click()}")
        page.wait_for_timeout(4000)
        info = shadow_call(
            cdp,
            """function(){
              const out = [];
              for (const el of this.querySelectorAll('*')) {
                const s = getComputedStyle(el);
                if (!/(auto|scroll)/.test(s.overflowY) || el.scrollHeight <= el.clientHeight) continue;
                const r = el.getBoundingClientRect();
                out.push({cls: el.className, bg: s.backgroundColor, w: Math.round(r.width),
                          h: Math.round(r.height), scrollH: el.scrollHeight,
                          x: r.left + r.width / 2, y: r.top + r.height / 2});
              }
              return out; }""",
        )
        for item in info:
            print(
                f"[{tag}] 滚动容器 .{item['cls']} 背景 {item['bg']} {item['w']}x{item['h']} 内容高 {item['scrollH']}"
            )
        target = next((i for i in info if "rc-list" in i["cls"]), None)
        if not target:
            print("面板里没有可滚动的 .rc-list")
            ctx.close()
            return 1
        cdp.send("Overlay.enable")
        cdp.send("Overlay.setShowScrollBottleneckRects", {"show": True})
        page.wait_for_timeout(600)
        SHOTS.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(SHOTS / f"scroll_bottleneck_{tag}.png"))
        cdp.send("Overlay.setShowScrollBottleneckRects", {"show": False})

        chunks: list[dict] = []
        done = {"v": False}
        cdp.on("Tracing.dataCollected", lambda e: chunks.extend(e["value"]))
        cdp.on("Tracing.tracingComplete", lambda e: done.__setitem__("v", True))
        cdp.send(
            "Tracing.start",
            {
                "categories": "devtools.timeline,disabled-by-default-devtools.timeline",
                "transferMode": "ReportEvents",
            },
        )
        page.mouse.move(target["x"], target["y"])
        for step in range(160):
            page.mouse.wheel(0, 100 if (step // 40) % 2 == 0 else -100)
            page.wait_for_timeout(16)
        cdp.send("Tracing.end")
        while not done["v"]:
            page.wait_for_timeout(100)
        names: dict = {}
        for e in chunks:
            if e.get("ph") == "M" and e.get("name") == "thread_name":
                names[(e["pid"], e["tid"])] = e["args"]["name"]
        main = Counter()
        for e in chunks:
            if e.get("ph") == "X" and names.get((e["pid"], e["tid"])) == "CrRendererMain":
                main[e["name"]] += e.get("dur", 0) / 1000
        print(
            f"[{tag}] 面板滚动 160 次滚轮期间主线程: "
            + "，".join(
                f"{k} {main[k]:.0f}ms"
                for k in ("Paint", "PrePaint", "Layout", "UpdateLayoutTree", "Layerize", "RunTask")
            )
        )
        ctx.close()
    return 0


if __name__ == "__main__":
    sys.exit(run())
