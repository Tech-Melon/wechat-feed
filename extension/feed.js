(function (root) {
  function escapeText(value) {
    return String(value ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function escapeRegExp(value) {
    return String(value).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  }

  const CA_PATTERN = String.raw`0x[a-fA-F0-9]{40,64}|\bT[1-9A-HJ-NP-Za-km-z]{33}\b|\b(?:EQ|UQ)[A-Za-z0-9_-]{46}\b|\b[1-9A-HJ-NP-Za-km-z]{32,44}pump\b`;
  let apiBase = "";
  function assetUrl(path) {
    if (!path) return "";
    if (/^https?:/i.test(path)) return path;
    return `${apiBase}${path}`;
  }
  function setApiBase(value) {
    apiBase = String(value || "").replace(/\/$/, "");
  }
  const EMOTE_SLUGS = {
    "微笑": "0",
    "撇嘴": "1",
    "色": "2",
    "发呆": "3",
    "得意": "4",
    "流泪": "5",
    "害羞": "6",
    "闭嘴": "7",
    "睡": "8",
    "大哭": "9",
    "尴尬": "10",
    "发怒": "11",
    "调皮": "12",
    "呲牙": "13",
    "惊讶": "14",
    "难过": "15",
    "酷": "16",
    "冷汗": "17",
    "抓狂": "18",
    "吐": "19",
    "偷笑": "20",
    "愉快": "21",
    "可爱": "21",
    "白眼": "22",
    "傲慢": "23",
    "饥饿": "24",
    "困": "25",
    "惊恐": "26",
    "流汗": "27",
    "憨笑": "28",
    "大笑": "28",
    "悠闲": "29",
    "奋斗": "30",
    "咒骂": "31",
    "疑问": "32",
    "嘘": "33",
    "晕": "34",
    "折磨": "35",
    "衰": "36",
    "骷髅": "37",
    "敲打": "38",
    "再见": "39",
    "擦汗": "40",
    "抠鼻": "41",
    "鼓掌": "42",
    "糗大了": "43",
    "坏笑": "44",
    "左哼哼": "45",
    "右哼哼": "46",
    "哈欠": "47",
    "鄙视": "48",
    "委屈": "49",
    "快哭了": "50",
    "阴险": "51",
    "亲亲": "52",
    "吓": "53",
    "可怜": "54",
    "菜刀": "55",
    "西瓜": "56",
    "啤酒": "57",
    "篮球": "58",
    "乒乓": "59",
    "咖啡": "60",
    "饭": "61",
    "猪头": "62",
    "玫瑰": "63",
    "凋谢": "64",
    "嘴唇": "65",
    "爱心": "66",
    "心碎": "67",
    "蛋糕": "68",
    "闪电": "69",
    "炸弹": "70",
    "刀": "71",
    "足球": "72",
    "瓢虫": "73",
    "便便": "74",
    "月亮": "75",
    "太阳": "76",
    "礼物": "77",
    "拥抱": "78",
    "强": "79",
    "弱": "80",
    "握手": "81",
    "胜利": "82",
    "抱拳": "83",
    "勾引": "84",
    "拳头": "85",
    "差劲": "86",
    "爱你": "87",
    "NO": "88",
    "OK": "89",
    "爱情": "90",
    "飞吻": "91",
    "跳跳": "92",
    "发抖": "93",
    "怄火": "94",
    "转圈": "95",
    "磕头": "96",
    "回头": "97",
    "跳绳": "98",
    "投降": "99",
    "激动": "100",
    "乱舞": "101",
    "献吻": "102",
    "左太极": "103",
    "右太极": "104",
    "囧": "jiong",
    "生病": "sb",
    "脸红": "lh",
    "破涕为笑": "ptwx",
    "恐惧": "kj",
    "失望": "sw",
    "无语": "wy",
    "嘿哈": "hh",
    "捂脸": "wl",
    "奸笑": "jx",
    "机智": "jz",
    "皱眉": "zm2",
    "耶": "ye",
    "吃瓜": "cg",
    "加油": "jy2",
    "汗": "han",
    "天啊": "ta",
    "Emm": "emm",
    "社会社会": "shsh",
    "旺柴": "doge",
    "好的": "hd",
    "打脸": "dl",
    "哇": "wa",
    "翻白眼": "fby",
    "666": "666",
    "让我看看": "rwkk",
    "叹气": "tq",
    "苦涩": "ks",
    "裂开": "lk",
    "合十": "hs",
    "庆祝": "qz",
    "红包": "hb",
    "發": "fa",
    "福": "fu",
    "烟花": "yh",
    "爆竹": "bz2",
    "加油加油": "jyjy",
  };

  const CHAIN_ALIAS = {
    bnb: "bsc", binance: "bsc", "binance-smart-chain": "bsc",
    ethereum: "eth", solana: "sol", matic: "polygon", trx: "tron",
    avalanche: "avax", optimism: "op", arbitrum: "arb", "x-layer": "xlayer",
  };
  const CHAIN_UI = {
    sol: { label: "Solana", cls: "is-sol" },
    base: { label: "Base", cls: "is-base" },
    bsc: { label: "BNB", cls: "is-bsc" },
    eth: { label: "Ethereum", cls: "is-eth" },
    robinhood: { label: "Robinhood", cls: "is-robinhood" },
    arc: { label: "Arc", cls: "is-arc" },
    tron: { label: "Tron", cls: "is-tron" },
    ton: { label: "TON", cls: "is-ton" },
    sui: { label: "Sui", cls: "is-sui" },
    polygon: { label: "Polygon", cls: "is-polygon" },
    arb: { label: "Arbitrum", cls: "is-arb" },
    avax: { label: "Avalanche", cls: "is-avax" },
    op: { label: "Optimism", cls: "is-op" },
    blast: { label: "Blast", cls: "is-blast" },
    monad: { label: "Monad", cls: "is-monad" },
    linea: { label: "Linea", cls: "is-linea" },
    scroll: { label: "Scroll", cls: "is-scroll" },
    zksync: { label: "zkSync", cls: "is-zksync" },
    sei: { label: "Sei", cls: "is-sei" },
    mantle: { label: "Mantle", cls: "is-mantle" },
    ape: { label: "Ape", cls: "is-ape" },
    hyperevm: { label: "HyperEVM", cls: "is-hyperevm" },
    plasma: { label: "Plasma", cls: "is-plasma" },
    xlayer: { label: "X Layer", cls: "is-xlayer" },
  };

  function chainKey(value) {
    const raw = String(value || "").trim().toLowerCase();
    return CHAIN_ALIAS[raw] || raw;
  }

  function chainMeta(value) {
    const key = chainKey(value);
    return key ? CHAIN_UI[key] || null : null;
  }

  function chainIcon(value) {
    const key = chainKey(value);
    if (!CHAIN_UI[key]) return "";
    const src = escapeText(assetUrl(`/api/chainicon?n=${encodeURIComponent(key)}`));
    return `<img class="wf-chain-ico" src="${src}" alt="" />`;
  }

  function chainBadge(value) {
    const key = chainKey(value);
    if (!key) return "";
    const meta = chainMeta(key);
    const label = escapeText(meta ? meta.label : String(value).toUpperCase());
    const cls = meta ? meta.cls : "is-unknown";
    return `<span class="wf-chain ${cls}" title="${label}">${chainIcon(key)}${label}</span>`;
  }

  function chainMap(card) {
    const map = Object.create(null);
    const put = (addr, chain) => {
      const key = String(addr || "").toLowerCase();
      if (!key || !chain || map[key]) return;
      map[key] = chain;
    };
    for (const token of card.tokenCards || []) put(token.address, token.chain);
    for (const item of card.cas || []) put(item.address, item.chain);
    return map;
  }

  function caChip(address, chain) {
    const text = escapeText(address);
    return `<span class="wf-ca">${chainIcon(chain)}<code>${text}</code><button type="button" class="wf-ca-copy" data-copy="${text}">复制</button></span>`;
  }

  function knownAddresses(card) {
    return [
      ...(card.tokenCards || []).map((token) => token.address),
      ...(card.cas || []).map((item) => item.address),
      ...(card.addresses || []),
    ];
  }

  // 规避分隔符只在 URL 片段里还原成 `.` / `:` / `/`，避免「这一点」误伤。
  const URL_SOFT_CHARS = String.raw`\u00B7\u2024\u2027\u30FB\uFF0E\u3002点點丶‧`;
  // [.] (.) （.） 【.】 这类括号点，括号里也可以是中文点。
  const URL_BRACKET_DOT = String.raw`[\[(（【]\s*[.${URL_SOFT_CHARS}]\s*[\])）】]`;
  // 中文点 / 括号点：明显是有意规避，裸域名带空格时只认这种。
  const URL_SOFT_DOT = new RegExp(String.raw`[${URL_SOFT_CHARS}]|${URL_BRACKET_DOT}`);
  const URL_DOT = String.raw`(?:[.${URL_SOFT_CHARS}]|${URL_BRACKET_DOT})`;
  const URL_GAP = String.raw`[\s\u00A0\u200B-\u200D\uFEFF\u3000]*`;
  // 只用于没写协议头的裸域名；写了 http(s) 的任意后缀都认。
  const URL_TLD = String.raw`(?:finance|online|website|exchange|market|space|store|cloud|tech|info|site|shop|blog|wiki|live|news|link|page|meme|cash|swap|nft|dao|fun|app|xyz|pro|dev|net|org|com|top|vip|club|biz|icu|wang|xin|gov|edu|mil|int|near|sol|eth|ton|defi|io|ai|cc|me|co|gg|cn|hk|tv|to|fi|uk|us)`;
  const URL_SCHEME = String.raw`(?:https?|hxxps?)[:：]${URL_GAP}[/／]{0,2}`;
  const URL_BODY = String.raw`[A-Za-z0-9\-._~:/?#@!$&*+,;=%|点點。．·・丶‧／：\[\]()]*`;
  const URL_LABEL = String.raw`[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?`;
  const URL_HOST = String.raw`${URL_LABEL}(?:${URL_GAP}${URL_DOT}${URL_GAP}${URL_LABEL})*${URL_GAP}${URL_DOT}${URL_GAP}${URL_TLD}`;
  // 标准写法的域名，后缀不限（bit.ly、example.de）。
  const URL_STRICT_HOST = String.raw`${URL_LABEL}(?:\.${URL_LABEL})*\.[A-Za-z]{2,24}`;
  const URL_IP = String.raw`(?:\d{1,3}\.){3}\d{1,3}`;
  // 域名到此为止：后面不能再接字母数字，也不能是「.字母」，避免 x.company 截成 x.com。
  const URL_END = String.raw`(?![A-Za-z0-9-]|\.[A-Za-z0-9])`;
  const URL_PORT = String.raw`(?::\d{1,5})?`;
  const URL_PATH = String.raw`(?:[/／?#]${URL_BODY})?`;
  const URL_PATTERN = String.raw`(?:(?<![@A-Za-z0-9])${URL_SCHEME}${URL_GAP}(?:${URL_DOT}${URL_GAP})*(?:${URL_STRICT_HOST}|${URL_HOST}|${URL_IP}|localhost)${URL_END}${URL_PORT}${URL_PATH})|(?:(?<![@A-Za-z0-9])${URL_HOST}${URL_END}${URL_PATH})`;
  const URL_TRAIL = /[.,;:!?'"`>》」』，。、；：！？…\u00B7\u2024\u2027\u30FB\uFF0E点點丶‧]+$/;
  const URL_PAIRS = { ")": "(", "]": "[" };
  const URL_BRACKET_DOT_RE = new RegExp(URL_BRACKET_DOT, "g");

  // 全角字母数字转半角，长度不变，原文下标仍然对得上。
  function toHalfWidth(value) {
    return String(value ?? "").replace(/[\uFF10-\uFF19\uFF21-\uFF3A\uFF41-\uFF5A]/g, (ch) =>
      String.fromCharCode(ch.charCodeAt(0) - 0xfee0),
    );
  }

  function restoreHref(raw) {
    let text = toHalfWidth(raw).trim();
    if (!text) return "";
    text = text.replace(/[\u200B-\u200D\uFEFF]/g, "");
    text = text.replace(URL_BRACKET_DOT_RE, ".");
    text = text.replace(/^hxxps/i, "https").replace(/^hxxp/i, "http");
    text = text.replace(/^(https?)[:：][\s\u00A0\u3000]*[/／]*/i, "$1://");
    text = text.replace(/[\u00B7\u2024\u2027\u30FB\uFF0E\u3002点點丶‧]/g, ".");
    text = text.replace(/[\s\u00A0\u3000]*\.[\s\u00A0\u3000]*/g, ".");
    text = text.replace(/^(https?:\/\/)[\s\u00A0\u3000]+/i, "$1");
    text = text.replace(/^(https?:\/\/)\.+/i, "$1");
    text = text.replace(/／/g, "/");
    text = text.replace(/：/g, ":");
    if (!/^[a-z][a-z0-9+.-]*:/i.test(text)) text = `https://${text}`;
    const hostPart = text.replace(/^[a-z][a-z0-9+.-]*:\/\//i, "").split(/[/?#]/)[0].split("@").pop().split(":")[0];
    if (hostPart !== "localhost" && !/[A-Za-z0-9]/.test(hostPart)) return "";
    try {
      const url = new URL(text);
      if (url.protocol !== "http:" && url.protocol !== "https:") return "";
      const host = String(url.hostname || "");
      if (host !== "localhost" && !/^[a-z0-9.-]+$/i.test(host)) return "";
      if (host !== "localhost" && !host.includes(".")) return "";
      return url.href;
    } catch (error) {
      return "";
    }
  }

  // 裁掉句末标点；右括号只在没有配对左括号时才裁，保留 wiki/Foo_(bar) 这种路径。
  function trimUrlToken(raw) {
    let text = String(raw || "");
    const count = (ch) => text.split(ch).length - 1;
    for (;;) {
      const before = text;
      text = text.replace(URL_TRAIL, "").replace(/[([]+$/, "");
      const tail = text.slice(-1);
      const open = URL_PAIRS[tail];
      if (open && count(open) < count(tail)) text = text.slice(0, -1);
      if (text === before) return text;
    }
  }

  // 没写协议头、点两边又有空格时，必须是中文点 / 括号点或带路径，避免「ok . io」这种句子误伤。
  function looseUrlOk(token) {
    if (/^(?:https?|hxxps?)[:：]/i.test(token)) return true;
    const host = token.split(/[/／?#]/)[0];
    const spaced = host.replace(URL_BRACKET_DOT_RE, ".");
    if (!/[\s\u00A0\u200B-\u200D\uFEFF\u3000]/.test(spaced)) return true;
    return URL_SOFT_DOT.test(host) || host.length < token.length;
  }

  function webLink(raw, href) {
    return `<a class="wf-text-link" href="${escapeText(href)}" target="_blank" rel="noreferrer">${escapeText(raw)}</a>`;
  }

  function decoratePlain(text) {
    return escapeText(text).replace(/\n/g, "<br>").replace(/\[([^\]\[]{1,8})\]/g, (all, name) => {
      if (!Object.prototype.hasOwnProperty.call(EMOTE_SLUGS, name)) return all;
      const src = escapeText(assetUrl(`/api/wxemote?n=${encodeURIComponent(name)}`));
      return `<img class="wf-emote" data-wf-src="${src}" src="${src}" alt="${all}" title="${all}" />`;
    });
  }

  // 一次扫描切出网页和地址。URL 优先，避免路径里的 0x 把链接打断。
  function highlight(value, addresses, chains) {
    const source = String(value ?? "");
    const extra = [...new Set((addresses || []).map(String))]
      .filter((item) => item && !item.startsWith("0x"))
      .map(escapeRegExp);
    const caRe = new RegExp([...extra, CA_PATTERN].join("|"), "g");
    const urlRe = new RegExp(URL_PATTERN, "gi");
    const marks = [];
    // 在半角副本上找链接，显示仍取原文。
    for (const match of toHalfWidth(source).matchAll(urlRe)) {
      const token = trimUrlToken(match[0]);
      if (!token || !looseUrlOk(token)) continue;
      const href = restoreHref(token);
      if (!href) continue;
      const end = match.index + token.length;
      marks.push({ start: match.index, end, kind: "url", raw: source.slice(match.index, end), href });
    }
    for (const match of source.matchAll(caRe)) {
      marks.push({
        start: match.index,
        end: match.index + match[0].length,
        kind: "ca",
        raw: match[0],
      });
    }
    marks.sort((a, b) => a.start - b.start || b.end - a.end);
    let html = "";
    let last = 0;
    for (const mark of marks) {
      if (mark.start < last) continue;
      html += decoratePlain(source.slice(last, mark.start));
      if (mark.kind === "url") html += webLink(mark.raw, mark.href);
      else html += caChip(mark.raw, chains ? chains[String(mark.raw).toLowerCase()] : "");
      last = mark.end;
    }
    return html + decoratePlain(source.slice(last));
  }

  function formatTime(seconds) {
    const time = Number(seconds) || 0;
    if (!time) return "";
    const date = new Date(time * 1000);
    const pad = (value) => `${value}`.padStart(2, "0");
    const clock = `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
    const now = new Date();
    const sameDay = date.getFullYear() === now.getFullYear()
      && date.getMonth() === now.getMonth()
      && date.getDate() === now.getDate();
    if (sameDay) return clock;
    return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${clock}`;
  }

  function formatDateTime(seconds) {
    const time = Number(seconds) || 0;
    if (!time) return "";
    const date = new Date(time * 1000);
    const pad = (value) => `${value}`.padStart(2, "0");
    const clock = `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
    const md = `${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
    const now = new Date();
    if (date.getFullYear() !== now.getFullYear()) {
      return `${date.getFullYear()}-${md} ${clock}`;
    }
    return `${md} ${clock}`;
  }

  function formatFresh(seconds, nowSec) {
    const time = Number(seconds) || 0;
    if (!time) return "";
    const now = Number.isFinite(Number(nowSec)) ? Number(nowSec) : Date.now() / 1000;
    const delta = Math.floor(now - time);
    if (delta < 60) return "刚刚";
    if (delta < 10 * 60) return `${Math.floor(delta / 60)}分钟前`;
    return "";
  }

  // 标记与时间无关，文字由 tickFresh 填：卡片 HTML 不随「刚刚 → 1分钟前」变化，增量渲染才能复用节点。
  function freshHtml(seconds) {
    const time = Number(seconds) || 0;
    if (!time) return "";
    return `<span class="wf-fresh" data-fresh-at="${time}" hidden></span>`;
  }

  function tickFresh(root) {
    const nodes = [];
    const scan = (node) => {
      if (!node) return;
      if (node.querySelectorAll) nodes.push(...node.querySelectorAll("[data-fresh-at]"));
      if (node.shadowRoot) scan(node.shadowRoot);
    };
    scan(root || document);
    for (const el of nodes) {
      const label = formatFresh(Number(el.dataset.freshAt));
      // 没变就不写，避免每 15 秒让整列表样式失效。
      if (el.textContent !== label) el.textContent = label;
      if (el.hidden !== !label) el.hidden = !label;
    }
  }

  // 按 key 增量更新列表：HTML 没变的节点原样保留（已解码的图片、hover、滚动锚定都不丢），
  // 只替换变了的、插入新的、删掉没了的。返回新建的节点，调用方只需给它们补图片等。
  function syncList(container, items, keyOf, htmlOf) {
    const keys = items.map((item, index) => String(keyOf(item, index)));
    const wanted = new Set(keys);
    const existing = new Map();
    for (const node of Array.from(container.children)) {
      const key = node.__wfKey;
      if (!key || !wanted.has(key) || existing.has(key)) node.remove();
      else existing.set(key, node);
    }
    const created = [];
    let cursor = container.firstElementChild;
    items.forEach((item, index) => {
      const key = keys[index];
      const html = htmlOf(item, index);
      let node = existing.get(key);
      if (!node || node.__wfHtml !== html) {
        const tpl = document.createElement("template");
        tpl.innerHTML = html.trim();
        const next = tpl.content.firstElementChild;
        if (!next) return;
        next.__wfKey = key;
        next.__wfHtml = html;
        if (node) {
          if (cursor === node) cursor = next;
          node.replaceWith(next);
        }
        node = next;
        created.push(next);
      }
      if (node === cursor) cursor = cursor.nextElementSibling;
      else container.insertBefore(node, cursor);
    });
    return created;
  }

  function startFreshTick(root) {
    tickFresh(root);
    const host = root || document;
    if (host.__wfFreshTimer) return host.__wfFreshTimer;
    host.__wfFreshTimer = setInterval(() => tickFresh(root), 15000);
    return host.__wfFreshTimer;
  }

  function initial(name) {
    const text = String(name || "").trim();
    return text ? text.slice(0, 1) : "群";
  }

  function hue(seed) {
    let value = 0;
    const text = String(seed || "");
    for (let index = 0; index < text.length; index += 1) {
      value = (value * 33 + text.charCodeAt(index)) % 360;
    }
    return value;
  }

  function formatAbs(seconds) {
    const time = Number(seconds) || 0;
    if (!time) return "";
    const date = new Date(time * 1000);
    const pad = (value) => `${value}`.padStart(2, "0");
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
  }

  function shortAddress(address) {
    const text = String(address || "");
    if (text.length <= 14) return text;
    return `${text.slice(0, 6)}…${text.slice(-4)}`;
  }

  function formatCreated(seconds) {
    const time = Number(seconds) || 0;
    if (!time) return "";
    const date = new Date(time * 1000);
    const pad = (value) => `${value}`.padStart(2, "0");
    return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
  }

  function caRow(address, chain) {
    const full = String(address || "");
    if (!full) return "";
    const copy = escapeText(full);
    return `<div class="wf-ca-row">
      <span class="wf-ca-label">合约</span>
      ${chainIcon(chain)}
      <code class="wf-ca-addr" title="${copy}">${escapeText(shortAddress(full))}</code>
      <button type="button" class="wf-copy-btn" data-copy="${copy}">复制</button>
    </div>`;
  }

  let numberFormat = "cn";
  function setNumberFormat(value) {
    numberFormat = value === "en" ? "en" : "cn";
  }
  function currentNumberFormat() {
    return numberFormat;
  }

  function trimAmount(value, digits) {
    const text = Number(value).toFixed(digits);
    return text.replace(/(\.\d*?)0+$/, "$1").replace(/\.$/, "");
  }

  function formatUsd(value, style) {
    const number = Number(value);
    if (!Number.isFinite(number) || number <= 0) return { text: "", unit: "" };
    if (style === "en") {
      if (number >= 1e9) return { text: trimAmount(number / 1e9, 2), unit: "B" };
      if (number >= 1e6) return { text: trimAmount(number / 1e6, 1), unit: "M" };
      if (number >= 1e3) return { text: trimAmount(number / 1e3, 1), unit: "K" };
      return { text: number >= 100 ? String(Math.round(number)) : trimAmount(number, 2), unit: "" };
    }
    if (number >= 1e8) return { text: trimAmount(number / 1e8, 2), unit: "亿" };
    if (number >= 1e4) return { text: trimAmount(number / 1e4, 1), unit: "万" };
    return { text: number >= 1 ? String(Math.round(number)) : trimAmount(number, 4), unit: "" };
  }

  function mcapTone(value) {
    const number = Number(value) || 0;
    if (number >= 5e7) return "hot";
    if (number >= 1e7) return "amber";
    if (number >= 1e6) return "green";
    if (number >= 1e5) return "blue";
    return "muted";
  }

  function moneyHtml(value, fallback) {
    const parts = formatUsd(value, numberFormat);
    if (parts.text) {
      const unit = parts.unit ? `<span class="wf-metric-unit">${escapeText(parts.unit)}</span>` : "";
      return `${escapeText(parts.text)}${unit}`;
    }
    if (fallback && fallback !== "—") return escapeText(fallback);
    return "";
  }

  function metricTile(label, valueHtml) {
    const empty = !valueHtml;
    return `<div class="wf-metric${empty ? " is-empty" : ""}"><span class="wf-metric-k">${escapeText(label)}</span><strong class="wf-metric-v">${empty ? "—" : valueHtml}</strong></div>`;
  }

  const ICON_X = `<svg class="wf-tag-ico" viewBox="0 0 16 16" aria-hidden="true"><path fill="currentColor" d="M9.4 6.9 14.7 1h-1.5L8.9 6 5.7 1H1.2l5.6 8.1L1.2 15h1.5L7 9.8 10.4 15h4.5L9.4 6.9zm-1.5 2.2-.4-.6L3.5 2.2h1.8l3.2 4.6.4.6 4.1 5.9h-1.8L7.9 9.1z"/></svg>`;
  const ICON_WEB = `<svg class="wf-tag-ico" viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="6" fill="none" stroke="currentColor" stroke-width="1.4"/><path d="M2.2 8h11.6M8 2.2c1.9 2 2.9 3.8 2.9 5.8S9.9 11.8 8 13.8C6.1 11.8 5.1 10 5.1 8S6.1 4.2 8 2.2z" fill="none" stroke="currentColor" stroke-width="1.4"/></svg>`;

  function formatX(value) {
    const number = Number(value);
    if (!Number.isFinite(number) || number <= 0) return "";
    if (number >= 100) return `${Math.round(number)}x`;
    if (number >= 10) return `${number.toFixed(1).replace(/\.0$/, "")}x`;
    return `${Number(number.toFixed(2))}x`;
  }

  function tokenHtml(token) {
    const isWallet = token.kind === "wallet" || token.source === "gmgn-wallet";
    const headLogo = token.logo
      ? `<img class="wf-token-logo" src="${escapeText(token.logo)}" data-wf-src="${escapeText(token.logo)}" alt="" />`
      : `<span class="wf-token-logo wf-token-fallback">${escapeText((token.symbol || (isWallet ? "钱" : "?")).slice(0, 1))}</span>`;
    const symbol = isWallet
      ? "钱包地址"
      : token.symbol
        ? `$${escapeText(token.symbol)}`
        : "未知代币";
    const chain = token.chain ? chainBadge(token.chain) : "";
    const debutBits = [];
    if (!isWallet && token.isFirstCall) debutBits.push("所有群首发");
    if (!isWallet && token.isGroupFirst) debutBits.push("本群首发");
    const debutBar = debutBits.length
      ? `<div class="wf-debut${token.isFirstCall ? "" : " is-group"}" title="${token.isFirstCall ? "监控里第一次出现这个合约" : "这个群第一次出现这个合约"}">${escapeText(debutBits.join(" · "))}</div>`
      : "";
    const launch = token.launchpad
      ? `<span class="wf-tag">${escapeText(token.launchpad)}</span>`
      : "";
    const twitter = token.twitter
      ? `<a class="wf-tag twitter" href="https://x.com/${escapeText(token.twitter)}" target="_blank" rel="noreferrer">${ICON_X}推特</a>`
      : "";
    const website = token.website
      ? `<a class="wf-tag web" href="${escapeText(token.website)}" target="_blank" rel="noreferrer">${ICON_WEB}官网</a>`
      : "";
    const telegram = token.telegram
      ? `<a class="wf-tag web" href="${escapeText(token.telegram)}" target="_blank" rel="noreferrer">Telegram</a>`
      : "";
    const tax = token.buyTax || token.sellTax
      ? `<span class="wf-tax buy">买 ${escapeText(token.buyTax || "0%")}</span><span class="wf-tax">卖 ${escapeText(token.sellTax || "0%")}</span>`
      : "";
    const flags = [
      token.honeypot === "yes" ? `<span class="wf-flag bad">貔貅：是</span>` : "",
      token.honeypot === "no" ? `<span class="wf-flag ok">貔貅：否</span>` : "",
      token.openSource === "yes" ? `<span class="wf-flag ok">开源：是</span>` : "",
      token.openSource === "no" ? `<span class="wf-flag">开源：否</span>` : "",
      token.renounced === "yes" ? `<span class="wf-flag ok">弃权：是</span>` : "",
      token.renounced === "no" ? `<span class="wf-flag">弃权：否</span>` : "",
      token.top10 ? `<span class="wf-flag">前10 ${escapeText(token.top10)}</span>` : "",
    ].join("");
    const cap = moneyHtml(token.marketCap, token.marketCapText);
    const liq = moneyHtml(token.liquidity, token.liquidityText);
    const holders = token.holders ? escapeText(String(token.holders)) : "";
    const created = formatCreated(token.createdAt);
    const nowX = formatX(token.nowX);
    const maxX = formatX(token.maxX);
    const groupNowX = formatX(token.groupNowX);
    const groupMaxX = formatX(token.groupMaxX);
    const firstCap = moneyHtml(token.firstMcap, "");
    const groupFirstCap = moneyHtml(token.groupFirstMcap, "");
    const showGroupX = !token.sameGroupAsGlobal && (groupNowX || groupMaxX);
    const xrow = (label, cur, peak, hint) => (cur || peak)
      ? `<div class="wf-xrow">
           <span class="wf-xlab">${escapeText(label)}</span>
           ${cur ? `<span class="wf-xchip now">当前 <b>${cur}</b></span>` : ""}
           ${peak ? `<span class="wf-xchip max" title="${escapeText(hint || "")}">最高 <b>${peak}</b></span>` : ""}
         </div>`
      : "";
    const xbar = (nowX || maxX || showGroupX)
      ? `<div class="wf-xbar">${xrow("所有群", nowX, maxX, "自所有群首发之后的最高")}${showGroupX ? xrow("本群", groupNowX, groupMaxX, "自本群首发之后的最高") : ""}</div>`
      : "";
    const foundBlock = (kind, name, userId, group, capHtml, when) => {
      if (!name && !group) return "";
      const av = userId
        ? `<div class="wf-avatar wf-discover-av" style="background:hsl(${hue(userId)} 62% 46%)">${lazyImg(assetUrl(`/api/avatar?username=${encodeURIComponent(userId)}`), "")}<span>${escapeText(initial(name))}</span></div>`
        : (name ? `<div class="wf-avatar wf-discover-av" style="background:hsl(${hue(name)} 62% 46%)">${escapeText(initial(name))}</div>` : "");
      const who = userId
        ? `<button type="button" class="wf-discover-name" data-ca-user="${escapeText(userId)}">${escapeText(name || "未知")}</button>`
        : `<strong>${escapeText(name || "未知")}</strong>`;
      return `<div class="wf-discover${kind === "本群首发" ? " is-group" : ""}">
           ${av}
           <div class="wf-discover-body">
             <span class="wf-discover-k">${escapeText(kind)}</span>
             <div class="wf-discover-line">
               ${who}
               ${group ? `<span class="wf-discover-g">${escapeText(group)}</span>` : ""}
             </div>
             <div class="wf-discover-meta">${capHtml ? `发现市值 ${capHtml}` : ""}${when ? `${capHtml ? " · " : ""}${when}` : ""}</div>
           </div>
         </div>`;
    };
    const foundWhen = token.foundAt ? escapeText(formatCreated(token.foundAt) || formatTime(token.foundAt)) : "";
    const groupWhen = token.groupFoundAt ? escapeText(formatCreated(token.groupFoundAt) || formatTime(token.groupFoundAt)) : "";
    const discover = isWallet ? "" : [
      (!token.isFirstCall && (token.foundBy || token.foundGroup))
        ? foundBlock("所有群首发", token.foundBy || token.foundById, token.foundById, token.foundGroup, firstCap, foundWhen)
        : "",
      (!token.isGroupFirst && !token.sameGroupAsGlobal && (token.groupFoundBy || token.groupFoundGroup))
        ? foundBlock("本群首发", token.groupFoundBy || token.groupFoundById, token.groupFoundById, token.groupFoundGroup, groupFirstCap, groupWhen)
        : "",
    ].join("");
    const note = token.note ? `<div class="wf-wallet-note">${escapeText(token.note)}</div>` : "";
    const tags = `${tax}${flags}${twitter}${website}${telegram}`;
    const fmt = numberFormat === "en" ? "en" : "cn";
    const metrics = isWallet
      ? ""
      : (cap || liq || holders || created)
        ? `<div class="wf-stats">
            <div class="wf-mcap is-${mcapTone(token.marketCap)}${cap ? "" : " is-empty"}">
              <div class="wf-mcap-head">
                <span class="wf-mcap-k">市值</span>
                <div class="wf-numfmt" title="切换市值 / 池子数字格式">
                  <button type="button" data-mcap-format="cn"${fmt === "cn" ? ' class="on"' : ""}>万</button>
                  <button type="button" data-mcap-format="en"${fmt === "en" ? ' class="on"' : ""}>K</button>
                </div>
              </div>
              <strong class="wf-mcap-v">${cap || "—"}</strong>
              ${xbar}
            </div>
            <div class="wf-metrics">${metricTile("池子", liq)}${metricTile("持有", holders)}${metricTile("发现", created ? escapeText(created) : "")}</div>
          </div>`
        : "";
    const links = isWallet && (twitter || website || telegram)
      ? `<div class="wf-token-tags">${twitter}${website}${telegram}</div>`
      : tags
        ? `<div class="wf-token-tags">${tags}</div>`
        : "";
    return `<div class="wf-token${isWallet ? " is-wallet" : ""}${!isWallet && (token.isFirstCall || token.isGroupFirst) ? " is-debut" : ""}${!isWallet && token.isGroupFirst && !token.isFirstCall ? " is-group-debut" : ""}">
      ${debutBar}
      <div class="wf-token-top">
        ${headLogo}
        <div class="wf-token-id">
          <div class="wf-token-title">
            <strong>${symbol}</strong>
            ${isWallet ? `<span class="wf-tag">钱包</span>` : ""}${chain}${launch}
            <a class="wf-token-link" href="${escapeText(token.url || "#")}" target="_blank" rel="noreferrer">${isWallet ? "GMGN 钱包" : "GMGN"}</a>
          </div>
          ${token.name ? `<em>${escapeText(token.name)}</em>` : ""}
          ${caRow(token.address, token.chain)}
        </div>
      </div>
      ${note}
      ${metrics}
      ${discover}
      ${links}
    </div>`;
  }

  function rawAddressHtml(address) {
    return `<div class="wf-token is-pending">${caRow(address)}<div class="wf-token-pending">正在查询代币信息…</div></div>`;
  }

  function contextHtml(card, addresses, chains) {
    const before = card.contextBefore || [];
    const after = card.contextAfter || [];
    if (!before.length && !after.length) return "";
    const line = (item, tag) => {
      const quote = item.quoteText
        ? `<em class="wf-context-quote">${escapeText(item.quoteText)}</em>`
        : "";
      return `<div class="wf-context-item ${tag}">
        <time>${escapeText(formatTime(item.createTime))}</time>
        <p>${highlight(item.text || "…", addresses, chains)}${quote}</p>
      </div>`;
    };
    return `<div class="wf-context">
      <div class="wf-context-label">该用户前后 3 句</div>
      ${before.map((item) => line(item, "before")).join("")}
      <div class="wf-context-now">本条 · 含合约</div>
      ${after.map((item) => line(item, "after")).join("")}
    </div>`;
  }

  function isLocalHttp(url) {
    return /^https?:\/\/(?:127\.0\.0\.1|localhost)\b/i.test(String(url || ""));
  }

  function lazyImg(url, className) {
    const src = escapeText(url || "");
    if (isLocalHttp(url)) {
      return `<img class="${className}" data-wf-src="${src}" alt="" />`;
    }
    return `<img class="${className}" src="${src}" alt="" onerror="this.remove()" />`;
  }

  function mediaHtml(card) {
    const sticker = card.renderType === "emoji";
    const kind = sticker ? "wf-emoji" : "wf-media";
    const miss = sticker ? "[表情]" : "[图片]";
    const src = escapeText(card.mediaUrl || "");
    const full = !sticker && card.mediaFullUrl ? ` data-wf-full="${escapeText(card.mediaFullUrl)}"` : "";
    if (isLocalHttp(card.mediaUrl)) {
      return `<img class="${kind}" data-wf-src="${src}"${full} alt="${miss}" />`;
    }
    return `<img class="${kind}" src="${src}"${full} alt="${miss}" onerror="this.replaceWith('${miss}')" />`;
  }

  function avatarHtml(card, size) {
    const name = card.displayName || card.senderName || "群";
    const fallback = `background:hsl(${hue(card.senderUsername || name)} 62% 46%)`;
    const dim = size ? `style="${fallback};width:${size}px;height:${size}px"` : `style="${fallback}"`;
    if (card.avatarUrl) {
      return `<div class="wf-avatar" ${dim}>${lazyImg(card.avatarUrl, "")}<span>${escapeText(initial(name))}</span></div>`;
    }
    return `<div class="wf-avatar" ${dim}>${escapeText(initial(name))}</div>`;
  }

  function identityHtml(card) {
    const name = card.displayName || card.senderName || "群成员";
    const group = card.displayGroup || card.conversationName || "群聊";
    const nameClass = `wf-name${card.nameRemarked ? " is-remarked" : ""}${card.starred ? " is-starred" : ""}`;
    const groupClass = `wf-group${card.groupRemarked ? " is-remarked" : ""}`;
    const vip = card.starred ? `<span class="wf-vip">关注</span>` : "";
    const starLabel = card.starred ? "取消星标" : "星标此人";
    const starClass = card.starred ? "wf-star on" : "wf-star";
    return `
      <div class="wf-who">
        <button type="button" class="${nameClass}" data-remark-user title="${escapeText(name)} · 点击备注此人">${escapeText(clipText(name, NAME_MAX))}</button>
        ${vip}
        <span class="wf-in">在</span>
        <button type="button" class="${groupClass}" data-remark-group title="${escapeText(group)} · 点击给这个群写备注">${escapeText(clipText(group, GROUP_MAX))}</button>
        ${card.demo ? `<span class="wf-demo">样例</span>` : ""}
        <button type="button" class="${starClass}" data-star title="${starLabel}">${card.starred ? "★" : "☆"}</button>
        <button type="button" class="wf-mute" data-mute-user>不看此人</button>
        <button type="button" class="wf-mute" data-mute>不看此群</button>
      </div>`;
  }

  function quoteBlock(card, addresses, chains) {
    if (!card.quoteText) return "";
    const who = {
      displayName: card.quoteName || "群友",
      senderUsername: card.quoteUsername || "",
      avatarUrl: card.quoteAvatarUrl || "",
    };
    const name = card.quoteName
      ? `<div class="wf-quote-name">${escapeText(card.quoteName)}</div>`
      : "";
    return `<div class="wf-quote">
      <div class="wf-quote-who">${avatarHtml(who, 28)}${name}</div>
      <div class="wf-quote-text">${highlight(card.quoteText, addresses, chains)}</div>
    </div>`;
  }

  function bodyHtml(card) {
    const tokens = card.tokenCards || [];
    const known = knownAddresses(card);
    const chains = chainMap(card);
    const quote = quoteBlock(card, known, chains);
    const reply = card.quoteName ? `<div class="wf-reply">回复 ${escapeText(card.quoteName)}</div>` : "";
    const tokenStrip = tokens.length
      ? `<div class="wf-tokens">${tokens.map(tokenHtml).join("")}</div>`
      : (card.addresses || []).length
        ? `<div class="wf-tokens">${card.addresses.map(rawAddressHtml).join("")}</div>`
        : "";
    // 有图时「[图片]」占位字就不重复显示了，图加载失败会换回这几个字。
    const onlyPlaceholder = Boolean(card.mediaUrl) && (
      (card.renderType === "image" && card.text === "[图片]")
      || (card.renderType === "emoji" && card.text === "[表情]")
    );
    const text = onlyPlaceholder ? "" : `<div class="wf-text">${highlight(card.text || "", known, chains)}</div>`;
    return `${reply}
      ${text}
      ${card.mediaUrl ? mediaHtml(card) : ""}
      ${tokenStrip}
      ${contextHtml(card, known, chains)}
      ${quote}`;
  }

  const STAR_COLORS = [
    { id: "amber", name: "琥珀", hex: "#d97706" },
    { id: "red", name: "赤红", hex: "#dc2626" },
    { id: "rose", name: "玫红", hex: "#db2777" },
    { id: "violet", name: "紫罗兰", hex: "#7c3aed" },
    { id: "indigo", name: "靛蓝", hex: "#2563eb" },
    { id: "cyan", name: "青色", hex: "#0891b2" },
    { id: "green", name: "翠绿", hex: "#059669" },
    { id: "lime", name: "柠檬", hex: "#65a30d" },
    { id: "orange", name: "橙色", hex: "#ea580c" },
    { id: "slate", name: "石墨", hex: "#334155" },
  ];

  function starColorId(card) {
    const id = String(card.starColor || "amber");
    return STAR_COLORS.some((item) => item.id === id) ? id : "amber";
  }

  function cardClass(card) {
    const parts = ["wf-card"];
    if (card.starred) parts.push("is-starred", `star-${starColorId(card)}`);
    if (card.demo) parts.push("is-demo");
    if ((card.cas || []).length || (card.addresses || []).length) parts.push("is-ca");
    return parts.join(" ");
  }

  // 信息流新消息在上。同一人同一群、间隔不超过 5 分钟的连续发言，簇顶出昵称，后续只留头像。
  const STACK_GAP_SEC = 300;
  const NAME_MAX = 10;
  const GROUP_MAX = 8;

  function clipText(value, max) {
    const chars = [...String(value || "")];
    if (chars.length <= max) return chars.join("");
    return `${chars.slice(0, max).join("")}…`;
  }

  function sameSpeaker(a, b) {
    if (!a || !b) return false;
    const user = a.senderUsername || "";
    const group = a.conversationId || "";
    if (!user || !group) return false;
    return user === (b.senderUsername || "") && group === (b.conversationId || "");
  }

  function closeInTime(a, b, gap) {
    const left = Number(a && a.createTime) || 0;
    const right = Number(b && b.createTime) || 0;
    if (!left || !right) return false;
    return Math.abs(left - right) <= gap;
  }

  function stackMeta(cards, index, gapSec) {
    const list = Array.isArray(cards) ? cards : [];
    const card = list[index];
    const raw = Number(gapSec);
    const gap = Number.isFinite(raw) && raw > 0 ? raw : STACK_GAP_SEC;
    if (!card) return { stacked: false, stackPos: "solo" };
    const newer = list[index - 1];
    const older = list[index + 1];
    const stacked = sameSpeaker(card, newer) && closeInTime(card, newer, gap);
    const continues = sameSpeaker(card, older) && closeInTime(card, older, gap);
    let stackPos = "solo";
    if (stacked && continues) stackPos = "mid";
    else if (stacked) stackPos = "end";
    else if (continues) stackPos = "start";
    return { stacked, stackPos };
  }

  function stackClass(meta) {
    const pos = meta && meta.stackPos;
    return pos && pos !== "solo" ? ` is-stack-${pos}` : "";
  }

  function cardHtml(card, meta) {
    const tone = hue(card.conversationId || card.displayGroup || "g");
    const stacked = Boolean(meta && meta.stacked);
    const avatar = avatarHtml(card, stacked ? 36 : undefined);
    const head = stacked
      ? ""
      : `<div class="wf-head">
          ${identityHtml(card)}
          <div class="wf-head-right">${freshHtml(card.createTime)}<time>${escapeText(formatTime(card.createTime))}</time></div>
        </div>`;
    const stackTime = stacked
      ? `<div class="wf-stack-time">${freshHtml(card.createTime)}<time>${escapeText(formatTime(card.createTime))}</time></div>`
      : "";
    return `<article class="${cardClass(card)}${stackClass(meta)}" data-id="${escapeText(card.id)}" data-group="${escapeText(card.conversationId || "")}" data-user="${escapeText(card.senderUsername || "")}" style="--wf-hue:${tone}">
      ${avatar}
      <div class="wf-main">
        ${head}
        ${bodyHtml(card)}
        ${stackTime}
      </div>
    </article>`;
  }

  function logoSvg(size) {
    const px = Number(size) || 28;
    return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128" width="${px}" height="${px}" aria-hidden="true">
      <rect width="128" height="128" rx="28" fill="#111827"/>
      <path d="M24 62c0-20 18-36 40-36s40 16 40 36-18 36-40 36c-5 0-9-.6-13-1.8L26 106l5-16C27 82 24 73 24 62z" fill="#16a34a"/>
      <ellipse cx="64" cy="60" rx="33" ry="29" fill="#fefce8"/>
      <ellipse cx="64" cy="60" rx="28" ry="24" fill="#e11d48"/>
      <ellipse cx="52" cy="48" rx="10" ry="6" fill="#fb7185" opacity=".45"/>
      <circle cx="52" cy="60" r="5" fill="#f8fafc"/>
      <circle cx="64" cy="62" r="5" fill="#f8fafc"/>
      <circle cx="76" cy="60" r="5" fill="#f8fafc"/>
      <path d="M93 22c16-9 26 6 17 18-10-3-15-8-17-18z" fill="#86efac"/>
      <path d="M93 22c3 8 10 14 17 18" fill="none" stroke="#166534" stroke-width="2" stroke-linecap="round"/>
    </svg>`;
  }
  const AUTHOR_X = "https://x.com/0xTechMelon";
  const DEFAULT_WATERMARK = "𝕏 @0xTechMelon";

  function watermarkHtml(className) {
    return `<a class="${className}" href="${AUTHOR_X}" target="_blank" rel="noreferrer">${escapeText(DEFAULT_WATERMARK)}</a>`;
  }

  function gmgnCardHtml(card, meta) {
    const tone = hue(card.conversationId || card.displayGroup || "g");
    const stacked = Boolean(meta && meta.stacked);
    const name = card.displayName || card.senderName || "群成员";
    const group = card.displayGroup || card.conversationName || "群聊";
    const copyText = [card.text, card.quoteText ? `「${card.quoteName || "群友"}：${card.quoteText}」` : ""]
      .filter(Boolean)
      .join("\n");
    const nameClass = `wf-name${card.nameRemarked ? " is-remarked" : ""}${card.starred ? " is-starred" : ""}`;
    const groupClass = `wf-group${card.groupRemarked ? " is-remarked" : ""}`;
    const header = stacked
      ? `<header class="wf-xhead">${avatarHtml(card, 22)}</header>`
      : `<header class="wf-xhead">
        ${avatarHtml(card, 26)}
        <div class="wf-xnames">
          <button type="button" class="${nameClass}" data-remark-user title="${escapeText(name)} · 点击备注此人">${escapeText(clipText(name, NAME_MAX))}</button>
          ${card.starred ? `<span class="wf-starmark">★</span>` : ""}
          <span class="wf-in">在</span>
          <button type="button" class="${groupClass}" data-remark-group title="${escapeText(group)} · 点击给这个群写备注">${escapeText(clipText(group, GROUP_MAX))}</button>
          ${card.demo ? `<span class="wf-demo">样例</span>` : ""}
        </div>
        <div class="wf-xside">
          ${freshHtml(card.createTime)}
          <div class="wf-xactions">
            <button type="button" class="wf-star${card.starred ? " on" : ""}" data-star title="${card.starred ? "取消星标" : "星标此人"}">${card.starred ? "★" : "☆"}</button>
            <button type="button" class="wf-mute" data-mute-user title="屏蔽这个人的发言">不看此人</button>
            <button type="button" class="wf-mute" data-mute>不看此群</button>
          </div>
        </div>
      </header>`;
    const mark = stacked ? "" : `<i>|</i>${watermarkHtml("wf-footmark")}`;
    return `<article class="${cardClass(card)} wf-xcard${stackClass(meta)}" data-id="${escapeText(card.id)}" data-group="${escapeText(card.conversationId || "")}" data-user="${escapeText(card.senderUsername || "")}" style="--wf-hue:${tone}">
      ${header}
      <div class="wf-xbody">${bodyHtml(card)}</div>
      <footer class="wf-xfoot">
        <span class="wf-xfoot-meta"><time title="${escapeText(formatAbs(card.createTime))}">${escapeText(formatTime(card.createTime))}</time>${mark}</span>
        ${copyText ? `<button type="button" class="wf-xcopy" data-copy="${escapeText(copyText)}">复制</button>` : ""}
      </footer>
    </article>`;
  }

  function cardCss() {
    return `
      :host, * { box-sizing: border-box; }
      :host { --bg:#fff; --ink:#101828; --muted:#667085; --line:#e4e7ec; --card:#fff; color: var(--ink); }
      :host([data-theme="dark"]) { --bg:#111214; --ink:#f5f5f5; --muted:#98a2b3; --line:#2b2f36; --card:#1b1d22; }
      .wf-card { display:grid; grid-template-columns:44px minmax(0,1fr); gap:10px; padding:12px; border:1px solid var(--line); border-radius:14px; background:var(--card); border-left:3px solid hsl(var(--wf-hue, 150) 42% 48%); width:100%; max-width:100%; min-width:0; height:auto; flex:0 0 auto; position:relative; overflow:visible; }
      .wf-card.is-starred { box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--wf-star, #d97706) 35%, transparent); }
      ${STAR_COLORS.map((item) => `.wf-card.star-${item.id}{--wf-star:${item.hex};border-left-color:${item.hex};background:linear-gradient(90deg, color-mix(in srgb, ${item.hex} 16%, var(--card)), var(--card) 48%);} .wf-card.star-${item.id} .wf-name.is-starred,.wf-card.star-${item.id} .wf-star.on{color:${item.hex};border-color:${item.hex};}`).join("")}
      .wf-avatar { position:relative; display:grid; place-items:center; width:44px; height:44px; overflow:hidden; border-radius:50%; color:#fff; font-weight:700; flex:none; }
      .wf-avatar img { position:absolute; inset:0; width:100%; height:100%; object-fit:cover; }
      .wf-main { min-width:0; display:block; }
      .wf-head { display:flex; justify-content:space-between; gap:8px; align-items:flex-start; }
      .wf-head-right, .wf-stack-time { display:flex; align-items:center; gap:8px; color:var(--muted); font-size:12px; white-space:nowrap; }
      .wf-fresh { color:#e5484d; font-size:11px; font-weight:800; letter-spacing:.02em; white-space:nowrap; }
      .wf-debut { display:flex; align-items:center; gap:8px; margin:0 0 8px; padding:7px 12px; border-radius:12px; background:linear-gradient(90deg,#fff7ed,#ffedd5 60%,#fef3c7); border:1px solid #f5b942; color:#9a3412; font-size:13px; font-weight:800; letter-spacing:.12em; }
      .wf-debut::before { content:""; width:8px; height:8px; border-radius:50%; background:#f59e0b; box-shadow:0 0 0 3px rgba(245,158,11,.28); flex:none; }
      .wf-token.is-debut { box-shadow:inset 0 0 0 1.5px #f5b942; }
      .wf-token.is-group-debut { box-shadow:inset 0 0 0 1.5px #2e90fa; }
      .wf-debut.is-group { background:linear-gradient(90deg,#eff8ff,#d1e9ff); border-color:#84caff; color:#175cd3; }
      .wf-debut.is-group::before { background:#2e90fa; box-shadow:0 0 0 3px rgba(46,144,250,.28); }
      .wf-xcard .wf-debut { margin:6px 0 4px; padding:5px 10px; font-size:12px; }
      .wf-discover-name { border:0; padding:0; background:transparent; font:inherit; font-weight:800; cursor:pointer; color:inherit; }
      .wf-discover-name:hover { color:#175cd3; }
      .wf-who { display:flex; flex-wrap:wrap; gap:6px; align-items:center; min-width:0; }
      .wf-name, .wf-group { border:0; padding:0; background:transparent; font:inherit; cursor:pointer; color:inherit; }
      .wf-name { font-weight:800; font-size:15px; }
      .wf-name.is-starred { color:#b45309; }
      .wf-name.is-remarked { font-style:italic; color:#175cd3; }
      .wf-group { padding:1px 8px; border-radius:999px; background:hsl(var(--wf-hue, 150) 42% 92%); color:hsl(var(--wf-hue, 150) 42% 28%); font-weight:700; }
      .wf-group:hover { filter:brightness(0.97); }
      .wf-group.is-remarked { color:#175cd3; background:#e8f1ff; box-shadow:inset 0 0 0 1.5px #2e90fa; font-style:normal; font-weight:800; }
      .wf-group.is-remarked:hover { filter:none; background:#dce9ff; }
      .wf-vip, .wf-demo { padding:0 6px; border-radius:999px; font-size:11px; }
      .wf-vip { background:#fff4d6; color:#b54708; }
      .wf-demo { background:#fff4d6; color:#946200; }
      .wf-mute, .wf-star { border:1px solid var(--line); background:transparent; border-radius:999px; font:inherit; font-size:12px; cursor:pointer; padding:0 8px; height:22px; line-height:20px; color:var(--muted); white-space:nowrap; flex:none; }
      .wf-xcard .wf-mute, .wf-xcard .wf-star { height:20px; line-height:18px; font-size:11px; padding:0 7px; }
      .wf-star.on { color:#b54708; border-color:#f6d6a8; background:#fff8eb; }
      .wf-mute { color:#b42318; opacity:.45; }
      .wf-card:hover .wf-mute { opacity:1; }
      .wf-text { margin-top:8px; line-height:1.6; word-break:break-word; }
      .wf-text-link { color:#175cd3; text-decoration:underline; text-underline-offset:2px; overflow-wrap:anywhere; }
      .wf-text-link:hover { color:#1570ef; }
      .wf-reply { color:var(--muted); font-size:12px; margin-top:4px; }
      .wf-ca { display:inline-flex; align-items:center; gap:6px; max-width:100%; margin:1px 2px; vertical-align:middle; }
      .wf-ca .wf-chain-ico { flex:none; }
      .wf-ca code { padding:2px 8px; border-radius:6px; background:#fff; color:#c11fa8; font:12.5px/1.5 Consolas,"SFMono-Regular",Menlo,monospace; overflow-wrap:anywhere; box-shadow:inset 0 0 0 1px dashed #f63d68; }
      .wf-ca-copy { flex:none; border:0; height:24px; padding:0 10px; border-radius:6px; background:#2e90fa; color:#fff; font:700 12px/24px inherit; cursor:pointer; }
      .wf-ca-copy:hover { background:#1570ef; }
      :host([data-theme="dark"]) .wf-ca code { background:rgba(217,70,239,.12); color:#f0abfc; box-shadow:inset 0 0 0 1px dashed rgba(246,61,104,.7); }
      :host([data-theme="dark"]) .wf-ca-copy { background:#2e90fa; color:#fff; }
      :host([data-theme="dark"]) .wf-group { background:hsl(var(--wf-hue, 150) 30% 22%); color:hsl(var(--wf-hue, 150) 60% 80%); }
      :host([data-theme="dark"]) .wf-group.is-remarked { background:rgba(46,144,250,.22); color:#b2ccff; box-shadow:inset 0 0 0 1.5px #84adff; }
      :host([data-theme="dark"]) .wf-name.is-remarked { color:#84adff; }
      :host([data-theme="dark"]) .wf-name.is-starred { color:#fdb022; }
      :host([data-theme="dark"]) .wf-vip, :host([data-theme="dark"]) .wf-demo { background:rgba(247,144,9,.18); color:#fdb022; }
      :host([data-theme="dark"]) .wf-star.on { background:rgba(247,144,9,.14); border-color:rgba(247,144,9,.4); color:#fdb022; }
      :host([data-theme="dark"]) .wf-mute { color:#f97066; }
      :host([data-theme="dark"]) .wf-card.is-ca { background:linear-gradient(180deg, rgba(46,144,250,.08), var(--card)); }
      :host([data-theme="dark"]) .wf-token { background:linear-gradient(180deg, rgba(229,72,77,.12), var(--card) 46%); border-color:rgba(249,194,200,.28); }
      :host([data-theme="dark"]) .wf-token.is-wallet { background:linear-gradient(180deg, rgba(124,58,237,.16), var(--card)); border-color:rgba(167,139,250,.35); }
      :host([data-theme="dark"]) .wf-wallet-note { color:#c4b5fd; }
      :host([data-theme="dark"]) .wf-token-fallback { background:rgba(18,183,106,.2); color:#75e0a7; }
      :host([data-theme="dark"]) .wf-token-link { background:#344054; }
      :host([data-theme="dark"]) .wf-ca-addr { background:rgba(255,255,255,.04); color:#f0abfc; box-shadow:inset 0 0 0 1px dashed rgba(246,61,104,.7); }
      :host([data-theme="dark"]) .wf-copy-btn { background:#2e90fa; color:#fff; }
      :host([data-theme="dark"]) .wf-metric { background:rgba(255,255,255,.04); border-color:rgba(255,255,255,.08); }
      :host([data-theme="dark"]) .wf-mcap { background:rgba(255,255,255,.04); border-color:rgba(255,255,255,.08); }
      :host([data-theme="dark"]) .wf-mcap.is-blue { background:rgba(46,144,250,.14); border-color:rgba(132,173,255,.35); }
      :host([data-theme="dark"]) .wf-mcap.is-blue .wf-mcap-v { color:#84adff; }
      :host([data-theme="dark"]) .wf-mcap.is-green { background:rgba(18,183,106,.14); border-color:rgba(117,224,167,.35); }
      :host([data-theme="dark"]) .wf-mcap.is-green .wf-mcap-v { color:#75e0a7; }
      :host([data-theme="dark"]) .wf-mcap.is-amber { background:rgba(247,144,9,.14); border-color:rgba(253,176,34,.35); }
      :host([data-theme="dark"]) .wf-mcap.is-amber .wf-mcap-v { color:#fdb022; }
      :host([data-theme="dark"]) .wf-mcap.is-hot { background:rgba(240,68,56,.16); border-color:rgba(253,162,155,.35); }
      :host([data-theme="dark"]) .wf-mcap.is-hot .wf-mcap-v { color:#fda29b; }
      :host([data-theme="dark"]) .wf-numfmt { background:rgba(255,255,255,.04); border-color:rgba(255,255,255,.12); }
      :host([data-theme="dark"]) .wf-numfmt button { color:#98a2b3; }
      :host([data-theme="dark"]) .wf-numfmt button.on { background:#f5f5f5; color:#111827; }
      :host([data-theme="dark"]) .wf-tag.twitter { background:rgba(46,144,250,.18); color:#84adff; }
      :host([data-theme="dark"]) .wf-tag.web, :host([data-theme="dark"]) .wf-tax.buy, :host([data-theme="dark"]) .wf-flag.ok { background:rgba(18,183,106,.16); color:#75e0a7; }
      :host([data-theme="dark"]) .wf-tax, :host([data-theme="dark"]) .wf-flag.bad { background:rgba(240,68,56,.16); color:#fda29b; }
      :host([data-theme="dark"]) .wf-flag { background:rgba(255,255,255,.06); }
      :host([data-theme="dark"]) .wf-context-now { color:#47cd89; }
      :host([data-theme="dark"]) .wf-text-link { color:#84adff; }
      :host([data-theme="dark"]) .wf-text-link:hover { color:#b2ccff; }
      .wf-media { display:block; max-width:100%; max-height:280px; width:auto; height:auto; margin-top:8px; border-radius:12px; object-fit:contain; object-position:left top; cursor:zoom-in; }
      .wf-emoji { display:block; max-width:120px; max-height:120px; width:auto; height:auto; margin-top:6px; object-fit:contain; object-position:left top; }
      .wf-emote { width:24px; height:24px; vertical-align:-5px; display:inline-block; }
      .wf-quote { margin-top:8px; padding:8px 10px; border-radius:10px; background:rgba(127,127,127,.08); color:var(--muted); display:grid; gap:6px; }
      .wf-quote-who { display:flex; align-items:center; gap:8px; min-width:0; }
      .wf-quote-who .wf-avatar { width:28px; height:28px; font-size:12px; }
      .wf-quote-name { color:var(--ink); font-weight:700; margin:0; }
      .wf-quote-text { line-height:1.5; }
      .wf-card.is-ca { background: linear-gradient(180deg, #f4f9ff, var(--card)); }
      .wf-tokens { display:grid; gap:8px; margin-top:8px; }
      .wf-token { display:grid; gap:10px; padding:12px; border-radius:16px; background:linear-gradient(180deg, #fff5f6, var(--card) 42%); border:1px solid #f9c2c8; min-width:0; }
      .wf-token.is-wallet { background:linear-gradient(180deg, #f4f1ff, var(--card)); border-color:#ddd6fe; }
      .wf-token.is-pending { padding:12px; gap:8px; }
      .wf-token-pending { color:var(--muted); font-size:12px; }
      .wf-wallet-note { color:#6941c6; font-size:12px; font-weight:700; }
      .wf-token-top { display:flex; gap:10px; align-items:flex-start; min-width:0; }
      .wf-token-id { display:grid; gap:6px; min-width:0; flex:1; }
      .wf-token-title { display:flex; flex-wrap:wrap; gap:6px; align-items:center; min-width:0; }
      .wf-token-title strong { font-size:15px; line-height:1.2; }
      .wf-token-id em { color:var(--muted); font-style:normal; font-size:12px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
      .wf-ca-row { display:flex; flex-wrap:wrap; gap:8px; align-items:center; min-width:0; }
      .wf-ca-label { color:var(--muted); font-size:12px; font-weight:700; }
      .wf-ca-addr { padding:5px 10px; border-radius:8px; background:#fff; color:#c11fa8; font:13px/1.3 Consolas,"SFMono-Regular",Menlo,monospace; box-shadow:inset 0 0 0 1px dashed #f63d68; }
      .wf-copy-btn { flex:none; border:0; height:34px; padding:0 16px; border-radius:8px; background:#2e90fa; color:#fff; font:800 14px/34px inherit; cursor:pointer; }
      .wf-copy-btn:hover { background:#1570ef; }
      .wf-stats { display:grid; gap:8px; }
      .wf-mcap { padding:10px 12px; border-radius:14px; background:#f8fafc; border:1px solid #e4e7ec; min-width:0; }
      .wf-mcap-head { display:flex; justify-content:space-between; align-items:center; gap:8px; }
      .wf-mcap-k { color:#667085; font-size:12px; font-weight:800; letter-spacing:.04em; }
      .wf-mcap-v { display:block; margin-top:4px; color:var(--ink); font-size:26px; font-weight:800; letter-spacing:-.03em; line-height:1.15; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
      .wf-xbar { display:grid; gap:6px; margin-top:8px; }
      .wf-xrow { display:flex; flex-wrap:wrap; gap:6px; align-items:center; }
      .wf-xlab { color:#667085; font-size:11px; font-weight:800; letter-spacing:.04em; }
      .wf-xchip { display:inline-flex; align-items:center; gap:4px; height:22px; padding:0 8px; border-radius:999px; font-size:12px; font-weight:700; }
      .wf-xchip b { font-size:13px; }
      .wf-xchip.now { background:#ecfdf3; color:#067647; }
      .wf-xchip.max { background:#fff7ed; color:#c2410c; }
      .wf-xfound { color:#667085; font-size:12px; }
      .wf-discover { display:flex; gap:10px; align-items:center; margin-top:8px; padding:8px 10px; border-radius:12px; background:#f8fafc; border:1px solid #e4e7ec; }
      .wf-discover-av { width:32px; height:32px; font-size:13px; flex:none; }
      .wf-discover-body { min-width:0; }
      .wf-discover-k { display:block; color:#667085; font-size:11px; font-weight:800; letter-spacing:.06em; }
      .wf-discover-line { display:flex; flex-wrap:wrap; gap:6px; align-items:center; }
      .wf-discover-line strong { font-size:14px; }
      .wf-discover-g { padding:1px 8px; border-radius:999px; background:#e8f1ff; color:#175cd3; font-size:12px; font-weight:700; }
      .wf-discover-meta { color:#667085; font-size:12px; margin-top:2px; }
      :host([data-theme="dark"]) .wf-xchip.now { background:rgba(18,183,106,.16); color:#75e0a7; }
      :host([data-theme="dark"]) .wf-xchip.max { background:rgba(247,144,9,.16); color:#fdb022; }
      :host([data-theme="dark"]) .wf-xfound { color:#98a2b3; }
      :host([data-theme="dark"]) .wf-discover { background:rgba(255,255,255,.04); border-color:rgba(255,255,255,.08); }
      :host([data-theme="dark"]) .wf-discover-g { background:rgba(46,144,250,.22); color:#b2ccff; }
      :host([data-theme="dark"]) .wf-fresh { color:#fda29b; }
      :host([data-theme="dark"]) .wf-debut { background:linear-gradient(90deg,rgba(247,144,9,.28),rgba(247,144,9,.12)); border-color:#d97706; color:#fdb022; }
      :host([data-theme="dark"]) .wf-token.is-debut { box-shadow:inset 0 0 0 1.5px #d97706; }
      :host([data-theme="dark"]) .wf-token.is-group-debut { box-shadow:inset 0 0 0 1.5px #2e90fa; }
      :host([data-theme="dark"]) .wf-debut.is-group { background:rgba(46,144,250,.18); border-color:#2e90fa; color:#b2ccff; }
      :host([data-theme="dark"]) .wf-xlab { color:#98a2b3; }
      .wf-metric-unit { margin-left:3px; font-size:.62em; font-weight:800; }
      .wf-mcap.is-blue { background:linear-gradient(180deg,#eff6ff,#fff); border-color:#b2ccff; }
      .wf-mcap.is-blue .wf-mcap-v { color:#175cd3; }
      .wf-mcap.is-green { background:linear-gradient(180deg,#ecfdf3,#fff); border-color:#a6f4c5; }
      .wf-mcap.is-green .wf-mcap-v { color:#067647; }
      .wf-mcap.is-amber { background:linear-gradient(180deg,#fffaeb,#fff); border-color:#fde68a; }
      .wf-mcap.is-amber .wf-mcap-v { color:#b54708; }
      .wf-mcap.is-hot { background:linear-gradient(180deg,#fff1f3,#fff); border-color:#fda29b; }
      .wf-mcap.is-hot .wf-mcap-v { color:#b42318; }
      .wf-mcap.is-empty .wf-mcap-v { color:#98a2b3; font-size:18px; font-weight:600; }
      .wf-numfmt { display:inline-flex; padding:2px; border-radius:999px; background:#fff; border:1px solid #e4e7ec; }
      .wf-numfmt button { height:20px; padding:0 8px; border:0; border-radius:999px; background:transparent; color:#667085; font:700 11px/20px inherit; cursor:pointer; }
      .wf-numfmt button.on { background:#111827; color:#fff; }
      .wf-metrics { display:grid; grid-template-columns:repeat(3, minmax(0,1fr)); gap:8px; }
      .wf-metric { padding:8px 10px; border-radius:12px; background:#fff; border:1px solid #eef2f6; min-width:0; }
      .wf-metric-k { display:block; color:#98a2b3; font-size:11px; font-weight:700; }
      .wf-metric-v { display:block; margin-top:4px; color:var(--ink); font-size:16px; font-weight:800; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
      .wf-metric.is-empty .wf-metric-v { color:#98a2b3; font-weight:600; font-size:14px; }
      .wf-token-tags { display:flex; flex-wrap:wrap; gap:6px; align-items:center; }
      .wf-token-logo { width:48px; height:48px; border-radius:12px; object-fit:cover; flex:none; }
      .wf-token-fallback { display:grid; place-items:center; background:#d1fadf; color:#087443; font-weight:800; }
      .wf-token-link, .wf-tag, .wf-tax, .wf-flag { padding:2px 8px; border-radius:999px; border:0; font:inherit; font-size:12px; text-decoration:none; cursor:pointer; }
      .wf-tag { display:inline-flex; align-items:center; gap:4px; }
      .wf-tag-ico { width:12px; height:12px; flex:none; display:block; }
      .wf-token-link { margin-left:auto; background:#111827; color:#fff; }
      .wf-chain { display:inline-flex; align-items:center; gap:4px; height:22px; padding:0 8px 0 4px; border-radius:999px; border:1px solid var(--wf-chain-line, #d0d5dd); background:var(--wf-chain-bg, #fff); color:var(--wf-chain-ink, #344054); font:700 12px/20px inherit; }
      .wf-chain-ico { display:block; width:16px; height:16px; border-radius:4px; object-fit:contain; flex:none; }
      .wf-chain.is-sol { --wf-chain-line:#c4b5fd; --wf-chain-bg:#f5f3ff; --wf-chain-ink:#5b21b6; }
      .wf-chain.is-base { --wf-chain-line:#93c5fd; --wf-chain-bg:#eff6ff; --wf-chain-ink:#1d4ed8; }
      .wf-chain.is-bsc { --wf-chain-line:#f5c518; --wf-chain-bg:#fffbeb; --wf-chain-ink:#854d0e; }
      .wf-chain.is-eth { --wf-chain-line:#d0d5dd; --wf-chain-bg:#f8fafc; --wf-chain-ink:#344054; }
      .wf-chain.is-robinhood { --wf-chain-line:#86efac; --wf-chain-bg:#f0fdf4; --wf-chain-ink:#166534; }
      .wf-chain.is-arc { --wf-chain-line:#98a2b3; --wf-chain-bg:#f2f4f7; --wf-chain-ink:#101828; }
      .wf-chain.is-tron { --wf-chain-line:#fca5a5; --wf-chain-bg:#fef2f2; --wf-chain-ink:#b91c1c; }
      .wf-chain.is-ton { --wf-chain-line:#7dd3fc; --wf-chain-bg:#f0f9ff; --wf-chain-ink:#0369a1; }
      .wf-chain.is-sui { --wf-chain-line:#93c5fd; --wf-chain-bg:#eff6ff; --wf-chain-ink:#1d4ed8; }
      .wf-chain.is-polygon { --wf-chain-line:#d8b4fe; --wf-chain-bg:#faf5ff; --wf-chain-ink:#6b21a8; }
      .wf-chain.is-arb { --wf-chain-line:#7dd3fc; --wf-chain-bg:#f0f9ff; --wf-chain-ink:#0369a1; }
      .wf-chain.is-avax { --wf-chain-line:#fca5a5; --wf-chain-bg:#fef2f2; --wf-chain-ink:#b91c1c; }
      .wf-chain.is-op { --wf-chain-line:#fca5a5; --wf-chain-bg:#fef2f2; --wf-chain-ink:#b91c1c; }
      .wf-chain.is-blast { --wf-chain-line:#eab308; --wf-chain-bg:#fefce8; --wf-chain-ink:#854d0e; }
      .wf-chain.is-monad { --wf-chain-line:#c4b5fd; --wf-chain-bg:#f5f3ff; --wf-chain-ink:#5b21b6; }
      .wf-chain.is-linea, .wf-chain.is-ape, .wf-chain.is-arc { --wf-chain-line:#98a2b3; --wf-chain-bg:#f2f4f7; --wf-chain-ink:#101828; }
      .wf-chain.is-scroll, .wf-chain.is-blast { --wf-chain-line:#eab308; --wf-chain-bg:#fefce8; --wf-chain-ink:#854d0e; }
      .wf-chain.is-zksync { --wf-chain-line:#c4b5fd; --wf-chain-bg:#f5f3ff; --wf-chain-ink:#5b21b6; }
      .wf-chain.is-sei { --wf-chain-line:#fca5a5; --wf-chain-bg:#fef2f2; --wf-chain-ink:#b91c1c; }
      .wf-chain.is-mantle { --wf-chain-line:#5eead4; --wf-chain-bg:#f0fdfa; --wf-chain-ink:#115e59; }
      .wf-chain.is-hyperevm { --wf-chain-line:#5eead4; --wf-chain-bg:#f0fdfa; --wf-chain-ink:#115e59; }
      .wf-chain.is-plasma { --wf-chain-line:#d8b4fe; --wf-chain-bg:#faf5ff; --wf-chain-ink:#6b21a8; }
      .wf-chain.is-xlayer { --wf-chain-line:#a3e635; --wf-chain-bg:#f7fee7; --wf-chain-ink:#3f6212; }
      .wf-chain.is-unknown { --wf-chain-line:#d0d5dd; --wf-chain-bg:#fff; --wf-chain-ink:#344054; }
      .wf-ca-row .wf-chain-ico { flex:none; }
      .wf-tag.twitter { background:#e8f1ff; color:#175cd3; }
      .wf-tag.web { background:#e7f8f0; color:#087443; }
      .wf-tax { background:#fef3f2; color:#b42318; }
      .wf-tax.buy { background:#ecfdf3; color:#067647; }
      .wf-flag.ok { background:#ecfdf3; color:#067647; }
      .wf-flag.bad { background:#fef3f2; color:#b42318; }
      .wf-context { margin-top:10px; padding:10px 12px; border-radius:14px; background:rgba(127,127,127,.06); border:1px solid var(--line); }
      .wf-context-label { color:var(--muted); font-size:12px; font-weight:700; }
      .wf-context-now { margin:6px 0; color:#087443; font-size:12px; font-weight:700; }
      .wf-context-item { display:grid; grid-template-columns:88px minmax(0,1fr); gap:8px; padding:6px 0; border-top:1px dashed var(--line); font-size:13px; min-width:0; }
      .wf-context-item p { margin:0; min-width:0; overflow-wrap:anywhere; }
      .wf-context-quote { display:block; color:var(--muted); font-size:12px; }
      .wf-xcard { --wf-accent:#07c160; --wf-tint:rgba(7,193,96,.08); display:flex; flex-direction:column; gap:0; margin:0 0 6px; padding:0; border:1px solid var(--line); border-left:3px solid var(--wf-accent); border-radius:12px; background:var(--card); overflow:hidden; box-shadow:0 1px 2px rgba(16,24,40,.05); font:13px/1.5 "Segoe UI","PingFang SC","Microsoft YaHei",sans-serif; }
      .wf-xcard.is-ca { --wf-accent:#e5484d; --wf-tint:rgba(229,72,77,.08); background:linear-gradient(180deg, rgba(229,72,77,.05), var(--card) 65%); }
      .wf-xcard.is-starred { --wf-accent:var(--wf-star, #d97706); --wf-tint:color-mix(in srgb, var(--wf-star, #d97706) 13%, transparent); background:var(--card); }
      .wf-xcard.is-stack-start { margin-bottom:2px; border-radius:12px 12px 6px 6px; }
      .wf-xcard.is-stack-mid { margin-bottom:2px; border-radius:6px; }
      .wf-xcard.is-stack-end { margin-bottom:6px; border-radius:6px 6px 12px 12px; }
      .wf-xhead { display:flex; gap:8px; align-items:center; padding:5px 10px; background:var(--wf-tint); }
      .wf-xhead .wf-avatar { font-size:11px; }
      .wf-xnames { display:flex; flex-wrap:nowrap; gap:2px 5px; align-items:center; flex:1; min-width:0; overflow:hidden; }
      .wf-xcard .wf-name { font-size:13px; padding:0 2px; border-radius:3px; max-width:10em; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
      .wf-xcard .wf-name.is-remarked { font-style:normal; color:#175cd3; background:linear-gradient(transparent 58%, rgba(46,144,250,.24) 58%); }
      .wf-xcard .wf-group { font-style:normal; padding:0 6px; font-size:11px; max-width:8em; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
      .wf-xcard .wf-group.is-remarked { font-style:normal; background:#e8f1ff; color:#175cd3; box-shadow:inset 0 0 0 1.5px #2e90fa; }
      .wf-xcard .wf-in { color:var(--muted); font-size:11px; }
      .wf-starmark { color:var(--wf-star, #d97706); font-size:12px; line-height:1; }
      .wf-xside { position:relative; display:flex; align-items:center; justify-content:flex-end; margin-left:4px; flex:none; min-width:0; min-height:20px; }
      .wf-xcard .wf-fresh { font-size:11px; }
      .wf-xactions { position:absolute; right:0; top:50%; transform:translateY(-50%); display:flex; flex-wrap:nowrap; gap:4px; padding-left:8px; background:var(--wf-tint); opacity:0; pointer-events:none; transition:opacity .15s; }
      .wf-xcard:hover .wf-fresh { visibility:hidden; }
      .wf-xcard:hover .wf-xactions { opacity:1; pointer-events:auto; }
      .wf-xcard .wf-mute { opacity:1; }
      .wf-xbody { padding:1px 10px 2px; }
      .wf-xcard.is-stack-mid, .wf-xcard.is-stack-end { display:grid; grid-template-columns:22px minmax(0,1fr); grid-template-rows:auto auto; column-gap:8px; align-items:start; padding:6px 10px 4px; }
      .wf-xcard.is-stack-mid .wf-xhead, .wf-xcard.is-stack-end .wf-xhead { grid-column:1; grid-row:1 / span 2; padding:0; background:transparent; align-items:flex-start; }
      .wf-xcard.is-stack-mid .wf-xbody, .wf-xcard.is-stack-end .wf-xbody { grid-column:2; grid-row:1; padding:0; }
      .wf-xcard.is-stack-mid .wf-xfoot, .wf-xcard.is-stack-end .wf-xfoot { grid-column:2; grid-row:2; margin:0; padding:1px 0 2px; }
      .wf-xcard .wf-media { width:100%; max-width:100%; max-height:280px; height:auto; object-fit:contain; object-position:left top; margin-top:4px; }
      .wf-xcard .wf-emoji { max-width:120px; max-height:120px; width:auto; margin-top:4px; }
      .wf-xcard .wf-emote { width:22px; height:22px; }
      .wf-xcard .wf-text { margin-top:2px; font-size:14px; }
      .wf-xcard .wf-mcap { padding:8px 10px; border-radius:12px; }
      .wf-xcard .wf-mcap-v { font-size:22px; }
      .wf-xcard .wf-metrics { gap:6px; }
      .wf-xcard .wf-metric { padding:6px 8px; }
      .wf-xcard.is-stack-mid .wf-text, .wf-xcard.is-stack-end .wf-text { margin-top:0; }
      .wf-xcard .wf-reply { display:none; }
      .wf-xcard .wf-quote { border:1px solid rgba(124,58,237,.25); border-left:3px solid #7c3aed; background:rgba(124,58,237,.05); }
      .wf-xcard .wf-quote::before { content:"回复的原消息"; justify-self:start; padding:0 7px; border-radius:6px; background:#7c3aed; color:#fff; font-size:11px; font-weight:700; line-height:18px; }
      .wf-xfoot { display:flex; justify-content:space-between; align-items:center; gap:8px; margin:0 10px; padding:1px 0 5px; border-top:0; color:var(--muted); font-size:11px; }
      .wf-xfoot-meta { opacity:.7; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
      .wf-xfoot-meta time { color:inherit; font-size:inherit; }
      .wf-xfoot-meta i { margin:0 5px; font-style:normal; opacity:.55; }
      .wf-footmark { color:inherit; text-decoration:none; font-weight:600; font-size:10px; white-space:nowrap; }
      a.wf-footmark:hover { opacity:1; text-decoration:underline; }
      .wf-xcopy { flex:none; border:1px solid var(--line); border-radius:999px; background:transparent; color:var(--muted); font:inherit; font-size:11px; padding:0 8px; height:18px; line-height:16px; cursor:pointer; }
      .wf-xcopy:hover { color:var(--ink); border-color:var(--muted); }
      .wf-media-miss { display:inline-block; margin-top:6px; color:var(--muted); }
      :host([data-theme="dark"]) .wf-xcard { --wf-tint:rgba(7,193,96,.12); box-shadow:none; }
      :host([data-theme="dark"]) .wf-xcard.is-ca { --wf-tint:rgba(229,72,77,.14); background:linear-gradient(180deg, rgba(229,72,77,.08), var(--card) 65%); }
      :host([data-theme="dark"]) .wf-xcard.is-starred { --wf-tint:color-mix(in srgb, var(--wf-star, #d97706) 20%, transparent); background:var(--card); }
      :host([data-theme="dark"]) .wf-xcard .wf-name.is-remarked { color:#84adff; background:linear-gradient(transparent 58%, rgba(46,144,250,.3) 58%); }
      :host([data-theme="dark"]) .wf-xcard .wf-group.is-remarked { background:rgba(46,144,250,.22); color:#b2ccff; box-shadow:inset 0 0 0 1.5px #84adff; }
      :host([data-theme="dark"]) .wf-xcard .wf-quote { background:rgba(124,58,237,.1); border-color:rgba(167,139,250,.3); border-left-color:#a78bfa; }
      :host([data-theme="dark"]) .wf-xactions { background:var(--wf-tint); }
      .empty { color:var(--muted); padding:12px 4px; }
      time { color:var(--muted); font-size:12px; flex:none; }
    `;
  }

  function feedGroupIds(settings, caOnly) {
    const list = (caOnly ? settings.ca_feed_groups : settings.all_feed_groups) || [];
    return Array.isArray(list) ? list.filter(Boolean) : [];
  }

  function monitoredGroupIds(settings) {
    return (settings.groups || []).map((item) => item && item.id).filter(Boolean);
  }

  function selectedFeedGroups(stored, monitoredIds) {
    const all = Array.isArray(monitoredIds) ? monitoredIds.filter(Boolean) : [];
    const storedList = Array.isArray(stored) ? stored.filter(Boolean) : [];
    if (!storedList.length) return all.slice();
    return storedList.slice();
  }

  function nextFeedGroups(stored, groupId, monitoredIds) {
    const all = Array.isArray(monitoredIds) ? monitoredIds.filter(Boolean) : [];
    if (!groupId) return [];
    const selected = new Set(selectedFeedGroups(stored, all));
    if (selected.has(groupId)) selected.delete(groupId);
    else selected.add(groupId);
    const next = (selected.size <= all.length ? all.filter((id) => selected.has(id)) : Array.from(selected));
    if (!next.length || (all.length && next.length === all.length && next.every((id) => all.includes(id)))) {
      return [];
    }
    return next;
  }

  function groupListenUsers(settings, groupId) {
    const map = settings.group_users || {};
    if (!Object.prototype.hasOwnProperty.call(map, groupId)) return null;
    const list = map[groupId];
    return Array.isArray(list) ? list.filter(Boolean) : [];
  }

  function senderInFeed(card, settings) {
    if ((settings.blocked_users || []).includes(card.senderUsername)) return false;
    const users = groupListenUsers(settings, card.conversationId);
    if (users === null) return true;
    return users.includes(card.senderUsername);
  }

  function cardInFeed(card, settings, caOnly) {
    if ((settings.blocked_groups || []).includes(card.conversationId)) return false;
    if (!senderInFeed(card, settings)) return false;
    if (caOnly && !(card.addresses || []).length) return false;
    const selected = feedGroupIds(settings, caOnly);
    if (selected.length) return selected.includes(card.conversationId);
    const monitored = monitoredGroupIds(settings);
    if (!monitored.length || card.demo) return true;
    return monitored.includes(card.conversationId);
  }

  root.WechatFeed = {
    cardHtml,
    gmgnCardHtml,
    cardCss,
    restoreHref,
    setNumberFormat,
    numberFormat: currentNumberFormat,
    formatUsd,
    chainBadge,
    setApiBase,
    stackMeta,
    logoSvg,
    escapeText,
    formatTime,
    formatDateTime,
    formatFresh,
    tickFresh,
    startFreshTick,
    syncList,
    hue,
    feedGroupIds,
    cardInFeed,
    senderInFeed,
    groupListenUsers,
    monitoredGroupIds,
    selectedFeedGroups,
    nextFeedGroups,
    starColors: STAR_COLORS,
  };
})(globalThis);
