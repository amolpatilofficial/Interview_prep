/* =========================================================
   Senior Data Engineer Interview Prep — app logic
   Pure vanilla JS. No framework. No backend.
   ========================================================= */

(function () {
  "use strict";

  // ---- Aggregate data from per-topic files ---------------------------------
  const D = window.DATA || {};
  const QUESTION_BUCKETS = {
    sql: { title: "SQL", desc: "Joins, windows, CTEs, indexing, perf tuning, advanced queries.", items: D.sql || [] },
    snowflake: { title: "Snowflake", desc: "Warehouses, micro-partitions, time travel, Snowpipe, performance.", items: D.snowflake || [] },
    python: { title: "Python", desc: "Core Python, OOP, decorators, generators, ETL patterns.", items: D.python || [] },
    pyspark: { title: "PySpark", desc: "RDDs, DataFrames, optimizations, partitioning, joins, shuffles.", items: D.pyspark || [] },
    adf: { title: "Azure Data Factory", desc: "Pipelines, datasets, triggers, IR, mapping data flows, CI/CD.", items: D.adf || [] },
    aws: { title: "AWS Services", desc: "S3, Glue, EMR, Redshift, Kinesis, Lambda, Athena, IAM.", items: D.aws || [] },
    migration: { title: "Migration", desc: "On-prem to cloud, Hadoop→Spark, Oracle→Snowflake, schema/data migration.", items: D.migration || [] },
    systemdesign: { title: "System Design", desc: "Lambda/Kappa, lakehouse, streaming, data modeling, governance.", items: D.systemdesign || [] },
    coding: { title: "Coding Problems", desc: "Hands-on coding — SQL puzzles, Python scripts, PySpark transformations.", items: D.coding || [] },
  };

  const USECASES = D.usecases || [];
  const BESTPRACTICES = D.bestpractices || [];
  const NOTES = D.notes || [];

  // ---- DOM refs ------------------------------------------------------------
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const contentEl = $("#content");
  const searchEl = $("#searchInput");
  const diffEl = $("#difficultyFilter");
  const tagEl = $("#tagFilter");
  const nav = $("#nav");
  const sidebar = $("#sidebar");
  const menuBtn = $("#menuBtn");
  const themeToggle = $("#themeToggle");
  const totalQEl = $("#totalQ");
  const totalUCEl = $("#totalUC");

  // ---- State ---------------------------------------------------------------
  const state = {
    view: "dashboard",
    search: "",
    difficulty: "",
    tag: "",
    page: 1,
    perPage: 25,
    bookmarks: new Set(JSON.parse(localStorage.getItem("bm") || "[]")),
    expanded: new Set(),
  };

  // ---- Totals --------------------------------------------------------------
  function totalQuestions() {
    return Object.values(QUESTION_BUCKETS).reduce((s, b) => s + b.items.length, 0);
  }
  function totalUseCases() { return USECASES.length; }
  totalQEl.textContent = totalQuestions().toLocaleString();
  totalUCEl.textContent = totalUseCases().toLocaleString();

  // ---- Tag collection ------------------------------------------------------
  function collectTags() {
    const tags = new Set();
    for (const b of Object.values(QUESTION_BUCKETS)) {
      for (const q of b.items) (q.tags || []).forEach(t => tags.add(t));
    }
    USECASES.forEach(u => (u.tags || []).forEach(t => tags.add(t)));
    return [...tags].sort();
  }
  function fillTagFilter() {
    const tags = collectTags();
    tagEl.innerHTML = '<option value="">All Tags</option>' +
      tags.map(t => `<option value="${esc(t)}">${esc(t)}</option>`).join("");
  }
  fillTagFilter();

  // ---- Persistence helpers -------------------------------------------------
  function persistBookmarks() {
    localStorage.setItem("bm", JSON.stringify([...state.bookmarks]));
  }
  function toggleBookmark(id) {
    if (state.bookmarks.has(id)) state.bookmarks.delete(id);
    else state.bookmarks.add(id);
    persistBookmarks();
  }

  // ---- Utility -------------------------------------------------------------
  function esc(s) {
    return String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  }
  // Lightweight markdown renderer: code blocks, inline code, bold, lists, tables, headings, links.
  function renderMD(text) {
    if (!text) return "";
    let t = text;
    // code blocks ```lang\n...```
    t = t.replace(/```(\w+)?\n([\s\S]*?)```/g, (_, lang, body) =>
      `<pre><code class="lang-${esc(lang || "")}">${esc(body)}</code></pre>`);
    // Tables: a simple pipe-format if line starts with |
    t = t.replace(/((?:^\|.*\|\s*\n?)+)/gm, (block) => {
      const lines = block.trim().split("\n").filter(Boolean);
      if (lines.length < 2) return block;
      const headers = lines[0].split("|").slice(1, -1).map(s => s.trim());
      const rows = lines.slice(2).map(l => l.split("|").slice(1, -1).map(s => s.trim()));
      return `<table><thead><tr>${headers.map(h => `<th>${esc(h)}</th>`).join("")}</tr></thead><tbody>${
        rows.map(r => `<tr>${r.map(c => `<td>${esc(c)}</td>`).join("")}</tr>`).join("")
      }</tbody></table>`;
    });
    // headings #### / ###
    t = t.replace(/^####\s+(.+)$/gm, "<h4>$1</h4>");
    t = t.replace(/^###\s+(.+)$/gm, "<h4>$1</h4>");
    // bold
    t = t.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    // inline code
    t = t.replace(/`([^`]+)`/g, (_, c) => `<code>${esc(c)}</code>`);
    // lists: simple bullet/numbered
    t = t.replace(/(?:^|\n)((?:- [^\n]+\n?)+)/g, (_, blk) => {
      const items = blk.trim().split(/\n/).map(l => l.replace(/^- /, "")).map(i => `<li>${i}</li>`).join("");
      return `\n<ul>${items}</ul>`;
    });
    t = t.replace(/(?:^|\n)((?:\d+\. [^\n]+\n?)+)/g, (_, blk) => {
      const items = blk.trim().split(/\n/).map(l => l.replace(/^\d+\. /, "")).map(i => `<li>${i}</li>`).join("");
      return `\n<ol>${items}</ol>`;
    });
    // paragraphs from double newlines
    t = t.split(/\n{2,}/).map(p =>
      /^(<h\d|<ul|<ol|<pre|<table|<blockquote)/.test(p.trim()) ? p : `<p>${p.replace(/\n/g, "<br/>")}</p>`
    ).join("\n");
    return t;
  }

  function filterItems(items) {
    const q = state.search.trim().toLowerCase();
    const d = state.difficulty;
    const tag = state.tag;
    return items.filter(it => {
      if (d && (it.difficulty || "").toLowerCase() !== d) return false;
      if (tag && !(it.tags || []).includes(tag)) return false;
      if (!q) return true;
      const hay = `${it.q || it.title || ""} ${it.a || it.desc || it.approach || ""} ${(it.tags||[]).join(" ")}`.toLowerCase();
      return hay.includes(q);
    });
  }

  // ---- Renderers -----------------------------------------------------------
  function renderDashboard() {
    const buckets = Object.entries(QUESTION_BUCKETS);
    const tQ = totalQuestions();
    const tUC = totalUseCases();
    return `
      <h1 class="page-title">Senior Data Engineer Interview Prep</h1>
      <p class="page-sub">A comprehensive corpus of ${tQ.toLocaleString()}+ interview questions and ${tUC.toLocaleString()}+ real-world use cases across SQL, Snowflake, Python, PySpark, ADF, AWS, migrations and system design.</p>

      <div class="cards">
        <div class="card"><div class="label">Total Questions</div><div class="value">${tQ.toLocaleString()}</div><div class="sub">across ${buckets.length} topics</div></div>
        <div class="card"><div class="label">Use Cases</div><div class="value">${tUC.toLocaleString()}</div><div class="sub">scenario-based</div></div>
        <div class="card"><div class="label">Best Practices</div><div class="value">${BESTPRACTICES.length}</div><div class="sub">curated</div></div>
        <div class="card"><div class="label">Cheat Notes</div><div class="value">${NOTES.length}</div><div class="sub">quick refs</div></div>
        <div class="card"><div class="label">Bookmarks</div><div class="value">${state.bookmarks.size}</div><div class="sub">your saved</div></div>
      </div>

      <h2 class="section-title">Browse by Topic</h2>
      <div class="tiles">
        ${buckets.map(([key, b]) => `
          <div class="tile" data-jump="${key}">
            <h3>${esc(b.title)}</h3>
            <p>${esc(b.desc)}</p>
            <div class="meta">${b.items.length} questions</div>
          </div>
        `).join("")}
        <div class="tile" data-jump="usecases">
          <h3>1000+ Use Cases</h3>
          <p>Real-world data engineering problems and how to solve them end-to-end.</p>
          <div class="meta">${USECASES.length} use cases</div>
        </div>
        <div class="tile" data-jump="bestpractices">
          <h3>Best Practices</h3>
          <p>Patterns, anti-patterns and battle-tested guidance for senior DEs.</p>
          <div class="meta">${BESTPRACTICES.length} sections</div>
        </div>
        <div class="tile" data-jump="notes">
          <h3>Cheat Notes</h3>
          <p>Distilled summaries — keep these open before your interview.</p>
          <div class="meta">${NOTES.length} notes</div>
        </div>
      </div>

      <h2 class="section-title">How to use this guide</h2>
      <div class="note-section">
        <ul>
          <li><strong>Sequential mode:</strong> walk topic by topic from sidebar — every question has a deep answer.</li>
          <li><strong>Quick lookup:</strong> use the top search to grep across all 2000+ items by keyword.</li>
          <li><strong>Filter by difficulty / tag:</strong> narrow to <em>expert</em> or to a single tech like <em>partitioning</em>.</li>
          <li><strong>Bookmark</strong> tricky ones with the ★ button — they appear under <em>Bookmarks</em>.</li>
          <li><strong>Use Cases:</strong> for system-design rounds, study these end-to-end scenarios.</li>
        </ul>
      </div>
    `;
  }

  function renderQuestionList(bucketKey) {
    const bucket = QUESTION_BUCKETS[bucketKey];
    if (!bucket) return `<div class="empty">Unknown topic.</div>`;
    const filtered = filterItems(bucket.items);
    const total = filtered.length;
    const pages = Math.max(1, Math.ceil(total / state.perPage));
    if (state.page > pages) state.page = 1;
    const slice = filtered.slice((state.page - 1) * state.perPage, state.page * state.perPage);

    return `
      <h1 class="page-title">${esc(bucket.title)} Questions</h1>
      <p class="page-sub">${esc(bucket.desc)}</p>
      <div class="toolbar">
        <div class="count">${total.toLocaleString()} / ${bucket.items.length.toLocaleString()} questions shown</div>
        <div class="chips">
          <span class="chip ${state.difficulty===''?'on':''}" data-diff="">All</span>
          <span class="chip ${state.difficulty==='easy'?'on':''}" data-diff="easy">Easy</span>
          <span class="chip ${state.difficulty==='medium'?'on':''}" data-diff="medium">Medium</span>
          <span class="chip ${state.difficulty==='hard'?'on':''}" data-diff="hard">Hard</span>
          <span class="chip ${state.difficulty==='expert'?'on':''}" data-diff="expert">Expert</span>
        </div>
      </div>
      <div class="qlist">
        ${slice.length === 0 ? `<div class="empty">No questions match your filters.</div>` : slice.map(renderQuestion).join("")}
      </div>
      ${renderPager(pages)}
    `;
  }

  function renderQuestion(q) {
    const id = q.id || `${q.q}`;
    const open = state.expanded.has(id);
    const bm = state.bookmarks.has(id);
    const diff = (q.difficulty || "medium").toLowerCase();
    const tags = (q.tags || []).map(t => `<span class="badge">${esc(t)}</span>`).join("");
    return `
      <div class="qitem ${open ? "open" : ""}" data-id="${esc(id)}">
        <div class="qhead" data-toggle>
          <div class="qnum">${esc(q.num || "")}</div>
          <div class="qtitle">${esc(q.q || q.title)}</div>
          <div class="qbadges">
            <span class="badge ${diff}">${esc(diff)}</span>
            ${tags}
            <button class="bookmark-btn ${bm ? "on" : ""}" data-bookmark title="Bookmark">★</button>
            <span class="qchevron">${open ? "▾" : "▸"}</span>
          </div>
        </div>
        <div class="qbody">${renderMD(q.a || "")}</div>
      </div>
    `;
  }

  function renderUseCases() {
    const filtered = filterItems(USECASES);
    const pages = Math.max(1, Math.ceil(filtered.length / state.perPage));
    if (state.page > pages) state.page = 1;
    const slice = filtered.slice((state.page - 1) * state.perPage, state.page * state.perPage);
    return `
      <h1 class="page-title">Real-World Use Cases</h1>
      <p class="page-sub">${USECASES.length.toLocaleString()} senior-level scenarios with recommended approach, trade-offs and gotchas.</p>
      <div class="toolbar">
        <div class="count">${filtered.length.toLocaleString()} matching</div>
      </div>
      ${slice.length === 0 ? `<div class="empty">No use cases match.</div>` : `<div class="uc-grid">${slice.map(renderUseCase).join("")}</div>`}
      ${renderPager(pages)}
    `;
  }

  function renderUseCase(u) {
    const id = u.id;
    const bm = state.bookmarks.has(id);
    return `
      <div class="uc-card" data-id="${esc(id)}">
        <h3>${esc(u.title)} <button class="bookmark-btn ${bm?'on':''}" data-bookmark style="float:right">★</button></h3>
        <div class="meta-row">
          ${(u.tags||[]).map(t=>`<span class="badge">${esc(t)}</span>`).join("")}
          ${u.difficulty?`<span class="badge ${u.difficulty}">${esc(u.difficulty)}</span>`:""}
        </div>
        <div class="desc">${renderMD(u.desc || "")}</div>
        <div class="approach"><strong>Approach:</strong><br/>${renderMD(u.approach || "")}</div>
      </div>
    `;
  }

  function renderBestPractices() {
    return `
      <h1 class="page-title">Best Practices</h1>
      <p class="page-sub">Senior-level patterns and anti-patterns to bring up in design discussions.</p>
      ${BESTPRACTICES.map(b => `
        <div class="note-section">
          <h3>${esc(b.title)}</h3>
          ${renderMD(b.body || "")}
        </div>
      `).join("")}
    `;
  }

  function renderNotes() {
    return `
      <h1 class="page-title">Cheat Notes</h1>
      <p class="page-sub">Quick reference — open before your interview.</p>
      ${NOTES.map(n => `
        <div class="note-section">
          <h3>${esc(n.title)}</h3>
          ${renderMD(n.body || "")}
        </div>
      `).join("")}
    `;
  }

  function renderBookmarks() {
    const all = [];
    for (const [k, b] of Object.entries(QUESTION_BUCKETS)) {
      for (const q of b.items) if (state.bookmarks.has(q.id)) all.push({ kind: k, item: q });
    }
    for (const u of USECASES) if (state.bookmarks.has(u.id)) all.push({ kind: "usecases", item: u });
    if (all.length === 0) {
      return `<h1 class="page-title">Bookmarks</h1><div class="empty">No bookmarks yet. Click the ★ on any question or use case.</div>`;
    }
    return `
      <h1 class="page-title">Bookmarks (${all.length})</h1>
      <div class="qlist">
        ${all.map(({ kind, item }) => kind === "usecases" ? renderUseCase(item) : renderQuestion(item)).join("")}
      </div>
    `;
  }

  function renderPager(pages) {
    if (pages <= 1) return "";
    const cur = state.page;
    const btns = [];
    const push = (p, label = p) => btns.push(`<button class="${p===cur?'active':''}" data-page="${p}">${label}</button>`);
    btns.push(`<button data-page="${Math.max(1,cur-1)}" ${cur===1?'disabled':''}>‹ Prev</button>`);
    const max = pages;
    const window = 2;
    const set = new Set([1, max, cur, cur-1, cur+1, cur-window, cur+window]);
    const list = [...set].filter(p => p >= 1 && p <= max).sort((a,b)=>a-b);
    let prev = 0;
    for (const p of list) {
      if (prev && p - prev > 1) btns.push(`<button disabled>…</button>`);
      push(p);
      prev = p;
    }
    btns.push(`<button data-page="${Math.min(max,cur+1)}" ${cur===max?'disabled':''}>Next ›</button>`);
    return `<div class="pager">${btns.join("")}</div>`;
  }

  // ---- Main render dispatch ------------------------------------------------
  function render() {
    let html = "";
    switch (state.view) {
      case "dashboard": html = renderDashboard(); break;
      case "usecases": html = renderUseCases(); break;
      case "bestpractices": html = renderBestPractices(); break;
      case "notes": html = renderNotes(); break;
      case "bookmarks": html = renderBookmarks(); break;
      default:
        if (QUESTION_BUCKETS[state.view]) html = renderQuestionList(state.view);
        else html = `<div class="empty">Page not found</div>`;
    }
    contentEl.innerHTML = html;
    // Re-mark active nav
    $$(".nav-item").forEach(a => a.classList.toggle("active", a.dataset.view === state.view));
    window.scrollTo({ top: 0, behavior: "instant" });
  }

  // ---- Event wiring --------------------------------------------------------
  nav.addEventListener("click", (e) => {
    const a = e.target.closest(".nav-item");
    if (!a) return;
    state.view = a.dataset.view;
    state.page = 1;
    sidebar.classList.remove("open");
    render();
  });

  contentEl.addEventListener("click", (e) => {
    const tile = e.target.closest("[data-jump]");
    if (tile) { state.view = tile.dataset.jump; state.page = 1; render(); return; }

    const pageBtn = e.target.closest(".pager button[data-page]");
    if (pageBtn && !pageBtn.disabled) { state.page = parseInt(pageBtn.dataset.page, 10); render(); return; }

    const chip = e.target.closest(".chip[data-diff]");
    if (chip) { state.difficulty = chip.dataset.diff; diffEl.value = state.difficulty; state.page = 1; render(); return; }

    const bm = e.target.closest("[data-bookmark]");
    if (bm) {
      e.stopPropagation();
      const item = bm.closest("[data-id]");
      if (item) { toggleBookmark(item.dataset.id); bm.classList.toggle("on"); }
      return;
    }

    const toggle = e.target.closest("[data-toggle]");
    if (toggle) {
      const item = toggle.closest(".qitem");
      const id = item.dataset.id;
      if (state.expanded.has(id)) state.expanded.delete(id);
      else state.expanded.add(id);
      item.classList.toggle("open");
      const chev = item.querySelector(".qchevron");
      if (chev) chev.textContent = item.classList.contains("open") ? "▾" : "▸";
    }
  });

  let searchTimer = null;
  searchEl.addEventListener("input", (e) => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      state.search = e.target.value;
      state.page = 1;
      render();
    }, 120);
  });

  diffEl.addEventListener("change", (e) => { state.difficulty = e.target.value; state.page = 1; render(); });
  tagEl.addEventListener("change", (e) => { state.tag = e.target.value; state.page = 1; render(); });

  menuBtn.addEventListener("click", () => sidebar.classList.toggle("open"));
  themeToggle.addEventListener("click", () => {
    const cur = document.documentElement.getAttribute("data-theme");
    const next = cur === "light" ? "" : "light";
    if (next) document.documentElement.setAttribute("data-theme", next);
    else document.documentElement.removeAttribute("data-theme");
    localStorage.setItem("theme", next);
  });
  // Restore theme
  const savedTheme = localStorage.getItem("theme");
  if (savedTheme === "light") document.documentElement.setAttribute("data-theme", "light");

  // Initial render
  render();

  // Expose for debugging
  window.__APP__ = { state, QUESTION_BUCKETS, USECASES };
})();
