(() => {
  // 静态注入和白名单动态注入可能落在同一页面上。
  if (globalThis.__wechatFeedLoaded) return;
  globalThis.__wechatFeedLoaded = true;
  const API = "http://127.0.0.1:8765";
  const DEFAULT_FLOAT_SITES = ["https://gmgn.ai/follow?target=xTracker"];
  let floatSites = DEFAULT_FLOAT_SITES;
  let watermark = "";
  let lastHref = location.href;
  const cards = new Map();
  let settings = {
    gmgn_overlay: false,
    float_panel: false,
    ca_only: false,
    user_remarks: {},
    group_remarks: {},
    starred_users: [],
    blocked_groups: [],
  };
  let port = null;
  let hasMore = false;
  let oldest = 0;
  let oldestReceived = 0;
  let collapsed = false;
  let floatHost = null;
  let reconnectTimer = 0;
  let snapshotPending = false;
  let streamTimer = 0;
  let streamPoll = 0;
  let streamList = null;
  let streamObservers = [];
  let dialogHost = null;
  let imagePreview = null;

  const avatarCache = new Map();
  const MIN_FLOAT_W = 280;
  const MIN_FLOAT_H = 220;
  const TOP_SLOT_LIMIT = 20;

  function isGmgn() {
    return location.hostname === "gmgn.ai";
  }

  function isFollowPage() {
    return isGmgn() && location.pathname.startsWith("/follow");
  }

  function overlayOn() {
    return Boolean(settings.gmgn_overlay);
  }

  function floatOn() {
    return Boolean(settings.float_panel);
  }

  // 协议、域名、路径一致，且白名单里写出的查询参数都相同；当前网址多出的参数不影响。
  function urlMatches(pattern, href) {
    let want;
    let got;
    try {
      want = new URL(pattern);
      got = new URL(href);
    } catch (error) {
      return false;
    }
    if (want.origin !== got.origin) return false;
    const trim = (path) => path.replace(/\/+$/, "") || "/";
    if (trim(want.pathname) !== trim(got.pathname)) return false;
    for (const [key, value] of want.searchParams) {
      if (got.searchParams.get(key) !== value) return false;
    }
    return true;
  }

  function floatAllowed() {
    return floatSites.some((site) => urlMatches(site, location.href));
  }

  function floatWanted() {
    return floatOn() && floatAllowed();
  }

  function send(type, payload = {}, attempt = 0) {
    return new Promise((resolve) => {
      try {
        chrome.runtime.sendMessage({ type, ...payload }, (result) => {
          const err = chrome.runtime.lastError;
          if (err) {
            if (attempt < 2) {
              setTimeout(() => send(type, payload, attempt + 1).then(resolve), 160 * (attempt + 1));
              return;
            }
            resolve({ ok: false, detail: err.message });
            return;
          }
          resolve(result || { ok: false });
        });
      } catch (error) {
        resolve({ ok: false, detail: String(error) });
      }
    });
  }

  function present(card) {
    const userRemarks = settings.user_remarks || {};
    const groupRemarks = settings.group_remarks || {};
    const starred = new Set(settings.starred_users || []);
    const remarkName = userRemarks[card.senderUsername] || "";
    const remarkGroup = groupRemarks[card.conversationId] || "";
    const originalName = card.senderName || "群成员";
    const originalGroup = card.conversationName || card.displayGroup || "群聊";
    const next = {
      ...card,
      displayName: remarkName || card.displayName || originalName,
      displayGroup: remarkGroup || card.displayGroup || originalGroup,
      nameRemarked: Boolean(remarkName),
      groupRemarked: Boolean(remarkGroup),
      originalName: remarkName && remarkName !== originalName ? originalName : "",
      originalGroup: remarkGroup && remarkGroup !== originalGroup ? originalGroup : "",
      starred: starred.has(card.senderUsername),
      starColor: (settings.star_colors || {})[card.senderUsername] || "amber",
    };
    if (card.senderUsername) {
      next.avatarUrl = `${API}/api/avatar?username=${encodeURIComponent(card.senderUsername)}`;
    }
    if (card.quoteUsername) {
      next.quoteUsername = card.quoteUsername;
      next.quoteAvatarUrl = `${API}/api/avatar?username=${encodeURIComponent(card.quoteUsername)}`;
    }
    if (next.mediaUrl && next.mediaUrl.startsWith("/")) {
      next.mediaUrl = `${API}${next.mediaUrl}`;
    }
    if (next.mediaFullUrl && next.mediaFullUrl.startsWith("/")) {
      next.mediaFullUrl = `${API}${next.mediaFullUrl}`;
    }
    next.watermark = watermark;
    return next;
  }

  function visibleCards() {
    const blocked = new Set(settings.blocked_groups || []);
    return Array.from(cards.values())
      .filter((card) => !blocked.has(card.conversationId))
      .filter((card) => !settings.ca_only || (card.addresses || []).length)
      .sort((a, b) => (b.createTime || 0) - (a.createTime || 0));
  }

  async function proxied(url) {
    if (avatarCache.has(url)) return avatarCache.get(url);
    const result = await send("proxyDataUrl", { url });
    if (!result?.dataUrl) return "";
    avatarCache.set(url, result.dataUrl);
    return result.dataUrl;
  }

  // 新消息到达时微信可能还在下载缩略图，图片多试几次再换回「[图片]」。
  async function loadMedia(img, url) {
    for (const wait of [0, 1500, 4000, 10000]) {
      if (wait) await new Promise((resolve) => setTimeout(resolve, wait));
      if (!img.isConnected) return;
      const dataUrl = await proxied(url);
      if (dataUrl) {
        img.src = dataUrl;
        return;
      }
    }
    const miss = document.createElement("span");
    miss.className = "wf-media-miss";
    miss.textContent = "[图片]";
    img.replaceWith(miss);
  }

  async function hydrate(root) {
    const nodes = [...root.querySelectorAll("img[data-wf-src], img")];
    await Promise.all(
      nodes.map(async (img) => {
        if ((img.getAttribute("src") || "").startsWith("data:")) return;
        const src = img.getAttribute("data-wf-src") || img.getAttribute("src") || "";
        if (!src.includes("127.0.0.1:8765") && !src.includes("localhost:8765")) return;
        const url = src.startsWith("http") ? src : `${API}${src}`;
        img.removeAttribute("src");
        img.removeAttribute("data-wf-src");
        if (img.classList.contains("wf-media")) {
          await loadMedia(img, url);
          return;
        }
        const dataUrl = await proxied(url);
        if (dataUrl) img.src = dataUrl;
      }),
    );
  }

  function askIn(root, title, value) {
    return new Promise((resolve) => {
      let box = root.querySelector(".wf-ask");
      if (!box) {
        box = document.createElement("div");
        box.className = "wf-ask";
        box.innerHTML = `<style>
          .wf-ask{position:absolute;inset:0;display:grid;place-items:center;background:rgba(16,24,40,.42);z-index:9;}
          .wf-ask[hidden]{display:none;}
          .wf-ask-card{width:min(320px,calc(100% - 24px));padding:14px;border-radius:16px;background:#fff7f2;color:#101828;font:13px/1.5 sans-serif;}
          .wf-ask-card h3{margin:0 0 10px;}
          .wf-ask-card input{width:100%;height:36px;border:1px solid #f9d2c4;border-radius:10px;padding:0 10px;}
          .wf-ask-actions{display:flex;justify-content:flex-end;gap:8px;margin-top:12px;}
          .wf-ask-actions button{height:32px;padding:0 12px;border:0;border-radius:999px;background:#175cd3;color:#fff;cursor:pointer;}
          .wf-ask-actions .ghost{background:#f2f4f7;color:#344054;}
        </style>
        <div class="wf-ask-card"><h3></h3><input /><div class="wf-ask-actions"><button type="button" data-ok>确定</button><button type="button" class="ghost" data-cancel>取消</button></div></div>`;
        root.append(box);
      }
      const titleNode = box.querySelector("h3");
      const input = box.querySelector("input");
      titleNode.textContent = title;
      input.value = value || "";
      box.hidden = false;
      input.focus();
      input.select();
      const finish = (result) => {
        box.hidden = true;
        resolve(result);
      };
      box.querySelector("[data-ok]").onclick = () => finish(input.value);
      box.querySelector("[data-cancel]").onclick = () => finish(null);
    });
  }

  function pickStarColor(anchor, current) {
    const colors = (typeof WechatFeed !== "undefined" && WechatFeed.starColors) || [];
    return new Promise((resolve) => {
      const host = document.createElement("div");
      const rect = anchor.getBoundingClientRect();
      host.style.cssText = "position:fixed;z-index:2147483647;display:flex;flex-wrap:wrap;gap:6px;width:196px;padding:8px;border-radius:12px;background:#fff;box-shadow:0 12px 32px rgba(0,0,0,.28);";
      host.style.left = `${Math.max(8, Math.min(rect.left, window.innerWidth - 210))}px`;
      host.style.top = `${Math.min(rect.bottom + 6, window.innerHeight - 88)}px`;
      const buttons = colors
        .map((item) => `<button type="button" data-color="${item.id}" title="${item.name}" style="width:28px;height:28px;border-radius:50%;border:2px solid ${current === item.id ? "#111" : "transparent"};background:${item.hex};cursor:pointer;"></button>`)
        .join("");
      host.innerHTML = `${buttons}<button type="button" data-color="" style="height:28px;padding:0 8px;border:0;border-radius:999px;background:#f2f4f7;cursor:pointer;">取消</button>`;
      const close = (value) => {
        host.remove();
        document.removeEventListener("mousedown", outside);
        resolve(value);
      };
      const outside = (event) => {
        if (!host.contains(event.target)) close(null);
      };
      host.addEventListener("mousedown", (event) => event.stopPropagation());
      host.addEventListener("click", (event) => {
        const button = event.target.closest("[data-color]");
        if (!button) return;
        close(button.dataset.color);
      });
      document.documentElement.append(host);
      setTimeout(() => document.addEventListener("mousedown", outside), 0);
    });
  }

  function showImagePreview(img) {
    const src = img.currentSrc || img.getAttribute("src") || "";
    if (!src) return;
    const full = img.dataset.wfFull;
    if (full) {
      proxied(full).then((dataUrl) => {
        if (dataUrl && imagePreview && imagePreview.dataset.from === src) imagePreview.src = dataUrl;
      });
    }
    if (!imagePreview) {
      imagePreview = document.createElement("img");
      imagePreview.alt = "预览";
      imagePreview.style.cssText = "position:fixed;z-index:2147483647;max-width:min(480px, calc(100vw - 24px));max-height:min(560px, calc(100vh - 24px));border-radius:12px;box-shadow:0 18px 50px rgba(0,0,0,.38);pointer-events:none;background:#111;object-fit:contain;";
      document.documentElement.append(imagePreview);
    }
    imagePreview.dataset.from = src;
    imagePreview.src = (full && avatarCache.get(full)) || src;
    const rect = img.getBoundingClientRect();
    const previewWidth = Math.min(480, window.innerWidth - 24);
    let left = rect.right + 12;
    if (left + previewWidth > window.innerWidth - 8) left = Math.max(8, rect.left - previewWidth - 12);
    const top = Math.max(8, Math.min(rect.top, window.innerHeight - 80));
    imagePreview.style.left = `${left}px`;
    imagePreview.style.top = `${top}px`;
    imagePreview.style.display = "block";
  }

  function hideImagePreview() {
    if (!imagePreview) return;
    imagePreview.style.display = "none";
    imagePreview.dataset.from = "";
  }

  function bindList(root, list, { askRoot } = {}) {
    if (list.dataset.wfBound) return;
    list.dataset.wfBound = "1";
    const dialogIn = () => (askRoot ? askRoot() : root);
    list.addEventListener("click", async (event) => {
      const copy = event.target.closest("[data-copy]");
      if (copy) {
        navigator.clipboard.writeText(copy.dataset.copy || "");
        if (!copy.dataset.label) copy.dataset.label = copy.textContent;
        copy.textContent = "已复制";
        clearTimeout(copy._reset);
        copy._reset = setTimeout(() => {
          copy.textContent = copy.dataset.label;
        }, 1200);
        return;
      }
      const star = event.target.closest("[data-star]");
      if (star) {
        const card = star.closest("[data-user]");
        const userId = card && card.dataset.user;
        if (!userId) return;
        const current = new Set(settings.starred_users || []);
        const colors = { ...(settings.star_colors || {}) };
        const picked = await pickStarColor(star, current.has(userId) ? colors[userId] || "amber" : "");
        if (picked === null) return;
        if (!picked) {
          current.delete(userId);
          delete colors[userId];
        } else {
          current.add(userId);
          colors[userId] = picked;
        }
        settings.starred_users = Array.from(current);
        settings.star_colors = colors;
        paintAll();
        const saved = await send("starUser", {
          userId,
          starred: Boolean(picked),
          color: picked || "",
        });
        if (!saved?.ok) paintAll();
        return;
      }
      const mute = event.target.closest("[data-mute]");
      if (mute) {
        const card = mute.closest("[data-group]");
        const groupId = card && card.dataset.group;
        if (!groupId) return;
        const blocked = new Set(settings.blocked_groups || []);
        blocked.add(groupId);
        settings.blocked_groups = Array.from(blocked);
        for (const [id, item] of Array.from(cards.entries())) {
          if (item.conversationId === groupId) cards.delete(id);
        }
        paintAll();
        send("muteGroup", { groupId });
        return;
      }
      const user = event.target.closest("[data-remark-user]");
      if (user) {
        const card = user.closest("[data-user]");
        const next = await askIn(dialogIn(), "给这个人的备注", user.textContent || "");
        if (next === null) return;
        const remarks = { ...(settings.user_remarks || {}) };
        const text = String(next || "").trim();
        const userId = card && card.dataset.user;
        if (!userId) return;
        if (text) remarks[userId] = text;
        else delete remarks[userId];
        settings.user_remarks = remarks;
        paintAll();
        send("remarkUser", { userId, text: next });
        return;
      }
      const group = event.target.closest("[data-remark-group]");
      if (group) {
        const card = group.closest("[data-group]");
        const next = await askIn(dialogIn(), "给这个群的备注", group.textContent || "");
        if (next === null) return;
        const remarks = { ...(settings.group_remarks || {}) };
        const text = String(next || "").trim();
        const groupId = card && card.dataset.group;
        if (!groupId) return;
        if (text) remarks[groupId] = text;
        else delete remarks[groupId];
        settings.group_remarks = remarks;
        paintAll();
        send("remarkGroup", { groupId, text: next });
      }
    });
    list.addEventListener("mouseover", (event) => {
      const img = event.target.closest(".wf-media");
      if (img) showImagePreview(img);
    });
    list.addEventListener("mouseout", (event) => {
      const img = event.target.closest(".wf-media");
      if (!img) return;
      const next = event.relatedTarget;
      if (next && img.contains(next)) return;
      hideImagePreview();
    });
    list.addEventListener("scroll", () => {
      if (!hasMore) return;
      if (list.scrollTop + list.clientHeight >= list.scrollHeight - 48) {
        requestSnapshot(true);
      }
    });
  }

  function paintList(list, { gmgn, limit } = {}) {
    if (!list || typeof WechatFeed === "undefined") return;
    const rows = visibleCards();
    const max = limit || 80;
    const shown = rows.slice(0, max);
    const starred = new Set(settings.starred_users || []);
    const userRemarks = settings.user_remarks || {};
    const groupRemarks = settings.group_remarks || {};
    const stamp = watermark + "#" + shown
      .map((card) => `${card.id}:${starred.has(card.senderUsername) ? 1 : 0}:${(settings.star_colors || {})[card.senderUsername] || ""}:${userRemarks[card.senderUsername] || ""}:${groupRemarks[card.conversationId] || ""}:${(card.tokenCards || []).length}`)
      .join("|");
    if (list.dataset.wfStamp === stamp) return;
    list.dataset.wfStamp = stamp;
    if (!shown.length) {
      list.innerHTML = `<div class="empty">${cards.size ? "当前筛选下没有发言。" : "等待群里的新发言。"}</div>`;
      return;
    }
    list.innerHTML = shown.map((card) => WechatFeed.gmgnCardHtml(present(card))).join("");
    hydrate(list);
  }

  function paintAll() {
    if (floatHost) {
      const list = floatHost._list;
      const all = floatHost._all;
      const ca = floatHost._ca;
      if (all && ca) {
        all.classList.toggle("on", !settings.ca_only);
        ca.classList.toggle("on", Boolean(settings.ca_only));
      }
      paintList(list, { gmgn: false });
    }
    if (overlayOn() && isFollowPage()) syncStream();
  }

  function panelShell(title) {
    return `<style>${WechatFeed.cardCss()}
      .wrap { position:relative; display:flex; flex-direction:column; height:100%; min-height:0; overflow:hidden; background:var(--card); color:var(--ink); font:13px/1.5 "Segoe UI","PingFang SC","Microsoft YaHei",sans-serif; box-shadow:0 16px 40px rgba(0,0,0,.28); border:1px solid var(--line); border-radius:14px; }
      header.bar { display:flex; justify-content:space-between; gap:8px; align-items:center; padding:10px 12px; border-bottom:1px solid var(--line); cursor:move; }
      header.bar p { margin:2px 0 0; color:var(--muted); font-size:12px; }
      .mode { display:flex; gap:4px; }
      .mode button, .bar button { border:1px solid var(--line); border-radius:999px; background:transparent; color:inherit; font:inherit; padding:3px 10px; cursor:pointer; }
      .mode .on { background:#111827; color:#fff; }
      .list { overflow:auto; overflow-x:hidden; padding:8px; display:flex; flex-direction:column; align-items:stretch; gap:8px; min-height:0; min-width:0; flex:1 1 auto; }
      .chip { border:0; border-radius:999px; background:#111827; color:white; padding:8px 12px; box-shadow:0 8px 24px rgba(0,0,0,.2); cursor:pointer; }
      .list .wf-xcard { margin:0; }
      .resize { position:absolute; right:4px; bottom:4px; width:18px; height:18px; cursor:nwse-resize; border-right:3px solid #98a2b3; border-bottom:3px solid #98a2b3; opacity:.85; }
    </style>
    <section class="wrap">
      <header class="bar">
        <div>
          <strong>${title}</strong>
          <p>本地信息流，点击昵称备注、点☆星标。</p>
          <div class="mode">
            <button type="button" data-all class="on">全部</button>
            <button type="button" data-ca>只看CA</button>
          </div>
        </div>
        <button type="button" data-collapse>收起</button>
      </header>
      <div class="list"><div class="empty">等待群里的新发言。</div></div>
      <div class="resize" data-resize></div>
    </section>`;
  }

  function rgbOf(value) {
    const match = String(value || "").match(/[\d.]+/g);
    if (!match || match.length < 3) return null;
    const alpha = match.length > 3 ? Number(match[3]) : 1;
    const luma = (Number(match[0]) * 299 + Number(match[1]) * 587 + Number(match[2]) * 114) / 1000;
    return { luma, alpha };
  }

  function opaqueBackgroundDark(node) {
    for (let current = node; current; current = current.parentElement) {
      const bg = rgbOf(getComputedStyle(current).backgroundColor);
      if (bg && bg.alpha > 0.5) return bg.luma < 128;
    }
    return false;
  }

  // gmgn 的 body 背景和实际主题可能对不上，推文文字颜色才可靠。
  function isDarkPage() {
    const sample = isGmgn()
      ? document.querySelector('[data-testid="x-tracker-root"] [data-index]') ||
        document.querySelector('[data-testid="x-tracker-root"]')
      : null;
    if (sample) {
      const ink = rgbOf(getComputedStyle(sample).color);
      if (ink) return ink.luma > 128;
      return opaqueBackgroundDark(sample);
    }
    return opaqueBackgroundDark(document.body);
  }

  function applyTheme(host) {
    const theme = isDarkPage() ? "dark" : "light";
    if (host.dataset.theme !== theme) host.dataset.theme = theme;
  }

  let themeTimer = 0;
  let lastTheme = "";
  function checkTheme() {
    themeTimer = 0;
    const theme = isDarkPage() ? "dark" : "light";
    if (theme === lastTheme) return;
    lastTheme = theme;
    if (floatHost) applyTheme(floatHost);
    if (overlayOn() && isFollowPage()) syncStream();
  }

  const themeWatch = new MutationObserver(() => {
    if (!themeTimer) themeTimer = setTimeout(checkTheme, 120);
  });
  const themeAttrs = { attributes: true, attributeFilter: ["class", "style", "data-theme", "data-color-mode", "custom-theme"] };
  themeWatch.observe(document.documentElement, themeAttrs);
  if (document.body) themeWatch.observe(document.body, themeAttrs);

  function viewportSize() {
    const view = window.visualViewport;
    return {
      width: Math.round(view ? view.width : window.innerWidth),
      height: Math.round(view ? view.height : window.innerHeight),
    };
  }

  function clampRect(rect) {
    const view = viewportSize();
    const width = Math.min(
      Math.max(MIN_FLOAT_W, Number(rect.width) || 380),
      Math.max(MIN_FLOAT_W, view.width - 16),
    );
    const height = Math.min(
      Math.max(MIN_FLOAT_H, Number(rect.height) || 520),
      Math.max(MIN_FLOAT_H, view.height - 16),
    );
    let right = Number(rect.right);
    if (!Number.isFinite(right) && Number.isFinite(Number(rect.left))) {
      right = view.width - Number(rect.left) - width;
    }
    if (!Number.isFinite(right)) right = 16;
    let top = Number(rect.top);
    if (!Number.isFinite(top)) top = 72;
    right = Math.max(8, Math.min(right, view.width - width - 8));
    top = Math.max(8, Math.min(top, view.height - height - 8));
    return { width, height, right, top };
  }

  function applyRect(host, rect) {
    const next = clampRect(rect);
    host.style.cssText = [
      "display:block",
      "position:fixed",
      "z-index:2147483646",
      "box-sizing:border-box",
      `top:${next.top}px`,
      `right:${next.right}px`,
      "left:auto",
      `width:${next.width}px`,
      `height:${next.height}px`,
      "overflow:hidden",
      "border-radius:14px",
    ].join(";");
    return next;
  }

  async function loadRect() {
    const stored = await chrome.storage.local.get("floatRect");
    return clampRect(stored.floatRect || { width: 380, height: 520, right: 16, top: 72 });
  }

  function saveRect(rect) {
    chrome.storage.local.set({ floatRect: clampRect(rect) });
  }

  function currentRect(host) {
    const box = host.getBoundingClientRect();
    const view = viewportSize();
    return clampRect({
      width: box.width,
      height: box.height,
      right: view.width - box.right,
      top: box.top,
    });
  }

  function keepFloatInView() {
    if (!floatHost || collapsed) return;
    applyRect(floatHost, currentRect(floatHost));
  }

  function enableDrag(host, handle, onChange) {
    handle.addEventListener("mousedown", (event) => {
      if (event.button !== 0) return;
      event.preventDefault();
      const startX = event.clientX;
      const startY = event.clientY;
      const origin = currentRect(host);
      const move = (next) => {
        applyRect(host, {
          ...origin,
          right: origin.right - (next.clientX - startX),
          top: origin.top + (next.clientY - startY),
        });
      };
      const up = () => {
        document.removeEventListener("mousemove", move);
        document.removeEventListener("mouseup", up);
        onChange();
      };
      document.addEventListener("mousemove", move);
      document.addEventListener("mouseup", up);
    });
  }

  function enableResize(host, handle, onChange) {
    handle.addEventListener("mousedown", (event) => {
      if (event.button !== 0) return;
      event.preventDefault();
      event.stopPropagation();
      const startX = event.clientX;
      const startY = event.clientY;
      const origin = currentRect(host);
      const view = viewportSize();
      const left = view.width - origin.right - origin.width;
      const move = (next) => {
        const width = origin.width + (next.clientX - startX);
        const height = origin.height + (next.clientY - startY);
        applyRect(host, {
          width,
          height,
          top: origin.top,
          right: view.width - left - width,
        });
      };
      const up = () => {
        document.removeEventListener("mousemove", move);
        document.removeEventListener("mouseup", up);
        onChange();
      };
      document.addEventListener("mousemove", move);
      document.addEventListener("mouseup", up);
    });
  }

  function wireChrome(host, shadow, { collapse } = {}) {
    const list = shadow.querySelector(".list");
    const all = shadow.querySelector("[data-all]");
    const ca = shadow.querySelector("[data-ca]");
    host._list = list;
    host._all = all;
    host._ca = ca;
    bindList(shadow, list);
    all.addEventListener("click", () => {
      settings.ca_only = false;
      paintAll();
      send("setCaOnly", { enabled: false });
    });
    ca.addEventListener("click", () => {
      settings.ca_only = true;
      paintAll();
      send("setCaOnly", { enabled: true });
    });
    shadow.querySelector("[data-collapse]").addEventListener("click", collapse);
    paintList(list, { gmgn: false });
  }

  async function mountFloat() {
    if (floatHost || collapsed) return;
    const rect = await loadRect();
    floatHost = document.createElement("div");
    applyTheme(floatHost);
    applyRect(floatHost, rect);
    const shadow = floatHost.attachShadow({ mode: "closed" });
    shadow.innerHTML = panelShell("微信群");
    const persist = () => saveRect(currentRect(floatHost));
    enableDrag(floatHost, shadow.querySelector("header.bar"), persist);
    enableResize(floatHost, shadow.querySelector("[data-resize]"), persist);
    wireChrome(floatHost, shadow, {
      collapse: () => {
        collapsed = true;
        removeFloat();
        mountChip();
      },
    });
    document.documentElement.append(floatHost);
  }

  function mountChip() {
    removeFloat();
    floatHost = document.createElement("div");
    floatHost.style.cssText = "display:block;position:fixed;right:16px;bottom:16px;z-index:2147483646;";
    const shadow = floatHost.attachShadow({ mode: "closed" });
    shadow.innerHTML = `<style>${WechatFeed.cardCss()}</style><button class="chip" type="button">微信群</button>
      <style>.chip{border:0;border-radius:999px;background:#111827;color:#fff;padding:8px 12px;box-shadow:0 8px 24px rgba(0,0,0,.2);cursor:pointer;font:13px/1.4 sans-serif;}</style>`;
    shadow.querySelector("button").addEventListener("click", () => {
      collapsed = false;
      removeFloat();
      mountFloat();
    });
    document.documentElement.append(floatHost);
  }

  function removeFloat() {
    if (floatHost) floatHost.remove();
    floatHost = null;
  }

  function findTweetItemList() {
    const root = document.querySelector('[data-testid="x-tracker-root"]');
    const scope = root || document;
    const scrollers = [...scope.querySelectorAll('[data-testid="virtuoso-scroller"]')];
    const scroller =
      scrollers.find((node) => {
        const overflow = getComputedStyle(node).overflowY;
        return overflow === "auto" || overflow === "scroll";
      }) || scrollers[scrollers.length - 1];
    if (scroller) {
      const list = scroller.querySelector('[data-testid="virtuoso-item-list"]');
      if (list) return list;
    }
    return document.querySelector('[data-testid="x-tracker-root"] [data-testid="virtuoso-item-list"]');
  }

  function cardStamp(card) {
    const starred = new Set(settings.starred_users || []);
    return [
      card.id,
      watermark,
      starred.has(card.senderUsername) ? 1 : 0,
      (settings.star_colors || {})[card.senderUsername] || "",
      (settings.user_remarks || {})[card.senderUsername] || "",
      (settings.group_remarks || {})[card.conversationId] || "",
      card.text || "",
      (card.tokenCards || []).length,
      (card.contextBefore || []).length + (card.contextAfter || []).length,
    ].join(":");
  }

  function dialogRoot() {
    if (dialogHost && dialogHost.isConnected) return dialogHost._shadow;
    dialogHost = document.createElement("div");
    dialogHost.style.cssText = "position:fixed;left:0;top:0;width:0;height:0;z-index:2147483647;";
    const shadow = dialogHost.attachShadow({ mode: "closed" });
    shadow.innerHTML = "<style>.wf-ask{position:fixed !important;}</style>";
    dialogHost._shadow = shadow;
    document.documentElement.append(dialogHost);
    return shadow;
  }

  function slotCards(rows, item) {
    const ts = Number(item.dataset.wfTs) || 0;
    const prev = Number(item.dataset.wfPrev) || 0;
    if (!ts) return [];
    const picked = rows.filter((card) => {
      const time = (Number(card.createTime) || 0) * 1000;
      return time > ts && (!prev || time <= prev);
    });
    return prev ? picked : picked.slice(0, TOP_SLOT_LIMIT);
  }

  function ensureSlot(item) {
    let host = item.querySelector(":scope > [data-wf-slot]");
    if (host && host._list) {
      if (item.firstElementChild !== host) item.prepend(host);
      return host;
    }
    if (host) host.remove();
    host = document.createElement("div");
    host.dataset.wfSlot = "1";
    host.style.cssText = "display:block;";
    const shadow = host.attachShadow({ mode: "closed" });
    shadow.innerHTML = `<style>${WechatFeed.cardCss()}
      .list { display:flex; flex-direction:column; gap:8px; padding:8px 8px 0; }
      .wf-xcard { margin:0; }
    </style><div class="list"></div>`;
    host._list = shadow.querySelector(".list");
    bindList(shadow, host._list, { askRoot: dialogRoot });
    host.addEventListener("click", (event) => event.stopPropagation());
    item.prepend(host);
    return host;
  }

  function paintSlot(host, rows, theme) {
    if (host.dataset.theme !== theme) host.dataset.theme = theme;
    const stamp = rows.map((card) => cardStamp(card)).join("|");
    if (host._stamp === stamp) return;
    host._stamp = stamp;
    host._list.innerHTML = rows.map((card) => WechatFeed.gmgnCardHtml(present(card))).join("");
    hydrate(host._list);
  }

  function loadOlderForTweets(itemList) {
    if (!hasMore || snapshotPending || !oldest) return;
    const oldestTweet = Number(itemList.dataset.wfOldest) || 0;
    if (oldestTweet && oldest * 1000 > oldestTweet) requestSnapshot(true);
  }

  function syncStream() {
    streamTimer = 0;
    if (!overlayOn() || !isFollowPage() || typeof WechatFeed === "undefined") return;
    const itemList = streamList && streamList.isConnected ? streamList : findTweetItemList();
    if (!itemList) return;
    const rows = visibleCards();
    const theme = isDarkPage() ? "dark" : "light";
    for (const item of itemList.querySelectorAll(":scope > [data-index]")) {
      const picked = slotCards(rows, item);
      if (!picked.length) {
        const stale = item.querySelector(":scope > [data-wf-slot]");
        if (stale) stale.remove();
        continue;
      }
      paintSlot(ensureSlot(item), picked, theme);
    }
    loadOlderForTweets(itemList);
  }

  function queueStream() {
    if (!streamTimer) streamTimer = setTimeout(syncStream, 60);
  }

  function attachStream() {
    checkTheme();
    const follow = isFollowPage();
    if (!overlayOn() || !follow) {
      detachStream();
      return;
    }
    const itemList = findTweetItemList();
    if (!itemList || itemList === streamList) return;
    streamObservers.forEach((item) => item.disconnect());
    streamList = itemList;
    streamObservers = [new MutationObserver(queueStream), new MutationObserver(queueStream)];
    streamObservers[0].observe(itemList, { childList: true });
    streamObservers[1].observe(itemList, {
      subtree: true,
      attributes: true,
      attributeFilter: ["data-wf-ts", "data-wf-prev"],
    });
    queueStream();
  }

  function mountStream() {
    attachStream();
    if (!streamPoll) streamPoll = setInterval(attachStream, 1000);
  }

  function removeStream() {
    if (streamPoll) clearInterval(streamPoll);
    streamPoll = 0;
    detachStream();
  }

  function detachStream() {
    if (!streamList && !streamObservers.length) return;
    streamObservers.forEach((item) => item.disconnect());
    streamObservers = [];
    streamList = null;
    document.querySelectorAll("[data-wf-slot]").forEach((node) => node.remove());
  }

  function requestSnapshot(append) {
    if (!port) return;
    if (append && snapshotPending) return;
    if (!append) {
      cards.clear();
      oldest = 0;
      oldestReceived = 0;
    }
    const payload = { type: "snapshot" };
    if (append && oldest) payload.before = oldest;
    if (append && oldestReceived) payload.before_received = oldestReceived;
    snapshotPending = true;
    port.postMessage(payload);
  }

  function remember(card, paint = true) {
    if (!card || !card.id) return;
    cards.set(card.id, card);
    const time = Number(card.createTime) || 0;
    const received = Number(card.receivedAt) || 0;
    if (
      !oldest ||
      (time && time < oldest) ||
      (time === oldest && received && received < oldestReceived)
    ) {
      oldest = time;
      oldestReceived = received;
    }
    if (paint) paintAll();
  }

  function connect() {
    if (port) return;
    port = chrome.runtime.connect({ name: "gmgn-feed" });
    port.onMessage.addListener((message) => {
      if (message.type === "settings" && message.settings) {
        const caChanged = Boolean(settings.ca_only) !== Boolean(message.settings.ca_only);
        const overlayChanged = Boolean(settings.gmgn_overlay) !== Boolean(message.settings.gmgn_overlay);
        const floatChanged = Boolean(settings.float_panel) !== Boolean(message.settings.float_panel);
        settings = { ...settings, ...message.settings };
        if (overlayChanged || floatChanged) applySurface();
        else paintAll();
        if (caChanged) requestSnapshot(false);
      }
      if (message.type === "snapshot") {
        snapshotPending = false;
        if (!message.append) cards.clear();
        for (const card of message.cards || []) remember(card, false);
        hasMore = Boolean(message.has_more);
        paintAll();
      }
      if (message.type === "message") remember(message.card);
      if (message.type === "patch" && message.card) remember(message.card);
      if (message.type === "cleared") {
        cards.clear();
        paintAll();
      }
    });
    port.onDisconnect.addListener(() => {
      port = null;
      snapshotPending = false;
      if (floatWanted() || (overlayOn() && isGmgn())) {
        if (reconnectTimer) clearTimeout(reconnectTimer);
        reconnectTimer = setTimeout(connect, 800);
      }
    });
    requestSnapshot(false);
  }

  function disconnect() {
    if (reconnectTimer) {
      clearTimeout(reconnectTimer);
      reconnectTimer = 0;
    }
    if (port) port.disconnect();
    port = null;
  }

  function applySurface() {
    const need = floatWanted() || (overlayOn() && isGmgn());
    if (!need) {
      removeFloat();
      removeStream();
      disconnect();
      return;
    }
    connect();
    if (floatWanted()) {
      if (collapsed) mountChip();
      else if (!floatHost) mountFloat();
      else applyTheme(floatHost);
    } else {
      collapsed = false;
      removeFloat();
    }
    if (overlayOn() && isGmgn()) mountStream();
    else removeStream();
    paintAll();
  }

  window.addEventListener("resize", keepFloatInView);
  if (window.visualViewport) {
    window.visualViewport.addEventListener("resize", keepFloatInView);
    window.visualViewport.addEventListener("scroll", keepFloatInView);
  }

  function applyStored(stored) {
    if (stored.settings) settings = { ...settings, ...stored.settings };
    if (stored.gmgnOverlay !== undefined) settings.gmgn_overlay = Boolean(stored.gmgnOverlay);
    if (stored.floatPanel !== undefined) settings.float_panel = Boolean(stored.floatPanel);
    if (stored.caOnly !== undefined) settings.ca_only = Boolean(stored.caOnly);
    if (Array.isArray(stored.floatSites)) floatSites = stored.floatSites;
    if (typeof stored.watermark === "string") watermark = stored.watermark;
  }

  chrome.storage.onChanged.addListener((changes, area) => {
    if (area !== "local") return;
    const stored = {};
    if (changes.settings) stored.settings = changes.settings.newValue;
    if (changes.gmgnOverlay) stored.gmgnOverlay = changes.gmgnOverlay.newValue;
    if (changes.floatPanel) stored.floatPanel = changes.floatPanel.newValue;
    if (changes.caOnly) stored.caOnly = changes.caOnly.newValue;
    if (changes.floatSites) stored.floatSites = changes.floatSites.newValue || DEFAULT_FLOAT_SITES;
    if (changes.watermark) stored.watermark = changes.watermark.newValue || "";
    if (!Object.keys(stored).length) return;
    const prevOverlay = overlayOn();
    const prevFloat = floatWanted();
    applyStored(stored);
    if (prevOverlay === overlayOn() && prevFloat === floatWanted()) {
      paintAll();
      return;
    }
    applySurface();
  });

  async function boot() {
    try {
      const stored = await chrome.storage.local.get(["gmgnOverlay", "floatPanel", "caOnly", "settings", "floatSites", "watermark"]);
      applyStored(stored);
      applySurface();
    } catch (error) {
      // storage 读失败时仍去拉一次状态
    }
    const state = await send("getState");
    if (state?.ok) {
      settings = {
        ...settings,
        ...(state.settings || {}),
        gmgn_overlay: state.enabled,
        float_panel: state.float_panel,
        ca_only: state.ca_only,
      };
      applySurface();
    }
  }

  // 单页应用切换路由不会重新注入脚本，网址变了要重新判断白名单。
  setInterval(() => {
    if (location.href === lastHref) return;
    lastHref = location.href;
    applySurface();
  }, 1000);

  boot();
})();
