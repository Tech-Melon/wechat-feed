"use strict";

// 开放接口管理页：开关与监听、密钥、接收统计、推送过的群。

const state = { data: null, formDirty: false };

function $(sel) {
  return document.querySelector(sel);
}

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

function clock(sec) {
  if (!sec) return "—";
  const d = new Date(sec * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getMonth() + 1}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

async function call(url, options = {}) {
  const res = await fetch(url, options);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
  return data;
}

function jsonInit(method, body) {
  return { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
}

async function saveSettings(patch) {
  return call("/api/settings", jsonInit("PUT", patch));
}

function renderForm(data) {
  if (state.formDirty) return;
  $("#op-enabled").checked = Boolean(data.enabled);
  $("#op-host").value = data.host || "0.0.0.0";
  $("#op-port").value = data.port || 8766;
  $("#op-allow").value = (data.allowIps || []).join("\n");
  $("#op-auto").value = (data.autoWatch || []).join(", ");
}

function endpoints(data) {
  const port = data.port || 8766;
  const host = data.host || "0.0.0.0";
  const ips = host === "0.0.0.0" || host === "::" ? data.lanAddresses || [] : [host];
  return ips.map((ip) => `http://${ip}:${port}/open/${data.version}/messages`);
}

function render(data) {
  state.data = data;
  renderForm(data);
  const badge = $("#op-badge");
  const listener = data.listener || {};
  if (listener.listening) {
    badge.className = "gm-badge ok";
    badge.textContent = "监听中";
  } else if (listener.error) {
    badge.className = "gm-badge bad";
    badge.textContent = "监听失败";
  } else {
    badge.className = "gm-badge";
    badge.textContent = data.enabled && !data.hasKey ? "缺少密钥" : "未开启";
  }
  const urls = endpoints(data);
  $("#op-listen").innerHTML = listener.listening
    ? `推送地址：${urls.map((u) => `<code>${esc(u)}</code>`).join(" 或 ") || esc(listener.address)}`
    : listener.error
      ? `<span class="op-err">${esc(listener.address)} ${esc(listener.error)}</span>`
      : data.enabled
        ? "已开启，但还没有密钥，生成密钥后才会监听。"
        : "未开启。";
  $("#op-key-state").textContent = data.hasKey ? `已设置密钥 ${data.keyHint}` : "还没有密钥。";

  const s = data.stats || {};
  const kpi = (label, value, extra, cls) =>
    `<div class="gm-kpi${cls ? ` ${cls}` : ""}"><span>${esc(label)}</span><b>${esc(value)}</b>${extra ? `<small>${esc(extra)}</small>` : ""}</div>`;
  $("#op-kpis").innerHTML = [
    kpi("请求", s.requests || 0),
    kpi("入队", s.accepted || 0, `队列中 ${data.queue || 0}`),
    kpi("重复（已忽略）", s.duplicate || 0),
    kpi("未监控仅入库", s.stored || 0),
    kpi("拒收", s.rejected || 0, "", s.rejected ? "warn" : ""),
    kpi("签名失败", s.authFailed || 0, s.lastError || "", s.authFailed ? "bad" : ""),
  ].join("");
  $("#op-last").textContent = s.lastAt ? `最近一次 ${clock(s.lastAt)} 来自 ${s.lastIp}` : "";

  const chats = data.chats || [];
  $("#op-chats-empty").hidden = chats.length > 0;
  window.WechatFeed.syncList(
    $("#op-chats"),
    chats,
    (chat) => chat.id,
    (chat) => `<tr>
      <td class="l"><span class="op-plat">${esc(chat.platformName || chat.platform)}</span>${esc(chat.name || "未命名群")}</td>
      <td class="l"><code>${esc(chat.id)}</code></td>
      <td>${esc(chat.messages)}</td>
      <td>${esc(clock(chat.lastSeen))}</td>
      <td>${chat.watched ? "已监控" : "未监控（不显示）"}</td>
      <td>
        <button type="button" class="dm-btn small" data-watch="${esc(chat.id)}" data-on="${chat.watched ? "0" : "1"}">${chat.watched ? "移除监控" : "加入监控"}</button>
        <button type="button" class="dm-btn small" data-forget="${esc(chat.id)}" title="从列表里隐藏；以后再推送会重新出现，但不会被自动监控加回来">删除记录</button>
      </td>
    </tr>`,
  );

  const url = urls[0] || `http://<本机局域网 IP>:${data.port || 8766}/open/${data.version}/messages`;
  $("#op-sample").textContent = [
    `POST ${url}`,
    "Content-Type: application/json",
    "X-WF-Timestamp: <当前 Unix 秒>",
    "X-WF-Signature: hex( HMAC_SHA256( 密钥, \"<时间戳>\\nPOST\\n/open/v1/messages\\n\" + 原始请求体字节 ) )",
    "",
    JSON.stringify(
      {
        platform: "telegram",
        messages: [
          {
            id: "8812",
            chat: { id: "-1001234567", name: "Alpha 冲狗群", avatar: "https://..." },
            sender: { id: "998877", name: "Alice", avatar: "https://..." },
            time: Date.now(),
            type: "text",
            text: "看下这个 0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c",
          },
        ],
      },
      null,
      2,
    ),
  ].join("\n");
}

async function refresh() {
  try {
    render(await call("/api/open-api"));
  } catch (error) {
    $("#op-badge").className = "gm-badge bad";
    $("#op-badge").textContent = "读不到本地服务";
  }
}

async function setWatched(chatId, on) {
  // 每次现读：本地页可能刚改过监控群，不能拿旧配置覆盖。
  const settings = await call("/api/settings");
  const chat = (state.data.chats || []).find((item) => item.id === chatId) || {};
  const groups = (settings.groups || []).filter((item) => item.id !== chatId);
  const patch = {};
  if (on) {
    groups.push({ id: chatId, name: chat.name || "" });
    patch.blocked_groups = (settings.blocked_groups || []).filter((item) => item !== chatId);
  } else {
    // 和「群聊选择管理」里的移除一致：两套显示来源和成员监听一起去掉。
    patch.all_feed_groups = (settings.all_feed_groups || []).filter((item) => item !== chatId);
    patch.ca_feed_groups = (settings.ca_feed_groups || []).filter((item) => item !== chatId);
    const users = { ...(settings.group_users || {}) };
    delete users[chatId];
    patch.group_users = users;
  }
  patch.groups = groups;
  await saveSettings(patch);
}

$("#op-form").addEventListener("input", () => {
  state.formDirty = true;
});

$("#op-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const out = $("#op-form-out");
  const port = Number($("#op-port").value || 8766);
  const allow = $("#op-allow")
    .value.split(/[\s,]+/)
    .map((item) => item.trim())
    .filter(Boolean);
  try {
    await saveSettings({
      open_api_enabled: $("#op-enabled").checked,
      open_api_host: $("#op-host").value.trim() || "0.0.0.0",
      open_api_port: port,
      open_api_allow_ips: allow,
      open_auto_watch: $("#op-auto")
        .value.split(/[\s,，]+/)
        .map((item) => item.trim().toLowerCase())
        .filter(Boolean),
    });
    state.formDirty = false;
    out.textContent = "已保存。";
    await refresh();
  } catch (error) {
    out.textContent = `保存失败：${error.message}`;
  }
});

$("#op-rotate").addEventListener("click", async () => {
  if (state.data && state.data.hasKey && !confirm("重新生成后旧密钥立即失效，推送端要换成新密钥。继续？")) return;
  try {
    const out = await call("/api/open-api/key", jsonInit("POST", { action: "rotate" }));
    $("#op-key-text").textContent = out.key;
    $("#op-key-new").hidden = false;
    await refresh();
  } catch (error) {
    alert(`生成失败：${error.message}`);
  }
});

$("#op-clear").addEventListener("click", async () => {
  if (!confirm("清除密钥会同时关闭开放接口，推送端会收到 503。继续？")) return;
  try {
    await call("/api/open-api/key", jsonInit("POST", { action: "clear" }));
    $("#op-key-new").hidden = true;
    state.formDirty = false;
    await refresh();
  } catch (error) {
    alert(`清除失败：${error.message}`);
  }
});

$("#op-key-copy").addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText($("#op-key-text").textContent);
    $("#op-key-copy").textContent = "已复制";
  } catch (error) {
    $("#op-key-copy").textContent = "复制失败，请手动选中";
  }
});

$("#op-chats").addEventListener("click", async (event) => {
  const watch = event.target.closest("[data-watch]");
  const forget = event.target.closest("[data-forget]");
  try {
    if (watch) {
      await setWatched(watch.dataset.watch, watch.dataset.on === "1");
    } else if (forget) {
      if (!confirm("从列表里隐藏这个群？已入库的消息不受影响，再推送会重新出现。")) return;
      await call(`/api/open-api/chats/${encodeURIComponent(forget.dataset.forget)}`, { method: "DELETE" });
    } else {
      return;
    }
    await refresh();
  } catch (error) {
    alert(error.message);
  }
});

refresh();
setInterval(() => {
  if (!document.hidden) refresh();
}, 5000);
