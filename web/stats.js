"use strict";

// 消息统计页：时间范围 × 来源 × 群 / 发言人筛选，数据来自 GET /api/stats（小时汇总表）。

const state = {
  range: localStorage.getItem("wf-stats-range") || "7d",
  source: "all",
  group: "",
  groupName: "",
  sender: "",
  senderName: "",
  data: null,
};

function $(sel) {
  return document.querySelector(sel);
}

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

function num(value) {
  return Number(value || 0).toLocaleString("zh-CN");
}

function when(sec) {
  if (!sec) return "—";
  const d = new Date(sec * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getMonth() + 1}-${pad(d.getDate())} ${pad(d.getHours())}:00`;
}

function sourceName(key) {
  if (key === "all") return "全部";
  if (key === "wechat") return "微信";
  return window.WechatFeed.groupSource(`${key}:x`).name;
}

function chip(groupId) {
  const source = window.WechatFeed.groupSource(groupId);
  if (!source.open) return "";
  return `<span class="st-plat" style="background:${esc(source.color)}">${esc(source.short)}</span>`;
}

/** 柱状图：两组数（全部 / 含合约）叠在同一根柱上，含合约画在底部。只拼数字和转义过的标签。 */
function bars(rows, labels, height) {
  const width = 960;
  const pad = { left: 44, right: 8, top: 10, bottom: 24 };
  const inner = width - pad.left - pad.right;
  const tall = height - pad.top - pad.bottom;
  const max = Math.max(1, ...rows.map((r) => r[0]));
  const step = inner / Math.max(1, rows.length);
  const barW = Math.max(1, Math.min(28, step * 0.7));
  const y = (v) => pad.top + tall - (v / max) * tall;
  const grid = [0, 0.5, 1]
    .map((f) => {
      const v = Math.round(max * f);
      return `<line class="grid" x1="${pad.left}" x2="${width - pad.right}" y1="${y(v)}" y2="${y(v)}"/><text class="axis" x="${pad.left - 6}" y="${y(v) + 4}" text-anchor="end">${num(v)}</text>`;
    })
    .join("");
  const every = Math.max(1, Math.ceil(rows.length / 12));
  const body = rows
    .map((r, i) => {
      const x = pad.left + i * step + (step - barW) / 2;
      const title = `<title>${esc(labels[i])}：${num(r[0])} 条，含合约 ${num(r[1])}</title>`;
      const all = `<rect class="bar" x="${x}" y="${y(r[0])}" width="${barW}" height="${Math.max(0, pad.top + tall - y(r[0]))}">${title}</rect>`;
      const ca = r[1] ? `<rect class="bar ca" x="${x}" y="${y(r[1])}" width="${barW}" height="${Math.max(0, pad.top + tall - y(r[1]))}">${title}</rect>` : "";
      const label = i % every === 0 ? `<text class="axis" x="${x + barW / 2}" y="${height - 6}" text-anchor="middle">${esc(labels[i])}</text>` : "";
      return all + ca + label;
    })
    .join("");
  return `<svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" role="img">${grid}${body}</svg>`;
}

/** 补齐没有消息的日期，曲线不会把空白天挤掉。 */
function fillDays(daily, since) {
  const map = new Map(daily.map((d) => [d.day, d]));
  const out = [];
  const start = new Date(since * 1000);
  const today = new Date();
  for (let d = new Date(start); d <= today; d.setDate(d.getDate() + 1)) {
    const key = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
    out.push(map.get(key) || { day: key, messages: 0, ca: 0 });
  }
  return out;
}

function renderControls(data) {
  for (const btn of document.querySelectorAll("#st-range button")) {
    btn.classList.toggle("on", btn.dataset.range === state.range);
  }
  const keys = ["all", ...data.sources.map((s) => s.source)];
  if (!keys.includes(state.source)) keys.push(state.source);
  const counts = Object.fromEntries(data.sources.map((s) => [s.source, s.messages]));
  $("#st-source").innerHTML = keys
    .map((key) => {
      const count = key === "all" ? "" : ` ${num(counts[key])}`;
      return `<button type="button" data-source="${esc(key)}" class="${key === state.source ? "on" : ""}">${esc(sourceName(key))}${esc(count)}</button>`;
    })
    .join("");
  const filters = [];
  if (state.group) filters.push(`<span class="st-chip">群：${esc(state.groupName || state.group)}<button type="button" data-clear="group" aria-label="取消群筛选">×</button></span>`);
  if (state.sender) filters.push(`<span class="st-chip">发言人：${esc(state.senderName || state.sender)}<button type="button" data-clear="sender" aria-label="取消发言人筛选">×</button></span>`);
  $("#st-filters").innerHTML = filters.join("");
}

function render(data) {
  state.data = data;
  renderControls(data);
  const t = data.totals;
  const kpi = (label, value, extra) =>
    `<div class="gm-kpi"><span>${esc(label)}</span><b>${esc(value)}</b>${extra ? `<small>${esc(extra)}</small>` : ""}</div>`;
  const share = t.messages ? `占 ${((t.ca / t.messages) * 100).toFixed(1)}%` : "";
  $("#st-kpis").innerHTML = [
    kpi("消息", num(t.messages)),
    kpi("含合约的消息", num(t.ca), share),
    kpi("活跃群", num(t.groups)),
    kpi("发言人", num(t.senders)),
  ].join("");

  const days = fillDays(data.daily, data.since);
  $("#st-daily").innerHTML = days.length
    ? bars(days.map((d) => [d.messages, d.ca]), days.map((d) => d.day.slice(5)), 220)
    : `<p class="gm-muted">这段时间没有消息。</p>`;
  $("#st-hours").innerHTML = bars(
    data.hours.map((v) => [v, 0]),
    data.hours.map((_, h) => `${h}时`),
    140,
  );

  window.WechatFeed.syncList(
    $("#st-groups"),
    data.groups,
    (row) => row.id,
    (row) => `<tr>
      <td class="l">${chip(row.id)}<button type="button" class="st-link" data-group="${esc(row.id)}" data-name="${esc(row.name)}" title="${esc(row.id)}">${esc(row.name)}</button>${row.watched ? "" : `<span class="st-tag">未监控</span>`}${row.blocked ? `<span class="st-tag">已屏蔽</span>` : ""}</td>
      <td>${num(row.messages)}</td><td>${num(row.ca)}</td><td>${num(row.senders)}</td><td>${esc(when(row.lastAt))}</td>
    </tr>`,
  );
  window.WechatFeed.syncList(
    $("#st-senders"),
    data.senders,
    (row) => row.id,
    (row) => `<tr>
      <td class="l">${chip(row.id)}<button type="button" class="st-link" data-sender="${esc(row.id)}" data-name="${esc(row.name)}" title="${esc(row.id)}">${esc(row.name)}</button>${row.starred ? `<span class="st-tag">★ 星标</span>` : ""}</td>
      <td>${num(row.messages)}</td><td>${num(row.ca)}</td><td>${num(row.groups)}</td><td>${esc(when(row.lastAt))}</td>
    </tr>`,
  );
}

async function load() {
  const params = new URLSearchParams({ range: state.range, source: state.source });
  if (state.group) params.set("group", state.group);
  if (state.sender) params.set("sender", state.sender);
  const res = await fetch(`/api/stats?${params}`, { cache: "no-store" });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
  render(data);
}

function reload() {
  load().catch((error) => {
    $("#st-kpis").innerHTML = `<p class="gm-muted">读取失败：${esc(error.message)}</p>`;
  });
}

document.addEventListener("click", (event) => {
  const range = event.target.closest("[data-range]");
  const source = event.target.closest("[data-source]");
  const group = event.target.closest("[data-group]");
  const sender = event.target.closest("[data-sender]");
  const clear = event.target.closest("[data-clear]");
  if (range) {
    state.range = range.dataset.range;
    localStorage.setItem("wf-stats-range", state.range);
  } else if (source) {
    state.source = source.dataset.source;
  } else if (group) {
    state.group = group.dataset.group;
    state.groupName = group.dataset.name;
  } else if (sender) {
    state.sender = sender.dataset.sender;
    state.senderName = sender.dataset.name;
  } else if (clear) {
    state[clear.dataset.clear] = "";
    state[`${clear.dataset.clear}Name`] = "";
  } else {
    return;
  }
  reload();
});

reload();
setInterval(() => {
  if (!document.hidden) reload();
}, 60000);
