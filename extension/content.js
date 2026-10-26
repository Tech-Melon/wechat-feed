(() => {
  // 静态注入和白名单动态注入可能落在同一页面上。
  if (globalThis.__wechatFeedLoaded) return;
  globalThis.__wechatFeedLoaded = true;
  const API = "http://127.0.0.1:8765";
  if (typeof WechatFeed !== "undefined" && WechatFeed.setApiBase) WechatFeed.setApiBase(API);
  const DEFAULT_FLOAT_SITES = ["https://gmgn.ai/follow?target=xTracker"];
  let floatSites = DEFAULT_FLOAT_SITES;
  let lastHref = location.href;
  const cards = new Map();
  // 最近 CA 面板单独存一份含合约的卡：主列表分页会清空 cards，这里不跟着丢。
  const caCards = new Map();
  const CA_KEEP = 300;
  const RECENT_LIMIT = 10;
  let recentSeen = 0;
  let recentTop = "";
  // 最近 CA 的最新市值（本地库，不打 GMGN）：面板开着每 10 秒读一次并加急后台刷新，
  // 只有顶部一行时 30 秒一次。后台同一个币最快 60 秒刷一次，额度见 castore.URGENT_MIN_AGE。
  const quotes = new Map();
  const QUOTE_OPEN_MS = 10_000;
  const QUOTE_BAR_MS = 30_000;
  let quoteTimer = 0;
  let quoteBusy = false;
  let settings = {
    gmgn_overlay: false,
    float_panel: false,
    ca_only: false,
    gmgn_ca_only: false,
    float_ca_only: false,
    all_feed_groups: [],
    ca_feed_groups: [],
    user_remarks: {},
    group_remarks: {},
    starred_users: [],
    blocked_groups: [],
    blocked_users: [],
    group_users: {},
  };
  let port = null;
  let hasMore = false;
  let oldest = 0;
  let oldestReceived = 0;
  let collapsed = false;
  let floatHost = null;
  let reconnectTimer = 0;
  let snapshotPending = false;
  let fillPages = 0;
  const FILL_VISIBLE = 24;
  const FILL_PAGES = 16;
  let streamTimer = 0;
  let streamPoll = 0;
  let streamList = null;
  let streamObservers = [];
  let dialogHost = null;
  let imagePreview = null;

  const avatarCache = new Map();
  // 浮窗先显示一页，滚到底再多显示一页（不够就向后台要更早的）；回到顶部后收回一页，DOM 保持小。
  const FLOAT_PAGE = 80;
  let floatLimit = FLOAT_PAGE;
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

  function extensionAlive() {
    try {
      return Boolean(chrome.runtime?.id);
    } catch (error) {
      return false;
    }
  }

  function send(type, payload = {}, attempt = 0) {
    return new Promise((resolve) => {
      if (!extensionAlive()) {
        resolve({ ok: false, detail: "插件已更新，刷新页面后再用" });
        return;
      }
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
    return next;
  }

  function visibleCards(caOnly) {
    return Array.from(cards.values())
      .filter((card) => typeof WechatFeed !== "undefined" && WechatFeed.cardInFeed(card, settings, Boolean(caOnly)))
      .sort((a, b) => (b.createTime || 0) - (a.createTime || 0));
  }

  async function proxied(url) {
    if (avatarCache.has(url)) return avatarCache.get(url);
    const result = await send("proxyDataUrl", { url });
    if (!result?.dataUrl) return "";
    avatarCache.set(url, result.dataUrl);
    return result.dataUrl;
  }

  // 浮窗/插入流走 data URL：必须先拉 hd=1，缩略 _t 原生只有一两百像素，CSS 放大不了。
  async function loadMedia(img, thumbUrl) {
    const full = img.dataset.wfFull || "";
    if (full) {
      const hd = await proxied(full);
      if (hd && img.isConnected) {
        img.src = hd;
        return;
      }
    }
    for (const wait of [0, 1500, 4000, 10000]) {
      if (wait) await new Promise((resolve) => setTimeout(resolve, wait));
      if (!img.isConnected) return;
      const dataUrl = await proxied(thumbUrl);
      if (dataUrl) {
        img.src = dataUrl;
        return;
      }
    }
    const miss = document.createElement("span");
    miss.className = "wf-media-miss";
    miss.textContent = img.classList.contains("wf-emoji") ? "[表情]" : "[图片]";
    img.replaceWith(miss);
  }

  async function hydrate(root) {
    const nodes = [...root.querySelectorAll("img[data-wf-src], img")];
    await Promise.all(
      nodes.map(async (img) => {
        if ((img.getAttribute("src") || "").startsWith("data:")) return;
        const src = img.getAttribute("data-wf-src") || img.getAttribute("src") || "";
        const isLocal = src.includes("127.0.0.1:8765") || src.includes("localhost:8765") || src.startsWith("/");
        const isTokenImg = img.classList.contains("wf-token-logo") || img.classList.contains("wf-token-hero");
        if (!src || (!isLocal && !isTokenImg)) return;
        const url = src.startsWith("http") ? src : `${API}${src}`;
        img.removeAttribute("src");
        img.removeAttribute("data-wf-src");
        if (img.classList.contains("wf-media") || img.classList.contains("wf-emoji")) {
          await loadMedia(img, url);
          return;
        }
        const dataUrl = await proxied(url);
        if (dataUrl) {
          img.src = dataUrl;
          return;
        }
        if (img.classList.contains("wf-chain-ico")) {
          img.remove();
          return;
        }
        if (img.classList.contains("wf-token-logo")) {
          const letter = document.createElement("span");
          // 保留原有的类和尺寸（最近 CA 的小图标是内联尺寸）。
          letter.className = `${img.className} wf-token-fallback`;
          letter.style.cssText = img.style.cssText;
          const sym =
            img
              .closest(".wf-token, .rc-item, .rc-tick")
              ?.querySelector(".wf-token-title strong, .rc-sym")?.textContent || "?";
          letter.textContent = String(sym).replace("$", "").slice(0, 1) || "?";
          img.replaceWith(letter);
          return;
        }
        if (isTokenImg) img.remove();
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
          .wf-ask{position:absolute;inset:0;display:grid;place-items:center;background:rgba(16,24,40,.42);z-index:9;pointer-events:auto;}
          .wf-ask[hidden]{display:none;}
          .wf-ask-card{width:min(320px,calc(100% - 24px));padding:14px;border-radius:16px;background:#fff7f2;color:#101828;font:13px/1.5 sans-serif;box-shadow:0 16px 40px rgba(16,24,40,.28);}
          .wf-ask-card h3{margin:0 0 10px;}
          .wf-ask-card input{width:100%;height:36px;border:1px solid #f9d2c4;border-radius:10px;padding:0 10px;}
          .wf-ask-actions{display:flex;justify-content:flex-end;gap:8px;margin-top:12px;}
          .wf-ask-actions button{height:32px;padding:0 12px;border:0;border-radius:999px;background:#175cd3;color:#fff;cursor:pointer;}
          .wf-ask-actions .ghost{background:#f2f4f7;color:#344054;}
        </style>
        <div class="wf-ask-card"><h3></h3><input /><div class="wf-ask-actions"><button type="button" data-ok>确定</button><button type="button" class="ghost" data-cancel>取消</button></div></div>`;
        root.append(box);
      }
      if (typeof box._finish === "function") box._finish(null);
      const titleNode = box.querySelector("h3");
      const input = box.querySelector("input");
      titleNode.textContent = title;
      input.value = value || "";
      box.hidden = false;
      input.focus();
      input.select();
      const finish = (result) => {
        if (box._finish !== finish) return;
        box._finish = null;
        box.hidden = true;
        resolve(result);
      };
      box._finish = finish;
      box.querySelector("[data-ok]").onclick = () => finish(input.value);
      box.querySelector("[data-cancel]").onclick = () => finish(null);
      box.onclick = (event) => {
        if (event.target === box) finish(null);
      };
      input.onkeydown = (event) => {
        if (event.key === "Enter") finish(input.value);
        if (event.key === "Escape") finish(null);
      };
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

  function sizeToBox(el, maxW, maxH) {
    const nw = el.naturalWidth;
    const nh = el.naturalHeight;
    if (!nw || !nh) return false;
    const scale = Math.min(maxW / nw, maxH / nh);
    el.style.width = `${Math.round(nw * scale)}px`;
    el.style.height = `${Math.round(nh * scale)}px`;
    return true;
  }

  function placePreview(el, img) {
    const rect = img.getBoundingClientRect();
    const previewWidth = el.offsetWidth || Math.min(480, window.innerWidth - 24);
    const previewHeight = el.offsetHeight || 80;
    let left = rect.right + 12;
    if (left + previewWidth > window.innerWidth - 8) left = Math.max(8, rect.left - previewWidth - 12);
    const top = Math.max(8, Math.min(rect.top, window.innerHeight - previewHeight - 8));
    el.style.left = `${left}px`;
    el.style.top = `${top}px`;
  }

  function showImagePreview(img) {
    const src = img.currentSrc || img.getAttribute("src") || "";
    if (!src) return;
    const full = img.dataset.wfFull;
    const maxW = Math.min(480, window.innerWidth - 24);
    const maxH = Math.min(560, window.innerHeight - 24);
    const layout = () => {
      sizeToBox(imagePreview, maxW, maxH);
      placePreview(imagePreview, img);
    };
    if (full) {
      proxied(full).then((dataUrl) => {
        if (dataUrl && imagePreview && imagePreview.dataset.from === src) imagePreview.src = dataUrl;
      });
    }
    if (!imagePreview) {
      imagePreview = document.createElement("img");
      imagePreview.alt = "预览";
      imagePreview.style.cssText = "position:fixed;z-index:2147483647;border-radius:12px;box-shadow:0 18px 50px rgba(0,0,0,.38);pointer-events:none;background:#111;object-fit:contain;";
      document.documentElement.append(imagePreview);
    }
    imagePreview.dataset.from = src;
    imagePreview.onload = layout;
    imagePreview.src = (full && avatarCache.get(full)) || src;
    if (imagePreview.complete) layout();
    imagePreview.style.display = "block";
  }

  function hideImagePreview() {
    if (!imagePreview) return;
    imagePreview.style.display = "none";
    imagePreview.dataset.from = "";
  }

  function showImageLightbox(img) {
    hideImagePreview();
    const src = img.currentSrc || img.getAttribute("src") || "";
    if (!src) return;
    const full = img.dataset.wfFull;
    const overlay = document.createElement("div");
    overlay.style.cssText = "position:fixed;inset:0;z-index:2147483647;background:rgba(0,0,0,.78);display:flex;align-items:center;justify-content:center;cursor:zoom-out;";
    const picture = document.createElement("img");
    picture.alt = "";
    picture.style.cssText = "border-radius:12px;box-shadow:0 18px 50px rgba(0,0,0,.45);background:#111;object-fit:contain;";
    const maxW = window.innerWidth * 0.92;
    const maxH = window.innerHeight * 0.92;
    picture.onload = () => sizeToBox(picture, maxW, maxH);
    picture.src = src;
    overlay.append(picture);
    overlay.addEventListener("click", () => overlay.remove());
    document.documentElement.append(overlay);
    if (!full) return;
    avatarCache.delete(full);
    proxied(full).then((dataUrl) => {
      if (dataUrl && overlay.isConnected) picture.src = dataUrl;
    });
  }

  function bindList(root, list, { askRoot } = {}) {
    if (list.dataset.wfBound) return;
    list.dataset.wfBound = "1";
    const dialogIn = () => (askRoot ? askRoot() : root);
    list.addEventListener("click", async (event) => {
      if (event.target.closest("a.wf-text-link, a.wf-token-link")) return;
      const fmtBtn = event.target.closest("[data-mcap-format]");
      if (fmtBtn) {
        const next = fmtBtn.dataset.mcapFormat === "en" ? "en" : "cn";
        settings.mcap_format = next;
        if (typeof WechatFeed !== "undefined") WechatFeed.setNumberFormat(next);
        paintAll();
        send("setMcapFormat", { format: next });
        return;
      }
      const media = event.target.closest(".wf-media, .wf-emoji");
      if (media) {
        showImageLightbox(media);
        return;
      }
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
      const muteUser = event.target.closest("[data-mute-user]");
      if (muteUser) {
        const card = muteUser.closest("[data-user]");
        const userId = card && card.dataset.user;
        if (!userId) return;
        const blocked = new Set(settings.blocked_users || []);
        blocked.add(userId);
        settings.blocked_users = Array.from(blocked);
        for (const [id, item] of Array.from(cards.entries())) {
          if (item.senderUsername === userId) cards.delete(id);
        }
        paintAll();
        send("muteUser", { userId, blocked: true });
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
      const caUser = event.target.closest("[data-ca-user]");
      if (caUser) {
        const userId = caUser.dataset.caUser;
        if (userId) window.open(`${API}/#board/${encodeURIComponent(userId)}`, "_blank", "noreferrer");
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
        send("remarkUser", { userId, text });
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
        send("remarkGroup", { groupId, text });
      }
    });
    list.addEventListener("mouseover", (event) => {
      const img = event.target.closest(".wf-media, .wf-emoji");
      if (img) showImagePreview(img);
    });
    list.addEventListener("mouseout", (event) => {
      const img = event.target.closest(".wf-media, .wf-emoji");
      if (!img) return;
      const next = event.relatedTarget;
      if (next && img.contains(next)) return;
      hideImagePreview();
    });
  }

  // 只有浮窗的 .list 会滚动；插入推文流的列表跟着 gmgn 页面走。
  function bindFloatScroll(host, list) {
    const toTop = host._toTop;
    let frame = 0;
    const update = () => {
      frame = 0;
      const top = list.scrollTop;
      // 往下滑超过一屏才出现「回到最新」，平时不显示。
      if (toTop) toTop.hidden = top < Math.max(240, list.clientHeight);
      if (top < 40 && floatLimit > FLOAT_PAGE) {
        floatLimit = FLOAT_PAGE;
        paintList(list);
        return;
      }
      if (top + list.clientHeight < list.scrollHeight - 48) return;
      const total = visibleCards(settings.float_ca_only).length;
      if (floatLimit <= total) {
        floatLimit += FLOAT_PAGE;
        paintList(list);
      }
      if (total < floatLimit && hasMore) requestSnapshot(true);
    };
    list.addEventListener(
      "scroll",
      () => {
        if (!frame) frame = requestAnimationFrame(update);
      },
      { passive: true },
    );
    if (toTop) {
      toTop.addEventListener("mousedown", (event) => event.stopPropagation());
      toTop.addEventListener("click", () => list.scrollTo({ top: 0, behavior: "smooth" }));
    }
  }

  function statStamp(stats) {
    return stats ? `${stats.calls || 0}/${stats.wins || 0}/${stats.settled || 0}/${stats.rank || 0}` : "-";
  }

  function tokenStamp(card) {
    return (card.tokenCards || [])
      .map(
        (token) =>
          `${token.source || ""}:${token.marketCap || 0}:${token.liquidity || 0}:${token.holders || 0}:${token.nowX || 0}:${token.maxX || 0}:${token.groupNowX || 0}:${token.foundById || ""}:${token.isFirstCall ? 1 : 0}:${token.isGroupFirst ? 1 : 0}:${statStamp(token.callerStats)}:${statStamp(token.foundByStats)}:${statStamp(token.groupFoundByStats)}`
      )
      .join(",");
  }

  function paintList(list) {
    if (!list || typeof WechatFeed === "undefined") return;
    const shown = visibleCards(settings.float_ca_only).slice(0, floatLimit);
    if (!shown.length) {
      const text = cards.size ? "当前筛选下没有发言。" : "等待群里的新发言。";
      if (list.dataset.wfEmpty !== text) {
        list.innerHTML = `<div class="empty">${text}</div>`;
        list.dataset.wfEmpty = text;
      }
      return;
    }
    delete list.dataset.wfEmpty;
    // 增量更新：一条消息/一次市值刷新只动那一张卡，其余节点和已解码的图片原样保留。
    const created = WechatFeed.syncList(
      list,
      shown,
      (card) => card.id,
      (card, index) => WechatFeed.gmgnCardHtml(present(card), WechatFeed.stackMeta(shown, index)),
    );
    created.forEach((node) => hydrate(node));
    if (WechatFeed.startFreshTick) WechatFeed.startFreshTick(list);
  }

  function paintAll() {
    if (floatHost) {
      const list = floatHost._list;
      const all = floatHost._all;
      const ca = floatHost._ca;
      if (all && ca) {
        all.classList.toggle("on", !settings.float_ca_only);
        ca.classList.toggle("on", Boolean(settings.float_ca_only));
      }
      syncGroupButton(floatHost);
      paintList(list);
      paintRecent(floatHost);
    }
    if (overlayOn() && isFollowPage()) syncStream();
  }

  function panelShell(title) {
    return `<style>${WechatFeed.cardCss()}
      .wrap { position:relative; display:flex; flex-direction:column; height:100%; min-height:0; overflow:hidden; background:var(--card); color:var(--ink); font:13px/1.5 "Segoe UI","PingFang SC","Microsoft YaHei",sans-serif; box-shadow:0 16px 40px rgba(0,0,0,.28); border:1px solid var(--line); border-radius:14px; }
      header.bar { display:flex; gap:8px; align-items:center; padding:6px 8px 6px 10px; border-bottom:1px solid var(--line); cursor:move; }
      header.bar .brand { display:flex; gap:6px; align-items:center; min-width:0; flex:1; }
      header.bar .brand svg { flex:none; border-radius:5px; }
      header.bar strong { font-size:13px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
      .mode { display:flex; flex:none; padding:2px; gap:2px; border-radius:999px; background:#f2f4f7; }
      .mode button, .bar [data-collapse], .bar [data-groups] { height:22px; border:0; border-radius:999px; background:transparent; color:inherit; font:inherit; font-size:12px; padding:0 8px; cursor:pointer; }
      .bar [data-collapse], .bar [data-groups] { flex:none; border:1px solid var(--line); }
      .mode .on { background:#111827; color:#fff; }
      :host([data-theme="dark"]) .mode { background:rgba(255,255,255,.08); }
      .gpick { position:absolute; top:38px; right:44px; z-index:6; width:min(260px, calc(100% - 16px)); max-height:min(360px, 78%); overflow:auto; background:var(--card); color:var(--ink); border:1px solid var(--line); border-radius:12px; box-shadow:0 12px 32px rgba(0,0,0,.24); padding:8px; }
      .gpick[hidden] { display:none; }
      .gpick p { margin:0 0 6px; color:var(--muted); font-size:12px; }
      .gpick h4 { margin:8px 0 4px; font-size:12px; }
      .gpick label { display:flex; gap:6px; align-items:center; min-height:26px; font-size:12px; cursor:pointer; }
      .gpick button { display:block; width:100%; text-align:left; height:28px; margin:0 0 4px; padding:0 8px; border:0; border-radius:8px; background:transparent; color:inherit; font:inherit; cursor:pointer; }
      .gpick button.on { background:#111827; color:#fff; }
      /* 背景必须不透明：1 倍屏上 Chrome 不给透明背景的滚动容器做合成线程滚动，
         每帧都要主线程重绘，宿主页面（gmgn）一忙就卡。网页版是整页滚动，没有这个问题。 */
      .list { background:var(--card); overflow:auto; overflow-x:hidden; overscroll-behavior:contain; padding:8px; display:flex; flex-direction:column; align-items:stretch; gap:0; min-height:0; min-width:0; flex:1 1 auto; }
      /* 屏幕外的卡跳过布局与绘制：翻到几百条历史后，顶部插入新消息不必重排整列 */
      .list > .wf-card { content-visibility:auto; contain-intrinsic-size:auto 120px; }
      .totop { position:absolute; right:14px; bottom:30px; z-index:5; display:grid; place-items:center; width:34px; height:34px; padding:0; border:0; border-radius:50%; background:#111827; color:#fff; font:700 16px/1 sans-serif; box-shadow:0 6px 18px rgba(0,0,0,.28); cursor:pointer; opacity:.88; }
      .totop:hover { opacity:1; }
      .totop[hidden] { display:none; }
      :host([data-theme="dark"]) .totop { background:#f2f4f7; color:#111827; }
      .chip { border:0; border-radius:999px; background:#111827; color:white; padding:8px 12px; box-shadow:0 8px 24px rgba(0,0,0,.2); cursor:pointer; }
      .resize { position:absolute; right:4px; bottom:4px; width:18px; height:18px; cursor:nwse-resize; border-right:3px solid #98a2b3; border-bottom:3px solid #98a2b3; opacity:.85; }
      /* 最近 CA：标题栏图标 + 一行最新 CA + 下拉面板 */
      .bar [data-recent] { position:relative; flex:none; display:grid; place-items:center; width:26px; height:22px; padding:0; border:1px solid var(--line); border-radius:999px; background:transparent; color:inherit; cursor:pointer; }
      .bar [data-recent].on { background:#111827; color:#fff; border-color:#111827; }
      :host([data-theme="dark"]) .bar [data-recent].on { background:#f2f4f7; color:#111827; border-color:#f2f4f7; }
      .rc-badge { position:absolute; top:-6px; right:-6px; min-width:15px; height:15px; padding:0 4px; border-radius:999px; background:#e5484d; color:#fff; font:700 10px/15px sans-serif; text-align:center; box-shadow:0 0 0 2px var(--card); }
      .rc-badge[hidden], .rc-bar[hidden], .rc-panel[hidden] { display:none; }
      .rc-bar { flex:none; border-bottom:1px solid var(--line); }
      .rc-tick { display:flex; align-items:center; gap:6px; width:100%; height:28px; padding:0 6px 0 8px; background:color-mix(in srgb, #e5484d 6%, var(--card)); color:var(--ink); font-size:12px; line-height:28px; cursor:pointer; white-space:nowrap; overflow:hidden; }
      .rc-tick:hover { background:color-mix(in srgb, #e5484d 12%, var(--card)); }
      .rc-tick.is-new { animation:rc-flash 1.6s ease-out 1; }
      @keyframes rc-flash { 0%,30% { background:color-mix(in srgb, #e5484d 30%, var(--card)); } 100% { background:color-mix(in srgb, #e5484d 6%, var(--card)); } }
      .rc-tag { flex:none; display:grid; place-items:center; width:18px; height:18px; border-radius:5px; background:#e5484d; color:#fff; font-size:11px; font-weight:800; line-height:1; }
      .rc-chain { flex:none; display:grid; place-items:center; width:20px; height:20px; border-radius:6px; background:var(--card); box-shadow:0 0 0 1px var(--line); }
      .rc-chain .wf-chain-ico { width:16px; height:16px; border-radius:4px; }
      .rc-chain:empty { display:none; }
      .rc-logo { flex:none; display:grid; place-items:center; object-fit:cover; line-height:1; }
      .rc-sym { flex:none; max-width:84px; overflow:hidden; text-overflow:ellipsis; font-weight:800; font-size:13px; }
      .rc-cap { flex:none; font-weight:800; }
      .rc-cap .wf-metric-unit, .rc-m .wf-metric-unit { margin-left:1px; font-size:.75em; }
      .rc-cap.is-hot, .rc-m.is-cap.is-hot b { color:#d92d20; }
      .rc-cap.is-amber, .rc-m.is-cap.is-amber b { color:#dc6803; }
      .rc-cap.is-green, .rc-m.is-cap.is-green b { color:#079455; }
      .rc-cap.is-blue, .rc-m.is-cap.is-blue b { color:#1570ef; }
      :host([data-theme="dark"]) .rc-cap.is-hot, :host([data-theme="dark"]) .rc-m.is-cap.is-hot b { color:#fda29b; }
      :host([data-theme="dark"]) .rc-cap.is-amber, :host([data-theme="dark"]) .rc-m.is-cap.is-amber b { color:#fdb022; }
      :host([data-theme="dark"]) .rc-cap.is-green, :host([data-theme="dark"]) .rc-m.is-cap.is-green b { color:#75e0a7; }
      :host([data-theme="dark"]) .rc-cap.is-blue, :host([data-theme="dark"]) .rc-m.is-cap.is-blue b { color:#84adff; }
      .rc-x { flex:none; font-weight:800; }
      .rc-x.is-up, .rc-m.is-up b { color:#067647; }
      .rc-x.is-down, .rc-m.is-down b { color:#b42318; }
      .rc-m.is-max b { color:#c2410c; }
      :host([data-theme="dark"]) .rc-x.is-up, :host([data-theme="dark"]) .rc-m.is-up b { color:#75e0a7; }
      :host([data-theme="dark"]) .rc-x.is-down, :host([data-theme="dark"]) .rc-m.is-down b { color:#fda29b; }
      :host([data-theme="dark"]) .rc-m.is-max b { color:#fdb022; }
      .rc-dup { flex:none; padding:0 6px; border-radius:999px; background:#fff4d6; color:#b54708; font-size:11px; font-weight:800; line-height:16px; }
      :host([data-theme="dark"]) .rc-dup { background:rgba(247,144,9,.18); color:#fdb022; }
      .rc-who { flex:0 1 auto; min-width:2em; overflow:hidden; text-overflow:ellipsis; color:var(--muted); }
      .rc-tick .wf-cs { height:18px; line-height:18px; padding:0 6px; font-size:11px; }
      .rc-time { flex:none; margin-left:auto; color:var(--muted); font-size:11.5px; }
      .rc-time .wf-fresh:not([hidden]) + time { display:none; }
      .rc-copy { flex:none; height:20px; padding:0 8px; border:0; border-radius:6px; background:#2e90fa; color:#fff; font:700 11px/20px inherit; cursor:pointer; }
      .rc-copy:hover { background:#1570ef; }
      .rc-panel { position:absolute; left:8px; right:8px; z-index:7; display:flex; flex-direction:column; max-height:72%; background:var(--card); color:var(--ink); border:1px solid var(--line); border-radius:12px; box-shadow:0 12px 32px rgba(0,0,0,.28); overflow:hidden; }
      .rc-head { display:flex; align-items:baseline; gap:8px; padding:8px 10px; border-bottom:1px solid var(--line); }
      .rc-head strong { font-size:13px; }
      .rc-sub { color:var(--muted); font-size:11px; }
      .rc-list { flex:1 1 auto; min-height:0; overflow:auto; overscroll-behavior:contain; padding:6px; display:flex; flex-direction:column; gap:8px; }
      .rc-list .empty { padding:18px 8px; text-align:center; color:var(--muted); }
      .rc-item { padding:10px; border:1px solid var(--line); border-left:3px solid #e5484d; border-radius:12px; display:grid; gap:8px; }
      .rc-item.is-unread { background:color-mix(in srgb, #e5484d 5%, var(--card)); }
      .rc-top { display:flex; gap:10px; align-items:center; min-width:0; }
      .rc-id { display:grid; gap:3px; min-width:0; flex:1; }
      .rc-row { display:flex; align-items:center; gap:6px; min-width:0; }
      .rc-item .rc-sym { max-width:120px; font-size:15px; }
      .rc-item .wf-chain { flex:none; }
      .rc-sub2 { font-size:12px; }
      .rc-age { flex:none; margin-left:auto; color:#079455; font-size:11px; }
      .rc-age.is-stale { color:var(--muted); }
      :host([data-theme="dark"]) .rc-age { color:#75e0a7; }
      :host([data-theme="dark"]) .rc-age.is-stale { color:var(--muted); }
      .rc-name { min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; color:var(--muted); }
      .rc-metrics { display:grid; grid-template-columns:repeat(4, minmax(0,1fr)); gap:4px; }
      .rc-m { display:grid; gap:1px; padding:5px 7px; border-radius:8px; background:color-mix(in srgb, var(--ink) 4%, transparent); min-width:0; }
      .rc-m span { color:var(--muted); font-size:10.5px; font-weight:700; }
      .rc-m b { font-size:14px; font-weight:800; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
      .rc-m.is-cap { background:color-mix(in srgb, #2e90fa 8%, transparent); }
      .rc-m.is-cap b { font-size:15px; }
      .rc-caller { display:flex; align-items:center; gap:6px; min-width:0; font-size:12px; }
      .rc-caller.is-first { margin-top:-4px; color:var(--muted); }
      .rc-av { width:22px; height:22px; font-size:11px; }
      .rc-k { flex:none; color:var(--muted); font-size:11px; font-weight:800; }
      .rc-person { flex:0 1 auto; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; border:0; padding:0; background:transparent; color:inherit; font:inherit; font-weight:800; cursor:pointer; }
      button.rc-person:hover { color:#175cd3; }
      .rc-in { flex:none; margin-left:auto; padding:0 7px; border-radius:999px; background:#e8f1ff; color:#175cd3; font-size:11px; font-weight:700; line-height:18px; }
      :host([data-theme="dark"]) .rc-in { background:rgba(46,144,250,.22); color:#b2ccff; }
      .rc-ctx { margin:0; padding:6px 8px; list-style:none; border-radius:8px; background:color-mix(in srgb, var(--ink) 4%, transparent); display:grid; gap:2px; font-size:12px; }
      .rc-ctx li { display:grid; grid-template-columns:auto minmax(0,1fr); gap:6px; color:var(--muted); }
      .rc-ctx li span { overflow:hidden; display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; word-break:break-word; }
      .rc-ctx li.is-now { color:var(--ink); font-weight:600; }
      .rc-ctx time { font-size:11px; opacity:.8; }
      .rc-ctx code, .rc-addr { padding:0 4px; border-radius:4px; background:rgba(193,31,168,.08); color:#c11fa8; font:11.5px/1.5 Consolas,"SFMono-Regular",Menlo,monospace; }
      :host([data-theme="dark"]) .rc-ctx code, :host([data-theme="dark"]) .rc-addr { background:rgba(217,70,239,.12); color:#f0abfc; }
      .rc-acts { display:flex; gap:6px; }
      .rc-acts .rc-btn.is-main { flex:1; }
      .rc-btn { height:22px; padding:0 8px; border:1px solid var(--line); border-radius:6px; background:transparent; color:inherit; font:700 11.5px/20px inherit; cursor:pointer; text-decoration:none; }
      .rc-btn.is-main { border-color:#2e90fa; background:#2e90fa; color:#fff; }
      .list > .wf-card.wf-jump-flash { animation:rc-jump 1.8s ease-out 1; }
      @keyframes rc-jump { 0%,40% { box-shadow:0 0 0 2px #e5484d; } 100% { box-shadow:0 0 0 0 transparent; } }
    </style>
    <section class="wrap">
      <header class="bar" title="点昵称备注此人、点群名备注这个群、点☆星标。拖动此栏可移动窗口。">
        <div class="brand">
          ${WechatFeed.logoSvg(20)}
          <strong>${title}</strong>
        </div>
        <div class="mode">
          <button type="button" data-all class="on">全部</button>
          <button type="button" data-ca>只看CA</button>
        </div>
        <button type="button" data-recent title="最近 10 条 CA" aria-label="最近 10 条 CA"><svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"><circle cx="8" cy="8" r="6.2" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M8 4.6V8l2.4 1.6" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg><span class="rc-badge" hidden></span></button>
        <button type="button" data-groups>群</button>
        <button type="button" data-collapse>收起</button>
      </header>
      <div class="rc-bar" hidden></div>
      <div class="rc-panel" hidden>
        <div class="rc-head"><strong>最近 CA</strong><span class="rc-sub">只看 CA 的群来源 · 按合约去重</span></div>
        <div class="rc-list"></div>
      </div>
      <div class="gpick" hidden>
        <p class="gpick-title">群聊选择管理</p>
        <div class="gpick-list"></div>
      </div>
      <div class="list"><div class="empty">等待群里的新发言。</div></div>
      <button type="button" class="totop" data-totop hidden title="回到最新" aria-label="回到最新">↑</button>
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

  function syncGroupButton(host) {
    if (!host || !host._groups) return;
    const allCount = WechatFeed.feedGroupIds(settings, false).length;
    const caCount = WechatFeed.feedGroupIds(settings, true).length;
    host._groups.textContent = allCount || caCount ? `群 ${allCount || "全"}/${caCount || "全"}` : "群";
    host._groups.title = `全部消息：${allCount ? allCount + " 个群" : "全部监控群"}；只看 CA：${caCount ? caCount + " 个群" : "全部监控群"}`;
  }

  function fillGroupColumn(list, storedKey, title) {
    const heading = document.createElement("h4");
    heading.textContent = title;
    list.append(heading);
    const monitored = WechatFeed.monitoredGroupIds(settings);
    const stored = settings[storedKey] || [];
    const selected = new Set(WechatFeed.selectedFeedGroups(stored, monitored));
    const allLabel = document.createElement("label");
    const allBox = document.createElement("input");
    allBox.type = "checkbox";
    allBox.checked = !stored.length;
    allBox.dataset.feedKey = storedKey;
    allBox.dataset.group = "";
    allLabel.append(allBox, document.createTextNode("全选监控群"));
    list.append(allLabel);
    for (const item of settings.groups || []) {
      if ((settings.blocked_groups || []).includes(item.id)) continue;
      const label = document.createElement("label");
      const check = document.createElement("input");
      check.type = "checkbox";
      check.checked = selected.has(item.id);
      check.dataset.feedKey = storedKey;
      check.dataset.group = item.id;
      label.append(check, document.createTextNode((settings.group_remarks || {})[item.id] || item.name || item.id));
      list.append(label);
    }
  }

  function fillGroupPicker(host) {
    const box = host && host._gpick;
    if (!box) return;
    const list = box.querySelector(".gpick-list");
    list.replaceChildren();
    fillGroupColumn(list, "all_feed_groups", "全部消息来源");
    fillGroupColumn(list, "ca_feed_groups", "只看 CA 来源");
  }

  function wireChrome(host, shadow, { collapse } = {}) {
    const list = shadow.querySelector(".list");
    const all = shadow.querySelector("[data-all]");
    const ca = shadow.querySelector("[data-ca]");
    host._list = list;
    host._all = all;
    host._ca = ca;
    host._groups = shadow.querySelector("[data-groups]");
    host._gpick = shadow.querySelector(".gpick");
    host._toTop = shadow.querySelector("[data-totop]");
    floatLimit = FLOAT_PAGE;
    bindList(shadow, list);
    bindFloatScroll(host, list);
    all.addEventListener("click", () => {
      settings.float_ca_only = false;
      if (host._gpick) host._gpick.hidden = true;
      paintAll();
      send("setSurfaceMode", { surface: "float", enabled: false });
    });
    ca.addEventListener("click", () => {
      settings.float_ca_only = true;
      if (host._gpick) host._gpick.hidden = true;
      paintAll();
      send("setSurfaceMode", { surface: "float", enabled: true });
    });
    host._groups.addEventListener("click", (event) => {
      event.stopPropagation();
      const box = host._gpick;
      if (!box.hidden) {
        box.hidden = true;
        return;
      }
      fillGroupPicker(host);
      box.hidden = false;
    });
    host._gpick.addEventListener("change", (event) => {
      const box = event.target.closest("input[data-feed-key]");
      if (!box) return;
      const key = box.dataset.feedKey;
      const monitored = WechatFeed.monitoredGroupIds(settings);
      const next = WechatFeed.nextFeedGroups(settings[key] || [], box.dataset.group || "", monitored);
      settings[key] = next;
      fillGroupPicker(host);
      paintAll();
      const saved = send("setFeedGroups", { caOnly: key === "ca_feed_groups", groups: next });
      // 本地已先改了 settings，后台回推的设置比对不出变化，这里主动重拉最近 CA。
      if (key === "ca_feed_groups") saved.then(requestRecentCa);
    });
    shadow.querySelector("[data-collapse]").addEventListener("click", collapse);
    wireRecent(host, shadow);
    syncGroupButton(host);
    paintList(list);
  }

  // ---- 最近 CA ----

  function quoteTarget(entry) {
    const token = entry.token || {};
    const chain = String(token.chain || "").toLowerCase();
    const address = token.address || entry.address;
    return chain && address ? { chain, address } : null;
  }

  function quoteKey(target) {
    return `${target.chain}|${WechatFeed.caKey(target.address)}`;
  }

  function recentEntries() {
    if (typeof WechatFeed === "undefined" || !WechatFeed.recentCaEntries) return [];
    return WechatFeed.recentCaEntries(Array.from(caCards.values()), settings, RECENT_LIMIT).map((entry) => {
      const next = { ...entry, card: present(entry.card) };
      const target = quoteTarget(entry);
      const quote = target && quotes.get(quoteKey(target));
      // 本地库里的最新市值覆盖消息里的快照；倍数口径不变（相对所有群首发市值）。
      if (quote && quote.marketCap > 0) {
        next.token = {
          ...entry.token,
          marketCap: quote.marketCap,
          firstMcap: quote.firstMcap,
          athMarketCap: quote.athMarketCap,
          nowX: quote.nowX,
          maxX: quote.maxX,
        };
        next.quotedAt = quote.refreshedAt;
      }
      return next;
    });
  }

  function scheduleQuotes(delay) {
    clearTimeout(quoteTimer);
    quoteTimer = setTimeout(pollQuotes, delay);
  }

  async function pollQuotes() {
    const host = floatHost;
    if (!host || !host._rcBar || !extensionAlive()) return;
    const open = !host._rcPanel.hidden;
    const next = open ? QUOTE_OPEN_MS : QUOTE_BAR_MS;
    // 标签页在后台或已经在请求中：不看就不刷，回到前台由 visibilitychange 立即补一次。
    if (document.hidden || quoteBusy) {
      scheduleQuotes(next);
      return;
    }
    const entries = recentEntries();
    const targets = (open ? entries : entries.slice(0, 1)).map(quoteTarget).filter(Boolean);
    if (!targets.length) {
      scheduleQuotes(next);
      return;
    }
    quoteBusy = true;
    try {
      const result = await send("caQuotes", { items: targets, watch: true });
      for (const quote of result?.quotes || []) {
        quotes.set(quoteKey({ chain: String(quote.chain || "").toLowerCase(), address: quote.address }), quote);
      }
      if (result?.quotes?.length && floatHost === host) paintRecent(host);
    } finally {
      quoteBusy = false;
      if (floatHost === host) scheduleQuotes(next);
    }
  }

  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && floatHost && floatHost._rcBar) scheduleQuotes(0);
  });

  function markRecentSeen(entries) {
    const latest = entries.reduce((max, entry) => Math.max(max, Number(entry.card.createTime) || 0), 0);
    if (latest <= recentSeen) return;
    recentSeen = latest;
    try {
      chrome.storage.local.set({ recentCaSeen: recentSeen });
    } catch (error) {
      // 插件重载后旧脚本的 storage 失效，下次打开再记
    }
  }

  function paintRecent(host) {
    if (!host || !host._rcBar) return;
    const entries = recentEntries();
    const open = !host._rcPanel.hidden;
    if (open) markRecentSeen(entries);
    const unread = entries.filter((entry) => (Number(entry.card.createTime) || 0) > recentSeen).length;
    host._rcBadge.hidden = !unread;
    host._rcBadge.textContent = unread > 9 ? "9+" : String(unread);
    const top = entries[0];
    host._rcBar.hidden = !top;
    if (top) {
      const html = WechatFeed.recentTickHtml(top);
      if (host._rcBar.__wfHtml !== html) {
        host._rcBar.__wfHtml = html;
        host._rcBar.innerHTML = html;
        // 只有换了一个新的最新 CA 才闪一下，市值刷新不闪。
        const topKey = `${top.key}:${top.card.id}`;
        if (recentTop && recentTop !== topKey) host._rcBar.firstElementChild.classList.add("is-new");
        recentTop = topKey;
        hydrate(host._rcBar);
        WechatFeed.startFreshTick(host._rcBar);
      }
    } else {
      host._rcBar.__wfHtml = "";
      host._rcBar.replaceChildren();
    }
    if (!open) return;
    const list = host._rcList;
    if (!entries.length) {
      list.innerHTML = `<div class="empty">关注群里还没有 CA。</div>`;
      return;
    }
    if (list.querySelector(":scope > .empty")) list.replaceChildren();
    // 打开那一刻的已读线：本次打开前没看过的条目保留未读底色，直到关掉。
    const seenLine = host._rcSeenAtOpen || 0;
    const created = WechatFeed.syncList(
      list,
      entries,
      (entry) => entry.key,
      (entry) => WechatFeed.recentItemHtml(entry, (Number(entry.card.createTime) || 0) > seenLine),
    );
    created.forEach((node) => hydrate(node));
    WechatFeed.startFreshTick(list);
  }

  function setRecentOpen(host, open) {
    const panel = host._rcPanel;
    if (open === !panel.hidden) return;
    if (open) {
      if (host._gpick) host._gpick.hidden = true;
      host._rcSeenAtOpen = recentSeen;
      const below = host._rcBar.hidden ? host._rcBar.previousElementSibling : host._rcBar;
      panel.style.top = `${below.offsetTop + below.offsetHeight + 4}px`;
      panel.hidden = false;
      host._rcList.replaceChildren();
    } else {
      panel.hidden = true;
    }
    host._rcBtn.classList.toggle("on", open);
    paintRecent(host);
    // 打开面板立刻读一次并加急；关上后退回只刷顶部一行的节奏。
    scheduleQuotes(open ? 0 : QUOTE_BAR_MS);
  }

  function flashButton(button, text) {
    if (!button.dataset.label) button.dataset.label = button.textContent;
    button.textContent = text;
    clearTimeout(button._reset);
    button._reset = setTimeout(() => {
      button.textContent = button.dataset.label;
    }, 1400);
  }

  function jumpToCard(host, cardId, button) {
    const card = cards.get(cardId);
    if (!card) {
      flashButton(button, "较早未加载");
      return;
    }
    // 当前模式/群来源下看不到这条，就切到只看 CA（它一定在 CA 来源里）。
    if (!WechatFeed.cardInFeed(card, settings, Boolean(settings.float_ca_only))) {
      settings.float_ca_only = true;
      send("setSurfaceMode", { surface: "float", enabled: true });
    }
    const index = visibleCards(settings.float_ca_only).findIndex((item) => item.id === cardId);
    if (index < 0) {
      flashButton(button, "当前筛选看不到");
      return;
    }
    if (index >= floatLimit) floatLimit = Math.ceil((index + 1) / FLOAT_PAGE) * FLOAT_PAGE;
    paintAll();
    const node = host._list.querySelector(`:scope > [data-id="${CSS.escape(cardId)}"]`);
    if (!node) {
      flashButton(button, "没找到");
      return;
    }
    setRecentOpen(host, false);
    node.scrollIntoView({ block: "center", behavior: "smooth" });
    node.classList.remove("wf-jump-flash");
    void node.offsetWidth;
    node.classList.add("wf-jump-flash");
  }

  function wireRecent(host, shadow) {
    host._rcBtn = shadow.querySelector("[data-recent]");
    host._rcBadge = shadow.querySelector(".rc-badge");
    host._rcBar = shadow.querySelector(".rc-bar");
    host._rcPanel = shadow.querySelector(".rc-panel");
    host._rcList = shadow.querySelector(".rc-list");
    // 标题栏整条可拖动，按钮上按下不能触发拖动。
    host._rcBtn.addEventListener("mousedown", (event) => event.stopPropagation());
    host._rcBtn.addEventListener("click", (event) => {
      event.stopPropagation();
      setRecentOpen(host, host._rcPanel.hidden);
    });
    // 顶部这一行：「复制」走通用复制，其余位置点开 / 收起面板。
    bindList(shadow, host._rcBar);
    host._rcBar.addEventListener("click", (event) => {
      if (event.target.closest("[data-copy]")) return;
      setRecentOpen(host, host._rcPanel.hidden);
    });
    // 复制、点头像等沿用列表的通用处理；跳转单独处理。
    bindList(shadow, host._rcList);
    host._rcList.addEventListener("click", (event) => {
      const jump = event.target.closest("[data-jump]");
      if (jump) jumpToCard(host, jump.dataset.jump, jump);
    });
    // 点面板外（含浮窗外）或按 Esc 关闭。
    shadow.addEventListener("mousedown", (event) => {
      if (host._rcPanel.hidden) return;
      const path = event.composedPath();
      if (path.includes(host._rcPanel) || path.includes(host._rcBtn) || path.includes(host._rcBar)) return;
      setRecentOpen(host, false);
    });
    const outside = (event) => {
      if (!host.isConnected) {
        document.removeEventListener("mousedown", outside, true);
        document.removeEventListener("keydown", onKey, true);
        return;
      }
      if (!host._rcPanel.hidden && !event.composedPath().includes(host)) setRecentOpen(host, false);
    };
    const onKey = (event) => {
      if (event.key === "Escape" && !host._rcPanel.hidden) setRecentOpen(host, false);
    };
    document.addEventListener("mousedown", outside, true);
    document.addEventListener("keydown", onKey, true);
    paintRecent(host);
    // 等首屏最近 CA 拉回来再读市值。
    scheduleQuotes(3000);
  }

  async function mountFloat() {
    if (floatHost || collapsed) return;
    const rect = await loadRect();
    floatHost = document.createElement("div");
    applyTheme(floatHost);
    applyRect(floatHost, rect);
    const shadow = floatHost.attachShadow({ mode: "closed" });
    shadow.innerHTML = panelShell("技术瓜群流");
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
    shadow.innerHTML = `<style>${WechatFeed.cardCss()}</style><button class="chip" type="button">${WechatFeed.logoSvg(18)}技术瓜群流</button>
      <style>.chip{display:inline-flex;align-items:center;gap:6px;border:0;border-radius:999px;background:#111827;color:#fff;padding:6px 12px 6px 6px;box-shadow:0 8px 24px rgba(0,0,0,.2);cursor:pointer;font:13px/1.4 sans-serif;}.chip svg{flex:none;border-radius:6px;}</style>`;
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
      starred.has(card.senderUsername) ? 1 : 0,
      (settings.star_colors || {})[card.senderUsername] || "",
      (settings.user_remarks || {})[card.senderUsername] || "",
      (settings.group_remarks || {})[card.conversationId] || "",
      card.text || "",
      tokenStamp(card),
      (card.contextBefore || []).length + (card.contextAfter || []).length,
    ].join(":");
  }

  function dialogRoot() {
    if (dialogHost && dialogHost.isConnected) return dialogHost._shadow;
    dialogHost = document.createElement("div");
    dialogHost.style.cssText = "position:fixed;inset:0;z-index:2147483647;pointer-events:none;";
    const shadow = dialogHost.attachShadow({ mode: "closed" });
    shadow.innerHTML = "<style>.wf-ask{position:fixed !important;inset:0 !important;pointer-events:auto;}</style>";
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
      .list { display:flex; flex-direction:column; gap:0; padding:8px 8px 0; }
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
    // 只换变了的卡：整段重建会让插入的高度抖动，gmgn 的虚拟列表跟着重新测量、跳位置。
    const created = WechatFeed.syncList(
      host._list,
      rows,
      (card) => card.id,
      (card, index) => WechatFeed.gmgnCardHtml(present(card), WechatFeed.stackMeta(rows, index)),
    );
    created.forEach((node) => hydrate(node));
    if (WechatFeed.startFreshTick) WechatFeed.startFreshTick(host._list);
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
    const rows = visibleCards(settings.gmgn_ca_only);
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

  function shouldFillMore() {
    if (!hasMore || snapshotPending || fillPages >= FILL_PAGES) return false;
    if (floatWanted() && visibleCards(settings.float_ca_only).length < FILL_VISIBLE) return true;
    if (overlayOn() && isFollowPage() && visibleCards(settings.gmgn_ca_only).length < FILL_VISIBLE) {
      return true;
    }
    return false;
  }

  function requestSnapshot(append) {
    if (!port) return;
    if (append && snapshotPending) return;
    if (!append) {
      cards.clear();
      oldest = 0;
      oldestReceived = 0;
      fillPages = 0;
    }
    const payload = { type: "snapshot" };
    if (append && oldest) payload.before = oldest;
    if (append && oldestReceived) payload.before_received = oldestReceived;
    snapshotPending = true;
    port.postMessage(payload);
  }

  function rememberCa(card) {
    if (!card || !card.id || !(card.addresses || []).length) return;
    caCards.set(card.id, card);
    if (caCards.size <= CA_KEEP) return;
    const stale = Array.from(caCards.values())
      .sort((a, b) => (a.createTime || 0) - (b.createTime || 0))
      .slice(0, caCards.size - CA_KEEP);
    for (const item of stale) caCards.delete(item.id);
  }

  function requestRecentCa() {
    if (port) port.postMessage({ type: "recentCa" });
  }

  function remember(card, paint = true) {
    if (!card || !card.id) return;
    rememberCa(card);
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
    if (port || !extensionAlive()) return;
    try {
      port = chrome.runtime.connect({ name: "gmgn-feed" });
    } catch (error) {
      port = null;
      return;
    }
    port.onMessage.addListener((message) => {
      if (message.type === "settings" && message.settings) {
        const overlayChanged = Boolean(settings.gmgn_overlay) !== Boolean(message.settings.gmgn_overlay);
        const floatChanged = Boolean(settings.float_panel) !== Boolean(message.settings.float_panel);
        const listenChanged =
          JSON.stringify(settings.group_users || {}) !== JSON.stringify(message.settings.group_users || {})
          || JSON.stringify(settings.blocked_users || []) !== JSON.stringify(message.settings.blocked_users || []);
        const caGroupsChanged =
          JSON.stringify(settings.ca_feed_groups || []) !== JSON.stringify(message.settings.ca_feed_groups || []);
        settings = { ...settings, ...message.settings };
        if (typeof WechatFeed !== "undefined") WechatFeed.setNumberFormat(settings.mcap_format);
        if (overlayChanged || floatChanged) applySurface();
        else paintAll();
        if (listenChanged) requestSnapshot(false);
        if (listenChanged || caGroupsChanged) requestRecentCa();
      }
      if (message.type === "snapshot") {
        snapshotPending = false;
        if (!message.append) cards.clear();
        for (const card of message.cards || []) remember(card, false);
        hasMore = Boolean(message.has_more);
        paintAll();
        if (shouldFillMore()) {
          fillPages += 1;
          requestSnapshot(true);
        }
      }
      if (message.type === "recentCa") {
        for (const card of message.cards || []) rememberCa(card);
        paintAll();
      }
      if (message.type === "message") remember(message.card);
      if (message.type === "patch" && message.card) remember(message.card);
      if (message.type === "cleared") {
        cards.clear();
        caCards.clear();
        paintAll();
      }
    });
    port.onDisconnect.addListener(() => {
      port = null;
      snapshotPending = false;
      if (!extensionAlive()) return;
      if (floatWanted() || (overlayOn() && isGmgn())) {
        if (reconnectTimer) clearTimeout(reconnectTimer);
        reconnectTimer = setTimeout(connect, 800);
      }
    });
    requestSnapshot(false);
    requestRecentCa();
  }

  function disconnect() {
    if (reconnectTimer) {
      clearTimeout(reconnectTimer);
      reconnectTimer = 0;
    }
    try {
      if (port) port.disconnect();
    } catch (error) {
      // 插件重载后旧端口已经失效
    }
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
    if (typeof WechatFeed !== "undefined") WechatFeed.setNumberFormat(settings.mcap_format);
  }

  chrome.storage.onChanged.addListener((changes, area) => {
    if (!extensionAlive() || area !== "local") return;
    // 别的标签页看过最近 CA，这里的角标跟着清。
    if (changes.recentCaSeen) {
      const seen = Number(changes.recentCaSeen.newValue) || 0;
      if (seen > recentSeen) {
        recentSeen = seen;
        if (floatHost) paintRecent(floatHost);
      }
    }
    const stored = {};
    if (changes.settings) stored.settings = changes.settings.newValue;
    if (changes.gmgnOverlay) stored.gmgnOverlay = changes.gmgnOverlay.newValue;
    if (changes.floatPanel) stored.floatPanel = changes.floatPanel.newValue;
    if (changes.caOnly) stored.caOnly = changes.caOnly.newValue;
    if (changes.floatSites) stored.floatSites = changes.floatSites.newValue || DEFAULT_FLOAT_SITES;
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
      const stored = await chrome.storage.local.get([
        "gmgnOverlay",
        "floatPanel",
        "caOnly",
        "settings",
        "floatSites",
        "recentCaSeen",
      ]);
      // 第一次用时把「已读线」定在现在，免得一打开就是 10 条未读。
      recentSeen = Number(stored.recentCaSeen) || Math.floor(Date.now() / 1000);
      if (!stored.recentCaSeen) chrome.storage.local.set({ recentCaSeen: recentSeen });
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
      if (typeof WechatFeed !== "undefined") WechatFeed.setNumberFormat(settings.mcap_format);
      applySurface();
    }
  }

  // 单页应用切换路由不会重新注入脚本，网址变了要重新判断白名单。
  setInterval(() => {
    if (!extensionAlive() || location.href === lastHref) return;
    lastHref = location.href;
    applySurface();
  }, 1000);

  boot();
})();
