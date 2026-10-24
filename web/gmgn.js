"use strict";

// GMGN 用量页：每 2 秒读一次 /api/gmgn-stats，页面在后台时暂停。

const NAMES = {
  "/v1/market/search": "搜索 search",
  "/v1/market/token_kline": "K 线 token_kline",
  "/v1/token/info": "代币信息 token/info",
  "/v1/token/security": "安全检测 token/security",
  "/v1/token/pool": "池子 token/pool",
  "/v1/token/holders": "持仓 token/holders",
  "/v1/token/traders": "交易者 token/traders",
};
const OUTCOME_TEXT = {
  ok: "成功",
  biz: "业务错误",
  limited: "429 限频",
  auth: "鉴权失败",
  error: "HTTP 错误",
  exception: "网络异常",
  skipped: "冷却跳过",
};

const state = { win: "5m", data: null, timer: 0, hist: "24h", histData: null, histAt: 0 };

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

function pct(value) {
  return `${(Number(value || 0) * 100).toFixed(1)}%`;
}

function num(value, digits = 2) {
  const n = Number(value || 0);
  return Number.isInteger(n) ? String(n) : n.toFixed(digits);
}

function clock(sec) {
  const d = new Date(sec * 1000);
  return d.toLocaleTimeString("zh-CN", { hour12: false });
}

function duration(sec) {
  const s = Math.floor(sec);
  if (s < 60) return `${s} 秒`;
  if (s < 3600) return `${Math.floor(s / 60)} 分 ${s % 60} 秒`;
  return `${Math.floor(s / 3600)} 小时 ${Math.floor((s % 3600) / 60)} 分`;
}

function level(ratio) {
  if (ratio >= 1) return "bad";
  if (ratio >= 0.8) return "warn";
  return "";
}

function kpi(label, value, sub, ratio) {
  const tone = ratio === undefined ? "" : level(ratio);
  const bar = ratio === undefined
    ? ""
    : `<div class="gm-meter" role="meter" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.round(Math.min(1, ratio) * 100)}" aria-label="${esc(label)}"><i style="width:${Math.min(100, ratio * 100).toFixed(1)}%"></i></div>`;
  return `<div class="gm-kpi ${tone}"><span>${esc(label)}</span><b>${value}</b>${sub ? `<small>${sub}</small>` : ""}${bar}</div>`;
}

function renderHead(d) {
  const c = d.config;
  document.querySelector("#gm-config").textContent =
    `套餐每秒权重 ${c.limit} · 本地按 ${c.window} 秒窗口记账（上限约 ${num(c.safeCap)} 权重/秒）· 后台每窗口给实时留 ${c.reserve}（后台上限约 ${num(c.backgroundCap)} 权重/秒）· 已运行 ${duration(d.uptime)}`;
  const five = d.windows["5m"];
  const badge = document.querySelector("#gm-status");
  let tone = "ok";
  let text = "安全";
  if (!d.hasKey) {
    tone = "";
    text = "未配置 Key";
  } else if (five.outcomes.limited > 0) {
    tone = "bad";
    text = `5 分钟内 429 × ${five.outcomes.limited}`;
  } else if (five.peak / c.limit >= 0.8) {
    tone = "warn";
    text = "峰值接近上限";
  }
  badge.className = `gm-badge ${tone}`;
  badge.textContent = text;
}

function renderKpis(d) {
  const c = d.config;
  const one = d.windows["1m"];
  const five = d.windows["5m"];
  const hour = d.windows["1h"];
  const cd = d.current.cooldowns || {};
  const cooling = [cd.background > 0 ? `后台 ${cd.background}s` : "", cd.auth > 0 ? `鉴权 ${cd.auth}s` : ""].filter(Boolean).join(" · ");
  const minLeft = d.current.minRemaining5m;
  document.querySelector("#gm-kpis").innerHTML = [
    kpi("当前窗口已用", `${d.current.windowWeight} / ${c.limit}`, `最近 ${c.window} 秒内`, d.current.windowWeight / c.limit),
    kpi("最近 1 分钟", `${num(one.weightPerSec)} <small style="display:inline">权重/秒</small>`, `${one.calls} 次请求 · 占套餐 ${pct(one.usage)}`, one.usage),
    kpi("5 分钟峰值（任意 1 秒）", `${num(five.peak, 0)} / ${c.limit}`, "按服务端口径：任意 1 秒内最多发出的权重", five.peak / c.limit),
    kpi("最近 5 分钟 429", `<span class="${five.outcomes.limited ? "num-bad" : ""}">${five.outcomes.limited}</span>`, `失败 ${five.outcomes.error + five.outcomes.exception + five.outcomes.auth} · 冷却跳过 ${five.outcomes.skipped}`),
    kpi("服务端剩余（5 分钟最低）", minLeft === null || minLeft === undefined ? "—" : `${minLeft} / ${c.limit}`, `最近一次：${d.current.lastRemaining ?? "—"}`, minLeft === null || minLeft === undefined ? undefined : 1 - minLeft / c.limit),
    kpi("1 小时成功率", pct(hour.successRate), `${hour.calls} 次 · ${hour.weight} 权重 · 平均 ${num(hour.weightPerSec)} 权重/秒`),
    kpi("冷却状态", cooling || "无", cooling ? "冷却中后台不发请求" : "后台与实时都可请求"),
  ].join("");
}

function renderChart(d) {
  const c = d.config;
  const box = document.querySelector("#gm-chart");
  const rows = d.timeline;
  const width = 960;
  const height = 240;
  const pad = { l: 34, r: 8, t: 10, b: 22 };
  const plotW = width - pad.l - pad.r;
  const plotH = height - pad.t - pad.b;
  const perSec = (w) => w / c.bucket;
  const peakBar = Math.max(...rows.map((r) => perSec(r.realtime + r.background)), 0);
  const top = Math.max(c.limit * 1.15, peakBar * 1.1);
  const y = (v) => pad.t + plotH - (v / top) * plotH;
  const bw = plotW / rows.length;
  const bars = rows.map((r, i) => {
    const x = pad.l + i * bw + 1;
    const rt = perSec(r.realtime);
    const bg = perSec(r.background);
    const yBg = y(bg);
    const yRt = y(bg + rt);
    const marks = [];
    if (r.limited) marks.push(`<circle cx="${x + bw / 2 - 1}" cy="${pad.t + 6}" r="4" fill="var(--bad)"><title>${clock(r.at)} 起 429 × ${r.limited}</title></circle>`);
    if (r.failed) marks.push(`<circle cx="${x + bw / 2 - 1}" cy="${pad.t + 16}" r="3.5" fill="var(--warn)"><title>${clock(r.at)} 起失败 × ${r.failed}</title></circle>`);
    return `<g><title>${clock(r.at)}：实时 ${num(rt)} · 后台 ${num(bg)} 权重/秒</title>
      <rect x="${x}" y="${yBg}" width="${Math.max(1, bw - 2)}" height="${pad.t + plotH - yBg}" fill="var(--bg-bar)" rx="1.5"/>
      <rect x="${x}" y="${yRt}" width="${Math.max(1, bw - 2)}" height="${yBg - yRt}" fill="var(--rt)" rx="1.5"/>
      ${marks.join("")}</g>`;
  });
  const line = (v, color, label) =>
    `<line x1="${pad.l}" x2="${width - pad.r}" y1="${y(v)}" y2="${y(v)}" stroke="${color}" stroke-width="1.5" stroke-dasharray="5 4"><title>${label} ${num(v)} 权重/秒</title></line>`;
  const ticks = [0, 1, 2, 3, 4, 5].filter((v) => v <= top).map((v) =>
    `<line class="grid" x1="${pad.l}" x2="${width - pad.r}" y1="${y(v)}" y2="${y(v)}"/><text class="axis" x="${pad.l - 6}" y="${y(v) + 4}" text-anchor="end">${v}</text>`);
  const xLabels = [0, 0.25, 0.5, 0.75].map((f) => {
    const i = Math.floor(f * rows.length);
    return `<text class="axis" x="${pad.l + i * bw}" y="${height - 6}">${clock(rows[i].at)}</text>`;
  });
  xLabels.push(`<text class="axis" x="${width - pad.r}" y="${height - 6}" text-anchor="end">现在</text>`);
  box.innerHTML = `<svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" role="img" aria-label="最近 10 分钟 GMGN 权重每秒，峰值 ${num(peakBar)}，套餐上限 ${c.limit}">
    ${ticks.join("")}
    ${bars.join("")}
    ${line(c.limit, "var(--bad)", "套餐上限")}
    ${line(c.safeCap, "var(--warn)", "本地上限")}
    ${line(c.backgroundCap, "#7a5af8", "后台上限")}
    ${xLabels.join("")}
  </svg>`;
}

function renderPaths(d) {
  const w = d.windows[state.win];
  const table = document.querySelector("#gm-paths");
  for (const btn of document.querySelectorAll("[data-win]")) {
    const on = btn.dataset.win === state.win;
    btn.classList.toggle("on", on);
    btn.setAttribute("aria-selected", String(on));
  }
  if (!w.paths.length) {
    table.innerHTML = `<tbody><tr><td class="l">这段时间没有请求 GMGN。</td></tr></tbody>`;
    return;
  }
  const head = `<thead><tr><th scope="col">接口</th><th scope="col">请求</th><th scope="col">权重</th><th scope="col">权重占比</th><th scope="col">实时 / 后台</th><th scope="col">成功</th><th scope="col">业务错</th><th scope="col">429</th><th scope="col">失败</th><th scope="col">跳过</th><th scope="col">平均 / P95 耗时</th><th scope="col" title="等限频窗口腾出额度：后台要给实时留 2，所以每 1.2 秒最多 1 条 K 线，这是正常节流">平均限速等待</th><th scope="col" title="实时解析在跑时后台主动让开的时间">平均让路实时</th></tr></thead>`;
  const body = w.paths.map((r) => {
    const failed = r.error + r.exception + r.auth;
    return `<tr>
      <td class="gm-path">${esc(NAMES[r.path] || r.path)}<small>${esc(r.path)}</small></td>
      <td>${r.calls}</td>
      <td>${r.weight}</td>
      <td><span class="gm-share"><i style="width:${(r.share * 100).toFixed(1)}%"></i></span>${pct(r.share)}</td>
      <td>${r.realtime} / ${r.background}</td>
      <td>${r.ok}</td>
      <td>${r.biz}</td>
      <td class="${r.limited ? "num-bad" : ""}">${r.limited}</td>
      <td class="${failed ? "num-bad" : ""}">${failed}</td>
      <td>${r.skipped}</td>
      <td>${num(r.avgMs, 0)} / ${num(r.p95Ms, 0)} ms</td>
      <td>${num(r.avgWaitMs, 0)} ms</td>
      <td>${num(r.avgYieldMs, 0)} ms</td>
    </tr>`;
  }).join("");
  const sum = `<tfoot><tr><th scope="row">合计</th><td>${w.calls}</td><td>${w.weight}</td><td>${num(w.weightPerSec)} 权重/秒</td><td colspan="9" class="l">占套餐 ${pct(w.usage)} · 任意 1 秒峰值 ${num(w.peak, 0)} / ${d.config.limit}</td></tr></tfoot>`;
  table.innerHTML = head + `<tbody>${body}</tbody>` + sum;
}

function renderRecent(d) {
  const t = d.totals;
  document.querySelector("#gm-totals").textContent =
    `本次启动以来 ${t.calls} 次请求 · ${t.weight} 权重 · 429 ${t.outcomes.limited} 次 · 失败 ${t.outcomes.error + t.outcomes.exception + t.outcomes.auth} 次`;
  const table = document.querySelector("#gm-recent");
  if (!d.recent.length) {
    table.innerHTML = `<tbody><tr><td class="l">还没有请求记录。</td></tr></tbody>`;
    return;
  }
  const head = `<thead><tr><th scope="col">时间</th><th scope="col">接口</th><th scope="col">来源</th><th scope="col">结果</th><th scope="col">HTTP</th><th scope="col">权重</th><th scope="col">耗时</th><th scope="col" title="等限频窗口腾出额度（正常节流）">限速等待</th><th scope="col" title="实时解析在跑时后台主动让开的时间">让路实时</th><th scope="col">服务端剩余</th></tr></thead>`;
  const body = d.recent.map((e) => `<tr>
    <td class="l">${clock(e.at)}</td>
    <td class="l">${esc(NAMES[e.path] || e.path)}</td>
    <td><span class="tag ${e.kind === "realtime" ? "rt" : "bg"}">${e.kind === "manual" ? "手动刷新" : e.kind === "background" ? "后台" : "实时"}</span></td>
    <td><span class="tag ${esc(e.outcome)}">${esc(OUTCOME_TEXT[e.outcome] || e.outcome)}</span></td>
    <td>${e.status || "—"}</td>
    <td>${e.weight}</td>
    <td>${e.latencyMs ? `${num(e.latencyMs, 0)} ms` : "—"}</td>
    <td>${num(e.waitMs, 0)} ms</td>
    <td>${e.yieldMs ? `${num(e.yieldMs, 0)} ms` : "—"}</td>
    <td>${e.remaining ?? "—"}</td>
  </tr>`).join("");
  table.innerHTML = head + `<tbody>${body}</tbody>`;
}

function slotLabel(sec, bucket) {
  const d = new Date(sec * 1000);
  if (bucket >= 86400) return `${d.getMonth() + 1}/${d.getDate()}`;
  if (bucket >= 3600) return `${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, "0")}:00`;
  return clock(sec).slice(0, 5);
}

function renderHistory() {
  const h = state.histData;
  if (!h) return;
  for (const btn of document.querySelectorAll("[data-hist]")) {
    const on = btn.dataset.hist === state.hist;
    btn.classList.toggle("on", on);
    btn.setAttribute("aria-selected", String(on));
  }
  const limit = state.data?.config?.limit || 5;
  const t = h.totals;
  document.querySelector("#gm-hist-kpis").innerHTML = [
    kpi("请求", String(t.calls), `${t.weight} 权重`),
    kpi("平均", `${num(t.weightPerSec, 3)} <small style="display:inline">权重/秒</small>`, `占套餐 ${pct(t.weightPerSec / limit)}`, t.weightPerSec / limit),
    kpi("最高峰值（任意 1 秒）", `${t.peak} / ${limit}`, "整个范围内", t.peak / limit),
    kpi("429", `<span class="${t.limited ? "num-bad" : ""}">${t.limited}</span>`, `失败 ${t.failed}`),
  ].join("");
  const rows = h.series;
  const width = 960;
  const height = 200;
  const pad = { l: 34, r: 8, t: 10, b: 22 };
  const plotW = width - pad.l - pad.r;
  const plotH = height - pad.t - pad.b;
  const avg = (w) => w / h.bucket;
  const top = Math.max(limit * 1.15, ...rows.map((r) => r.peak * 1.05));
  const y = (v) => pad.t + plotH - (v / top) * plotH;
  const bw = plotW / rows.length;
  const bars = rows.map((r, i) => {
    const x = pad.l + i * bw;
    const bg = avg(r.bgWeight);
    const all = avg(r.weight);
    const peakY = y(r.peak);
    const tip = `${slotLabel(r.at, h.bucket)}：平均 ${num(all, 3)} 权重/秒，峰值 ${r.peak}，${r.calls} 次请求${r.limited ? `，429 × ${r.limited}` : ""}${r.failed ? `，失败 × ${r.failed}` : ""}`;
    return `<g><title>${esc(tip)}</title>
      <rect x="${x}" y="${pad.t}" width="${bw}" height="${plotH}" fill="transparent"/>
      <rect x="${x + 0.5}" y="${y(bg)}" width="${Math.max(1, bw - 1)}" height="${pad.t + plotH - y(bg)}" fill="var(--bg-bar)"/>
      <rect x="${x + 0.5}" y="${y(all)}" width="${Math.max(1, bw - 1)}" height="${y(bg) - y(all)}" fill="var(--rt)"/>
      ${r.peak ? `<line x1="${x}" x2="${x + bw}" y1="${peakY}" y2="${peakY}" stroke="var(--bad)" stroke-width="1.5"/>` : ""}
      ${r.limited ? `<circle cx="${x + bw / 2}" cy="${pad.t + 5}" r="3.5" fill="var(--bad)"/>` : ""}</g>`;
  });
  const ticks = [0, 1, 2, 3, 4, 5].filter((v) => v <= top).map((v) =>
    `<line class="grid" x1="${pad.l}" x2="${width - pad.r}" y1="${y(v)}" y2="${y(v)}"/><text class="axis" x="${pad.l - 6}" y="${y(v) + 4}" text-anchor="end">${v}</text>`);
  const xs = [0, 0.25, 0.5, 0.75].map((f) => {
    const i = Math.floor(f * rows.length);
    return `<text class="axis" x="${pad.l + i * bw}" y="${height - 6}">${slotLabel(rows[i].at, h.bucket)}</text>`;
  });
  document.querySelector("#gm-hist").innerHTML = `<svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" role="img" aria-label="历史 GMGN 用量，最高峰值 ${t.peak}，套餐上限 ${limit}">
    ${ticks.join("")}${bars.join("")}
    <line x1="${pad.l}" x2="${width - pad.r}" y1="${y(limit)}" y2="${y(limit)}" stroke="var(--bad)" stroke-width="1.5" stroke-dasharray="5 4"><title>套餐上限 ${limit}</title></line>
    ${xs.join("")}</svg>`;
  const table = document.querySelector("#gm-hist-paths");
  if (!h.paths.length) {
    table.innerHTML = `<tbody><tr><td class="l">这段时间没有记录。</td></tr></tbody>`;
    return;
  }
  table.innerHTML = `<thead><tr><th scope="col">接口</th><th scope="col">请求</th><th scope="col">权重</th><th scope="col">占比</th><th scope="col">成功</th><th scope="col">业务错</th><th scope="col">429</th><th scope="col">失败</th><th scope="col">跳过</th><th scope="col">平均 / 最长耗时</th></tr></thead><tbody>${h.paths
    .map((r) => `<tr><td class="gm-path">${esc(NAMES[r.path] || r.path)}<small>${esc(r.path)}</small></td><td>${r.calls}</td><td>${r.weight}</td>
      <td><span class="gm-share"><i style="width:${(r.share * 100).toFixed(1)}%"></i></span>${pct(r.share)}</td><td>${r.ok}</td><td>${r.biz}</td>
      <td class="${r.limited ? "num-bad" : ""}">${r.limited}</td><td class="${r.failed ? "num-bad" : ""}">${r.failed}</td><td>${r.skipped}</td><td>${num(r.avgMs, 0)} / ${num(r.maxMs, 0)} ms</td></tr>`)
    .join("")}</tbody>`;
}

async function loadHistory(force = false) {
  // 历史按分钟汇总，30 秒刷一次足够。
  if (!force && Date.now() - state.histAt < 30000) return;
  state.histAt = Date.now();
  try {
    const res = await fetch(`/api/gmgn-history?range=${encodeURIComponent(state.hist)}`, { cache: "no-store" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    state.histData = await res.json();
    renderHistory();
  } catch (error) {
    document.querySelector("#gm-hist").textContent = `历史读取失败：${error.message}`;
  }
}

function render() {
  const d = state.data;
  if (!d) return;
  renderHead(d);
  renderKpis(d);
  renderChart(d);
  renderPaths(d);
  renderRecent(d);
}

async function refresh() {
  try {
    const res = await fetch("/api/gmgn-stats", { cache: "no-store" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    state.data = await res.json();
    render();
    loadHistory();
  } catch (error) {
    const badge = document.querySelector("#gm-status");
    badge.className = "gm-badge bad";
    badge.textContent = `读取失败：${error.message}`;
  }
}

function schedule() {
  clearInterval(state.timer);
  if (document.querySelector("#gm-auto").checked && !document.hidden) {
    state.timer = setInterval(refresh, 2000);
  }
}

document.addEventListener("click", (event) => {
  const hist = event.target.closest("[data-hist]");
  if (hist) {
    state.hist = hist.dataset.hist;
    loadHistory(true);
    return;
  }
  const btn = event.target.closest("[data-win]");
  if (!btn) return;
  state.win = btn.dataset.win;
  render();
});
document.querySelector("#gm-auto").addEventListener("change", schedule);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) refresh();
  schedule();
});

refresh();
schedule();
