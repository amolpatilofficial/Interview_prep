/* Job Application Agent — operator console */
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const state = { applications: [], answers: [], openApp: null, awaiting: new Set() };

const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const clock = (iso) => {
  const d = iso ? new Date(iso) : new Date();
  return d.toLocaleTimeString([], { hour12: false });
};

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || res.statusText);
  return body;
}

/* ------------------------------------------------------------------ header */
async function loadProfile() {
  const { profile, settings } = await api("/api/profile");
  const line = $("#profile-line");
  if (!profile.loaded) {
    line.textContent = profile.error;
    line.style.color = "var(--err)";
  } else {
    const docs = profile.documents.filter((d) => d.exists).map((d) => d.key);
    line.textContent =
      `${profile.name} · ${profile.email || "no email"} · ` +
      `resume ${profile.resume_chars.toLocaleString()} chars · ` +
      `docs: ${docs.length ? docs.join(", ") : "none"}`;
  }
  $("#settings-chips").innerHTML = [
    `<span class="chip">${esc(settings.provider_label)}</span>`,
    `<span class="chip">min conf ${settings.min_confidence}</span>`,
    settings.field_batch
      ? `<span class="chip">${settings.field_batch} fields/call</span>`
      : "",
    settings.credential_error
      ? `<span class="chip bad" title="${esc(settings.credential_error)}">API key missing</span>`
      : `<span class="chip">API key ✓</span>`,
  ].filter(Boolean).join("");
  $("#mode").value = settings.default_mode;
  $("#concurrency").value = settings.concurrency;
  $("#headless").value = String(settings.headless);
}

/* ------------------------------------------------------------------- stats */
async function loadStats() {
  const { stats, awaiting_review } = await api("/api/stats");
  state.awaiting = new Set(awaiting_review);
  const s = stats.by_status || {};
  const cards = [
    ["Applications", stats.total],
    ["Submitted", (s.submitted || 0) + (s.submitted_unconfirmed || 0)],
    ["Awaiting review", s.awaiting_review || 0],
    ["Companies", stats.companies],
    ["Fields filled", stats.fields_filled],
    ["Answers remembered", stats.answers_remembered],
  ];
  $("#stats").innerHTML = cards
    .map(([k, n]) => `<div class="stat"><div class="n">${n ?? 0}</div><div class="k">${k}</div></div>`)
    .join("");
}

/* ------------------------------------------------------------ applications */
async function loadApplications() {
  const { applications } = await api("/api/applications?limit=300");
  state.applications = applications;
  const body = $("#apps-table tbody");
  $("#apps-empty").hidden = applications.length > 0;
  body.innerHTML = applications
    .map((a) => {
      const host = (() => { try { return new URL(a.url).hostname; } catch { return a.url; } })();
      const needsReview = a.status === "awaiting_review";
      return `
      <tr data-id="${a.id}">
        <td>
          <div class="cell-main">${esc(a.company || "—")}</div>
          <div class="cell-sub">${esc(host)}</div>
        </td>
        <td>
          <div class="cell-main">${esc(a.role || "—")}</div>
          <div class="cell-sub">${esc(a.location || "")}</div>
        </td>
        <td>${esc(a.ats || "—")}</td>
        <td class="num">${a.fields_filled ?? 0}/${a.fields_total ?? 0}</td>
        <td><span class="badge ${esc(a.status)}">${esc((a.status || "").replace(/_/g, " "))}</span></td>
        <td class="num">
          ${needsReview ? `<button class="approve small" data-approve="${a.id}">Approve</button>` : ""}
          <button class="ghost" data-open="${a.id}">Details</button>
        </td>
      </tr>`;
    })
    .join("");
}

/* ------------------------------------------------------------- answer bank */
async function loadAnswers() {
  const { answers } = await api("/api/answers");
  state.answers = answers;
  $("#answers-empty").hidden = answers.length > 0;
  $("#answers-table tbody").innerHTML = answers
    .map(
      (a) => `
      <tr>
        <td class="cell-main">${esc(a.question)}${a.pinned ? ' <span class="tag mem">pinned</span>' : ""}</td>
        <td>${esc(String(a.answer).slice(0, 220))}</td>
        <td class="num">${a.times_used}</td>
        <td class="num"><button class="ghost" data-del="${esc(a.normalized_question)}">Delete</button></td>
      </tr>`
    )
    .join("");
}

/* ------------------------------------------------------------------ drawer */
async function openApp(appId) {
  const { application: a, fields } = await api(`/api/applications/${appId}`);
  state.openApp = a;
  $("#drawer-title").textContent = `${a.company || "Unknown company"} — ${a.role || "role not detected"}`;
  $("#drawer-sub").innerHTML =
    `<a href="${esc(a.url)}" target="_blank" rel="noopener">${esc(a.url)}</a>`;

  const filled = fields.filter((f) => f.filled);
  const skipped = fields.filter((f) => !f.filled);

  const meta = [
    ["Status", (a.status || "").replace(/_/g, " ")],
    ["ATS", a.ats || "—"],
    ["Location", a.location || "—"],
    ["Fields filled", `${a.fields_filled ?? 0} / ${a.fields_total ?? 0}`],
    ["Files uploaded", a.files_uploaded ?? 0],
    ["Started", a.created_at ? new Date(a.created_at).toLocaleString() : "—"],
  ];

  const qa = (f) => `
    <div class="qa ${f.filled ? "" : "skipped"}">
      <div class="q">${esc(f.question || "(unlabelled)")}</div>
      <div class="a">${esc(f.filled ? f.answer : f.error || "left blank")}</div>
      <div class="f">
        <span class="tag">${esc(f.field_type)}</span>
        ${f.required ? '<span class="tag req">required</span>' : ""}
        ${f.source === "memory" ? '<span class="tag mem">from answer bank</span>' : ""}
        <span class="tag">confidence ${Number(f.confidence || 0).toFixed(2)}</span>
        ${!f.filled && f.error ? '<span class="tag err">not filled</span>' : ""}
      </div>
    </div>`;

  $("#drawer-body").innerHTML = `
    ${
      a.status === "awaiting_review"
        ? `<div class="review-bar">
             <p>Form is filled and waiting. Review the answers and the screenshot, then decide.</p>
             <button class="approve small" data-approve="${a.id}">Approve &amp; submit</button>
             <button class="reject small" data-reject="${a.id}">Reject</button>
           </div>`
        : ""
    }
    ${a.error ? `<p class="error">${esc(a.error)}</p>` : ""}
    <div class="meta-grid">
      ${meta.map(([k, v]) => `<div class="meta"><div class="k">${k}</div><div class="v">${esc(v)}</div></div>`).join("")}
    </div>
    <div class="section-title">Submitted answers (${filled.length})</div>
    ${filled.map(qa).join("") || '<p class="empty">Nothing was filled.</p>'}
    ${skipped.length ? `<div class="section-title">Left blank (${skipped.length})</div>${skipped.map(qa).join("")}` : ""}
    ${
      a.screenshot
        ? `<div class="section-title">Screenshot</div>
           <img class="shot" src="/api/screenshots/${encodeURIComponent(a.screenshot)}" alt="filled form" />`
        : ""
    }`;

  $("#drawer").hidden = false;
  $("#drawer-backdrop").hidden = false;
}

function closeDrawer() {
  $("#drawer").hidden = true;
  $("#drawer-backdrop").hidden = true;
  state.openApp = null;
}

/* ----------------------------------------------------------------- console */
function pushLine(level, message, at) {
  const box = $("#console");
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 60;
  const el = document.createElement("div");
  el.className = `line ${level || "info"}`;
  el.innerHTML = `<span class="t">${clock(at)}</span><span class="m">${esc(message)}</span>`;
  box.appendChild(el);
  while (box.childElementCount > 800) box.removeChild(box.firstChild);
  if (atBottom) box.scrollTop = box.scrollHeight;
}

function connectStream() {
  const src = new EventSource("/api/events");
  const dot = $("#sse-dot");
  src.onopen = () => { dot.classList.add("live"); dot.title = "stream live"; };
  src.onerror = () => { dot.classList.remove("live"); dot.title = "stream disconnected"; };
  src.onmessage = (ev) => {
    let data;
    try { data = JSON.parse(ev.data); } catch { return; }
    if (data.kind === "log") pushLine(data.level, data.message, data.at);
    if (data.kind === "application" || data.kind === "batch" || data.kind === "review") {
      scheduleRefresh();
    }
    if (data.kind === "application" && state.openApp && state.openApp.id === data.application_id) {
      openApp(data.application_id).catch(() => {});
    }
  };
}

let refreshTimer = null;
function scheduleRefresh() {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(() => {
    loadApplications().catch(() => {});
    loadStats().catch(() => {});
    loadAnswers().catch(() => {});
  }, 400);
}

/* ------------------------------------------------------------------ events */
$("#run").addEventListener("click", async () => {
  const btn = $("#run");
  const err = $("#composer-error");
  err.hidden = true;
  btn.disabled = true;
  btn.textContent = "Starting…";
  try {
    const res = await api("/api/runs", {
      method: "POST",
      body: JSON.stringify({
        urls: $("#urls").value,
        mode: $("#mode").value,
        concurrency: Number($("#concurrency").value) || 1,
        headless: $("#headless").value === "true",
      }),
    });
    pushLine("info", `Queued ${res.urls.length} application(s).`);
    $("#urls").value = "";
    scheduleRefresh();
  } catch (e) {
    err.textContent = e.message;
    err.hidden = false;
  } finally {
    btn.disabled = false;
    btn.textContent = "Run agent";
  }
});

document.addEventListener("click", async (ev) => {
  const open = ev.target.closest("[data-open]");
  if (open) return openApp(open.dataset.open);

  const row = ev.target.closest("#apps-table tbody tr");
  if (row && !ev.target.closest("button")) return openApp(row.dataset.id);

  const approve = ev.target.closest("[data-approve]");
  const reject = ev.target.closest("[data-reject]");
  if (approve || reject) {
    const id = (approve || reject).dataset.approve || reject.dataset.reject;
    try {
      await api(`/api/applications/${id}/decision`, {
        method: "POST",
        body: JSON.stringify({ approved: Boolean(approve) }),
      });
      if (state.openApp && state.openApp.id === id) closeDrawer();
      scheduleRefresh();
    } catch (e) {
      pushLine("error", e.message);
    }
    return;
  }

  const del = ev.target.closest("[data-del]");
  if (del) {
    await api(`/api/answers/${encodeURIComponent(del.dataset.del)}`, { method: "DELETE" });
    loadAnswers();
    return;
  }
});

$("#drawer-close").addEventListener("click", closeDrawer);
$("#drawer-backdrop").addEventListener("click", closeDrawer);
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });

$("#refresh").addEventListener("click", scheduleRefresh);

$$(".tab").forEach((tab) =>
  tab.addEventListener("click", () => {
    $$(".tab").forEach((t) => t.classList.toggle("active", t === tab));
    $("#tab-apps").hidden = tab.dataset.tab !== "apps";
    $("#tab-answers").hidden = tab.dataset.tab !== "answers";
  })
);

$("#answer-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const question = $("#answer-q").value.trim();
  const answer = $("#answer-a").value.trim();
  if (!question || !answer) return;
  await api("/api/answers", { method: "PUT", body: JSON.stringify({ question, answer }) });
  $("#answer-q").value = "";
  $("#answer-a").value = "";
  loadAnswers();
});

/* -------------------------------------------------------------------- boot */
(async function boot() {
  connectStream();
  await Promise.allSettled([loadProfile(), loadStats(), loadApplications(), loadAnswers()]);
  setInterval(() => { loadStats().catch(() => {}); }, 15000);
})();
