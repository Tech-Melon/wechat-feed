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
    ca_board: true,
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
  board: [],
  boardStats: {},
  userProfile: null,
  userCalls: [],
  userSort: "latest",
  userRange: "all",
  userWin: "all",
  boardRange: "all",
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
let groupUsersTimer = 0;
let groupUsersDirty = false;

async function ensureMembers(groupId) {
  const cached = memberCache.get(groupId);
  if (cached && Date.now() - cached.at < 15000) return cached.members;
  const payload = await readJson(await fetch(`/api/groups/${encodeURIComponent(groupId)}/members`));
  const members = payload.members || [];
  memberCache.set(groupId, { at: Date.now(), members });
  return members;
}

function setListenUsers(groupId, users) {
  const map = { ...(state.settings.group_users || {}) };
  if (users === null) delete map[groupId];
  else map[groupId] = users;
  state.settings.group_users = map;
  groupUsersDirty = true;
  renderGroups();
  renderFeed();
  clearTimeout(groupUsersTimer);
  groupUsersTimer = setTimeout(() => {
    flushGroupUsers().catch((error) => setNotice(error.message));
  }, 280);
}

async function flushGroupUsers() {
  const map = { ...(state.settings.group_users || {}) };
  await saveSettings({ group_users: map }, { skipFeed: true });
  groupUsersDirty = false;
  state.feeds.all = emptyFeedCache();
  state.feeds.ca = emptyFeedCache();
  await loadFeed({ reset: true });
  loadFeed({ reset: true, caOnly: !state.settings.ca_only }).catch(() => {});
}

function memberHaystack(member) {
  const remark = (state.settings.user_remarks || {})[member.id] || "";
  return [member.name, member.id, member.roomNick, member.remark, member.nick, remark]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
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
    setListenUsers(groupId, null);
  });
  partBtn.addEventListener("click", () => {
    setListenUsers(groupId, users || []);
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
    row.dataset.hay = memberHaystack(member);
    const hit = (row.dataset.hay || "").includes(needle);
    row.hidden = Boolean(needle) && !hit;
    if (partial) {
      const check = document.createElement("input");
      check.type = "checkbox";
      check.checked = listenSet.has(member.id);
      check.addEventListener("change", () => {
        const latest = window.WechatFeed.groupListenUsers(state.settings, groupId) || [];
        const next = new Set(latest);
        if (check.checked) next.add(member.id);
        else next.delete(member.id);
        setListenUsers(groupId, Array.from(next));
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
      const hay = (row.dataset.hay || "").toLowerCase();
      const id = (row.dataset.id || "").toLowerCase();
      row.hidden = Boolean(text) && !hay.includes(text) && !id.includes(text);
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
    await reloadFeeds();
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
  if (window.WechatFeed.tickFresh) window.WechatFeed.tickFresh(feed);
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
  if (groupUsersDirty) delete incoming.group_users;
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
  renderBoard();
}

let boardTimer = 0;
function boardEnabled() {
  return state.settings.ca_board !== false;
}

function boardRoute() {
  const hash = (location.hash || "").replace(/^#/, "");
  if (hash === "board") return { view: "board" };
  if (hash.startsWith("board/")) {
    try {
      return { view: "user", userId: decodeURIComponent(hash.slice(6)) };
    } catch (error) {
      return { view: "board" };
    }
  }
  return { view: "feed" };
}

function openBoard() {
  if (!boardEnabled()) {
    setNotice("排行榜已关闭");
    return;
  }
  location.hash = "board";
}

function openBoardUser(userId) {
  if (!userId) return;
  if (!boardEnabled()) {
    setNotice("排行榜已关闭");
    return;
  }
  location.hash = `board/${encodeURIComponent(userId)}`;
}

function closeBoard() {
  location.hash = boardRoute().view === "user" ? "board" : "";
}

function moneyText(value) {
  const parts = window.WechatFeed.formatUsd(value, window.WechatFeed.numberFormat());
  return parts && parts.text ? `${parts.text}${parts.unit || ""}` : "—";
}

function medalSvg(rank, size) {
  const n = Number(rank);
  if (n < 1 || n > 3) {
    return `<div class="ca-rank">${n || "—"}</div>`;
  }
  const tone = n === 1 ? "gold" : n === 2 ? "silver" : "bronze";
  const px = Number(size) || 32;
  const h = Math.round((px * 38) / 32);
  return `<svg class="ca-medal ${tone}" viewBox="0 0 32 38" width="${px}" height="${h}" aria-label="第${n}名">
    <path class="ribbon-l" d="M9.2 1.2h5.4l1.2 11.2H8.2z"/>
    <path class="ribbon-r" d="M17.4 1.2h5.4l1 11.2h-7.4z"/>
    <circle class="coin" cx="16" cy="23.2" r="12.4"/>
    <circle class="ring" cx="16" cy="23.2" r="9.4"/>
    <path class="star" d="M16 15.2l1.85 3.7 4.1.58-2.96 2.9.72 4.08L16 24.4l-3.71 2.06.72-4.08-2.96-2.9 4.1-.58z"/>
  </svg>`;
}

function rangeStart(range, nowSec) {
  const now = Number(nowSec);
  const stamp = Number.isFinite(now) ? now : Date.now() / 1000;
  if (range === "today") {
    const date = new Date(stamp * 1000);
    date.setHours(0, 0, 0, 0);
    return date.getTime() / 1000;
  }
  if (range === "7d") return stamp - 7 * 86400;
  if (range === "30d") return stamp - 30 * 86400;
  return 0;
}

function callStatus(item) {
  return item.status || (item.win ? "win" : "lose");
}

function filterUserCalls(calls, { range, win, nowSec } = {}) {
  const start = rangeStart(range || "all", nowSec);
  return (calls || []).filter((item) => {
    const at = Number(item.calledAt) || 0;
    if (start && at < start) return false;
    if (win && win !== "all" && callStatus(item) !== win) return false;
    return true;
  });
}

function sortUserCalls(calls, sort) {
  const rows = [...(calls || [])];
  const key = sort || "latest";
  rows.sort((a, b) => {
    if (key === "oldest") return (a.calledAt || 0) - (b.calledAt || 0);
    if (key === "maxX") return (b.maxX || 0) - (a.maxX || 0);
    if (key === "nowX") return (b.nowX || 0) - (a.nowX || 0);
    if (key === "mcap") return (b.callMcap || 0) - (a.callMcap || 0);
    return (b.calledAt || 0) - (a.calledAt || 0);
  });
  return rows;
}

function avatarBlock(userId, name, size) {
  const hue = window.WechatFeed.hue(userId || name);
  const px = size || 36;
  return `<div class="wf-avatar" style="width:${px}px;height:${px}px;background:hsl(${hue} 42% 48%)">
    <img src="/api/avatar?username=${encodeURIComponent(userId || "")}" alt="" />
  </div>`;
}

async function loadBoard() {
  if (!boardEnabled()) {
    state.board = [];
    state.boardStats = {};
    renderBoard();
    return;
  }
  try {
    const payload = await readJson(
      await fetch(`/api/ca-board?range=${encodeURIComponent(state.boardRange || "all")}`),
    );
    state.board = payload.board || [];
    state.boardStats = payload.stats || {};
  } catch (error) {
    state.board = [];
    state.boardStats = {};
  }
  const route = boardRoute();
  if (route.view === "user" && route.userId) {
    await loadUserCalls(route.userId);
  }
  renderBoard();
}

async function loadUserCalls(userId) {
  try {
    const payload = await readJson(await fetch(`/api/ca-board/user/${encodeURIComponent(userId)}`));
    state.userProfile = payload.user || null;
    state.userCalls = payload.calls || [];
  } catch (error) {
    state.userProfile = { userId, name: userId };
    state.userCalls = [];
  }
}

function scheduleBoard() {
  clearTimeout(boardTimer);
  boardTimer = setTimeout(() => {
    loadBoard().catch(() => {});
  }, 800);
}

function bindBrokenAvatars(root) {
  if (!root) return;
  for (const img of root.querySelectorAll("img")) {
    img.addEventListener("error", () => img.remove());
  }
}

function renderStrip() {
  const strip = document.querySelector("#ca-strip");
  const count = document.querySelector("#ca-strip-count");
  const top = document.querySelector("#ca-strip-top");
  const toggle = document.querySelector("#ca-board-on");
  if (!count || !top) return;
  const enabled = boardEnabled();
  if (strip) strip.classList.toggle("is-off", !enabled);
  if (toggle && toggle.checked !== enabled) toggle.checked = enabled;
  if (!enabled) {
    count.textContent = "已关闭，不刷 GMGN";
    top.innerHTML = "";
    return;
  }
  const rows = state.board || [];
  const stats = state.boardStats || {};
  const esc = window.WechatFeed.escapeText;
  if (!rows.length) {
    count.textContent = "还没有记录";
    top.innerHTML = "";
    return;
  }
  count.textContent = stats.users ? `${stats.users} 人 · ${stats.tokens || rows.length} 个 CA` : `${rows.length} 人`;
  top.innerHTML = rows
    .slice(0, 3)
    .map((row) => {
      const name = esc(row.name || row.userId);
      return `<button type="button" class="ca-chip" data-rank="${row.rank}" data-ca-user="${esc(row.userId)}">
        ${medalSvg(row.rank, 18)}
        ${avatarBlock(row.userId, row.name, 22)}
        <span>${name}</span>
        <em>${esc(row.winRateText)}</em>
      </button>`;
    })
    .join("");
  bindBrokenAvatars(top);
}

function personRow(row, starred) {
  const esc = window.WechatFeed.escapeText;
  const on = starred.has(row.userId);
  const name = esc(row.name || row.userId);
  return `<article class="ca-person" data-rank="${row.rank}" data-ca-user="${esc(row.userId)}">
    ${medalSvg(row.rank, 32)}
    ${avatarBlock(row.userId, row.name, 36)}
    <div class="ca-main">
      <div class="ca-who">
        <strong title="${name}">${name}</strong>
        <span>${row.calls} 次喊单</span>
      </div>
      <div class="ca-stats">
        <span class="ca-pill score" title="威尔逊下限 × 次数 × 平均倍数（待定单不计）"><span class="ca-k">得分</span><b>${esc(row.scoreText || "0")}</b></span>
        <span class="ca-pill win"><span class="ca-k">胜率</span><b>${esc(row.winRateText)}</b></span>
        <span class="ca-pill" title="单条最高倍按 20x 封顶后平均"><span class="ca-k">均倍</span><b>${esc(row.avgXText || "—")}</b></span>
        <span class="ca-pill max"><span class="ca-k">最高</span><b>${esc(row.maxXText || "—")}</b></span>
        <span class="ca-pill"><span class="ca-k">胜场</span><b>${row.wins}/${row.settled ?? row.calls}</b></span>
        ${row.pending ? `<span class="ca-pill" title="喊单不满 24 小时、还没到 1.5 倍，暂不计入胜率"><span class="ca-k">待定</span><b>${row.pending}</b></span>` : ""}
      </div>
    </div>
    <button type="button" class="ca-star${on ? " on" : ""}" data-board-star="${esc(row.userId)}">${on ? "已星标" : "星标"}</button>
  </article>`;
}

function renderUserPage(userId) {
  const title = document.querySelector("#ca-page-title");
  const sub = document.querySelector("#ca-page-sub");
  const box = document.querySelector("#ca-page-body");
  const esc = window.WechatFeed.escapeText;
  const user = state.userProfile || { userId, name: userId };
  const all = state.userCalls || [];
  const filtered = filterUserCalls(all, {
    range: state.userRange,
    win: state.userWin,
  });
  const calls = sortUserCalls(filtered, state.userSort);
  const starred = new Set(state.settings.starred_users || []);
  const on = starred.has(user.userId);
  const name = esc(user.name || user.userId);
  const sortOn = (key) => (state.userSort === key ? " on" : "");
  const rangeOn = (key) => (state.userRange === key ? " on" : "");
  const winOn = (key) => (state.userWin === key ? " on" : "");
  title.textContent = name;
  sub.innerHTML = `<span class="ca-sub-item"><em>喊单</em><b>${user.calls || all.length}</b></span>
    <span class="ca-sub-item win"><em>胜率</em><b>${esc(user.winRateText || "0%")}</b></span>
    <span class="ca-sub-item"><em>得分</em><b>${esc(user.scoreText || "0")}</b></span>`;
  const head = `<div class="ca-userhead">
    ${avatarBlock(user.userId, user.name, 48)}
    <div class="ca-who">
      <strong>${name}</strong>
      <div class="ca-stats">
        <span class="ca-pill"><span class="ca-k">均倍</span><b>${esc(user.avgXText || "—")}</b></span>
        <span class="ca-pill max"><span class="ca-k">最高</span><b>${esc(user.maxXText || "—")}</b></span>
      </div>
    </div>
    <span class="ca-pill score" title="威尔逊下限 × 次数 × 平均倍数"><span class="ca-k">得分</span><b>${esc(user.scoreText || "0")}</b></span>
    <button type="button" class="ca-star${on ? " on" : ""}" data-board-star="${esc(user.userId)}">${on ? "已星标" : "星标"}</button>
  </div>
  <div class="ca-toolbar">
    <div class="ca-seg" role="tablist" aria-label="排序">
      <button type="button" data-user-sort="latest" class="${sortOn("latest")}">最新</button>
      <button type="button" data-user-sort="oldest" class="${sortOn("oldest")}">最早</button>
      <button type="button" data-user-sort="maxX" class="${sortOn("maxX")}">最高倍</button>
      <button type="button" data-user-sort="nowX" class="${sortOn("nowX")}">当前倍</button>
      <button type="button" data-user-sort="mcap" class="${sortOn("mcap")}">喊单市值</button>
    </div>
    <div class="ca-seg" role="tablist" aria-label="日期">
      <button type="button" data-user-range="all" class="${rangeOn("all")}">全部</button>
      <button type="button" data-user-range="today" class="${rangeOn("today")}">今天</button>
      <button type="button" data-user-range="7d" class="${rangeOn("7d")}">近 7 天</button>
      <button type="button" data-user-range="30d" class="${rangeOn("30d")}">近 30 天</button>
    </div>
    <div class="ca-seg" role="tablist" aria-label="胜负">
      <button type="button" data-user-win="all" class="${winOn("all")}">全部</button>
      <button type="button" data-user-win="win" class="${winOn("win")}">已胜</button>
      <button type="button" data-user-win="pending" class="${winOn("pending")}">待定</button>
      <button type="button" data-user-win="lose" class="${winOn("lose")}">未到 50%</button>
    </div>
  </div>
  <p class="ca-filter-hint">${calls.length === all.length ? `${all.length} 条` : `显示 ${calls.length} / ${all.length} 条`}</p>`;
  if (!all.length) {
    box.innerHTML = `${head}<p class="ca-empty">这个人还没有记入有效 CA。</p>`;
    bindBrokenAvatars(box);
    return;
  }
  if (!calls.length) {
    box.innerHTML = `${head}<p class="ca-empty">当前筛选下没有 CA。</p>`;
    bindBrokenAvatars(box);
    return;
  }
  box.innerHTML = head + calls.map((item) => {
    const symbol = item.symbol ? `$${esc(item.symbol)}` : esc(item.address.slice(0, 8));
    const status = callStatus(item);
    const win = status === "win"
      ? `<span class="ca-pill win" title="喊单后最高 ≥ 该用户首次喊单市值的 1.5 倍（涨幅 50%）"><span class="ca-k">结果</span><b>胜</b></span>`
      : status === "pending"
        ? `<span class="ca-pill" title="喊单不满 24 小时、还没到 1.5 倍，暂不计入胜率"><span class="ca-k">结果</span><b>待定</b></span>`
        : `<span class="ca-pill" title="喊单 24 小时后仍未到首次喊单市值的 1.5 倍"><span class="ca-k">结果</span><b>未到 50%</b></span>`;
    const first = item.isFirst ? `<span class="wf-debut">所有群首发</span>` : "";
    const when = window.WechatFeed.formatDateTime
      ? window.WechatFeed.formatDateTime(item.calledAt)
      : window.WechatFeed.formatTime(item.calledAt);
    return `<article class="ca-call">
      <div class="ca-call-top">
        <strong>${symbol}</strong>
        ${first}
        ${win}
        ${window.WechatFeed.chainBadge ? window.WechatFeed.chainBadge(item.chain) : `<span class="ca-pill"><span class="ca-k">链</span><b>${esc(item.chain || "")}</b></span>`}
      </div>
      <div class="ca-call-x">
        <span class="ca-pill"><span class="ca-k">喊单市值</span><b>${esc(moneyText(item.callMcap))}</b></span>
        <span class="ca-pill"><span class="ca-k">当前倍</span><b>${esc(item.nowXText || "—")}</b></span>
        <span class="ca-pill max"><span class="ca-k">最高倍</span><b>${esc(item.maxXText || "—")}</b></span>
      </div>
      <div class="ca-call-meta">${esc(item.groupName || "")}${when ? ` · <time>${esc(when)}</time>` : ""} · ${esc(item.address)}</div>
    </article>`;
  }).join("");
  bindBrokenAvatars(box);
}

function renderBoardPage() {
  const title = document.querySelector("#ca-page-title");
  const sub = document.querySelector("#ca-page-sub");
  const box = document.querySelector("#ca-page-body");
  const rows = state.board || [];
  const stats = state.boardStats || {};
  const starred = new Set(state.settings.starred_users || []);
  title.textContent = "CA 发现榜";
  const esc = window.WechatFeed.escapeText;
  const rule = `<span class="ca-win-rule" title="相对该用户自己第一次喊到这个 CA 时的市值；喊单不满 24 小时且还没到的记为待定">胜：喊单后最高 ≥ 1.5 倍（涨幅 50%）</span>`;
  const dormant = stats.dormant
    ? ` · <span title="超过 24 小时没人再喊、现市值不到 ${esc(moneyText(stats.dormantMcap || 15000))} 的暂停刷新，再被喊到自动恢复">${stats.dormant} 个休眠</span>`
    : "";
  sub.innerHTML = stats.tokens
    ? `${stats.users || rows.length} 人 · ${stats.tokens} 个 CA · ${rule}${dormant}`
    : `按喊单次数、胜率和平均倍数综合打分 · ${rule}`;
  const rangeOn = (key) => ((state.boardRange || "all") === key ? " on" : "");
  const toolbar = `<div class="ca-toolbar">
    <div class="ca-seg" role="tablist" aria-label="统计范围">
      <button type="button" data-board-range="all" class="${rangeOn("all")}">全部</button>
      <button type="button" data-board-range="30d" class="${rangeOn("30d")}">近 30 天</button>
      <button type="button" data-board-range="7d" class="${rangeOn("7d")}">近 7 天</button>
    </div>
  </div>`;
  if (!rows.length) {
    box.innerHTML = `${toolbar}<p class="ca-empty">${
      (state.boardRange || "all") === "all"
        ? "还没有 CA 发现记录。群里出现合约并查出市值后会记在这里。"
        : "这个时间范围内还没有喊单。"
    }</p>`;
    return;
  }
  box.innerHTML = toolbar + rows.map((row) => personRow(row, starred)).join("");
  bindBrokenAvatars(box);
}

function renderBoard() {
  const page = document.querySelector("#ca-page");
  const route = boardRoute();
  renderStrip();
  if (!boardEnabled()) {
    document.body.classList.remove("is-board");
    if (page) page.hidden = true;
    if (route.view !== "feed" && location.hash) location.hash = "";
    return;
  }
  document.body.classList.toggle("is-board", route.view !== "feed");
  if (!page) return;
  if (route.view === "feed") {
    page.hidden = true;
    return;
  }
  page.hidden = false;
  if (route.view === "user") renderUserPage(route.userId);
  else renderBoardPage();
}

async function starFromBoard(userId) {
  if (!userId) return;
  const current = new Set(state.settings.starred_users || []);
  const colors = { ...(state.settings.star_colors || {}) };
  if (current.has(userId)) {
    current.delete(userId);
    delete colors[userId];
  } else {
    current.add(userId);
    colors[userId] = colors[userId] || "amber";
  }
  await saveSettings({ starred_users: Array.from(current), star_colors: colors });
  renderBoard();
}

async function onBoardHash() {
  const route = boardRoute();
  if (route.view === "user" && route.userId) {
    await loadUserCalls(route.userId);
  }
  renderBoard();
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
    renderBoard();
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
  loadBoard().catch(() => {});
  if (window.WechatFeed.startFreshTick) window.WechatFeed.startFreshTick(document);
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
  const caUser = event.target.closest("[data-ca-user]");
  if (caUser) {
    openBoardUser(caUser.dataset.caUser);
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
    if (groupUsersDirty) {
      renderRuntime();
      renderKey();
      return;
    }
    render();
    scheduleBoard();
    return;
  }
  if (payload.type === "ca-board") {
    scheduleBoard();
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
    if ((payload.card.tokenCards || []).length) scheduleBoard();
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
    if ((payload.card.cas || payload.card.tokenCards || []).length) scheduleBoard();
  }
};

const sentinel = new IntersectionObserver((entries) => {
  if (entries.some((entry) => entry.isIntersecting)) {
    loadFeed().catch((error) => setNotice(error.message));
  }
});
sentinel.observe(more);

document.querySelector("#ca-board-on")?.addEventListener("change", async (event) => {
  const on = Boolean(event.target.checked);
  try {
    await saveSettings({ ca_board: on }, { skipFeed: true });
    if (!on && boardRoute().view !== "feed") location.hash = "";
    await loadBoard();
  } catch (error) {
    event.target.checked = boardEnabled();
    setNotice(error.message);
  }
});

document.addEventListener("click", (event) => {
  const star = event.target.closest("[data-board-star]");
  if (star) {
    event.preventDefault();
    event.stopPropagation();
    starFromBoard(star.dataset.boardStar).catch((error) => setNotice(error.message));
    return;
  }
  if (event.target.closest("[data-open-board]")) {
    openBoard();
    return;
  }
  if (event.target.closest("[data-board-back]")) {
    closeBoard();
    return;
  }
  const sortBtn = event.target.closest("[data-user-sort]");
  if (sortBtn) {
    event.preventDefault();
    event.stopPropagation();
    state.userSort = sortBtn.dataset.userSort || "latest";
    if (boardRoute().view === "user") renderUserPage(boardRoute().userId);
    return;
  }
  const rangeBtn = event.target.closest("[data-user-range]");
  if (rangeBtn) {
    event.preventDefault();
    event.stopPropagation();
    state.userRange = rangeBtn.dataset.userRange || "all";
    if (boardRoute().view === "user") renderUserPage(boardRoute().userId);
    return;
  }
  const boardRangeBtn = event.target.closest("[data-board-range]");
  if (boardRangeBtn) {
    event.preventDefault();
    event.stopPropagation();
    state.boardRange = boardRangeBtn.dataset.boardRange || "all";
    loadBoard().catch((error) => setNotice(error.message));
    return;
  }
  const winBtn = event.target.closest("[data-user-win]");
  if (winBtn) {
    event.preventDefault();
    event.stopPropagation();
    state.userWin = winBtn.dataset.userWin || "all";
    if (boardRoute().view === "user") renderUserPage(boardRoute().userId);
    return;
  }
  const user = event.target.closest("[data-ca-user]");
  if (user && !event.target.closest("#feed")) {
    openBoardUser(user.dataset.caUser);
  }
});

window.addEventListener("hashchange", () => {
  onBoardHash().catch((error) => setNotice(error.message));
});

load().catch((error) => setNotice(error.message));
