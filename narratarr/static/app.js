// Narratarr web UI — no build step, just modules.

import { $, $$, esc, I, api, hue, coverHTML, firstAuthor, fmtBytes, fmtLeft, ago, toast } from "./util.js";
import { player } from "./player.js";


const ACTIVE = ["queued", "resolving", "caching", "downloading", "organizing"];
const STATUS = {
  wanted: "Wanted",
  queued: "Queued",
  resolving: "Resolving",
  caching: "On Real-Debrid",
  downloading: "Downloading",
  organizing: "Organizing",
  completed: "In library",
  failed: "Failed",
};
const PHASES = [
  ["resolving", "Resolve"],
  ["caching", "Real-Debrid"],
  ["downloading", "Download"],
  ["organizing", "Organize"],
];


// ---------------------------------------------------------------------------
// state + api

const state = {
  settings: null,
  sources: [],
  library: [],
  libraryKey: "",
  discover: { q: "", category: "", page: 0, total: 1, items: [], loading: false, error: "", token: 0, dym: "", lang: "", fmt: "", sort: "", source: lastSource() },
  libFilter: "all",
  libSearch: "",
  drawer: null, // { url, bookId }
  view: null,
};


const byUrl = () => new Map(state.library.map((b) => [b.source_url, b]));

// ---------------------------------------------------------------------------
// helpers


function statusTag(status) {
  return `<span class="status-tag s-${status}"><i></i>${esc(STATUS[status] || status)}</span>`;
}

function spinBtn(btn, on) {
  if (on) {
    btn.dataset.html = btn.innerHTML;
    btn.innerHTML = '<span class="spinner"></span>';
    btn.disabled = true;
  } else if (btn.dataset.html) {
    btn.innerHTML = btn.dataset.html;
    btn.disabled = false;
  }
}


function modal({ title, body, confirm = "Confirm", danger = false, extra = "" }) {
  return new Promise((resolve) => {
    const scrim = $("#modal");
    scrim.hidden = false;
    scrim.innerHTML = `
      <div class="modal" role="alertdialog" aria-modal="true" aria-labelledby="modal-title">
        <h3 id="modal-title">${esc(title)}</h3>
        <p>${esc(body)}</p>
        ${extra}
        <div class="actions">
          <button class="btn ghost" data-act="cancel">Cancel</button>
          <button class="btn ${danger ? "danger solid" : "primary"}" data-act="ok">${esc(confirm)}</button>
        </div>
      </div>`;
    const done = (ok) => {
      const extras = Object.fromEntries($$("input", scrim).map((i) => [i.name, i.checked]));
      scrim.hidden = true;
      scrim.innerHTML = "";
      document.removeEventListener("keydown", onKey, true);
      resolve(ok ? { ok, ...extras } : null);
    };
    const onKey = (e) => { if (e.key === "Escape") { e.stopPropagation(); done(false); } };
    document.addEventListener("keydown", onKey, true);
    scrim.onclick = (e) => {
      if (e.target === scrim) return done(false);
      const act = e.target.closest("[data-act]")?.dataset.act;
      if (act) done(act === "ok");
    };
    $('[data-act="ok"]', scrim).focus();
  });
}

function lastSource() {
  try { return localStorage.getItem("narratarr.source") || localStorage.getItem("audiobookeer.source") || "librivox"; } catch { return "librivox"; }
}
const sourceInfo = (name) => state.sources.find((s) => s.name === name) || { name, label: name, delivery: "torrent", categories: [] };
const isDirect = (name) => sourceInfo(name).delivery === "direct";

// Which download client a book needs that isn't set up yet ("" when it can download now).
function missingClient(source, protocol) {
  if (isDirect(source)) return "";
  if (protocol === "usenet") return state.settings?.sabnzbd_configured ? "" : "SABnzbd";
  return state.settings?.rd_configured ? "" : "Real-Debrid";
}

// ---------------------------------------------------------------------------
// library actions (shared by every view)

async function addBook(url, { cover = "", title = "", source = "audiobookbay", protocol = "" } = {}) {
  // Free sources download directly; torrents need Real-Debrid and Usenet releases need SABnzbd.
  const missing = missingClient(source, protocol);
  const download = !missing;
  const book = await api("/api/library", { method: "POST", body: { url, download, source } });
  await refreshLibrary();
  toast(
    download ? `${book.title || title} is on its way` : `Saved ${book.title || title} to Wanted. Connect ${missing} in Settings to download.`,
    { kind: "ok", cover: cover || book.cover_url }
  );
  return book;
}

async function bookAction(id, action) {
  if (action === "remove") {
    const book = state.library.find((b) => b.id === id);
    const res = await modal({
      title: "Remove from library?",
      body: `“${book?.title}” will be removed from Narratarr.`,
      confirm: "Remove",
      danger: true,
      extra: book?.path
        ? `<label class="switch"><input type="checkbox" name="files"><i></i>Also delete the downloaded files</label>`
        : "",
    });
    if (!res) return false;
    if (player.book?.id === id) player.close();
    await api(`/api/library/${id}?delete_files=${!!res.files}`, { method: "DELETE" });
    toast("Removed from library");
  } else {
    const book = state.library.find((b) => b.id === id);
    const missing = action === "download" && book && missingClient(book.source, book.protocol);
    if (missing) {
      toast(`Connect ${missing} in Settings first.`, { kind: "err" });
      return false;
    }
    await api(`/api/library/${id}/${action}`, { method: "POST" });
  }
  await refreshLibrary();
  return true;
}

// ---------------------------------------------------------------------------
// polling

async function refreshLibrary() {
  try {
    state.library = await api("/api/library");
  } catch {
    return;
  }
  const active = state.library.filter((b) => ACTIVE.includes(b.status)).length;
  const badge = $("#activity-badge");
  badge.hidden = !active;
  badge.textContent = active;

  const key = state.library.map((b) => `${b.id}:${b.status}`).join("|");
  const structural = key !== state.libraryKey;
  state.libraryKey = key;
  state.view?.update?.(structural);
  updateDrawerLive();
}

function updateRdPill() {
  const pill = $("#rd-pill");
  const rd = state.settings?.rd_configured;
  const sab = state.settings?.sabnzbd_configured;
  pill.className = `rd-pill ${rd || sab ? "ok" : "missing"}`;
  $("span", pill).textContent = rd ? "Real-Debrid connected" : sab ? "SABnzbd connected" : "Connect Real-Debrid";
}

// ---------------------------------------------------------------------------
// router

const views = { discover: Discover, library: Library, activity: Activity, settings: Settings };

function route() {
  const name = (location.hash.replace(/^#\/?/, "").split("?")[0] || "discover");
  const View = views[name] || Discover;
  $$("[data-nav]").forEach((a) => a.classList.toggle("active", a.dataset.nav === name));
  state.view?.destroy?.();
  const root = $("#view");
  root.classList.remove("enter");
  void root.offsetWidth;
  root.classList.add("enter");
  state.view = View(root);
  window.scrollTo({ top: 0 });
}

// ---------------------------------------------------------------------------
// Discover

function cardHTML(item, lib, i = 0) {
  const h = hue(item.title + item.author);
  const facts = [
    item.format, item.size, item.language !== "English" ? item.language : "",
    item.protocol === "usenet" ? "Usenet" : item.seeders != null ? `${item.seeders} seeder${item.seeders === 1 ? "" : "s"}` : "",
  ].filter(Boolean);
  const active = lib && ACTIVE.includes(lib.status);
  return `
    <article class="card" tabindex="0" data-url="${esc(item.url)}" ${lib ? `data-id="${lib.id}"` : ""} style="--glow:hsl(${h} 80% 55%);animation-delay:${Math.min(i, 12) * 35}ms">
      <div class="cover-wrap">
        ${coverHTML(item)}
        <span class="tag-slot">${lib ? statusTag(lib.status) : ""}</span>
        ${active ? `<div class="bar"><i style="width:${lib.progress || 0}%"></i></div>` : ""}
        ${quickButton(item, lib)}
      </div>
      <div class="meta">
        <div class="title">${esc(item.title)}</div>
        <div class="author">${esc(item.author || (item.indexer ? `via ${item.indexer}` : "Unknown author"))}</div>
        ${facts.length ? `<div class="facts">${facts.map((f) => `<span>${esc(f)}</span>`).join("")}</div>` : ""}
      </div>
    </article>`;
}

function quickButton(item, lib) {
  if (!lib) return `<button class="quick" data-quick aria-label="Download ${esc(item.title)}" title="Download">${I.plus}</button>`;
  if (lib.status === "completed") return `<button class="quick play-quick" data-play="${lib.id}" aria-label="Play ${esc(item.title)}" title="Play">${I.play}</button>`;
  return "";
}

const skeletons = (n) => Array.from({ length: n }, () => '<div class="card skeleton"><div class="cover-wrap"></div><div class="line"></div><div class="line short"></div></div>').join("");

function Discover(root) {
  const d = state.discover;
  if (!state.sources.some((s) => s.name === d.source)) d.source = "librivox";
  const src = sourceInfo(d.source);
  const st = state.settings;
  const needsClient = st && (src.delivery === "torrent" ? !st.rd_configured
    : src.delivery === "indexer" ? !st.rd_configured && !st.sabnzbd_configured : false);
  const greeting = (() => {
    const h = new Date().getHours();
    return h < 5 ? "Late night listening" : h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening";
  })();

  root.innerHTML = `
    <header class="hero">
      <div>
        <div class="eyebrow">${greeting}</div>
        <h1 class="page-title">What are we <em>listening</em> to next?</h1>
      </div>
      <div class="sources" id="sources" role="tablist" aria-label="Where to look">
        ${state.sources.map((s) => `
          <button role="tab" aria-selected="${s.name === d.source}" class="source ${s.name === d.source ? "on" : ""}" data-src="${esc(s.name)}">
            <b>${esc(s.label)}</b><small>${esc({ direct: "Free · direct download", indexer: "Your indexers" }[s.delivery] || "Via Real-Debrid")}</small>
          </button>`).join("")}
      </div>
      <div class="search-wrap">
        <form class="search" id="search-form" role="search">
          ${I.search}
          <input id="q" type="search" placeholder="Search titles, authors, narrators… typos are fine" autocomplete="off" value="${esc(d.q)}" aria-label="Search audiobooks" aria-autocomplete="list" aria-controls="suggest" />
          <kbd>/</kbd>
          <button class="btn primary" type="submit">Search</button>
        </form>
        <div class="suggest" id="suggest" role="listbox" hidden></div>
      </div>
      <div class="chips" id="chips">
        <button class="chip ${!d.q && !d.category ? "on" : ""}" data-cat="">${esc(src.default_label || "Fresh arrivals")}</button>
        ${src.categories.map((c) => `<button class="chip ${!d.q && d.category === c.slug ? "on" : ""}" data-cat="${esc(c.slug)}">${esc(c.name)}</button>`).join("")}
      </div>
    </header>
    ${needsClient ? `
      <div class="banner">
        <div class="ico">${I.key}</div>
        <div><strong>${src.delivery === "indexer" ? "Connect SABnzbd or Real-Debrid to start downloading" : "Connect Real-Debrid to start downloading"}</strong><p>${src.delivery === "indexer"
          ? "SABnzbd handles Usenet results and Real-Debrid handles torrents. You can save books to Wanted until then."
          : "You can browse and save books to Wanted. Downloads start once your API key is in."}</p></div>
        <a class="btn" href="#/settings">Open Settings</a>
      </div>` : ""}
    <div class="section-head"><h2 id="disc-title"></h2><div class="refine" id="refine"></div></div>
    <div id="dym"></div>
    <div class="grid" id="grid"></div>
    <div class="load-more" id="sentinel"></div>`;

  const grid = $("#grid", root);
  const sentinel = $("#sentinel", root);

  function heading() {
    const cat = src.categories.find((c) => c.slug === d.category);
    $("#disc-title", root).innerHTML = d.q
      ? `Results for <em style="font-style:italic">“${esc(d.q)}”</em> <span class="in-src">in ${esc(src.label)}</span>`
      : esc(cat ? cat.name : src.default_label || "Fresh arrivals");
    $("#dym", root).innerHTML = d.dym
      ? `<p class="dym">Including results for <em>${esc(d.dym)}</em> · <button data-literal>Show only close matches for “${esc(d.q)}”</button></p>`
      : "";
    refine();
  }

  // Filter + sort what's loaded, client side.
  function refine() {
    const opts = (vals, cur, all) => `<option value="">${all}</option>` + [...new Set(vals.filter(Boolean))].sort()
      .map((v) => `<option ${v === cur ? "selected" : ""}>${esc(v)}</option>`).join("");
    // The unsorted order is whatever the source gave us: relevance, popularity or recency.
    const listedNewest = !d.q && (src.delivery !== "direct" || d.category === "new");
    const natural = d.q ? "Best match" : listedNewest ? "Newest first" : "Most popular";
    const sorts = [["", natural], ["newest", "Newest first"], ["largest", "Largest"], ["smallest", "Smallest"], ["title", "Title A–Z"]]
      .filter(([v]) => !(v === "newest" && listedNewest));
    const shown = visibleItems().length;
    $("#refine", root).innerHTML = !d.items.length ? "" : `
      <span class="muted">${shown}${shown !== d.items.length ? ` of ${d.items.length}` : ""} books</span>
      <select class="select" data-r="lang" aria-label="Language">${opts(d.items.map((i) => i.language), d.lang, "All languages")}</select>
      <select class="select" data-r="fmt" aria-label="Format">${opts(d.items.map((i) => i.format.toUpperCase()), d.fmt, "All formats")}</select>
      <select class="select" data-r="sort" aria-label="Sort">${sorts.map(([v, l]) => `<option value="${v}" ${v === d.sort ? "selected" : ""}>${l}</option>`).join("")}</select>`;
  }

  const sizeOf = (s) => {
    const m = /([\d.]+)\s*([KMGT]?B)/i.exec(s || "");
    return m ? parseFloat(m[1]) * 1024 ** ["B", "KB", "MB", "GB", "TB"].indexOf(m[2].toUpperCase()) : 0;
  };

  function visibleItems() {
    let items = d.items.filter((i) => (!d.lang || i.language === d.lang) && (!d.fmt || i.format.toUpperCase() === d.fmt));
    const by = {
      newest: (a, b) => (Date.parse(b.posted) || 0) - (Date.parse(a.posted) || 0),
      largest: (a, b) => sizeOf(b.size) - sizeOf(a.size),
      smallest: (a, b) => sizeOf(a.size) - sizeOf(b.size),
      title: (a, b) => a.title.localeCompare(b.title),
    }[d.sort];
    return by ? [...items].sort(by) : items;
  }

  function renderItems() {
    const lib = byUrl();
    const items = visibleItems();
    grid.innerHTML = items.map((it, i) => cardHTML(it, lib.get(it.url), i)).join("");
    if (!items.length && !d.loading) {
      const filtered = d.items.length > 0;
      grid.innerHTML = `<div class="empty" style="grid-column:1/-1">
        <div class="art">${d.error ? I.alert : I.search}</div>
        <h3>${d.error ? "Couldn’t load books" : filtered ? "Nothing matches those filters" : "Nothing found"}</h3>
        <p>${esc(d.error || (filtered ? "Try another language or format." : "Try searching by author, or just part of the title."))}</p>
        ${d.error ? '<button class="btn" data-retry>Try again</button>' : ""}
      </div>`;
    }
  }

  function footer() {
    if (d.loading) sentinel.innerHTML = '<span class="spinner" style="color:var(--text-3)"></span>';
    else if (d.page && d.page < d.total) sentinel.innerHTML = '<button class="btn" data-more>Load more</button>';
    else sentinel.innerHTML = "";
  }

  async function load(reset) {
    if (d.loading && !reset) return;
    const token = ++d.token;
    if (reset) { d.items = []; d.page = 0; d.total = 1; d.error = ""; d.dym = ""; grid.innerHTML = skeletons(12); }
    d.loading = true;
    heading();
    footer();
    const slow = setTimeout(() => {
      if (d.loading && token === d.token) {
        sentinel.innerHTML = '<div style="display:grid;justify-items:center;gap:12px;color:var(--text-3)"><span class="spinner"></span><span>${esc(src.label)} is slow to respond. Still trying…</span></div>';
      }
    }, 6000);
    try {
      const params = new URLSearchParams({ q: d.q, category: d.category, page: d.page + 1, source: d.source });
      const res = await api(`/api/discover?${params}`);
      if (token !== d.token) return;
      const seen = new Set(d.items.map((i) => i.url));
      d.items.push(...res.items.filter((i) => !seen.has(i.url)));
      d.page = res.page;
      d.total = res.total_pages;
      if (reset) d.dym = res.did_you_mean || "";
    } catch (e) {
      if (token !== d.token) return;
      d.error = e.message;
      if (!reset) toast(e.message, { kind: "err" });
    }
    clearTimeout(slow);
    d.loading = false;
    if (!root.isConnected) return;
    renderItems();
    heading();
    footer();
  }

  const input = $("#q", root);
  const box = $("#suggest", root);
  let sugg = [], hi = -1, suggTimer = 0, suggToken = 0;

  function search(q) {
    d.q = q;
    d.lang = d.fmt = d.sort = "";
    if (q) d.category = "";
    $$(".chip", root).forEach((c) => c.classList.toggle("on", !q && c.dataset.cat === d.category));
    hideSuggest();
    load(true);
  }

  function hideSuggest() { clearTimeout(suggTimer); box.hidden = true; hi = -1; suggToken++; }

  function showSuggest() {
    if (!sugg.length || document.activeElement !== input) return hideSuggest();
    box.innerHTML = `<div class="label">Did you mean</div>` + sugg.map((sg, i) => `
      <button type="button" role="option" class="${i === hi ? "on" : ""}" data-s="${i}">
        ${/^https:\/\//.test(sg.cover_url) ? `<img src="${esc(sg.cover_url)}" alt="" referrerpolicy="no-referrer">` : '<span class="ph"></span>'}
        <span style="min-width:0"><b>${esc(sg.title)}</b><small>${esc(sg.author)}</small></span>
      </button>`).join("");
    box.hidden = false;
  }

  const pickSuggestion = (sg) => { input.value = sg.title.split(":")[0]; search(input.value.trim()); };

  input.addEventListener("input", () => {
    clearTimeout(suggTimer);
    const q = input.value.trim();
    if (q.length < 3 || !state.settings?.smart_search) return hideSuggest();
    suggTimer = setTimeout(async () => {
      const token = ++suggToken;
      try {
        const res = await api(`/api/suggest?q=${encodeURIComponent(q)}`);
        if (token !== suggToken) return;
        sugg = res; hi = -1; showSuggest();
      } catch { /* suggestions are a nicety */ }
    }, 300);
  });
  input.addEventListener("keydown", (e) => {
    if (box.hidden) return;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const n = sugg.length;
      hi = e.key === "ArrowDown" ? (hi + 1 >= n ? -1 : hi + 1) : (hi <= -1 ? n - 1 : hi - 1);
      showSuggest();
    } else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); hideSuggest(); }
    else if (e.key === "Enter" && hi >= 0) { e.preventDefault(); pickSuggestion(sugg[hi]); }
  });
  input.addEventListener("blur", () => setTimeout(hideSuggest, 150));
  box.onpointerdown = (e) => e.preventDefault(); // keep focus so the click lands
  box.onclick = (e) => { const b = e.target.closest("[data-s]"); if (b) pickSuggestion(sugg[Number(b.dataset.s)]); };

  $("#search-form", root).onsubmit = (e) => { e.preventDefault(); search(input.value.trim()); };
  input.addEventListener("search", () => { if (!input.value && d.q) search(""); });

  $("#refine", root).onchange = (e) => {
    const r = e.target.dataset.r;
    if (!r) return;
    d[r] = e.target.value;
    renderItems();
    refine();
  };

  $("#sources", root).onclick = (e) => {
    const b = e.target.closest("[data-src]");
    if (!b || b.dataset.src === d.source) return;
    d.source = b.dataset.src;
    try { localStorage.setItem("narratarr.source", d.source); } catch { /* private mode */ }
    // Keep the search (if any) and run it against the new source.
    d.category = "";
    d.items = [];
    d.lang = d.fmt = d.sort = "";
    d.token++;
    d.loading = false;
    route();
  };

  $("#chips", root).onclick = (e) => {
    const chip = e.target.closest(".chip");
    if (!chip) return;
    d.category = chip.dataset.cat;
    d.q = "";
    $("#q", root).value = "";
    $$(".chip", root).forEach((c) => c.classList.toggle("on", c === chip));
    chip.scrollIntoView({ behavior: "smooth", inline: "center", block: "nearest" });
    load(true);
  };

  root.onclick = async (e) => {
    if (e.target.closest("[data-more]")) return load(false);
    if (e.target.closest("[data-retry]")) return load(true);
    if (e.target.closest("[data-literal]")) {
      // Re-run without typo correction by searching the exact phrase.
      d.dym = "";
      d.items = d.items.filter((i) => i.score >= 90);
      renderItems(); heading();
      return;
    }
    const play = e.target.closest("[data-play]");
    if (play) return playBook(Number(play.dataset.play));
    const card = e.target.closest(".card[data-url]");
    if (!card) return;
    const item = d.items.find((i) => i.url === card.dataset.url);
    if (e.target.closest("[data-quick]")) {
      const btn = e.target.closest("[data-quick]");
      spinBtn(btn, true);
      try { await addBook(item.url, { cover: item.cover_url, title: item.title, source: d.source, protocol: item.protocol }); }
      catch (err) { toast(err.message, { kind: "err" }); spinBtn(btn, false); }
      return;
    }
    openDrawer({ url: item.url, preview: item, source: d.source });
  };
  root.onkeydown = (e) => {
    if (e.key === "Enter" && e.target.matches(".card")) e.target.click();
  };

  const io = new IntersectionObserver((entries) => {
    if (entries[0].isIntersecting && !d.loading && d.page && d.page < d.total && !d.error) load(false);
  }, { rootMargin: "600px" });
  io.observe(sentinel);

  if (d.items.length) { renderItems(); heading(); footer(); }
  else load(true);

  return {
    update(structural) {
      if (structural && !d.loading && d.items.length) return renderItems();
      const lib = byUrl();
      $$(".card[data-url]", grid).forEach((card) => {
        const b = lib.get(card.dataset.url);
        const bar = $(".bar i", card);
        if (bar && b) bar.style.width = `${b.progress || 0}%`;
      });
    },
    destroy() { io.disconnect(); },
  };
}

// ---------------------------------------------------------------------------
// Library

function libCardHTML(b, i) {
  const h = hue(b.title + b.author);
  const active = ACTIVE.includes(b.status);
  const listening = b.status === "completed" && !b.finished && b.listen_position > 30 && b.listen_duration;
  return `
    <article class="card" tabindex="0" data-id="${b.id}" style="--glow:hsl(${h} 80% 55%);animation-delay:${Math.min(i, 12) * 30}ms">
      <div class="cover-wrap">
        ${coverHTML(b)}
        ${b.status === "completed" ? "" : statusTag(b.status)}
        ${b.finished ? `<span class="finished-tag" title="Finished">${I.check}</span>` : ""}
        ${active ? `<div class="bar"><i style="width:${b.progress || 0}%"></i></div>` : ""}
        ${listening ? `<div class="listen-bar"><i style="width:${(b.listen_position / b.listen_duration) * 100}%"></i></div>` : ""}
        ${b.status === "completed" ? `<button class="quick play-quick" data-play="${b.id}" aria-label="Play ${esc(b.title)}" title="Play">${I.play}</button>` : ""}
      </div>
      <div class="meta">
        <div class="title">${esc(b.title)}</div>
        <div class="author">${esc(b.author || "Unknown author")}</div>
        <div class="facts">${(listening ? [fmtLeft(b.listen_duration - b.listen_position)] : [b.length ? b.length.replace(/ hours?/, "h").replace(/ and /, " ").replace(/ minutes?/, "m") : "", b.size]).filter(Boolean).map((f) => `<span>${esc(f)}</span>`).join("")}</div>
      </div>
    </article>`;
}

function continueListening() {
  const books = state.library
    .filter((b) => b.status === "completed" && !b.finished && b.listen_position > 30 && b.listen_duration)
    .sort((a, b) => (b.listened_at || 0) - (a.listened_at || 0))
    .slice(0, 8);
  if (!books.length) return "";
  return `
    <div class="section-head"><h2>Continue listening</h2></div>
    <div class="shelf">${books.map((b, i) => `
      <button class="resume" data-play="${b.id}" style="animation-delay:${i * 40}ms">
        ${coverHTML(b)}
        <span style="min-width:0">
          <b>${esc(b.title)}</b>
          <small>${esc(fmtLeft(b.listen_duration - b.listen_position))}</small>
          <span class="progress"><i style="width:${(b.listen_position / b.listen_duration) * 100}%"></i></span>
        </span>
        <span class="go">${I.play}</span>
      </button>`).join("")}
    </div>`;
}

async function playBook(id) {
  const book = state.library.find((b) => b.id === id);
  if (!book) return;
  if (player.book?.id === id) player.toggle();
  else await player.open(book);
}

function Library(root) {
  const filters = [
    ["all", "All", () => true],
    ["completed", "Downloaded", (b) => b.status === "completed"],
    ["active", "In progress", (b) => ACTIVE.includes(b.status)],
    ["wanted", "Wanted", (b) => b.status === "wanted"],
    ["failed", "Failed", (b) => b.status === "failed"],
  ];

  root.innerHTML = `
    <header>
      <div class="eyebrow">Your collection</div>
      <h1 class="page-title">The <em>Library</em></h1>
    </header>
    <div id="shelf"></div>
    <div class="stats" id="stats"></div>
    <div class="toolbar">
      <div class="segmented" id="filters"></div>
      <input class="input" id="lib-search" type="search" placeholder="Filter by title or author" value="${esc(state.libSearch)}" aria-label="Filter library" />
    </div>
    <div class="grid" id="lib-grid"></div>`;

  function render() {
    const lib = state.library;
    const count = (fn) => lib.filter(fn).length;
    $("#shelf", root).innerHTML = continueListening();
    $("#stats", root).innerHTML = [
      [lib.length, "Books in library"],
      [count((b) => b.status === "completed"), "Downloaded"],
      [count((b) => ACTIVE.includes(b.status)), "In progress"],
      [count((b) => b.status === "wanted"), "Wanted"],
    ].map(([n, l]) => `<div class="stat"><b>${n}</b><span>${l}</span></div>`).join("");

    $("#filters", root).innerHTML = filters.map(([k, label, fn]) =>
      `<button class="${state.libFilter === k ? "on" : ""}" data-f="${k}">${label} <small>${count(fn)}</small></button>`).join("");

    const fn = filters.find((f) => f[0] === state.libFilter)[2];
    const q = state.libSearch.toLowerCase();
    const books = lib.filter(fn).filter((b) => !q || `${b.title} ${b.author} ${b.narrators.join(" ")}`.toLowerCase().includes(q));
    const grid = $("#lib-grid", root);

    if (!lib.length) {
      grid.innerHTML = `<div class="empty" style="grid-column:1/-1">
        <div class="art">${I.books}</div>
        <h3>Your shelves are empty</h3>
        <p>Find something on Discover and it’ll land here, organized and ready to listen to.</p>
        <a class="btn primary" href="#/discover">${I.search} Start discovering</a></div>`;
    } else if (!books.length) {
      grid.innerHTML = `<div class="empty" style="grid-column:1/-1"><div class="art">${I.search}</div><h3>No matches</h3><p>Nothing in this view matches your filter.</p></div>`;
    } else {
      grid.innerHTML = books.map(libCardHTML).join("");
    }
  }

  $("#filters", root).onclick = (e) => {
    const b = e.target.closest("[data-f]");
    if (!b) return;
    state.libFilter = b.dataset.f;
    render();
  };
  $("#lib-search", root).oninput = (e) => { state.libSearch = e.target.value; render(); };
  root.onclick = (e) => {
    const play = e.target.closest("[data-play]");
    if (play) return playBook(Number(play.dataset.play));
    const card = e.target.closest(".card[data-id]");
    if (card) openDrawer({ bookId: Number(card.dataset.id) });
  };
  root.onkeydown = (e) => { if (e.key === "Enter" && e.target.matches(".card")) e.target.click(); };

  let listenSig = "";
  render();
  return {
    update(structural) {
      const sig = state.library.map((b) => `${b.id}:${b.finished}:${Math.round(b.listen_position / 60)}`).join("|");
      if (structural || sig !== listenSig) { listenSig = sig; return render(); }
      for (const b of state.library) {
        const bar = $(`.card[data-id="${b.id}"] .bar i`, root);
        if (bar) bar.style.width = `${b.progress || 0}%`;
      }
    },
  };
}

// ---------------------------------------------------------------------------
// Activity

function jobDetail(b) {
  const parts = [];
  if (b.status === "queued") parts.push("Waiting for a free slot…");
  else if (b.detail) parts.push(esc(b.detail));
  if (b.speed && ["caching", "downloading"].includes(b.status)) parts.push(`${fmtBytes(b.speed)}/s`);
  if (["caching", "downloading"].includes(b.status)) parts.push(`${(b.progress || 0).toFixed(1)}%`);
  return parts.map((p) => `<span>${p}</span>`).join("");
}

function stepsHTML(status, source, protocol) {
  const phases = protocol === "usenet" ? [["resolving", "Resolve"], ["downloading", "SABnzbd"], ["organizing", "Organize"]]
    : isDirect(source) ? PHASES.filter(([s]) => s !== "caching") : PHASES;
  const idx = phases.findIndex(([s]) => s === status);
  return `<div class="steps">${phases.map(([, label], i) =>
    `<span class="step ${idx > i ? "done" : idx === i ? "now" : ""}"><i></i>${label}</span>`).join("")}</div>`;
}

function Activity(root) {
  root.innerHTML = `
    <header>
      <div class="eyebrow">Live</div>
      <h1 class="page-title">Activity</h1>
      <p class="lede">Every download, from search result to your library shelf.</p>
    </header>
    <div class="section-head"><h2>In progress</h2><span class="muted" id="act-count"></span></div>
    <div class="jobs" id="jobs"></div>
    <div id="hist-wrap"></div>`;

  function render() {
    const active = state.library.filter((b) => ACTIVE.includes(b.status))
      .sort((a, b) => ACTIVE.indexOf(b.status) - ACTIVE.indexOf(a.status) || a.updated_at - b.updated_at);
    const recent = state.library.filter((b) => ["completed", "failed"].includes(b.status))
      .sort((a, b) => (b.completed_at || b.updated_at) - (a.completed_at || a.updated_at)).slice(0, 25);

    $("#act-count", root).textContent = active.length ? `${active.length} active` : "";
    $("#jobs", root).innerHTML = active.length ? active.map((b, i) => `
      <div class="job" data-id="${b.id}" style="animation-delay:${i * 40}ms">
        ${coverHTML(b)}
        <div style="min-width:0">
          <h3>${esc(b.title)}</h3>
          <div class="by">${esc(b.author)}</div>
          ${stepsHTML(b.status, b.source, b.protocol)}
          <div class="progress ${["caching", "downloading"].includes(b.status) ? "" : "indeterminate"}"><i style="width:${b.progress || 0}%"></i></div>
          <div class="detail">${jobDetail(b)}</div>
        </div>
        <div class="actions"><button class="btn sm" data-act="stop">${I.stop} Stop</button></div>
      </div>`).join("") : `
      <div class="empty" style="padding:50px 20px">
        <div class="art">${I.headphones}</div>
        <h3>All quiet</h3>
        <p>Nothing downloading right now. Queue up your next listen from Discover.</p>
        <a class="btn" href="#/discover">Browse books</a>
      </div>`;

    $("#hist-wrap", root).innerHTML = recent.length ? `
      <div class="section-head"><h2>Recently finished</h2></div>
      <div class="history">${recent.map((b) => `
        <div class="row" data-id="${b.id}">
          ${coverHTML(b)}
          <div style="min-width:0"><div class="t">${esc(b.title)}</div><div class="s">${esc(b.status === "failed" ? b.error : b.author)}</div></div>
          <div class="when">${statusTag(b.status)}<span>${ago(b.completed_at || b.updated_at)}</span></div>
        </div>`).join("")}</div>` : "";
  }

  root.onclick = async (e) => {
    const job = e.target.closest("[data-id]");
    if (!job) return;
    const id = Number(job.dataset.id);
    const btn = e.target.closest("[data-act]");
    if (btn) {
      spinBtn(btn, true);
      try { await bookAction(id, btn.dataset.act); } catch (err) { toast(err.message, { kind: "err" }); spinBtn(btn, false); }
      return;
    }
    openDrawer({ bookId: id });
  };

  render();
  return {
    update(structural) {
      if (structural) return render();
      for (const b of state.library.filter((x) => ACTIVE.includes(x.status))) {
        const el = $(`.job[data-id="${b.id}"]`, root);
        if (!el) continue;
        $(".progress i", el).style.width = `${b.progress || 0}%`;
        $(".detail", el).innerHTML = jobDetail(b);
      }
    },
  };
}

// ---------------------------------------------------------------------------
// Settings

function Settings(root) {
  const s = state.settings || {};
  root.innerHTML = `
    <header>
      <div class="eyebrow">Preferences</div>
      <h1 class="page-title">Settings</h1>
    </header>
    <form id="settings" class="settings" autocomplete="off">
      <section class="panel">
        <div><h3>Real-Debrid</h3><p class="desc">Fetches the files for you. Find your key at <a href="https://real-debrid.com/apitoken" target="_blank" rel="noopener">real-debrid.com/apitoken</a>.</p></div>
        <div class="body">
          <div class="field">
            <label for="rd_api_key">API key</label>
            <div class="input-row">
              <input class="input mono" id="rd_api_key" name="rd_api_key" type="password" placeholder="Paste your private API token" value="${esc(s.rd_api_key)}" />
              <button class="btn" type="button" id="test-rd">Test</button>
            </div>
          </div>
          <div id="rd-result"></div>
          <label class="switch"><input type="checkbox" name="delete_from_rd_after" ${s.delete_from_rd_after ? "checked" : ""}><i></i>Remove torrents from Real-Debrid after downloading</label>
        </div>
      </section>

      <section class="panel">
        <div><h3>Library</h3><p class="desc">Where finished audiobooks go, and how folders are named.</p></div>
        <div class="body">
          <div class="field">
            <label for="library_dir">Library folder</label>
            <input class="input mono" id="library_dir" name="library_dir" value="${esc(s.library_dir)}" />
          </div>
          <div class="field">
            <label for="folder_template">Folder layout</label>
            <input class="input mono" id="folder_template" name="folder_template" value="${esc(s.folder_template)}" />
            <span class="hint">Use <code>{author}</code>, <code>{title}</code> and <code>{narrator}</code>. Separate folders with <code>/</code>.</span>
            <div class="preview-path" id="path-preview"></div>
          </div>
          <div class="field">
            <label for="max_concurrent">Simultaneous downloads</label>
            <input class="input" id="max_concurrent" name="max_concurrent" type="number" min="1" max="8" value="${esc(s.max_concurrent)}" style="width:120px" />
          </div>
        </div>
      </section>

      <section class="panel">
        <div><h3>Source</h3><p class="desc">The site Discover browses. Change this if the domain moves.</p></div>
        <div class="body">
          <div class="field">
            <label for="abb_base_url">AudiobookBay address</label>
            <input class="input mono" id="abb_base_url" name="abb_base_url" value="${esc(s.abb_base_url)}" />
          </div>
          <div class="field">
            <label>Search with</label>
            <div class="segmented" id="provider" role="radiogroup" aria-label="Search provider">
              <button type="button" role="radio" data-v="audiobookbay">AudiobookBay’s search</button>
              <button type="button" role="radio" data-v="brave">Brave Search</button>
            </div>
            <input type="hidden" name="search_provider" value="${esc(s.search_provider)}" />
            <span class="hint" id="provider-hint"></span>
          </div>
          <div class="field" id="brave-field">
            <label for="brave_api_key">Brave Search API key</label>
            <div class="input-row">
              <input class="input mono" id="brave_api_key" name="brave_api_key" type="password" placeholder="Paste your Brave Search API key" value="${esc(s.brave_api_key)}" />
              <button class="btn" type="button" id="test-brave">Test</button>
            </div>
            <span class="hint">Get one at <a href="https://api-dashboard.search.brave.com" target="_blank" rel="noopener" style="color:var(--accent)">api-dashboard.search.brave.com</a>. Each search uses one query from your Brave plan.</span>
            <div id="brave-result"></div>
          </div>
          <div class="field">
            <label class="switch"><input type="checkbox" name="smart_search" ${s.smart_search ? "checked" : ""}><i></i>Smart search</label>
            <span class="hint">Fixes typos and suggests titles as you type, using Apple’s public audiobook catalog. What you type in the search box is sent to Apple.</span>
          </div>
        </div>
      </section>

      <section class="panel">
        <div><h3>Prowlarr</h3><p class="desc">Optional. Search the indexers you’ve added to Prowlarr. Usenet results download through SABnzbd, torrents through Real-Debrid.</p></div>
        <div class="body">
          <div class="field"><label for="prowlarr_url">Prowlarr address</label><input class="input mono" id="prowlarr_url" name="prowlarr_url" placeholder="http://localhost:9696" value="${esc(s.prowlarr_url)}" /></div>
          <div class="field">
            <label for="prowlarr_api_key">API key</label>
            <div class="input-row">
              <input class="input mono" id="prowlarr_api_key" name="prowlarr_api_key" type="password" placeholder="Prowlarr → Settings → General → API Key" value="${esc(s.prowlarr_api_key)}" />
              <button class="btn" type="button" id="test-prowlarr">Test</button>
            </div>
            <div id="prowlarr-result"></div>
          </div>
          <div class="field">
            <label for="prowlarr_categories">Categories</label>
            <input class="input mono" id="prowlarr_categories" name="prowlarr_categories" value="${esc(s.prowlarr_categories)}" style="max-width:220px" />
            <span class="hint"><code>3030</code> is Audio › Audiobook. Separate several with commas.</span>
          </div>
        </div>
      </section>

      <section class="panel">
        <div><h3>SABnzbd</h3><p class="desc">Optional. Downloads the Usenet results you pick from Prowlarr, then Narratarr files them into your library.</p></div>
        <div class="body">
          <div class="field"><label for="sabnzbd_url">SABnzbd address</label><input class="input mono" id="sabnzbd_url" name="sabnzbd_url" placeholder="http://localhost:8080" value="${esc(s.sabnzbd_url)}" /></div>
          <div class="field">
            <label for="sabnzbd_api_key">API key</label>
            <div class="input-row">
              <input class="input mono" id="sabnzbd_api_key" name="sabnzbd_api_key" type="password" placeholder="SABnzbd → Config → General → API Key" value="${esc(s.sabnzbd_api_key)}" />
              <button class="btn" type="button" id="test-sabnzbd">Test</button>
            </div>
            <div id="sabnzbd-result"></div>
          </div>
          <div class="field">
            <label for="sabnzbd_category">Category</label>
            <input class="input mono" id="sabnzbd_category" name="sabnzbd_category" placeholder="audiobooks" value="${esc(s.sabnzbd_category)}" style="max-width:220px" />
            <span class="hint">Optional. Must already exist in SABnzbd. Leave empty to use its default.</span>
          </div>
          <div class="field">
            <label for="sabnzbd_remote_path">Path mapping</label>
            <div class="input-row">
              <input class="input mono" id="sabnzbd_remote_path" name="sabnzbd_remote_path" placeholder="SABnzbd’s folder, e.g. /downloads" value="${esc(s.sabnzbd_remote_path)}" />
              <input class="input mono" id="sabnzbd_local_path" name="sabnzbd_local_path" placeholder="Same folder here, e.g. /data/usenet" value="${esc(s.sabnzbd_local_path)}" />
            </div>
            <span class="hint">Only needed when SABnzbd and Narratarr see the downloads folder at different paths, such as separate Docker containers.</span>
          </div>
        </div>
      </section>

      <section class="panel">
        <div><h3>Audiobookshelf</h3><p class="desc">Optional. Tells your Audiobookshelf server to rescan after each download.</p></div>
        <div class="body">
          <div class="field"><label for="audiobookshelf_url">Server URL</label><input class="input mono" id="audiobookshelf_url" name="audiobookshelf_url" placeholder="http://localhost:13378" value="${esc(s.audiobookshelf_url)}" /></div>
          <div class="field"><label for="audiobookshelf_token">API token</label><input class="input mono" id="audiobookshelf_token" name="audiobookshelf_token" type="password" value="${esc(s.audiobookshelf_token)}" /></div>
          <div class="field"><label for="audiobookshelf_library_id">Library ID</label><input class="input mono" id="audiobookshelf_library_id" name="audiobookshelf_library_id" value="${esc(s.audiobookshelf_library_id)}" /></div>
        </div>
      </section>

      <div class="save-bar"><button class="btn primary" type="submit" id="save">${I.check} Save changes</button></div>
    </form>`;

  const form = $("#settings", root);
  const preview = () => {
    const dir = form.library_dir.value.replace(/\/+$/, "");
    const sample = { author: "Frank Herbert", title: "Dune", narrator: "Scott Brick" };
    const rel = (form.folder_template.value || "{author}/{title}").replace(/\{(\w+)\}/g, (_, k) => sample[k] ?? `{${k}}`);
    $("#path-preview", root).innerHTML = `${esc(dir)}/<b>${esc(rel)}</b>/Dune.m4b`;
  };
  form.folder_template.oninput = preview;
  form.library_dir.oninput = preview;
  preview();

  const hints = {
    audiobookbay: "Searches the site directly. Free, but needs exact spelling (smart search fixes that) and stops working when the site is down.",
    brave: "Searches Brave’s copy of the site. Faster, fixes spelling on its own, and keeps working when AudiobookBay is slow. Very new uploads may not be indexed yet.",
  };
  const setProvider = (v) => {
    form.search_provider.value = v;
    $$("#provider button", root).forEach((b) => {
      b.classList.toggle("on", b.dataset.v === v);
      b.setAttribute("aria-checked", b.dataset.v === v);
    });
    $("#provider-hint", root).textContent = hints[v];
    $("#brave-field", root).hidden = v !== "brave";
  };
  setProvider(s.search_provider || "audiobookbay");
  $("#provider", root).onclick = (e) => { const b = e.target.closest("[data-v]"); if (b) setProvider(b.dataset.v); };

  const collect = () => {
    const data = Object.fromEntries(new FormData(form));
    data.delete_from_rd_after = form.delete_from_rd_after.checked;
    data.smart_search = form.smart_search.checked;
    data.max_concurrent = Number(data.max_concurrent) || 1;
    return data;
  };

  async function save(quiet) {
    state.settings = await api("/api/settings", { method: "PUT", body: collect() });
    form.rd_api_key.value = state.settings.rd_api_key;
    form.audiobookshelf_token.value = state.settings.audiobookshelf_token;
    form.brave_api_key.value = state.settings.brave_api_key;
    form.prowlarr_api_key.value = state.settings.prowlarr_api_key;
    form.sabnzbd_api_key.value = state.settings.sabnzbd_api_key;
    state.sources = await api("/api/sources"); // the Prowlarr tab appears once it's set up
    updateRdPill();
    if (!quiet) toast("Settings saved", { kind: "ok" });
  }

  form.onsubmit = async (e) => {
    e.preventDefault();
    const btn = $("#save", root);
    spinBtn(btn, true);
    try { await save(); } catch (err) { toast(err.message, { kind: "err" }); }
    spinBtn(btn, false);
  };

  $("#test-brave", root).onclick = async (e) => {
    const btn = e.currentTarget;
    const out = $("#brave-result", root);
    spinBtn(btn, true);
    try {
      await save(true);
      const r = await api("/api/settings/test-brave", { method: "POST" });
      out.innerHTML = `<div class="rd-result">${I.check.replace("<svg", '<svg width="18" height="18" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"')}
        <span>Brave is working · found <b>${r.results}</b> AudiobookBay results for “dune”</span></div>`;
    } catch (err) {
      out.innerHTML = `<div class="rd-result bad">${esc(err.message)}</div>`;
    }
    spinBtn(btn, false);
  };

  const okLine = (html) => `<div class="rd-result">${I.check.replace("<svg", '<svg width="18" height="18" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"')}<span>${html}</span></div>`;
  const tester = (name, describe) => {
    $(`#test-${name}`, root).onclick = async (e) => {
      const btn = e.currentTarget;
      const out = $(`#${name}-result`, root);
      spinBtn(btn, true);
      try {
        await save(true);
        out.innerHTML = okLine(describe(await api(`/api/settings/test-${name}`, { method: "POST" })));
      } catch (err) {
        out.innerHTML = `<div class="rd-result bad">${esc(err.message)}</div>`;
      }
      spinBtn(btn, false);
    };
  };
  tester("prowlarr", (r) => `Connected to Prowlarr ${esc(r.version)} · <b>${r.indexers}</b> indexer${r.indexers === 1 ? "" : "s"} enabled`);
  tester("sabnzbd", (r) => `Connected to SABnzbd ${esc(r.version)}`);

  $("#test-rd", root).onclick = async (e) => {
    const btn = e.currentTarget;
    const out = $("#rd-result", root);
    spinBtn(btn, true);
    try {
      await save(true);
      const u = await api("/api/settings/test-rd", { method: "POST" });
      const exp = u.expiration ? new Date(u.expiration).toLocaleDateString(undefined, { dateStyle: "medium" }) : "";
      out.innerHTML = `<div class="rd-result">${I.check.replace("<svg", '<svg width="18" height="18" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"')}
        <span>Connected as <b>${esc(u.username)}</b> · ${esc(u.type)}${exp ? ` until ${esc(exp)}` : ""}</span></div>`;
    } catch (err) {
      out.innerHTML = `<div class="rd-result bad">${esc(err.message)}</div>`;
    }
    spinBtn(btn, false);
  };

  return {};
}

// ---------------------------------------------------------------------------
// Drawer (book details)

const drawer = $("#drawer");
const scrim = $("#drawer-scrim");

function closeDrawer() {
  drawer.classList.remove("open");
  scrim.classList.remove("open");
  document.body.style.overflow = "";
  state.drawer = null;
}
scrim.onclick = closeDrawer;

function drawerBook() {
  const d = state.drawer;
  if (!d) return null;
  return d.bookId ? state.library.find((b) => b.id === d.bookId) : byUrl().get(d.url);
}

function liveHTML(lib) {
  if (!lib) {
    return `<div class="cta">
      <button class="btn primary" data-act="add">${I.down} Download</button>
      <button class="btn" data-act="want">${I.bookmark} Add to Wanted</button>
    </div>`;
  }
  if (ACTIVE.includes(lib.status)) {
    const measurable = ["caching", "downloading"].includes(lib.status);
    return `<div class="state">
        <div class="top"><span>${esc(STATUS[lib.status])}</span><span>${measurable ? `${(lib.progress || 0).toFixed(0)}%${lib.speed ? ` · ${fmtBytes(lib.speed)}/s` : ""}` : ""}</span></div>
        <div class="progress ${measurable ? "" : "indeterminate"}"><i style="width:${lib.progress || 0}%"></i></div>
        <div class="top" style="margin-top:10px"><span style="color:var(--text-3);font-size:12.5px">${esc(lib.status === "queued" ? "Waiting for a free slot…" : lib.detail)}</span></div>
      </div>
      <div class="cta"><button class="btn" data-act="stop">${I.stop} Stop</button></div>`;
  }
  if (lib.status === "completed") {
    const current = player.book?.id === lib.id;
    const left = lib.listen_duration - lib.listen_position;
    const label = current && player.playing ? `${I.pause} Pause`
      : !lib.finished && lib.listen_position > 30 && lib.listen_duration ? `${I.play} Resume · ${esc(fmtLeft(left))}`
      : `${I.play} ${lib.finished ? "Listen again" : "Play"}`;
    return `<div class="cta">
        <button class="btn primary" data-act="play">${label}</button>
        <button class="btn ghost" data-act="toggle-finished">${lib.finished ? "Mark unfinished" : "Mark finished"}</button>
      </div>
      <div class="state done">Downloaded ${esc(ago(lib.completed_at))} to<code>${esc(lib.path)}</code></div>
      <div class="cta" style="margin-top:14px">
        <button class="btn sm ghost" data-act="download">${I.retry} Download again</button>
        <button class="btn sm ghost danger" data-act="remove">${I.trash} Remove</button>
      </div>`;
  }
  if (lib.status === "failed") {
    return `<div class="state failed">${esc(lib.error || "Something went wrong.")}</div>
      <div class="cta">
        <button class="btn primary" data-act="download">${I.retry} Try again</button>
        <button class="btn ghost danger" data-act="remove">${I.trash} Remove</button>
      </div>`;
  }
  return `<div class="cta">
      <button class="btn primary" data-act="download">${I.down} Download now</button>
      <button class="btn ghost danger" data-act="remove">${I.trash} Remove</button>
    </div>`;
}

function updateDrawerLive() {
  const live = $("#drawer-live");
  if (!live || !state.drawer || state.drawer.loading) return; // keep the spinner until details arrive
  const html = liveHTML(drawerBook());
  if (live.dataset.sig === html) return;
  live.dataset.sig = html;
  live.innerHTML = html;
}

function drawerHTML(b, loading) {
  const narrators = b.narrators || [];
  const pills = [
    b.length && [I.clock, b.length],
    b.format && [I.file, b.format],
    b.bitrate && b.bitrate !== "?" && [I.wave, b.bitrate],
    b.size && [I.disc, b.size],
    b.language && [I.globe, b.language],
    isDirect(b.source) && [I.check, "Free & legal"],
    b.protocol === "usenet" && [I.down, "Usenet"],
    b.protocol === "torrent" && [I.down, b.seeders != null ? `Torrent · ${b.seeders} seeder${b.seeders === 1 ? "" : "s"}` : "Torrent"],
  ].filter(Boolean);
  const desc = (b.description || "").trim();
  return `
    <div class="backdrop"><div></div></div>
    <button class="btn icon close" data-close aria-label="Close">${I.x}</button>
    <div class="drawer-body">
      ${coverHTML(b, "hero-cover")}
      <h2>${esc(b.title)}</h2>
      <div class="by">by <strong>${esc(b.author || "Unknown author")}</strong></div>
      ${narrators.length ? `<div class="narr">Narrated by ${esc(narrators.slice(0, 3).join(", "))}${narrators.length > 3 ? ` +${narrators.length - 3} more` : ""}</div>` : ""}
      ${pills.length ? `<div class="pills">${pills.map(([ic, t]) => `<span class="pill">${ic}${esc(t)}</span>`).join("")}</div>` : ""}
      <div id="drawer-live">${loading ? '<div class="cta"><span class="spinner" style="color:var(--text-3)"></span></div>' : ""}</div>
      ${desc ? `<div class="about"><h4>About this book</h4><p class="${desc.length > 520 ? "clamped" : ""}">${esc(desc)}</p>${desc.length > 520 ? '<button class="more" data-more>Read more</button>' : ""}</div>` : ""}
      ${!loading ? `<dl>
        ${(b.categories || []).length ? `<dt>Genres</dt><dd>${esc(b.categories.join(", "))}</dd>` : ""}
        ${b.posted ? `<dt>Posted</dt><dd>${esc(b.posted)}</dd>` : ""}
        ${b.info_hash ? `<dt>Info hash</dt><dd class="mono">${esc(b.info_hash)}</dd>` : ""}
        ${(b.files || []).length ? `<dt>Files</dt><dd>${b.files.length} ${esc(b.format || "audio")} file${b.files.length === 1 ? "" : "s"}${b.size ? ` · ${esc(b.size)}` : ""}</dd>` : ""}
        ${b.release ? `<dt>Release</dt><dd class="mono">${esc(b.release)}</dd>` : ""}
        ${b.indexer ? `<dt>Indexer</dt><dd>${esc(b.indexer)}</dd>` : ""}
        <dt>Source</dt><dd>${/^https?:/.test(b.url || b.source_url || "")
          ? `<a href="${esc(b.url || b.source_url)}" target="_blank" rel="noopener noreferrer">${esc(sourceInfo(b.source).label)}${isDirect(b.source) ? " on Internet Archive" : ""} ↗</a>`
          : esc(sourceInfo(b.source).label)}</dd>
      </dl>` : ""}
    </div>`;
}

function paintDrawer(b, loading) {
  drawer.innerHTML = drawerHTML(b, loading);
  const bg = $(".backdrop div", drawer);
  if (/^https?:\/\//.test(b.cover_url || "")) bg.style.backgroundImage = `url(${JSON.stringify(b.cover_url)})`;
  else bg.style.background = `hsl(${hue(b.title + b.author)} 60% 40%)`;
  if (!loading) updateDrawerLive();
}

async function openDrawer({ url, bookId, preview, source }) {
  state.drawer = { url, bookId, source, protocol: preview?.protocol || "" };
  drawer.classList.add("open");
  scrim.classList.add("open");
  document.body.style.overflow = "hidden";
  drawer.scrollTop = 0;

  const lib = drawerBook();
  if (lib) {
    // Library books already have everything we need.
    state.drawer.url = lib.source_url;
    state.drawer.source = lib.source;
    state.drawer.protocol = lib.protocol;
    paintDrawer(lib, false);
    $("[data-close]", drawer)?.focus({ preventScroll: true });
    return;
  }

  state.drawer.loading = true;
  paintDrawer({ title: "Loading…", author: "", ...preview, source }, true);
  $("[data-close]", drawer)?.focus({ preventScroll: true });
  try {
    const details = await api(`/api/details?${new URLSearchParams({ url, source: source || "" })}`);
    if (state.drawer?.url !== url) return;
    state.drawer.loading = false;
    // Keep listing facts (like size) when the book page leaves them blank.
    const merged = { ...preview };
    for (const [k, v] of Object.entries(details)) if (v !== "" && !(Array.isArray(v) && !v.length)) merged[k] = v;
    merged.library = details.library;
    paintDrawer(merged, false);
  } catch (e) {
    if (state.drawer?.url !== url) return;
    state.drawer.loading = false;
    $("#drawer-live").innerHTML = `<div class="state failed">${esc(e.message)}</div>`;
  }
}

drawer.onclick = async (e) => {
  if (e.target.closest("[data-close]")) return closeDrawer();
  if (e.target.closest("[data-more]")) {
    const p = $(".about p", drawer);
    p.classList.remove("clamped");
    e.target.remove();
    return;
  }
  const btn = e.target.closest("[data-act]");
  if (!btn || !state.drawer) return;
  const act = btn.dataset.act;
  spinBtn(btn, true);
  try {
    if (act === "add" || act === "want") {
      const url = state.drawer.url;
      if (act === "want") {
        await api("/api/library", { method: "POST", body: { url, download: false, source: state.drawer.source } });
        await refreshLibrary();
        toast("Saved to Wanted", { kind: "ok" });
      } else {
        await addBook(url, { cover: $(".hero-cover img", drawer)?.src, source: state.drawer.source, protocol: state.drawer.protocol });
      }
    } else if (act === "play") {
      spinBtn(btn, false);
      await playBook(drawerBook()?.id);
    } else if (act === "toggle-finished") {
      const lib = drawerBook();
      await api(`/api/library/${lib.id}/progress`, { method: "PUT", body: { position: 0, finished: !lib.finished } });
      if (player.book?.id === lib.id) { player.book.finished = !lib.finished; player.render(); }
      await refreshLibrary();
    } else {
      const lib = drawerBook();
      if (lib && (await bookAction(lib.id, act)) && act === "remove") closeDrawer();
    }
  } catch (err) {
    toast(err.message, { kind: "err" });
  }
  spinBtn(btn, false);
  updateDrawerLive();
};

// ---------------------------------------------------------------------------
// boot

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && state.drawer) closeDrawer();
  const typing = e.target.matches("input, textarea, select");
  if (!typing && (e.key === "/" || (e.key === "k" && (e.metaKey || e.ctrlKey)))) {
    e.preventDefault();
    if (!$("#q")) location.hash = "#/discover";
    requestAnimationFrame(() => $("#q")?.focus());
  }
});

async function boot() {
  try {
    [state.settings, state.sources, state.library] = await Promise.all([
      api("/api/settings"),
      api("/api/sources"),
      api("/api/library"),
    ]);
  } catch (e) {
    toast(`Couldn’t reach Narratarr: ${e.message}`, { kind: "err" });
  }
  state.libraryKey = state.library.map((b) => `${b.id}:${b.status}`).join("|");
  updateRdPill();
  window.addEventListener("hashchange", route);
  route();
  player.onChange = () => updateDrawerLive();
  player.restore(state.library);
  setInterval(refreshLibrary, 2000);
}

boot();
