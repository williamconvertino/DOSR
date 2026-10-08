// DOSR client GUI — vanilla JS over the /api endpoints in gui/server.py.
"use strict";

const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtMs = (v) => (v == null ? "" : v < 1000 ? `${Math.round(v)} ms` : `${(v / 1000).toFixed(2)} s`);
const shortOid = (oid, n = 12) => {
  if (!oid) return "—";
  const [fmt, hex] = oid.split(":");
  return hex ? `${fmt}:${hex.slice(0, n)}` : oid.slice(0, n);
};
const STATUS_ICON = { pending: "·", running: "▶", done: "✓", failed: "✗", skipped: "–" };

const ui = {
  state: null,
  repoId: null,
  repo: null,
  tab: "editor",
  file: null,
  fileDirty: false,
  jobId: null,
  jobTimer: null,
  armed: {},
};

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: opts.body ? { "Content-Type": "application/json" } : {},
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch { /* empty */ }
  if (!res.ok) {
    const d = data && data.detail;
    const msg = typeof d === "string" ? d : Array.isArray(d) ? d.map((e) => `${(e.loc || []).join(".")}: ${e.msg}`).join("; ") : `HTTP ${res.status}`;
    throw new Error(msg);
  }
  return data;
}

let toastTimer = null;
function toast(msg, bad = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast" + (bad ? " bad" : "");
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.hidden = true), bad ? 6000 : 3000);
}

// Two-click confirmation without browser dialogs.
function armed(key, btn, label) {
  if (ui.armed[key]) { delete ui.armed[key]; return true; }
  ui.armed[key] = true;
  const old = btn.textContent;
  btn.textContent = label;
  btn.classList.add("btn-danger");
  setTimeout(() => { delete ui.armed[key]; btn.textContent = old; btn.classList.remove("btn-danger"); }, 3000);
  return false;
}

// ------------------------------------------------------------ global state

async function loadState() {
  ui.state = await api("/api/state");
  const s = ui.state;
  $("#version").textContent = "v" + s.version;
  $("#workspace").textContent = s.workspace;
  $("#chain-path").textContent = s.chain_state;

  const sel = $("#profile");
  sel.innerHTML = s.profiles.map((p) => `<option value="${esc(p.id)}">${esc(p.name)}${p.description ? " — " + esc(p.description) : ""}</option>`).join("");
  sel.value = s.active_profile;
  renderAvatar();

  const list = $("#repo-list");
  list.innerHTML = s.repos.length
    ? s.repos.map((r) => `<li data-id="${esc(r.id)}" class="${r.id === ui.repoId ? "active" : ""}">
        <div class="r-name">${esc(r.name)}</div>
        <div class="muted small">${r.error ? "⚠ " + esc(r.error) : r.registered ? `${r.transitions} accepted PR(s)` : "not registered"}</div></li>`).join("")
    : `<li class="muted small">No repositories yet.</li>`;
  list.querySelectorAll("li[data-id]").forEach((li) => (li.onclick = () => selectRepo(li.dataset.id)));
}

function activeProfile() {
  return ui.state.profiles.find((p) => p.id === ui.state.active_profile);
}

function renderAvatar() {
  const p = activeProfile();
  const av = $("#profile-avatar");
  av.textContent = (p?.name || "?").slice(0, 1).toUpperCase();
  av.style.background = p?.color || "var(--accent)";
  $("#submit-as").textContent = p ? `${p.name} <${p.email}>` : "";
}

async function pollAttestor() {
  const pill = $("#attestor-pill");
  try {
    const a = await api("/api/attestor");
    if (a.up) {
      pill.className = "pill pill-up";
      $("#attestor-text").textContent = `attestor up · ${a.latency_ms} ms`;
      pill.title = `${a.url}\nkeys: ${a.keys.map((k) => k.key_id).join(", ")}`;
    } else {
      pill.className = "pill pill-down";
      $("#attestor-text").textContent = "attestor down";
      pill.title = a.error;
    }
  } catch (e) {
    pill.className = "pill pill-down";
    $("#attestor-text").textContent = "client error";
  }
}

// ------------------------------------------------------------ repo view

async function selectRepo(id) {
  if (ui.fileDirty && ui.repoId !== id && !armed("leave", $("#save-btn"), "Unsaved! click repo again")) return;
  ui.repoId = id;
  ui.file = null;
  ui.fileDirty = false;
  ui.jobId = null;
  $("#result").hidden = true;
  resetPipeline();
  document.querySelectorAll("#repo-list li").forEach((li) => li.classList.toggle("active", li.dataset.id === id));
  $("#empty-state").hidden = true;
  $("#repo-view").hidden = false;
  await refreshRepo();
  switchTab(ui.tab);
}

async function refreshRepo() {
  if (!ui.repoId) return;
  const r = (ui.repo = await api(`/api/repos/${encodeURIComponent(ui.repoId)}`));
  $("#repo-name").textContent = r.name;
  $("#repo-desc").textContent = r.description || "";
  const badges = [];
  if (!r.registered) badges.push(`<span class="badge bad">not on chain</span>`);
  else if (r.in_sync) badges.push(`<span class="badge ok">in sync with canonical HEAD</span>`);
  else if (r.diverged) badges.push(`<span class="badge bad">diverged — rebase needed</span>`);
  else if (r.ahead) badges.push(`<span class="badge warn">${r.ahead} commit(s) not yet accepted</span>`);
  badges.push(r.policy_hash_ok ? `<span class="badge ok">policy hash ok</span>` : `<span class="badge bad">policy hash mismatch</span>`);
  if (r.policy) badges.push(`<span class="badge">${esc(r.policy.review.prompt_template)}</span>`);
  $("#repo-badges").innerHTML = badges.join("");
  $("#repo-heads").innerHTML = `
    <span>Canonical HEAD <code>${esc(shortOid(r.canonical_head))}</code></span>
    <span>Local HEAD <code>${esc(shortOid(r.local_head))}</code> on <code>${esc(r.branch || "detached")}</code></span>
    <span>Accepted PRs <code>${r.accepted_transitions ?? 0}</code></span>
    <span>Repo ID <code title="${esc(r.repo_id)}">${esc(r.repo_id.slice(0, 18))}…</code></span>`;
  $("#changes-count").textContent = r.changes.length || "";

  const models = r.approved_models || [];
  const p = activeProfile();
  const profModel = p?.review_model ? `${p.review_model.provider}/${p.review_model.model}@${p.review_model.version}` : null;
  const opts = [`<option value="">Default (${esc(profModel ? "profile: " + profModel : "policy: " + (models[0] || "?"))})</option>`]
    .concat(models.map((m) => `<option value="${esc(m)}">${esc(m)} ✓ approved</option>`));
  if (profModel && !models.includes(profModel)) opts.push(`<option value="${esc(profModel)}">${esc(profModel)} ⚠ not in policy</option>`);
  const sel = $("#model-select");
  const prev = sel.value;
  sel.innerHTML = opts.join("");
  if ([...sel.options].some((o) => o.value === prev)) sel.value = prev;
}

function switchTab(tab) {
  ui.tab = tab;
  document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  document.querySelectorAll(".tab-panel").forEach((p) => (p.hidden = p.dataset.panel !== tab));
  if (tab === "editor") loadFiles();
  if (tab === "changes") loadDiff();
  if (tab === "history") loadHistory();
  if (tab === "config") loadConfig();
}

// ------------------------------------------------------------ files

async function loadFiles() {
  const { files } = await api(`/api/repos/${encodeURIComponent(ui.repoId)}/files`);
  const label = (code) => {
    if (!code) return "";
    if (code === "??") return `<span class="st st-U" title="untracked">U</span>`;
    const c = code.trim()[0];
    return `<span class="st st-${c}" title="${esc(code)}">${c}</span>`;
  };
  $("#file-list").innerHTML = files.map((f) => {
    const deleted = f.status && f.status.includes("D");
    return `<li data-path="${esc(f.path)}" class="${f.path === ui.file ? "active" : ""} ${deleted ? "st-D" : ""}"><span>${esc(f.path)}</span>${label(f.status)}</li>`;
  }).join("");
  $("#file-list").querySelectorAll("li").forEach((li) => {
    if (!li.classList.contains("st-D")) li.onclick = () => openFile(li.dataset.path);
  });
}

async function openFile(path) {
  if (ui.fileDirty && ui.file !== path && !armed("switch", $("#save-btn"), "Unsaved! click again")) return;
  const f = await api(`/api/repos/${encodeURIComponent(ui.repoId)}/file?path=${encodeURIComponent(path)}`);
  ui.file = path;
  ui.fileDirty = false;
  const code = $("#code");
  $("#current-file").textContent = path + (f.readonly ? "  (immutable policy — read only)" : "");
  $("#current-file").classList.remove("muted");
  if (f.binary) {
    code.value = `[binary or large file, ${f.size} bytes — not editable here]`;
    code.disabled = true;
  } else {
    code.value = f.content;
    code.disabled = !!f.readonly;
  }
  $("#save-btn").disabled = true;
  $("#delete-file-btn").disabled = !!f.readonly;
  $("#save-state").textContent = "";
  document.querySelectorAll("#file-list li").forEach((li) => li.classList.toggle("active", li.dataset.path === path));
}

async function saveFile() {
  if (!ui.file) return;
  try {
    await api(`/api/repos/${encodeURIComponent(ui.repoId)}/file`, { method: "PUT", body: { path: ui.file, content: $("#code").value } });
    ui.fileDirty = false;
    $("#save-btn").disabled = true;
    $("#save-state").textContent = "saved";
    await Promise.all([loadFiles(), refreshRepo()]);
  } catch (e) { toast(e.message, true); }
}

async function createFile() {
  const input = $("#new-file-name");
  const path = input.value.trim();
  if (!path) { input.focus(); return; }
  try {
    await api(`/api/repos/${encodeURIComponent(ui.repoId)}/file`, { method: "PUT", body: { path, content: "" } });
    input.value = "";
    await loadFiles();
    await openFile(path);
    await refreshRepo();
    $("#code").focus();
  } catch (e) { toast(e.message, true); }
}

async function deleteFile() {
  if (!ui.file || !armed("delete", $("#delete-file-btn"), "Confirm delete")) return;
  try {
    await api(`/api/repos/${encodeURIComponent(ui.repoId)}/file?path=${encodeURIComponent(ui.file)}`, { method: "DELETE" });
    toast(`Deleted ${ui.file}`);
    ui.file = null;
    $("#code").value = "";
    $("#code").disabled = true;
    $("#current-file").textContent = "Select a file";
    $("#save-btn").disabled = $("#delete-file-btn").disabled = true;
    await Promise.all([loadFiles(), refreshRepo()]);
  } catch (e) { toast(e.message, true); }
}

// ------------------------------------------------------------ changes

function renderDiff(text) {
  if (!text.trim()) return `<span class="muted">No changes relative to the canonical HEAD.</span>`;
  return text.split("\n").map((l) => {
    let cls = "";
    if (l.startsWith("+++") || l.startsWith("---") || l.startsWith("diff ") || l.startsWith("index ") || l.startsWith("new file") || l.startsWith("deleted file")) cls = "meta";
    else if (l.startsWith("@@")) cls = "hunk";
    else if (l.startsWith("+")) cls = "add";
    else if (l.startsWith("-")) cls = "del";
    return `<span class="${cls}">${esc(l) || " "}</span>`;
  }).join("");
}

async function loadDiff() {
  const d = await api(`/api/repos/${encodeURIComponent(ui.repoId)}/diff`);
  $("#diff-base").textContent = `Committed + uncommitted changes since canonical HEAD ${d.base ? d.base.slice(0, 12) : ""}`;
  $("#diff").innerHTML = renderDiff(d.diff);
}

async function discardChanges() {
  if (!armed("discard", $("#discard-btn"), "Click again to discard")) return;
  try {
    await api(`/api/repos/${encodeURIComponent(ui.repoId)}/discard`, { method: "POST" });
    toast("Uncommitted changes discarded");
    await Promise.all([loadDiff(), refreshRepo()]);
  } catch (e) { toast(e.message, true); }
}

// ------------------------------------------------------------ submit / pipeline

function resetPipeline() {
  const steps = ui.state?.steps || [];
  renderPipeline({ outcome: "pending", steps: steps.map((s) => ({ ...s, status: "pending", detail: "", estimate_ms: null })), elapsed_ms: 0, remaining_ms: null });
  $("#job-outcome").textContent = "idle";
}

function renderPipeline(job) {
  const out = $("#job-outcome");
  out.className = `outcome outcome-${job.outcome}`;
  out.textContent = job.outcome === "pending" ? "idle" : job.outcome;
  $("#elapsed").textContent = fmtMs(job.elapsed_ms) || "0 ms";
  $("#remaining").textContent = job.remaining_ms == null ? "—" : job.finished_at ? "done" : "~" + fmtMs(job.remaining_ms);
  $("#job-id").textContent = job.request_id ? `request ${job.request_id}` : "";
  const total = (job.elapsed_ms || 0) + (job.remaining_ms || 0);
  const pct = job.finished_at ? 100 : total ? Math.min(99, (100 * job.elapsed_ms) / total) : 0;
  const bar = $("#progress-bar");
  bar.style.width = pct + "%";
  bar.className = "progress-bar" + (job.finished_at ? ` done-${job.outcome}` : "");

  $("#steps").innerHTML = job.steps.map((s) => {
    const right = s.duration_ms != null ? fmtMs(s.duration_ms) : s.status === "pending" && s.estimate_ms ? `<span class="muted">~${fmtMs(s.estimate_ms)}</span>` : "";
    return `<li class="${s.status}">
      <span class="icon">${STATUS_ICON[s.status]}</span>
      <span class="label">${esc(s.label)}</span>
      <span class="dur">${right}</span>
      ${s.error ? `<span class="err">${esc(s.error)}</span>` : s.detail ? `<span class="detail">${esc(s.detail)}</span>` : ""}
    </li>`;
  }).join("");
}

function renderResult(job, target) {
  const labels = { approved: "Approved", rejected: "Rejected", error: "Submission failed", "dry-run": "Dry run complete" };
  const d = job.decision;
  const info = job.info || {};
  const kv = [];
  if (d) {
    kv.push(["Summary", esc(d.summary)]);
    kv.push(["Attestor", `${esc(d.attestor_key_id)} <span class="muted">${esc(d.signer)}</span>`]);
    kv.push(["Network", `upload ${fmtMs(d.upload_ms)} · server ${fmtMs(d.server_wait_ms)}`]);
    if (d.expires_at) kv.push(["Expires", new Date(d.expires_at * 1000).toLocaleString()]);
  }
  if (info.pr) kv.push(["PR", `${esc(info.pr.title)} <span class="muted">· ${info.pr.changed_files} file(s) +${info.pr.additions}/-${info.pr.deletions}</span>`]);
  if (info.parent_git_oid) kv.push(["Transition", `<code>${esc(shortOid(info.parent_git_oid))}</code> → <code>${esc(shortOid(info.candidate_git_oid))}</code>`]);
  if (info.model) kv.push(["Model", `<code>${esc(info.model)}</code>`]);
  if (info.chain_event) kv.push(["Chain", `canonical HEAD → <code>${esc(shortOid(info.chain_event.candidate_git_oid))}</code> (block ${info.chain_event.block})`]);
  kv.push(["Total time", fmtMs(job.elapsed_ms)]);
  kv.push(["Artifacts", `<code class="small">${esc(job.artifacts_dir)}</code>`]);

  const measured = job.steps.filter((s) => s.duration_ms != null);
  const longest = Math.max(1, ...measured.map((s) => s.duration_ms));
  const lat = measured.map((s) => `<div class="lat-row"><span>${esc(s.label)}</span>
      <div><div class="lat-bar ${s.status === "failed" ? "failed" : ""}" style="width:${(100 * s.duration_ms) / longest}%"></div></div>
      <span class="t">${fmtMs(s.duration_ms)}</span></div>`).join("");

  target.innerHTML = `
    <div class="decision ${job.outcome}">
      <h2>${labels[job.outcome] || esc(job.outcome)}</h2>
      <dl class="kv">${kv.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("")}</dl>
    </div>
    ${job.errors.length ? `<div class="error-box"><strong>Errors</strong><ul>${job.errors.map((e) => `<li>${esc(e)}</li>`).join("")}</ul></div>` : ""}
    ${job.warnings.length ? `<div class="warn-box"><strong>Warnings</strong><ul>${job.warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul></div>` : ""}
    ${lat ? `<div class="card"><h3>Latency by step</h3>${lat}</div>` : ""}`;
  target.hidden = false;
}

async function startSubmit(ev) {
  ev.preventDefault();
  const btn = $("#submit-btn");
  btn.disabled = true;
  $("#result").hidden = true;
  try {
    const { job_id } = await api(`/api/repos/${encodeURIComponent(ui.repoId)}/submit`, {
      method: "POST",
      body: {
        message: $("#commit-msg").value.trim() || null,
        model: $("#model-select").value || null,
        register_on_chain: $("#opt-register").checked,
        dry_run: $("#opt-dry").checked,
      },
    });
    ui.jobId = job_id;
    pollJob();
  } catch (e) {
    toast(e.message, true);
    btn.disabled = false;
  }
}

async function pollJob() {
  clearTimeout(ui.jobTimer);
  if (!ui.jobId) return;
  const jobId = ui.jobId;
  try {
    const job = await api(`/api/jobs/${jobId}`);
    if (jobId !== ui.jobId) return;
    renderPipeline(job);
    if (job.finished_at) {
      $("#submit-btn").disabled = false;
      renderResult(job, $("#result"));
      if (job.outcome === "approved" || job.outcome === "rejected") $("#commit-msg").value = "";
      await Promise.all([refreshRepo(), loadState()]);
      return;
    }
  } catch (e) {
    toast(e.message, true);
    $("#submit-btn").disabled = false;
    return;
  }
  ui.jobTimer = setTimeout(pollJob, 120);
}

// ------------------------------------------------------------ history

async function loadHistory() {
  const { submissions } = await api(`/api/repos/${encodeURIComponent(ui.repoId)}/submissions`);
  const tbody = $("#history-table tbody");
  tbody.innerHTML = submissions.length
    ? submissions.map((r) => `<tr data-id="${esc(r.request_id)}">
        <td>${new Date((r.started_at || 0) * 1000).toLocaleString()}</td>
        <td><span class="outcome outcome-${esc(r.outcome)}">${esc(r.outcome)}</span></td>
        <td>${esc(r.info?.pr?.title || "—")}</td>
        <td>${esc(r.info?.profile || "")}</td>
        <td><code>${esc(shortOid(r.info?.parent_git_oid, 8))}</code> → <code>${esc(shortOid(r.info?.candidate_git_oid, 8))}</code></td>
        <td>${fmtMs(r.elapsed_ms)}</td></tr>`).join("")
    : `<tr><td colspan="6" class="muted">No submissions yet.</td></tr>`;
  tbody.querySelectorAll("tr[data-id]").forEach((tr) => (tr.onclick = () => showSubmission(tr.dataset.id)));
  $("#history-detail").hidden = true;
}

async function showSubmission(id) {
  const s = await api(`/api/repos/${encodeURIComponent(ui.repoId)}/submissions/${encodeURIComponent(id)}`);
  const el = $("#history-detail");
  el.innerHTML = `<h3>Submission ${esc(id)}</h3><div id="hd-result" class="result"></div>
    <div class="subtabs">
      ${s.pr_markdown ? `<button class="btn btn-small" data-v="pr">PR document</button>` : ""}
      ${s.request ? `<button class="btn btn-small" data-v="request">Request JSON</button>` : ""}
      ${s.response ? `<button class="btn btn-small" data-v="response">Attestor response</button>` : ""}
    </div><pre class="block" id="hd-view" hidden></pre>`;
  renderResult(s.run, $("#hd-result"));
  el.querySelectorAll(".subtabs button").forEach((b) => (b.onclick = () => {
    const v = $("#hd-view");
    v.hidden = false;
    v.textContent = b.dataset.v === "pr" ? s.pr_markdown : JSON.stringify(s[b.dataset.v], null, 2);
  }));
  el.hidden = false;
  el.scrollIntoView({ behavior: "smooth", block: "start" });
}

// ------------------------------------------------------------ config

async function loadConfig() {
  const r = ui.repo;
  const chain = await api("/api/chain");
  const onchain = chain.repos[r.repo_id];
  const p = r.policy;
  const rows = (pairs) => `<dl class="kv">${pairs.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("")}</dl>`;
  $("#config-view").innerHTML = `
    <div class="config-grid">
      <div class="card"><h3>Repository</h3>${rows([
        ["Name", esc(r.name)], ["Repo ID", `<code>${esc(r.repo_id)}</code>`], ["Chain / contract", `${r.chain_id} / <code>${esc(r.contract)}</code>`],
        ["Policy hash", `<code>${esc(r.policy_hash)}</code> ${r.policy_hash_ok ? '<span class="badge ok">ok</span>' : '<span class="badge bad">mismatch</span>'}`],
        ["Created by", esc(r.repo_config.created_by)], ["Path", `<code>${esc(r.path)}</code>`]])}</div>
      <div class="card"><h3>Review policy (immutable)</h3>${p ? rows([
        ["Policy ID", esc(p.policy_id)], ["Approved models", (r.approved_models || []).map((m) => `<code>${esc(m)}</code>`).join("<br>")],
        ["Minimum approvals", p.review.minimum_approvals], ["PR template", `<code>${esc(p.review.prompt_template)}</code>`],
        ["Limits", `${p.review.max_changed_files} files · ${p.review.max_patch_bytes} patch bytes`],
        ["Attestors", p.attestors.map((a) => `${esc(a.key_id)} <code class="small">${esc(a.address)}</code>`).join("<br>")],
        ["Storage", `${esc(p.storage.bundle_format)} via ${esc(p.storage.distribution)}`]]) : `<div class="error-box">${esc(r.policy_error || "policy unreadable")}</div>`}</div>
      <div class="card"><h3>Mock chain record</h3>${onchain ? rows([
        ["Canonical HEAD", `<code>${esc(onchain.canonical_head)}</code>`], ["Genesis", `<code>${esc(onchain.genesis_git_oid)}</code>`],
        ["Accepted transitions", onchain.accepted_transition_count], ["Registered at block", onchain.registered_block],
        ["Recent events", onchain.events.slice(-5).reverse().map((e) => `block ${e.block}: <code>${esc(shortOid(e.parent_git_oid, 8))}</code> → <code>${esc(shortOid(e.candidate_git_oid, 8))}</code>`).join("<br>") || "—"]])
        : `<p class="muted">Not registered.</p><button class="btn btn-small" onclick="registerRepo()">Register genesis now</button>`}</div>
      <div class="card"><h3>policy.json</h3><pre class="block">${esc(JSON.stringify(p, null, 2))}</pre></div>
    </div>`;
}

async function registerRepo() {
  try {
    await api(`/api/repos/${encodeURIComponent(ui.repoId)}/register`, { method: "POST" });
    toast("Registered on mock chain");
    await refreshRepo();
    loadConfig();
  } catch (e) { toast(e.message, true); }
}

// ------------------------------------------------------------ new repo

function openNewRepo() {
  const s = ui.state;
  $("#nr-seed").innerHTML = `<option value="">(empty project)</option>` + s.seeds.map((x) => `<option value="${esc(x.id)}">${esc(x.name || x.id)}</option>`).join("");
  $("#nr-preset").innerHTML = s.presets.map((p) => `<option value="${esc(p.id)}">${esc(p.id)} — ${esc(p.description)}</option>`).join("");
  $("#nr-template").innerHTML = `<option value="">(preset default)</option>` + s.templates.map((t) => `<option value="${esc(t.id)}">${esc(t.id)}</option>`).join("");
  $("#nr-error").hidden = true;
  ["#nr-name", "#nr-desc", "#nr-models", "#nr-max-files", "#nr-max-bytes"].forEach((id) => ($(id).value = ""));
  updatePresetPreview();
  $("#modal").hidden = false;
  $("#nr-name").focus();
}

function closeModal() { $("#modal").hidden = true; }

function updatePresetPreview() {
  const p = ui.state.presets.find((x) => x.id === $("#nr-preset").value);
  $("#nr-policy").textContent = p ? JSON.stringify(p.policy, null, 2) : "";
  if (p) {
    $("#nr-max-files").placeholder = p.policy.review.max_changed_files;
    $("#nr-max-bytes").placeholder = p.policy.review.max_patch_bytes;
  }
}

function onSeedChange() {
  const seed = ui.state.seeds.find((x) => x.id === $("#nr-seed").value);
  if (!seed) return;
  if (!$("#nr-name").value) $("#nr-name").value = seed.id;
  if (!$("#nr-desc").value) $("#nr-desc").value = seed.description || "";
  if (seed.preset) { $("#nr-preset").value = seed.preset; updatePresetPreview(); }
}

async function createRepo(ev) {
  ev.preventDefault();
  const btn = $("#nr-submit");
  btn.disabled = true;
  btn.textContent = "Creating…";
  const num = (id) => ($(id).value ? parseInt($(id).value, 10) : null);
  try {
    const models = $("#nr-models").value.split(",").map((m) => m.trim()).filter(Boolean);
    const res = await api("/api/repos", {
      method: "POST",
      body: {
        name: $("#nr-name").value.trim(), description: $("#nr-desc").value.trim(), preset: $("#nr-preset").value,
        seed: $("#nr-seed").value || null, approved_models: models.length ? models : null,
        prompt_template: $("#nr-template").value || null, max_changed_files: num("#nr-max-files"), max_patch_bytes: num("#nr-max-bytes"),
      },
    });
    closeModal();
    toast(`Created ${res.repo.name} (genesis ${shortOid(res.genesis_git_oid)}) in ${fmtMs(res.took_ms)}`);
    await loadState();
    await selectRepo(res.id);
    switchTab("editor");
  } catch (e) {
    $("#nr-error").textContent = e.message;
    $("#nr-error").hidden = false;
  } finally {
    btn.disabled = false;
    btn.textContent = "Create repository";
  }
}

// ------------------------------------------------------------ wiring

function wire() {
  document.querySelectorAll("#tabs button").forEach((b) => (b.onclick = () => switchTab(b.dataset.tab)));
  $("#new-repo-btn").onclick = openNewRepo;
  $("#nr-preset").onchange = updatePresetPreview;
  $("#nr-seed").onchange = onSeedChange;
  $("#modal").addEventListener("click", (e) => { if (e.target.id === "modal") closeModal(); });
  $("#profile").onchange = async (e) => {
    try {
      await api("/api/profile", { method: "POST", body: { id: e.target.value } });
      ui.state.active_profile = e.target.value;
      renderAvatar();
      toast(`Now acting as ${activeProfile().name}`);
      pollAttestor();
      if (ui.repoId) refreshRepo();
    } catch (err) { toast(err.message, true); }
  };
  $("#code").addEventListener("input", () => {
    ui.fileDirty = true;
    $("#save-btn").disabled = false;
    $("#save-state").textContent = "unsaved";
  });
  $("#code").addEventListener("keydown", (e) => {
    if (e.key === "Tab") {
      e.preventDefault();
      const t = e.target, s = t.selectionStart;
      t.value = t.value.slice(0, s) + "    " + t.value.slice(t.selectionEnd);
      t.selectionStart = t.selectionEnd = s + 4;
      t.dispatchEvent(new Event("input"));
    }
  });
  $("#new-file-name").addEventListener("keydown", (e) => { if (e.key === "Enter") createFile(); });
  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "s" && ui.tab === "editor") { e.preventDefault(); saveFile(); }
    if (e.key === "Escape") closeModal();
  });
}

(async function init() {
  wire();
  try {
    await loadState();
    resetPipeline();
    if (ui.state.repos.length) await selectRepo(ui.state.repos[0].id);
  } catch (e) { toast(e.message, true); }
  pollAttestor();
  setInterval(pollAttestor, 5000);
})();
