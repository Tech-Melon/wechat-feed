const status = document.querySelector("#status");
const noteBox = document.querySelector("#note");
const checkbox = document.querySelector("#gmgn");
const floatBox = document.querySelector("#float-panel");
const boardBox = document.querySelector("#ca-board");
const keyInput = document.querySelector("#key");
const keyState = document.querySelector("#key-state");

try {
  chrome.runtime.connect({ name: "popup" });
} catch (error) {
  // 弹窗关掉时后台仍可能活着
}

let noteTimer = 0;
let liveSettings = {};
function note(text, bad = false) {
  noteBox.textContent = text;
  noteBox.classList.toggle("bad", bad);
  noteBox.hidden = !text;
  clearTimeout(noteTimer);
  if (text) noteTimer = setTimeout(() => { noteBox.hidden = true; }, 5000);
}

function send(type, payload = {}, attempt = 0) {
  return new Promise((resolve) => {
    try {
      chrome.runtime.sendMessage({ type, ...payload }, (state) => {
        const err = chrome.runtime.lastError;
        if (err) {
          if (attempt < 2) {
            setTimeout(() => {
              send(type, payload, attempt + 1).then(resolve);
            }, 160 * (attempt + 1));
            return;
          }
          resolve({ ok: false, detail: "插件后台刚唤醒，请再点一次" });
          return;
        }
        resolve(state || { ok: false });
      });
    } catch (error) {
      resolve({ ok: false, detail: "插件后台没有响应" });
    }
  });
}

function groupLabel(settings, groupId, fallback) {
  const remark = (settings.group_remarks || {})[groupId];
  if (remark) return remark;
  const watched = (settings.groups || []).find((item) => item.id === groupId);
  if (watched && watched.name && !String(watched.name).endsWith("@chatroom")) return watched.name;
  if (fallback && !String(fallback).endsWith("@chatroom")) return fallback;
  return "未命名群";
}

function paintSurfaceModes(settings) {
  for (const root of document.querySelectorAll(".surface-mode")) {
    if (root.dataset.numfmt !== undefined) {
      const fmt = settings.mcap_format === "en" ? "en" : "cn";
      for (const button of root.querySelectorAll("[data-mcap-format]")) {
        button.classList.toggle("on", button.dataset.mcapFormat === fmt);
      }
      continue;
    }
    const key = root.dataset.surface === "gmgn" ? "gmgn_ca_only" : "float_ca_only";
    const caOnly = Boolean(settings[key]);
    for (const button of root.querySelectorAll("[data-mode]")) {
      button.classList.toggle("on", button.dataset.mode === (caOnly ? "ca" : "all"));
    }
  }
}

const expandedGroups = new Set();
const memberCache = new Map();
const memberQuery = new Map();

// 界面状态（折叠、群筛选词）存在插件自己的 localStorage；群勾选仍存本地服务设置。与本地页面同一套逻辑
const UI_KEY = "popupUi";
const ui = (() => {
  try {
    const saved = JSON.parse(localStorage.getItem(UI_KEY) || "{}");
    return saved && typeof saved === "object" ? saved : {};
  } catch (error) {
    return {};
  }
})();
function saveUi() {
  try {
    localStorage.setItem(UI_KEY, JSON.stringify(ui));
  } catch (error) {
    // 存不了只是不记忆
  }
}

const groupFold = document.querySelector("#fold-groups");
groupFold.open = typeof ui.groupsOpen === "boolean" ? ui.groupsOpen : true;
groupFold.addEventListener("toggle", () => {
  ui.groupsOpen = groupFold.open;
  saveUi();
});
const groupQuery = document.querySelector("#group-query");
groupQuery.value = ui.groupQuery || "";
groupQuery.addEventListener("input", () => {
  ui.groupQuery = groupQuery.value;
  saveUi();
  refreshGroupLists(liveSettings);
});

function feedGroupCounts(settings, storedKey) {
  const monitored = window.WechatFeed.monitoredGroupIds(settings);
  const stored = settings[storedKey] || [];
  if (!stored.length) return { allOn: true, text: "全选" };
  const picked = window.WechatFeed.selectedFeedGroups(stored, monitored).filter((id) => monitored.includes(id));
  return { allOn: false, text: `${picked.length}/${monitored.length}` };
}

function saveFeedGroups(storedKey, groups) {
  send("setFeedGroups", { caOnly: storedKey === "ca_feed_groups", groups }).then((state) => {
    if (state?.ok) show(state);
    else note(state?.detail || "没有保存成功", true);
  });
}

function refreshGroupLists(settings) {
  fillGroupTable(document.querySelector("#groups"), settings);
  const total = (settings.groups || []).length;
  document.querySelector("#groups-summary").textContent = total
    ? `${total} 群 · 全部 ${feedGroupCounts(settings, "all_feed_groups").text} · CA ${feedGroupCounts(settings, "ca_feed_groups").text}`
    : "还没有监控群";
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

async function ensureMembers(groupId) {
  const cached = memberCache.get(groupId);
  if (cached && Date.now() - cached.at < 15000) return cached.members;
  const result = await send("getGroupMembers", { groupId });
  if (!result?.ok) throw new Error(result?.detail || "读不到成员");
  const members = result.members || [];
  memberCache.set(groupId, { at: Date.now(), members });
  return members;
}

function memberHaystack(settings, member) {
  const remark = (settings.user_remarks || {})[member.id] || "";
  return [member.name, member.id, member.roomNick, member.remark, member.nick, remark]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
}

function applyListen(groupId, users) {
  const map = { ...(liveSettings.group_users || {}) };
  if (users === null) delete map[groupId];
  else map[groupId] = users;
  liveSettings = { ...liveSettings, group_users: map };
  refreshGroupLists(liveSettings);
  send("setGroupUsers", { groupId, users }).then((state) => {
    if (state?.ok) show(state);
    else note(state?.detail || "没有保存成功", true);
  });
}

function renderMemberPanel(panel, settings, groupId, members) {
  panel.replaceChildren();
  const users = window.WechatFeed.groupListenUsers(settings, groupId);
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
    applyListen(groupId, null);
  });
  partBtn.addEventListener("click", () => {
    applyListen(groupId, users || []);
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
  const starred = new Set(settings.starred_users || []);
  const muted = new Set(settings.blocked_users || []);
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
    row.dataset.hay = memberHaystack(settings, member);
    const hit = (row.dataset.hay || "").includes(needle);
    row.hidden = Boolean(needle) && !hit;
    if (partial) {
      const check = document.createElement("input");
      check.type = "checkbox";
      check.checked = listenSet.has(member.id);
      check.addEventListener("change", () => {
        const latest = window.WechatFeed.groupListenUsers(liveSettings, groupId) || [];
        const next = new Set(latest);
        if (check.checked) next.add(member.id);
        else next.delete(member.id);
        applyListen(groupId, Array.from(next));
      });
      row.append(check);
    }
    const img = document.createElement("img");
    img.src = `http://127.0.0.1:8765/api/avatar?username=${encodeURIComponent(member.id)}`;
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
    star.addEventListener("click", async () => {
      const colors = settings.star_colors || {};
      const picked = await pickStarColor(star, starred.has(member.id) ? colors[member.id] || "amber" : "");
      if (picked === null) return;
      const state = await send("starUser", {
        userId: member.id,
        starred: Boolean(picked),
        color: picked || "amber",
      });
      if (state?.ok) show(state.settings ? state : await send("getState"));
      else note(state?.detail || "没有保存成功", true);
    });
    const mute = document.createElement("button");
    mute.type = "button";
    mute.className = `member-mute${muted.has(member.id) ? " on" : ""}`;
    mute.textContent = muted.has(member.id) ? "已屏蔽" : "屏蔽";
    mute.title = muted.has(member.id) ? "恢复这个人的发言" : "不看这个人的发言";
    mute.addEventListener("click", async () => {
      const state = await send("muteUser", {
        userId: member.id,
        blocked: !muted.has(member.id),
      });
      if (state?.ok) show(state.settings ? state : await send("getState"));
      else note(state?.detail || "没有保存成功", true);
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

/** 表头「全选」：勾上即全选（含以后新加的群）；已全选时不允许取消，改为逐个取消 */
function headCheck(settings, storedKey, title) {
  const label = document.createElement("label");
  label.className = "group-head-check";
  const box = document.createElement("input");
  box.type = "checkbox";
  const { allOn, text } = feedGroupCounts(settings, storedKey);
  box.checked = allOn;
  box.title = allOn ? "已全选，取消请逐个去掉" : "勾选 = 全选监控群";
  box.addEventListener("change", () => {
    if (!box.checked) {
      box.checked = true;
      return;
    }
    saveFeedGroups(storedKey, []);
  });
  const name = document.createElement("span");
  name.textContent = title;
  const count = document.createElement("small");
  count.textContent = text;
  label.append(box, name, count);
  return label;
}

function fillGroupTable(box, settings) {
  if (!box) return;
  const top = box.scrollTop;
  const monitored = window.WechatFeed.monitoredGroupIds(settings);
  const pickedAll = new Set(window.WechatFeed.selectedFeedGroups(settings.all_feed_groups || [], monitored));
  const pickedCa = new Set(window.WechatFeed.selectedFeedGroups(settings.ca_feed_groups || [], monitored));
  const needle = String(ui.groupQuery || "").trim().toLowerCase();
  box.replaceChildren();
  const head = document.createElement("div");
  head.className = "group-row group-head";
  const headName = document.createElement("span");
  headName.className = "group-name";
  headName.textContent = "群";
  head.append(headName, headCheck(settings, "all_feed_groups", "全部"), headCheck(settings, "ca_feed_groups", "CA"), document.createElement("span"));
  box.append(head);
  let shown = 0;
  for (const item of settings.groups || []) {
    if ((settings.blocked_groups || []).includes(item.id)) continue;
    const listen = window.WechatFeed.groupListenUsers(settings, item.id);
    const title = groupLabel(settings, item.id, item.name);
    if (needle && !`${title} ${item.name || ""} ${item.id}`.toLowerCase().includes(needle)) continue;
    shown += 1;
    const wrap = document.createElement("div");
    wrap.className = "group-item";
    const row = document.createElement("div");
    row.className = "group-row";
    row.classList.toggle("is-open", expandedGroups.has(item.id));
    const label = document.createElement("span");
    label.className = "group-name";
    label.textContent = title;
    label.title = item.id;
    const cell = (storedKey, picked, tip) => {
      const holder = document.createElement("label");
      holder.className = "group-check";
      holder.title = tip;
      const check = document.createElement("input");
      check.type = "checkbox";
      check.checked = picked.has(item.id);
      check.addEventListener("change", () => {
        saveFeedGroups(storedKey, window.WechatFeed.nextFeedGroups(settings[storedKey] || [], item.id, monitored));
      });
      holder.append(check);
      return holder;
    };
    const membersBtn = document.createElement("button");
    membersBtn.type = "button";
    membersBtn.className = "group-members";
    membersBtn.textContent = listen === null ? "成员" : `${listen.length}人`;
    membersBtn.title = listen === null ? "查看成员、星标、屏蔽、只听部分人" : "正在只听部分用户";
    membersBtn.addEventListener("click", () => {
      if (expandedGroups.has(item.id)) expandedGroups.delete(item.id);
      else expandedGroups.add(item.id);
      refreshGroupLists(settings);
    });
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "group-remove";
    remove.textContent = "移除";
    remove.addEventListener("click", () => {
      send("unwatchGroup", { groupId: item.id }).then((state) => {
        if (state?.ok) show(state);
        else note(state?.detail || "没有保存成功", true);
      });
    });
    const actions = document.createElement("span");
    actions.className = "group-tail";
    actions.append(membersBtn, remove);
    row.append(
      label,
      cell("all_feed_groups", pickedAll, "在「全部消息」里显示这个群"),
      cell("ca_feed_groups", pickedCa, "在「只看 CA」里显示这个群"),
      actions,
    );
    wrap.append(row);
    if (expandedGroups.has(item.id)) {
      const panel = document.createElement("div");
      panel.className = "member-panel";
      const cached = memberCache.get(item.id);
      if (cached && Date.now() - cached.at < 15000) {
        renderMemberPanel(panel, settings, item.id, cached.members);
      } else {
        panel.textContent = "正在读取成员…";
        ensureMembers(item.id)
          .then(() => refreshGroupLists(settings))
          .catch((error) => {
            panel.textContent = error.message;
          });
      }
      wrap.append(panel);
    }
    box.append(wrap);
  }
  if (!shown) {
    const empty = document.createElement("p");
    empty.className = "hint group-empty";
    empty.textContent = (settings.groups || []).length ? "没有匹配的群，换个关键字。" : "还没有监控群，去本地页面添加。";
    box.append(empty);
  }
  box.scrollTop = top;
}

function show(state) {
  checkbox.disabled = false;
  floatBox.disabled = false;
  boardBox.disabled = false;
  if (!state) return;
  if (state.ok) {
    checkbox.checked = Boolean(state.enabled);
    floatBox.checked = Boolean(state.float_panel);
    boardBox.checked = state.settings?.ca_board !== false;
    const settings = state.settings || {};
    liveSettings = settings;
    paintSurfaceModes(settings);
    refreshGroupLists(settings);
    status.className = "status ok";
    status.textContent = `本地服务已连接${state.has_gmgn_key ? " · GMGN Key 已保存" : ""}`;
    keyState.textContent = state.has_gmgn_key ? "（已保存）" : "（未填写，可选）";
    return;
  }
  status.className = "status bad";
  status.textContent = "本地服务没连上";
  note(state.detail || "先执行 wechat-feed.cmd start 启动本地服务。", true);
}

send("getState").then((state) => show(state || { ok: false, enabled: false }));

const tabs = [...document.querySelectorAll("[data-tab]")];
function openTab(name) {
  for (const tab of tabs) tab.classList.toggle("on", tab.dataset.tab === name);
  for (const panel of document.querySelectorAll("[data-panel]")) panel.hidden = panel.dataset.panel !== name;
  localStorage.setItem("popupTab", name);
}
for (const tab of tabs) tab.addEventListener("click", () => openTab(tab.dataset.tab));
openTab(new URLSearchParams(location.search).get("tab") || localStorage.getItem("popupTab") || "show");

async function toggle(box, type) {
  const wanted = box.checked;
  let state = await send(type, { enabled: wanted });
  if (!state?.ok) state = await send(type, { enabled: wanted });
  if (!state?.ok) {
    box.checked = !wanted;
    note(state?.detail || "没有保存成功", true);
    return;
  }
  show(state);
}

checkbox.addEventListener("change", () => toggle(checkbox, "setOverlay"));
floatBox.addEventListener("change", () => toggle(floatBox, "setFloatPanel"));
boardBox.addEventListener("change", () => toggle(boardBox, "setCaBoard"));

for (const root of document.querySelectorAll(".surface-mode")) {
  root.addEventListener("click", async (event) => {
    const fmtBtn = event.target.closest("[data-mcap-format]");
    if (fmtBtn) {
      const state = await send("setMcapFormat", { format: fmtBtn.dataset.mcapFormat });
      if (state?.ok) show(state);
      else note(state?.detail || "没有保存成功", true);
      return;
    }
    const button = event.target.closest("[data-mode]");
    if (!button) return;
    const state = await send("setSurfaceMode", {
      surface: root.dataset.surface,
      enabled: button.dataset.mode === "ca",
    });
    if (state?.ok) show(state);
    else note(state?.detail || "没有保存成功", true);
  });
}

document.querySelector("#save-key").addEventListener("click", async () => {
  const key = keyInput.value.trim();
  let state = await send("setApiKey", { key });
  if (!state?.ok) state = await send("setApiKey", { key });
  if (!state?.ok) {
    note(state?.detail || "保存失败", true);
    return;
  }
  keyInput.value = "";
  show(state);
  note(key ? "Key 已保存到本地服务。" : "已清空 GMGN Key。");
});

const floatSites = document.querySelector("#float-sites");
const DEFAULT_FLOAT_SITES = ["https://gmgn.ai/follow?target=xTracker"];

chrome.storage.local.get("floatSites", ({ floatSites: stored }) => {
  floatSites.value = (Array.isArray(stored) ? stored : DEFAULT_FLOAT_SITES).join("\n");
});

document.querySelector("#save-sites").addEventListener("click", () => {
  const sites = [];
  for (const line of floatSites.value.split(/\r?\n/)) {
    const text = line.trim();
    if (!text) continue;
    let url;
    try {
      url = new URL(text);
    } catch (error) {
      note(`不是合法网址：${text}`, true);
      return;
    }
    if (!/^https?:$/.test(url.protocol)) {
      note(`只支持 http / https：${text}`, true);
      return;
    }
    url.hash = "";
    if (!sites.includes(url.href)) sites.push(url.href);
  }
  chrome.storage.local.set({ floatSites: sites }, () => {
    floatSites.value = sites.join("\n");
    note(sites.length
      ? `已保存 ${sites.length} 个网址，新加的网站刷新后出现浮窗。`
      : "白名单已清空，浮窗不会在任何网页出现。");
  });
});

const voiceVolume = document.querySelector("#voice-volume");
const voiceVolumeText = document.querySelector("#voice-volume-text");
const MAX_CUSTOM_BYTES = 3 * 1024 * 1024;
// 字段名沿用旧版的 enabled / sound / voiceCustom，升级后合约提示设置不丢。
const CHANNELS = {
  ca: { enabled: "enabled", sound: "sound", custom: "voiceCustom", fallback: "voice" },
  star: { enabled: "starEnabled", sound: "starSound", custom: "voiceCustomStar", fallback: "star-bell" },
  normal: { enabled: "normalEnabled", sound: "normalSound", custom: "voiceCustomNormal", fallback: "wx-ding" },
};
const channelNodes = Object.fromEntries(
  [...document.querySelectorAll("[data-channel]")].map((node) => [
    node.dataset.channel,
    {
      root: node,
      on: node.querySelector("[data-on]"),
      sound: node.querySelector("[data-sound]"),
      fileRow: node.querySelector("[data-file-row]"),
      file: node.querySelector("[data-file]"),
      custom: node.querySelector("[data-custom]"),
      test: node.querySelector("[data-test]"),
    },
  ]),
);

function paintChannel(nodes) {
  nodes.root.classList.toggle("off", !nodes.on.checked);
  nodes.fileRow.hidden = nodes.sound.value !== "custom";
}

function voiceState() {
  const state = { volume: Number(voiceVolume.value) / 100 };
  for (const [channel, spec] of Object.entries(CHANNELS)) {
    state[spec.enabled] = channelNodes[channel].on.checked;
    state[spec.sound] = channelNodes[channel].sound.value;
  }
  return state;
}

function saveVoice() {
  voiceVolumeText.textContent = `${voiceVolume.value}%`;
  Object.values(channelNodes).forEach(paintChannel);
  chrome.storage.local.set({ voice: voiceState() });
}

const customKeys = Object.values(CHANNELS).map((spec) => spec.custom);
chrome.storage.local.get(["voice", ...customKeys], (stored) => {
  const state = { volume: 0.8, ...(stored.voice || {}) };
  for (const [channel, spec] of Object.entries(CHANNELS)) {
    const nodes = channelNodes[channel];
    nodes.on.checked = Boolean(state[spec.enabled]);
    nodes.sound.value = state[spec.sound] || spec.fallback;
    if (!nodes.sound.value) nodes.sound.value = spec.fallback;
    const custom = stored[spec.custom];
    if (custom?.name) nodes.custom.textContent = `已加载：${custom.name}（点击更换）`;
    paintChannel(nodes);
  }
  voiceVolume.value = String(Math.round(state.volume * 100));
  voiceVolumeText.textContent = `${voiceVolume.value}%`;
});

voiceVolume.addEventListener("input", saveVoice);

for (const [channel, spec] of Object.entries(CHANNELS)) {
  const nodes = channelNodes[channel];
  nodes.on.addEventListener("change", saveVoice);
  nodes.sound.addEventListener("change", saveVoice);
  nodes.file.addEventListener("change", () => {
    const file = nodes.file.files && nodes.file.files[0];
    if (!file) return;
    if (file.size > MAX_CUSTOM_BYTES) {
      note("文件超过 3 MB，换一个短一点的音频。", true);
      return;
    }
    const reader = new FileReader();
    reader.onload = () => {
      chrome.storage.local.set({ [spec.custom]: { name: file.name, dataUrl: String(reader.result) } }, () => {
        nodes.custom.textContent = `已加载：${file.name}（点击更换）`;
        nodes.sound.value = "custom";
        saveVoice();
      });
    };
    reader.onerror = () => note("读取文件失败。", true);
    reader.readAsDataURL(file);
  });
  nodes.test.addEventListener("click", async () => {
    const result = await send("voiceTest", {
      channel,
      sound: nodes.sound.value,
      volume: Number(voiceVolume.value) / 100,
    });
    if (!result?.ok) note(`试听失败：${result?.detail || "浏览器拒绝播放"}`, true);
  });
}

document.querySelector("#voice-tab").addEventListener("click", (event) => {
  event.preventDefault();
  chrome.tabs.create({ url: chrome.runtime.getURL("popup.html?tab=sound") });
});

document.querySelector("#open").addEventListener("click", () => {
  const ver = chrome.runtime.getManifest().version;
  chrome.tabs.create({ url: `http://127.0.0.1:8765/?v=${encodeURIComponent(ver)}` });
});
