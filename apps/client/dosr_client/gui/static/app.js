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
const fmtBytes = (n) => (n == null ? "—" : n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1048576).toFixed(2)} MB`);
const fmtNum = (n) => (n == null ? "—" : Number(n).toLocaleString());
const sentBytes = (t) => (t ? (t.request_bytes || 0) + (t.policy_bytes || 0) + (t.bundle_bytes || 0) + (t.prompt_bytes || 0) : null);
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
  keys: [],
  keysFile: null,
  armed: {},
  p2p: null,
  chain: null,
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
    const msg = typeof d === "string" ? d : Array.isArray(d) ? d.map((e) => `${(e.loc || []).join(".")}: ${e.msg}`).join("; ") : d && d.error ? d.error : `HTTP ${res.status}`;
    const err = new Error(msg);
    err.detail = d;
    throw err;
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
  await loadKeys();
  const s = ui.state;
  $("#version").textContent = "v" + s.version;
  for (const [id, path] of [["#workspace", s.workspace], ["#chain-path", s.chain_state]]) {
    $(id).textContent = path;
    $(id).title = path;
  }

  const sel = $("#profile");
  sel.innerHTML = s.profiles.map((p) => `<option value="${esc(p.id)}" title="${esc(p.description)}">${esc(p.name)}</option>`).join("");
  sel.value = s.active_profile;
  renderAvatar();

  const bad = $("#bad-request");
  if (!bad.options.length) {
    bad.innerHTML = `<option value="">None — send a normal request</option>` +
      s.bad_requests.map((b) => `<option value="${esc(b.id)}">${esc(b.id)}</option>`).join("");
    bad.onchange = () => {
      const b = s.bad_requests.find((x) => x.id === bad.value);
      $("#bad-request-hint").textContent = b ? b.description : "";
      bad.classList.toggle("is-bad", !!b);
    };
  }

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
  $("#profile").title = p?.description || "";
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
  else if (r.diverged) badges.push(`<span class="badge bad" title="The canonical HEAD moved (another PR landed first) or is not fetched yet">behind the chain — sync</span>`);
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
  $("#commit-form").hidden = !r.changes.length;

  renderModelPicker(r);
}

// Submit tab: only the models this repo's policy approves. Models whose provider key is
// missing are grayed out but selectable (the client then blocks: provider_key_available).
// With "Force send" ticked, any model can be chosen: other common models + a custom box.
function renderModelPicker(r) {
  const choices = r.model_choices || [];
  const force = $("#opt-force").checked;
  const prof = profileModel();
  const firstOk = choices.find((c) => c.approved);
  const def = prof ? "profile: " + prof : "policy: " + (firstOk?.request || "?");
  const opt = (c) => {
    const missing = !keyFor(c.provider).set;
    const range = c.range ? ` · policy allows ${esc(c.spec.split("@").pop())}` : "";
    return `<option value="${esc(c.request)}"${missing ? ' class="no-key"' : ""}>${esc(c.label)} — ${esc(c.request)}${range}${missing ? " · no API key" : ""}</option>`;
  };
  const defModel = prof || firstOk?.request || "";
  const defMissing = defModel.includes("/") && !keyFor(PROVIDER_ALIASES[defModel.split("/")[0]] || defModel.split("/")[0]).set;
  const groups = [`<option value=""${defMissing ? ' class="no-key"' : ""}>Default (${esc(def)})${defMissing ? " · no API key" : ""}</option>`];
  const ok = choices.filter((c) => c.approved);
  if (ok.length) groups.push(`<optgroup label="Approved by this repo's policy">${ok.map(opt).join("")}</optgroup>`);
  if (force) {
    const byProvider = {};
    choices.filter((c) => !c.approved).forEach((c) => (byProvider[c.provider_label] ||= []).push(c));
    Object.entries(byProvider).forEach(([label, cs]) =>
      groups.push(`<optgroup label="${esc(label)}: not approved (forced)">${cs.map(opt).join("")}</optgroup>`));
  }
  const sel = $("#model-select");
  const prev = sel.value;
  sel.innerHTML = groups.join("");
  if ([...sel.options].some((o) => o.value === prev)) sel.value = prev;
  sel.classList.toggle("no-key", !!sel.selectedOptions[0]?.classList.contains("no-key"));
  $("#model-custom-wrap").hidden = !force;
  updateModelHint();
}

function profileModel() {
  const p = activeProfile();
  return p?.review_model ? `${p.review_model.provider}/${p.review_model.model}@${p.review_model.version}` : null;
}

const PROVIDER_ALIASES = { claude: "anthropic", google: "gemini", glm: "zhipu", gpt: "openai" };

// {env, set} for a provider: the key screen's status, else the repo's (custom providers).
function keyFor(provider) {
  if (provider === "mock") return { env: null, set: true };
  const k = (ui.keys || []).find((x) => x.provider === provider);
  if (k) return k;
  const rk = (ui.repo?.provider_keys || {})[provider];
  return rk || { env: provider.toUpperCase().replace(/[^A-Z0-9]/g, "_") + "_API_KEY", set: false };
}

function chosenModel() {
  const custom = $("#opt-force").checked ? $("#model-custom").value.trim() : "";
  return custom || $("#model-select").value || null;
}

function updateModelHint() {
  const r = ui.repo || {};
  const choices = r.model_choices || [];
  const sel = $("#model-select");
  sel.classList.toggle("no-key", !!sel.selectedOptions[0]?.classList.contains("no-key"));
  const text = chosenModel() || profileModel() || choices.find((c) => c.approved)?.request || "";
  if (!text) { $("#model-hint").innerHTML = ""; return; }
  const slash = text.indexOf("/");
  const raw = slash > 0 ? text.slice(0, slash).toLowerCase() : "";
  const provider = PROVIDER_ALIASES[raw] || raw;
  const rest = slash > 0 ? text.slice(slash + 1) : "";
  const known = choices.find((c) => c.request === `${provider}/${rest}`);
  const parts = [];
  if (known) {
    parts.push(known.approved ? `<span class="ok-text">✓ approved by the policy</span>`
                              : `<span class="bad-text">✗ not approved by this policy</span>: the attestor will reject it`);
    if (provider !== "mock") parts.push(`calls <code>${esc(known.api_model)}</code>`);
  } else if (rest && !rest.includes("@")) {
    parts.push("no version: taken from the policy, else the newest known snapshot");
  } else if (rest) {
    parts.push(`<span class="bad-text">not approved by this policy</span>`);
  }
  const k = provider ? keyFor(provider) : null;
  if (provider === "mock") parts.push("mock reviewer: no API key needed");
  else if (k) parts.push(k.set ? `<span class="ok-text">✓ ${esc(k.env)} set</span>: you pay for the review with your own key`
                               : `<span class="bad-text">✗ no ${esc(k.env)}</span>: <a href="#" onclick="openKeys(); return false">add an API key</a> (otherwise the client blocks it)`);
  $("#model-hint").innerHTML = parts.join(" · ");
}

// ------------------------------------------------------------ API keys

async function loadKeys() {
  try {
    const res = await api("/api/keys");
    ui.keys = res.keys;
    ui.keysFile = res.env_file;
  } catch { ui.keys = ui.keys || []; }
}

function renderKeys() {
  $("#keys-file").innerHTML = ui.keysFile
    ? `Saved to <code title="${esc(ui.keysFile)}">${esc(ui.keysFile.split(/[\\/]/).pop())}</code> (git-ignored) and used right away, with no restart. Each key is sent only with your own review requests; the attestor uses it for that one call and never stores it.`
    : "Kept for this session only (start the GUI with <code>--env-file</code> to save them). Each key is sent only with your own review requests.";
  $("#keys-list").innerHTML = (ui.keys || []).map((k) => `
    <div class="key-row" data-provider="${esc(k.provider)}">
      <div class="key-name"><strong>${esc(k.label)}</strong><code class="small">${esc(k.env)}${k.alt_envs.length ? " / " + k.alt_envs.map(esc).join(" / ") : ""}</code></div>
      <div class="key-status">${k.set
        ? `<span class="badge ok">set</span> <code class="key-mask">${esc(k.masked)}</code> <span class="muted small">${k.source === "environment" ? `from the environment (${esc(k.set_via)})` : "saved"}</span>`
        : `<span class="badge">not set</span>`}</div>
      <div class="key-edit">
        <input type="password" class="key-input" autocomplete="new-password" spellcheck="false" placeholder="${k.set ? "Replace key" : "Paste API key"}" aria-label="${esc(k.label)} API key">
        <button type="button" class="btn btn-small btn-primary" data-act="save">Save</button>
        ${k.set ? `<button type="button" class="btn btn-small btn-ghost" data-act="remove">Remove</button>` : ""}
      </div>
    </div>`).join("");
}

async function openKeys() {
  await loadKeys();
  renderKeys();
  $("#keys-modal").hidden = false;
}

async function onKeyAction(e) {
  const btn = e.target.closest("button[data-act]");
  if (!btn) return;
  const row = btn.closest(".key-row");
  const provider = row.dataset.provider;
  const input = row.querySelector(".key-input");
  btn.disabled = true;
  try {
    if (btn.dataset.act === "save") {
      if (!input.value.trim()) { input.focus(); return; }
      await api(`/api/keys/${encodeURIComponent(provider)}`, { method: "PUT", body: { key: input.value } });
      toast("API key saved");
    } else {
      await api(`/api/keys/${encodeURIComponent(provider)}`, { method: "DELETE" });
      toast("API key removed");
    }
    input.value = "";
    await loadKeys();
    renderKeys();
    if (ui.repo) renderModelPicker(ui.repo);
  } catch (err) { toast(err.message, true); } finally { btn.disabled = false; }
}

// ------------------------------------------------------------ new-repo model rows

const DATED = /^\d{4}-\d{2}-\d{2}$|^\d{8}$/;
const DOTTED = /^\d+(\.\d+){0,2}$/;
const CUSTOM = "__custom";

function catalogModels(provider) {
  const out = [];
  ui.state.model_catalog.models.filter((m) => m.provider === provider).forEach((m) => {
    if (!out.some((x) => x.model === m.model)) out.push(m);
  });
  return out;
}

// "openai/gpt-4o@>=2024-08-06" -> a dropdown row if the catalog knows it, else a custom row.
function specToRow(spec) {
  const m = /^([^/]+)\/(.+)@(.+)$/.exec(spec.trim());
  if (m) {
    const provider = PROVIDER_ALIASES[m[1].toLowerCase()] || m[1].toLowerCase();
    let v = m[3], newer = false;
    if (v.startsWith(">=")) { v = v.slice(2); newer = DATED.test(v); }
    else if (v.startsWith("^")) { v = v.slice(1); newer = DOTTED.test(v); }
    const plain = newer || v === m[3];
    if (plain && ui.state.model_catalog.models.some((x) => x.provider === provider && x.model === m[2] && x.version === v)) {
      return { provider, model: m[2], version: v, newer };
    }
  }
  return { provider: CUSTOM, custom: spec };
}

function addModelRow(row) {
  const cat = ui.state.model_catalog;
  row = row || { provider: cat.providers[0].id };
  const div = document.createElement("div");
  div.className = "model-row";
  div.innerHTML = `
    <select class="mp-provider" aria-label="Provider">${cat.providers.map((p) => `<option value="${esc(p.id)}">${esc(p.label)}</option>`).join("")}<option value="${CUSTOM}">Custom…</option></select>
    <select class="mp-model" aria-label="Model"></select>
    <select class="mp-version" aria-label="Version"></select>
    <label class="inline mp-newer-wrap" title="Also accept newer versions (>= a dated snapshot, ^ for x.y.z)"><input type="checkbox" class="mp-newer"> or newer</label>
    <input class="mp-custom" placeholder="provider/model@version, e.g. deepseek/deepseek-chat@v3 or mock/reviewer@^1.2.0" aria-label="Custom model" hidden>
    <button type="button" class="btn btn-small btn-ghost mp-remove" title="Remove this model" aria-label="Remove this model">✕</button>`;
  $("#mp-rows").appendChild(div);
  div.querySelector(".mp-provider").value = row.provider;
  fillRow(div, row);
}

function fillRow(div, row = {}) {
  const provider = div.querySelector(".mp-provider").value;
  const custom = provider === CUSTOM;
  div.classList.toggle("custom", custom);
  div.querySelector(".mp-custom").hidden = !custom;
  ["mp-model", "mp-version"].forEach((c) => (div.querySelector("." + c).hidden = custom));
  div.querySelector(".mp-newer-wrap").hidden = custom;
  if (custom) {
    if (row.custom != null) div.querySelector(".mp-custom").value = row.custom;
    return;
  }
  const modelSel = div.querySelector(".mp-model");
  if (row.model !== undefined || !modelSel.options.length || modelSel.dataset.provider !== provider) {
    modelSel.innerHTML = catalogModels(provider).map((m) => `<option value="${esc(m.model)}">${esc(m.label)}</option>`).join("");
    modelSel.dataset.provider = provider;
    if (row.model) modelSel.value = row.model;
  }
  const versions = ui.state.model_catalog.models.filter((m) => m.provider === provider && m.model === modelSel.value);
  const verSel = div.querySelector(".mp-version");
  verSel.innerHTML = versions.map((v) => `<option value="${esc(v.version)}">${esc(v.version)}</option>`).join("");
  if (row.version) verSel.value = row.version;
  const v = verSel.value || "";
  const rangeable = DATED.test(v) || (DOTTED.test(v) && !/^\d{3}$/.test(v));
  const box = div.querySelector(".mp-newer");
  box.disabled = !rangeable;
  box.checked = rangeable && (row.newer ?? box.checked);
  div.querySelector(".mp-newer-wrap").classList.toggle("disabled", !rangeable);
}

function onModelRowsChange(e) {
  const div = e.target.closest(".model-row");
  if (!div) return;
  if (e.target.classList.contains("mp-provider")) fillRow(div, { newer: false });
  else if (e.target.classList.contains("mp-model")) fillRow(div, { newer: false });
  else if (e.target.classList.contains("mp-version")) fillRow(div);
}

function setModelRows(specs) {
  $("#mp-rows").innerHTML = "";
  specs.forEach((s) => addModelRow(specToRow(s)));
  if (!specs.length) addModelRow();
}

function modelRowSpecs() {
  return [...document.querySelectorAll("#mp-rows .model-row")].map((div) => {
    if (div.classList.contains("custom")) return div.querySelector(".mp-custom").value.trim();
    const p = div.querySelector(".mp-provider").value, m = div.querySelector(".mp-model").value;
    const v = div.querySelector(".mp-version").value;
    if (!m || !v) return "";
    const newer = div.querySelector(".mp-newer").checked;
    return `${p}/${m}@${newer ? (DATED.test(v) ? ">=" : "^") + v : v}`;
  }).filter(Boolean);
}

function presetModelSpecs() {
  const p = ui.state.presets.find((x) => x.id === $("#nr-preset").value);
  return p ? p.policy.review.approved_models.map((m) => `${m.provider}/${m.model}@${m.version}`) : [];
}

function switchTab(tab) {
  ui.tab = tab;
  document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  document.querySelectorAll(".tab-panel").forEach((p) => (p.hidden = p.dataset.panel !== tab));
  if (tab === "editor") loadFiles();
  if (tab === "changes") loadDiff();
  if (tab === "history") loadHistory();
  if (tab === "config") loadConfig();
  if (tab === "share") loadShare();
  if (tab === "chain") loadRepoChain();
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

async function commitChanges(ev) {
  ev.preventDefault();
  const message = $("#commit-input").value.trim();
  if (!message) { toast("Write a commit message first", true); return; }
  try {
    const r = await api(`/api/repos/${encodeURIComponent(ui.repoId)}/commit`, { method: "POST", body: { message } });
    $("#commit-input").value = "";
    toast(`Committed ${r.commit.slice(0, 10)}. Submit PR sends it to DOSR.`);
    await Promise.all([loadDiff(), refreshRepo()]);
  } catch (e) { toast(e.message, true); }
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

const OUTCOME_LABEL = {
  approved: "Approved", rejected: "Rejected by the attestor", blocked: "Blocked by your client — nothing was sent",
  error: "Submission failed", "dry-run": "Dry run complete",
  conflict: "Approved, but another PR landed first",
};

function checksCard(info) {
  const local = Object.fromEntries((info.local_checks || []).map((c) => [c.name, c]));
  const remote = Object.fromEntries((info.attestor_checks || []).map((c) => [c.name, c]));
  const names = [...Object.keys(local), ...Object.keys(remote).filter((n) => !(n in local))];
  if (!names.length) return "";
  const sent = !!info.attestor_checks;
  const mark = (c, shown) => (!c ? `<span class="muted">${shown ? "·" : ""}</span>` : c.ok === true ? `<span class="ok-text">✓</span>` : c.ok === false ? `<span class="bad-text">✗</span>` : `<span class="muted">–</span>`);
  const count = (list) => list ? `${list.filter((c) => c.ok === true).length}/${list.length} passed` : "nothing sent";
  const rows = names.map((n) => {
    const l = local[n], r = remote[n];
    const failed = (l && l.ok === false) || (r && r.ok === false);
    const detail = (r && r.ok === false && r.detail) || (l && l.ok === false && l.detail) || (l && l.ok === null && l.detail) || "";
    return `<tr class="${failed ? "failed" : ""}"><td><code>${esc(n)}</code></td><td class="c">${mark(l, true)}</td><td class="c">${mark(r, sent)}</td><td class="small">${esc(detail)}</td></tr>`;
  }).join("");
  return `<div class="card wide"><h3>Checks: your client (before sending) vs the attestor</h3>
    <p class="muted small">Same check names on both sides. The client runs them on the exact bytes it is about to send, so it can stop a bad request; the attestor re-runs them because a modified client could skip that. Client: ${count(info.local_checks)} · attestor: ${count(info.attestor_checks)}.</p>
    <table class="mini checks-table"><thead><tr><th>Check</th><th class="c">Client</th><th class="c">Attestor</th><th>Detail</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

function attestationCard(d) {
  if (!d) return "";
  const h = (v) => (v ? `<code title="${esc(v)}">${esc(v.slice(0, 22))}…</code>` : "—");
  return `<div class="card"><h3>Signed attestation</h3><dl class="kv">
    <dt>Decided by</dt><dd>${esc(d.decided_by || "—")}${d.model_called ? "" : ' <span class="muted">(model not called)</span>'}</dd>
    <dt>Signer</dt><dd>${esc(d.attestor_key_id)} <code class="small">${esc(d.signer)}</code></dd>
    <dt>Request hash</dt><dd>${h(d.request_hash)}</dd>
    <dt>Prompt hash</dt><dd>${h(d.review_input_hash)}</dd>
    <dt>Model output hash</dt><dd>${h(d.response_hash)}</dd>
    <dt>Signature</dt><dd>${h(d.signature)} <span class="muted small">EIP-712 secp256k1</span></dd>
    ${d.expires_at ? `<dt>Expires</dt><dd>${new Date(d.expires_at * 1000).toLocaleString()}</dd>` : ""}
  </dl></div>`;
}

function artifactViewer(repoId, requestId) {
  if (!repoId || !requestId) return "";
  return `<div class="card wide"><h3>Artifacts</h3>
    <div class="subtabs" data-repo="${esc(repoId)}" data-req="${esc(requestId)}">
      <button type="button" class="btn btn-small" data-v="prompt">Prompt sent to attestor</button>
      <button type="button" class="btn btn-small" data-v="pr_markdown">PR document</button>
      <button type="button" class="btn btn-small" data-v="request">Request JSON</button>
      <button type="button" class="btn btn-small" data-v="response">Attestor response</button>
    </div><pre class="block artifact-view" hidden></pre></div>`;
}

function wireArtifactViewer(target) {
  const tabs = target.querySelector(".subtabs[data-req]");
  if (!tabs) return;
  let cache = null;
  tabs.querySelectorAll("button").forEach((b) => (b.onclick = async () => {
    const view = target.querySelector(".artifact-view");
    try {
      cache = cache || await api(`/api/repos/${encodeURIComponent(tabs.dataset.repo)}/submissions/${encodeURIComponent(tabs.dataset.req)}`);
    } catch (e) { toast(e.message, true); return; }
    const v = cache[b.dataset.v];
    tabs.querySelectorAll("button").forEach((x) => x.classList.toggle("active", x === b));
    view.hidden = false;
    view.textContent = v == null ? "(not produced by this submission)" : typeof v === "string" ? v : JSON.stringify(v, null, 2);
  }));
}

function renderResult(job, target, repoId) {
  const labels = OUTCOME_LABEL;
  const d = job.decision;
  const info = job.info || {};
  const kv = [];
  if (job.outcome === "blocked") {
    const failed = (info.local_checks || []).filter((c) => c.ok === false);
    kv.push(["Why", `<ul class="issues">${failed.map((c) => `<li><code>${esc(c.name)}</code> ${esc(c.detail)}</li>`).join("")}</ul>`]);
    kv.push(["Next", `Fix the problem, or tick <em>Force send</em> to see the attestor reject it.`]);
  }
  if (d) {
    kv.push(["Summary", esc(d.summary)]);
    if (!d.approved && d.issues && d.issues.length) {
      kv.push(["Issues", `<ul class="issues">${d.issues.map((i) => `<li>${esc(i)}</li>`).join("")}</ul>`]);
    }
    kv.push(["Decided by", esc(d.decided_by || "—")]);
    kv.push(["Network", `upload ${fmtMs(d.upload_ms)} · server ${fmtMs(d.server_wait_ms)}`]);
  }
  if (info.pr) kv.push(["PR", `${esc(info.pr.title)} <span class="muted">· ${info.pr.changed_files} file(s) +${info.pr.additions}/-${info.pr.deletions}</span>`]);
  if (info.parent_git_oid) kv.push(["Transition", `<code>${esc(shortOid(info.parent_git_oid))}</code> → <code>${esc(shortOid(info.candidate_git_oid))}</code>`]);
  if (info.model) kv.push(["Model", `<code>${esc(d ? d.model : info.model)}</code>`]);
  if (info.pr?.context_files?.length) kv.push(["Context files", info.pr.context_files.map((f) => `<code>${esc(f)}</code>`).join(" ")]);
  if (info.prompt) kv.push(["Prompt", `${fmtNum(info.prompt.words)} words · ~${fmtNum(info.prompt.est_tokens)} tokens · <code title="${esc(info.prompt.sha256)}">sha256 ${esc(info.prompt.sha256.slice(0, 18))}…</code>`]);
  if (info.forced) kv.push(["Forced", `<span class="badge warn">local validation skipped (simulated modified client)</span>`]);
  if (info.chain_event) kv.push(["Chain", chainEventHtml(info.chain_event)]);
  if (info.chain_conflict) {
    kv.push(["Chain", `<span class="bad-text">${esc(info.chain_conflict.error)}</span>`]);
    kv.push(["Next", `The contract keeps one history: the PR that was mined first on this parent won. Your attestation is still valid evidence, but its parent is no longer HEAD. <button type="button" class="btn btn-small btn-primary" data-sync="1">Sync &amp; rebase onto the new HEAD</button> then submit again (the attestor reviews the rebased commit).`]);
  }
  if (info.published) kv.push(["Shared", `share code ${shareCodeHtml(info.published.share)} <span class="muted small">${info.published.backend === "ipfs" ? "on IPFS" : "local store only (IPFS not running)"}${announcedText(info.published.announced)}</span>`]);
  const m = info.pr_metrics?.total;
  if (m) kv.push(["PR size", `${fmtNum(m.words)} words · ${fmtNum(m.chars)} chars · ~${fmtNum(m.est_tokens)} tokens <span class="muted">(est. chars/4)</span>`]);
  const t = info.transfer;
  if (t) kv.push(["Transfer", `sent ${fmtBytes(sentBytes(t))} <span class="muted">(request ${fmtBytes(t.request_bytes)} · policy ${fmtBytes(t.policy_bytes)} · bundle ${fmtBytes(t.bundle_bytes)} · prompt ${fmtBytes(t.prompt_bytes)})</span> · received ${fmtBytes(t.response_bytes)}`]);
  if (info.bad_request) kv.push(["Demo", `<span class="badge warn">tampered request: ${esc(info.bad_request)}</span>`]);
  kv.push(["Total time", fmtMs(job.elapsed_ms)]);
  kv.push(["Artifacts", `<code class="small">${esc(job.artifacts_dir)}</code>`]);

  const measured = job.steps.filter((s) => s.duration_ms != null);
  const longest = Math.max(1, ...measured.map((s) => s.duration_ms));
  const lat = measured.map((s) => `<div class="lat-row"><span>${esc(s.label)}</span>
      <div><div class="lat-bar ${s.status === "failed" ? "failed" : ""}" style="width:${(100 * s.duration_ms) / longest}%"></div></div>
      <span class="t">${fmtMs(s.duration_ms)}</span></div>`).join("");

  const secs = info.pr_metrics?.sections;
  const sizeCard = secs ? `<div class="card"><h3>PR size by section</h3>
    <table class="mini"><thead><tr><th>Section</th><th class="num">Words</th><th class="num">Chars</th><th class="num">Lines</th><th class="num">~Tokens</th></tr></thead><tbody>
    ${Object.entries(secs).map(([k, v]) => `<tr><td>${esc(k.replace("_", " "))}</td><td class="num">${fmtNum(v.words)}</td><td class="num">${fmtNum(v.chars)}</td><td class="num">${fmtNum(v.lines)}</td><td class="num">${fmtNum(v.est_tokens)}</td></tr>`).join("")}
    <tr class="total"><td>Rendered PR (total)</td><td class="num">${fmtNum(m.words)}</td><td class="num">${fmtNum(m.chars)}</td><td class="num">${fmtNum(m.lines)}</td><td class="num">${fmtNum(m.est_tokens)}</td></tr>
    </tbody></table><p class="muted small">Total includes markdown headings and metadata, so it exceeds the sum of sections. Token counts are a rough chars/4 estimate.</p></div>` : "";

  target.innerHTML = `
    <div class="decision ${job.outcome}">
      <h2>${labels[job.outcome] || esc(job.outcome)}</h2>
      <dl class="kv">${kv.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("")}</dl>
    </div>
    ${job.errors.length && job.outcome !== "blocked" ? `<div class="error-box"><strong>Errors</strong><ul>${job.errors.map((e) => `<li>${esc(e)}</li>`).join("")}</ul></div>` : ""}
    ${job.warnings.length ? `<div class="warn-box"><strong>Warnings</strong><ul>${job.warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul></div>` : ""}
    <div class="result-cards">
      ${checksCard(info)}
      ${attestationCard(d)}
      ${lat ? `<div class="card"><h3>Latency by step</h3>${lat}</div>` : ""}
      ${sizeCard}
      ${artifactViewer(repoId, job.request_id)}
    </div>`;
  target.hidden = false;
  wireArtifactViewer(target);
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
        model: chosenModel(),
        register_on_chain: $("#opt-register").checked,
        dry_run: $("#opt-dry").checked,
        bad_request: $("#bad-request").value || null,
        force: $("#opt-force").checked,
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
      renderResult(job, $("#result"), ui.repoId);
      if (job.outcome === "approved" || job.outcome === "rejected") $("#commit-msg").value = "";
      if ($("#bad-request").value) { $("#bad-request").value = ""; $("#bad-request").onchange(); }
      $("#opt-force").checked = false;
      $("#submit-btn").classList.remove("btn-danger");
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
        <td class="num">${fmtNum(r.info?.pr_metrics?.total?.words)}</td>
        <td class="num">${r.info?.transfer ? `${fmtBytes(sentBytes(r.info.transfer))} / ${fmtBytes(r.info.transfer.response_bytes)}` : "—"}</td>
        <td class="num">${fmtMs(r.elapsed_ms)}</td></tr>`).join("")
    : `<tr><td colspan="8" class="muted">No submissions yet.</td></tr>`;
  tbody.querySelectorAll("tr[data-id]").forEach((tr) => (tr.onclick = () => showSubmission(tr.dataset.id)));
  $("#history-detail").hidden = true;
}

async function showSubmission(id) {
  const s = await api(`/api/repos/${encodeURIComponent(ui.repoId)}/submissions/${encodeURIComponent(id)}`);
  const el = $("#history-detail");
  el.innerHTML = `<h3>Submission ${esc(id)}</h3><div id="hd-result" class="result"></div>`;
  renderResult(s.run, $("#hd-result"), ui.repoId);
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
        ["Minimum approvals", p.review.minimum_approvals],
        ["PR template", `<code>${esc(p.review.prompt_template)}</code> <span class="badge">${r.template?.builtin ? "built-in" : "custom, embedded"}</span>`
          + (r.template?.hash ? `<br><span class="muted small">hash <code>${esc(r.template.hash.slice(0, 18))}…</code></span>` : "")
          + ` <button class="btn btn-small btn-ghost" onclick="openTemplateEditor({repo: ui.repoId})">View</button>`],
        ["Extra context files", (p.review.context_files || []).map((g) => `<code>${esc(g)}</code>`).join(" ") || '<span class="muted">none (template defaults only)</span>'],
        ["Limits", `${p.review.max_changed_files} files · ${p.review.max_patch_bytes} patch bytes`],
        ["Attestors", p.attestors.map((a) => `${esc(a.key_id)} <code class="small">${esc(a.address)}</code>`).join("<br>")],
        ["Storage", `${esc(p.storage.bundle_format)} via ${esc(p.storage.distribution)}`]]) : `<div class="error-box">${esc(r.policy_error || "policy unreadable")}</div>`}</div>
      <div class="card"><h3>Chain record <span class="muted small">${esc(chain.kind === "evm" ? "DOSRRegistry contract" : "mock chain file")}</span></h3>${onchain ? rows([
        ["Canonical HEAD", `<code>${esc(onchain.canonical_head)}</code>`], ["Genesis", `<code>${esc(onchain.genesis_git_oid)}</code>`],
        ["Accepted transitions", onchain.accepted_transition_count], ["Registered at block", onchain.registered_block],
        ["Registered attestors", (onchain.attestors || []).map((a) => `${esc(a.key_id)} <code class="small">${esc(a.address)}</code>`).join("<br>") || "—"],
        ["Recent events", onchain.events.slice(-5).reverse().map((e) => `block ${e.block}: <code>${esc(shortOid(e.parent_git_oid, 8))}</code> → <code>${esc(shortOid(e.candidate_git_oid, 8))}</code>`).join("<br>") || "—"],
        ["Details", `<button class="btn btn-small btn-ghost" onclick="switchTab('chain')">Chain tab →</button>`]])
        : `<p class="muted">Not registered.</p><button class="btn btn-small" onclick="registerRepo()">Register genesis now</button>`}</div>
      <div class="card wide"><h3>policy.json</h3><pre class="block">${esc(JSON.stringify(p, null, 2))}</pre></div>
    </div>`;
}

async function registerRepo() {
  try {
    await api(`/api/repos/${encodeURIComponent(ui.repoId)}/register`, { method: "POST" });
    toast("Registered on the chain");
    await refreshRepo();
    loadConfig();
  } catch (e) { toast(e.message, true); }
}

// ------------------------------------------------------------ new repo

function openNewRepo() {
  const s = ui.state;
  $("#nr-seed").innerHTML = `<option value="">(empty project)</option>` + s.seeds.map((x) => `<option value="${esc(x.id)}">${esc(x.name || x.id)}</option>`).join("");
  $("#nr-preset").innerHTML = s.presets.map((p) => `<option value="${esc(p.id)}">${esc(p.id)} — ${esc(p.description)}</option>`).join("");
  $("#nr-template").innerHTML = templateOptions("");
  updateTemplateHint();
  $("#nr-error").hidden = true;
  ["#nr-name", "#nr-desc", "#nr-context", "#nr-max-files", "#nr-max-bytes"].forEach((id) => ($(id).value = ""));
  updatePresetPreview();
  setModelRows(presetModelSpecs());
  $("#modal").hidden = false;
  $("#nr-name").focus();
}

function closeModal() { $("#modal").hidden = true; $("#keys-modal").hidden = true; }

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
  const over = seed.policy_overrides || {};
  setModelRows(over.approved_models?.length ? over.approved_models : presetModelSpecs());
  $("#nr-template").innerHTML = templateOptions(over.template || "");
  updateTemplateHint();
  $("#nr-context").value = (over.context_files || []).join(", ");
}

async function createRepo(ev) {
  ev.preventDefault();
  const btn = $("#nr-submit");
  btn.disabled = true;
  btn.textContent = "Creating…";
  const num = (id) => ($(id).value ? parseInt($(id).value, 10) : null);
  try {
    const models = modelRowSpecs();
    if (!models.length) throw new Error("Add at least one approved model.");
    const context = $("#nr-context").value.split(",").map((m) => m.trim()).filter(Boolean);
    const res = await api("/api/repos", {
      method: "POST",
      body: {
        name: $("#nr-name").value.trim(), description: $("#nr-desc").value.trim(), preset: $("#nr-preset").value,
        seed: $("#nr-seed").value || null, approved_models: models,
        prompt_template: $("#nr-template").value || null, max_changed_files: num("#nr-max-files"), max_patch_bytes: num("#nr-max-bytes"),
        context_files: context.length ? context : null,
      },
    });
    closeModal();
    toast(`Created ${res.repo.name} (genesis ${shortOid(res.genesis_git_oid)}) in ${fmtMs(res.took_ms)}` + (res.published ? " · published for sharing" : ""));
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

// ------------------------------------------------------------ PR templates

function templateOptions(selected) {
  const ts = ui.state.templates || [];
  const builtin = ts.filter((t) => t.source === "built-in");
  const files = ts.filter((t) => t.source === "file" && !t.error);
  const o = (t) => `<option value="${esc(t.id)}" title="${esc(t.description)}"${t.id === selected ? " selected" : ""}>${esc(t.id)}</option>`;
  return `<option value="">(preset default)</option>`
    + `<optgroup label="Built-in">${builtin.map(o).join("")}</optgroup>`
    + (files.length ? `<optgroup label="Your templates">${files.map(o).join("")}</optgroup>` : "");
}

function updateTemplateHint() {
  const id = $("#nr-template").value;
  const t = (ui.state.templates || []).find((x) => x.id === id);
  const bad = (ui.state.templates || []).filter((x) => x.error);
  $("#nr-template-hint").innerHTML = (t ? esc(t.description) + (t.source === "file" ? " · custom: embedded in the policy" : "") : "Uses the preset's template.")
    + (bad.length ? `<br><span class="bad-text">${bad.length} template file(s) have errors: ${bad.map((b) => esc(b.error)).join("; ")}</span>` : "");
}

// The editor: view a template (read-only), or create/edit one and save it as a file.
async function openTemplateEditor({ id = null, repo = null, base = null } = {}) {
  ui.tpl = { readonly: false };
  $("#tpl-status").innerHTML = "";
  $("#tpl-preview").textContent = "";
  $("#tpl-base").innerHTML = (ui.state.templates || []).filter((t) => !t.error)
    .map((t) => `<option value="${esc(t.id)}">${esc(t.id)}</option>`).join("");
  let view = null;
  try {
    if (repo) view = await api(`/api/repos/${encodeURIComponent(repo)}/template`);
    else if (id) view = await api(`/api/templates/${encodeURIComponent(id)}`);
  } catch (e) { toast(e.message, true); return; }
  if (view) {
    // Built-ins and repositories' embedded templates are fixed: copy to change them.
    ui.tpl.readonly = view.builtin || !!repo;
    $("#tpl-title").textContent = ui.tpl.readonly ? `PR template ${view.id}` : `Edit template ${view.id}`;
    $("#tpl-text").value = view.toml;
    showTemplateView(view);
    $("#tpl-base").value = view.id;
    $("#tpl-new-id").value = nextTemplateId(view.id);
  } else {
    $("#tpl-title").textContent = "New PR template";
    $("#tpl-base").value = base || $("#nr-template").value || "standard-code-review-v1";
    if (!$("#tpl-base").value) $("#tpl-base").selectedIndex = 0;
    $("#tpl-new-id").value = "my-review-v1";
    await scaffoldTemplate();
  }
  $("#tpl-text").readOnly = ui.tpl.readonly;
  $("#tpl-save").hidden = ui.tpl.readonly;
  $("#tpl-readonly-note").hidden = !ui.tpl.readonly;
  $("#tpl-modal").hidden = false;
}

function nextTemplateId(id) {
  const m = /^(.*-v)(\d+)$/.exec(id);
  return m ? `${m[1]}${+m[2] + 1}` : `${id}-v2`;
}

async function scaffoldTemplate() {
  const id = $("#tpl-new-id").value.trim(), base = $("#tpl-base").value;
  try {
    const res = await api(`/api/template-scaffold?id=${encodeURIComponent(id)}&base=${encodeURIComponent(base)}`);
    $("#tpl-text").value = res.toml;
    $("#tpl-text").readOnly = false;
    $("#tpl-save").hidden = false;
    $("#tpl-readonly-note").hidden = true;
    $("#tpl-title").textContent = "New PR template";
    ui.tpl.readonly = false;
    await checkTemplate();
  } catch (e) {
    $("#tpl-status").innerHTML = `<span class="bad-text">✗ ${esc(e.message)}</span>`;
  }
}

function showTemplateView(v) {
  $("#tpl-status").innerHTML = `<span class="ok-text">✓ valid</span> <code>${esc(v.id)}</code> · ${esc(v.description)}`
    + `<br><span class="muted small">hash <code>${esc(v.hash)}</code> · layout: ${v.sections.map(esc).join(" → ")}</span>`;
  $("#tpl-preview").textContent = v.preview;
}

async function checkTemplate() {
  const res = await api("/api/templates/check", { method: "POST", body: { text: $("#tpl-text").value } });
  if (res.ok) showTemplateView(res);
  else {
    $("#tpl-status").innerHTML = `<span class="bad-text">✗ ${esc(res.error)}</span>`;
    $("#tpl-preview").textContent = "";
  }
  return res;
}

async function saveTemplate(overwrite = false) {
  const res = await checkTemplate();
  if (!res.ok) return;
  try {
    const saved = await api("/api/templates", { method: "POST", body: { text: $("#tpl-text").value, overwrite } });
    toast(`Saved template ${saved.id} as ${saved.saved.split(/[\\/]/).pop()}`);
    ui.state.templates = (await api("/api/templates")).templates;
    if (!$("#modal").hidden) {
      $("#nr-template").innerHTML = templateOptions(saved.id);
      updateTemplateHint();
    }
    closeTemplateEditor();
  } catch (e) {
    if (/already exists/.test(e.message) && !overwrite) {
      $("#tpl-status").innerHTML = `<span class="bad-text">✗ ${esc(e.message)}</span> <button type="button" class="btn btn-small" onclick="saveTemplate(true)">Overwrite</button>`;
    } else toast(e.message, true);
  }
}

function closeTemplateEditor() { $("#tpl-modal").hidden = true; }

// ------------------------------------------------------------ P2P (IPFS sharing)

function shareCodeHtml(code) {
  return `<code class="small" title="${esc(code)}">${esc(code.length > 30 ? code.slice(0, 18) + "…" + code.slice(-8) : code)}</code> <button type="button" class="btn btn-small btn-ghost" data-copy="${esc(code)}">Copy</button>`;
}

function announcedText(list) {
  if (!list || !list.length) return "";
  return ` · sent to ${list.filter((a) => a.ok).length}/${list.length} peer(s)`;
}

async function copyText(text) {
  try { await navigator.clipboard.writeText(text); toast("Copied"); }
  catch {
    const t = document.createElement("textarea");
    t.value = text; document.body.appendChild(t); t.select();
    try { document.execCommand("copy"); toast("Copied"); } catch { toast("Copy failed: select the text instead", true); }
    t.remove();
  }
}

const ago = (ts) => {
  if (!ts) return "";
  const s = Math.max(0, Date.now() / 1000 - ts);
  return s < 60 ? "just now" : s < 3600 ? `${Math.round(s / 60)} min ago` : s < 86400 ? `${Math.round(s / 3600)} h ago` : new Date(ts * 1000).toLocaleString();
};

async function pollP2P() {
  const pill = $("#p2p-pill");
  try {
    const p = (ui.p2p = await api("/api/p2p"));
    if (!p.enabled) {
      pill.className = "pill pill-btn pill-down";
      $("#p2p-text").textContent = "P2P off";
    } else if (p.backend === "ipfs") {
      pill.className = "pill pill-btn pill-up";
      $("#p2p-text").textContent = `IPFS · ${p.ipfs.peers ?? "?"} peer${p.ipfs.peers === 1 ? "" : "s"}`;
      pill.title = `IPFS node ${p.ipfs.peer_id}\n${p.ipfs.agent || ""}\nClick for peers, inbox and import`;
    } else {
      pill.className = "pill pill-btn pill-local";
      $("#p2p-text").textContent = "local store (no IPFS)";
      pill.title = `${p.ipfs_error || "IPFS not running"}\nPublishing still works on this computer. Start IPFS to share with other devices.`;
    }
    const n = p.inbox_new || 0;
    $("#inbox-count").hidden = !n;
    $("#inbox-count").textContent = n;
    if (!$("#p2p-modal").hidden) renderP2P(false);
  } catch {
    pill.className = "pill pill-btn pill-down";
    $("#p2p-text").textContent = "P2P ?";
  }
}

async function openP2P(focus) {
  $("#p2p-modal").hidden = false;
  $("#import-result").innerHTML = "";
  await renderP2P(true);
  if (focus === "import") $("#import-share").focus();
}

function closeP2P() { $("#p2p-modal").hidden = true; }

async function renderP2P(full) {
  const p = ui.p2p || (ui.p2p = await api("/api/p2p"));
  const { items } = await api("/api/p2p/inbox");
  const visible = items.filter((i) => i.status !== "dismissed");
  $("#inbox-list").innerHTML = visible.length ? visible.map((i) => {
    const same = i.local_head && i.local_head === i.canonical_head;
    const what = i.local_repo ? (same ? "you already have this HEAD" : `updates your <strong>${esc(i.local_repo)}</strong>`) : "new repository";
    return `<div class="inbox-item ${i.status === "new" ? "new" : ""}">
      <div class="what"><strong>${esc(i.name)}</strong> @ <code>${esc(shortOid(i.canonical_head, 10))}</code> · ${i.accepted_transitions} accepted PR(s)
        <div class="muted small">from ${esc(i.from_name || "?")}${i.device ? " on " + esc(i.device) : ""} · ${ago(i.received_at)} · ${what}${i.status === "pulled" ? " · pulled" : ""}</div>
        <div class="muted small"><code>${esc(i.share)}</code></div></div>
      <div class="acts">
        <button type="button" class="btn btn-small ${same ? "" : "btn-primary"}" data-pull="${esc(i.id)}">${i.local_repo ? "Pull" : "Import"}</button>
        <button type="button" class="btn btn-small btn-ghost" data-dismiss="${esc(i.id)}" title="Hide">✕</button>
      </div></div>`;
  }).join("") : `<p class="muted small">Nothing yet. When a peer publishes, its share code shows up here${p.inbox_url ? "" : " (start the GUI with --p2p-port, as demo.py does, to receive them)"}.</p>`;
  $("#inbox-list").querySelectorAll("[data-pull]").forEach((b) => (b.onclick = () => {
    const item = items.find((x) => x.id === b.dataset.pull);
    runImport(item.share, null, item.id, b);
  }));
  $("#inbox-list").querySelectorAll("[data-dismiss]").forEach((b) => (b.onclick = async () => {
    await api(`/api/p2p/inbox/${encodeURIComponent(b.dataset.dismiss)}/dismiss`, { method: "POST" });
    pollP2P();
  }));

  $("#peer-list").innerHTML = (p.peers || []).length ? p.peers.map((x) => `<div class="peer-item">
      <div><strong>${esc(x.device || "device")}</strong> <code class="small">${esc(x.url)}</code>
        <div class="muted small">${x.via === "introduced" ? "added you" : "added by you"}${x.last_seen ? " · seen " + ago(x.last_seen) : ""}${x.peer_id ? ` · IPFS <code>${esc(x.peer_id.slice(0, 14))}…</code>` : ""}</div></div>
      <div class="acts"><button type="button" class="btn btn-small btn-ghost" data-rm="${esc(x.url)}">Remove</button></div></div>`).join("")
    : `<p class="muted small">No peers yet. Add the other device's address (shown under <em>This device</em> on its screen). Optional: you can always paste share codes by hand.</p>`;
  $("#peer-list").querySelectorAll("[data-rm]").forEach((b) => (b.onclick = async () => {
    await api(`/api/p2p/peers?url=${encodeURIComponent(b.dataset.rm)}`, { method: "DELETE" });
    pollP2P();
  }));

  if (!full) return;
  const rows = [["Device", esc(p.device)]];
  rows.push(["Your address", p.inbox_url
    ? `<code>${esc(p.inbox_url)}</code> <button type="button" class="btn btn-small btn-ghost" data-copy="${esc(p.inbox_url)}">Copy</button> <span class="muted small">give this to the other device (Add peer)</span>`
    : `<span class="muted">peer inbox not running (start with <code>demo.py up</code> or <code>dosr gui --p2p-port 8790</code>)</span>`]);
  if (p.backend === "ipfs") {
    rows.push(["Storage", `<span class="ok-text">IPFS</span> <code class="small">${esc(p.ipfs_api)}</code> · ${esc(p.ipfs.agent || "")} · ${p.ipfs.peers ?? "?"} connected peer(s)`]);
    rows.push(["IPFS peer ID", `<code class="small">${esc(p.ipfs.peer_id)}</code>`]);
    rows.push(["IPFS addresses", (p.ipfs_addrs || []).length ? `<ul class="addr-list">${p.ipfs_addrs.map((a) => `<li><code>${esc(a)}</code></li>`).join("")}</ul>` : "—"]);
    rows.push(["Dial a node", `<form class="import-form" onsubmit="dialAddr(event)"><input id="dial-addr" placeholder="/ip4/192.168.1.20/tcp/4001/p2p/12D3Koo…" spellcheck="false" aria-label="IPFS multiaddress"><button class="btn btn-small" type="submit">Connect</button></form>`]);
  } else if (p.enabled) {
    rows.push(["Storage", `<span class="bad-text">local store</span> <code class="small">${esc(p.local_store)}</code><br><span class="muted small">${esc(p.ipfs_error || "IPFS not running")}. Share codes made now (<code>local:…</code>) only work on this computer. Start IPFS (<code>demo.py ipfs</code>) to share with another device.</span>`]);
  }
  $("#device-info").innerHTML = `<dl class="kv">${rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("")}</dl>`;
}

async function submitImport(ev) {
  ev.preventDefault();
  await runImport($("#import-share").value.trim(), $("#import-head").value.trim() || null, null, $("#import-submit"));
}

async function runImport(share, expectHead, inboxId, btn, adopt = false) {
  const old = btn.textContent;
  btn.disabled = true;
  btn.textContent = "Fetching…";
  $("#import-result").innerHTML = `<p class="muted small">Fetching the share manifest and bundle${ui.p2p?.backend === "ipfs" ? " over IPFS (up to a minute if the other device is hard to reach)" : ""}…</p>`;
  try {
    const r = await api("/api/p2p/import", { method: "POST", body: { share, expect_head: expectHead, inbox_id: inboxId, adopt } });
    const verb = r.action === "clone" ? "Cloned" : r.status === "up-to-date" ? "Already up to date:" : r.status === "older" ? "Nothing to apply:" : r.status === "replaced" ? "Adopted their history for" : "Pulled";
    const applied = r.applied.map((a) => `<li>block ${a.block}: <code>${esc(shortOid(a.parent, 8))}</code> → <code>${esc(shortOid(a.candidate, 8))}</code> · signed by <code>${esc(a.attestor.slice(0, 10))}…</code> · ${esc(a.model)}</li>`).join("");
    $("#import-result").innerHTML = `<div class="decision approved"><h2>${verb} ${esc(r.name)}</h2><dl class="kv">
      <dt>Canonical HEAD</dt><dd><code>${esc(r.canonical_head)}</code></dd>
      <dt>Verified</dt><dd>bundle sha256 ✓ · history starts at the genesis with the registered policy ✓ · ${r.applied.length} signed transition(s) checked and applied${r.registered ? " · genesis registered on your mock chain (trusted from the share code)" : ""}${applied ? `<ul class="steps-list">${applied}</ul>` : ""}</dd>
      <dt>Working copy</dt><dd>${esc(r.working_copy)}</dd>
      <dt>From</dt><dd>${esc(r.published_by || "?")}${r.device ? " on " + esc(r.device) : ""} · ${fmtBytes(r.bundle_bytes)} in ${fmtMs(r.took_ms)}</dd>
      </dl></div>${r.warnings.length ? `<div class="warn-box"><ul>${r.warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul></div>` : ""}`;
    toast(`${verb} ${r.name}`);
    $("#import-share").value = "";
    $("#import-head").value = "";
    await loadState();
    await pollP2P();
    if (r.id === ui.repoId) { await refreshRepo(); switchTab(ui.tab); }
    else if (r.action === "clone") await selectRepo(r.id);
  } catch (e) {
    const conflict = /^conflict/.test(e.message);
    $("#import-result").innerHTML = `<div class="error-box"><strong>Import failed.</strong> ${esc(e.message)}
      ${conflict ? `<p class="small">Without a real chain, both devices accepted a change on the same parent. Dev fix: keep the other device's history and drop yours for this repo.</p><button type="button" class="btn btn-small btn-danger" id="adopt-btn">Adopt their history (dev)</button>` : ""}</div>`;
    if (conflict) $("#adopt-btn").onclick = () => runImport(share, expectHead, inboxId, $("#adopt-btn"), true);
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
}

async function addPeer(ev) {
  ev.preventDefault();
  const url = $("#peer-url").value.trim();
  if (!url) return;
  const btn = $("#peer-add");
  btn.disabled = true;
  btn.textContent = "Connecting…";
  try {
    const r = await api("/api/p2p/peers", { method: "POST", body: { url } });
    const ok = (r.dialed || []).filter((d) => d.ok).length;
    toast(`Added ${r.peer.device || r.peer.url}` + (r.dialed?.length ? ` · IPFS connected (${ok}/${r.dialed.length} addresses)` : ""));
    $("#peer-url").value = "";
    await pollP2P();
    renderP2P(true);
  } catch (e) { toast(e.message, true); } finally { btn.disabled = false; btn.textContent = "Add peer"; }
}

async function dialAddr(ev) {
  ev.preventDefault();
  try {
    const r = await api("/api/p2p/connect", { method: "POST", body: { addr: $("#dial-addr").value } });
    const res = r.results[0];
    toast(res.ok ? "Connected" : res.error, !res.ok);
    pollP2P();
  } catch (e) { toast(e.message, true); }
}

async function loadShare() {
  const r = ui.repo;
  const [info, p] = await Promise.all([api(`/api/repos/${encodeURIComponent(ui.repoId)}/p2p`), api("/api/p2p")]);
  ui.p2p = p;
  const last = info.last;
  const current = last && last.canonical_head === r.canonical_head;
  const where = (b) => (b === "ipfs" ? "IPFS" : "local store");
  const pubRows = info.published.slice(0, 10).map((x) => `<tr><td>${ago(x.at)}</td><td><code class="small">${esc(x.share)}</code></td><td><code>${esc(shortOid(x.canonical_head, 8))}</code></td><td class="num">${x.accepted_transitions}</td><td>${where(x.backend)}</td></tr>`).join("");
  const impRows = info.imported.slice(0, 10).map((x) => `<tr><td>${ago(x.at)}</td><td>${esc(x.from || "?")}${x.device ? " · " + esc(x.device) : ""}</td><td><code>${esc(shortOid(x.canonical_head, 8))}</code></td><td>${esc(x.status)}${x.applied ? ` (+${x.applied})` : ""}</td></tr>`).join("");
  $("#share-view").innerHTML = `<div class="share-grid">
    <div class="card"><h3>Share this repository</h3>
      ${last ? `<div class="muted small">${current ? "Latest share code (current canonical HEAD):" : `Last published ${ago(last.at)} for an older HEAD; publish again to share the current one.`}</div>
        <div class="share-code">${shareCodeHtml(last.share)}</div>
        <div class="muted small">HEAD <code>${esc(shortOid(last.canonical_head))}</code> · ${last.accepted_transitions} accepted PR(s) · ${where(last.backend)}</div>`
      : `<p class="muted small">Not published yet.</p>`}
      <div class="panel-actions" style="margin-top:12px"><span class="muted small">Approved PRs are published automatically.</span>
        <button type="button" class="btn btn-primary btn-small" id="publish-btn" onclick="publishRepo()" ${p.enabled ? "" : "disabled"}>Publish now</button></div>
      ${p.enabled && p.backend !== "ipfs" ? `<div class="warn-box small">IPFS is not running: share codes go to the local store and only work on this computer. Start IPFS to share with another device.</div>` : ""}
    </div>
    <div class="card"><h3>On the other device</h3>
      <ol class="steps-list">
        <li>Both devices run DOSR (<code>demo.py up</code>) with IPFS on.</li>
        <li>Send the share code: it reaches your peers' inboxes automatically, or paste it in chat.</li>
        <li>There: <strong>Import</strong> (sidebar) → paste → <em>Import / pull</em>. The first import clones; later ones pull newly accepted PRs.</li>
        <li>They edit and <em>Submit PR</em> to their own attestor; their new share code comes back here. <strong>Pull</strong> it from the inbox (P2P button, top right).</li>
      </ol>
      <p class="muted small">Optionally send the commit hash too (<code>${esc(shortOid(r.canonical_head, 10))}</code>) and enter it as the <em>expected commit</em>: the import then fails if the share code points anywhere else.</p>
    </div>
    <div class="card wide"><h3>Published</h3>${pubRows ? `<table class="mini"><thead><tr><th>When</th><th>Share code</th><th>HEAD</th><th class="num">PRs</th><th>Where</th></tr></thead><tbody>${pubRows}</tbody></table>` : `<p class="muted small">—</p>`}</div>
    <div class="card wide"><h3>Imported from other devices</h3>${impRows ? `<table class="mini"><thead><tr><th>When</th><th>From</th><th>HEAD</th><th>Result</th></tr></thead><tbody>${impRows}</tbody></table>` : `<p class="muted small">—</p>`}</div>
  </div>`;
}

async function publishRepo() {
  const btn = $("#publish-btn");
  btn.disabled = true;
  btn.textContent = "Publishing…";
  try {
    const r = await api(`/api/repos/${encodeURIComponent(ui.repoId)}/publish`, { method: "POST" });
    toast(`Published ${shortOid(r.canonical_head)} to ${r.backend === "ipfs" ? "IPFS" : "the local store"}${announcedText(r.announced)}`);
    await loadShare();
  } catch (e) { toast(e.message, true); btn.disabled = false; btn.textContent = "Publish now"; }
}

// ------------------------------------------------------------ chain

const txHtml = (h) => (h ? `<code class="small" title="${esc(h)}">${esc(h.slice(0, 10))}…</code> <button type="button" class="btn btn-small btn-ghost" data-copy="${esc(h)}">Copy</button>` : "—");

// Profile name for a dev-chain address (who submitted / paid gas).
function who(addr) {
  if (!addr) return "—";
  const p = (ui.state?.profiles || []).find((x) => x.chain_address && x.chain_address.toLowerCase() === addr.toLowerCase());
  return p ? `${esc(p.name)} <code class="small" title="${esc(addr)}">${esc(addr.slice(0, 8))}…</code>` : `<code class="small" title="${esc(addr)}">${esc(addr.slice(0, 10))}…</code>`;
}

function chainEventHtml(ev) {
  return `canonical HEAD → <code>${esc(shortOid(ev.candidate_git_oid))}</code> (block ${ev.block}`
    + (ev.tx_hash ? ` · tx <code class="small" title="${esc(ev.tx_hash)}">${esc(ev.tx_hash.slice(0, 10))}…</code> · ${fmtNum(ev.gas_used)} gas` : "") + ")";
}

async function pollChain() {
  const pill = $("#chain-pill");
  try {
    const c = (ui.chain = await api("/api/chain"));
    const i = c.info || {};
    if (c.kind === "mock") {
      pill.className = "pill pill-btn pill-local";
      $("#chain-text").textContent = "mock chain";
      pill.title = `Mock chain (a JSON file on this device, not shared): ${c.state_path}\nStart the demo with the EVM chain to share one canonical state.`;
    } else if (!i.up || !i.deployed) {
      pill.className = "pill pill-btn pill-down";
      $("#chain-text").textContent = i.up ? "no contract" : "chain down";
      pill.title = i.error || `no DOSR contract at ${i.contract}`;
    } else {
      pill.className = "pill pill-btn pill-up";
      $("#chain-text").textContent = `EVM · block ${i.block}`;
      pill.title = `${i.rpc_url} · chain ${i.chain_id}\nDOSRRegistry ${i.contract}\nClick to explore repositories and transactions`;
    }
    if (!$("#chain-modal").hidden) renderChain(false);
    // Another device landed a PR: refresh this repo's view.
    const on = ui.repo && c.repos[ui.repo.repo_id];
    if (on && on.canonical_head !== ui.repo.canonical_head) {
      await refreshRepo();
      if (ui.tab === "chain") loadRepoChain();
      toast(`${ui.repo.name}: the canonical HEAD moved on the chain (block ${on.head_block ?? "?"})`);
    }
    $("#chain-tab-badge").textContent = ui.repo && ui.repo.registered && ui.repo.diverged ? "!" : "";
  } catch {
    pill.className = "pill pill-btn pill-down";
    $("#chain-text").textContent = "chain ?";
  }
}

async function openChain() {
  $("#chain-modal").hidden = false;
  await renderChain(true);
}

function closeChain() { $("#chain-modal").hidden = true; }

let chainRendering = false;
async function renderChain(force) {
  if (chainRendering || (!force && !$("#chain-live").checked)) return;
  chainRendering = true;
  try {
    const c = ui.chain || (ui.chain = await api("/api/chain"));
    const i = c.info || {};
    const rows = [];
    if (c.kind === "evm") {
      const me = activeProfile();
      rows.push(["Network", `Local EVM dev chain <span class="muted small">${esc(i.client || "")}</span>`]);
      rows.push(["RPC", `<code>${esc(i.rpc_url)}</code>`]);
      rows.push(["Chain ID", esc(i.chain_id ?? "?")]);
      rows.push(["Contract", `<code>${esc(i.contract)}</code> ` + (i.deployed
        ? (i.code_matches_source ? `<span class="badge ok">code = contracts/src/DOSRRegistry.sol</span>` : `<span class="badge warn">code differs from this build</span>`)
        : `<span class="badge bad">no code at this address</span>`)]);
      rows.push(["Latest block", esc(i.block ?? "?")]);
      rows.push(["You", `${esc(me?.name || "?")} sends from <code class="small">${esc(i.account)}</code> · ${i.balance_wei != null ? (i.balance_wei / 1e18).toFixed(4) : "?"} ETH <span class="muted small">(prefunded dev account: no real money)</span>`]);
      if (i.error) rows.push(["Error", `<span class="bad-text">${esc(i.error)}</span>`]);
    } else {
      rows.push(["Network", `Mock chain <span class="muted small">a JSON file on this device that enforces the contract's checks; not shared with other devices</span>`]);
      rows.push(["State file", `<code class="small">${esc(c.state_path)}</code>`]);
      rows.push(["Block", esc(i.block ?? 0)]);
    }
    $("#chain-net").innerHTML = `<dl class="kv">${rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("")}</dl>`;

    const repos = Object.values(c.repos || {});
    $("#chain-repos").innerHTML = repos.length ? `<table class="mini chain-table"><thead><tr><th>Name</th><th>Repo ID</th><th>Canonical HEAD</th><th class="num">PRs</th><th>Registered</th><th>Snapshot (Git objects)</th><th></th></tr></thead><tbody>
      ${repos.map((r) => `<tr><td><strong>${esc(r.name)}</strong></td>
        <td><code class="small" title="${esc(r.repo_id)}">${esc(r.repo_id.slice(0, 12))}…</code></td>
        <td><code>${esc(shortOid(r.canonical_head, 10))}</code></td>
        <td class="num">${r.accepted_transition_count}</td>
        <td class="small">block ${esc(r.registered_block)}${r.creator ? " · " + who(r.creator) : ""}</td>
        <td class="small">${r.snapshot ? `<code title="${esc(r.snapshot.cid)}">${esc(r.snapshot.cid.slice(0, 14))}…</code> ${r.snapshot.head === r.canonical_head ? '<span class="badge ok">current</span>' : '<span class="badge warn">older HEAD</span>'}` : '<span class="muted">none published</span>'}</td>
        <td>${r.local_repo ? `<button type="button" class="btn btn-small" data-open="${esc(r.local_repo)}">Open</button>`
          : r.snapshot ? `<button type="button" class="btn btn-small btn-primary" data-clone="${esc(r.repo_id)}">Clone</button>` : ""}</td></tr>`).join("")}
      </tbody></table>` : `<p class="muted small">No repositories registered yet. <em>+ New</em> creates one and registers its genesis here.</p>`;
    $("#chain-repos").querySelectorAll("[data-open]").forEach((b) => (b.onclick = () => { closeChain(); selectRepo(b.dataset.open); }));
    $("#chain-repos").querySelectorAll("[data-clone]").forEach((b) => (b.onclick = () => cloneFromChain(b.dataset.clone, b)));

    if (c.kind !== "evm" || !i.deployed) {
      $("#chain-activity").innerHTML = `<p class="muted small">${c.kind === "evm" ? "The contract is not deployed." : "The mock chain keeps no transactions; run the demo on the EVM chain to see them."}</p>`;
      return;
    }
    const a = await api("/api/chain/activity?limit=40");
    const names = Object.fromEntries(repos.map((r) => [r.repo_id, r.name]));
    $("#chain-activity").innerHTML = a.transactions.length ? `<table class="mini chain-table"><thead><tr><th class="num">Block</th><th>When</th><th>From</th><th>Call</th><th>Result</th><th class="num">Gas</th><th>Tx</th></tr></thead><tbody>
      ${a.transactions.map((t) => {
        const repo = t.args?.repo_id ? names[t.args.repo_id] : null;
        const evs = t.events.map((e) => e.name === "TransitionAccepted" ? `HEAD #${e.seq}` : e.name.replace(/([a-z])([A-Z])/g, "$1 $2").toLowerCase()).join(", ");
        const result = t.status === "success" ? `<span class="ok-text">✓ ${esc(evs || "ok")}</span>`
          : `<span class="badge bad" title="${esc(t.error?.message || "reverted")}">reverted${t.error ? ": " + esc(t.error.name) : ""}</span>${t.error?.name === "StaleParent" ? '<div class="muted small">lost the race: its parent was no longer HEAD</div>' : ""}`;
        return `<tr class="${t.status === "success" ? "" : "failed"}"><td class="num">${t.block}</td><td class="small">${ago(t.timestamp)}</td>
          <td class="small">${esc(t.from_label || "")} <code class="small" title="${esc(t.from)}">${esc(t.from.slice(0, 8))}…</code></td>
          <td><code>${esc(t.function || "?")}</code>${repo ? ` <span class="muted small">${esc(repo)}</span>` : ""}<div class="small">${esc(t.summary)}</div></td>
          <td>${result}</td><td class="num">${fmtNum(t.gas_used)}</td>
          <td><code class="small" title="${esc(t.hash)}">${esc(t.hash.slice(0, 10))}…</code></td></tr>`;
      }).join("")}</tbody></table>` : `<p class="muted small">No transactions yet.</p>`;
  } catch (e) {
    $("#chain-activity").innerHTML = `<div class="error-box">${esc(e.message)}</div>`;
  } finally {
    chainRendering = false;
  }
}

async function cloneFromChain(repoId, btn) {
  btn.disabled = true;
  btn.textContent = "Cloning…";
  try {
    const r = await api("/api/chain/clone", { method: "POST", body: { repo_id: repoId } });
    toast(`Cloned ${r.name} at ${shortOid(r.canonical_head)} from its on-chain snapshot`);
    closeChain();
    await loadState();
    await selectRepo(r.id);
  } catch (e) { toast(e.message, true); btn.disabled = false; btn.textContent = "Clone"; }
}

const REL_BANNER = {
  "in-sync": ["ok", "Your copy is at the canonical HEAD recorded on the chain."],
  ahead: ["ok", "Your local commits build on the canonical HEAD. Submit them; once approved they land on the chain."],
  behind: ["warn", "The chain has moved on: newer PRs were accepted. Sync to fast-forward."],
  diverged: ["bad", "Another approved PR landed first. Your local commits no longer build on the canonical HEAD: sync to fetch it and rebase them, then submit again."],
  invalid: ["bad", "This clone does NOT match what the chain records (see the failed checks)."],
  "not-registered": ["bad", "The chain has no record of this repository."],
  error: ["bad", "Could not read the chain."],
};

async function loadRepoChain(note) {
  const el = $("#repo-chain-view");
  if (!el.innerHTML) el.innerHTML = `<p class="muted small">Reading the chain…</p>`;
  let v;
  try {
    v = await api(`/api/repos/${encodeURIComponent(ui.repoId)}/chain`);
  } catch (e) { el.innerHTML = `<div class="error-box">${esc(e.message)}</div>`; return; }
  const rel = v.relation || {};
  const [tone, text] = REL_BANNER[v.status] || ["warn", v.status];
  const canSync = ["behind", "diverged"].includes(v.status) || (v.checks || []).some((c) => c.fix === "sync");
  const mark = (ok) => (ok === true ? `<span class="ok-text">✓</span>` : ok === false ? `<span class="bad-text">✗</span>` : `<span class="muted">?</span>`);
  const checks = (v.checks || []).map((c) => `<tr class="${c.ok === false ? "failed" : ""}"><td><code>${esc(c.name)}</code></td><td class="c">${mark(c.ok)}</td><td class="small">${esc(c.detail)}</td></tr>`).join("");
  const hist = (v.history || []).slice().reverse().map((h) => `<tr>
      <td class="num">${h.seq}</td>
      <td><code title="${esc(h.oid)}">${esc(shortOid(h.oid, 10))}</code>${h.oid === v.canonical_head ? ' <span class="badge ok">HEAD</span>' : ""}${rel.local_head && h.oid.endsWith(rel.local_head) ? ' <span class="badge">your main</span>' : ""}</td>
      <td class="small">${h.kind === "genesis" ? "genesis (registered)" : esc(h.model || "")}${h.attestor_key_id ? ` · <code class="small">${esc(h.attestor_key_id)}</code>` : ""}</td>
      <td class="c">${h.kind === "genesis" ? "" : mark(h.signature_ok)}</td>
      <td class="num">${h.block ?? ""}</td>
      <td class="small">${h.kind === "genesis" ? "" : who(h.submitter)}</td>
      <td class="small">${h.tx_hash ? `<code title="${esc(h.tx_hash)}">${esc(h.tx_hash.slice(0, 10))}…</code>` : ""}</td>
      <td class="c">${h.local ? mark(true) : '<span class="bad-text" title="not in your local repository">missing</span>'}</td></tr>`).join("");
  const pending = (v.pending || []).map((p) => `<div class="inbox-item ${p.conflict ? "" : "new"}">
      <div class="what"><strong>${esc(p.title || p.request_id)}</strong> <code>${esc(shortOid(p.parent, 8))}</code> → <code>${esc(shortOid(p.candidate, 8))}</code>
        <div class="muted small">${p.conflict ? `<span class="bad-text">lost the race:</span> ${esc(p.conflict.error)}` : p.parent === v.canonical_head ? "approved by the attestor, not yet on the chain; its parent is still HEAD, so it can land" : `approved, but built on <code>${esc(shortOid(p.parent, 8))}</code>, which is no longer HEAD: landing it will revert (StaleParent)`}</div></div>
      <div class="acts">${p.conflict ? "" : `<button type="button" class="btn btn-small btn-primary" data-land="${esc(p.request_id)}">Land on chain</button>`}</div></div>`).join("");
  const snaps = (v.snapshots || []).slice(0, 8).map((s) => `<tr><td class="num">${s.block}</td><td><code>${esc(shortOid(s.head, 10))}</code></td><td><code class="small" title="${esc(s.cid)}">${esc(s.cid.slice(0, 24))}…</code></td><td class="small">${who(s.publisher)}</td></tr>`).join("");
  el.innerHTML = `
    ${note || ""}
    <div class="decision chain-banner ${tone}">
      <div><h2>${esc(v.status)}</h2><p>${text}</p>
        <div class="small">Canonical HEAD <code>${esc(shortOid(v.canonical_head))}</code> · your main <code>${esc(shortOid(rel.local_head ? "x:" + rel.local_head : null))}</code>${rel.ahead ? ` · ${rel.ahead} ahead` : ""}${rel.behind ? ` · ${rel.behind} behind` : ""} · verified in ${fmtMs(v.took_ms)}</div></div>
      <div class="acts">
        ${canSync ? `<button type="button" class="btn btn-primary" id="sync-btn">${v.status === "diverged" ? "Sync &amp; rebase" : "Sync"}</button>` : ""}
        ${v.status === "not-registered" ? `<button type="button" class="btn btn-primary" onclick="registerRepo()">Register genesis</button>` : ""}
        <button type="button" class="btn btn-small" onclick="loadRepoChain()">Re-verify</button>
      </div>
    </div>
    <div class="result-cards">
      <div class="card"><h3>Is my copy the canonical one?</h3>
        <p class="muted small">Checked against the chain alone: the genesis and policy hash it registered, the attestor keys it trusts, every accepted transition (signature re-verified from the transaction's calldata), and where your branch stands.</p>
        <table class="mini checks-table"><tbody>${checks}</tbody></table></div>
      <div class="card"><h3>Approved, waiting to land</h3>${pending || '<p class="muted small">None. (Untick <em>Register approval on chain</em> when submitting to keep an approval here; two approvals on the same parent then race: the first to land wins.)</p>'}</div>
      <div class="card wide"><h3>Canonical history on the chain <span class="muted small">newest first</span></h3>
        <table class="mini chain-table"><thead><tr><th class="num">#</th><th>Commit</th><th>Reviewed by</th><th class="c">Sig</th><th class="num">Block</th><th>Submitted by</th><th>Tx</th><th class="c">Local</th></tr></thead><tbody>${hist}</tbody></table></div>
      ${v.snapshots ? `<div class="card wide"><h3>Snapshots <span class="muted small">where to fetch the Git objects for a canonical HEAD (share codes recorded on-chain)</span></h3>${snaps ? `<table class="mini"><thead><tr><th class="num">Block</th><th>HEAD</th><th>Share code</th><th>Published by</th></tr></thead><tbody>${snaps}</tbody></table>` : '<p class="muted small">None yet.</p>'}</div>` : ""}
    </div>`;
  if ($("#sync-btn")) $("#sync-btn").onclick = () => syncRepo($("#sync-btn"));
  el.querySelectorAll("[data-land]").forEach((b) => (b.onclick = () => landSubmission(b.dataset.land, b)));
}

async function syncRepo(btn) {
  if (btn) { btn.disabled = true; btn.textContent = "Syncing…"; }
  try {
    const r = await api(`/api/repos/${encodeURIComponent(ui.repoId)}/sync`, { method: "POST", body: { rebase: true } });
    const note = `<div class="decision approved"><h2>Synced with the chain</h2><ul class="steps-list">${r.steps.map((s) => `<li>${esc(s)}</li>`).join("")}</ul>
      ${r.rebased ? `<p class="small">Rebased <code>${esc(r.rebased.old_head.slice(0, 10))}</code> → <code>${esc(r.rebased.new_head.slice(0, 10))}</code>. Submit again: the attestor must review the rebased commit, because the old approval named a parent that is no longer HEAD.</p>` : ""}</div>
      ${r.warnings.length ? `<div class="warn-box"><ul>${r.warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul></div>` : ""}`;
    toast(r.steps[r.steps.length - 1] || "Synced");
    await Promise.all([refreshRepo(), loadState()]);
    if (ui.tab !== "chain") switchTab("chain");
    await loadRepoChain(note);
  } catch (e) {
    toast(e.message, true);
    if (btn) { btn.disabled = false; btn.textContent = "Sync"; }
    $("#repo-chain-view").insertAdjacentHTML("afterbegin", `<div class="error-box"><strong>Sync failed.</strong> ${esc(e.message)}</div>`);
  }
}

async function landSubmission(id, btn) {
  btn.disabled = true;
  btn.textContent = "Sending…";
  try {
    const r = await api(`/api/repos/${encodeURIComponent(ui.repoId)}/submissions/${encodeURIComponent(id)}/land`, { method: "POST" });
    toast(`Landed: canonical HEAD → ${shortOid(r.event.candidate_git_oid)} (block ${r.event.block})`);
  } catch (e) {
    toast(e.detail?.conflict ? `Lost the race: ${e.message}` : e.message, true);
  }
  await Promise.all([refreshRepo(), loadState()]);
  loadRepoChain();
}

// Copy buttons anywhere (share codes, addresses); "Sync & rebase" from a conflict result.
document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-copy]");
  if (b) copyText(b.dataset.copy);
  const s = e.target.closest("[data-sync]");
  if (s) { switchTab("chain"); syncRepo(s); }
});

// ------------------------------------------------------------ wiring

function wire() {
  document.querySelectorAll("#tabs button").forEach((b) => (b.onclick = () => switchTab(b.dataset.tab)));
  $("#new-repo-btn").onclick = openNewRepo;
  $("#import-btn").onclick = () => openP2P("import");
  $("#p2p-pill").onclick = () => openP2P();
  $("#chain-pill").onclick = () => openChain();
  $("#chain-modal").addEventListener("click", (e) => { if (e.target.id === "chain-modal") closeChain(); });
  $("#p2p-modal").addEventListener("click", (e) => { if (e.target.id === "p2p-modal") closeP2P(); });
  $("#nr-preset").onchange = () => { updatePresetPreview(); setModelRows(presetModelSpecs()); };
  $("#nr-seed").onchange = onSeedChange;
  $("#mp-rows").addEventListener("change", onModelRowsChange);
  $("#mp-rows").addEventListener("click", (e) => {
    if (e.target.closest(".mp-remove")) e.target.closest(".model-row").remove();
  });
  $("#model-select").onchange = updateModelHint;
  $("#model-custom").addEventListener("input", updateModelHint);
  $("#keys-btn").onclick = openKeys;
  $("#nr-template").onchange = updateTemplateHint;
  $("#tpl-modal").addEventListener("click", (e) => { if (e.target.id === "tpl-modal") closeTemplateEditor(); });
  $("#tpl-text").addEventListener("keydown", (e) => {
    if (e.key === "Tab") {
      e.preventDefault();
      const t = e.target, s = t.selectionStart;
      t.value = t.value.slice(0, s) + "    " + t.value.slice(t.selectionEnd);
      t.selectionStart = t.selectionEnd = s + 4;
    }
  });
  $("#keys-list").addEventListener("click", onKeyAction);
  $("#keys-list").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && e.target.classList.contains("key-input")) {
      e.preventDefault();
      e.target.closest(".key-row").querySelector('button[data-act="save"]').click();
    }
  });
  $("#keys-modal").addEventListener("click", (e) => { if (e.target.id === "keys-modal") closeModal(); });
  $("#modal").addEventListener("click", (e) => { if (e.target.id === "modal") closeModal(); });
  $("#profile").onchange = async (e) => {
    try {
      await api("/api/profile", { method: "POST", body: { id: e.target.value } });
      ui.state.active_profile = e.target.value;
      renderAvatar();
      toast(`Now acting as ${activeProfile().name}`);
      pollAttestor();
      pollChain();
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
  $("#opt-force").onchange = () => {
    $("#submit-btn").classList.toggle("btn-danger", $("#opt-force").checked);
    if (ui.repo) renderModelPicker(ui.repo);
  };
  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "s" && ui.tab === "editor") { e.preventDefault(); saveFile(); }
    if (e.key === "Escape") {
      if (!$("#tpl-modal").hidden) closeTemplateEditor();
      else if (!$("#chain-modal").hidden) closeChain();
      else if (!$("#p2p-modal").hidden) closeP2P();
      else closeModal();
    }
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
  pollP2P();
  pollChain();
  setInterval(pollAttestor, 5000);
  setInterval(pollP2P, 5000);
  setInterval(pollChain, 2000);
})();
