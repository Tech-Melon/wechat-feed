# ruff: noqa: E501, E741  (嵌入的 JS 片段保持单行，方便和 DevTools 对照)
"""加载真实插件，查浮窗滚动容器是不是合成线程滚动：
1) Overlay.setShowScrollBottleneckRects 截图（标出「滚动时需要主线程」的区域）
2) LayerTree 里有没有和浮窗 .list 尺寸对应的可滚动合成层
3) 在不同 deviceScaleFactor 下对比主线程每帧的绘制量
用法：uv run --with playwright python scripts/scroll_float.py [dpr] [注入 CSS]
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
FIND_HOST = (
    "[...document.documentElement.children].find(n => n.tagName==='DIV' && "
    "getComputedStyle(n).position==='fixed' && n.getBoundingClientRect().height > 300)"
)


def shadow_call(cdp, fn, arg=None):
    doc = cdp.send("DOM.getDocument", {"depth": -1, "pierce": True})

    roots: list[dict] = []

    def collect(node):
        roots.extend(node.get("shadowRoots", []) or [])
        for child in (node.get("children", []) or []) + (node.get("shadowRoots", []) or []):
            collect(child)

    collect(doc["root"])
    # 浮窗的 shadow root 里有 .wrap > .list；gmgn 自己也可能有别的 shadow root
    for shadow in roots:
        obj = cdp.send("DOM.resolveNode", {"nodeId": shadow["nodeId"]})["object"]
        ok = cdp.send(
            "Runtime.callFunctionOn",
            {
                "objectId": obj["objectId"],
                "functionDeclaration": "function(){return Boolean(this.querySelector('.wrap > .list'))}",
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
    raise RuntimeError(f"没找到浮窗 shadow root（共 {len(roots)} 个）")


def run() -> int:
    dpr = float(sys.argv[1]) if len(sys.argv) > 1 else 1.0
    css = sys.argv[2] if len(sys.argv) > 2 else ""
    tag = f"dpr{dpr}" + ("-css" if css else "")
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
        page.wait_for_function(f"() => Boolean({FIND_HOST})", timeout=30000)
        page.wait_for_timeout(9000)
        cdp = ctx.new_cdp_session(page)
        if css:
            shadow_call(
                cdp,
                "function(css){const s=document.createElement('style');s.textContent=css;this.append(s);}",
                css,
            )
            page.wait_for_timeout(800)
        info = shadow_call(
            cdp,
            """function(){ const l=this.querySelector('.list'); const r=l.getBoundingClientRect();
               return {w:Math.round(r.width), h:Math.round(r.height), bg:getComputedStyle(l).backgroundColor,
                       scrollH:l.scrollHeight, dpr:devicePixelRatio}; }""",
        )
        print(f"[{tag}] 浮窗 .list:", info)

        # 1) 滚动瓶颈叠加层
        cdp.send("Overlay.enable")
        cdp.send("Overlay.setShowScrollBottleneckRects", {"show": True})
        page.wait_for_timeout(600)
        SHOTS.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(SHOTS / f"scroll_bottleneck_{tag}.png"))
        cdp.send("Overlay.setShowScrollBottleneckRects", {"show": False})

        # 2) 合成层：找和 .list 同尺寸、可滚动的层
        layers: list[dict] = []
        cdp.on(
            "LayerTree.layerTreeDidChange",
            lambda e: layers.__setitem__(slice(None), e.get("layers") or []),
        )
        cdp.send("LayerTree.enable")
        page.wait_for_timeout(800)
        scroll_like = [
            (
                round(l["width"]),
                round(l["height"]),
                bool(l.get("scrollRects")),
                l.get("drawsContent"),
            )
            for l in layers
            if abs(l["width"] - info["w"]) < 30 and l["height"] > info["h"] * 1.5
        ]
        print(
            f"[{tag}] 与 .list 同宽、高于视口的合成层（即可滚动内容层）: {scroll_like or '无 → 没有走合成线程滚动'}"
        )
        cdp.send("LayerTree.disable")

        # 3) 滚动中主线程的绘制量
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
        r = page.evaluate(
            f"() => {{ const b={FIND_HOST}.getBoundingClientRect(); return {{x:b.left+b.width/2,y:b.top+b.height/2}}; }}"
        )
        page.mouse.move(r["x"], r["y"])
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
            f"[{tag}] 滚动 160 次滚轮期间主线程: "
            + "，".join(
                f"{k} {main[k]:.0f}ms"
                for k in ("Paint", "PrePaint", "Layout", "UpdateLayoutTree", "Layerize", "RunTask")
            )
        )
        ctx.close()
    return 0


if __name__ == "__main__":
    sys.exit(run())
