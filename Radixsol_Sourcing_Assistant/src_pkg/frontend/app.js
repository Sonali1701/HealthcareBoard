"use strict";

const $ = (selector, element = document) => element.querySelector(selector);
const IS_EXTENSION = ["chrome-extension:", "moz-extension:"].includes(location.protocol);
const DEFAULT_BACKEND = "http://127.0.0.1:8090";
const STAGES = ["new", "enriched", "contacted", "replied", "submitted", "rejected"];

let apiBase = IS_EXTENSION ? DEFAULT_BACKEND : "";
let backendHealth = null;
let jobs = [];
let activeJobId = null;
let activeView = IS_EXTENSION ? "indeed" : "candidates";
let activeDraft = null;
let activeIndeedProfile = null;
let indeedCandidates = [];
let indeedSelected = new Set();
let indeedSaveStatus = null;
let indeedScanState = { phase: "idle", found: 0, total: 0 };
let indeedLookupState = new Map();
let indeedLookupSummary = null;
let indeedResultFilter = "all";
let indeedAutoScanTimer = null;
let indeedBetaNoticeVisible = true;
let toastTimer = null;
const resumeDownloadWaiters = new Map();

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (character) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    "\"": "&quot;",
    "'": "&#039;",
  })[character]);
}

function initials(name) {
  return String(name || "?")
    .split(/\s+/)
    .slice(0, 2)
    .map((part) => part[0] || "")
    .join("")
    .toUpperCase();
}

function notify(message, type = "") {
  const toast = $("#toast");
  clearTimeout(toastTimer);
  toast.textContent = message;
  toast.className = `toast${type ? ` ${type}` : ""}`;
  toastTimer = setTimeout(() => toast.classList.add("hidden"), 4200);
}

function setBusy(button, busy) {
  if (!button) return;
  if (busy) {
    button.dataset.originalText = button.textContent;
    button.textContent = "Working…";
    button.disabled = true;
  } else {
    button.textContent = button.dataset.originalText || button.textContent;
    button.disabled = false;
    delete button.dataset.originalText;
  }
}

async function withBusy(button, work) {
  setBusy(button, true);
  try {
    await work();
  } catch (error) {
    notify(error.message || "Something went wrong.", "error");
  } finally {
    setBusy(button, false);
  }
}

async function api(path, options = {}) {
  const { timeout = 30000, ...fetchOptions } = options;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);

  try {
    const response = await fetch(`${apiBase}${path}`, {
      ...fetchOptions,
      signal: controller.signal,
    });
    const type = response.headers.get("content-type") || "";
    const payload = type.includes("application/json")
      ? await response.json()
      : await response.text();

    if (!response.ok) {
      const detail = payload && typeof payload === "object" ? payload.detail : payload;
      throw new Error(detail || `Backend returned ${response.status}.`);
    }
    return payload;
  } catch (error) {
    if (error.name === "AbortError") {
      throw new Error("The backend request timed out.");
    }
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

function wait(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function setConnection(health = null) {
  const connection = $("#connection");
  const mode = $("#mode");

  mode.className = "pill";
  if (!health) {
    connection.textContent = `Backend unavailable at ${apiBase || location.origin}`;
    connection.className = "connection offline";
    mode.textContent = "offline";
    return;
  }

  connection.textContent = `Connected to ${apiBase || location.origin}`;
  connection.className = "connection online";
  mode.textContent = health.mode || "online";
  mode.classList.add(health.mode === "demo" ? "demo" : "live");
}

async function refreshHealth(showSuccess = false) {
  try {
    const health = await api("/health", { timeout: 4000 });
    backendHealth = health;
    setConnection(health);
    if (showSuccess) notify(`Backend connected in ${health.mode} mode.`);
    return health;
  } catch (error) {
    backendHealth = null;
    setConnection();
    if (showSuccess) notify(error.message || "Backend is unavailable.", "error");
    return null;
  }
}

function normalizeBackendUrl(raw) {
  let parsed;
  try {
    parsed = new URL(raw);
  } catch {
    throw new Error("Enter a valid backend URL.");
  }
  if (
    parsed.protocol !== "http:" ||
    !["127.0.0.1", "localhost"].includes(parsed.hostname) ||
    parsed.username ||
    parsed.password
  ) {
    throw new Error("The extension backend must use HTTP on localhost or 127.0.0.1.");
  }
  return parsed.origin;
}

function readExtensionSetting(key) {
  return new Promise((resolve) => {
    chrome.storage.local.get([key], (result) => resolve(result[key]));
  });
}

function writeExtensionSetting(key, value) {
  return new Promise((resolve) => {
    chrome.storage.local.set({ [key]: value }, resolve);
  });
}

async function loadBackendConfig() {
  if (!IS_EXTENSION) return;
  const saved = await readExtensionSetting("backendUrl");
  if (saved) {
    try {
      apiBase = normalizeBackendUrl(saved);
    } catch {
      apiBase = DEFAULT_BACKEND;
    }
  }
}

async function loadJobs() {
  jobs = await api("/jobs");
  if (activeJobId && !jobs.some((job) => Number(job.id) === Number(activeJobId))) {
    activeJobId = null;
  }
  if (!activeJobId && jobs.length) activeJobId = Number(jobs[0].id);
}

function backendError(error) {
  return `
    <div class="notice error">
      <strong>Backend connection failed.</strong><br>
      ${escapeHtml(error.message || "Start the local service, then try again.")}
    </div>
    <div class="card">
      <h3>Connect the extension</h3>
      <p class="muted small">
        Start <code>python -m uvicorn api:app --host 127.0.0.1 --port 8090</code>
        from the <code>src_pkg</code> folder.
      </p>
      <div class="row mt">
        <button type="button" class="btn teal" data-action="retry">Retry</button>
        <button type="button" class="btn ghost" data-action="navigate" data-view="settings">Settings</button>
      </div>
    </div>`;
}

function kpi(label, value) {
  return `<div class="kpi"><div class="label">${escapeHtml(label)}</div><div class="value">${Number(value) || 0}</div></div>`;
}

function stageClass(stage) {
  return STAGES.includes(stage) ? `stage stage-${stage}` : "stage stage-new";
}

function candidateCard(candidate) {
  const email = Array.isArray(candidate.emails) ? candidate.emails[0] : "";
  const phone = Array.isArray(candidate.phones) ? candidate.phones[0] : "";
  const address = Array.isArray(candidate.addresses) ? candidate.addresses[0] : "";
  const successful = candidate.enrich_status === "success";
  const stage = STAGES.includes(candidate.stage) ? candidate.stage : "new";
  const source = candidate.source
    ? `<span class="source-badge">${escapeHtml(candidate.source)}</span>`
    : "";
  const confidence = Number(candidate.confidence) > 0
    ? `<span class="muted small"> · confidence ${Math.round(Number(candidate.confidence) * 100)}%</span>`
    : "";
  const contact = successful
    ? `<div class="contact">
        <div class="contact-line"><span class="contact-key">✉</span><span>${escapeHtml(email || "No usable email")}</span></div>
        <div class="contact-line"><span class="contact-key">☎</span><span>${escapeHtml(phone || "No usable phone")}</span></div>
        <div class="contact-line"><span class="contact-key">⌂</span><span class="muted">${escapeHtml(address || candidate.location || "")}</span></div>
      </div>`
    : `<div class="contact"><span class="muted">Not enriched yet. Run Enrich to fetch licensed contact data.</span></div>`;

  return `<article class="candidate-card">
    <div class="candidate-head">
      <div class="candidate-avatar">${escapeHtml(initials(candidate.name))}</div>
      <div>
        <div class="candidate-name">${escapeHtml(candidate.name)}</div>
        <div class="candidate-location">${escapeHtml(candidate.location || "")} ${source}</div>
      </div>
      <div class="fit"><div class="number">${Number(candidate.fit_score) || 0}</div><div class="label">FIT</div></div>
    </div>
    <div><span class="${stageClass(stage)}">${escapeHtml(stage)}</span>${confidence}</div>
    ${contact}
    <div class="candidate-actions">
      <button type="button" class="btn teal sm" data-action="enrich" data-id="${Number(candidate.id)}">Enrich</button>
      <button type="button" class="btn sm" data-action="draft" data-id="${Number(candidate.id)}">Draft outreach</button>
      <button type="button" class="btn ghost sm" data-action="move" data-id="${Number(candidate.id)}">Move ▾</button>
    </div>
  </article>`;
}

function jobOptions(includeAll = false) {
  const all = includeAll
    ? `<option value=""${activeJobId ? "" : " selected"}>All candidates</option>`
    : `<option value="">No job selected</option>`;
  return all + jobs.map((job) => (
    `<option value="${Number(job.id)}"${Number(job.id) === Number(activeJobId) ? " selected" : ""}>${escapeHtml(job.title)}</option>`
  )).join("");
}

async function viewCandidates() {
  $("#title").textContent = "Candidates";
  try {
    const suffix = activeJobId ? `?job_id=${encodeURIComponent(activeJobId)}` : "";
    const [candidates, stats] = await Promise.all([
      api(`/candidates${suffix}`),
      api("/stats"),
    ]);
    $("#content").innerHTML = `
      <div class="notice">Licensed Enformion data · email-first · human approval required · do-not-contact enforced.</div>
      <div class="kpis">
        ${kpi("Candidates", stats.total_candidates)}
        ${kpi("Enriched", stats.enriched)}
        ${kpi("Contacted", stats.by_stage?.contacted)}
        ${kpi("Jobs", stats.jobs)}
        ${kpi("Do-Not-Contact", stats.dnc)}
      </div>
      <div class="card">
        <div class="row spread">
          <div class="row grow">
            <label class="muted small" for="jobSelect">Job</label>
            <select id="jobSelect" class="field-auto">${jobOptions(true)}</select>
          </div>
          <div class="row">
            ${IS_EXTENSION ? `<button type="button" class="btn capture-btn" data-action="capture-indeed">Capture Indeed profile</button>` : ""}
            <button type="button" class="btn teal" data-action="enrich-all">Enrich all</button>
            <button type="button" class="btn ghost" data-action="rank-all"${activeJobId ? "" : " disabled"}>Rank vs job</button>
            <button type="button" class="btn" data-action="navigate" data-view="add">Add candidates</button>
          </div>
        </div>
      </div>
      <div class="cards">
        ${candidates.length
          ? candidates.map(candidateCard).join("")
          : `<div class="card"><p class="muted">No candidates found for this selection.</p><button type="button" class="btn" data-action="navigate" data-view="add">Add candidates</button></div>`}
      </div>`;
  } catch (error) {
    setConnection();
    $("#content").innerHTML = backendError(error);
  }
}

function viewAdd() {
  $("#title").textContent = "Add Candidates";
  $("#content").innerHTML = `
    ${IS_EXTENSION ? `<div class="card capture-card">
      <h3>Import from Indeed</h3>
      <p class="muted small">Open one candidate profile in Indeed Smart Sourcing, then capture the visible profile for review and contact enrichment.</p>
      <button type="button" class="btn capture-btn" data-action="capture-indeed">Capture current Indeed profile</button>
    </div>` : ""}
    <div class="card">
      <h3>Choose the destination job</h3>
      <select id="addJobSelect">${jobOptions(false)}</select>
      <p class="muted small">Candidates can be added without a job, but ranking requires one.</p>
    </div>
    <div class="card">
      <h3>Create a job</h3>
      <div class="row">
        <input id="jobTitle" class="grow-2" placeholder="Job title (for example, Radiologic Technologist)">
        <input id="jobLocation" class="grow" placeholder="Location (for example, Atlanta, GA)">
        <button type="button" class="btn" data-action="create-job">Save job</button>
      </div>
      <textarea id="jobDescription" class="mt" rows="3" placeholder="Job description and key skills used for fit ranking"></textarea>
    </div>
    <div class="card">
      <h3>Add candidate names</h3>
      <p class="muted small">Paste names you are entitled to work with—one per line, or CSV with a <code>name</code> column.</p>
      <textarea id="candidatePaste" rows="8" placeholder="Jane Doe, Atlanta, GA&#10;John Smith - Dallas, TX&#10;Maria Lopez | Chicago, IL"></textarea>
      <div class="row mt">
        <button type="button" class="btn teal" data-action="submit-intake">Add candidates</button>
        <span class="muted small" id="intakeMessage"></span>
      </div>
      <p class="muted small mt">This tool does not scrape job platforms. Bring names from your ATS, applicants, referrals, or a manual list.</p>
    </div>`;
}

async function createJob() {
  const title = $("#jobTitle")?.value.trim();
  if (!title) throw new Error("Enter a job title.");
  const result = await api("/jobs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      title,
      location: $("#jobLocation")?.value.trim() || "",
      description: $("#jobDescription")?.value.trim() || "",
    }),
  });
  await loadJobs();
  activeJobId = Number(result.id);
  notify("Job saved.");
  viewAdd();
}

async function submitIntake() {
  const text = $("#candidatePaste")?.value || "";
  if (!text.trim()) throw new Error("Paste at least one candidate name.");
  const result = await api("/candidates/intake", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text, job_id: activeJobId }),
  });
  notify(`Added ${result.added} candidate${result.added === 1 ? "" : "s"}.`);
  await go("candidates");
}

async function enrichCandidate(id) {
  await api(`/candidates/${id}/enrich`, { method: "POST" });
  notify("Candidate enriched.");
  await viewCandidates();
}

async function enrichAll() {
  const suffix = activeJobId ? `?job_id=${encodeURIComponent(activeJobId)}` : "";
  const result = await api(`/enrich/batch${suffix}`, { method: "POST", timeout: 120000 });
  notify(`Enriched ${result.matched} of ${result.processed} processed candidates.`);
  await viewCandidates();
}

async function rankAll() {
  if (!activeJobId) throw new Error("Select a job before ranking.");
  const result = await api(`/jobs/${activeJobId}/rank`, { method: "POST" });
  notify(`Ranked ${result.ranked} candidates against ${result.job}.`);
  await viewCandidates();
}

async function moveCandidate(id) {
  const requested = prompt(`Move to: ${STAGES.join(", ")}`);
  if (requested === null) return;
  const stage = requested.trim().toLowerCase();
  if (!STAGES.includes(stage)) throw new Error("Choose a valid pipeline stage.");
  await api(`/candidates/${id}/stage`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ stage }),
  });
  notify(`Candidate moved to ${stage}.`);
  await viewCandidates();
}

function closeModal() {
  $("#modalRoot").replaceChildren();
  activeDraft = null;
  activeIndeedProfile = null;
}

function sendTabMessage(tabId, message) {
  return new Promise((resolve, reject) => {
    chrome.tabs.sendMessage(tabId, message, (response) => {
      const error = chrome.runtime.lastError;
      if (error) {
        reject(new Error(error.message));
        return;
      }
      resolve(response);
    });
  });
}

function sendExtensionMessage(message) {
  return new Promise((resolve, reject) => {
    chrome.runtime.sendMessage(message, (response) => {
      const error = chrome.runtime.lastError;
      if (error) {
        reject(new Error(error.message));
        return;
      }
      resolve(response);
    });
  });
}

function isIndeedUrl(value) {
  try {
    const hostname = new URL(value).hostname.toLowerCase();
    return hostname === "indeed.com" || hostname.endsWith(".indeed.com");
  } catch {
    return false;
  }
}

async function activeIndeedTab(findExisting = false) {
  if (!IS_EXTENSION) throw new Error("Indeed capture is only available in the browser extension.");
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (tab?.id && isIndeedUrl(tab.url)) return tab;
  if (findExisting) {
    const tabs = await chrome.tabs.query({ currentWindow: true });
    const indeedTab = tabs.find((candidate) => candidate?.id && isIndeedUrl(candidate.url));
    if (indeedTab) {
      await chrome.tabs.update(indeedTab.id, { active: true });
      return indeedTab;
    }
  }
  if (!tab?.id || !isIndeedUrl(tab.url)) {
    throw new Error("Open Indeed Smart Sourcing in the active tab first.");
  }
  return tab;
}

async function sendIndeedMessage(message, findExisting = false) {
  const tab = await activeIndeedTab(findExisting);
  try {
    return await sendTabMessage(tab.id, message);
  } catch {
    await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      files: ["indeed-content.js"],
    });
    return sendTabMessage(tab.id, message);
  }
}

async function captureIndeedProfile() {
  const result = await sendIndeedMessage({ type: "RADIXSOL_CAPTURE_INDEED_PROFILE" });
  if (!result?.ok) {
    throw new Error(result?.error || "The visible Indeed profile could not be read.");
  }
  showIndeedImport(result.profile);
}

function showIndeedImport(profile) {
  activeIndeedProfile = profile;
  $("#modalRoot").innerHTML = `<div class="modal" role="presentation">
    <section class="sheet" role="dialog" aria-modal="true" aria-labelledby="importTitle">
      <h3 id="importTitle">Review Indeed profile</h3>
      <div class="notice">Confirm the extracted identity before contact enrichment. Only this reviewed profile will be imported.</div>
      <label class="field-label" for="importName">Candidate name</label>
      <input id="importName" value="${escapeHtml(profile.name || "")}">
      <label class="field-label" for="importLocation">Location</label>
      <input id="importLocation" value="${escapeHtml(profile.location || "")}">
      <label class="field-label" for="importHeadline">Headline or current role</label>
      <input id="importHeadline" value="${escapeHtml(profile.headline || "")}">
      <label class="field-label" for="importJobSelect">Add to job</label>
      <select id="importJobSelect">${jobOptions(false)}</select>
      <details class="mt">
        <summary>Captured profile text</summary>
        <textarea id="importNotes" class="mt" rows="8">${escapeHtml(profile.notes || "")}</textarea>
      </details>
      <p class="muted small source-url">Source: ${escapeHtml(profile.source_url || "Indeed")}</p>
      <div class="row modal-actions">
        <button type="button" class="btn ghost" data-action="close-modal">Cancel</button>
        <button type="button" class="btn teal" data-action="import-indeed">Import & enrich</button>
      </div>
    </section>
  </div>`;
}

async function importIndeedProfile() {
  if (!activeIndeedProfile) throw new Error("Capture an Indeed profile first.");
  const name = $("#importName")?.value.trim();
  if (!name) throw new Error("Confirm the candidate name before importing.");
  const selectedJob = $("#importJobSelect")?.value;
  const jobId = selectedJob ? Number(selectedJob) : null;

  const imported = await api("/candidates/import", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      ...activeIndeedProfile,
      name,
      location: $("#importLocation")?.value.trim() || "",
      headline: $("#importHeadline")?.value.trim() || "",
      notes: $("#importNotes")?.value.trim() || "",
      job_id: jobId,
    }),
  });
  const enriched = await api(`/candidates/${imported.id}/enrich`, {
    method: "POST",
    timeout: 60000,
  });
  const candidateJob = imported.candidate?.job_id;
  activeJobId = candidateJob ?? jobId;
  closeModal();
  await go("candidates");

  if (enriched.error) {
    notify(`Profile imported, but enrichment reported: ${enriched.error}`, "error");
    return;
  }
  const contacts = (enriched.emails?.length || 0) + (enriched.phones?.length || 0);
  notify(`${imported.imported ? "Imported" : "Opened existing"} Indeed candidate with ${contacts} contact result${contacts === 1 ? "" : "s"}.`);
}

function indeedProfileKey(profile, index) {
  if (profile.source_id) return `id:${profile.source_id}`;
  return `row:${index}:${profile.name || ""}:${profile.location || ""}`;
}

function updateIndeedSelectionUi() {
  const count = indeedSelected.size;
  const counter = $("#indeedSelectedCount");
  if (counter) counter.textContent = String(count);
  const lookupLabel = $("#indeedLookupLabel");
  if (lookupLabel) {
    lookupLabel.textContent = `Look up ${count} candidate${count === 1 ? "" : "s"}`;
  }
  document.querySelectorAll("[data-requires-indeed-selection]").forEach((button) => {
    button.disabled = count === 0;
  });
  const selectAll = $("#indeedSelectAll");
  if (selectAll) {
    selectAll.checked = indeedCandidates.length > 0 && count === indeedCandidates.length;
    selectAll.indeterminate = count > 0 && count < indeedCandidates.length;
  }
}

function indeedAvatarHue(name) {
  let hash = 0;
  for (const character of String(name || "")) {
    hash = ((hash << 5) - hash + character.charCodeAt(0)) | 0;
  }
  return Math.abs(hash) % 360;
}

function indeedLookupFor(profile) {
  return indeedLookupState.get(profile._selectionKey) || { status: "pending" };
}

function isIndeedMatch(result) {
  return result?.status === "success" &&
    ((result.emails?.length || 0) > 0 || (result.phones?.length || 0) > 0);
}

function hasCompleteIndeedContact(result) {
  return isIndeedMatch(result) &&
    (result.emails?.length || 0) > 0 &&
    (result.phones?.length || 0) > 0;
}

function indeedResultStatus(profile) {
  const result = indeedLookupFor(profile);
  const resume = result.resume
    ? `<button type="button" class="resume-link" data-action="open-resume" data-candidate-id="${Number(profile._candidateId)}" data-resume-id="${Number(result.resume.id)}">Show resume · ${escapeHtml(result.resume.filename || "resume.pdf")}</button>`
    : "";
  const resumeStatus = result.resume_status === "downloading"
    ? `<span class="lookup-detail">Downloading and storing resumeâ€¦</span>`
    : result.resume_error
      ? `<span class="lookup-detail" title="${escapeHtml(result.resume_error)}">Resume not stored: ${escapeHtml(result.resume_error)}</span>`
      : "";
  if (result.status === "searching") {
    return `<span class="lookup-searching">${escapeHtml(result.message || "Searching…")}</span>`;
  }
  if (isIndeedMatch(result)) {
    const email = result.emails?.[0] || "";
    const phone = result.phones?.[0] || "";
    const providerName = result.provider === "usphonebook" ? "USPhoneBook match" : "Provider match";
    const confidence = Number(result.confidence) > 0
      ? `<span class="confidence-chip" title="Identity match confidence">${Math.round(Number(result.confidence) * 100)}%</span>`
      : "";
    const source = result.profile_url
      ? `<button type="button" class="provider-source-link" data-action="open-provider-result" data-provider-url="${escapeHtml(result.profile_url)}">Source</button>`
      : "";
    return `<div class="lookup-contact">
      <span class="lookup-state match">${providerName} ${confidence}</span>
      ${email ? `<span class="lookup-value">${escapeHtml(email)}</span>` : ""}
      ${phone ? `<span class="lookup-value">${escapeHtml(phone)}</span>` : ""}
      ${source}
      ${resume}
      ${resumeStatus}
    </div>`;
  }
  if (["no_match", "rejected", "multiple_matches", "location_unverified", "contact_incomplete"].includes(result.status)) {
    const labels = {
      multiple_matches: "Ambiguous USPhoneBook profiles",
      location_unverified: "Current location not verified",
      contact_incomplete: "No complete contact displayed",
      rejected: "Identity evidence rejected",
      no_match: "No exact USPhoneBook match",
    };
    const detail = result.error || result.message || "No confident candidate identity could be established.";
    return `<div class="lookup-outcome">
      <span class="lookup-state no-match">${labels[result.status] || "No confident USPhoneBook match"}</span>
      <span class="lookup-detail" title="${escapeHtml(detail)}">${escapeHtml(detail)}</span>
    </div>`;
  }
  if (["captcha_required", "blocked", "error"].includes(result.status)) {
    const labels = {
      captcha_required: "Manual verification required",
      blocked: "USPhoneBook blocked the lookup",
      error: "Lookup failed",
    };
    const detail = result.error || result.message || "No technical error details were returned.";
    return `<div class="lookup-outcome">
      <span class="lookup-state lookup-error">${labels[result.status] || "Lookup failed"}</span>
      <span class="lookup-detail" title="${escapeHtml(detail)}">${escapeHtml(detail)}</span>
    </div>`;
  }
  return resume;
}

function indeedFilteredProfiles() {
  if (indeedResultFilter === "matched") {
    return indeedCandidates.filter((profile) => isIndeedMatch(indeedLookupFor(profile)));
  }
  if (indeedResultFilter === "no_match") {
    return indeedCandidates.filter((profile) => {
      const status = indeedLookupFor(profile).status;
      return ["no_match", "rejected", "error"].includes(status);
    });
  }
  return indeedCandidates;
}

function indeedPanelHeader() {
  return `
    <div class="panel-brand">
      <div class="workflow-logo" aria-hidden="true">R</div>
      <span class="panel-brand-divider" aria-hidden="true"></span>
      <button type="button" class="panel-rescan-button" data-action="refresh-indeed" title="Scan this Indeed page again" aria-label="Scan this Indeed page again">↗</button>
    </div>
    ${indeedBetaNoticeVisible ? `
      <div class="beta-banner">
        <span>App is currently in beta <small>v2.5.2</small></span>
        <button type="button" data-action="placeholder-action" data-label="Issue reporting">Report an issue</button>
        <button type="button" class="beta-close" data-action="dismiss-beta-notice" title="Dismiss beta notice" aria-label="Dismiss beta notice">×</button>
      </div>` : ""}
    <div class="workflow-accent">Radixsol</div>`;
}

function renderIndeedScanning() {
  const total = Number(indeedScanState.total) || 0;
  const found = Number(indeedScanState.found) || 0;
  const progress = total ? Math.min(100, Math.round((found / total) * 100)) : 8;
  $("#title").textContent = "Indeed Sourcing";
  $("#content").innerHTML = `
    <section class="indeed-workflow scan-view">
      ${indeedPanelHeader()}
      <div class="scan-card">
        <div class="scan-heading">
          <strong>Scanning candidate cards…</strong>
          <span>${found}${total ? ` of ${total}` : ""} found</span>
        </div>
        <div class="progress-track"><span style="width:${progress}%"></span></div>
        <div id="indeedScanPreview" class="scan-preview"></div>
      </div>
      <div class="scan-guidance">
        <strong>Stay on this Indeed tab</strong>
        <span>The page may scroll while the extension reads the result cards. No enrichment credits are used during scanning.</span>
      </div>
    </section>`;
}

function renderIndeedProfiles(scan = {}) {
  const profiles = indeedFilteredProfiles();
  const hasResults = indeedScanState.phase === "results" && Boolean(indeedLookupSummary);
  const isLookingUp = indeedScanState.phase === "lookup";
  const matched = Number(indeedLookupSummary?.matched) || 0;
  const noMatch = Number(indeedLookupSummary?.no_match) || 0;
  $("#content").innerHTML = `
    <section class="indeed-workflow">
      ${indeedPanelHeader()}

      ${hasResults ? `
        <div class="result-summary">
          <button type="button" class="summary-tile matched${indeedResultFilter === "matched" ? " active" : ""}" data-action="filter-indeed-results" data-filter="matched">
            <strong>${matched}</strong><span>matches</span>
          </button>
          <button type="button" class="summary-tile${indeedResultFilter === "no_match" ? " active" : ""}" data-action="filter-indeed-results" data-filter="no_match">
            <strong>${noMatch}</strong><span>no match</span>
          </button>
        </div>` : `
        <div class="capture-toolbar">
          <strong>${indeedCandidates.length} candidates captured</strong>
          <button type="button" class="text-button" data-action="toggle-all-indeed">${indeedSelected.size === indeedCandidates.length ? "Deselect all" : "Select all"}</button>
        </div>`}

      ${isLookingUp ? `
        <div class="lookup-progress">
          <div class="scan-heading"><strong>Looking up candidates…</strong><span id="indeedLookupProgress">${Number(indeedLookupSummary?.processed) || 0} of ${Number(indeedLookupSummary?.total) || indeedSelected.size} done</span></div>
          <div class="progress-track"><span id="indeedLookupBar" style="width:0%"></span></div>
        </div>` : ""}

      <div class="indeed-candidate-list" id="indeedCandidateList">
        ${profiles.length
          ? profiles.map((profile) => {
          const key = profile._selectionKey;
          const originalIndex = indeedCandidates.indexOf(profile);
          return `<article class="capture-row" data-profile-key="${escapeHtml(key)}">
            ${hasResults || isLookingUp ? "" : `<input type="checkbox" class="indeed-select" data-key="${escapeHtml(key)}"${indeedSelected.has(key) ? " checked" : ""} aria-label="Select ${escapeHtml(profile.name)}">`}
            <div class="capture-avatar" style="--avatar-hue:${indeedAvatarHue(profile.name)}">${escapeHtml(initials(profile.name))}</div>
            <button type="button" class="capture-identity" data-action="open-indeed-result" data-index="${originalIndex}">
              <strong>${escapeHtml(profile.name)}</strong>
              <span>${escapeHtml([profile.location, profile.headline].filter(Boolean).join(" · ") || "Profile details captured")}</span>
            </button>
            ${hasResults || isLookingUp ? `<div class="capture-result">${indeedResultStatus(profile)}</div>` : ""}
          </article>`;
        }).join("")
          : `<div class="empty-results"><strong>No candidate cards detected</strong><span>Run an Indeed Smart Sourcing search, wait for the results, and scan again.</span></div>`}
      </div>

      ${hasResults ? `
        <div class="resume-guidance">
          <strong>Automatic resume storage</strong>
          <span>When both phone and email are found, Radixsol opens the exact Indeed profile, starts its PDF download, and stores the resume.</span>
        </div>
        <div class="batch-actions">
          <span>Batch actions (${matched} matched)</span>
          <div>
            <button type="button" data-action="placeholder-action" data-label="ATS">ATS</button>
            <button type="button" data-action="placeholder-action" data-label="Pool">Pool</button>
            <button type="button" data-action="placeholder-action" data-label="Campaign">Campaign</button>
            <button type="button" data-action="placeholder-action" data-label="Email">Email</button>
            <button type="button" data-action="export-indeed">Export</button>
          </div>
        </div>` : `
        <div class="workflow-footer">
          <button type="button" class="lookup-button" data-action="lookup-indeed" data-requires-indeed-selection${indeedSelected.size ? "" : " disabled"}>
            <span class="lookup-icon" aria-hidden="true"></span>
            <span id="indeedLookupLabel">Look up ${indeedSelected.size} candidate${indeedSelected.size === 1 ? "" : "s"}</span>
          </button>
          <span class="credit-note">Searches USPhoneBook sequentially and saves confident matches.</span>
          <button type="button" class="view-results-link" data-action="show-cached-indeed-results">View Results</button>
          <span id="indeedSaveStatus" class="sync-status ${escapeHtml(indeedSaveStatus?.state || "muted")}">${escapeHtml(indeedSaveStatus?.message || "")}</span>
        </div>`}
    </section>`;
  updateIndeedSelectionUi();
}

function showIndeedSaveStatus(state, message) {
  indeedSaveStatus = { state, message };
  const element = $("#indeedSaveStatus");
  if (!element) return;
  element.textContent = message;
  element.className = `sync-status small ${state}`;
}

async function saveDisplayedIndeedCandidates(searchUrl) {
  if (!indeedCandidates.length) {
    showIndeedSaveStatus("muted", "No displayed profiles to save.");
    return null;
  }
  showIndeedSaveStatus("saving", `Saving ${indeedCandidates.length} displayed profiles…`);
  const profiles = indeedCandidates.map((profile) => {
    const { _selectionKey, ...storedProfile } = profile;
    return storedProfile;
  });
  try {
    const result = await api("/candidates/import/batch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        profiles,
        job_id: activeJobId,
        search_url: searchUrl || "",
      }),
      timeout: 120000,
    });
    (result.results || []).forEach((saved, index) => {
      const profile = indeedCandidates[index];
      if (!profile) return;
      profile._candidateId = Number(saved.id);
      const candidate = saved.candidate;
      if (!candidate || candidate.enrich_status === "pending") return;
      indeedLookupState.set(profile._selectionKey, {
        status: candidate.enrich_status,
        emails: candidate.verification?.provider_contacts?.emails || candidate.emails || [],
        phones: candidate.verification?.provider_contacts?.phones || candidate.phones || [],
        confidence: Number(candidate.confidence) || 0,
        verification: candidate.verification || {},
        provider: candidate.verification?.source || "",
        profile_url: candidate.verification?.profile_url || "",
        cached: true,
      });
    });
    const database = result.database === "postgresql" ? "Neon" : "local database";
    showIndeedSaveStatus(
      "saved",
      `${result.saved} saved to ${database} · ${result.imported} new · ${result.existing} already present`,
    );
    return result;
  } catch (error) {
    showIndeedSaveStatus("failed", `Automatic save failed: ${error.message}`);
    return null;
  }
}

async function scanIndeedCandidates(options = {}) {
  const { quiet = false, preserveSelection = false } = options;
  $("#title").textContent = "Indeed Sourcing";
  if (!quiet) {
    indeedScanState = { phase: "scanning", found: 0, total: 0 };
    indeedLookupSummary = null;
    indeedResultFilter = "all";
    renderIndeedScanning();
  }
  try {
    const previousSelection = new Set(indeedSelected);
    const previouslySelectedAll = indeedCandidates.length > 0 &&
      previousSelection.size === indeedCandidates.length;
    const result = await sendIndeedMessage({
      type: quiet
        ? "RADIXSOL_LIST_INDEED_CANDIDATES"
        : "RADIXSOL_SCAN_INDEED_CANDIDATES",
    });
    if (!result?.ok) throw new Error(result?.error || "Indeed results could not be read.");
    indeedCandidates = (result.profiles || []).map((profile, index) => ({
      ...profile,
      _selectionKey: indeedProfileKey(profile, index),
    }));
    if (!preserveSelection) indeedLookupState = new Map();
    if (quiet) {
      indeedLookupSummary = null;
      indeedResultFilter = "all";
    }
    indeedSelected = preserveSelection
      ? (previouslySelectedAll
        ? new Set(indeedCandidates.map((profile) => profile._selectionKey))
        : new Set(
        indeedCandidates
          .map((profile) => profile._selectionKey)
          .filter((key) => previousSelection.has(key)),
        ))
      : new Set(indeedCandidates.map((profile) => profile._selectionKey));
    indeedScanState = {
      phase: "captured",
      found: indeedCandidates.length,
      total: Number(result.expected_count) || indeedCandidates.length,
    };
    indeedSaveStatus = null;
    renderIndeedProfiles(result);
    await saveDisplayedIndeedCandidates(result.page_url);
  } catch (error) {
    if (quiet) {
      showIndeedSaveStatus("failed", `Automatic rescan failed: ${error.message}`);
      return;
    }
    indeedCandidates = [];
    indeedSelected = new Set();
    indeedScanState = { phase: "error", found: 0, total: 0 };
    $("#content").innerHTML = `
      <div class="notice error"><strong>Indeed results could not be read.</strong><br>${escapeHtml(error.message)}</div>
      <div class="card">
        <p class="muted small">Open an Indeed Smart Sourcing search-results tab, wait for its candidate cards, and try again.</p>
        <button type="button" class="btn" data-action="refresh-indeed">Try again</button>
      </div>`;
  }
}

async function viewIndeed() {
  await scanIndeedCandidates();
}

if (IS_EXTENSION) {
  chrome.runtime.onMessage.addListener((message) => {
    if (message?.type === "RADIXSOL_RESUME_DOWNLOADED") {
      handleDownloadedResume(message);
      return false;
    }
    if (activeView !== "indeed") {
      return false;
    }
    if (message?.type === "RADIXSOL_USPHONEBOOK_PROGRESS") {
      const profile = indeedCandidates.find((candidate) => (
        (message.profileKey && candidate._selectionKey === message.profileKey) ||
        (
          candidate.name === message.name &&
          (candidate.location || "") === (message.location || "")
        )
      ));
      if (profile) {
        indeedLookupState.set(profile._selectionKey, {
          ...indeedLookupFor(profile),
          status: "searching",
          provider: "usphonebook",
          message: message.message || "Searching USPhoneBook…",
        });
        updateIndeedLookupProgressUi(profile);
      }
      return false;
    }
    if (message?.type === "RADIXSOL_INDEED_SCAN_PROGRESS") {
      indeedScanState = {
        phase: "scanning",
        found: Number(message.found) || 0,
        total: Number(message.total) || 0,
      };
      if (!$(".scan-view")) renderIndeedScanning();
      const progress = message.total
        ? Math.min(100, Math.round((Number(message.found) / Number(message.total)) * 100))
        : 8;
      const heading = $(".scan-heading span");
      const bar = $(".progress-track span");
      if (heading) heading.textContent = `${Number(message.found) || 0}${message.total ? ` of ${Number(message.total)}` : ""} found`;
      if (bar) bar.style.width = `${progress}%`;
      const preview = $("#indeedScanPreview");
      if (preview && Array.isArray(message.preview)) {
        preview.innerHTML = message.preview.slice(-5).map((profile) => `
          <div><span class="capture-avatar mini-avatar" style="--avatar-hue:${indeedAvatarHue(profile.name)}">${escapeHtml(initials(profile.name))}</span><span>${escapeHtml(profile.name)}</span></div>
        `).join("");
      }
      return false;
    }
    if (message?.type !== "RADIXSOL_INDEED_RESULTS_CHANGED") return false;
    if (["scanning", "lookup"].includes(indeedScanState.phase)) return false;
    if ($("#modalRoot")?.childElementCount) return false;
    clearTimeout(indeedAutoScanTimer);
    indeedAutoScanTimer = setTimeout(() => {
      scanIndeedCandidates({ quiet: true, preserveSelection: true });
    }, 800);
    return false;
  });
}

async function openIndeedResult(index) {
  const profile = indeedCandidates[index];
  if (!profile) throw new Error("That displayed candidate is no longer available.");
  const result = await sendIndeedMessage({
    type: "RADIXSOL_OPEN_INDEED_CANDIDATE",
    index: profile.result_index ?? index,
  });
  if (!result?.ok) throw new Error(result?.error || "Indeed could not open that candidate.");
  if (profile._candidateId) {
    await sendExtensionMessage({
      type: "RADIXSOL_SET_ACTIVE_CANDIDATE",
      candidateId: profile._candidateId,
      name: profile.name,
      sourceId: profile.source_id || "",
    });
  }
  notify(`Opened ${profile.name} in Indeed.`);
}

function selectedIndeedProfiles() {
  return indeedCandidates.filter((profile) => indeedSelected.has(profile._selectionKey));
}

function updateIndeedLookupProgressUi(profile = null) {
  const summary = indeedLookupSummary || {};
  const progress = summary.total
    ? Math.min(100, Math.round((Number(summary.processed) / Number(summary.total)) * 100))
    : 0;
  const label = $("#indeedLookupProgress");
  const bar = $("#indeedLookupBar");
  if (label) label.textContent = `${Number(summary.processed) || 0} of ${Number(summary.total) || 0} done`;
  if (bar) bar.style.width = `${progress}%`;
  if (profile) {
    const row = Array.from(document.querySelectorAll("[data-profile-key]"))
      .find((element) => element.dataset.profileKey === profile._selectionKey);
    const result = row?.querySelector(".capture-result");
    if (result) result.innerHTML = indeedResultStatus(profile);
  }
}

async function lookupSelectedIndeedCandidates() {
  const profiles = selectedIndeedProfiles();
  if (!profiles.length) throw new Error("Select at least one Indeed profile.");
  if (
    profiles.length > 1 &&
    !confirm(
      `Search USPhoneBook for ${profiles.length} selected candidates? ` +
      "Searches run one at a time and a large batch can take several minutes. " +
      "Only confident identity matches supported by location and profile evidence will be saved."
    )
  ) {
    return;
  }

  if (profiles.some((profile) => !profile._candidateId)) {
    await saveDisplayedIndeedCandidates(location.href);
  }

  indeedScanState.phase = "lookup";
  indeedLookupSummary = {
    total: profiles.length,
    processed: 0,
    matched: 0,
    no_match: 0,
    errors: 0,
  };
  for (const profile of profiles) {
    const existing = indeedLookupFor(profile);
    if (!(isIndeedMatch(existing) && existing.provider === "usphonebook")) {
      indeedLookupState.set(profile._selectionKey, {
        status: "searching",
        provider: "usphonebook",
        message: "Opening USPhoneBook search…",
      });
    }
  }
  renderIndeedProfiles();

  for (const profile of profiles) {
    const existing = indeedLookupFor(profile);
    if (isIndeedMatch(existing) && existing.provider === "usphonebook") {
      if (hasCompleteIndeedContact(existing) && !existing.resume) {
        await downloadMatchedIndeedResume(profile);
      }
      indeedLookupSummary.matched += 1;
      indeedLookupSummary.processed += 1;
      updateIndeedLookupProgressUi(profile);
      continue;
    }

    try {
      if (!profile._candidateId) throw new Error("Candidate was not saved to the database.");
      const lookup = await sendExtensionMessage({
        type: "RADIXSOL_USPHONEBOOK_LOOKUP",
        name: profile.name,
        location: profile.location || "",
        jobTitle: profile.headline || "",
        profileKey: profile._selectionKey,
      });
      if (!lookup?.ok) throw new Error(lookup?.error || "USPhoneBook lookup failed.");
      const providerResult = lookup.result || {};
      let result = {
        status: providerResult.status || "error",
        emails: providerResult.emails || [],
        phones: providerResult.phones || [],
        addresses: providerResult.addresses || [],
        confidence: Number(providerResult.confidence) || 0,
        provider: "usphonebook",
        profile_url: providerResult.profile_url || "",
        cached: Boolean(providerResult.cached),
        message: providerResult.message || "",
        error: providerResult.status === "error" ? (providerResult.message || "Lookup failed.") : "",
      };
      if (
        providerResult.status === "success" &&
        ((providerResult.emails?.length || 0) || (providerResult.phones?.length || 0))
      ) {
        const saved = await api(`/candidates/${profile._candidateId}/provider-result`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(providerResult),
          timeout: 30000,
        });
        result = {
          ...result,
          status: saved.enrich_status || "no_match",
          emails: saved.emails || [],
          phones: saved.phones || [],
          addresses: saved.addresses || [],
          confidence: Number(saved.confidence) || result.confidence,
          verification: saved.verification || {},
          error: saved.error || "",
        };
      }
      const status = result.status;
      indeedLookupState.set(profile._selectionKey, result);
      if (hasCompleteIndeedContact(result)) {
        await downloadMatchedIndeedResume(profile);
        result = indeedLookupFor(profile);
      }
      if (isIndeedMatch(result)) indeedLookupSummary.matched += 1;
      else if (status === "error") indeedLookupSummary.errors += 1;
      else indeedLookupSummary.no_match += 1;
    } catch (error) {
      indeedLookupState.set(profile._selectionKey, {
        status: "error",
        provider: "usphonebook",
        error: error.message || "Lookup failed.",
      });
      indeedLookupSummary.errors += 1;
    }
    indeedLookupSummary.processed += 1;
    updateIndeedLookupProgressUi(profile);
  }

  indeedLookupSummary.no_match += indeedLookupSummary.errors;
  indeedScanState.phase = "results";
  indeedResultFilter = "matched";
  renderIndeedProfiles();
  notify(
    `${indeedLookupSummary.matched} USPhoneBook match${indeedLookupSummary.matched === 1 ? "" : "es"} and ${indeedLookupSummary.no_match} without a confident match.`,
  );
}

function toggleAllIndeedCandidates() {
  indeedSelected = indeedSelected.size === indeedCandidates.length
    ? new Set()
    : new Set(indeedCandidates.map((profile) => profile._selectionKey));
  renderIndeedProfiles();
}

function showCachedIndeedResults() {
  const completed = indeedCandidates.filter((profile) => {
    const status = indeedLookupFor(profile)?.status;
    return status && !["pending", "searching"].includes(status);
  });
  if (!completed.length) {
    notify("No lookup results yet. Select candidates and run a lookup first.");
    return;
  }
  const matched = completed.filter((profile) => isIndeedMatch(indeedLookupFor(profile))).length;
  indeedLookupSummary = {
    total: completed.length,
    processed: completed.length,
    matched,
    no_match: completed.length - matched,
    errors: completed.filter((profile) => indeedLookupFor(profile)?.status === "error").length,
  };
  indeedScanState.phase = "results";
  indeedResultFilter = matched ? "matched" : "no_match";
  renderIndeedProfiles();
}

function dismissIndeedBetaNotice() {
  indeedBetaNoticeVisible = false;
  $(".beta-banner")?.remove();
}

async function openProviderResult(url) {
  if (!IS_EXTENSION) throw new Error("Provider pages can only be opened from the extension.");
  let parsed;
  try {
    parsed = new URL(url);
  } catch {
    throw new Error("The provider result URL is invalid.");
  }
  if (
    parsed.protocol !== "https:" ||
    !["usphonebook.com", "www.usphonebook.com"].includes(parsed.hostname.toLowerCase())
  ) {
    throw new Error("Only USPhoneBook result links can be opened.");
  }
  await chrome.tabs.create({ url: parsed.href, active: true });
}

function filterIndeedResults(filter) {
  indeedResultFilter = indeedResultFilter === filter ? "all" : filter;
  renderIndeedProfiles();
}

function csvCell(value) {
  return `"${String(value ?? "").replace(/"/g, "\"\"")}"`;
}

function exportIndeedMatches() {
  const matches = indeedCandidates.filter((profile) => isIndeedMatch(indeedLookupFor(profile)));
  if (!matches.length) throw new Error("There are no matched candidates to export.");
  const rows = [
    ["Name", "Location", "Headline", "Email", "Phone", "Identity confidence", "Source URL"],
    ...matches.map((profile) => {
      const result = indeedLookupFor(profile);
      return [
        profile.name,
        profile.location,
        profile.headline,
        result.emails?.[0] || "",
        result.phones?.[0] || "",
        Number(result.confidence) > 0 ? `${Math.round(Number(result.confidence) * 100)}%` : "",
        profile.source_url || "",
      ];
    }),
  ];
  const csv = rows.map((row) => row.map(csvCell).join(",")).join("\r\n");
  const url = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = `radixsol-indeed-matches-${new Date().toISOString().slice(0, 10)}.csv`;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  notify(`Exported ${matches.length} matched candidates.`);
}

async function attachDownloadedResume(message) {
  const candidateId = Number(message.candidateId);
  if (!candidateId || !message.path) throw new Error("The completed resume download could not be identified.");
  const attached = await api(`/candidates/${candidateId}/resume/from-download`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      path: message.path,
      filename: message.filename || "",
    }),
    timeout: 60000,
  });
  const profile = indeedCandidates.find(
    (candidate) => Number(candidate._candidateId) === candidateId,
  );
  if (profile) {
    const current = indeedLookupFor(profile);
    indeedLookupState.set(profile._selectionKey, {
      ...current,
      resume: attached.resume,
      resume_status: "stored",
      resume_error: "",
    });
    if (indeedScanState.phase === "results") renderIndeedProfiles();
  }
  notify(`Stored ${attached.resume.filename} for ${message.candidateName || "the candidate"}.`);
  return attached.resume;
}

async function handleDownloadedResume(message) {
  const candidateId = Number(message.candidateId);
  const waiter = resumeDownloadWaiters.get(candidateId);
  try {
    const resume = await attachDownloadedResume(message);
    waiter?.resolve(resume);
  } catch (error) {
    waiter?.reject(error);
    notify(`Resume downloaded, but storage failed: ${error.message}`, "error");
  }
}

async function saveStoredResumeDownload(profile, candidateId, resume) {
  const safeName = String(profile.name || "candidate")
    .replace(/[^a-z0-9 _-]/gi, "_")
    .trim()
    .replace(/\s+/g, "_") || "candidate";
  return chrome.downloads.download({
    url: `${apiBase}/candidates/${Number(candidateId)}/resumes/${Number(resume.id)}`,
    filename: `RadixsolResumes/${safeName}_Indeed_resume_enriched.pdf`,
    saveAs: false,
  });
}

async function recoverStoredResume(candidateId, uploadStartedAt, timeoutMs = 120000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const candidate = await api(`/candidates/${Number(candidateId)}`, { timeout: 15000 });
      const resume = (candidate.resumes || []).find(
        (item) => Number(item.created || 0) >= uploadStartedAt - 5,
      );
      if (resume) {
        return {
          ...resume,
          contact_sheet_embedded: /enriched\.pdf$/i.test(resume.filename || ""),
          recovered_after_timeout: true,
        };
      }
    } catch {
      // The original request can still be finishing in the backend. Keep
      // polling until its resume metadata is committed.
    }
    await wait(2500);
  }
  return null;
}

async function downloadMatchedIndeedResume(profile) {
  const candidateId = Number(profile._candidateId);
  const current = indeedLookupFor(profile);
  if (!candidateId || current.resume || !hasCompleteIndeedContact(current)) return;

  indeedLookupState.set(profile._selectionKey, {
    ...current,
    resume_status: "downloading",
    resume_error: "",
  });
  updateIndeedLookupProgressUi(profile);

  try {
    // Automatic capture stores the bytes returned by the proven MAIN-world
    // hook. Clear the older file-path tracker so a normal Indeed download does
    // not create a duplicate resume record in parallel.
    await sendExtensionMessage({ type: "RADIXSOL_CLEAR_ACTIVE_CANDIDATE" });

    const captured = await sendIndeedMessage({
      type: "RADIXSOL_DOWNLOAD_INDEED_RESUME",
      index: profile.result_index,
      expectedName: profile.name,
    }, true);
    if (!captured?.ok) {
      const diagnostics = captured?.diagnostics?.length
        ? ` (${captured.diagnostics.join(", ")})`
        : "";
      throw new Error((captured?.error || "Indeed resume download could not be captured.") + diagnostics);
    }
    if (!captured.base64) throw new Error("Indeed returned no captured resume bytes.");

    const filename = `${profile.name || "candidate"} - Indeed resume.pdf`;
    const uploadStartedAt = Date.now() / 1000;
    let attached;
    try {
      attached = await api(`/candidates/${candidateId}/resume/from-browser`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          content_base64: captured.base64,
          filename,
        }),
        // PDF enrichment plus a transient R2 retry can exceed 90 seconds.
        timeout: 300000,
      });
    } catch (error) {
      if (error.message !== "The backend request timed out.") throw error;
      const recovered = await recoverStoredResume(candidateId, uploadStartedAt);
      if (!recovered) throw error;
      attached = { attached: true, resume: recovered, recovered_after_timeout: true };
    }

    // Always download the server-generated copy. It contains the contact page
    // built from the phone/email values already committed to the database.
    await saveStoredResumeDownload(profile, candidateId, attached.resume);

    const latest = indeedLookupFor(profile);
    indeedLookupState.set(profile._selectionKey, {
      ...latest,
      resume: attached.resume,
      resume_status: "stored",
      resume_error: "",
    });
    updateIndeedLookupProgressUi(profile);
    const contactNote = attached.resume.contact_sheet_embedded
      ? " with phone and email"
      : " (contact page could not be embedded)";
    const recoveredNote = attached.recovered_after_timeout ? " (confirmed after upload delay)" : "";
    notify(`Stored ${attached.resume.filename}${contactNote}${recoveredNote} for ${profile.name}.`);
  } catch (error) {
    const latest = indeedLookupFor(profile);
    indeedLookupState.set(profile._selectionKey, {
      ...latest,
      resume_status: "failed",
      resume_error: error.message || "Resume download failed.",
    });
    updateIndeedLookupProgressUi(profile);
  }
}

async function openStoredResume(candidateId, resumeId) {
  if (!candidateId || !resumeId) throw new Error("Stored resume was not found.");
  const url = `${apiBase}/candidates/${candidateId}/resumes/${resumeId}`;
  if (IS_EXTENSION) {
    await chrome.tabs.create({ url });
  } else {
    window.open(url, "_blank", "noopener");
  }
}

async function bulkImportIndeed(enrichContacts) {
  const profiles = selectedIndeedProfiles();
  if (!profiles.length) throw new Error("Select at least one Indeed profile.");
  if (
    enrichContacts &&
    !confirm(`Import and run contact enrichment for ${profiles.length} selected profile${profiles.length === 1 ? "" : "s"}? This may use licensed provider credits.`)
  ) {
    return;
  }

  const selectedJob = $("#indeedJobSelect")?.value;
  const jobId = selectedJob ? Number(selectedJob) : null;
  const progress = $("#indeedBulkProgress");
  let importedCount = 0;
  let existingCount = 0;
  let enrichedCount = 0;
  let failedCount = 0;

  for (let index = 0; index < profiles.length; index += 1) {
    const profile = profiles[index];
    if (progress) progress.textContent = `Processing ${index + 1} of ${profiles.length}: ${profile.name}`;
    try {
      const imported = await api("/candidates/import", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...profile, job_id: jobId, _selectionKey: undefined }),
      });
      if (imported.imported) importedCount += 1;
      else existingCount += 1;
      if (enrichContacts) {
        const enriched = await api(`/candidates/${imported.id}/enrich`, {
          method: "POST",
          timeout: 60000,
        });
        if (!enriched.error) enrichedCount += 1;
        else failedCount += 1;
      }
    } catch {
      failedCount += 1;
    }
  }

  activeJobId = jobId;
  notify(
    `${importedCount} imported, ${existingCount} already present` +
    `${enrichContacts ? `, ${enrichedCount} enriched` : ""}` +
    `${failedCount ? `, ${failedCount} failed` : ""}.`,
    failedCount ? "error" : "",
  );
  await go("candidates");
}

function showDraft(draft) {
  activeDraft = draft;
  $("#modalRoot").innerHTML = `<div class="modal" role="presentation">
    <section class="sheet" role="dialog" aria-modal="true" aria-labelledby="draftTitle">
      <h3 id="draftTitle">Outreach draft <span class="muted small">· review before approving</span></h3>
      <div class="muted small">To: ${escapeHtml((draft.to || []).join(", "))}</div>
      <input class="mt" aria-label="Subject" readonly value="${escapeHtml(draft.subject || "")}">
      <textarea class="mt" aria-label="Message body" readonly rows="10">${escapeHtml(draft.body || "")}</textarea>
      <div class="notice mt">${escapeHtml(draft.compliance || "")}</div>
      <div class="row modal-actions">
        <button type="button" class="btn ghost" data-action="close-modal">Close</button>
        <button type="button" class="btn ghost" data-action="copy-draft">Copy</button>
        <button type="button" class="btn teal" data-action="approve-draft" data-id="${Number(draft.outreach_id)}">Approve draft</button>
      </div>
      <p class="muted small">Approval records your review; it does not send the message.</p>
    </section>
  </div>`;
}

async function draftOutreach(candidateId) {
  const draft = await api("/outreach/draft", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ candidate_id: candidateId, job_id: activeJobId }),
    timeout: 60000,
  });
  showDraft(draft);
}

async function approveDraft(id) {
  await api(`/outreach/${id}/approve`, { method: "POST" });
  closeModal();
  notify("Draft approved. Send it through your email provider.");
  if (activeView === "candidates") await viewCandidates();
}

async function copyDraft() {
  if (!activeDraft) return;
  const text = `Subject: ${activeDraft.subject || ""}\n\n${activeDraft.body || ""}`;
  await navigator.clipboard.writeText(text);
  notify("Draft copied to the clipboard.");
}

async function viewPipeline() {
  $("#title").textContent = "Pipeline";
  try {
    const suffix = activeJobId ? `?job_id=${encodeURIComponent(activeJobId)}` : "";
    const candidates = await api(`/candidates${suffix}`);
    $("#content").innerHTML = `<div class="card">
      <div class="row spread">
        <h3>Pipeline board</h3>
        <select id="jobSelect" class="field-auto">${jobOptions(true)}</select>
      </div>
      <div class="board-wrap"><div class="board">
        ${STAGES.map((stage) => {
          const inStage = candidates.filter((candidate) => candidate.stage === stage);
          return `<section class="column">
            <h4>${escapeHtml(stage)} (${inStage.length})</h4>
            ${inStage.map((candidate) => `<div class="mini"><strong>${escapeHtml(candidate.name)}</strong><div class="muted">${escapeHtml(candidate.location || "")}</div></div>`).join("")}
          </section>`;
        }).join("")}
      </div></div>
    </div>`;
  } catch (error) {
    setConnection();
    $("#content").innerHTML = backendError(error);
  }
}

async function viewDnc() {
  $("#title").textContent = "Do-Not-Contact";
  try {
    const list = await api("/dnc");
    $("#content").innerHTML = `
      <div class="card">
        <h3>Add a suppressed contact</h3>
        <div class="row">
          <input id="dncValue" class="grow" placeholder="Email or phone to suppress">
          <input id="dncReason" class="grow" placeholder="Reason (optional)">
          <button type="button" class="btn" data-action="add-dnc">Add</button>
        </div>
      </div>
      <div class="card">
        <h3>Suppressed contacts (${list.length})</h3>
        ${list.length
          ? list.map((entry) => `<div class="mini"><strong>${escapeHtml(entry.value)}</strong> <span class="muted">${escapeHtml(entry.reason || "")}</span></div>`).join("")
          : `<p class="muted">None yet. Suppressed emails and phones are removed from enrichment and blocked from outreach.</p>`}
      </div>`;
  } catch (error) {
    setConnection();
    $("#content").innerHTML = backendError(error);
  }
}

async function addDnc() {
  const value = $("#dncValue")?.value.trim();
  if (!value) throw new Error("Enter an email address or phone number.");
  await api("/dnc", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      value,
      reason: $("#dncReason")?.value.trim() || "",
    }),
  });
  notify("Contact added to the do-not-contact list.");
  await viewDnc();
}

function viewSettings() {
  $("#title").textContent = "Settings";
  const backendControls = IS_EXTENSION
    ? `<div class="card">
        <h3>Local backend</h3>
        <p class="muted small">The extension keeps API credentials and candidate data in the Python service, not in the browser.</p>
        <div class="row">
          <input id="backendUrl" class="grow" value="${escapeHtml(apiBase)}" aria-label="Backend URL">
          <button type="button" class="btn" data-action="save-backend">Save & test</button>
          <button type="button" class="btn ghost" data-action="test-backend">Test</button>
        </div>
        <p class="muted small">Allowed hosts: <code>127.0.0.1</code> and <code>localhost</code>.</p>
      </div>`
    : "";

  $("#content").innerHTML = `${backendControls}
    <div class="card">
      <h3>Provider and compliance</h3>
      <table class="settings-table"><tbody>
        <tr><td>Indeed Lookup</td><td>USPhoneBook browser-assisted search</td></tr>
        <tr><td>Other enrichment</td><td>Enformion licensed API when configured</td></tr>
        <tr><td>Name sources</td><td>Manual entry, CSV, or your ATS—no platform scraping</td></tr>
        <tr><td>Indeed integration</td><td>User-triggered capture of the currently open Smart Sourcing profile</td></tr>
        <tr><td>Default outreach</td><td>Email; phone and SMS require TCPA consent</td></tr>
        <tr><td>Sending</td><td>Human approval required; nothing auto-sends</td></tr>
        <tr><td>Database</td><td>Neon/PostgreSQL when <code>DATABASE_URL</code> is configured; SQLite fallback</td></tr>
      </tbody></table>
      <p class="muted small mt">Configure <code>ENFORMION_AP_NAME</code>, <code>ENFORMION_AP_PASSWORD</code>, and optionally <code>GEMINI_API_KEY</code> in the backend environment. With no Enformion key, the service runs in deterministic demo mode.</p>
    </div>`;
}

async function saveBackend() {
  if (!IS_EXTENSION) return;
  const value = normalizeBackendUrl($("#backendUrl")?.value.trim() || "");
  apiBase = value;
  await writeExtensionSetting("backendUrl", value);
  const health = await refreshHealth(true);
  if (health) await loadJobs();
  viewSettings();
}

async function retry() {
  const health = await refreshHealth(true);
  if (health) {
    await loadJobs();
    await go(activeView);
  }
}

const views = {
  candidates: viewCandidates,
  indeed: viewIndeed,
  add: viewAdd,
  pipeline: viewPipeline,
  dnc: viewDnc,
  settings: viewSettings,
};

async function go(view) {
  activeView = views[view] ? view : "candidates";
  document.querySelectorAll(".nav-link").forEach((link) => {
    link.classList.toggle("active", link.dataset.view === activeView);
  });
  await views[activeView]();
}

document.addEventListener("change", async (event) => {
  if (event.target.classList.contains("indeed-select")) {
    if (event.target.checked) indeedSelected.add(event.target.dataset.key);
    else indeedSelected.delete(event.target.dataset.key);
    updateIndeedSelectionUi();
    return;
  }
  if (event.target.id === "indeedSelectAll") {
    indeedSelected = event.target.checked
      ? new Set(indeedCandidates.map((profile) => profile._selectionKey))
      : new Set();
    document.querySelectorAll(".indeed-select").forEach((checkbox) => {
      checkbox.checked = event.target.checked;
    });
    updateIndeedSelectionUi();
    return;
  }
  if (!["jobSelect", "addJobSelect", "indeedJobSelect"].includes(event.target.id)) return;
  activeJobId = event.target.value ? Number(event.target.value) : null;
  if (event.target.id === "jobSelect") await go(activeView);
});

document.addEventListener("click", async (event) => {
  const nav = event.target.closest(".nav-link");
  if (nav) {
    await go(nav.dataset.view);
    return;
  }

  if (event.target.classList.contains("modal")) {
    closeModal();
    return;
  }

  const button = event.target.closest("[data-action]");
  if (!button || button.disabled) return;
  const action = button.dataset.action;
  const id = Number(button.dataset.id);
  const index = Number(button.dataset.index);

  if (action === "close-modal") {
    closeModal();
    return;
  }
  if (action === "navigate") {
    await go(button.dataset.view);
    return;
  }

  const actions = {
    "retry": retry,
    "refresh-indeed": scanIndeedCandidates,
    "open-indeed-result": () => openIndeedResult(index),
    "review-indeed-result": () => {
      const profile = indeedCandidates[index];
      if (!profile) throw new Error("That displayed candidate is no longer available.");
      showIndeedImport(profile);
    },
    "toggle-all-indeed": toggleAllIndeedCandidates,
    "lookup-indeed": lookupSelectedIndeedCandidates,
    "show-cached-indeed-results": showCachedIndeedResults,
    "dismiss-beta-notice": dismissIndeedBetaNotice,
    "filter-indeed-results": () => filterIndeedResults(button.dataset.filter),
    "export-indeed": exportIndeedMatches,
    "open-provider-result": () => openProviderResult(button.dataset.providerUrl || ""),
    "open-resume": () => openStoredResume(
      Number(button.dataset.candidateId),
      Number(button.dataset.resumeId),
    ),
    "placeholder-action": () => notify(`${button.dataset.label || "This integration"} requires account configuration.`),
    "bulk-import-indeed": () => bulkImportIndeed(false),
    "bulk-enrich-indeed": () => bulkImportIndeed(true),
    "capture-indeed": captureIndeedProfile,
    "import-indeed": importIndeedProfile,
    "create-job": createJob,
    "submit-intake": submitIntake,
    "enrich": () => enrichCandidate(id),
    "enrich-all": enrichAll,
    "rank-all": rankAll,
    "move": () => moveCandidate(id),
    "draft": () => draftOutreach(id),
    "approve-draft": () => approveDraft(id),
    "copy-draft": copyDraft,
    "add-dnc": addDnc,
    "save-backend": saveBackend,
    "test-backend": () => refreshHealth(true),
  };

  if (actions[action]) await withBusy(button, actions[action]);
});

(async function initialize() {
  if (!IS_EXTENSION) {
    document.querySelector("[data-view='indeed']")?.classList.add("hidden");
  } else {
    document.body.classList.add("extension-shell");
  }
  await loadBackendConfig();
  const health = await refreshHealth();
  if (health) {
    try {
      await loadJobs();
    } catch {
      setConnection();
    }
  }
  await go(IS_EXTENSION ? "indeed" : "candidates");
})();
