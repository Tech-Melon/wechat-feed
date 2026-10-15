importScripts("feed.js");

const API = "http://127.0.0.1:8765";

let overlay = false;
let floatPanel = false;
let settings = {};
let source = null;
const ports = new Set();

const SOUND_IDS = [
  "voice", "chime", "alarm",
  "wx-ding", "wx-pop", "wx-tick",
  "star-voice", "star-bell", "star-harp",
];
// 一条消息只响一种：合约 > 星标用户 > 普通消息。前一种没开就往下找。
// 听得见的范围跟群来源一致：只勾了「只看 CA」的群，没有合约的普通消息不响。
const CHANNELS = {
  ca: { rank: 3, enabled: "enabled", sound: "sound", custom: "voiceCustom", fallback: "voice" },
  star: { rank: 2, enabled: "starEnabled", sound: "starSound", custom: "voiceCustomStar", fallback: "star-bell" },
  normal: { rank: 1, enabled: "normalEnabled", sound: "normalSound", custom: "voiceCustomNormal", fallback: "wx-ding" },
};
const ALERT_GAP_MS = 1500;
// 服务启动回拉的历史消息也会走事件流，太旧的不报。
const ALERT_MAX_AGE_S = 120;
let voice = {
  enabled: false,
  sound: "voice",
  starEnabled: false,
  starSound: "star-bell",
  normalEnabled: false,
  normalSound: "wx-ding",
  volume: 0.8,
};
let lastAlert = 0;
let lastRank = 0;
let creatingOffscreen = null;
let keepAlive = 0;
const announced = new Set();

async function ensureOffscreen() {
  if (await chrome.offscreen.hasDocument()) return;
  if (!creatingOffscreen) {
    creatingOffscreen = chrome.offscreen
      .createDocument({
        url: "offscreen.html",
        reasons: ["AUDIO_PLAYBACK"],
        justification: "群里有新消息或新合约时播放提示音",
      })
      .finally(() => {
        creatingOffscreen = null;
      });
  }
  await creatingOffscreen;
}

async function soundSrc(choice, channel) {
  const spec = CHANNELS[channel] || CHANNELS.ca;
  if (choice === "custom") {
    const stored = await chrome.storage.local.get(spec.custom);
    if (stored[spec.custom]?.dataUrl) return stored[spec.custom].dataUrl;
  }
  const id = SOUND_IDS.includes(choice) ? choice : spec.fallback;
  return chrome.runtime.getURL(`sounds/${id}.wav`);
}

async function playSound(choice, volume, channel = "ca") {
  await ensureOffscreen();
  const src = await soundSrc(choice, channel);
  return chrome.runtime.sendMessage({ target: "offscreen", type: "play", src, volume });
}

function pickChannel(card) {
  const inAll = WechatFeed.cardInFeed(card, settings, false);
  const inCa = WechatFeed.cardInFeed(card, settings, true);
  if (!inAll && !inCa) return "";
  if ((card.addresses || []).length && voice.enabled) return "ca";
  if (voice.starEnabled && (settings.starred_users || []).includes(card.senderUsername)) return "star";
  if (voice.normalEnabled && inAll) return "normal";
  return "";
}

function announce(card) {
  if (!card?.id || card.isSent) return;
  if ((settings.blocked_groups || []).includes(card.conversationId)) return;
  if ((settings.blocked_users || []).includes(card.senderUsername)) return;
  if (Date.now() / 1000 - (Number(card.createTime) || 0) > ALERT_MAX_AGE_S) return;
  if (announced.has(card.id)) return;
  announced.add(card.id);
  if (announced.size > 500) announced.delete(announced.values().next().value);
  const channel = pickChannel(card);
  if (!channel) return;
  const spec = CHANNELS[channel];
  const now = Date.now();
  // 间隔内同级或更低级的不再响，更重要的可以插队。
  if (now - lastAlert < ALERT_GAP_MS && spec.rank <= lastRank) return;
  lastAlert = now;
  lastRank = spec.rank;
  playSound(voice[spec.sound], voice.volume, channel).catch(() => {});
}

function anyVoice() {
  return Boolean(voice.enabled || voice.starEnabled || voice.normalEnabled);
}

// 开着语音时要一直收事件流；扩展 API 调用会重置 service worker 的空闲计时。
function applyVoice(next) {
  voice = { ...voice, ...(next || {}) };
  if (anyVoice() && !keepAlive) {
    keepAlive = setInterval(() => chrome.runtime.getPlatformInfo(() => {}), 20000);
    startStream();
  } else if (!anyVoice() && keepAlive) {
    clearInterval(keepAlive);
    keepAlive = 0;
  }
}

function publicSettings(raw) {
  const next = { ...(raw || {}) };
  next.has_gmgn_key = Boolean(next.has_gmgn_key || next.gmgn_api_key);
  next.gmgn_api_key = "";
  return next;
}

function snapshotState(extra = {}) {
  return {
    ok: true,
    enabled: Boolean(overlay),
    float_panel: Boolean(floatPanel),
    has_gmgn_key: Boolean(settings.has_gmgn_key),
    ca_only: Boolean(settings.ca_only),
    upstream: settings.upstream_base,
    settings,
    ...extra,
  };
}

function postToPorts(message) {
  for (const port of ports) {
    try {
      port.postMessage(message);
    } catch (error) {
      ports.delete(port);
    }
  }
}

function stopStream() {
  if (source) {
    source.close();
    source = null;
  }
}

function persistFlags() {
  return chrome.storage.local.set({
    gmgnOverlay: overlay,
    floatPanel,
    caOnly: Boolean(settings.ca_only),
    hasGmgnKey: Boolean(settings.has_gmgn_key),
    settings,
  });
}

function applySettings(raw) {
  settings = publicSettings(raw);
  overlay = Boolean(settings.gmgn_overlay);
  floatPanel = Boolean(settings.float_panel);
  postToPorts({ type: "settings", settings });
  startStream();
  return persistFlags();
}

function startStream() {
  if (source) return;
  source = new EventSource(`${API}/api/events`);
  source.onerror = () => {
    stopStream();
    setTimeout(startStream, 1500);
  };
  source.onmessage = (event) => {
    let payload;
    try {
      payload = JSON.parse(event.data);
    } catch (error) {
      return;
    }
    if (payload.type === "hello" && payload.settings) {
      applySettings(payload.settings);
    }
    if (payload.type === "settings" && payload.settings) {
      applySettings(payload.settings);
    }
    if (payload.type === "message" && payload.card) {
      postToPorts({ type: "message", card: payload.card });
      announce(payload.card);
    }
    if (payload.type === "patch" && payload.card) {
      postToPorts({ type: "patch", card: payload.card });
    }
    if (payload.type === "cleared") {
      postToPorts({ type: "cleared" });
    }
  };
}

async function refreshSettings() {
  const response = await fetch(`${API}/api/settings`);
  if (!response.ok) {
    throw new Error("settings");
  }
  await applySettings(await response.json());
  return settings;
}

async function putSettings(patch) {
  const merged = publicSettings({ ...settings, ...patch });
  overlay = Boolean(merged.gmgn_overlay);
  floatPanel = Boolean(merged.float_panel);
  settings = merged;
  await persistFlags();
  postToPorts({ type: "settings", settings });
  const response = await fetch(`${API}/api/settings`, {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(patch),
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = typeof body.detail === "string" ? body.detail : "保存失败";
    throw new Error(detail);
  }
  await applySettings(body);
  return settings;
}

function blobToDataUrl(blob) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ""));
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(blob);
  });
}

const avatarMem = new Map();
const AVATAR_TTL_MS = 24 * 3600 * 1000;

async function proxyCached(url) {
  const mem = avatarMem.get(url);
  if (mem && Date.now() - mem.at < AVATAR_TTL_MS) return mem.dataUrl;
  const cache = await caches.open("wf-avatar-v2");
  let response = await cache.match(url);
  const cachedAt = Number(response?.headers.get("X-Wf-Cached-At") || 0);
  if (!response || Date.now() - cachedAt >= AVATAR_TTL_MS) {
    try {
      const fresh = await fetch(url, { cache: "no-store" });
      if (!fresh.ok) throw new Error("proxy");
      const headers = new Headers(fresh.headers);
      headers.set("X-Wf-Cached-At", String(Date.now()));
      const stored = new Response(await fresh.clone().blob(), {
        status: fresh.status,
        headers,
      });
      await cache.put(url, stored.clone());
      response = stored;
    } catch (error) {
      if (!response) throw error;
    }
  }
  const dataUrl = await blobToDataUrl(await response.blob());
  avatarMem.set(url, { dataUrl, at: Date.now() });
  return dataUrl;
}

chrome.runtime.onConnect.addListener((port) => {
  ports.add(port);
  port.onDisconnect.addListener(() => {
    ports.delete(port);
  });
  if (port.name === "gmgn-feed") {
    port.onMessage.addListener((message) => {
      if (message?.type === "snapshot") {
        const params = new URLSearchParams({ limit: "80", include_all_groups: "true" });
        if (message.before) params.set("before", String(message.before));
        if (message.before_received) params.set("before_received", String(message.before_received));
        if (message.conversation_id) params.set("conversation_id", String(message.conversation_id));
        fetch(`${API}/api/messages?${params}`)
          .then((response) => response.json())
          .then((body) => {
            port.postMessage({
              type: "snapshot",
              cards: body.messages || [],
              has_more: Boolean(body.has_more),
              append: Boolean(message.before),
            });
          })
          .catch(() => {
            port.postMessage({
              type: "snapshot",
              cards: [],
              has_more: false,
              append: Boolean(message.before),
            });
          });
      }
    });
  }
  startStream();
  port.postMessage({ type: "settings", settings });
});

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type === "getState") {
    refreshSettings()
      .then(() => sendResponse(snapshotState()))
      .catch(() => {
        const hasCache = Boolean(settings && Object.keys(settings).length);
        sendResponse({
          ...snapshotState(),
          ok: hasCache,
          stale: true,
          detail: hasCache ? "" : "本地服务没连上。先启动本地页菜单第 1 项。",
        });
      });
    return true;
  }
  if (message?.type === "setOverlay") {
    putSettings({ gmgn_overlay: Boolean(message.enabled) })
      .then(() => sendResponse(snapshotState()))
      .catch((error) => sendResponse({ ok: false, detail: error.message || "本地服务没有启动" }));
    return true;
  }
  if (message?.type === "setFloatPanel") {
    putSettings({ float_panel: Boolean(message.enabled) })
      .then(() => sendResponse(snapshotState()))
      .catch((error) => sendResponse({ ok: false, detail: error.message || "本地服务没有启动" }));
    return true;
  }
  if (message?.type === "muteGroup" && message.groupId) {
    const blocked = Array.from(new Set([...(settings.blocked_groups || []), String(message.groupId)]));
    putSettings({ blocked_groups: blocked })
      .then(() => sendResponse({ ok: true }))
      .catch(() => sendResponse({ ok: false }));
    return true;
  }
  if (message?.type === "muteUser" && message.userId) {
    const userId = String(message.userId);
    const blocked = new Set(settings.blocked_users || []);
    if (message.blocked === false) blocked.delete(userId);
    else blocked.add(userId);
    putSettings({ blocked_users: Array.from(blocked) })
      .then(() => sendResponse(snapshotState({ blocked_users: Array.from(blocked) })))
      .catch(() => sendResponse({ ok: false }));
    return true;
  }
  if (message?.type === "starUser" && message.userId) {
    const current = new Set(settings.starred_users || []);
    const colors = { ...(settings.star_colors || {}) };
    const userId = String(message.userId);
    const color = String(message.color || colors[userId] || "amber");
    if (message.starred === false) {
      current.delete(userId);
      delete colors[userId];
    } else {
      current.add(userId);
      colors[userId] = color;
    }
    putSettings({ starred_users: Array.from(current), star_colors: colors })
      .then(() => sendResponse(snapshotState({ starred_users: Array.from(current), star_colors: colors })))
      .catch(() => sendResponse({ ok: false }));
    return true;
  }
  if (message?.type === "setGroupUsers" && message.groupId) {
    const groupId = String(message.groupId);
    const map = { ...(settings.group_users || {}) };
    if (message.users === null || message.users === undefined) delete map[groupId];
    else map[groupId] = Array.isArray(message.users) ? message.users.map(String) : [];
    putSettings({ group_users: map })
      .then(() => sendResponse(snapshotState()))
      .catch(() => sendResponse({ ok: false }));
    return true;
  }
  if (message?.type === "getGroupMembers" && message.groupId) {
    fetch(`${API}/api/groups/${encodeURIComponent(message.groupId)}/members`)
      .then(async (response) => {
        const body = await response.json().catch(() => ({}));
        if (!response.ok) {
          sendResponse({ ok: false, detail: body.detail || "读不到成员" });
          return;
        }
        sendResponse({ ok: true, members: body.members || [] });
      })
      .catch(() => sendResponse({ ok: false, detail: "读不到成员" }));
    return true;
  }
  if (message?.type === "remarkUser" && message.userId) {
    const remarks = { ...(settings.user_remarks || {}) };
    const text = String(message.text || "").trim();
    if (text) remarks[message.userId] = text;
    else delete remarks[message.userId];
    putSettings({ user_remarks: remarks })
      .then(() => sendResponse({ ok: true }))
      .catch(() => sendResponse({ ok: false }));
    return true;
  }
  if (message?.type === "remarkGroup" && message.groupId) {
    const remarks = { ...(settings.group_remarks || {}) };
    const text = String(message.text || "").trim();
    if (text) remarks[message.groupId] = text;
    else delete remarks[message.groupId];
    putSettings({ group_remarks: remarks })
      .then(() => sendResponse({ ok: true }))
      .catch(() => sendResponse({ ok: false }));
    return true;
  }
  if (message?.type === "setMcapFormat") {
    const format = String(message.format || "cn") === "en" ? "en" : "cn";
    putSettings({ mcap_format: format })
      .then(() => sendResponse(snapshotState()))
      .catch((error) => sendResponse({ ok: false, detail: error.message || "没有保存成功" }));
    return true;
  }
  if (message?.type === "setApiKey") {
    putSettings({ gmgn_api_key: String(message.key || "") })
      .then(() => sendResponse(snapshotState()))
      .catch((error) => sendResponse({ ok: false, detail: error.message || "保存失败" }));
    return true;
  }
  if (message?.type === "setCaOnly") {
    putSettings({ ca_only: Boolean(message.enabled) })
      .then(() => sendResponse(snapshotState()))
      .catch(() => sendResponse({ ok: false }));
    return true;
  }
  if (message?.type === "setSurfaceMode") {
    const key =
      message.surface === "gmgn"
        ? "gmgn_ca_only"
        : message.surface === "float"
          ? "float_ca_only"
          : "ca_only";
    putSettings({ [key]: Boolean(message.enabled) })
      .then(() => sendResponse(snapshotState()))
      .catch(() => sendResponse({ ok: false }));
    return true;
  }
  if (message?.type === "setFeedGroups") {
    const key = message.caOnly ? "ca_feed_groups" : "all_feed_groups";
    const groups = Array.isArray(message.groups) ? message.groups.map(String) : [];
    putSettings({ [key]: groups })
      .then(() => sendResponse(snapshotState()))
      .catch(() => sendResponse({ ok: false }));
    return true;
  }
  if (message?.type === "unwatchGroup" && message.groupId) {
    const id = String(message.groupId);
    const groups = (settings.groups || []).filter((item) => item.id !== id);
    const allFeed = (settings.all_feed_groups || []).filter((item) => item !== id);
    const caFeed = (settings.ca_feed_groups || []).filter((item) => item !== id);
    const users = { ...(settings.group_users || {}) };
    delete users[id];
    putSettings({ groups, all_feed_groups: allFeed, ca_feed_groups: caFeed, group_users: users })
      .then(() => sendResponse(snapshotState()))
      .catch(() => sendResponse({ ok: false }));
    return true;
  }
  if (message?.type === "voiceTest") {
    const channel = CHANNELS[message.channel] ? message.channel : "ca";
    playSound(message.sound || voice[CHANNELS[channel].sound], message.volume ?? voice.volume, channel)
      .then((result) => sendResponse(result || { ok: true }))
      .catch((error) => sendResponse({ ok: false, detail: String(error?.message || error) }));
    return true;
  }
  if (message?.type === "proxyDataUrl" && message.url) {
    proxyCached(String(message.url))
      .then((dataUrl) => sendResponse({ ok: true, dataUrl }))
      .catch(() => sendResponse({ ok: false }));
    return true;
  }
  return false;
});

chrome.storage.local.get(["settings", "gmgnOverlay", "floatPanel", "caOnly", "voice"], (stored) => {
  if (stored.settings) settings = publicSettings(stored.settings);
  overlay = Boolean(stored.gmgnOverlay);
  floatPanel = Boolean(stored.floatPanel);
  if (stored.caOnly !== undefined) settings.ca_only = Boolean(stored.caOnly);
  applyVoice(stored.voice);
  refreshSettings().catch(() => {});
});

const DEFAULT_FLOAT_SITES = ["https://gmgn.ai/follow?target=xTracker"];
const FLOAT_SCRIPT_ID = "wf-float";
const STATIC_HOSTS = new Set(["gmgn.ai"]);

// 只给白名单所在的站点注入脚本；具体到路径和参数的判断在 content.js 里做。
function floatMatches(sites) {
  const matches = new Set();
  for (const site of sites || []) {
    try {
      const url = new URL(site);
      if (!/^https?:$/.test(url.protocol) || STATIC_HOSTS.has(url.hostname)) continue;
      matches.add(`${url.protocol}//${url.hostname}/*`);
    } catch (error) {
      // 弹窗保存前已校验，这里跳过坏数据
    }
  }
  return [...matches];
}

let floatSync = Promise.resolve();
function syncFloatScripts(sites) {
  floatSync = floatSync
    .then(async () => {
      const existing = await chrome.scripting.getRegisteredContentScripts({ ids: [FLOAT_SCRIPT_ID] });
      if (existing.length) await chrome.scripting.unregisterContentScripts({ ids: [FLOAT_SCRIPT_ID] });
      const matches = floatMatches(sites);
      if (!matches.length) return;
      await chrome.scripting.registerContentScripts([
        {
          id: FLOAT_SCRIPT_ID,
          matches,
          js: ["feed.js", "content.js"],
          runAt: "document_idle",
          persistAcrossSessions: true,
        },
      ]);
    })
    .catch(() => {});
  return floatSync;
}

chrome.storage.local.get("floatSites", ({ floatSites }) => {
  if (!Array.isArray(floatSites)) {
    chrome.storage.local.set({ floatSites: DEFAULT_FLOAT_SITES });
    return;
  }
  syncFloatScripts(floatSites);
});

chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== "local") return;
  if (changes.voice) applyVoice(changes.voice.newValue);
  if (changes.floatSites) syncFloatScripts(changes.floatSites.newValue || DEFAULT_FLOAT_SITES);
});

// 浏览器启动时唤醒后台，语音提醒不用等打开网页。
chrome.runtime.onStartup.addListener(() => {});
