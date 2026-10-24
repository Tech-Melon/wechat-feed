"use strict";

// 数据管理页：按月份或自定义日期删除排行榜 / GMGN 调用记录，先预览再确认。

const TARGET_TEXT = { board: "排行榜喊单", gmgn: "GMGN 调用记录", all: "排行榜与 GMGN 调用记录" };
const state = { pending: null };

function $(sel) {
  return document.querySelector(sel);
}

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

function day(sec) {
  if (!sec) return "—";
  const d = new Date(sec * 1000);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

function size(bytes) {
  const n = Number(bytes || 0);
  if (n >= 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  return `${Math.max(1, Math.round(n / 1024))} KB`;
}

function toInput(date) {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
}

// 本地日期的 00:00 → 秒；结束日期含当天，所以取次日 00:00。
function startOf(value) {
  const [y, m, d] = value.split("-").map(Number);
  return Math.floor(new Date(y, m - 1, d).getTime() / 1000);
}

function endOf(value) {
  const [y, m, d] = value.split("-").map(Number);
  return Math.floor(new Date(y, m - 1, d + 1).getTime() / 1000);
}

function monthRange(month) {
  const [y, m] = month.split("-").map(Number);
  return [Math.floor(new Date(y, m - 1, 1).getTime() / 1000), Math.floor(new Date(y, m, 1).getTime() / 1000)];
}

async function post(url, body) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
  return data;
}

function countsText(out) {
  const parts = [];
  if (out.board) parts.push(`排行榜：喊单 ${out.board.calls} 条、群首喊 ${out.board.groupFirsts} 条、随之清空的代币 ${out.board.tokens} 个`);
  if (out.gmgn) parts.push(`GMGN：明细 ${out.gmgn.rawRows} 条、分钟汇总 ${out.gmgn.minuteRows} 条`);
  return parts.join("；");
}

async function preview(target, start, end, label) {
  const box = $("#dm-preview");
  box.textContent = "正在统计…";
  try {
    const out = await post("/api/data/purge", { target, start, end, dryRun: true });
    state.pending = { target, start, end, label };
    const empty = (!out.board || out.board.calls + out.board.groupFirsts === 0) && (!out.gmgn || out.gmgn.rawRows + out.gmgn.minuteRows === 0);
    box.innerHTML = empty
      ? `${esc(label)}：没有可删除的${esc(TARGET_TEXT[target])}。`
      : `<p>将删除 <b>${esc(label)}</b> 的${esc(TARGET_TEXT[target])}：${esc(countsText(out))}。</p>
         <div class="dm-actions"><button type="button" class="dm-btn danger" data-confirm>确认删除</button><button type="button" class="dm-btn" data-cancel>取消</button></div>`;
  } catch (error) {
    box.textContent = `预览失败：${error.message}`;
  }
}

async function confirmPurge() {
  const job = state.pending;
  if (!job) return;
  if (!window.confirm(`删除 ${job.label} 的${TARGET_TEXT[job.target]}？此操作不可恢复。`)) return;
  const box = $("#dm-preview");
  box.textContent = "正在删除…";
  try {
    const out = await post("/api/data/purge", { target: job.target, start: job.start, end: job.end, dryRun: false });
    box.textContent = `已删除：${countsText(out)}。可以在下方压缩数据库回收空间。`;
    state.pending = null;
    await load();
  } catch (error) {
    box.textContent = `删除失败：${error.message}`;
  }
}

function monthRows(rows, target, cols) {
  if (!rows.length) return `<tbody><tr><td class="l">还没有数据。</td></tr></tbody>`;
  const head = `<thead><tr><th scope="col">月份</th>${cols.map((c) => `<th scope="col">${c[0]}</th>`).join("")}<th scope="col"></th></tr></thead>`;
  const body = rows
    .map((r) => `<tr><td class="l">${esc(r.month)}</td>${cols.map((c) => `<td>${esc(r[c[1]])}</td>`).join("")}
      <td><button type="button" class="dm-btn small" data-month="${esc(r.month)}" data-target="${target}">删除这个月</button></td></tr>`)
    .join("");
  return head + `<tbody>${body}</tbody>`;
}

async function load() {
  const res = await fetch("/api/data", { cache: "no-store" });
  const data = await res.json();
  const b = data.board;
  const g = data.gmgn;
  $("#dm-board-meta").textContent = `${b.calls} 条喊单 · ${b.tokens} 个代币 · ${day(b.oldest)} ~ ${day(b.newest)} · 文件 ${size(b.bytes)} · 保留 ${b.keepDays} 天`;
  $("#dm-board").innerHTML = monthRows(b.months, "board", [["喊单", "calls"], ["代币", "tokens"], ["喊单人", "users"]]);
  $("#dm-purged").textContent = b.purged.length
    ? `手动删过的时间段（启动回灌消息时不会再写回）：${b.purged.map((p) => `${day(p.start)} ~ ${day(p.end - 1)}`).join("、")}`
    : "";
  $("#dm-gmgn-meta").textContent = `明细 ${g.rawRows} 条（自 ${day(g.rawOldest)}）· 分钟汇总 ${g.minuteRows} 条（${day(g.oldest)} ~ ${day(g.newest)}）· 文件 ${size(g.bytes)}`;
  $("#dm-gmgn").innerHTML = monthRows(g.months, "gmgn", [["请求", "calls"], ["权重", "weight"]]);
}

function applyPreset(value) {
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const shift = (days) => new Date(today.getFullYear(), today.getMonth(), today.getDate() + days);
  const ranges = {
    older30: [new Date(2000, 0, 1), shift(-31)],
    older90: [new Date(2000, 0, 1), shift(-91)],
    older180: [new Date(2000, 0, 1), shift(-181)],
    last1: [today, today],
    last7: [shift(-6), today],
    everything: [new Date(2000, 0, 1), today],
  };
  const r = ranges[value];
  if (!r) return;
  $("#dm-start").value = toInput(r[0]);
  $("#dm-end").value = toInput(r[1]);
}

$("#dm-preset").addEventListener("change", (event) => applyPreset(event.target.value));
$("#dm-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const s = $("#dm-start").value;
  const e = $("#dm-end").value;
  if (!s || !e) return;
  if (startOf(s) >= endOf(e)) {
    $("#dm-preview").textContent = "开始日期不能晚于结束日期。";
    return;
  }
  preview($("#dm-target").value, startOf(s), endOf(e), `${s} ~ ${e}`);
});
document.addEventListener("click", (event) => {
  const month = event.target.closest("[data-month]");
  if (month) {
    const [start, end] = monthRange(month.dataset.month);
    preview(month.dataset.target, start, end, month.dataset.month);
    $("#dm-preview").scrollIntoView({ behavior: "smooth", block: "center" });
    return;
  }
  if (event.target.closest("[data-confirm]")) {
    confirmPurge();
    return;
  }
  if (event.target.closest("[data-cancel]")) {
    state.pending = null;
    $("#dm-preview").textContent = "";
  }
});
$("#dm-vacuum").addEventListener("click", async () => {
  const out = $("#dm-vacuum-out");
  out.textContent = "正在压缩…";
  try {
    const r = await post("/api/data/vacuum", {});
    out.textContent = `完成：排行榜 ${size(r.boardBytes)}，GMGN 记录 ${size(r.gmgnBytes)}。`;
    await load();
  } catch (error) {
    out.textContent = `压缩失败：${error.message}`;
  }
});

applyPreset("older90");
$("#dm-preset").value = "older90";
load().catch((error) => {
  $("#dm-board-meta").textContent = `读取失败：${error.message}`;
});
