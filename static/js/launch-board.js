(function(){
  const S = {
    user:null, profile:null, authMode:"login",
    provider:{
      q:"", category:"", license_title:"", zip:"", radius_mi:"25", state_code:"",
      city:"", min_experience:"", max_experience:"", contact_available:"", compact:"",
      licensed_state:"", worked_at:"", travel_experience:""
    },
    providerCache:new Map(), providerInflight:new Map(), providerCards:new Map(), releasedContacts:new Map(), providerReq:0,
    providerOffset:0, providerTotal:null, providerHasNext:false, facetCategories:{},
    providerLastData:null, facetsLoaded:false, activeCounts:null,
    // Messaging + talent pools
    threads:[], activeThread:null, threadPoll:null, msgFilter:"",
    threadsReq:0, threadReq:0,
    pools:[], activePool:null, poolStage:"", poolMembership:new Map(), poolsReq:0,
    // Candidate matching
    jobsById:new Map(), matchJobId:null, matchRun:null,
    // Bulk selection in the directory / AI results
    selected:new Set(),
    // Saved searches (standing sourcing criteria + their alerts)
    searches:[],
    // Duplicates review + employer portal
    dupes:[], employer:null, templates:[], credits:null
  };

  const $ = (sel, root=document) => root.querySelector(sel);
  const $$ = (sel, root=document) => Array.from(root.querySelectorAll(sel));
  function esc(value){
    return (value == null ? "" : String(value)).replace(/[&<>"']/g, c => {
      if (c === "&") return "&amp;";
      if (c === "<") return "&lt;";
      if (c === ">") return "&gt;";
      if (c === '"') return "&quot;";
      return "&#39;";
    });
  }
  const token = () => localStorage.getItem("hb_token") || "";
  const setToken = v => v ? localStorage.setItem("hb_token", v) : localStorage.removeItem("hb_token");
  const setRefresh = v => v ? localStorage.setItem("hb_refresh", v) : localStorage.removeItem("hb_refresh");
  const isRecruiter = () => S.user && ["recruiter","employer","admin"].includes(S.user.role);
  const initials = (f,l) => ((f||" ")[0] + (l||" ")[0]).toUpperCase();
  // Provider identity is withheld until the recruiter deliberately reveals the
  // contact, so the directory shows initials only ("T. H.") up to that point.
  const isRevealed = id => S.releasedContacts.has(id);
  // The server only sends first/last name once the profile is released; until
  // then all we have (and all we show) is `masked_name`.
  const displayName = (p) => {
    const full = `${(p.first_name || "").trim()} ${(p.last_name || "").trim()}`.trim();
    return (isRevealed(p.profile_id) && full) ? full : (p.masked_name || "—");
  };
  const loading = text => `<div class="loading-state"><span class="spinner"></span><strong>${esc(text)}</strong></div>`;
  // Same state block, but legal inside a <tbody>. Column counts must match the
  // <thead> in board.html so the state spans the full table width.
  const JOB_COLS = 5, PROVIDER_COLS = 8;   // 8 = checkbox + 7 data columns
  const loadingRow = (cols, text) => `<tr class="row-state"><td colspan="${cols}">${loading(text)}</td></tr>`;
  const clean = value => (value == null ? "" : String(value)).replace(/\s+/g, " ").trim();
  const short = (value, limit=64) => {
    const text = clean(value);
    return text.length > limit ? text.slice(0, limit - 1).trim() + "..." : text;
  };
  const placeholder = text => `<span class="contact-placeholder">${esc(text)}</span>`;
  const noisyProfileText = value => {
    const text = clean(value);
    if (!text) return true;
    if (text.length > 72) return true;
    if (/@|\(\d{3}\)|\d{3}[-.\s]\d{3}[-.\s]\d{4}|[_-]{4,}|•/.test(text)) return true;
    if (/\u2022|\u00e2\u20ac\u00a2/.test(text)) return true;
    if (/^\d+\s+/.test(text)) return true;
    if (/\b(road|rd|street|st|drive|dr|court|ct|way|avenue|ave|blvd|lane|ln|apt|suite)\b/i.test(text)) return true;
    if (/\b(summary|membership|organizational|education|licensure|certifications?|experience|social media|proficiency|training)\b/i.test(text)) return true;
    return false;
  };
  const providerSubtitle = p => {
    if (!noisyProfileText(p.specialty)) return short(p.specialty, 58);
    if (!noisyProfileText(p.headline)) return short(p.headline, 58);
    if (p.provider_category && !["Other", "Others"].includes(p.provider_category)) return `${p.provider_category} provider`;
    if (p.profession_type) return `${p.profession_type} provider`;
    return "Healthcare provider";
  };
  const providerLocation = p => {
    const city = noisyProfileText(p.city) ? "" : clean(p.city);
    const state = /^[A-Z]{2}$/.test(clean(p.state_code).toUpperCase()) ? clean(p.state_code).toUpperCase() : "";
    return [city, state].filter(Boolean).join(", ");
  };

  async function api(method, path, body){
    const headers = {};
    if (body !== undefined && !(body instanceof FormData)) headers["Content-Type"] = "application/json";
    if (token()) headers.Authorization = "Bearer " + token();
    // no-store: this data (threads, pools, counts) changes constantly, and a
    // cached GET would silently render stale state after a write.
    const res = await fetch(path, {method, headers, cache:"no-store", body: body instanceof FormData ? body : body !== undefined ? JSON.stringify(body) : undefined});
    const text = await res.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch(e) { data = text; }
    if (!res.ok) {
      const detail = data && data.detail ? data.detail : res.statusText;
      const err = new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
      err.status = res.status;
      throw err;
    }
    return data;
  }
  const get = p => api("GET", p);
  const post = (p,b={}) => api("POST", p, b);
  const patch = (p,b={}) => api("PATCH", p, b);

  async function login(email,password){
    const r = await post("/api/auth/login", {email, password, mfa_code:null});
    setToken(r.access_token); setRefresh(r.refresh_token);
  }
  async function register(payload){
    const r = await post("/api/auth/register", payload);
    setToken(r.access_token); setRefresh(r.refresh_token);
  }

  function applyRole(){
    $$(".recruiter-only").forEach(el => el.classList.toggle("hidden", !isRecruiter()));
    if (!isRecruiter() && $("#page-providers").classList.contains("active")) showPage("dashboard");
  }

  async function loadMe(){
    if (!token()) return false;
    try {
      S.user = await get("/api/auth/me");
      try { S.profile = await get("/api/profiles/me"); } catch(e) { S.profile = null; }
      applyRole();
      const name = S.profile ? `${S.profile.first_name} ${S.profile.last_name}` : S.user.email.split("@")[0];
      const role = isRecruiter() ? "Recruiter" : (S.profile && (S.profile.specialty || S.profile.profession_type)) || "Healthcare Pro";
      $("#mini-name").textContent = name; $("#top-name").textContent = name.split(" ")[0];
      $("#mini-role").textContent = role;
      const av = S.profile ? initials(S.profile.first_name,S.profile.last_name) : S.user.email[0].toUpperCase();
      $("#mini-avatar").textContent = av; $("#top-avatar").textContent = av;
      $("#auth-gate").classList.add("hidden"); $("#app-shell").classList.remove("hidden");
      return true;
    } catch(e) {
      setToken(""); setRefresh(""); return false;
    }
  }

  function showAuthMode(mode){
    S.authMode = mode;
    $$(".auth-tab").forEach(b => b.classList.toggle("active", b.dataset.authMode === mode));
    $("#signup-fields").classList.toggle("open", mode === "signup");
    $("#auth-title").textContent = mode === "signup" ? "Create your account" : "Welcome back";
    $("#auth-subtitle").textContent = mode === "signup" ? "Join HealthBoard in seconds." : "Sign in to access your workspace.";
    $("#auth-submit").textContent = mode === "signup" ? "Create account" : "Sign in";
    $("#auth-hint").classList.toggle("show", mode === "signup");
    $("#auth-password").setAttribute("autocomplete", mode === "signup" ? "new-password" : "current-password");
    $("#auth-error").textContent = "";
  }

  async function submitAuth(){
    const err = $("#auth-error");
    const email = $("#auth-email").value.trim();
    const password = $("#auth-password").value;
    err.textContent = "";
    if (!email || !password) { err.textContent = "Email and password are required."; return; }
    const btn = $("#auth-submit"); const label = btn.textContent;
    btn.disabled = true; btn.textContent = "Please wait…";
    try {
      if (S.authMode === "signup") {
        if (password.length < 8) throw new Error("Password must be at least 8 characters.");
        await register({
          email, password,
          role: $("input[name='signup-role']:checked").value,
          first_name: $("#auth-first").value.trim(),
          last_name: $("#auth-last").value.trim()
        });
      } else {
        await login(email,password);
      }
      await startApp();
    } catch(e) {
      btn.disabled = false; btn.textContent = label;
      err.textContent = e.message || "Authentication failed.";
    }
  }

  function showPage(id){
    if ((id === "providers" || id === "ai" || id === "extension" || id === "pools"
         || id === "matching" || id === "outreach" || id === "credits") && !isRecruiter()) id = "dashboard";
    if (id !== "messages") stopMessagePolling();
    try { localStorage.setItem("hb_page", id); } catch(e) {}   // restored on refresh
    $$(".page").forEach(p => p.classList.toggle("active", p.id === "page-" + id));
    $$(".nav-item").forEach(n => n.classList.toggle("active", n.dataset.page === id));
    if (id === "dashboard") loadDashboard();
    if (id === "jobs") loadJobs();
    if (id === "providers") { if (!S.facetsLoaded) loadProviderFacets(); loadProviders(); loadSavedSearches(); }
    if (id === "ai") setTimeout(() => { const el = $("#ai-input"); if (el) el.focus(); }, 60);
    if (id === "extension") loadExtensionPage();
    if (id === "profile") loadProfile();
    if (id === "community") loadFeed();
    if (id === "notifications") { loadNotifications(); refreshNotificationBadge(); }
    if (id === "messages") loadMessages();
    if (id === "pools") loadPools();
    if (id === "employer") loadEmployer();
    if (id === "analytics") loadAnalytics();
    if (id === "outreach") loadOutreach();
    if (id === "credits") loadCredits();
  }

  function jobRow(j){
    const loc = [j.city,j.state_code].filter(Boolean).join(", ") || "Flexible";
    const pay = j.pay_rate_max ? `$${Math.round(j.pay_rate_max)}${j.pay_unit === "hourly" ? "/hr" : ""}` : "";
    // Agencies file one req per seat; the list is grouped, so say how many.
    const seats = (j.openings || 1) > 1 ? `<span class="badge accent openings">${j.openings} openings</span>` : "";
    const fit = j.fit_score > 0 ? `<span class="fit-badge">match</span>` : "";
    const sub = [j.facility, loc].filter(Boolean).join(" · ");
    return `<tr>
      <td>
        <div class="cell-name">${esc(j.title)}${j.is_urgent ? `<span class="badge coral">Urgent</span>` : ""}${seats}${fit}</div>
        <div class="cell-sub">${esc(sub)}</div>
      </td>
      <td>${j.job_type ? `<span class="badge accent">${esc(j.job_type)}</span>` : `<span class="cell-none">—</span>`}</td>
      <td>${j.specialty ? `<span class="badge">${esc(j.specialty)}</span>` : `<span class="cell-none">—</span>`}</td>
      <td>${pay ? `<strong>${esc(pay)}</strong>` : `<span class="cell-none">—</span>`}</td>
      <td class="td-actions">${isRecruiter()
        ? `<button class="btn small primary" data-source="${j.job_id}" title="Find matching candidates"><i class="fas fa-bolt"></i>Source</button>`
        : `<button class="btn small primary" data-apply="${j.job_id}">Apply</button>`}</td>
    </tr>`;
  }

  async function loadDashboard(){
    $("#dashboard-jobs").innerHTML = loadingRow(JOB_COLS, "Loading jobs...");
    try {
      // A professional sees roles ranked against their own profile; a
      // recruiter just sees what is live on the board.
      const jobs = await get(isRecruiter() ? "/api/jobs?limit=4"
                                           : "/api/jobs/recommended?limit=4");
      const sub = $("#rec-sub");
      if (sub && !isRecruiter())
        sub.textContent = (jobs.items || []).some(j => j.fit_score > 0)
          ? "Matched to your licence, specialty and location"
          : "Complete your profile to get roles matched to you";
      $("#metric-jobs").textContent = jobs.total || 0;
      $("#dashboard-jobs").innerHTML = jobs.items.length ? jobs.items.map(jobRow).join("") : loadingRow(JOB_COLS, "No open roles yet.");
    } catch(e) { $("#dashboard-jobs").innerHTML = loadingRow(JOB_COLS, "Could not load jobs."); }
    if (S.profile) {
      $("#dash-sub").textContent = `Welcome back, ${S.profile.first_name}.`;
      $("#metric-profile").textContent = `${S.profile.completion_score || 0}%`;
      try { $("#metric-apps").textContent = (await get("/api/applications/mine")).length; } catch(e) {}
      try { $("#metric-saved").textContent = (await get("/api/applications/saved")).length; } catch(e) {}
    } else {
      $("#dash-sub").textContent = isRecruiter() ? "Recruiter workspace" : "Complete your profile to get matched.";
    }
  }

  async function loadJobs(){
    const params = new URLSearchParams({limit:"50", group_openings:"true"});
    if ($("#job-q").value.trim()) params.set("q", $("#job-q").value.trim());
    if ($("#job-type").value) params.set("job_type", $("#job-type").value);
    if ($("#job-state").value.trim()) params.set("state_code", $("#job-state").value.trim().toUpperCase());
    $("#jobs-list").innerHTML = loadingRow(JOB_COLS, "Loading jobs...");
    try {
      const data = await get("/api/jobs?" + params.toString());
      const seats = data.items.reduce((n,j) => n + (j.openings || 1), 0);
      $("#jobs-count").textContent = `${data.total} role${data.total === 1 ? "" : "s"}`
        + (seats > data.items.length ? ` · ${seats} openings on this page` : "");
      data.items.forEach(j => S.jobsById.set(j.job_id, j));   // for the sourcing header
      $("#jobs-list").innerHTML = data.items.length ? data.items.map(jobRow).join("") : loadingRow(JOB_COLS, "No jobs match this search.");
    } catch(e) { $("#jobs-list").innerHTML = loadingRow(JOB_COLS, "Could not load jobs."); }
  }

  // Built to scale to millions of rows: server-side filtering + paging. We never
  // fetch an exact COUNT for broad browses (that's an O(rows) scan) — the tab
  // totals come from the cached /facets endpoint, and Prev/Next uses the API's
  // has_next flag. An exact total is requested only for selective filter combos.
  const PAGE_SIZE = 60;
  // Curated license/title options (relevant to healthcare staffing) shown with
  // full names. Codes not listed here (MBBS, PharmD, degrees, ...) are hidden.
  const LICENSE_LABELS = {
    MD:"Physician (MD)", DO:"Physician (DO)", NP:"Nurse Practitioner (NP)",
    PA:"Physician Assistant (PA)", CRNA:"Nurse Anesthetist (CRNA)",
    CNM:"Nurse Midwife (CNM)", DNP:"Doctor of Nursing Practice (DNP)",
    FNP:"Family Nurse Practitioner (FNP)", RN:"Registered Nurse (RN)",
    LPN:"Licensed Practical Nurse (LPN)", LVN:"Licensed Vocational Nurse (LVN)",
    CNA:"Certified Nursing Assistant (CNA)", RT:"Respiratory Therapist (RT)",
    PT:"Physical Therapist (PT)", OT:"Occupational Therapist (OT)",
  };
  const LICENSE_ORDER = ["MD","DO","NP","PA","CRNA","CNM","DNP","FNP","RN","LPN","LVN","CNA","RT","PT","OT"];
  function providerParams(state=S.provider, opts={}){
    const params = new URLSearchParams({limit:String(PAGE_SIZE), providers_only:"true"});
    Object.entries(state).forEach(([k,v]) => {
      if (k === "zip" || k === "radius_mi") return;   // sent together below
      if (v !== "" && v != null) params.set(k, v);
    });
    // A radius search centres on a ZIP or a city (city is sent by the loop above).
    if (state.radius_mi && (state.zip || state.city)){
      if (state.zip) params.set("zip", state.zip);
      params.set("radius_mi", state.radius_mi);
    }
    params.set("offset", String(opts.offset || 0));
    params.set("count", opts.count ? "1" : "0");
    return params;
  }
  function hasNonCategoryFilters(){
    const s = S.provider;
    return ["q","license_title","state_code","licensed_state","worked_at","travel_experience","city","min_experience","max_experience","contact_available","compact"].some(k => s[k]) || !!(s.radius_mi && (s.zip || s.city));
  }
  function facetTotalFor(category){
    const c = S.activeCounts || S.facetCategories;   // counts reflect the active filters
    if (!c || !Object.keys(c).length) return null;
    return category ? (c[category] || 0) : ["Physicians","Nursing","Allied","APP","Others"].reduce((s,k)=>s+(c[k]||0),0);
  }
  // Faceted counts: the headline + tab numbers reflect the CURRENT filters.
  function countParams(){
    const s = S.provider, p = new URLSearchParams();
    ["q","license_title","state_code","licensed_state","worked_at","travel_experience","city","min_experience","max_experience","contact_available","compact"].forEach(k => { if (s[k]) p.set(k, s[k]); });
    if (s.radius_mi && (s.zip || s.city)){ if (s.zip) p.set("zip", s.zip); p.set("radius_mi", s.radius_mi); }
    return p.toString();
  }
  function paintCounts(){
    const c = S.activeCounts || S.facetCategories || {};
    const total = ["Physicians","Nursing","Allied","APP","Others"].reduce((s,k)=>s+(c[k]||0),0);
    $("#providers-count").textContent = `${total.toLocaleString()} provider${total===1?"":"s"}`;
    $$("#provider-tabs .tab").forEach(t => {
      if (t.dataset.base == null) t.dataset.base = t.textContent.replace(/\s+[\d,]+$/, "").trim();
      const n = t.dataset.category ? (c[t.dataset.category] || 0) : total;
      t.textContent = `${t.dataset.base} ${Number(n).toLocaleString()}`;
    });
  }
  let _countSeq = 0;
  const _countCache = new Map();
  async function refreshCounts(){
    if (!isRecruiter()) return;
    const seq = ++_countSeq;
    if (!hasNonCategoryFilters()){ S.activeCounts = S.facetCategories; paintCounts(); return; }
    const key = countParams();
    if (_countCache.has(key)){ S.activeCounts = _countCache.get(key); paintCounts(); return; }
    let counts;
    try { counts = await get("/api/profiles/category-counts?" + key); }
    catch(e){ return; }
    if (seq !== _countSeq) return;   // a newer filter change superseded this
    _countCache.set(key, counts);
    S.activeCounts = counts;
    paintCounts();
  }
  async function fetchProviders(params){
    const key = params.toString();
    if (S.providerCache.has(key)) return S.providerCache.get(key);
    if (S.providerInflight.has(key)) return S.providerInflight.get(key);
    const req = get("/api/profiles?" + key)
      .then(d => { S.providerCache.set(key,d); S.providerInflight.delete(key); return d; })
      .catch(e => { S.providerInflight.delete(key); throw e; });
    S.providerInflight.set(key, req);
    return req;
  }
  function prefetchProviderTabs(){
    if (hasNonCategoryFilters()) return;  // only warm tabs on an unfiltered browse
    ["Physicians","Nursing","Allied","APP","Others"].forEach(category => {
      fetchProviders(providerParams({...S.provider, category}, {offset:0, count:false})).catch(()=>{});
    });
  }
  // The headline shows the whole directory's size and stays constant regardless of
  // the selected tab/filter. The current view's count lives on the tabs + the pager.
  function providerCountLabel(){
    const t = facetTotalFor("");
    return t == null ? "Loading providers…" : `${t.toLocaleString()} providers`;
  }
  function renderProviderPage(data){
    const items = data.items || [];
    items.forEach(p => {
      S.providerCards.set(p.profile_id, p);
      // The server remembers releases, so one made in an earlier session comes
      // back already unlocked — seed the session map from it.
      if (p.is_released && !S.releasedContacts.has(p.profile_id)) {
        S.releasedContacts.set(p.profile_id, p);
      }
    });
    $("#providers-count").textContent = providerCountLabel();
    $("#providers-grid").innerHTML = items.length ? items.map(providerRow).join("") : loadingRow(PROVIDER_COLS, "No providers match these filters.");
    refreshPoolMembership(items.map(p => p.profile_id));
    renderBulkBar();
  }
  function renderPager(data){
    const pager = $("#providers-pager"); if (!pager) return;
    const shown = (data.items || []).length;
    const start = shown ? S.providerOffset + 1 : 0;
    const end = S.providerOffset + shown;
    const total = S.providerTotal;
    const label = !shown ? ""
      : total != null ? `${start.toLocaleString()}–${end.toLocaleString()} of ${total.toLocaleString()}`
      : `Page ${Math.floor(S.providerOffset / PAGE_SIZE) + 1}`;
    pager.innerHTML = shown ? `
      <button class="btn small" id="prov-prev" ${S.providerOffset <= 0 ? "disabled" : ""}><i class="fas fa-chevron-left"></i> Prev</button>
      <span class="pager-label">${label}</span>
      <button class="btn small" id="prov-next" ${!S.providerHasNext ? "disabled" : ""}>Next <i class="fas fa-chevron-right"></i></button>` : "";
    const prev = $("#prov-prev"), next = $("#prov-next");
    if (prev) prev.onclick = () => { if (S.providerOffset > 0){ S.providerOffset = Math.max(0, S.providerOffset - PAGE_SIZE); loadProviders(); document.querySelector('.main').scrollTo(0,0); } };
    if (next) next.onclick = () => { if (S.providerHasNext){ S.providerOffset += PAGE_SIZE; loadProviders(); document.querySelector('.main').scrollTo(0,0); } };
  }
  function providerFilterChanged(){ S.providerOffset = 0; refreshCounts(); loadProviders(); }
  function setExperienceFilter(value){
    S.provider.min_experience = "";
    S.provider.max_experience = "";
    if (value === "0-2") {
      S.provider.min_experience = "0";
      S.provider.max_experience = "2";
    } else if (value === "3-5") {
      S.provider.min_experience = "3";
      S.provider.max_experience = "5";
    } else if (value === "6-10") {
      S.provider.min_experience = "6";
      S.provider.max_experience = "10";
    } else if (value === "10+") {
      S.provider.min_experience = "10";
    }
  }
  function providerContactCell(p){
    const released = S.releasedContacts.get(p.profile_id);
    if (released) {
      const lines = [];
      if (released.email) lines.push(`<a href="mailto:${esc(released.email)}"><i class="fas fa-envelope"></i><span>${esc(released.email)}</span></a>`);
      if (released.phone) lines.push(`<a href="tel:${esc(released.phone)}"><i class="fas fa-phone"></i><span>${esc(released.phone)}</span></a>`);
      // Provenance is only worth showing once the contact is revealed.
      const by = p.contact_updated_by_email ? `<span class="contact-meta"><i class="fas fa-clock-rotate-left"></i>Updated by ${esc(p.contact_updated_by_email)}</span>` : "";
      return `<div class="contact-cell is-revealed">
        ${lines.length ? `<div class="contact-lines">${lines.join("")}</div>` : `<span class="contact-none">No contact on file</span>`}
        ${by}
      </div>`;
    }
    // Availability comes from has_email/has_phone — the values themselves are
    // not in the payload until the profile is released.
    if (!(p.has_email || p.has_phone)) {
      return `<div class="contact-cell"><span class="contact-none"><i class="fas fa-minus"></i>No contact on file</span></div>`;
    }
    // Locked: one compact reveal action + a hint of which channels are on file.
    const avail = [
      p.has_email ? `<i class="fas fa-envelope" title="Email on file"></i>` : "",
      p.has_phone ? `<i class="fas fa-phone" title="Phone on file"></i>` : "",
    ].join("");
    return `<div class="contact-cell">
      <button class="reveal-btn" data-release="${p.profile_id}" title="Reveal contact details"><i class="fas fa-unlock-keyhole"></i>Reveal contact</button>
      <span class="contact-avail">${avail}</span>
    </div>`;
  }
  function providerRow(p,i){
    S.providerCards.set(p.profile_id, p);
    const loc = providerLocation(p);
    const profession = short(p.profession_type || "Pro", 28);
    const specialty = providerSubtitle(p);
    return `<tr data-row="${p.profile_id}"${S.selected.has(p.profile_id) ? ' class="is-selected"' : ""}>
      <td class="td-check"><input type="checkbox" class="row-check" data-pick="${p.profile_id}"${S.selected.has(p.profile_id) ? " checked" : ""}></td>
      <td>
        <div class="cell-user">
          <span class="avatar">${esc(p.initials || initials(p.first_name, p.last_name))}</span>
          <div class="cell-id">
            <div class="cell-name">${esc(displayName(p))}${isRevealed(p.profile_id) ? "" : `<i class="fas fa-lock name-lock" title="Reveal contact to see the full name"></i>`}</div>
            <div class="cell-sub">${esc(specialty)}</div>
          </div>
        </div>
      </td>
      <td><span class="badge accent"><i class="fas fa-circle-check"></i>${esc(profession)}</span></td>
      <td><span class="badge">${esc(p.provider_category || "Others")}</span></td>
      <td>${p.years_experience ? `${esc(p.years_experience)} yrs` : `<span class="cell-none">—</span>`}</td>
      <td>${loc ? esc(loc) : `<span class="cell-none">—</span>`}</td>
      <td class="td-contact">${providerContactCell(p)}</td>
      <td class="td-actions">
        <button class="btn small" data-resume="${p.profile_id}" title="View résumé"><i class="fas fa-file-lines"></i>Résumé</button>
        <button class="btn small${S.poolMembership.get(p.profile_id) ? " saved" : ""}" data-pool-save="${p.profile_id}" title="Save to a talent pool"><i class="fas fa-layer-group"></i>${S.poolMembership.get(p.profile_id) ? "Saved" : "Save"}</button>
      </td>
    </tr>`;
  }
  async function loadProviders(){
    if (!isRecruiter()) return;
    const filtered = hasNonCategoryFilters();
    // Exact count only for selective filter combos, computed once on page 1.
    const wantCount = filtered && S.providerOffset === 0;
    const params = providerParams(S.provider, {offset:S.providerOffset, count:wantCount});
    const seq = ++S.providerReq;
    if (!S.providerCache.has(params.toString())) {
      $("#providers-grid").innerHTML = loadingRow(PROVIDER_COLS, "Loading providers...");
      $("#providers-count").textContent = providerCountLabel();   // constant grand total
    }
    try {
      const data = await fetchProviders(params);
      if (seq !== S.providerReq) return;   // a newer request superseded this one
      if (data.total != null) S.providerTotal = data.total;         // filtered page 1
      else if (!filtered) S.providerTotal = facetTotalFor(S.provider.category);
      // (filtered offset>0 keeps the total captured on page 1)
      S.providerHasNext = !!data.has_next;
      S.providerLastData = data;
      renderProviderPage(data);
      renderPager(data);
      prefetchProviderTabs();
    } catch(e) {
      $("#providers-grid").innerHTML = loadingRow(PROVIDER_COLS, e.status === 403 ? "Recruiter access required." : "Could not load providers.");
      $("#providers-pager").innerHTML = "";
    }
  }
  async function loadProviderFacets(){
    if (!isRecruiter() || S.facetsLoaded) return;
    try {
      const f = await get("/api/profiles/facets");
      S.facetsLoaded = true;
      S.facetCategories = f.categories || {};
      const avail = new Set((f.license_titles || f.professions || []).map(v => String(v).toUpperCase().replace(/[.\s-]/g, "")));
      const licOpts = LICENSE_ORDER.filter(code => avail.has(code)).map(code => `<option value="${code}">${esc(LICENSE_LABELS[code])}</option>`);
      $("#provider-license-title").innerHTML = `<option value="">Any license/title</option>` + licOpts.join("");
      $("#provider-state").innerHTML = `<option value="">Any state</option>` + (f.states || []).map(v => `<option>${esc(v)}</option>`).join("");
      // The selects only just gained their options — re-apply any active filter
      // (e.g. one the copilot set) so the dropdowns reflect it.
      syncProviderControls();
      // Set the tab + headline counts (respecting any filters already applied).
      if (!hasNonCategoryFilters()) S.activeCounts = S.facetCategories;
      paintCounts();
      // Facets arrived after the first paint: refresh the current view's pager.
      if (!hasNonCategoryFilters() && S.providerLastData){
        S.providerTotal = facetTotalFor(S.provider.category);
        renderPager(S.providerLastData);
      }
    } catch(e) {}
  }

  function fieldRow(label, value, hint){
    return `<div><div class="muted">${esc(label)}</div><strong>${
      value ? esc(value) : `<span class="cell-none">${esc(hint || "Not provided")}</span>`}</strong></div>`;
  }
  async function loadProfile(){
    if (!S.profile) { $("#profile-card").innerHTML = loading("No profile found."); return; }
    const p = S.profile;
    $("#profile-sub").textContent = `${p.completion_score || 0}% complete`;
    $("#profile-card").innerHTML = `<div class="profile-grid">
      ${fieldRow("Name", `${p.first_name || ""} ${p.last_name || ""}`.trim())}
      ${fieldRow("Licence / title", p.profession_type, "Add your licence")}
      ${fieldRow("Specialty", p.specialty, "Add your specialty")}
      ${fieldRow("Experience", p.years_experience ? `${p.years_experience} years` : "", "Add your experience")}
      ${fieldRow("Email", p.email || (S.user && S.user.email))}
      ${fieldRow("Phone", p.phone)}
      ${fieldRow("Location", [p.city, p.state_code].filter(Boolean).join(", "))}
      ${fieldRow("Desired rate", p.pay_min_hourly ? `$${p.pay_min_hourly}/hr` : "")}
      ${fieldRow("Open to work", p.open_to_work ? "Yes" : "Not set")}
      ${fieldRow("Résumé", p.resume_url ? "On file" : "", "Upload one")}
    </div>`;
    loadCompletion();
    loadCredentials();
  }
  // Tells a professional exactly what is still missing. An incomplete profile
  // is not a cosmetic problem here: the matching engine ranks on licence,
  // specialty, experience and location, so a blank profile is unfindable.
  async function loadCompletion(){
    const box = $("#profile-progress");
    if (!box) return;
    try {
      const c = await get("/api/profiles/me/completion");
      const pct = Math.max(0, Math.min(100, c.score || 0));
      box.innerHTML = `<div class="pc-wrap">
        <div class="pc-head"><b>${pct}%</b><span>${c.complete
          ? "Your profile is complete — recruiters can find you."
          : "Complete your profile so recruiters can find you"}</span></div>
        <div class="pc-bar"><i class="${c.complete ? "done" : ""}" style="width:${pct}%"></i></div>
        ${c.missing.length ? `<div class="pc-missing">${
          c.missing.map(m => `<span class="pc-chip">${esc(m)}</span>`).join("")}</div>` : ""}
        ${c.missing.length ? `<p class="pc-note">Recruiters search by licence, specialty, experience and location —
          without them your profile will not appear in their results.</p>` : ""}
      </div>`;
    } catch(e) { box.innerHTML = ""; }
  }
  // --- Credentials (the professional's own licences) -----------------------
  async function loadCredentials(){
    const box = $("#profile-credentials");
    if (!box || isRecruiter()) { if (box) box.innerHTML = ""; return; }
    try {
      const c = await get("/api/profiles/me/credentials");
      const row = (label, sub, status, days, id) => `<div class="cred-row">
        <b>${esc(label)}</b><span class="muted">${esc(sub || "")}</span>
        <span class="spacer"></span>
        <span class="cred-pill ${status}">${status === "expiring" ? `${days}d left`
          : status === "expired" ? "expired" : status === "valid" ? "valid" : "no expiry"}</span>
        ${id ? `<button class="cred-del" data-cred-del="${id}" title="Remove"><i class="fas fa-xmark"></i></button>` : ""}
      </div>`;
      box.innerHTML = `<div class="cred-wrap">
        <div class="cred-head"><h3>Licences &amp; certifications</h3>
          <button class="btn small primary" id="cred-add"><i class="fas fa-plus"></i>Add licence</button></div>
        ${c.alerts.length ? `<div class="cred-alert"><i class="fas fa-triangle-exclamation"></i>
           ${c.alerts.map(a => `${esc(a.label)} ${a.status === "expired" ? "has expired"
             : `expires in ${a.days_left} days`}`).join(" · ")}</div>` : ""}
        ${c.licenses.map(l => row(`${l.license_type} — ${l.state_code}`,
            [l.license_number, l.is_compact ? "compact" : ""].filter(Boolean).join(" · "),
            l.status, l.days_left, l.license_id)).join("")}
        ${c.certifications.map(x => row(x.cert_name, "", x.status, x.days_left, null)).join("")}
        ${(!c.licenses.length && !c.certifications.length)
          ? `<p class="muted" style="font-size:12.5px">No licences on file. Recruiters filter by
             licence and state — adding yours makes you findable.</p>` : ""}
        ${c.compact_eligible ? `<p class="pc-note"><i class="fas fa-shield-halved"></i>
           You hold a compact licence — eligible to practise in around 40 states.</p>` : ""}
      </div>`;
      const add = $("#cred-add");
      if (add) add.onclick = addCredential;
      $$("#profile-credentials [data-cred-del]").forEach(b => b.onclick = async () => {
        try { await del(`/api/profiles/me/licenses/${b.dataset.credDel}`); loadCredentials(); }
        catch(e) { alert(e.message); }
      });
    } catch(e) { box.innerHTML = ""; }
  }
  async function addCredential(){
    const type = prompt("Licence type (RN, LPN, MD, PT…):", "");
    if (!type || !type.trim()) return;
    const st = prompt("Issuing state (2 letters, e.g. TX):", "");
    if (!st || !st.trim()) return;
    const num = prompt("Licence number (optional):", "") || "";
    const exp = prompt("Expiry date (YYYY-MM-DD, optional):", "") || null;
    try {
      await post("/api/profiles/me/licenses", {
        license_type: type.trim().toUpperCase(), state_code: st.trim().toUpperCase(),
        license_number: num.trim(), expiry_date: exp || null});
      loadCredentials();
    } catch(e) { alert(e.message); }
  }

  function openProfileForm(){
    const p = S.profile || {}, f = $("#profile-form");
    f.classList.remove("hidden");
    $("#profile-card").classList.add("hidden");
    $("#profile-edit").classList.add("hidden");
    ["first_name","last_name","profession_type","specialty","years_experience","phone",
     "city","state_code","pay_min_hourly","headline","bio"].forEach(k => {
      const el = f.elements[k];
      if (el) el.value = p[k] == null ? "" : p[k];
    });
    if (f.elements.available_date)
      f.elements.available_date.value = p.available_date ? String(p.available_date).slice(0,10) : "";
    if (f.elements.open_to_work) f.elements.open_to_work.checked = !!p.open_to_work;
    $("#profile-msg").textContent = "";
  }
  function closeProfileForm(){
    $("#profile-form").classList.add("hidden");
    $("#profile-card").classList.remove("hidden");
    $("#profile-edit").classList.remove("hidden");
  }
  async function saveProfile(e){
    e.preventDefault();
    const f = $("#profile-form"), msg = $("#profile-msg");
    const body = {};
    ["first_name","last_name","profession_type","specialty","phone","city",
     "headline","bio"].forEach(k => { const v = (f.elements[k].value || "").trim(); if (v) body[k] = v; });
    const st = (f.elements.state_code.value || "").trim().toUpperCase();
    if (st) body.state_code = st;
    ["years_experience","pay_min_hourly"].forEach(k => {
      const v = f.elements[k].value;
      if (v !== "" && !isNaN(Number(v))) body[k] = Number(v);
    });
    if (f.elements.available_date.value) body.available_date = f.elements.available_date.value;
    body.open_to_work = !!f.elements.open_to_work.checked;
    msg.className = "pf-msg"; msg.textContent = "Saving…";
    try {
      S.profile = await patch("/api/profiles/me", body);
      msg.className = "pf-msg ok"; msg.textContent = "Saved.";
      loadProfile();
      setTimeout(closeProfileForm, 700);
    } catch(err) {
      msg.className = "pf-msg err"; msg.textContent = err.message || "Could not save.";
    }
  }

  async function loadFeed(){
    $("#feed-list").innerHTML = loading("Loading feed...");
    try {
      const data = await get("/api/social/posts?limit=20");
      $("#feed-list").innerHTML = (data.items || data || []).map(p => `<div class="list-row"><div><strong>${esc(p.author_name || "HealthBoard")}</strong><div class="muted">${esc(p.body || "")}</div></div></div>`).join("") || loading("No posts yet.");
    } catch(e) { $("#feed-list").innerHTML = loading("Could not load feed."); }
  }
  async function loadNotifications(){
    $("#notifications-list").innerHTML = loading("Loading notifications...");
    try {
      const data = await get("/api/notifications");
      $("#notifications-list").innerHTML = (data || []).map(n => `<div class="list-row"><div><strong>${esc(n.title)}</strong><div class="muted">${esc(n.body || "")}</div></div></div>`).join("") || loading("No notifications.");
    } catch(e) { $("#notifications-list").innerHTML = loading("Could not load notifications."); }
  }
  async function loadEmployer(){
    $("#employer-panel").innerHTML = loading("Loading recruiter dashboard...");
    try {
      const d = await get("/api/employers/me/dashboard");
      $("#employer-sub").textContent = d.employer ? d.employer.org_name : "Create an organization first";
      S.employer = d.employer || null;
      $("#employer-panel").innerHTML = d.employer
        ? `<div class="profile-grid"><div><div class="muted">Organization</div><strong>${esc(d.employer.org_name)}</strong></div><div><div class="muted">Open jobs</div><strong>${esc(d.kpis.jobs)}</strong></div><div><div class="muted">Applications</div><strong>${esc(d.kpis.applications)}</strong></div><div><div class="muted">Interviews</div><strong>${esc(d.kpis.interviews)}</strong></div></div>`
        : `<div class="match-empty"><i class="fas fa-building"></i><h3>No organization yet</h3>
           <p>Create one to post jobs and source against them.</p>
           <button class="btn primary" id="org-create"><i class="fas fa-plus"></i>Create organization</button></div>`;
      const oc = $("#org-create");
      if (oc) oc.onclick = createOrg;
      loadEmployerJobs();
    } catch(e) { $("#employer-panel").innerHTML = loading("Could not load employer dashboard."); }
  }
  async function createOrg(){
    const name = prompt("Organization name:", "");
    if (!name || !name.trim()) return;
    try { await post("/api/employers", {org_name: name.trim()}); loadEmployer(); }
    catch(e) { alert(e.message); }
  }
  async function loadEmployerJobs(){
    const box = $("#employer-jobs");
    if (!box) return;
    if (!S.employer){ box.innerHTML = ""; return; }
    box.innerHTML = loading("Loading your jobs...");
    try {
      const d = await get(`/api/jobs?employer_id=${encodeURIComponent(S.employer.employer_id)}&limit=50`);
      box.innerHTML = `<div class="an-section"><h2>Your job postings</h2>${
        d.items.length
          ? `<div class="table-wrap"><table class="table">
              <thead><tr><th>Role</th><th>Type</th><th>Location</th><th>Pay</th><th class="th-actions"></th></tr></thead>
              <tbody>${d.items.map(j => `<tr>
                <td><div class="cell-name">${esc(j.title)}</div><div class="cell-sub">${esc(j.specialty || j.profession_type || "")}</div></td>
                <td><span class="badge accent">${esc(j.job_type || "-")}</span></td>
                <td>${esc([j.city, j.state_code].filter(Boolean).join(", ") || "-")}</td>
                <td>${j.pay_rate_max ? `<strong>$${Math.round(j.pay_rate_max)}/hr</strong>` : "-"}</td>
                <td class="td-actions"><button class="btn small primary" data-source="${j.job_id}"><i class="fas fa-bolt"></i>Source</button></td>
              </tr>`).join("")}</tbody></table></div>`
          : `<p class="muted" style="font-size:13px">No jobs posted yet.</p>`}</div>`;
    } catch(e) { box.innerHTML = loading("Could not load your jobs."); }
  }
  async function postJob(){
    if (!S.employer) return alert("Create an organization first.");
    const title = prompt("Job title (e.g. \"ICU Registered Nurse\"):", "");
    if (!title || !title.trim()) return;
    const city = prompt("City:", "") || null;
    const state = (prompt("State code (e.g. TX):", "") || "").trim().toUpperCase() || null;
    const pay = parseFloat(prompt("Max pay rate per hour (numbers only):", "") || "");
    const spec = prompt("Specialty (optional, e.g. ICU):", "") || null;
    const prof = prompt("License / profession (optional, e.g. RN):", "") || null;
    try {
      const body = {title: title.trim(), city, state_code: state, specialty: spec,
                    profession_type: prof, job_type: "travel", pay_unit: "hourly"};
      if (!isNaN(pay)) { body.pay_rate_max = pay; body.pay_rate_min = pay; }
      await post(`/api/jobs?employer_id=${encodeURIComponent(S.employer.employer_id)}`, body);
      alert("Job posted.");
      loadEmployerJobs();
      loadJobs();
    } catch(e) { alert(e.message); }
  }

  async function applyJob(id){
    try { await post(`/api/jobs/${id}/apply`, {}); alert("Application submitted."); loadDashboard(); }
    catch(e) { alert(e.status === 409 ? "You already applied to this job." : e.message); }
  }
  async function uploadResume(file){
    const fd = new FormData(); fd.append("file", file);
    try {
      const res = await fetch("/api/uploads/resume", {method:"POST", headers:{Authorization:"Bearer " + token()}, body:fd});
      if (!res.ok) throw new Error(await res.text());
      const out = await res.json();
      await loadMe();
      // The upload also parses the résumé and fills blank profile fields. Say
      // which ones, so the person can see the work it saved them.
      const filled = Object.keys(out.contact_updated || {});
      $("#resume-drop span").textContent = filled.length
        ? `Uploaded — filled in ${filled.join(", ").replace(/_/g, " ")} from your résumé.`
        : "Résumé uploaded.";
      if ($("#page-profile").classList.contains("active")) loadProfile();
    } catch(e) { alert("Upload failed."); }
  }
  function renderResume(r){
    const sec = r.sections || {};
    const ini = (r.name || "?").split(/\s+/).map(w => w[0] || "").join("").slice(0, 2).toUpperCase();
    const lines = arr => (arr && arr.length) ? `<div class="rz-lines">${arr.map(x => `<p>${esc(x)}</p>`).join("")}</div>` : `<div class="rz-empty">Not listed on this résumé.</div>`;
    const fact = (label, val) => val ? `<div class="rz-fact"><span>${label}</span><b>${esc(val)}</b></div>` : "";
    // Enriched, structured data (no identity in it — safe even when name withheld).
    const avail = r.available_date ? new Date(r.available_date).toLocaleDateString(undefined,{month:"short",year:"numeric"}) : "";
    const licBlock = (r.licenses && r.licenses.length) ? `<h5>Verified licenses</h5>
      <div class="rz-lic">${r.licenses.map(l => `<span class="rz-lic-chip${l.compact ? " compact" : ""}">${esc(l.type)}<b>${esc(l.state)}</b>${l.compact ? `<i class="fas fa-shield-halved" title="Compact / multistate license"></i>` : ""}${l.expiry ? `<em>exp ${esc(l.expiry.slice(0,7))}</em>` : ""}</span>`).join("")}</div>
      ${r.has_compact ? `<p class="rz-compact-note"><i class="fas fa-shield-halved"></i>Holds a compact / multistate license — eligible to practice in ~40 states.</p>` : ""}` : "";
    const workStruct = (r.work_history && r.work_history.length) ? `<div class="rz-jobs">${r.work_history.map(w => `
      <div class="rz-job"><div class="rz-job-top"><b>${esc(w.title || "Role")}</b>${w.type ? `<span class="rz-job-type">${esc(w.type)}</span>` : ""}</div>
        <div class="rz-job-sub">${esc([w.employer, w.specialty, w.location].filter(Boolean).join("  ·  "))}</div>
        ${(w.start || w.end) ? `<div class="rz-job-dates">${esc(w.start || "?")} – ${esc(w.end || "present")}</div>` : ""}</div>`).join("")}</div>` : "";
    const eduStruct = (r.education && r.education.length) ? `<div class="rz-edu">${r.education.map(e => `<div class="rz-edu-item"><b>${esc([e.degree, e.field].filter(Boolean).join(", ") || e.institution || "—")}</b>${(e.institution || e.year) ? `<span>${esc([e.institution, e.year].filter(Boolean).join("  ·  "))}</span>` : ""}</div>`).join("")}</div>` : "";
    const overview = `
      <div class="rz-facts">
        ${fact("Focus", r.role)}
        ${fact("Location", r.location)}
        ${fact("License / title", r.credential)}
        ${r.years_experience ? fact("Experience", r.years_experience + " yrs") : ""}
        ${(r.licensed_states && r.licensed_states.length) ? fact("Licensed in", r.licensed_states.join(", ")) : ""}
        ${fact("Work authorization", r.work_authorization)}
        ${avail ? fact("Available", avail) : ""}
        ${fact("Board certification", r.board)}
      </div>
      ${licBlock}
      ${sec["Professional Summary"] ? `<h5>Summary</h5>${lines(sec["Professional Summary"])}` : ""}`;
    const skillsSec = `
      ${(r.skills && r.skills.length) ? `<h5>Skill map</h5><div class="rz-chips">${r.skills.map(s => `<span class="rz-chip">${esc(s)}</span>`).join("")}</div>` : ""}
      ${sec["Skills"] ? `<h5>Skills</h5>${lines(sec["Skills"])}` : ""}
      ${sec["Languages"] ? `<h5>Languages</h5>${lines(sec["Languages"])}` : ""}
      ${(!(r.skills || []).length && !sec["Skills"] && !sec["Languages"]) ? `<div class="rz-empty">Not listed on this résumé.</div>` : ""}`;
    const certsSec = `
      ${sec["Certifications & Licensure"] ? `<h5>Certifications & Licensure</h5>${lines(sec["Certifications & Licensure"])}` : ""}
      ${sec["Professional Memberships"] ? `<h5>Memberships</h5>${lines(sec["Professional Memberships"])}` : ""}
      ${(!sec["Certifications & Licensure"] && !sec["Professional Memberships"]) ? `<div class="rz-empty">Not listed on this résumé.</div>` : ""}`;
    // The whole résumé renders as one scrolling document; the nav jumps to a section.
    // Prefer the structured (enriched) work history / education; fall back to the
    // résumé text sections when enrichment hasn't reached this candidate yet.
    const expSec = workStruct
      ? workStruct + (sec["Experience"] ? `<h5>From résumé</h5>${lines(sec["Experience"])}` : "")
      : lines(sec["Experience"]);
    const eduSec = eduStruct
      ? eduStruct + (sec["Education & Training"] ? `<h5>From résumé</h5>${lines(sec["Education & Training"])}` : "")
      : lines(sec["Education & Training"]);
    const sections = [
      ["overview", "Overview", overview],
      ["experience", "Experience", expSec],
      ["education", "Education", eduSec],
      ["skills", "Skills", skillsSec],
      ["certifications", "Certifications", certsSec],
    ];
    return `<div class="rz">
      <div class="rz-head">
        <div class="rz-avatar">${esc(ini)}</div>
        <div class="rz-id"><div class="rz-name">${esc(r.name)}${r.withheld ? `<i class="fas fa-lock name-lock" title="Reveal contact to see the full name"></i>` : ""}</div>
          <div class="rz-sub">${esc([r.role, r.location].filter(Boolean).join("  ·  ")) || "Healthcare provider"}</div></div>
        <span class="rz-lock"><i class="fas fa-${r.withheld ? "user-secret" : "lock"}"></i> ${r.withheld ? "Name withheld" : "View only"}</span>
      </div>
      <div class="rz-ai" id="rz-ai-summary">
        <div class="rz-ai-head"><i class="fas fa-wand-magic-sparkles"></i>AI Summary</div>
        <div class="rz-ai-body"><span class="copilot-dots"><i></i><i></i><i></i></span> Generating a recruiter briefing…</div>
      </div>
      <div class="rz-tabs">${sections.map((t, i) => `<button class="rz-tab${i === 0 ? " active" : ""}" data-rz="${t[0]}">${t[1]}</button>`).join("")}</div>
      <div class="rz-doc">
        ${sections.map(t => `<section class="rz-sec" id="rz-${t[0]}"><h4>${t[1]}</h4>${t[2]}</section>`).join("")}
      </div>
    </div>`;
  }
  // Nav jumps to a section, and the highlight follows the section you scroll to.
  function wireResumeNav(){
    const scroller = $("#modal-root .modal-body");
    const tabs = $$("#modal-root .rz-tab");
    const sections = tabs.map(t => document.getElementById("rz-" + t.dataset.rz)).filter(Boolean);
    if (!scroller || !sections.length) return;
    const activate = id => tabs.forEach(t => t.classList.toggle("active", t.dataset.rz === id));
    tabs.forEach(t => t.onclick = () => {
      const target = document.getElementById("rz-" + t.dataset.rz);
      if (!target) return;
      activate(t.dataset.rz);
      target.scrollIntoView({behavior:"smooth", block:"start"});
    });
    let queued = false;
    scroller.addEventListener("scroll", () => {
      if (queued) return;
      queued = true;
      requestAnimationFrame(() => {
        queued = false;
        // At the end of the scroll the trailing sections are all on screen at once
        // and never cross the line below, so pin the highlight to the last one.
        if (scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 4) {
          activate(sections[sections.length - 1].id.replace(/^rz-/, ""));
          return;
        }
        // Otherwise: whichever section has passed just under the sticky nav.
        const line = scroller.getBoundingClientRect().top + 72;
        let current = sections[0];
        sections.forEach(s => { if (s.getBoundingClientRect().top <= line) current = s; });
        activate(current.id.replace(/^rz-/, ""));
      });
    }, {passive:true});
  }
  async function viewResume(id){
    $("#modal-root").innerHTML = `<div class="modal"><div class="modal-card resume-modal"><div class="modal-head"><strong>Résumé</strong><button class="icon-btn" data-close-modal><i class="fas fa-xmark"></i></button></div><div class="modal-body">${loading("Loading résumé...")}</div></div></div>`;
    try {
      // The server already withholds the name (and scrubs it from the body)
      // when the profile hasn't been released — `withheld` just tells the UI.
      const r = await get(`/api/profiles/${id}/resume`);
      $("#modal-root .modal-head strong").textContent = r.name ? `${r.name} — Résumé` : "Résumé";
      $("#modal-root .modal-body").innerHTML = renderResume(r);
      wireResumeNav();
      loadResumeSummary(id);
    } catch(e) { $("#modal-root .modal-body").innerHTML = `<div style="padding:24px">${esc(e.message || "Could not load résumé.")}</div>`; }
  }
  // Lazily generate the AI briefing so the résumé renders instantly and the
  // summary streams into its card a moment later.
  async function loadResumeSummary(id){
    const card = $("#rz-ai-summary"); if (!card) return;
    const body = card.querySelector(".rz-ai-body");
    try {
      const s = await get(`/api/profiles/${id}/summary`);
      if (s && s.summary){
        body.innerHTML = `<p class="rz-ai-text">${esc(s.summary)}</p>`
          + ((s.highlights && s.highlights.length)
              ? `<div class="rz-ai-chips">${s.highlights.map(h => `<span class="rz-ai-chip">${esc(h)}</span>`).join("")}</div>`
              : "");
      } else {
        card.remove();   // not enriched yet, or AI unavailable — hide the card
      }
    } catch(e) { card.remove(); }
  }
  async function releaseContact(id){
    const btn = Array.from(document.querySelectorAll("[data-release]"))
      .find(el => el.dataset.release === id);
    const original = btn ? btn.innerHTML : "";
    if (btn) {
      btn.disabled = true;
      btn.innerHTML = '<span class="spinner sm"></span>Revealing…';
    }
    try {
      const released = await post(`/api/profiles/${id}/contact-release`, {});
      S.releasedContacts.set(id, released);
      if (S.providerCards.has(id)) {
        S.providerCards.set(id, {...S.providerCards.get(id), ...released});
      }
      // The cached page still holds the masked row, and renderProviderPage
      // re-renders from it — merge the newly released name/contact in, or the
      // row would keep showing initials until the next fetch.
      if (S.providerLastData) {
        const items = S.providerLastData.items || [];
        const i = items.findIndex(x => x.profile_id === id);
        if (i >= 0) items[i] = {...items[i], ...released};
        renderProviderPage(S.providerLastData);
      }
      refreshAiCard(id);   // keep any AI Search result card in sync too
      if (released.credits_remaining != null) refreshCredits();
      return released;
    } catch(e) {
      if (btn) {
        btn.disabled = false;
        btn.innerHTML = original;
      }
      // 402 means the balance ran out — say so plainly rather than "failed".
      if (e.status === 402){
        alert(e.message);
        refreshCredits();
      } else {
        alert(e.message || "Could not release contact.");
      }
      return null;
    }
  }
  // --- AI Search (full-page natural-language candidate search) ------------
  // Results render in the SAME table format as the Providers directory, reusing
  // providerRow so columns, masking and the reveal control stay identical.
  const AI_TABLE_HEAD =
    "<tr><th class=\"th-check\"></th><th>Provider</th><th>License</th><th>Category</th><th>Experience</th>"
    + "<th>Location</th><th>Contact</th><th class=\"th-actions\"></th></tr>";
  function aiResultsTable(items){
    setTimeout(() => refreshPoolMembership(items.map(p => p.profile_id)), 0);
    return `<div class="table-wrap"><table class="table"><thead>${AI_TABLE_HEAD}</thead>`
      + `<tbody>${items.map(providerRow).join("")}</tbody></table></div>`;
  }
  // Re-render any AI result row for a profile whose contact was just revealed.
  function refreshAiCard(id){
    const p = S.providerCards.get(id);
    if (!p) return;
    $$(`#ai-thread tr[data-row="${id}"]`).forEach(row => {
      const tmp = document.createElement("tbody");
      tmp.innerHTML = providerRow(p);
      row.replaceWith(tmp.firstElementChild);
    });
  }
  async function aiSearch(message){
    message = (message || "").trim();
    if (!message || S.aiBusy) return;
    S.aiBusy = true;
    $("#page-ai").classList.add("has-results");   // collapse hero → compact top bar
    $("#ai-input").value = "";
    const thread = $("#ai-thread");
    const turn = document.createElement("div");
    turn.className = "ai-turn";
    turn.innerHTML = `<div class="ai-q">${esc(message)}</div>
      <div class="ai-answer"><i class="fas fa-wand-magic-sparkles ai-spark"></i>
        <div class="ai-answer-body">Searching<span class="copilot-dots"><i></i><i></i><i></i></span></div></div>`;
    thread.appendChild(turn);
    turn.scrollIntoView({behavior:"smooth", block:"start"});
    const body = turn.querySelector(".ai-answer-body");
    try {
      // Send the previous turn's filters so a follow-up refines instead of
      // restarting ("RN nurses" → then "only in California").
      const r = await post("/api/profiles/copilot", {message, context: S.aiContext || null});
      S.aiContext = r.filters || {};   // carry forward for the next refinement
      $("#ai-input").placeholder = "Refine your search — e.g. only in California, with 10+ years";
      body.textContent = r.answer || "Here's what I found.";
      const items = r.items || [];
      items.forEach(p => {
        S.providerCards.set(p.profile_id, p);
        if (p.is_released && !S.releasedContacts.has(p.profile_id))
          S.releasedContacts.set(p.profile_id, p);
      });
      if (items.length){
        const results = document.createElement("div");
        results.className = "ai-results";
        results.innerHTML = aiResultsTable(items);
        turn.appendChild(results);
        if ((r.total || 0) > items.length){
          const more = document.createElement("button");
          more.className = "ai-viewall";
          more.innerHTML = `<i class="fas fa-table-list"></i>View all ${r.total.toLocaleString()} in the Providers directory`;
          more.onclick = () => copilotApplyToDirectory(r.filters || {});
          turn.appendChild(more);
        }
      }
    } catch(e) {
      body.textContent = e.status === 403
        ? "AI Search is available to recruiter accounts."
        : "Sorry — I couldn't complete that search. Please try again.";
    } finally {
      S.aiBusy = false;
      $("#ai-input").focus();
    }
  }
  function aiNewSearch(){
    S.aiContext = null;
    $("#ai-thread").innerHTML = "";
    $("#page-ai").classList.remove("has-results");   // back to the centered hero
    $("#ai-input").value = "";
    $("#ai-input").placeholder = "e.g. ICU nurses in California with 10+ years of experience";
    $("#ai-input").focus();
  }
  function wireAi(){
    const form = $("#ai-form");
    if (!form) return;
    form.addEventListener("submit", e => { e.preventDefault(); aiSearch($("#ai-input").value); });
    $$("#ai-suggestions .ai-chip").forEach(c => c.onclick = () => aiSearch(c.textContent));
    $("#ai-new").onclick = aiNewSearch;
    const fab = $("#copilot-fab");
    if (fab) fab.onclick = () => showPage("ai");
  }
  // Push the AI-understood filters onto the real Providers directory, so results
  // render in the main table (full pagination + filter chips), not just cards.
  function applyCopilotFilters(f){
    f = f || {};
    S.provider = {
      q: f.q || "", category: f.category || "", license_title: f.license_title || "",
      zip: "", radius_mi: f.radius_mi != null ? String(f.radius_mi) : "25",
      state_code: f.state_code || "", city: f.city || "",
      min_experience: f.min_experience != null ? String(f.min_experience) : "",
      max_experience: f.max_experience != null ? String(f.max_experience) : "",
      contact_available: f.contact_available || "", compact: f.compact ? "true" : "",
      licensed_state: f.licensed_state || "", worked_at: f.worked_at || "",
      travel_experience: f.travel_experience ? "true" : "",
    };
  }
  // Mirror S.provider into the visible filter controls (called again once the
  // facet-populated <select>s exist, so their values actually stick).
  function syncProviderControls(){
    const s = S.provider, set = (sel, v) => { const el = $(sel); if (el) el.value = v || ""; };
    set("#provider-q", s.q);
    set("#provider-state", s.state_code);
    set("#provider-license-title", s.license_title);
    set("#provider-contact", s.contact_available);
    set("#provider-zip", s.zip);
    const exp = $("#provider-experience");
    if (exp){
      const mn = s.min_experience, mx = s.max_experience;
      exp.value = (mn === "0" && mx === "2") ? "0-2" : (mn === "3" && mx === "5") ? "3-5"
        : (mn === "6" && mx === "10") ? "6-10" : (mn === "10" && !mx) ? "10+" : "";
    }
    $$("#provider-tabs .tab").forEach(t =>
      t.classList.toggle("active", (t.dataset.category || "") === (s.category || "")));
    $$(".provider-toggles .ptoggle").forEach(t =>
      t.classList.toggle("active", !!s[t.dataset.toggle]));
  }
  function copilotApplyToDirectory(filters){
    applyCopilotFilters(filters);
    S.providerOffset = 0;
    syncProviderControls();
    showPage("providers");   // makes the page active + loads facets + loadProviders
    refreshCounts();         // headline + tab counts for these filters
  }

  // --- Get Extension page -------------------------------------------------
  async function loadExtensionPage(){
    try {
      const c = await get("/api/extension/connect");
      $("#ext-api-base").value = c.api_base || location.origin;
      $("#ext-token").value = c.capture_token || "";
      $("#ext-platforms").innerHTML = (c.platforms || []).map(p =>
        `<div class="ext-plat"><span>${esc(p.name)}</span>`
        + `<span class="ext-badge ${p.status === "live" ? "live" : "coming"}">`
        + `${p.status === "live" ? "Live" : "Coming soon"}</span></div>`).join("");
    } catch(e) { /* recruiter-only; ignore for others */ }
  }
  async function extDownload(){
    const btn = $("#ext-download"), orig = btn.innerHTML;
    btn.disabled = true; btn.innerHTML = '<span class="spinner sm"></span>Preparing…';
    try {
      const res = await fetch("/api/extension/download", {headers:{Authorization:"Bearer " + token()}});
      if (!res.ok) throw new Error("download failed");
      const url = URL.createObjectURL(await res.blob());
      const a = document.createElement("a");
      a.href = url; a.download = "healthboard-capture-extension.zip";
      document.body.appendChild(a); a.click(); a.remove(); URL.revokeObjectURL(url);
      $("#ext-download-hint").textContent = "Downloaded. Unzip it, then load it in your browser (step 2).";
    } catch(e) { $("#ext-download-hint").textContent = "Download failed — please try again."; }
    finally { btn.disabled = false; btn.innerHTML = orig; }
  }
  function wireExtension(){
    const dl = $("#ext-download");
    if (!dl) return;
    dl.onclick = extDownload;
    $("#ext-token-reveal").onclick = () => {
      const t = $("#ext-token"), show = t.type === "password";
      t.type = show ? "text" : "password";
      $("#ext-token-reveal").innerHTML = show ? '<i class="fas fa-eye-slash"></i>' : '<i class="fas fa-eye"></i>';
    };
    $("#ext-token-regen").onclick = async () => {
      if (!confirm("Regenerate your capture token? The current one will stop working.")) return;
      try { const r = await post("/api/extension/token", {}); $("#ext-token").value = r.capture_token; }
      catch(e) { alert("Could not regenerate the token."); }
    };
    $$("#page-extension [data-copy]").forEach(b => b.onclick = () => {
      const el = $("#" + b.dataset.copy); if (!el) return;
      if (navigator.clipboard) navigator.clipboard.writeText(el.value);
      const i = b.querySelector("i"), prev = i.className;
      i.className = "fas fa-check"; setTimeout(() => { i.className = prev; }, 1200);
    });
  }

  function wire(){
    $$(".auth-tab").forEach(b => b.onclick = () => showAuthMode(b.dataset.authMode));
    $("#auth-submit").onclick = submitAuth;
    $("#auth-password").addEventListener("keydown", e => { if (e.key === "Enter") submitAuth(); });
    $("#auth-email").addEventListener("keydown", e => { if (e.key === "Enter") $("#auth-password").focus(); });
    const pwt = $("#auth-pw-toggle");
    if (pwt) pwt.onclick = () => { const i = $("#auth-password"); const show = i.type === "password"; i.type = show ? "text" : "password"; pwt.innerHTML = show ? '<i class="fas fa-eye-slash"></i>' : '<i class="fas fa-eye"></i>'; };
    $$(".nav-item,.top-user,.top-actions .icon-btn,.credit-chip,.hero-band .btn,.logo").forEach(el => el.addEventListener("click", e => { const page = el.dataset.page; if (page) { e.preventDefault(); showPage(page); } }));
    $("#logout-btn").onclick = () => { setToken(""); setRefresh(""); location.reload(); };
    ["job-q","job-type","job-state"].forEach(id => { const el = $("#" + id); el.addEventListener(id === "job-q" ? "input" : "change", debounce(loadJobs, 250)); });
    $("#provider-q").addEventListener("input", debounce(() => { S.provider.q = $("#provider-q").value.trim(); providerFilterChanged(); }, 300));
    $("#provider-license-title").onchange = () => { S.provider.license_title = $("#provider-license-title").value; providerFilterChanged(); };
    const radiusEl = $("#provider-radius");
    const radiusControl = radiusEl.closest(".radius-control");
    const paintRadius = () => {
      const pct = ((radiusEl.value - radiusEl.min) / (radiusEl.max - radiusEl.min)) * 100;
      radiusEl.style.background = `linear-gradient(90deg, var(--accent) ${pct}%, var(--line) ${pct}%)`;
      const rv = $("#radius-value"); if (rv) rv.textContent = radiusEl.value;
      if (radiusControl) radiusControl.classList.toggle("is-disabled", radiusEl.disabled);
    };
    paintRadius();
    radiusEl.addEventListener("input", paintRadius);   // live label + fill while dragging
    radiusEl.addEventListener("change", () => {        // query only when the drag ends
      S.provider.radius_mi = radiusEl.value;
      if (S.provider.zip) providerFilterChanged();
    });
    $("#provider-zip").addEventListener("input", debounce(() => {
      const z = $("#provider-zip").value.replace(/\D/g, "").slice(0, 5);
      $("#provider-zip").value = z;
      const valid = z.length === 5;
      S.provider.zip = valid ? z : "";
      radiusEl.disabled = !valid;                      // radius only applies with a ZIP
      paintRadius();
      providerFilterChanged();
    }, 350));
    $("#provider-experience").onchange = () => { setExperienceFilter($("#provider-experience").value); providerFilterChanged(); };
    $("#provider-contact").onchange = () => { S.provider.contact_available = $("#provider-contact").value; providerFilterChanged(); };
    $("#provider-state").onchange = () => { S.provider.state_code = $("#provider-state").value; providerFilterChanged(); };
    $$("#provider-tabs .tab").forEach(t => t.onclick = () => { $$("#provider-tabs .tab").forEach(x => x.classList.remove("active")); t.classList.add("active"); S.provider.category = t.dataset.category; S.providerOffset = 0; loadProviders(); });
    $$(".provider-toggles .ptoggle").forEach(t => t.onclick = () => {
      const on = t.classList.toggle("active");
      S.provider[t.dataset.toggle] = on ? "true" : "";
      providerFilterChanged();
    });
    document.body.addEventListener("click", e => {
      const apply = e.target.closest("[data-apply]"); if (apply) applyJob(apply.dataset.apply);
      const resume = e.target.closest("[data-resume]"); if (resume) viewResume(resume.dataset.resume);
      const release = e.target.closest("[data-release]"); if (release) releaseContact(release.dataset.release);
      const pick = e.target.closest("[data-pick]");
      if (pick) togglePick(pick.dataset.pick, pick.checked);
      const source = e.target.closest("[data-source]"); if (source) sourceForJob(source.dataset.source);
      const save = e.target.closest("[data-pool-save]");
      if (save){ e.stopPropagation(); openPoolMenu(save, save.dataset.poolSave); }
      else if (!e.target.closest(".pool-menu")) closePoolMenu();
      if (e.target.closest("[data-close-modal]") || e.target.classList.contains("modal")) $("#modal-root").innerHTML = "";
    });
    const poolNew = $("#pool-new");
    if (poolNew) poolNew.onclick = createPool;
    const matchBack = $("#match-back");
    if (matchBack) matchBack.onclick = () => showPage("jobs");
    const bulkAll = $("#bulk-all");
    if (bulkAll) bulkAll.onchange = () => {
      // Capture the intent first: togglePick re-renders the bar, which rewrites
      // this checkbox's own state mid-loop and would flip the remaining rows.
      const on = bulkAll.checked;
      $$("#providers-grid .row-check").forEach(b => {
        b.checked = on;
        togglePick(b.dataset.pick, on);
      });
      bulkAll.checked = on;
    };
    const bulkPool = $("#bulk-to-pool");
    if (bulkPool) bulkPool.onclick = bulkAddToPool;
    const bulkClear = $("#bulk-clear");
    if (bulkClear) bulkClear.onclick = clearSelection;
    const pfEdit = $("#profile-edit");
    if (pfEdit) pfEdit.onclick = openProfileForm;
    const pfCancel = $("#profile-cancel");
    if (pfCancel) pfCancel.onclick = closeProfileForm;
    const pfForm = $("#profile-form");
    if (pfForm) pfForm.addEventListener("submit", saveProfile);
    const searchSave = $("#search-save");
    if (searchSave) searchSave.onclick = saveCurrentSearch;
    const jobNew = $("#job-new");
    if (jobNew) jobNew.onclick = postJob;
    const tmplNew = $("#tmpl-new");
    if (tmplNew) tmplNew.onclick = newTemplate;
    const campNew = $("#camp-new");
    if (campNew) campNew.onclick = newCampaign;
    wireMessages();
    $("#resume-drop").onclick = () => $("#resume-file").click();
    $("#resume-file").onchange = () => { if ($("#resume-file").files[0]) uploadResume($("#resume-file").files[0]); };
    wireAi();
    wireExtension();
  }
  function debounce(fn, ms){ let t; return () => { clearTimeout(t); t = setTimeout(fn, ms); }; }

  // --- Messaging -----------------------------------------------------------
  const del = p => api("DELETE", p);
  const ATS_STAGES = ["sourced","contacted","screening","submitted","hired","rejected"];

  function shortTime(iso){
    if (!iso) return "";
    const d = new Date(iso), now = new Date();
    const sameDay = d.toDateString() === now.toDateString();
    if (sameDay) return d.toLocaleTimeString([], {hour:"numeric", minute:"2-digit"});
    const days = Math.round((now - d) / 86400000);
    if (days < 7) return d.toLocaleDateString([], {weekday:"short"});
    return d.toLocaleDateString([], {month:"short", day:"numeric"});
  }
  function dayLabel(iso){
    const d = new Date(iso), now = new Date();
    if (d.toDateString() === now.toDateString()) return "Today";
    const y = new Date(now); y.setDate(y.getDate() - 1);
    if (d.toDateString() === y.toDateString()) return "Yesterday";
    return d.toLocaleDateString([], {month:"long", day:"numeric", year:"numeric"});
  }

  async function refreshUnreadBadge(){
    if (!token()) return;
    try {
      const d = await get("/api/messages/unread-count");
      const el = $("#nav-unread");
      if (!el) return;
      el.textContent = d.unread > 99 ? "99+" : String(d.unread || "");
      el.classList.toggle("hidden", !d.unread);
    } catch(e) { /* badge is cosmetic — never break the page for it */ }
  }

  function threadItem(t){
    const preview = t.last_message
      ? (t.last_sender_is_me ? "You: " : "") + t.last_message
      : "No messages yet";
    return `<button class="msg-thread${t.unread_count ? " unread" : ""}${S.activeThread === t.thread_id ? " active" : ""}" data-thread="${t.thread_id}">
      <span class="msg-avatar">${esc(t.other_initials || "?")}</span>
      <span class="msg-thread-body">
        <span class="msg-thread-top">
          <span class="msg-thread-name">${esc(t.other_name)}</span>
          <span class="msg-thread-time">${esc(shortTime(t.last_message_at || t.created_at))}</span>
        </span>
        <span class="msg-thread-preview">${esc(preview)}</span>
      </span>
      ${t.unread_count ? `<span class="msg-unread-dot">${t.unread_count}</span>` : ""}
    </button>`;
  }

  function renderThreads(){
    const box = $("#msg-threads");
    if (!box) return;
    const q = S.msgFilter.trim().toLowerCase();
    const list = q
      ? S.threads.filter(t => (t.other_name || "").toLowerCase().includes(q)
          || (t.last_message || "").toLowerCase().includes(q))
      : S.threads;
    if (!list.length){
      box.innerHTML = `<div class="msg-empty" style="padding:28px 16px">
        <i class="fas fa-inbox"></i><h3>${q ? "No matches" : "No conversations yet"}</h3>
        <p>${q ? "Try a different search." : "Messages from candidates and recruiters land here."}</p></div>`;
      return;
    }
    box.innerHTML = list.map(threadItem).join("");
    $$("#msg-threads .msg-thread").forEach(b =>
      b.onclick = () => openThread(b.dataset.thread));
  }

  async function loadThreads({quiet=false} = {}){
    const box = $("#msg-threads");
    // Sequence guard: a slow earlier fetch must not overwrite a newer render
    // with pre-send state (same pattern as S.providerReq in the directory).
    const seq = ++S.threadsReq;
    if (box && !quiet && !S.threads.length) box.innerHTML = loading("Loading conversations...");
    try {
      const data = await get("/api/messages/threads") || [];
      if (seq !== S.threadsReq) return;
      S.threads = data;
      renderThreads();
      const total = S.threads.reduce((n,t) => n + (t.unread_count || 0), 0);
      const sub = $("#messages-sub");
      if (sub) sub.textContent = S.threads.length
        ? `${S.threads.length} conversation${S.threads.length === 1 ? "" : "s"}${total ? ` · ${total} unread` : ""}`
        : "Conversations";
      refreshUnreadBadge();
    } catch(e) {
      if (box) box.innerHTML = loading("Could not load conversations.");
    }
  }

  function renderMessages(detail){
    const box = $("#msg-bubbles");
    if (!box) return;
    const me = S.user && S.user.user_id;
    let lastDay = "";
    const html = (detail.messages || []).map(m => {
      const mine = m.sender_id === me;
      const day = dayLabel(m.created_at);
      const sep = day !== lastDay ? `<div class="msg-day">${esc(day)}</div>` : "";
      lastDay = day;
      // Non-text messages (offers, interview slots) render as system notes.
      if (m.kind && m.kind !== "text"){
        return `${sep}<div class="msg-system"><i class="fas fa-circle-info"></i> ${esc(m.body || m.kind)}</div>`;
      }
      return `${sep}<div class="msg-row ${mine ? "me" : "them"}">
        <div><div class="msg-bubble">${esc(m.body || "")}</div>
        <div class="msg-meta">${esc(shortTime(m.created_at))}${mine && m.is_read ? " · Read" : ""}</div></div>
      </div>`;
    }).join("");
    box.innerHTML = html || `<div class="msg-system">No messages yet — say hello.</div>`;
    box.scrollTop = box.scrollHeight;
  }

  async function openThread(id, {quiet=false} = {}){
    S.activeThread = id;
    $("#msg-placeholder").classList.add("hidden");
    $("#msg-convo-inner").classList.remove("hidden");
    if (!quiet) $("#msg-bubbles").innerHTML = loading("Loading messages...");
    const seq = ++S.threadReq;
    try {
      const d = await get(`/api/messages/threads/${id}`);
      if (seq !== S.threadReq) return;   // superseded by a newer open/refresh
      $("#msg-head-name").textContent = d.other_name || "Unknown";
      $("#msg-head-avatar").textContent = d.other_initials || "?";
      $("#msg-head-role").textContent = (d.other_role || "").replace("_"," ");
      const sel = $("#msg-stage");
      sel.innerHTML = ATS_STAGES.map(s =>
        `<option value="${s}"${d.ats_stage === s ? " selected" : ""}>${s[0].toUpperCase()+s.slice(1)}</option>`).join("");
      renderMessages(d);
      // Opening marks inbound messages read, so refresh the list + badge.
      await loadThreads({quiet:true});
    } catch(e) {
      $("#msg-bubbles").innerHTML = loading("Could not load this conversation.");
    }
  }

  async function sendMessage(){
    const input = $("#msg-input");
    const body = (input.value || "").trim();
    if (!body || !S.activeThread) return;
    input.value = ""; input.style.height = "auto";
    const threadId = S.activeThread;
    try {
      await post(`/api/messages/threads/${threadId}/messages`, {body, kind:"text", payload:{}});
      // Reflect the sent message in the list straight away. Re-fetching races
      // with list requests already in flight, which would render pre-send state.
      const t = S.threads.find(x => x.thread_id === threadId);
      if (t){
        t.last_message = body;
        t.last_sender_is_me = true;
        t.last_message_at = new Date().toISOString();
        t.unread_count = 0;
        S.threads = [t, ...S.threads.filter(x => x !== t)];   // newest first
      }
      S.threadsReq++;    // discard any list fetch issued before this send
      renderThreads();
      await openThread(threadId, {quiet:true});
    } catch(e) {
      input.value = body;   // don't lose what they typed
      alert(e.message || "Could not send the message.");
    }
  }

  function startMessagePolling(){
    stopMessagePolling();
    // Light polling keeps the inbox live without a websocket layer.
    S.threadPoll = setInterval(() => {
      if (!document.hidden && $("#page-messages").classList.contains("active")){
        loadThreads({quiet:true});
        if (S.activeThread) openThread(S.activeThread, {quiet:true});
      }
    }, 15000);
  }
  function stopMessagePolling(){
    if (S.threadPoll){ clearInterval(S.threadPoll); S.threadPoll = null; }
  }

  function loadMessages(){
    loadThreads();
    startMessagePolling();
  }

  function wireMessages(){
    const form = $("#msg-composer");
    if (!form) return;
    form.addEventListener("submit", e => { e.preventDefault(); sendMessage(); });
    const input = $("#msg-input");
    input.addEventListener("keydown", e => {
      if (e.key === "Enter" && !e.shiftKey){ e.preventDefault(); sendMessage(); }
    });
    input.addEventListener("input", () => {
      input.style.height = "auto";
      input.style.height = Math.min(input.scrollHeight, 120) + "px";
    });
    const search = $("#msg-search");
    if (search) search.addEventListener("input", () => { S.msgFilter = search.value; renderThreads(); });
    const stage = $("#msg-stage");
    if (stage) stage.onchange = async () => {
      if (!S.activeThread) return;
      try { await patch(`/api/messages/threads/${S.activeThread}/ats`, {ats_stage: stage.value}); }
      catch(e) { alert(e.message); }
    };
  }

  // --- Credits -------------------------------------------------------------
  const ACTION_LABELS = {reveal_contact:"Reveal a candidate's contact"};
  async function refreshCredits(){
    if (!token()) return;
    // Credits pay for revealing candidate contacts — a recruiter-only action.
    // A healthcare professional has no use for a balance, so hide it entirely.
    if (!isRecruiter()){
      const chip = $("#credit-chip");
      if (chip) chip.classList.add("hidden");
      return;
    }
    try {
      const c = await get("/api/credits");
      S.credits = c;
      const chip = $("#credit-chip"), bal = $("#credit-balance");
      if (!chip || !bal) return;
      chip.classList.toggle("hidden", !c.enabled);
      bal.textContent = c.balance;
      chip.classList.toggle("low", c.balance <= 5);
      chip.title = c.balance <= 5
        ? `Only ${c.balance} credits left` : `${c.balance} credits remaining`;
    } catch(e) { /* the chip is informational — never block the app for it */ }
  }
  async function loadCredits(){
    const box = $("#credits-body");
    box.innerHTML = loading("Loading credits...");
    try {
      const [c, txns, usage] = await Promise.all([
        get("/api/credits"), get("/api/credits/transactions?limit=40"), get("/api/credits/usage")]);
      S.credits = c;
      $("#credits-sub").textContent = c.enabled
        ? `${c.lifetime_spent} spent of ${c.lifetime_granted} granted`
        : "Credits are currently switched off — nothing is being charged.";
      box.innerHTML = `
        <div class="credit-hero"><b>${c.balance}</b><span>credits remaining</span></div>
        <div class="an-section"><h2>What things cost</h2>
          <div class="pipe-card">${Object.entries(c.costs).map(([a,n]) =>
            `<div class="price-row"><span>${esc(ACTION_LABELS[a] || a)}</span>
             ${n ? `<b>${n} credit${n === 1 ? "" : "s"}</b>` : `<span class="price-free">free</span>`}</div>`).join("")}</div>
          <p class="merge-hint">One credit per candidate, charged when you reveal their contact. Revealing them again, emailing them and viewing their résumé are all free.</p>
        </div>
        <div class="an-section"><h2>Where they went</h2>
          ${usage.by_action.length
            ? `<div class="an-grid">${usage.by_action.map(u =>
                `<div class="an-stat"><b>${u.credits}</b><span>${esc(ACTION_LABELS[u.action] || u.action)}</span>
                 <small>${u.count} action${u.count === 1 ? "" : "s"}</small></div>`).join("")}</div>`
            : `<p class="muted" style="font-size:13px">Nothing spent yet.</p>`}
        </div>
        <div class="an-section"><h2>History</h2>
          ${txns.items.length ? `<div class="table-wrap"><table class="table">
            <thead><tr><th>When</th><th>What</th><th>Change</th><th>Balance</th></tr></thead>
            <tbody>${txns.items.map(t => `<tr>
              <td>${esc(shortTime(t.created_at))}</td>
              <td>${esc(ACTION_LABELS[t.action] || t.note || t.reason)}</td>
              <td class="txn-delta ${t.delta < 0 ? "neg" : "pos"}">${t.delta > 0 ? "+" : ""}${t.delta}</td>
              <td>${t.balance_after}</td></tr>`).join("")}</tbody></table></div>`
            : `<p class="muted" style="font-size:13px">No transactions yet.</p>`}
        </div>`;
    } catch(e) { box.innerHTML = loading("Could not load credits."); }
  }

  // --- Outreach ------------------------------------------------------------
  function campCard(c){
    return `<div class="camp-card" data-camp="${c.campaign_id}">
      <div class="camp-head">
        <h3>${esc(c.name)}</h3>
        <span class="status-pill ${c.status === "sent" ? "ok" : ""}">${esc(c.status)}</span>
        <span class="spacer"></span>
        ${c.status !== "sent" ? `<button class="btn small primary" data-send="${c.campaign_id}"><i class="fas fa-paper-plane"></i>Send</button>` : ""}
      </div>
      <p class="camp-subject">${esc(c.subject)}</p>
      <div class="camp-stats">
        <span><b>${c.total}</b>recipients</span>
        <span><b>${c.sent}</b>sent</span>
        <span><b>${c.skipped}</b>skipped</span>
        <span><b>${c.opened}</b>opened</span>
        <span><b>${c.open_rate}%</b>open rate</span>
        <span><b>${c.replied}</b>replied</span>
      </div>
    </div>`;
  }
  async function loadOutreach(){
    const box = $("#outreach-body"), banner = $("#outreach-banner");
    box.innerHTML = loading("Loading outreach...");
    try {
      const [camps, tmpls] = await Promise.all([
        get("/api/outreach/campaigns"), get("/api/outreach/templates")]);
      S.templates = tmpls.items || [];
      banner.innerHTML = camps.email_configured ? "" : `<div class="warn-banner">
        <i class="fas fa-triangle-exclamation"></i>
        <div><b>No mail provider connected.</b> Campaigns run end to end and record
        every send, but nothing is delivered. Set <code>email_enabled</code> and
        <code>sendgrid_api_key</code> (plus a verified sending domain) to send for real.</div></div>`;
      $("#outreach-sub").textContent = `${camps.items.length} campaign${camps.items.length === 1 ? "" : "s"} · ${S.templates.length} template${S.templates.length === 1 ? "" : "s"}`;
      box.innerHTML = `
        <div class="an-section"><h2>Templates</h2>
          ${S.templates.length
            ? S.templates.map(t => `<span class="tmpl-chip"><b>${esc(t.name)}</b>
                <button class="drop" data-tmpl-del="${t.template_id}" title="Delete"><i class="fas fa-xmark"></i></button></span>`).join("")
            : `<p class="muted" style="font-size:13px">No templates yet.</p>`}
          <div class="merge-hint">Merge fields: ${(tmpls.merge_fields || []).map(f => `<code>{{${f}}}</code>`).join(" ")}</div>
        </div>
        <div class="an-section"><h2>Campaigns</h2>
          ${camps.items.length ? camps.items.map(campCard).join("")
            : `<div class="match-empty"><i class="fas fa-paper-plane"></i><h3>No campaigns yet</h3>
               <p>Create a template, then run a campaign against one of your talent pools.</p></div>`}
        </div>`;
      $$("#outreach-body [data-send]").forEach(b => b.onclick = e => { e.stopPropagation(); sendCampaign(b.dataset.send); });
      $$("#outreach-body [data-tmpl-del]").forEach(b => b.onclick = async e => {
        e.stopPropagation();
        try { await del(`/api/outreach/templates/${b.dataset.tmplDel}`); loadOutreach(); }
        catch(err) { alert(err.message); }
      });
      $$("#outreach-body .camp-card").forEach(c => c.onclick = () => openCampaign(c.dataset.camp));
    } catch(e) { box.innerHTML = loading("Could not load outreach."); }
  }
  async function newTemplate(){
    const name = prompt("Template name (e.g. \"ICU intro\"):", "");
    if (!name || !name.trim()) return;
    const subject = prompt("Subject line — merge fields allowed, e.g. {{first_name}}:",
                           "{{specialty}} roles near {{city}}");
    if (!subject) return;
    const body = prompt("Message body:",
      "Hi {{first_name}},\n\nI'm recruiting for {{specialty}} roles near {{city}}. "
      + "With your {{years_experience}} years of experience I thought it might be a fit.\n\n"
      + "Would you be open to a quick chat?\n\nBest regards");
    if (!body) return;
    try { await post("/api/outreach/templates", {name:name.trim(), subject, body}); loadOutreach(); }
    catch(e) { alert(e.status === 409 ? "You already have a template with that name." : e.message); }
  }
  async function newCampaign(){
    if (!S.pools.length){
      try { S.pools = (await get("/api/pools")).items || []; } catch(e) { S.pools = []; }
    }
    if (!S.pools.length) return alert("Create a talent pool first — a campaign sends to a pool.");
    if (!S.templates.length) return alert("Create a template first.");
    const pl = prompt(`Send to which pool?\n\n${S.pools.map((p,i) => `${i+1}. ${p.name} (${p.member_count})`).join("\n")}\n\nEnter a number:`, "1");
    const pool = S.pools[parseInt(pl, 10) - 1];
    if (!pool) return;
    const tl = prompt(`Use which template?\n\n${S.templates.map((t,i) => `${i+1}. ${t.name}`).join("\n")}\n\nEnter a number:`, "1");
    const tmpl = S.templates[parseInt(tl, 10) - 1];
    if (!tmpl) return;
    const name = prompt("Campaign name:", `${pool.name} — ${tmpl.name}`);
    if (!name || !name.trim()) return;
    try {
      const r = await post("/api/outreach/campaigns",
        {name:name.trim(), pool_id: pool.pool_id, template_id: tmpl.template_id});
      alert(`Campaign built.\n${r.ready_to_send} ready to send, ${r.skipped} skipped.\n\n`
            + `Skipped candidates have no released contact, no email, or opted out.`);
      loadOutreach();
    } catch(e) { alert(e.message); }
  }
  async function sendCampaign(id){
    if (!confirm("Send this campaign now?")) return;
    try {
      const r = await post(`/api/outreach/campaigns/${id}/send`, {});
      alert(`Sent ${r.sent}, skipped ${r.skipped}, failed ${r.failed}.`
            + (r.note ? `\n\n${r.note}` : ""));
      loadOutreach();
    } catch(e) { alert(e.message); }
  }
  async function openCampaign(id){
    try {
      const d = await get(`/api/outreach/campaigns/${id}`);
      const reasons = Object.entries(d.skip_reasons || {})
        .map(([r,n]) => `${n} ${r}`).join(" · ") || "none";
      alert(`${d.name}\n\nSubject: ${d.subject}\n\n`
            + `${d.total} recipients · ${d.sent} sent · ${d.opened} opened · ${d.replied} replied\n`
            + `Skipped: ${reasons}`);
    } catch(e) { alert(e.message); }
  }

  // --- Analytics -----------------------------------------------------------
  const STAGE_COLORS = {sourced:"#94a3b8", contacted:"#0ea5e9", screening:"#6366f1",
                        submitted:"#8b5cf6", hired:"#059669", rejected:"#e11d48"};
  function stat(value, label, sub, accent){
    return `<div class="an-stat"><b${accent ? ' class="accent"' : ""}>${esc(value)}</b>
      <span>${esc(label)}</span>${sub ? `<small>${esc(sub)}</small>` : ""}</div>`;
  }
  async function loadAnalytics(){
    const box = $("#analytics-panel");
    box.innerHTML = loading("Loading your activity...");
    try {
      const d = await get("/api/analytics/sourcing?days=30");
      const dir = d.directory, pools = d.pools, runs = d.sourcing_runs, msg = d.messaging;
      const stages = pools.by_stage || {};
      const totalStaged = Object.values(stages).reduce((a,b) => a+b, 0);
      const bar = totalStaged
        ? `<div class="an-bar">${Object.entries(stages).map(([s,n]) =>
             `<i style="width:${(100*n/totalStaged).toFixed(1)}%;background:${STAGE_COLORS[s] || "#94a3b8"}"></i>`).join("")}</div>
           <div class="an-legend">${Object.entries(stages).map(([s,n]) =>
             `<span><i class="an-dot" style="background:${STAGE_COLORS[s] || "#94a3b8"}"></i>${esc(s)} ${n}</span>`).join("")}</div>`
        : `<p class="muted" style="font-size:12.5px">No candidates shortlisted yet.</p>`;
      box.innerHTML = `
        <div class="an-section"><h2>Directory reach</h2><div class="an-grid">
          ${stat(dir.listable.toLocaleString(), "Listable providers", "after screening")}
          ${stat(dir.reachable.toLocaleString(), "Reachable", "have an email or phone", true)}
          ${stat(dir.reachable_pct + "%", "Reachable share", "of the listed directory")}
        </div></div>
        <div class="an-section"><h2>Your sourcing</h2><div class="an-grid">
          ${stat(d.contacts.released_total, "Contacts released", `${d.contacts.released_recent} in ${d.window_days} days`, true)}
          ${stat(pools.pools, "Talent pools")}
          ${stat(pools.shortlisted, "Candidates shortlisted")}
          ${stat(pools.worked_pct + "%", "Shortlist worked", `${pools.worked} moved past sourced`)}
          ${stat(runs.runs, "Sourcing runs")}
          ${stat(runs.candidates_ranked.toLocaleString(), "Candidates ranked", `avg score ${runs.avg_match_score}`)}
        </div></div>
        <div class="an-section"><h2>Pipeline</h2>${bar}</div>
        <div class="an-section"><h2>Outreach</h2><div class="an-grid">
          ${stat(msg.threads, "Conversations")}
          ${stat(msg.sent, "Messages sent")}
          ${stat(msg.received, "Messages received")}
          ${stat(d.saved_searches, "Saved searches")}
          ${stat(d.notifications, "Notifications")}
        </div></div>`;
      $("#analytics-sub").textContent = `Last ${d.window_days} days · ${dir.listable.toLocaleString()} providers listed`;
    } catch(e) { box.innerHTML = loading("Could not load analytics."); }
  }

  // --- Saved searches ------------------------------------------------------
  // The saved params are exactly the directory filter state, so applying one is
  // just restoring S.provider and re-querying.
  function currentSearchParams(){
    const s = S.provider, out = {};
    Object.entries(s).forEach(([k, v]) => { if (v !== "" && v != null) out[k] = v; });
    if (!(out.radius_mi && (out.zip || out.city))) { delete out.radius_mi; delete out.zip; }
    return out;
  }
  function describeSearch(p){
    const bits = [p.q, p.license_title, p.specialty, p.state_code, p.city,
                  p.compact ? "compact" : "", p.travel_experience ? "travel" : ""];
    return bits.filter(Boolean).join(" · ") || "all providers";
  }
  function renderSavedChips(){
    const box = $("#saved-chips"); if (!box) return;
    box.innerHTML = (S.searches || []).map(s => `
      <span class="saved-chip" data-search="${s.search_id}" title="${esc(describeSearch(s.params || {}))}">
        <b>${esc(s.name)}</b>
        ${s.new_matches ? `<span class="new">+${s.new_matches}</span>` : ""}
        <button class="drop" data-search-del="${s.search_id}" title="Delete"><i class="fas fa-xmark"></i></button>
      </span>`).join("");
    $$("#saved-chips .saved-chip").forEach(c => c.onclick = e => {
      if (e.target.closest("[data-search-del]")) return;
      applySavedSearch(c.dataset.search);
    });
    $$("#saved-chips [data-search-del]").forEach(b => b.onclick = async e => {
      e.stopPropagation();
      try { await del(`/api/saved-searches/${b.dataset.searchDel}`); loadSavedSearches(); }
      catch(err) { alert(err.message); }
    });
  }
  async function loadSavedSearches(){
    if (!isRecruiter()) return;
    try {
      const d = await get("/api/saved-searches");
      S.searches = d.items || [];
      renderSavedChips();
    } catch(e) { /* the directory still works without them */ }
  }
  function applySavedSearch(id){
    const s = (S.searches || []).find(x => x.search_id === id);
    if (!s) return;
    Object.keys(S.provider).forEach(k => { S.provider[k] = ""; });
    S.provider.radius_mi = "25";
    Object.entries(s.params || {}).forEach(([k, v]) => {
      if (k in S.provider) S.provider[k] = typeof v === "boolean" ? (v ? "true" : "") : String(v);
    });
    S.providerOffset = 0;
    syncProviderControls();
    loadProviders();
    refreshCounts();
    s.new_matches = 0;              // acknowledged by opening it
    renderSavedChips();
  }
  async function saveCurrentSearch(){
    const name = prompt("Name this search (e.g. \"ICU RNs in Texas\"):", "");
    if (!name || !name.trim()) return;
    try {
      await post("/api/saved-searches", {name: name.trim(), params: currentSearchParams(), notify: true});
      loadSavedSearches();
    } catch(e) { alert(e.status === 409 ? "You already have a search with that name." : e.message); }
  }
  // Re-count every saved search; growth since the last check becomes a
  // notification. This is what finally makes the Notifications page live.
  async function checkSavedSearches(){
    if (!isRecruiter()) return;
    try {
      const d = await post("/api/saved-searches/check", {});
      const byId = {};
      (d.results || []).forEach(r => { byId[r.search_id] = r.new; });
      (S.searches || []).forEach(s => { s.new_matches = byId[s.search_id] || 0; });
      renderSavedChips();
      if (d.notifications_created) refreshNotificationBadge();
    } catch(e) { /* non-critical */ }
  }
  async function refreshNotificationBadge(){
    try {
      const list = await get("/api/notifications");
      const n = (list || []).filter(x => !x.is_read).length;
      const el = $("#nav-notif");
      if (el){ el.textContent = n > 99 ? "99+" : String(n || ""); el.classList.toggle("hidden", !n); }
    } catch(e) {}
  }

  // --- Bulk selection ------------------------------------------------------
  function renderBulkBar(){
    const bar = $("#bulk-bar"); if (!bar) return;
    const n = S.selected.size;
    bar.classList.toggle("hidden", n === 0);
    const label = $("#bulk-count");
    if (label) label.textContent = `${n} selected`;
    // "select all" reflects only the rows currently on screen.
    const boxes = $$("#providers-grid .row-check");
    const all = $("#bulk-all");
    if (all) all.checked = boxes.length > 0 && boxes.every(b => b.checked);
  }
  function togglePick(id, on){
    if (on) S.selected.add(id); else S.selected.delete(id);
    const row = document.querySelector(`tr[data-row="${id}"]`);
    if (row) row.classList.toggle("is-selected", on);
    renderBulkBar();
  }
  function clearSelection(){
    S.selected.clear();
    $$(".row-check").forEach(b => { b.checked = false; });
    $$("tr.is-selected").forEach(r => r.classList.remove("is-selected"));
    renderBulkBar();
  }
  async function bulkAddToPool(){
    const ids = [...S.selected];
    if (!ids.length) return;
    await addMatchesToPool(ids);      // same pool picker the sourcing page uses
    clearSelection();
  }

  // --- Candidate matching (sourcing for a req) -----------------------------
  function scoreCell(c){
    const s = c.score_total, cls = s >= 85 ? "hi" : s < 70 ? "low" : "";
    const b = c.score_breakdown || {};
    return `<div class="score-cell">
        <span class="score-num">${s.toFixed(0)}</span>
        <span class="score-bar"><i class="${cls}" style="width:${Math.max(0, Math.min(100, s))}%"></i></span>
      </div>
      <div class="score-parts">
        <span class="score-part">skills ${Math.round(b.skills || 0)}</span>
        <span class="score-part">exp ${Math.round(b.experience || 0)}</span>
        <span class="score-part">loc ${Math.round(b.location || 0)}</span>
        <span class="score-part">pay ${Math.round(b.pay || 0)}</span>
      </div>`;
  }
  function matchRow(c){
    // Reuse the directory's card shape so Résumé / Reveal / Save all work here.
    S.providerCards.set(c.profile_id, {
      profile_id:c.profile_id, masked_name:c.name, initials:c.initials,
      is_released:c.is_released, specialty:c.specialty, profession_type:c.title,
      city:c.city, state_code:c.state_code, years_experience:c.years_experience,
      has_email:true, has_phone:true
    });
    const loc = c.location || "—";
    return `<tr data-row="${c.profile_id}">
      <td><span class="match-rank">#${c.rank}</span></td>
      <td>
        <div class="cell-user">
          <span class="avatar">${esc(c.initials || "?")}</span>
          <div class="cell-id">
            <div class="cell-name">${esc(c.name)}${c.is_released ? "" : `<i class="fas fa-lock name-lock" title="Reveal contact to see the full name"></i>`}</div>
            <div class="cell-sub">${esc(c.specialty || c.title || "Provider")}</div>
          </div>
        </div>
      </td>
      <td>${scoreCell(c)}</td>
      <td><div class="match-reason">${esc(c.match_reason || "")}</div></td>
      <td>${c.years_experience ? `${esc(c.years_experience)} yrs` : `<span class="cell-none">—</span>`}</td>
      <td>${esc(loc)}</td>
      <td class="td-actions">
        <button class="btn small" data-resume="${c.profile_id}" title="View résumé"><i class="fas fa-file-lines"></i></button>
        <button class="btn small${(S.poolMembership.get(c.profile_id) || []).length ? " saved" : ""}" data-pool-save="${c.profile_id}" title="Save to a talent pool"><i class="fas fa-layer-group"></i></button>
      </td>
    </tr>`;
  }

  async function sourceForJob(jobId){
    const job = (S.jobsById && S.jobsById.get(jobId)) || null;
    S.matchJobId = jobId;
    showPage("matching");
    $("#match-title").textContent = job ? job.title : "Sourcing";
    $("#match-sub").textContent = job
      ? [job.city, job.state_code].filter(Boolean).join(", ") || "Ranked candidates for this role"
      : "Ranked candidates for this role";
    const box = $("#match-body");
    box.innerHTML = loading("Scoring candidates against this role...");
    try {
      const r = await post("/api/matching/run", {job_id: jobId, top_n: 50});
      S.matchRun = r;
      renderMatches(r);
    } catch(e) {
      box.innerHTML = `<div class="match-empty"><i class="fas fa-triangle-exclamation"></i>
        <h3>Could not score this role</h3><p>${esc(e.message || "")}</p></div>`;
    }
  }

  function renderMatches(r){
    const box = $("#match-body"), s = r.summary || {}, list = r.candidates || [];
    if (!list.length){
      box.innerHTML = `<div class="match-empty"><i class="fas fa-user-slash"></i>
        <h3>No candidates matched</h3>
        <p>This role has no license or specialty on file yet, or no provider in the
           database fits it. Try a role with a clearer title.</p></div>`;
      return;
    }
    box.innerHTML = `
      <div class="match-summary">
        <div class="match-stat tier-hi"><b>${s.excellent_90plus || 0}</b><span>Excellent 90+</span></div>
        <div class="match-stat tier-mid"><b>${s.great_80_89 || 0}</b><span>Great 80-89</span></div>
        <div class="match-stat tier-low"><b>${s.good_70_79 || 0}</b><span>Good 70-79</span></div>
        <div class="match-stat"><b>${(s.avg_score || 0).toFixed(1)}</b><span>Avg score</span></div>
        <div class="match-stat"><b>${s.total || 0}</b><span>Ranked</span></div>
      </div>
      <div class="match-actions">
        <button class="btn primary" id="match-to-pool"><i class="fas fa-layer-group"></i>Add top 20 to a pool</button>
        <span class="match-spec">Scored on <b>skills</b>, <b>experience</b>, <b>location</b> and <b>pay</b>.</span>
      </div>
      <div class="table-wrap"><table class="table">
        <thead><tr><th></th><th>Candidate</th><th>Match score</th><th>Why</th><th>Experience</th><th>Location</th><th class="th-actions"></th></tr></thead>
        <tbody>${list.map(matchRow).join("")}</tbody>
      </table></div>`;
    $("#match-to-pool").onclick = () => addMatchesToPool(list.slice(0, 20).map(c => c.profile_id));
    refreshPoolMembership(list.map(c => c.profile_id));
  }

  async function addMatchesToPool(profileIds){
    if (!profileIds.length) return;
    if (!S.pools.length){
      try { S.pools = (await get("/api/pools")).items || []; } catch(e) { S.pools = []; }
    }
    const names = S.pools.map((p,i) => `${i+1}. ${p.name}`).join("\n");
    const answer = prompt(
      `Add ${profileIds.length} candidates to which pool?\n\n${names || "(no pools yet)"}\n\n` +
      `Enter a number, or type a new pool name:`, S.pools.length ? "1" : "");
    if (answer === null || !answer.trim()) return;
    try {
      let pool = S.pools[parseInt(answer, 10) - 1];
      if (!pool){
        pool = await post("/api/pools", {name: answer.trim(), color:"blue"});
        S.pools.unshift(pool);
      }
      const res = await post(`/api/pools/${pool.pool_id}/members`, {profile_ids: profileIds});
      profileIds.forEach(pid => S.poolMembership.set(pid,
        [...new Set([...(S.poolMembership.get(pid) || []), pool.pool_id])]));
      paintPoolButtons();
      alert(`Added ${res.added} candidate${res.added === 1 ? "" : "s"} to "${pool.name}"`
            + (res.skipped ? ` (${res.skipped} already there).` : "."));
    } catch(e) { alert(e.status === 409 ? "You already have a pool with that name." : e.message); }
  }

  // --- Talent pools --------------------------------------------------------
  function poolCard(p){
    const stages = Object.entries(p.stages || {})
      .map(([s,n]) => `<span class="pool-stage-chip">${esc(s)} ${esc(n)}</span>`).join("");
    return `<div class="pool-card" data-pool="${p.pool_id}">
      <button class="pool-del" data-pool-del="${p.pool_id}" title="Delete pool"><i class="fas fa-trash"></i></button>
      <h3>${esc(p.name)}</h3>
      <p class="pool-desc">${esc(p.description || "No description")}</p>
      <div class="pool-count">${esc(p.member_count)}<small>candidate${p.member_count === 1 ? "" : "s"}</small></div>
      <div class="pool-stages">${stages || `<span class="pool-stage-chip">empty</span>`}</div>
    </div>`;
  }

  async function loadPools(){
    const box = $("#pools-body");
    if (!box) return;
    S.activePool = null;
    const seq = ++S.poolsReq;
    box.innerHTML = loading("Loading talent pools...");
    try {
      const d = await get("/api/pools");
      if (seq !== S.poolsReq) return;   // a newer load already rendered
      S.pools = d.items || [];
      $("#pools-sub").textContent = S.pools.length
        ? `${S.pools.length} pool${S.pools.length === 1 ? "" : "s"} · ${S.pools.reduce((n,p) => n + p.member_count, 0)} candidates shortlisted`
        : "Shortlist candidates, track them through your pipeline, and export for outreach.";
      box.innerHTML = S.pools.length
        ? `<div class="pool-grid">${S.pools.map(poolCard).join("")}</div>`
        : `<div class="msg-empty"><i class="fas fa-layer-group"></i><h3>No talent pools yet</h3>
           <p>Create a pool, then save candidates to it from the Providers directory.</p></div>`;
      $$("#pools-body .pool-card").forEach(c => c.onclick = e => {
        if (e.target.closest("[data-pool-del]")) return;
        openPool(c.dataset.pool);
      });
      $$("#pools-body [data-pool-del]").forEach(b => b.onclick = async e => {
        e.stopPropagation();
        const pool = S.pools.find(p => p.pool_id === b.dataset.poolDel);
        if (!confirm(`Delete "${pool ? pool.name : "this pool"}"? The candidates stay in the directory.`)) return;
        try { await del(`/api/pools/${b.dataset.poolDel}`); loadPools(); }
        catch(err) { alert(err.message); }
      });
    } catch(e) { box.innerHTML = loading("Could not load talent pools."); }
  }

  function poolMemberRow(m){
    const loc = providerLocation(m);
    return `<tr data-row="${m.profile_id}">
      <td>
        <div class="cell-user">
          <span class="avatar">${esc(m.initials || "?")}</span>
          <div class="cell-id">
            <div class="cell-name">${esc(displayName(m))}</div>
            <div class="cell-sub">${esc(providerSubtitle(m))}</div>
          </div>
        </div>
      </td>
      <td><span class="badge accent">${esc(short(m.profession_type || "Pro", 24))}</span></td>
      <td>${loc ? esc(loc) : `<span class="cell-none">—</span>`}</td>
      <td>
        <select class="stage-select" data-stage-for="${m.profile_id}">
          ${ATS_STAGES.map(s => `<option value="${s}"${m.stage === s ? " selected" : ""}>${s[0].toUpperCase()+s.slice(1)}</option>`).join("")}
        </select>
      </td>
      <td>
        <span class="pool-note">${esc(m.note || "—")}</span>
        <button class="pool-note-edit" data-note-for="${m.profile_id}" title="Edit note"><i class="fas fa-pen"></i></button>
      </td>
      <td class="td-actions">
        <button class="btn small" data-resume="${m.profile_id}" title="View résumé"><i class="fas fa-file-lines"></i></button>
        <button class="btn small" data-pool-remove="${m.profile_id}" title="Remove from pool"><i class="fas fa-xmark"></i></button>
      </td>
    </tr>`;
  }

  async function openPool(poolId, stage=""){
    S.activePool = poolId; S.poolStage = stage;
    const box = $("#pools-body");
    box.innerHTML = loading("Loading pool...");
    try {
      const d = await get(`/api/pools/${poolId}/members${stage ? `?stage=${stage}` : ""}`);
      const p = d.pool;
      const counts = p.stages || {};
      const tabs = [["", `All ${p.member_count}`]].concat(
        ATS_STAGES.map(s => [s, `${s[0].toUpperCase()+s.slice(1)} ${counts[s] || 0}`]));
      box.innerHTML = `
        <div class="pool-detail-head">
          <button class="btn ghost" id="pool-back"><i class="fas fa-arrow-left"></i>All pools</button>
          <h2>${esc(p.name)}</h2>
          <div class="spacer"></div>
          <a class="btn" id="pool-export" href="#"><i class="fas fa-file-csv"></i>Export CSV</a>
        </div>
        <div class="pool-stage-tabs">
          ${tabs.map(([v,label]) => `<button class="pool-stage-tab${S.poolStage === v ? " active" : ""}" data-pstage="${v}">${esc(label)}</button>`).join("")}
        </div>
        <div class="table-wrap"><table class="table">
          <thead><tr><th>Candidate</th><th>License</th><th>Location</th><th>Stage</th><th>Note</th><th></th></tr></thead>
          <tbody>${(d.items || []).map(poolMemberRow).join("")
            || `<tr class="row-state"><td colspan="6">${loading(stage ? "No candidates at this stage." : "No candidates in this pool yet — add them from the Providers directory.")}</td></tr>`}</tbody>
        </table></div>`;
      $("#pool-back").onclick = loadPools;
      $("#pool-export").onclick = e => { e.preventDefault(); exportPool(poolId); };
      $$("#pools-body [data-pstage]").forEach(b => b.onclick = () => openPool(poolId, b.dataset.pstage));
      $$("#pools-body [data-stage-for]").forEach(sel => sel.onchange = async () => {
        try { await patch(`/api/pools/${poolId}/members/${sel.dataset.stageFor}`, {stage: sel.value}); openPool(poolId, S.poolStage); }
        catch(err) { alert(err.message); }
      });
      $$("#pools-body [data-note-for]").forEach(b => b.onclick = async () => {
        const row = (d.items || []).find(x => x.profile_id === b.dataset.noteFor);
        const note = prompt("Note about this candidate:", (row && row.note) || "");
        if (note === null) return;
        try { await patch(`/api/pools/${poolId}/members/${b.dataset.noteFor}`, {note}); openPool(poolId, S.poolStage); }
        catch(err) { alert(err.message); }
      });
      $$("#pools-body [data-pool-remove]").forEach(b => b.onclick = async () => {
        try { await del(`/api/pools/${poolId}/members/${b.dataset.poolRemove}`); openPool(poolId, S.poolStage); }
        catch(err) { alert(err.message); }
      });
      // [data-resume] is handled by the global click listener in wire().
    } catch(e) {
      console.error("openPool failed", e);
      box.innerHTML = loading("Could not load this pool.");
    }
  }

  async function exportPool(poolId){
    // Authenticated download: fetch with the bearer token, then save the blob.
    try {
      const res = await fetch(`/api/pools/${poolId}/export.csv`, {headers:{Authorization:"Bearer " + token()}});
      if (!res.ok) throw new Error("Export failed");
      const blob = await res.blob();
      const pool = S.pools.find(p => p.pool_id === poolId);
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = `${(pool ? pool.name : "pool").replace(/[^\w-]+/g,"-")}.csv`;
      document.body.appendChild(a); a.click();
      setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 0);
    } catch(e) { alert("Could not export this pool."); }
  }

  async function createPool(){
    const name = prompt("Name this pool (e.g. \"ICU travel RNs — Q3\"):", "");
    if (!name || !name.trim()) return;
    try {
      await post("/api/pools", {name:name.trim(), color:"blue"});
      loadPools();
    } catch(e) { alert(e.status === 409 ? "You already have a pool with that name." : e.message); }
  }

  // Repaint the Save/Saved pills from local state — no network, so an
  // optimistic save shows instantly and never waits on a round trip.
  function paintPoolButtons(){
    $$("[data-pool-save]").forEach(b => {
      const on = (S.poolMembership.get(b.dataset.poolSave) || []).length > 0;
      b.classList.toggle("saved", on);
      b.innerHTML = `<i class="fas fa-layer-group"></i>${on ? "Saved" : "Save"}`;
    });
  }
  // Which of the visible candidates are already in a pool (drives the Saved pill).
  async function refreshPoolMembership(profileIds){
    if (!isRecruiter() || !profileIds.length) return;
    try {
      const map = await post("/api/pools/membership", {profile_ids: profileIds});
      // Only trust the server for the profiles we asked about, so a pending
      // optimistic save for another row is never clobbered.
      profileIds.forEach(pid => S.poolMembership.set(pid, (map && map[pid]) || []));
      paintPoolButtons();
    } catch(e) { /* non-critical */ }
  }

  function closePoolMenu(){
    const m = $(".pool-menu");
    if (m) m.remove();
  }

  async function openPoolMenu(btn, profileId){
    closePoolMenu();
    if (!S.pools.length){
      try { S.pools = (await get("/api/pools")).items || []; } catch(e) { S.pools = []; }
    }
    const inPools = new Set(S.poolMembership.get(profileId) || []);
    const menu = document.createElement("div");
    menu.className = "pool-menu";
    menu.innerHTML = `<div class="pool-menu-head">Save to pool</div>
      ${S.pools.map(p => `<button data-add="${p.pool_id}" class="${inPools.has(p.pool_id) ? "in-pool" : ""}">
          <i class="fas ${inPools.has(p.pool_id) ? "fa-check" : "fa-plus"}"></i>${esc(p.name)}
        </button>`).join("") || `<div class="pool-menu-head" style="font-weight:400;text-transform:none">No pools yet.</div>`}
      <div class="sep"></div>
      <button data-new-pool="1"><i class="fas fa-folder-plus"></i>New pool…</button>`;
    document.body.appendChild(menu);
    const r = btn.getBoundingClientRect();
    menu.style.top = `${Math.min(r.bottom + 6 + window.scrollY, window.scrollY + innerHeight - menu.offsetHeight - 10)}px`;
    menu.style.left = `${Math.min(r.left + window.scrollX, innerWidth - menu.offsetWidth - 12)}px`;

    menu.querySelectorAll("[data-add]").forEach(b => b.onclick = async () => {
      const poolId = b.dataset.add;
      closePoolMenu();
      const before = S.poolMembership.get(profileId) || [];
      const removing = inPools.has(poolId);
      // Paint first, reconcile after: the pill responds on click, not on latency.
      S.poolMembership.set(profileId, removing
        ? before.filter(x => x !== poolId) : [...before, poolId]);
      paintPoolButtons();
      try {
        if (removing) await del(`/api/pools/${poolId}/members/${profileId}`);
        else await post(`/api/pools/${poolId}/members`, {profile_id: profileId});
      } catch(e) {
        S.poolMembership.set(profileId, before);   // roll back the optimistic paint
        paintPoolButtons();
        alert(e.message);
      }
    });
    menu.querySelector("[data-new-pool]").onclick = async () => {
      closePoolMenu();
      const name = prompt("Name this pool:", "");
      if (!name || !name.trim()) return;
      try {
        const pool = await post("/api/pools", {name:name.trim(), color:"blue"});
        S.pools.unshift(pool);
        S.poolMembership.set(profileId, [...(S.poolMembership.get(profileId) || []), pool.pool_id]);
        paintPoolButtons();
        await post(`/api/pools/${pool.pool_id}/members`, {profile_id: profileId});
        pool.member_count = (pool.member_count || 0) + 1;   // keep the card count honest
      } catch(e) { alert(e.status === 409 ? "You already have a pool with that name." : e.message); }
    };
  }

  async function startApp(){
    // A splash covers the screen while we validate an existing token, so a
    // logged-in user never sees the login form flash on refresh.
    if (token()) {
      const ok = await loadMe();
      if (ok) {
        $("#boot-splash").classList.add("hidden");
        const saved = localStorage.getItem("hb_page");
        showPage(saved && document.getElementById("page-" + saved) ? saved : "dashboard");
        loadJobs();
        refreshUnreadBadge();
        refreshNotificationBadge();
        refreshCredits();
        if (isRecruiter()){
          loadProviderFacets();
          // Standing searches are re-counted on entry; anything that grew since
          // the last visit turns into a notification.
          loadSavedSearches().then(checkSavedSearches);
        }
        return;
      }
    }
    // No token (or it was rejected): show the login form.
    $("#boot-splash").classList.add("hidden");
    $("#auth-gate").classList.remove("hidden");
  }
  wire();
  startApp();
})();
