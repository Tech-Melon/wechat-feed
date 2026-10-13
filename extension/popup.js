const status = document.querySelector("#status");
const noteBox = document.querySelector("#note");
const checkbox = document.querySelector("#gmgn");
const floatBox = document.querySelector("#float-panel");
const keyInput = document.querySelector("#key");
const keyState = document.querySelector("#key-state");

try {
  chrome.runtime.connect({ name: "popup" });
} catch (error) {
  // 弹窗关掉时后台仍可能活着
}

let noteTimer = 0;
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

function show(state) {
  checkbox.disabled = false;
  floatBox.disabled = false;
  if (!state) return;
  if (state.ok) {
    checkbox.checked = Boolean(state.enabled);
    floatBox.checked = Boolean(state.float_panel);
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

const watermark = document.querySelector("#watermark");
chrome.storage.local.get("watermark", ({ watermark: stored }) => {
  watermark.value = typeof stored === "string" ? stored : "";
});
watermark.addEventListener("change", () => {
  chrome.storage.local.set({ watermark: watermark.value.trim() }, () => note("水印已更新。"));
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
  chrome.tabs.create({ url: "http://127.0.0.1:8765/" });
});
