const PAGE_SIZE = 30;

const state = {
  settings: {
    groups: [],
    gmgn_overlay: false,
    float_panel: false,
    blocked_groups: [],
    blocked_users: [],
    user_remarks: {},
    group_remarks: {},
    starred_users: [],
    group_users: {},
    ca_only: false,
    gmgn_ca_only: false,
    float_ca_only: false,
    all_feed_groups: [],
    ca_feed_groups: [],
  },
  runtime: { upstream: "idle", detail: "正在读取状态" },
  messages: [],
  directory: [],
  directoryQuery: "",
  query: "",
  hasMore: false,
  oldest: 0,
  oldestReceived: 0,
  loadingMore: false,
  feeds: {
    all: emptyFeedCache(),
    ca: emptyFeedCache(),
  },
};

function emptyFeedCache() {
  return { messages: [], hasMore: false, oldest: 0, oldestReceived: 0, loaded: false, loading: false };
}

function feedKey(caOnly = Boolean(state.settings.ca_only)) {
  return caOnly ? "ca" : "all";
}

function activeCache() {
  return state.feeds[feedKey()];
}

function syncViewFromCache() {
  const cache = activeCache();
  state.messages = cache.messages;
  state.hasMore = cache.hasMore;
  state.oldest = cache.oldest;
  state.oldestReceived = cache.oldestReceived;
  state.loadingMore = cache.loading;
}

const feed = document.querySelector("#feed");
const more = document.querySelector("#more");
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

function watchedGroupName(groupId, fallback) {
  const watched = (state.settings.groups || []).find((item) => item.id === groupId);
  if (watched && watched.name && !String(watched.name).endsWith("@chatroom")) return watched.name;
  if (fallback && !String(fallback).endsWith("@chatroom")) return fallback;
  const fromMsg = state.messages.find((item) => item.conversationId === groupId);
  const fromName = fromMsg && fromMsg.conversationName;
  if (fromName && !String(fromName).endsWith("@chatroom")) return fromName;
  return "未命名群";
}

function groupLabel(groupId, fallback) {
  if (!groupId) return "全部群";
  const remark = (state.settings.group_remarks || {})[groupId];
  if (remark) return remark;
  return watchedGroupName(groupId, fallback);
}

function present(card) {
  const userRemarks = state.settings.user_remarks || {};
  const groupRemarks = state.settings.group_remarks || {};
  const starred = new Set(state.settings.starred_users || []);
  const remarkName = userRemarks[card.senderUsername] || "";
  const remarkGroup = groupRemarks[card.conversationId] || "";
  const originalName = card.senderName || "群成员";
  const originalGroup = watchedGroupName(card.conversationId, card.conversationName);
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
  const caOnly = Boolean(state.settings.ca_only);
  return state.messages.filter((card) => {
    if (!window.WechatFeed.cardInFeed(card, state.settings, caOnly)) return false;
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

function surfaceCaKey(surface) {
  if (surface === "gmgn") return "gmgn_ca_only";
  if (surface === "float") return "float_ca_only";
  return "ca_only";
}

function renderSurfaceModes() {
  for (const root of document.querySelectorAll(".surface-mode")) {
    const key = surfaceCaKey(root.dataset.surface);
    const caOnly = Boolean(state.settings[key]);
    for (const button of root.querySelectorAll("[data-mode]")) {
      button.classList.toggle("on", button.dataset.mode === (caOnly ? "ca" : "all"));
    }
  }
}

const expandedGroups = new Set();
const memberCache = new Map();
const memberQuery = new Map();

async function ensureMembers(groupId) {
  const cached = memberCache.get(groupId);
  if (cached && Date.now() - cached.at < 15000) return cached.members;
  const payload = await readJson(await fetch(`/api/groups/${encodeURIComponent(groupId)}/members`));
  const members = payload.members || [];
  memberCache.set(groupId, { at: Date.now(), members });
  return members;
}

async function saveGroupUsers(groupId, users) {
  const map = { ...(state.settings.group_users || {}) };
  if (users === null) delete map[groupId];
  else map[groupId] = users;
  await saveSettings({ group_users: map }, { skipFeed: true });
  renderGroups();
  renderFeed();
  state.feeds.all = emptyFeedCache();
  state.feeds.ca = emptyFeedCache();
  await loadFeed({ reset: true });
  loadFeed({ reset: true, caOnly: !state.settings.ca_only }).catch(() => {});
}

function renderMemberPanel(panel, groupId, members) {
  panel.replaceChildren();
  const users = window.WechatFeed.groupListenUsers(state.settings, groupId);
  const partial = users !== null;
  const mode = document.createElement("div");
  mode.className = "listen-mode";
  const allBtn = document.createElement("button");
  allBtn.type = "button";
  allBtn.textContent = "监听全员";
  allBtn.classList.toggle("on", !partial);
  const partBtn = document.createElement("button");
  partBtn.type = "button";
  partBtn.textContent = "只听部分";
  partBtn.classList.toggle("on", partial);
  allBtn.addEventListener("click", () => {
    saveGroupUsers(groupId, null).catch((error) => setNotice(error.message));
  });
  partBtn.addEventListener("click", () => {
    saveGroupUsers(groupId, users || []).catch((error) => setNotice(error.message));
  });
  mode.append(allBtn, partBtn);
  panel.append(mode);
  if (partial && !users.length) {
    const hint = document.createElement("p");
    hint.className = "hint";
    hint.textContent = "勾选要听的人。现在这个群不会显示任何人。";
    panel.append(hint);
  }
  const filter = document.createElement("input");
  filter.type = "search";
  filter.className = "member-filter";
  filter.placeholder = "筛选成员";
  filter.value = memberQuery.get(groupId) || "";
  const list = document.createElement("div");
  list.className = "member-list";
  const starred = new Set(state.settings.starred_users || []);
  const muted = new Set(state.settings.blocked_users || []);
  const listenSet = new Set(users || []);
  const needle = filter.value.trim().toLowerCase();
  const rank = (id) => (muted.has(id) ? 2 : starred.has(id) ? 0 : 1);
  const sorted = members.slice().sort((a, b) => {
    const diff = rank(a.id) - rank(b.id);
    if (diff) return diff;
    return String(a.name || a.id).localeCompare(String(b.name || b.id), "zh");
  });
  for (const member of sorted) {
    const row = document.createElement("div");
    row.className = "member-row";
    row.dataset.id = member.id;
    row.dataset.name = member.name || "";
    const hit = String(member.name || "").toLowerCase().includes(needle) || member.id.toLowerCase().includes(needle);
    row.hidden = Boolean(needle) && !hit;
    if (partial) {
      const check = document.createElement("input");
      check.type = "checkbox";
      check.checked = listenSet.has(member.id);
      check.addEventListener("change", () => {
        const next = new Set(users || []);
        if (check.checked) next.add(member.id);
        else next.delete(member.id);
        saveGroupUsers(groupId, Array.from(next)).catch((error) => setNotice(error.message));
      });
      row.append(check);
    }
    const img = document.createElement("img");
    img.src = `/api/avatar?username=${encodeURIComponent(member.id)}`;
    img.alt = "";
    img.addEventListener("error", () => img.remove());
    const name = document.createElement("span");
    name.className = "name";
    name.textContent = member.name || member.id;
    name.title = member.id;
    const star = document.createElement("button");
    star.type = "button";
    star.className = `member-star${starred.has(member.id) ? " on" : ""}`;
    star.textContent = starred.has(member.id) ? "★" : "☆";
    star.title = starred.has(member.id) ? "改星标颜色或取消" : "星标这个人";
    star.addEventListener("click", () => {
      toggleStar(member.id, star).catch((error) => setNotice(error.message));
    });
    const mute = document.createElement("button");
    mute.type = "button";
    mute.className = `member-mute${muted.has(member.id) ? " on" : ""}`;
    mute.textContent = muted.has(member.id) ? "已屏蔽" : "屏蔽";
    mute.title = muted.has(member.id) ? "恢复这个人的发言" : "不看这个人的发言";
    mute.addEventListener("click", () => {
      toggleMuteUser(member.id).catch((error) => setNotice(error.message));
    });
    if (muted.has(member.id)) row.classList.add("is-blocked");
    row.append(img, name, star, mute);
    list.append(row);
  }
  if (!members.length) {
    const empty = document.createElement("p");
    empty.className = "hint";
    empty.textContent = "这个群暂时读不到成员。";
    list.append(empty);
  }
  filter.addEventListener("input", () => {
    memberQuery.set(groupId, filter.value);
    const text = filter.value.trim().toLowerCase();
    for (const row of list.querySelectorAll(".member-row")) {
      const name = (row.dataset.name || "").toLowerCase();
      const id = (row.dataset.id || "").toLowerCase();
      row.hidden = Boolean(text) && !name.includes(text) && !id.includes(text);
    }
  });
  panel.append(filter, list);
}

function fillGroupList(box, storedKey) {
  if (!box) return;
  const top = box.scrollTop;
  const monitored = window.WechatFeed.monitoredGroupIds(state.settings);
  const stored = state.settings[storedKey] || [];
  const selected = new Set(window.WechatFeed.selectedFeedGroups(stored, monitored));
  const allOn = !stored.length;
  box.replaceChildren();
  const allLabel = document.createElement("label");
  allLabel.className = "all";
  const allBox = document.createElement("input");
  allBox.type = "checkbox";
  allBox.checked = allOn;
  allLabel.append(allBox, document.createTextNode("全选监控群"));
  allBox.addEventListener("change", () => {
    if (!allBox.checked) {
      allBox.checked = true;
      return;
    }
    toggleFeedGroup(storedKey, "");
  });
  box.append(allLabel);
  for (const item of state.settings.groups || []) {
    if ((state.settings.blocked_groups || []).includes(item.id)) continue;
    const wrap = document.createElement("div");
    wrap.className = "group-item";
    const row = document.createElement("div");
    row.className = "group-row";
    const label = document.createElement("label");
    const check = document.createElement("input");
    check.type = "checkbox";
    check.checked = selected.has(item.id);
    const name = groupLabel(item.id, item.name);
    label.append(check, document.createTextNode(name));
    label.title = `${item.id} · 双击改备注`;
    check.addEventListener("change", () => toggleFeedGroup(storedKey, item.id));
    label.addEventListener("dblclick", (event) => {
      event.preventDefault();
      remarkGroup(item.id, name);
    });
    const listen = window.WechatFeed.groupListenUsers(state.settings, item.id);
    const membersBtn = document.createElement("button");
    membersBtn.type = "button";
    membersBtn.className = "group-members";
    membersBtn.textContent = listen === null ? "成员" : `${listen.length}人`;
    membersBtn.title = listen === null ? "查看成员、星标、屏蔽、只听部分人" : "正在只听部分用户";
    membersBtn.addEventListener("click", () => {
      if (expandedGroups.has(item.id)) expandedGroups.delete(item.id);
      else expandedGroups.add(item.id);
      renderGroups();
    });
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "group-remove";
    remove.textContent = "移除";
    remove.title = "停止监控这个群";
    remove.addEventListener("click", () => {
      removeGroup(item.id).catch((error) => setNotice(error.message));
    });
    row.append(label, membersBtn, remove);
    wrap.append(row);
    if (expandedGroups.has(item.id)) {
      const panel = document.createElement("div");
      panel.className = "member-panel";
      const cached = memberCache.get(item.id);
      if (cached && Date.now() - cached.at < 15000) {
        renderMemberPanel(panel, item.id, cached.members);
      } else {
        panel.textContent = "正在读取成员…";
        ensureMembers(item.id)
          .then(() => renderGroups())
          .catch((error) => {
            panel.textContent = error.message;
          });
      }
      wrap.append(panel);
    }
    box.append(wrap);
  }
  box.scrollTop = top;
}

function renderGroups() {
  fillGroupList(document.querySelector("#groups-all"), "all_feed_groups");
  fillGroupList(document.querySelector("#groups-ca"), "ca_feed_groups");
}

async function toggleFeedGroup(storedKey, groupId) {
  const monitored = window.WechatFeed.monitoredGroupIds(state.settings);
  const next = window.WechatFeed.nextFeedGroups(state.settings[storedKey] || [], groupId, monitored);
  try {
    await saveSettings({ [storedKey]: next }, { skipFeed: true });
    renderGroups();
    renderFeed();
  } catch (error) {
    setNotice(error.message);
  }
}

function userLabel(userId) {
  const remark = (state.settings.user_remarks || {})[userId];
  if (remark) return remark;
  for (const card of state.messages) {
    if (card.senderUsername === userId) return card.senderName || userId;
  }
  for (const cached of memberCache.values()) {
    for (const member of cached.members || []) {
      if (member.id === userId) return member.name || userId;
    }
  }
  return userId;
}

function renderBlocked() {
  blockedBox.replaceChildren();
  for (const groupId of state.settings.blocked_groups || []) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = `恢复群 ${groupLabel(groupId, groupId)}`;
    button.addEventListener("click", () => unmuteGroup(groupId));
    blockedBox.append(button);
  }
  for (const userId of state.settings.blocked_users || []) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = `恢复 ${userLabel(userId)}`;
    button.addEventListener("click", () => unmuteUser(userId));
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
  feed.innerHTML = cards.map((card, index) => WechatFeed.cardHtml(present(card), WechatFeed.stackMeta(cards, index))).join("");
  more.hidden = !state.hasMore;
  more.textContent = state.loadingMore ? "正在加载…" : "加载更早的消息…";
  for (const img of feed.querySelectorAll(".wf-token-logo[src], .wf-token-hero[src]")) {
    img.addEventListener("error", () => {
      if (img.classList.contains("wf-token-logo")) {
        const letter = document.createElement("span");
        letter.className = "wf-token-logo wf-token-fallback";
        const sym = img.closest(".wf-token")?.querySelector(".wf-token-title strong")?.textContent || "?";
        letter.textContent = String(sym).replace("$", "").slice(0, 1) || "?";
        img.replaceWith(letter);
        return;
      }
      img.remove();
    });
  }
}

function renderDirectory() {
  const needle = state.directoryQuery.trim().toLowerCase();
  const rows = (state.directory || []).filter((group) => {
    if (!needle) return true;
    return `${group.name || ""} ${group.id}`.toLowerCase().includes(needle);
  });
  const watched = new Set((state.settings.groups || []).map((item) => item.id));
  directorySearch.hidden = !state.directory.length;
  directory.replaceChildren();
  for (const group of rows) {
    const label = document.createElement("label");
    if (watched.has(group.id)) label.className = "on";
    const check = document.createElement("input");
    check.type = "checkbox";
    check.checked = watched.has(group.id);
    const name = document.createElement("strong");
    name.textContent = groupLabel(group.id, group.name || "未命名群");
    const id = document.createElement("span");
    id.textContent = group.id;
    label.title = group.name && group.name !== name.textContent ? group.name : group.id;
    label.append(check, name, id);
    check.addEventListener("change", () => {
      const action = check.checked
        ? addGroup(group.id, group.name)
        : removeGroup(group.id);
      action.catch((error) => {
        check.checked = !check.checked;
        setNotice(error.message);
      });
    });
    directory.append(label);
  }
  if (!rows.length && state.directory.length) {
    const hint = document.createElement("p");
    hint.className = "notice";
    hint.textContent = "没有匹配的群，换个关键字再筛。";
    directory.append(hint);
  }
}

function mergeSettings(next) {
  const incoming = { ...(next || {}) };
  delete incoming.gmgn_api_key;
  state.settings = { ...state.settings, ...incoming };
  if (window.WechatFeed) window.WechatFeed.setNumberFormat(state.settings.mcap_format);
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
  renderSurfaceModes();
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

async function saveSettings(patch, { skipFeed = false } = {}) {
  const settings = await readJson(
    await fetch("/api/settings", {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(patch),
    }),
  );
  mergeSettings(settings);
  if (skipFeed) {
    gmgn.checked = Boolean(state.settings.gmgn_overlay);
    floatPanel.checked = Boolean(state.settings.float_panel);
    renderKey();
    renderRuntime();
    renderMode();
    renderSurfaceModes();
    renderGroups();
    renderBlocked();
  } else {
    render();
  }
  return state.settings;
}

async function loadFeed({ reset = false, caOnly = Boolean(state.settings.ca_only) } = {}) {
  const key = feedKey(caOnly);
  const cache = state.feeds[key];
  if (cache.loading) {
    while (cache.loading) {
      await new Promise((resolve) => setTimeout(resolve, 40));
    }
    if (key === feedKey()) {
      syncViewFromCache();
      renderFeed();
    }
    return;
  }
  if (!reset && cache.loaded && !cache.hasMore) return;
  cache.loading = true;
  if (key === feedKey()) {
    state.loadingMore = true;
    renderFeed();
  }
  try {
    const params = new URLSearchParams({ limit: String(PAGE_SIZE) });
    if (caOnly) params.set("ca_only", "true");
    if (!reset && cache.oldest) {
      params.set("before", String(cache.oldest));
      if (cache.oldestReceived) params.set("before_received", String(cache.oldestReceived));
    }
    const payload = await readJson(await fetch(`/api/messages?${params}`));
    const incoming = payload.messages || [];
    if (reset) {
      cache.messages = incoming;
    } else {
      const seen = new Set(cache.messages.map((item) => item.id));
      cache.messages = cache.messages.concat(incoming.filter((item) => !seen.has(item.id)));
    }
    cache.hasMore = Boolean(payload.has_more);
    cache.loaded = true;
    if (incoming.length) {
      const last = incoming[incoming.length - 1];
      cache.oldest = Number(last.createTime) || 0;
      cache.oldestReceived = Number(last.receivedAt) || 0;
    }
    if (reset && !incoming.length) {
      cache.oldest = 0;
      cache.oldestReceived = 0;
    }
  } finally {
    cache.loading = false;
    if (key === feedKey()) {
      syncViewFromCache();
      renderFeed();
    }
  }
}

async function addGroup(id, name) {
  const next = state.settings.groups.filter((item) => item.id !== id);
  next.push({ id, name: name || "" });
  const blocked = (state.settings.blocked_groups || []).filter((item) => item !== id);
  await saveSettings({ groups: next, blocked_groups: blocked });
  setNotice(`已监控 ${name || id}`);
}

async function removeGroup(id) {
  if (!id) return;
  const next = (state.settings.groups || []).filter((item) => item.id !== id);
  const allFeed = (state.settings.all_feed_groups || []).filter((item) => item !== id);
  const caFeed = (state.settings.ca_feed_groups || []).filter((item) => item !== id);
  const users = { ...(state.settings.group_users || {}) };
  delete users[id];
  await saveSettings({
    groups: next,
    all_feed_groups: allFeed,
    ca_feed_groups: caFeed,
    group_users: users,
  });
  setNotice(`已停止监控 ${groupLabel(id, id)}`);
}

async function reloadFeeds() {
  state.feeds.all = emptyFeedCache();
  state.feeds.ca = emptyFeedCache();
  await loadFeed({ reset: true });
  loadFeed({ reset: true, caOnly: !state.settings.ca_only }).catch(() => {});
}

async function muteGroup(groupId) {
  if (!groupId) return;
  const blocked = Array.from(new Set([...(state.settings.blocked_groups || []), groupId]));
  await saveSettings({ blocked_groups: blocked });
  setNotice("已屏蔽这个群。点上方红色按钮可恢复。");
}

async function unmuteGroup(groupId) {
  const blocked = (state.settings.blocked_groups || []).filter((item) => item !== groupId);
  await saveSettings({ blocked_groups: blocked }, { skipFeed: true });
  renderBlocked();
  renderGroups();
  await reloadFeeds();
}

async function muteUser(userId) {
  if (!userId) return;
  const blocked = Array.from(new Set([...(state.settings.blocked_users || []), userId]));
  await saveSettings({ blocked_users: blocked });
  setNotice("已屏蔽这个人的发言。点上方红色按钮可恢复。");
}

async function unmuteUser(userId) {
  const blocked = (state.settings.blocked_users || []).filter((item) => item !== userId);
  await saveSettings({ blocked_users: blocked }, { skipFeed: true });
  renderBlocked();
  renderGroups();
  renderFeed();
  await reloadFeeds();
}

async function toggleMuteUser(userId) {
  if (!userId) return;
  if ((state.settings.blocked_users || []).includes(userId)) {
    await unmuteUser(userId);
    return;
  }
  await muteUser(userId);
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
  await saveSettings({ group_remarks: remarks });
}

async function setCaOnly(enabled) {
  state.settings.ca_only = Boolean(enabled);
  syncViewFromCache();
  renderMode();
  renderFeed();
  try {
    await saveSettings({ ca_only: Boolean(enabled) }, { skipFeed: true });
    const cache = activeCache();
    if (!cache.loaded) await loadFeed({ reset: true, caOnly: Boolean(enabled) });
  } catch (error) {
    setNotice(error.message);
  }
}

async function setSurfaceCaOnly(surface, enabled) {
  const key = surfaceCaKey(surface);
  try {
    await saveSettings({ [key]: Boolean(enabled) }, { skipFeed: true });
    renderSurfaceModes();
  } catch (error) {
    setNotice(error.message);
  }
}

async function load() {
  const [settings, health] = await Promise.all([
    readJson(await fetch("/api/settings")),
    readJson(await fetch("/api/health")),
  ]);
  mergeSettings(settings);
  state.runtime = health.runtime || state.runtime;
  render();
  await loadFeed({ reset: true, caOnly: Boolean(state.settings.ca_only) });
  loadFeed({ reset: true, caOnly: !state.settings.ca_only }).catch(() => {});
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

for (const root of document.querySelectorAll(".surface-mode")) {
  root.addEventListener("click", (event) => {
    const button = event.target.closest("[data-mode]");
    if (!button) return;
    setSurfaceCaOnly(root.dataset.surface, button.dataset.mode === "ca");
  });
}

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
    setNotice(
      state.directory.length
        ? "输入关键字筛选群。勾选加入监控，取消勾选即从监控里移除。"
        : "没有读到群会话。",
    );
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
  if (!imagePreview) {
    imagePreview = document.createElement("img");
    imagePreview.alt = "预览";
    imagePreview.style.cssText = "position:fixed;z-index:40;border-radius:12px;box-shadow:0 18px 50px rgba(0,0,0,.38);pointer-events:none;background:#111;object-fit:contain;";
    document.body.append(imagePreview);
  }
  const full = img.dataset.wfFull;
  const maxW = Math.min(480, window.innerWidth - 24);
  const maxH = Math.min(560, window.innerHeight - 24);
  const layout = () => {
    sizeToBox(imagePreview, maxW, maxH);
    placePreview(imagePreview, img);
  };
  imagePreview.onload = layout;
  imagePreview.onerror = full ? () => {
    imagePreview.onerror = null;
    imagePreview.src = src;
  } : null;
  imagePreview.src = full || src;
  if (imagePreview.complete) layout();
  imagePreview.hidden = false;
}

function hideImagePreview() {
  if (imagePreview) imagePreview.hidden = true;
}

function showImageLightbox(img) {
  hideImagePreview();
  const src = img.currentSrc || img.getAttribute("src") || "";
  if (!src) return;
  const full = img.dataset.wfFull;
  const overlay = document.createElement("div");
  overlay.style.cssText = "position:fixed;inset:0;z-index:80;background:rgba(0,0,0,.78);display:flex;align-items:center;justify-content:center;cursor:zoom-out;";
  const picture = document.createElement("img");
  picture.alt = "";
  picture.style.cssText = "border-radius:12px;box-shadow:0 18px 50px rgba(0,0,0,.45);background:#111;object-fit:contain;";
  const maxW = window.innerWidth * 0.92;
  const maxH = window.innerHeight * 0.92;
  picture.onload = () => sizeToBox(picture, maxW, maxH);
  picture.onerror = full ? () => {
    picture.onerror = null;
    picture.src = src;
  } : null;
  picture.src = full || src;
  overlay.append(picture);
  overlay.addEventListener("click", () => overlay.remove());
  document.body.append(overlay);
}

feed.addEventListener("mouseover", (event) => {
  const img = event.target.closest(".wf-media, .wf-emoji");
  if (img) showImagePreview(img);
});

feed.addEventListener("mouseout", (event) => {
  const img = event.target.closest(".wf-media, .wf-emoji");
  if (!img) return;
  if (event.relatedTarget && img.contains(event.relatedTarget)) return;
  hideImagePreview();
});

feed.addEventListener("click", (event) => {
  if (event.target.closest("a.wf-text-link, a.wf-token-link")) return;
  const fmtBtn = event.target.closest("[data-mcap-format]");
  if (fmtBtn) {
    const next = fmtBtn.dataset.mcapFormat === "en" ? "en" : "cn";
    saveSettings({ mcap_format: next }).catch((error) => setNotice(error.message));
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
    setNotice("已复制合约");
    return;
  }
  const star = event.target.closest("[data-star]");
  if (star) {
    const card = star.closest(".wf-card");
    toggleStar(card && card.dataset.user, star);
    return;
  }
  const muteUserBtn = event.target.closest("[data-mute-user]");
  if (muteUserBtn) {
    const card = muteUserBtn.closest(".wf-card");
    muteUser(card && card.dataset.user).catch((error) => setNotice(error.message));
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
    state.feeds.all = emptyFeedCache();
    state.feeds.ca = emptyFeedCache();
    syncViewFromCache();
    renderFeed();
    return;
  }
  if (payload.type === "patch" && payload.card) {
    const apply = (list) => list.map((item) => (item.id === payload.card.id ? payload.card : item));
    state.feeds.all.messages = apply(state.feeds.all.messages);
    state.feeds.ca.messages = apply(state.feeds.ca.messages);
    syncViewFromCache();
    renderFeed();
    return;
  }
  if (payload.type === "message" && payload.card) {
    const remember = (cache, card) => {
      if (!cache.messages.some((item) => item.id === card.id)) {
        cache.messages.unshift(card);
      }
    };
    remember(state.feeds.all, payload.card);
    if ((payload.card.addresses || []).length) remember(state.feeds.ca, payload.card);
    syncViewFromCache();
    renderFeed();
  }
};

const sentinel = new IntersectionObserver((entries) => {
  if (entries.some((entry) => entry.isIntersecting)) {
    loadFeed().catch((error) => setNotice(error.message));
  }
});
sentinel.observe(more);

load().catch((error) => setNotice(error.message));
