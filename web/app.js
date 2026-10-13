const PAGE_SIZE = 30;

const state = {
  settings: {
    groups: [],
    gmgn_overlay: false,
    float_panel: false,
    blocked_groups: [],
    user_remarks: {},
    group_remarks: {},
    starred_users: [],
    ca_only: false,
  },
  runtime: { upstream: "idle", detail: "正在读取状态" },
  messages: [],
  directory: [],
  directoryQuery: "",
  query: "",
  activeGroup: "",
  hasMore: false,
  oldest: 0,
  oldestReceived: 0,
  loadingMore: false,
};

const feed = document.querySelector("#feed");
const more = document.querySelector("#more");
const groups = document.querySelector("#groups");
const blockedBox = document.querySelector("#blocked");
const directory = document.querySelector("#directory");
const directorySearch = document.querySelector(".directory-search");
const directoryQuery = document.querySelector("#directory-query");
const notice = document.querySelector("#notice");
const runtime = document.querySelector("#runtime");
const gmgn = document.querySelector("#gmgn");
const floatPanel = document.querySelector("#float-panel");
const query = document.querySelector("#query");
const modeAll = document.querySelector("#mode-all");
const modeCa = document.querySelector("#mode-ca");

function setNotice(text) {
  notice.textContent = text || "";
}

function groupLabel(groupId, fallback) {
  if (!groupId) return "全部群";
  const remark = (state.settings.group_remarks || {})[groupId];
  if (remark) return remark;
  const watched = (state.settings.groups || []).find((item) => item.id === groupId);
  if (watched && watched.name && !String(watched.name).endsWith("@chatroom")) return watched.name;
  if (fallback && !String(fallback).endsWith("@chatroom")) return fallback;
  const fromMsg = state.messages.find((item) => item.conversationId === groupId);
  const fromName = fromMsg && (fromMsg.conversationName || fromMsg.displayGroup);
  if (fromName && !String(fromName).endsWith("@chatroom")) return fromName;
  return "未命名群";
}

function present(card) {
  const userRemarks = state.settings.user_remarks || {};
  const groupRemarks = state.settings.group_remarks || {};
  const starred = new Set(state.settings.starred_users || []);
  const remarkName = userRemarks[card.senderUsername] || "";
  const remarkGroup = groupRemarks[card.conversationId] || "";
  const originalName = card.senderName || "群成员";
  const originalGroup = groupLabel(card.conversationId, card.conversationName);
  return {
    ...card,
    displayName: remarkName || originalName,
    displayGroup: remarkGroup || originalGroup,
    nameRemarked: Boolean(remarkName),
    groupRemarked: Boolean(remarkGroup),
    originalName: remarkName && remarkName !== originalName ? originalName : "",
    originalGroup: remarkGroup && remarkGroup !== originalGroup ? originalGroup : "",
    starred: starred.has(card.senderUsername),
    starColor: (state.settings.star_colors || {})[card.senderUsername] || "amber",
    avatarUrl: card.senderUsername
      ? `/api/avatar?username=${encodeURIComponent(card.senderUsername)}`
      : "",
    quoteUsername: card.quoteUsername || "",
    quoteAvatarUrl: card.quoteUsername
      ? `/api/avatar?username=${encodeURIComponent(card.quoteUsername)}`
      : card.quoteAvatarUrl || "",
  };
}

function visibleMessages() {
  const needle = state.query.trim().toLowerCase();
  const blocked = new Set(state.settings.blocked_groups || []);
  return state.messages.filter((card) => {
    if (blocked.has(card.conversationId)) return false;
    if (state.activeGroup && card.conversationId !== state.activeGroup) return false;
    if (state.settings.ca_only && !(card.addresses || []).length) return false;
    if (!needle) return true;
    const shown = present(card);
    const blob = [shown.displayName, shown.displayGroup, card.text, card.quoteText, ...(card.addresses || [])]
      .join("\n")
      .toLowerCase();
    return blob.includes(needle);
  });
}

function renderRuntime() {
  const status = state.runtime.upstream || "idle";
  runtime.className = `runtime ${status === "live" ? "live" : status === "error" ? "error" : ""}`;
  runtime.textContent = state.runtime.detail || status;
}

function renderMode() {
  modeAll.classList.toggle("active", !state.settings.ca_only);
  modeCa.classList.toggle("active", Boolean(state.settings.ca_only));
}

function renderGroups() {
  const items = [{ id: "", name: "全部群" }, ...state.settings.groups];
  groups.replaceChildren();
  for (const item of items) {
    if ((state.settings.blocked_groups || []).includes(item.id) && item.id) continue;
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = groupLabel(item.id, item.name);
    button.title = item.id || "全部群";
    if (item.id === state.activeGroup) button.className = "active";
    button.addEventListener("click", () => {
      state.activeGroup = item.id;
      renderGroups();
      loadFeed({ reset: true }).catch((error) => setNotice(error.message));
    });
    if (item.id) {
      button.addEventListener("dblclick", (event) => {
        event.preventDefault();
        remarkGroup(item.id, groupLabel(item.id, item.name));
      });
    }
    groups.append(button);
  }
}

function renderBlocked() {
  blockedBox.replaceChildren();
  for (const groupId of state.settings.blocked_groups || []) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = `恢复 ${groupLabel(groupId, groupId)}`;
    button.addEventListener("click", () => unmuteGroup(groupId));
    blockedBox.append(button);
  }
}

function renderFeed() {
  const cards = visibleMessages();
  if (!cards.length) {
    feed.innerHTML = "";
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = state.messages.length
      ? "当前筛选下没有发言。可继续下拉加载更早的消息。"
      : "还没有群消息。微信数据服务连上后，新发言会出现在这里。";
    feed.append(empty);
    more.hidden = true;
    return;
  }
  feed.innerHTML = cards.map((card) => WechatFeed.cardHtml(present(card))).join("");
  more.hidden = !state.hasMore;
  more.textContent = state.loadingMore ? "正在加载…" : "加载更早的消息…";
}

function renderDirectory() {
  const needle = state.directoryQuery.trim().toLowerCase();
  const rows = (state.directory || []).filter((group) => {
    if (!needle) return true;
    return `${group.name || ""} ${group.id}`.toLowerCase().includes(needle);
  });
  directorySearch.hidden = !state.directory.length;
  directory.replaceChildren();
  for (const group of rows.slice(0, 12)) {
    const button = document.createElement("button");
    button.type = "button";
    const name = document.createElement("strong");
    name.textContent = group.name || "未命名群";
    const id = document.createElement("span");
    id.textContent = group.id;
    button.append(name, id);
    button.addEventListener("click", () => addGroup(group.id, group.name));
    directory.append(button);
  }
  if (rows.length > 12) {
    const hint = document.createElement("p");
    hint.className = "notice";
    hint.textContent = `还有 ${rows.length - 12} 个群，输入关键字继续筛选。`;
    directory.append(hint);
  }
}

function mergeSettings(next) {
  const incoming = { ...(next || {}) };
  delete incoming.gmgn_api_key;
  state.settings = { ...state.settings, ...incoming };
}

function renderKey() {
  const keyInput = document.querySelector("#gmgn-key");
  const status = document.querySelector("#key-status");
  if (!keyInput) return;
  if (!keyInput.dataset.dirty) {
    keyInput.value = "";
    keyInput.placeholder = state.settings.has_gmgn_key
      ? "已保存。输入新 Key 可覆盖，留空再保存则清空"
      : "gmgn_ 开头的 Key，用于代币头像和税率";
  }
  if (status) status.textContent = state.settings.has_gmgn_key ? "本机已保存 Key" : "尚未填写";
}

function render() {
  gmgn.checked = Boolean(state.settings.gmgn_overlay);
  floatPanel.checked = Boolean(state.settings.float_panel);
  renderKey();
  renderRuntime();
  renderMode();
  renderGroups();
  renderBlocked();
  renderDirectory();
  renderFeed();
}

async function readJson(response) {
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.detail || "请求失败");
  }
  return payload;
}

async function saveSettings(patch) {
  const settings = await readJson(
    await fetch("/api/settings", {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(patch),
    }),
  );
  mergeSettings(settings);
  render();
  return state.settings;
}

async function loadFeed({ reset = false } = {}) {
  if (state.loadingMore) return;
  if (!reset && !state.hasMore) return;
  state.loadingMore = true;
  renderFeed();
  try {
    const params = new URLSearchParams({ limit: String(PAGE_SIZE) });
    if (state.settings.ca_only) params.set("ca_only", "true");
    if (state.activeGroup) params.set("conversation_id", state.activeGroup);
    if (!reset && state.oldest) {
      params.set("before", String(state.oldest));
      if (state.oldestReceived) params.set("before_received", String(state.oldestReceived));
    }
    const payload = await readJson(await fetch(`/api/messages?${params}`));
    const incoming = payload.messages || [];
    if (reset) {
      state.messages = incoming;
    } else {
      const seen = new Set(state.messages.map((item) => item.id));
      state.messages = state.messages.concat(incoming.filter((item) => !seen.has(item.id)));
    }
    state.hasMore = Boolean(payload.has_more);
    if (incoming.length) {
      const last = incoming[incoming.length - 1];
      state.oldest = Number(last.createTime) || 0;
      state.oldestReceived = Number(last.receivedAt) || 0;
    }
    if (reset && !incoming.length) {
      state.oldest = 0;
      state.oldestReceived = 0;
    }
  } finally {
    state.loadingMore = false;
    renderFeed();
  }
}

async function addGroup(id, name) {
  const next = state.settings.groups.filter((item) => item.id !== id);
  next.push({ id, name: name || "" });
  const blocked = (state.settings.blocked_groups || []).filter((item) => item !== id);
  await saveSettings({ groups: next, blocked_groups: blocked });
  setNotice(`已监控 ${name || id}`);
}

async function muteGroup(groupId) {
  if (!groupId) return;
  const blocked = Array.from(new Set([...(state.settings.blocked_groups || []), groupId]));
  await saveSettings({ blocked_groups: blocked });
  setNotice("已屏蔽这个群。点上方红色按钮可恢复。");
}

async function unmuteGroup(groupId) {
  const blocked = (state.settings.blocked_groups || []).filter((item) => item !== groupId);
  await saveSettings({ blocked_groups: blocked });
}

function pickStarColor(anchor, current) {
  const colors = window.WechatFeed?.starColors || [];
  return new Promise((resolve) => {
    const host = document.createElement("div");
    host.className = "star-palette";
    const rect = anchor.getBoundingClientRect();
    host.style.left = `${Math.max(8, Math.min(rect.left, window.innerWidth - 210))}px`;
    host.style.top = `${Math.min(rect.bottom + 6, window.innerHeight - 88)}px`;
    host.innerHTML = `${colors
      .map((item) => `<button type="button" data-color="${item.id}" title="${item.name}" style="background:${item.hex};outline:${current === item.id ? "2px solid #111" : "none"}"></button>`)
      .join("")}<button type="button" data-color="">取消</button>`;
    const close = (value) => {
      host.remove();
      document.removeEventListener("mousedown", outside);
      resolve(value);
    };
    const outside = (event) => {
      if (!host.contains(event.target)) close(null);
    };
    host.addEventListener("click", (event) => {
      const button = event.target.closest("[data-color]");
      if (!button) return;
      close(button.dataset.color);
    });
    document.body.append(host);
    setTimeout(() => document.addEventListener("mousedown", outside), 0);
  });
}

async function toggleStar(userId, anchor) {
  if (!userId) return;
  const current = new Set(state.settings.starred_users || []);
  const colors = { ...(state.settings.star_colors || {}) };
  const picked = await pickStarColor(anchor, current.has(userId) ? colors[userId] || "amber" : "");
  if (picked === null) return;
  if (!picked) {
    current.delete(userId);
    delete colors[userId];
  } else {
    current.add(userId);
    colors[userId] = picked;
  }
  await saveSettings({ starred_users: Array.from(current), star_colors: colors });
}

function ask(title, value) {
  const root = document.querySelector("#dialog");
  const titleNode = document.querySelector("#dialog-title");
  const input = document.querySelector("#dialog-input");
  const ok = root.querySelector("[data-ok]");
  const cancel = root.querySelector("[data-cancel]");
  titleNode.textContent = title;
  input.value = value || "";
  root.hidden = false;
  input.focus();
  input.select();
  return new Promise((resolve) => {
    const finish = (result) => {
      root.hidden = true;
      ok.onclick = null;
      cancel.onclick = null;
      root.onclick = null;
      resolve(result);
    };
    ok.onclick = () => finish(input.value);
    cancel.onclick = () => finish(null);
    root.onclick = (event) => {
      if (event.target === root) finish(null);
    };
    input.onkeydown = (event) => {
      if (event.key === "Enter") finish(input.value);
      if (event.key === "Escape") finish(null);
    };
  });
}

async function remarkUser(userId, current) {
  const next = await ask("给这个人的备注", current || "");
  if (next === null) return;
  const remarks = { ...(state.settings.user_remarks || {}) };
  if (next.trim()) remarks[userId] = next.trim();
  else delete remarks[userId];
  await saveSettings({ user_remarks: remarks });
}

async function remarkGroup(groupId, current) {
  const next = await ask("给这个群的备注", current || "");
  if (next === null) return;
  const remarks = { ...(state.settings.group_remarks || {}) };
  if (next.trim()) remarks[groupId] = next.trim();
  else delete remarks[groupId];
  const nextGroups = (state.settings.groups || []).map((item) =>
    item.id === groupId ? { ...item, name: next.trim() } : item,
  );
  await saveSettings({ group_remarks: remarks, groups: nextGroups });
}

async function setCaOnly(enabled) {
  await saveSettings({ ca_only: Boolean(enabled) });
  await loadFeed({ reset: true });
}

async function load() {
  const [settings, health] = await Promise.all([
    readJson(await fetch("/api/settings")),
    readJson(await fetch("/api/health")),
  ]);
  mergeSettings(settings);
  state.runtime = health.runtime || state.runtime;
  render();
  await loadFeed({ reset: true });
}

query.addEventListener("input", () => {
  state.query = query.value;
  renderFeed();
});

directoryQuery.addEventListener("input", () => {
  state.directoryQuery = directoryQuery.value;
  renderDirectory();
});

modeAll.addEventListener("click", () => setCaOnly(false));
modeCa.addEventListener("click", () => setCaOnly(true));

gmgn.addEventListener("change", async () => {
  try {
    await saveSettings({ gmgn_overlay: gmgn.checked });
    setNotice(gmgn.checked ? "已打开 gmgn 信息流插入。刷新 gmgn 页面后会出现微信卡片。" : "gmgn 信息流插入已关闭。");
  } catch (error) {
    gmgn.checked = Boolean(state.settings.gmgn_overlay);
    setNotice(error.message);
  }
});

floatPanel.addEventListener("change", async () => {
  try {
    await saveSettings({ float_panel: floatPanel.checked });
    setNotice(floatPanel.checked ? "已打开白名单网页浮窗。" : "白名单网页浮窗已关闭。");
  } catch (error) {
    floatPanel.checked = Boolean(state.settings.float_panel);
    setNotice(error.message);
  }
});

document.querySelector("#add-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const id = document.querySelector("#group-id").value.trim();
  const name = document.querySelector("#group-name").value.trim();
  try {
    await addGroup(id, name);
    event.target.reset();
  } catch (error) {
    setNotice(error.message);
  }
});

document.querySelector("#load-groups").addEventListener("click", async () => {
  setNotice("正在读取本机群列表");
  try {
    const payload = await readJson(await fetch("/api/groups"));
    state.directory = payload.groups || [];
    const named = Object.fromEntries(state.directory.map((item) => [item.id, item.name || ""]));
    const nextGroups = (state.settings.groups || []).map((item) => {
      const current = item.name || "";
      const incoming = named[item.id] || "";
      if (current && !current.endsWith("@chatroom")) return item;
      if (incoming && !incoming.endsWith("@chatroom")) return { ...item, name: incoming };
      return item;
    });
    if (JSON.stringify(nextGroups) !== JSON.stringify(state.settings.groups)) {
      await saveSettings({ groups: nextGroups });
    }
    renderDirectory();
    setNotice(state.directory.length ? "输入关键字筛选群，点击即可加入监控。" : "没有读到群会话。");
  } catch (error) {
    setNotice(error.message);
  }
});

document.querySelector("#demo").addEventListener("click", async () => {
  try {
    await readJson(await fetch("/api/demo", { method: "POST" }));
  } catch (error) {
    setNotice(error.message);
  }
});

document.querySelector("#clear").addEventListener("click", async () => {
  try {
    await readJson(await fetch("/api/messages", { method: "DELETE" }));
  } catch (error) {
    setNotice(error.message);
  }
});

document.querySelector("#gmgn-key").addEventListener("input", () => {
  document.querySelector("#gmgn-key").dataset.dirty = "1";
});

document.querySelector("#save-key").addEventListener("click", async () => {
  const key = document.querySelector("#gmgn-key").value.trim();
  try {
    await saveSettings({ gmgn_api_key: key });
    document.querySelector("#gmgn-key").dataset.dirty = "";
    setNotice(key ? "GMGN Key 已保存，新合约会走 OpenAPI 补全头像和税率。" : "已清空 GMGN Key。");
  } catch (error) {
    setNotice(error.message);
  }
});

let imagePreview = null;

function showImagePreview(img) {
  const src = img.currentSrc || img.getAttribute("src") || "";
  if (!src) return;
  if (!imagePreview) {
    imagePreview = document.createElement("img");
    imagePreview.alt = "预览";
    imagePreview.style.cssText = "position:fixed;z-index:40;max-width:min(480px, calc(100vw - 24px));max-height:min(560px, calc(100vh - 24px));border-radius:12px;box-shadow:0 18px 50px rgba(0,0,0,.38);pointer-events:none;background:#111;object-fit:contain;";
    document.body.append(imagePreview);
  }
  const full = img.dataset.wfFull;
  imagePreview.onerror = full ? () => {
    imagePreview.onerror = null;
    imagePreview.src = src;
  } : null;
  imagePreview.src = full || src;
  const rect = img.getBoundingClientRect();
  const previewWidth = Math.min(480, window.innerWidth - 24);
  let left = rect.right + 12;
  if (left + previewWidth > window.innerWidth - 8) left = Math.max(8, rect.left - previewWidth - 12);
  const top = Math.max(8, Math.min(rect.top, window.innerHeight - 80));
  imagePreview.style.left = `${left}px`;
  imagePreview.style.top = `${top}px`;
  imagePreview.hidden = false;
}

function hideImagePreview() {
  if (imagePreview) imagePreview.hidden = true;
}

feed.addEventListener("mouseover", (event) => {
  const img = event.target.closest(".wf-media");
  if (img) showImagePreview(img);
});

feed.addEventListener("mouseout", (event) => {
  const img = event.target.closest(".wf-media");
  if (!img) return;
  if (event.relatedTarget && img.contains(event.relatedTarget)) return;
  hideImagePreview();
});

feed.addEventListener("click", (event) => {
  const copy = event.target.closest("[data-copy]");
  if (copy) {
    navigator.clipboard.writeText(copy.dataset.copy || "");
    setNotice("已复制合约");
    return;
  }
  const star = event.target.closest("[data-star]");
  if (star) {
    const card = star.closest(".wf-card");
    toggleStar(card && card.dataset.user, star);
    return;
  }
  const mute = event.target.closest("[data-mute]");
  if (mute) {
    const card = mute.closest(".wf-card");
    muteGroup(card && card.dataset.group);
    return;
  }
  const user = event.target.closest("[data-remark-user]");
  if (user) {
    const card = user.closest(".wf-card");
    remarkUser(card && card.dataset.user, user.textContent);
    return;
  }
  const group = event.target.closest("[data-remark-group]");
  if (group) {
    const card = group.closest(".wf-card");
    remarkGroup(card && card.dataset.group, group.textContent);
  }
});

const source = new EventSource("/api/events");
source.onmessage = (event) => {
  const payload = JSON.parse(event.data);
  if (payload.type === "hello" || payload.type === "status") {
    state.runtime = payload.status || state.runtime;
    if (payload.settings) mergeSettings(payload.settings);
    renderRuntime();
    renderKey();
    return;
  }
  if (payload.type === "settings" && payload.settings) {
    mergeSettings(payload.settings);
    render();
    return;
  }
  if (payload.type === "cleared") {
    state.messages = [];
    state.hasMore = false;
    renderFeed();
    return;
  }
  if (payload.type === "patch" && payload.card) {
    state.messages = state.messages.map((item) => (item.id === payload.card.id ? payload.card : item));
    renderFeed();
    return;
  }
  if (payload.type === "message" && payload.card) {
    if (!state.messages.some((item) => item.id === payload.card.id)) {
      state.messages.unshift(payload.card);
      renderFeed();
    }
  }
};

const sentinel = new IntersectionObserver((entries) => {
  if (entries.some((entry) => entry.isIntersecting)) {
    loadFeed().catch((error) => setNotice(error.message));
  }
});
sentinel.observe(more);

load().catch((error) => setNotice(error.message));
