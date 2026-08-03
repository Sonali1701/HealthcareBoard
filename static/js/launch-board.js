(function(){
  const S = {
    user:null, profile:null, authMode:"login",
    provider:{
      q:"", category:"", license_title:"", zip:"", radius_mi:"25", state_code:"",
      min_experience:"", max_experience:"", contact_available:""
    },
    providerCache:new Map(), providerInflight:new Map(), providerCards:new Map(), releasedContacts:new Map(), providerReq:0,
    providerOffset:0, providerTotal:null, providerHasNext:false, facetCategories:{},
    providerLastData:null, facetsLoaded:false, activeCounts:null
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
  const loading = text => `<div class="loading-state"><span class="spinner"></span><strong>${esc(text)}</strong></div>`;
  // Same state block, but legal inside a <tbody>. Column counts must match the
  // <thead> in board.html so the state spans the full table width.
  const JOB_COLS = 5, PROVIDER_COLS = 7;
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
    const res = await fetch(path, {method, headers, body: body instanceof FormData ? body : body !== undefined ? JSON.stringify(body) : undefined});
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
    if (id === "providers" && !isRecruiter()) id = "dashboard";
    try { localStorage.setItem("hb_page", id); } catch(e) {}   // restored on refresh
    $$(".page").forEach(p => p.classList.toggle("active", p.id === "page-" + id));
    $$(".nav-item").forEach(n => n.classList.toggle("active", n.dataset.page === id));
    if (id === "dashboard") loadDashboard();
    if (id === "jobs") loadJobs();
    if (id === "providers") { if (!S.facetsLoaded) loadProviderFacets(); loadProviders(); }
    if (id === "profile") loadProfile();
    if (id === "community") loadFeed();
    if (id === "notifications") loadNotifications();
    if (id === "employer") loadEmployer();
    if (id === "analytics") loadAnalytics();
  }

  function jobRow(j){
    const loc = [j.city,j.state_code].filter(Boolean).join(", ") || "Flexible";
    const pay = j.pay_rate_max ? `$${Math.round(j.pay_rate_max)}${j.pay_unit === "hourly" ? "/hr" : ""}` : "";
    return `<tr>
      <td>
        <div class="cell-name">${esc(j.title)}${j.is_urgent ? `<span class="badge coral">Urgent</span>` : ""}</div>
        <div class="cell-sub">${esc(loc)}</div>
      </td>
      <td>${j.job_type ? `<span class="badge accent">${esc(j.job_type)}</span>` : `<span class="cell-none">—</span>`}</td>
      <td>${j.specialty ? `<span class="badge">${esc(j.specialty)}</span>` : `<span class="cell-none">—</span>`}</td>
      <td>${pay ? `<strong>${esc(pay)}</strong>` : `<span class="cell-none">—</span>`}</td>
      <td class="td-actions"><button class="btn small primary" data-apply="${j.job_id}">Apply</button></td>
    </tr>`;
  }

  async function loadDashboard(){
    $("#dashboard-jobs").innerHTML = loadingRow(JOB_COLS, "Loading jobs...");
    try {
      const jobs = await get("/api/jobs?limit=4");
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
    const params = new URLSearchParams({limit:"50"});
    if ($("#job-q").value.trim()) params.set("q", $("#job-q").value.trim());
    if ($("#job-type").value) params.set("job_type", $("#job-type").value);
    if ($("#job-state").value.trim()) params.set("state_code", $("#job-state").value.trim().toUpperCase());
    $("#jobs-list").innerHTML = loadingRow(JOB_COLS, "Loading jobs...");
    try {
      const data = await get("/api/jobs?" + params.toString());
      $("#jobs-count").textContent = `${data.total} job${data.total === 1 ? "" : "s"}`;
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
    if (state.zip && state.radius_mi){ params.set("zip", state.zip); params.set("radius_mi", state.radius_mi); }
    params.set("offset", String(opts.offset || 0));
    params.set("count", opts.count ? "1" : "0");
    return params;
  }
  function hasNonCategoryFilters(){
    const s = S.provider;
    return ["q","license_title","state_code","min_experience","max_experience","contact_available"].some(k => s[k]) || !!(s.zip && s.radius_mi);
  }
  function facetTotalFor(category){
    const c = S.activeCounts || S.facetCategories;   // counts reflect the active filters
    if (!c || !Object.keys(c).length) return null;
    return category ? (c[category] || 0) : ["Physicians","Nursing","Allied","APP","Others"].reduce((s,k)=>s+(c[k]||0),0);
  }
  // Faceted counts: the headline + tab numbers reflect the CURRENT filters.
  function countParams(){
    const s = S.provider, p = new URLSearchParams();
    ["q","license_title","state_code","min_experience","max_experience","contact_available"].forEach(k => { if (s[k]) p.set(k, s[k]); });
    if (s.zip && s.radius_mi){ p.set("zip", s.zip); p.set("radius_mi", s.radius_mi); }
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
    items.forEach(p => S.providerCards.set(p.profile_id, p));
    $("#providers-count").textContent = providerCountLabel();
    $("#providers-grid").innerHTML = items.length ? items.map(providerRow).join("") : loadingRow(PROVIDER_COLS, "No providers match these filters.");
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
    if (!(p.email || p.phone)) {
      return `<div class="contact-cell"><span class="contact-none"><i class="fas fa-minus"></i>No contact on file</span></div>`;
    }
    // Locked: one compact reveal action + a hint of which channels are on file.
    const avail = [
      p.email ? `<i class="fas fa-envelope" title="Email on file"></i>` : "",
      p.phone ? `<i class="fas fa-phone" title="Phone on file"></i>` : "",
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
    return `<tr>
      <td>
        <div class="cell-user">
          <span class="avatar">${initials(p.first_name,p.last_name)}</span>
          <div class="cell-id">
            <div class="cell-name">${esc(p.first_name)} ${esc(p.last_name)}</div>
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

  async function loadProfile(){
    if (!S.profile) { $("#profile-card").innerHTML = loading("No profile found."); return; }
    const p = S.profile;
    $("#profile-sub").textContent = `${p.completion_score || 0}% complete`;
    $("#profile-card").innerHTML = `<div class="profile-grid">
      <div><div class="muted">Name</div><strong>${esc(p.first_name)} ${esc(p.last_name)}</strong></div>
      <div><div class="muted">Role</div><strong>${esc(p.headline || p.profession_type || "Healthcare Pro")}</strong></div>
      <div><div class="muted">Email</div><strong>${esc(p.email || S.user.email)}</strong></div>
      <div><div class="muted">Phone</div><strong>${esc(p.phone || "Not provided")}</strong></div>
      <div><div class="muted">Location</div><strong>${esc([p.city,p.state_code].filter(Boolean).join(", ") || "Not provided")}</strong></div>
      <div><div class="muted">Resume</div><strong>${p.resume_url ? "On file" : "Missing"}</strong></div>
    </div>`;
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
      $("#employer-panel").innerHTML = d.employer ? `<div class="profile-grid"><div><div class="muted">Organization</div><strong>${esc(d.employer.org_name)}</strong></div><div><div class="muted">Open jobs</div><strong>${esc(d.kpis.jobs)}</strong></div><div><div class="muted">Applications</div><strong>${esc(d.kpis.applications)}</strong></div><div><div class="muted">Interviews</div><strong>${esc(d.kpis.interviews)}</strong></div></div>` : loading("No employer organization yet.");
    } catch(e) { $("#employer-panel").innerHTML = loading("Could not load employer dashboard."); }
  }
  async function loadAnalytics(){
    try {
      const f = await get("/api/analytics/funnel");
      $("#analytics-panel").innerHTML = Object.entries(f || {}).map(([k,v]) => `<div class="metric"><span>${esc(v)}</span><small>${esc(k)}</small></div>`).join("");
    } catch(e) { $("#analytics-panel").innerHTML = `<div class="metric"><span>0</span><small>No analytics yet</small></div>`; }
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
      await loadMe(); $("#resume-drop span").textContent = "Resume uploaded.";
    } catch(e) { alert("Upload failed."); }
  }
  function renderResume(r){
    const sec = r.sections || {};
    const ini = (r.name || "?").split(/\s+/).map(w => w[0] || "").join("").slice(0, 2).toUpperCase();
    const lines = arr => (arr && arr.length) ? `<div class="rz-lines">${arr.map(x => `<p>${esc(x)}</p>`).join("")}</div>` : `<div class="rz-empty">Not listed on this résumé.</div>`;
    const fact = (label, val) => val ? `<div class="rz-fact"><span>${label}</span><b>${esc(val)}</b></div>` : "";
    const overview = `
      <div class="rz-facts">
        ${fact("Focus", r.role)}
        ${fact("Location", r.location)}
        ${fact("License / title", r.credential)}
        ${r.years_experience ? fact("Experience", r.years_experience + " yrs") : ""}
        ${fact("Board certification", r.board)}
      </div>
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
    const sections = [
      ["overview", "Overview", overview],
      ["experience", "Experience", lines(sec["Experience"])],
      ["education", "Education", lines(sec["Education & Training"])],
      ["skills", "Skills", skillsSec],
      ["certifications", "Certifications", certsSec],
    ];
    return `<div class="rz">
      <div class="rz-head">
        <div class="rz-avatar">${esc(ini)}</div>
        <div class="rz-id"><div class="rz-name">${esc(r.name)}</div>
          <div class="rz-sub">${esc([r.role, r.location].filter(Boolean).join("  ·  ")) || "Healthcare provider"}</div></div>
        <span class="rz-lock"><i class="fas fa-lock"></i> View only</span>
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
      const r = await get(`/api/profiles/${id}/resume`);
      $("#modal-root .modal-head strong").textContent = r.name ? `${r.name} — Résumé` : "Résumé";
      $("#modal-root .modal-body").innerHTML = renderResume(r);
      wireResumeNav();
    } catch(e) { $("#modal-root .modal-body").innerHTML = `<div style="padding:24px">${esc(e.message || "Could not load résumé.")}</div>`; }
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
      if (S.providerLastData) renderProviderPage(S.providerLastData);
      return released;
    } catch(e) {
      if (btn) {
        btn.disabled = false;
        btn.innerHTML = original;
      }
      alert(e.message || "Could not release contact.");
      return null;
    }
  }
  function wire(){
    $$(".auth-tab").forEach(b => b.onclick = () => showAuthMode(b.dataset.authMode));
    $("#auth-submit").onclick = submitAuth;
    $("#auth-password").addEventListener("keydown", e => { if (e.key === "Enter") submitAuth(); });
    $("#auth-email").addEventListener("keydown", e => { if (e.key === "Enter") $("#auth-password").focus(); });
    const pwt = $("#auth-pw-toggle");
    if (pwt) pwt.onclick = () => { const i = $("#auth-password"); const show = i.type === "password"; i.type = show ? "text" : "password"; pwt.innerHTML = show ? '<i class="fas fa-eye-slash"></i>' : '<i class="fas fa-eye"></i>'; };
    $$(".nav-item,.top-user,.top-actions .icon-btn,.hero-band .btn,.logo").forEach(el => el.addEventListener("click", e => { const page = el.dataset.page; if (page) { e.preventDefault(); showPage(page); } }));
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
    document.body.addEventListener("click", e => {
      const apply = e.target.closest("[data-apply]"); if (apply) applyJob(apply.dataset.apply);
      const resume = e.target.closest("[data-resume]"); if (resume) viewResume(resume.dataset.resume);
      const release = e.target.closest("[data-release]"); if (release) releaseContact(release.dataset.release);
      if (e.target.closest("[data-close-modal]") || e.target.classList.contains("modal")) $("#modal-root").innerHTML = "";
    });
    $("#resume-drop").onclick = () => $("#resume-file").click();
    $("#resume-file").onchange = () => { if ($("#resume-file").files[0]) uploadResume($("#resume-file").files[0]); };
  }
  function debounce(fn, ms){ let t; return () => { clearTimeout(t); t = setTimeout(fn, ms); }; }

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
        if (isRecruiter()) loadProviderFacets();
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
