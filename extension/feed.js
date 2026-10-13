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

  function caChip(address) {
    const text = escapeText(address);
    return `<span class="wf-ca"><code>${text}</code><button type="button" class="wf-ca-copy" data-copy="${text}">复制</button></span>`;
  }

  function knownAddresses(card) {
    return [
      ...(card.tokenCards || []).map((token) => token.address),
      ...(card.cas || []).map((item) => item.address),
      ...(card.addresses || []),
    ];
  }

  // 一次扫描切出地址，避免替换后的 data-copy 属性再被正则命中。
  function highlight(value, addresses) {
    const source = String(value ?? "");
    const extra = [...new Set((addresses || []).map(String))]
      .filter((item) => item && !item.startsWith("0x"))
      .map(escapeRegExp);
    const pattern = new RegExp([...extra, CA_PATTERN].join("|"), "g");
    const plain = (text) => escapeText(text).replace(/\n/g, "<br>");
    let html = "";
    let last = 0;
    for (const match of source.matchAll(pattern)) {
      html += plain(source.slice(last, match.index)) + caChip(match[0]);
      last = match.index + match[0].length;
    }
    return html + plain(source.slice(last));
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

  function kvRow(label, value) {
    if (!value) return "";
    return `<div class="wf-kv"><span class="wf-k">${escapeText(label)}</span><div class="wf-v">${value}</div></div>`;
  }

  function copyChip(address) {
    return address ? caChip(String(address)) : "";
  }

  function tokenHtml(token) {
    const headLogo = token.logo
      ? `<img class="wf-token-logo" src="${escapeText(token.logo)}" alt="" onerror="this.remove()" />`
      : `<span class="wf-token-logo wf-token-fallback">${escapeText((token.symbol || "?").slice(0, 1))}</span>`;
    const hero = token.logo
      ? `<img class="wf-token-hero" src="${escapeText(token.logo)}" alt="" onerror="this.remove()" />`
      : "";
    const symbol = token.symbol ? `$${escapeText(token.symbol)}` : "未知代币";
    const chain = token.chain
      ? `<span class="wf-chain">${escapeText(String(token.chain).toUpperCase())}</span>`
      : "";
    const launch = token.launchpad
      ? `<span class="wf-tag">${escapeText(token.launchpad)}</span>`
      : "";
    const twitter = token.twitter
      ? `<a class="wf-tag twitter" href="https://x.com/${escapeText(token.twitter)}" target="_blank" rel="noreferrer">推特</a>`
      : "";
    const website = token.website
      ? `<a class="wf-tag web" href="${escapeText(token.website)}" target="_blank" rel="noreferrer">官网</a>`
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
    const cap = token.marketCapText && token.marketCapText !== "—" ? escapeText(token.marketCapText) : "";
    const liq = token.liquidityText && token.liquidityText !== "—" ? escapeText(token.liquidityText) : "";
    const holders = token.holders ? String(token.holders) : "";
    const created = formatAbs(token.createdAt);
    const rows = [
      kvRow("合约", copyChip(token.address)),
      kvRow("市值", cap),
      kvRow("池子", liq),
      kvRow("持有", holders),
      kvRow("链接", `${twitter}${website}${telegram}`),
      kvRow("税率", tax),
      kvRow("安全", flags),
      kvRow("发现时间", created ? escapeText(created) : ""),
    ].join("");
    return `<div class="wf-token">
      <div class="wf-token-head">
        ${headLogo}
        <div class="wf-token-meta">
          <strong>${symbol}</strong>
          <em>${escapeText(token.name || "")}</em>
        </div>
        ${chain}${launch}
        <a class="wf-token-link" href="${escapeText(token.url || "#")}" target="_blank" rel="noreferrer">GMGN</a>
      </div>
      <div class="wf-token-body">${rows}</div>
      ${hero}
    </div>`;
  }

  function rawAddressHtml(address) {
    return `<div class="wf-token"><div class="wf-token-body">${kvRow("合约", copyChip(address))}</div></div>`;
  }

  function contextHtml(card, addresses) {
    const before = card.contextBefore || [];
    const after = card.contextAfter || [];
    if (!before.length && !after.length) return "";
    const line = (item, tag) => {
      const quote = item.quoteText
        ? `<em class="wf-context-quote">${escapeText(item.quoteText)}</em>`
        : "";
      return `<div class="wf-context-item ${tag}">
        <time>${escapeText(formatTime(item.createTime))}</time>
        <p>${highlight(item.text || "…", addresses)}${quote}</p>
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
    const src = escapeText(card.mediaUrl || "");
    const full = card.mediaFullUrl ? ` data-wf-full="${escapeText(card.mediaFullUrl)}"` : "";
    if (isLocalHttp(card.mediaUrl)) {
      return `<img class="wf-media" data-wf-src="${src}"${full} alt="[图片]" />`;
    }
    return `<img class="wf-media" src="${src}"${full} alt="[图片]" onerror="this.replaceWith('[图片]')" />`;
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
    const remarkName = card.nameRemarked ? `<span class="wf-badge">备注</span>` : "";
    const remarkGroup = card.groupRemarked ? `<span class="wf-badge">群备注</span>` : "";
    const vip = card.starred ? `<span class="wf-vip">关注</span>` : "";
    const origin = [card.originalName, card.originalGroup].filter(Boolean).join(" · ");
    const originHtml = origin ? `<div class="wf-origin">原名 ${escapeText(origin)}</div>` : "";
    const starLabel = card.starred ? "取消星标" : "星标此人";
    const starClass = card.starred ? "wf-star on" : "wf-star";
    return `
      <div class="wf-who">
        <button type="button" class="${nameClass}" data-remark-user title="点击备注此人">${escapeText(name)}</button>
        ${vip}${remarkName}
        <span class="wf-in">在</span>
        <button type="button" class="${groupClass}" data-remark-group title="点击备注这个群">${escapeText(group)}</button>
        ${remarkGroup}
        ${card.demo ? `<span class="wf-demo">样例</span>` : ""}
        <button type="button" class="${starClass}" data-star title="${starLabel}">${card.starred ? "★" : "☆"}</button>
        <button type="button" class="wf-mute" data-mute>不看此群</button>
      </div>
      ${originHtml}`;
  }

  function quoteBlock(card, addresses) {
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
      <div class="wf-quote-text">${highlight(card.quoteText, addresses)}</div>
    </div>`;
  }

  function bodyHtml(card) {
    const tokens = card.tokenCards || [];
    const known = knownAddresses(card);
    const quote = quoteBlock(card, known);
    const reply = card.quoteName ? `<div class="wf-reply">回复 ${escapeText(card.quoteName)}</div>` : "";
    const tokenStrip = tokens.length
      ? `<div class="wf-tokens">${tokens.map(tokenHtml).join("")}</div>`
      : (card.addresses || []).length
        ? `<div class="wf-tokens">${card.addresses.map(rawAddressHtml).join("")}</div>`
        : "";
    // 有图时「[图片]」占位字就不重复显示了，图加载失败会换回这几个字。
    const onlyPlaceholder = card.mediaUrl && card.renderType === "image" && card.text === "[图片]";
    const text = onlyPlaceholder ? "" : `<div class="wf-text">${highlight(card.text || "", known)}</div>`;
    return `${reply}
      ${text}
      ${card.mediaUrl ? mediaHtml(card) : ""}
      ${tokenStrip}
      ${contextHtml(card, known)}
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

  function cardHtml(card) {
    const tone = hue(card.conversationId || card.displayGroup || "g");
    return `<article class="${cardClass(card)}" data-id="${escapeText(card.id)}" data-group="${escapeText(card.conversationId || "")}" data-user="${escapeText(card.senderUsername || "")}" style="--wf-hue:${tone}">
      ${avatarHtml(card)}
      <div class="wf-main">
        <div class="wf-head">
          ${identityHtml(card)}
          <time>${escapeText(formatTime(card.createTime))}</time>
        </div>
        ${bodyHtml(card)}
      </div>
    </article>`;
  }

  const WECHAT_ICON = `<svg viewBox="0 0 24 24" width="13" height="13" aria-hidden="true"><path fill="currentColor" d="M9 4C5.1 4 2 6.6 2 9.8c0 1.8 1 3.4 2.6 4.5L4 16.5l2.5-1.3c.8.2 1.6.4 2.5.4h.4a5 5 0 0 1-.2-1.4c0-3.1 3-5.6 6.7-5.6h.4C15.6 6 12.6 4 9 4zm-2.5 3.2a.9.9 0 1 1 0 1.8.9.9 0 0 1 0-1.8zm5 0a.9.9 0 1 1 0 1.8.9.9 0 0 1 0-1.8zM16 9.6c-3.3 0-6 2.2-6 4.9s2.7 4.9 6 4.9c.7 0 1.4-.1 2-.3l2 1.1-.5-1.8c1.5-.9 2.5-2.3 2.5-3.9 0-2.7-2.7-4.9-6-4.9zm-2 2.6a.8.8 0 1 1 0 1.6.8.8 0 0 1 0-1.6zm4 0a.8.8 0 1 1 0 1.6.8.8 0 0 1 0-1.6z"/></svg>`;
  const AUTHOR_X = "https://x.com/0xTechMelon";
  const DEFAULT_WATERMARK = "关注 X: @0xTechMelon";

  // 默认水印可点开作者推特；用户自定义的水印只显示文字。
  function watermarkHtml(card, className) {
    const text = card.watermark || DEFAULT_WATERMARK;
    if (text === DEFAULT_WATERMARK) {
      return `<a class="${className}" href="${AUTHOR_X}" target="_blank" rel="noreferrer">${escapeText(text)}</a>`;
    }
    return `<span class="${className}">${escapeText(text)}</span>`;
  }

  function remarkTitle(original, what) {
    return original ? `原名：${original}，点击修改${what}` : `点击给${what === "备注" ? "此人" : "这个群"}写${what}`;
  }

  function gmgnCardHtml(card) {
    const tone = hue(card.conversationId || card.displayGroup || "g");
    const name = card.displayName || card.senderName || "群成员";
    const group = card.displayGroup || card.conversationName || "群聊";
    const copyText = [card.text, card.quoteText ? `「${card.quoteName || "群友"}：${card.quoteText}」` : ""]
      .filter(Boolean)
      .join("\n");
    const nameClass = `wf-name${card.nameRemarked ? " is-remarked" : ""}${card.starred ? " is-starred" : ""}`;
    const groupClass = `wf-group${card.groupRemarked ? " is-remarked" : ""}`;
    return `<article class="${cardClass(card)} wf-xcard" data-id="${escapeText(card.id)}" data-group="${escapeText(card.conversationId || "")}" data-user="${escapeText(card.senderUsername || "")}" style="--wf-hue:${tone}">
      <header class="wf-xhead">
        ${avatarHtml(card, 36)}
        <div class="wf-xnames">
          <button type="button" class="${nameClass}" data-remark-user title="${escapeText(remarkTitle(card.originalName, "备注"))}">${escapeText(name)}</button>
          ${card.starred ? `<span class="wf-starmark">★</span>` : ""}
          <span class="wf-in">在</span>
          <button type="button" class="${groupClass}" data-remark-group title="${escapeText(remarkTitle(card.originalGroup, "群备注"))}">${escapeText(group)}</button>
          <span class="wf-kind">${WECHAT_ICON}微信群</span>
          ${card.demo ? `<span class="wf-demo">样例</span>` : ""}
        </div>
        <div class="wf-xside">
          ${watermarkHtml(card, "wf-mark")}
          <div class="wf-xactions">
            <button type="button" class="wf-star${card.starred ? " on" : ""}" data-star title="${card.starred ? "取消星标" : "星标此人"}">${card.starred ? "★" : "☆"}</button>
            <button type="button" class="wf-mute" data-mute>不看此群</button>
          </div>
        </div>
      </header>
      <div class="wf-xbody">${bodyHtml(card)}</div>
      <footer class="wf-xfoot">
        <span class="wf-xfoot-meta"><time title="${escapeText(formatAbs(card.createTime))}">${escapeText(formatTime(card.createTime))}</time><i>|</i>${watermarkHtml(card, "wf-footmark")}</span>
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
      .wf-who { display:flex; flex-wrap:wrap; gap:6px; align-items:center; min-width:0; }
      .wf-name, .wf-group { border:0; padding:0; background:transparent; font:inherit; cursor:pointer; color:inherit; }
      .wf-name { font-weight:800; font-size:15px; }
      .wf-name.is-starred { color:#b45309; }
      .wf-name.is-remarked, .wf-group.is-remarked { font-style:italic; }
      .wf-name.is-remarked { color:#175cd3; }
      .wf-group { padding:1px 8px; border-radius:999px; background:hsl(var(--wf-hue, 150) 42% 92%); color:hsl(var(--wf-hue, 150) 42% 28%); font-weight:700; }
      .wf-group.is-remarked { color:#175cd3; background:#eef4ff; }
      .wf-badge, .wf-vip, .wf-demo { padding:0 6px; border-radius:999px; font-size:11px; }
      .wf-badge { background:#eef4ff; color:#175cd3; }
      .wf-vip { background:#fff4d6; color:#b54708; }
      .wf-demo { background:#fff4d6; color:#946200; }
      .wf-origin { color:var(--muted); font-size:12px; margin-top:2px; }
      .wf-mute, .wf-star { border:1px solid var(--line); background:transparent; border-radius:999px; font:inherit; font-size:12px; cursor:pointer; padding:0 8px; height:22px; color:var(--muted); }
      .wf-star.on { color:#b54708; border-color:#f6d6a8; background:#fff8eb; }
      .wf-mute { color:#b42318; opacity:.45; }
      .wf-card:hover .wf-mute { opacity:1; }
      .wf-text { margin-top:8px; line-height:1.6; word-break:break-word; }
      .wf-reply { color:var(--muted); font-size:12px; margin-top:4px; }
      .wf-ca { display:inline-flex; align-items:center; gap:4px; max-width:100%; margin:1px 2px; vertical-align:middle; }
      .wf-ca code { padding:1px 6px; border-radius:4px; background:#f8eefb; color:#c11fa8; font:12.5px/1.6 Consolas,"SFMono-Regular",Menlo,monospace; overflow-wrap:anywhere; }
      .wf-ca-copy { flex:none; border:0; padding:1px 4px; border-radius:4px; background:#f3edff; color:#6d28d9; font:inherit; font-size:12px; cursor:pointer; }
      .wf-ca-copy:hover { background:#e9ddff; }
      :host([data-theme="dark"]) .wf-ca code { background:rgba(217,70,239,.14); color:#f0abfc; }
      :host([data-theme="dark"]) .wf-ca-copy { background:rgba(139,92,246,.2); color:#c4b5fd; }
      :host([data-theme="dark"]) .wf-group { background:hsl(var(--wf-hue, 150) 30% 22%); color:hsl(var(--wf-hue, 150) 60% 80%); }
      :host([data-theme="dark"]) .wf-group.is-remarked, :host([data-theme="dark"]) .wf-badge { background:rgba(46,144,250,.18); color:#84adff; }
      :host([data-theme="dark"]) .wf-name.is-remarked { color:#84adff; }
      :host([data-theme="dark"]) .wf-name.is-starred { color:#fdb022; }
      :host([data-theme="dark"]) .wf-vip, :host([data-theme="dark"]) .wf-demo { background:rgba(247,144,9,.18); color:#fdb022; }
      :host([data-theme="dark"]) .wf-star.on { background:rgba(247,144,9,.14); border-color:rgba(247,144,9,.4); color:#fdb022; }
      :host([data-theme="dark"]) .wf-mute { color:#f97066; }
      :host([data-theme="dark"]) .wf-card.is-ca { background:linear-gradient(180deg, rgba(46,144,250,.08), var(--card)); }
      :host([data-theme="dark"]) .wf-token { background:linear-gradient(180deg, rgba(46,144,250,.1), var(--card)); border-color:rgba(46,144,250,.3); }
      :host([data-theme="dark"]) .wf-token-fallback { background:rgba(18,183,106,.2); color:#75e0a7; }
      :host([data-theme="dark"]) .wf-token-link, :host([data-theme="dark"]) .wf-chain { background:#344054; }
      :host([data-theme="dark"]) .wf-tag.twitter { background:rgba(46,144,250,.18); color:#84adff; }
      :host([data-theme="dark"]) .wf-tag.web, :host([data-theme="dark"]) .wf-tax.buy, :host([data-theme="dark"]) .wf-flag.ok { background:rgba(18,183,106,.16); color:#75e0a7; }
      :host([data-theme="dark"]) .wf-tax, :host([data-theme="dark"]) .wf-flag.bad { background:rgba(240,68,56,.16); color:#fda29b; }
      :host([data-theme="dark"]) .wf-flag { background:rgba(255,255,255,.06); }
      :host([data-theme="dark"]) .wf-context-now { color:#47cd89; }
      .wf-media { display:block; max-width:min(220px, 100%); max-height:220px; margin-top:8px; border-radius:12px; object-fit:cover; cursor:zoom-in; }
      .wf-quote { margin-top:8px; padding:8px 10px; border-radius:10px; background:rgba(127,127,127,.08); color:var(--muted); display:grid; gap:6px; }
      .wf-quote-who { display:flex; align-items:center; gap:8px; min-width:0; }
      .wf-quote-who .wf-avatar { width:28px; height:28px; font-size:12px; }
      .wf-quote-name { color:var(--ink); font-weight:700; margin:0; }
      .wf-quote-text { line-height:1.5; }
      .wf-card.is-ca { background: linear-gradient(180deg, #f4f9ff, var(--card)); }
      .wf-tokens { display:grid; gap:8px; margin-top:8px; }
      .wf-token { display:grid; gap:8px; padding:12px; border-radius:16px; background:linear-gradient(180deg, #eef7ff, var(--card)); border:1px solid #cfe3ff; min-width:0; }
      .wf-token-head { display:flex; flex-wrap:wrap; gap:8px; align-items:center; min-width:0; }
      .wf-token-body { display:grid; gap:6px; }
      .wf-kv { display:grid; grid-template-columns:64px 1fr; gap:8px; align-items:start; font-size:13px; }
      .wf-k { color:var(--muted); padding-top:2px; }
      .wf-v { display:flex; flex-wrap:wrap; gap:6px; align-items:center; min-width:0; }
      .wf-token-logo { width:36px; height:36px; border-radius:50%; object-fit:cover; flex:none; }
      .wf-token-fallback { display:grid; place-items:center; background:#d1fadf; color:#087443; font-weight:800; }
      .wf-token-meta { display:grid; min-width:0; flex:1; }
      .wf-token-meta em { color:var(--muted); font-style:normal; font-size:12px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
      .wf-token-hero { width:88px; height:88px; border-radius:14px; object-fit:cover; }
      .wf-token-link, .wf-tag, .wf-tax, .wf-flag, .wf-chain { padding:2px 8px; border-radius:999px; border:0; font:inherit; font-size:12px; text-decoration:none; cursor:pointer; }
      .wf-token-link, .wf-chain { background:#111827; color:#fff; }
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
      .wf-xcard { --wf-accent:#07c160; --wf-tint:rgba(7,193,96,.08); display:flex; flex-direction:column; gap:0; margin:0 0 8px; padding:0; border:1px solid var(--line); border-left:4px solid var(--wf-accent); border-radius:16px; background:var(--card); overflow:hidden; box-shadow:0 1px 3px rgba(16,24,40,.06); font:13px/1.55 "Segoe UI","PingFang SC","Microsoft YaHei",sans-serif; }
      .wf-xcard.is-ca { --wf-accent:#e5484d; --wf-tint:rgba(229,72,77,.08); background:linear-gradient(180deg, rgba(229,72,77,.05), var(--card) 65%); }
      .wf-xcard.is-starred { --wf-accent:var(--wf-star, #d97706); --wf-tint:color-mix(in srgb, var(--wf-star, #d97706) 13%, transparent); background:var(--card); }
      .wf-xhead { display:flex; gap:10px; align-items:center; padding:10px 14px; background:var(--wf-tint); }
      .wf-xhead .wf-avatar { font-size:14px; }
      .wf-xnames { display:flex; flex-wrap:wrap; gap:4px 6px; align-items:center; flex:1; min-width:0; }
      .wf-xcard .wf-name { font-size:14px; padding:0 2px; border-radius:3px; }
      .wf-xcard .wf-name.is-remarked { font-style:normal; color:#175cd3; background:linear-gradient(transparent 58%, rgba(46,144,250,.24) 58%); }
      .wf-xcard .wf-group { font-style:normal; padding:1px 8px; font-size:12px; }
      .wf-xcard .wf-group.is-remarked { font-style:normal; background:#eef4ff; color:#175cd3; box-shadow:inset 0 0 0 1px #b2ccff; }
      .wf-xcard .wf-in { color:var(--muted); font-size:12px; }
      .wf-starmark { color:var(--wf-star, #d97706); font-size:13px; line-height:1; }
      .wf-kind { display:inline-flex; align-items:center; gap:3px; padding:0 6px; height:18px; border-radius:5px; color:#07a35a; background:rgba(7,193,96,.1); font-size:11px; font-weight:700; line-height:1; }
      .wf-xside { position:relative; display:flex; align-items:center; justify-content:flex-end; margin-left:auto; flex:none; min-height:24px; }
      .wf-mark { color:var(--muted); opacity:.7; font-size:12px; font-weight:600; white-space:nowrap; text-decoration:none; transition:opacity .15s; }
      a.wf-mark:hover, a.wf-footmark:hover { opacity:1; text-decoration:underline; }
      .wf-xactions { position:absolute; right:0; top:50%; transform:translateY(-50%); display:flex; gap:6px; opacity:0; pointer-events:none; transition:opacity .15s; }
      .wf-xcard:hover .wf-xactions { opacity:1; pointer-events:auto; }
      .wf-xcard:hover .wf-mark { opacity:0; }
      .wf-xcard .wf-mute { opacity:1; }
      .wf-xbody { padding:2px 14px 10px; }
      .wf-xcard .wf-text { margin-top:6px; font-size:14px; }
      .wf-xcard .wf-reply { display:none; }
      .wf-xcard .wf-quote { border:1px solid rgba(124,58,237,.25); border-left:3px solid #7c3aed; background:rgba(124,58,237,.05); }
      .wf-xcard .wf-quote::before { content:"回复的原消息"; justify-self:start; padding:0 7px; border-radius:6px; background:#7c3aed; color:#fff; font-size:11px; font-weight:700; line-height:18px; }
      .wf-xfoot { display:flex; justify-content:space-between; align-items:center; gap:8px; margin:0 14px; padding:8px 0 10px; border-top:1px solid var(--line); color:var(--muted); font-size:12px; }
      .wf-xfoot-meta { opacity:.75; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
      .wf-xfoot-meta time { color:inherit; font-size:inherit; }
      .wf-xfoot-meta i { margin:0 6px; font-style:normal; opacity:.6; }
      .wf-footmark { color:inherit; text-decoration:none; font-weight:600; }
      .wf-xcopy { flex:none; border:1px solid var(--line); border-radius:999px; background:transparent; color:var(--muted); font:inherit; font-size:12px; padding:1px 10px; cursor:pointer; }
      .wf-xcopy:hover { color:var(--ink); border-color:var(--muted); }
      .wf-media-miss { display:inline-block; margin-top:6px; color:var(--muted); }
      :host([data-theme="dark"]) .wf-xcard { --wf-tint:rgba(7,193,96,.12); box-shadow:none; }
      :host([data-theme="dark"]) .wf-xcard.is-ca { --wf-tint:rgba(229,72,77,.14); background:linear-gradient(180deg, rgba(229,72,77,.08), var(--card) 65%); }
      :host([data-theme="dark"]) .wf-xcard.is-starred { --wf-tint:color-mix(in srgb, var(--wf-star, #d97706) 20%, transparent); background:var(--card); }
      :host([data-theme="dark"]) .wf-kind { color:#3ddc84; background:rgba(7,193,96,.16); }
      :host([data-theme="dark"]) .wf-xcard .wf-name.is-remarked { color:#84adff; background:linear-gradient(transparent 58%, rgba(46,144,250,.3) 58%); }
      :host([data-theme="dark"]) .wf-xcard .wf-group.is-remarked { background:rgba(46,144,250,.16); color:#84adff; box-shadow:inset 0 0 0 1px rgba(132,173,255,.45); }
      :host([data-theme="dark"]) .wf-xcard .wf-quote { background:rgba(124,58,237,.1); border-color:rgba(167,139,250,.3); border-left-color:#a78bfa; }
      .empty { color:var(--muted); padding:12px 4px; }
      time { color:var(--muted); font-size:12px; flex:none; }
    `;
  }

  root.WechatFeed = { cardHtml, gmgnCardHtml, cardCss, escapeText, formatTime, hue, starColors: STAR_COLORS };
})(globalThis);
