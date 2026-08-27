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
    // Duplicates review + job orders (the recruiter's org/postings hub)
    dupes:[], employer:null, templates:[], credits:null,
    jobAlerts:[], subStatuses:[],
    // Travel pay calculator: retain the exact request/result so the comparison
    // can be saved without recomputing it with different assumptions.
    payInputs:null, payPackage:null
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
  const isAdmin = () => S.user && S.user.role === "admin";
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
  // Finished states, with no spinner. These used to reuse loading(), which left
  // a spinner turning under "No notifications" and under every error — a page
  // that has given up must not look like a page still working.
  const stateBlock = (icon, text, hint, cls) =>
    `<div class="loading-state ${cls}"><i class="fas ${icon}"></i><strong>${esc(text)}</strong>${
      hint ? `<span class="state-hint">${esc(hint)}</span>` : ""}</div>`;
  const emptyState = (text, hint = "", icon = "fa-inbox") =>
    stateBlock(icon, text, hint, "is-empty");
  const errorState = (text, hint = "That is usually temporary — try again in a moment.") =>
    stateBlock("fa-triangle-exclamation", text, hint, "is-error");
  // Same state blocks, but legal inside a <tbody>. Column counts must match the
  // <thead> in board.html so the state spans the full table width.
  const JOB_COLS = 5, PROVIDER_COLS = 8;   // 8 = checkbox + 7 data columns
  const stateRow = (cols, html) => `<tr class="row-state"><td colspan="${cols}">${html}</td></tr>`;
  const loadingRow = (cols, text) => stateRow(cols, loading(text));
  const emptyRow = (cols, text, hint, icon) => stateRow(cols, emptyState(text, hint, icon));
  const errorRow = (cols, text) => stateRow(cols, errorState(text));
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
    // Single active session: the server signals (via header) that this login was
    // superseded by one on another device. Sign out locally and say why.
    if (res.status === 401 && token() && res.headers.get("X-Session-Superseded") === "1"){
      handleSuperseded();
      const err = new Error("Signed out"); err.status = 401; err.superseded = true; throw err;
    }
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
  // Clear local auth and reload to the signed-out screen, leaving a one-shot
  // flag so the reason can be shown once on the landing page.
  function handleSuperseded(){
    if (S._superseded) return;
    S._superseded = true;
    setToken(""); setRefresh("");
    try { localStorage.setItem("hb_signout_reason", "superseded"); } catch(e){}
    location.reload();
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
    $$(".seeker-only").forEach(el => el.classList.toggle("hidden", isRecruiter()));
    $$(".admin-only").forEach(el => el.classList.toggle("hidden", !isAdmin()));
    if (!isRecruiter() && $("#page-providers").classList.contains("active")) showPage("dashboard");
    if (!isAdmin() && $("#page-admin").classList.contains("active")) showPage("dashboard");
  }

  async function loadMe(){
    if (!token()) return false;
    try {
      S.user = await get("/api/auth/me");
      // An unverified account (only possible when email delivery is on) is held
      // at a verification gate rather than let into the app.
      if (S.user.status === "pending_verify"){ showVerifyGate(); return "pending"; }
      try { S.profile = await get("/api/profiles/me"); } catch(e) { S.profile = null; }
      applyRole();
      const name = S.profile ? `${S.profile.first_name} ${S.profile.last_name}` : S.user.email.split("@")[0];
      const role = isRecruiter() ? "Recruiter" : (S.profile && (S.profile.specialty || S.profile.profession_type)) || "Healthcare Pro";
      $("#mini-name").textContent = name; $("#top-name").textContent = name.split(" ")[0];
      $("#mini-role").textContent = role;
      const av = S.profile ? initials(S.profile.first_name,S.profile.last_name) : S.user.email[0].toUpperCase();
      $("#mini-avatar").textContent = av; $("#top-avatar").textContent = av;
      $("#landing").classList.add("hidden");
      $("#auth-gate").classList.add("hidden");
      $("#verify-gate").classList.add("hidden");
      $("#app-shell").classList.remove("hidden");
      return true;
    } catch(e) {
      setToken(""); setRefresh(""); return false;
    }
  }

  function showVerifyGate(){
    $("#boot-splash").classList.add("hidden");
    $("#auth-gate").classList.add("hidden");
    $("#app-shell").classList.add("hidden");
    const g = $("#verify-gate");
    if (!g) return;
    g.classList.remove("hidden");
    const em = $("#verify-email");
    if (em && S.user) em.textContent = S.user.email;
  }
  function verifyMsg(text, kind){
    const m = $("#verify-msg");
    if (m){ m.className = "verify-msg" + (kind ? " " + kind : ""); m.textContent = text; }
  }

  function showAuthMode(mode){
    S.authMode = mode;
    $$(".auth-tab").forEach(b => b.classList.toggle("active", b.dataset.authMode === mode));
    $("#signup-fields").classList.toggle("open", mode === "signup");
    $("#auth-title").textContent = mode === "signup" ? "Create your account" : "Welcome back";
    $("#auth-subtitle").textContent = mode === "signup" ? "Join HealthBoard in seconds." : "Sign in to access your workspace.";
    $("#auth-submit").textContent = mode === "signup" ? "Create account" : "Sign in";
    $("#auth-hint").classList.toggle("show", mode === "signup");
    $("#signup-agree-row").classList.toggle("show", mode === "signup");
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
        if (!$("#auth-agree").checked)
          throw new Error("Please agree to the Terms of Service and Privacy Policy to continue.");
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
    // Community Feed is hidden pre-launch (read-only, seeded demo content). The
    // page, social.py API and loadFeed() are kept — remove this line and restore
    // the nav button in board.html to bring it back.
    if (id === "community") id = "dashboard";
    // Retired from the product — keep the code/pages but make them unreachable.
    if (["submissions", "clients", "placements", "extension", "paytools"].includes(id)) id = "dashboard";
    if ((id === "providers" || id === "ai" || id === "extension" || id === "pools"
         || id === "matching" || id === "outreach" || id === "credits" || id === "submissions"
         || id === "applicants" || id === "calculator" || id === "clients" || id === "orgadmin"
         || id === "placements") && !isRecruiter()) id = "dashboard";
    // Seeker-only pages: a staffing agency sources candidates, it doesn't find
    // jobs, apply, or keep a résumé.
    if ((id === "resume" || id === "applications" || id === "jobs") && isRecruiter()) id = "dashboard";
    // The admin console is for platform admins only.
    if (id === "admin" && !isAdmin()) id = "dashboard";
    if (id !== "messages") stopMessagePolling();
    try { localStorage.setItem("hb_page", id); } catch(e) {}   // restored on refresh
    $$(".page").forEach(p => p.classList.toggle("active", p.id === "page-" + id));
    $$(".nav-item").forEach(n => n.classList.toggle("active", n.dataset.page === id));
    if (id === "dashboard") loadDashboard();
    if (id === "jobs") loadJobs();
    if (id === "providers") { if (!S.facetsLoaded) loadProviderFacets(); loadProviders(); loadSavedSearches(); }
    if (id === "ai") setTimeout(() => { const el = $("#ai-input"); if (el) el.focus(); }, 60);
    if (id === "jobai") setTimeout(() => { const el = $("#jai-input"); if (el) el.focus(); }, 60);
    if (id === "extension") loadExtensionPage();
    if (id === "profile") loadProfile();
    if (id === "community") loadFeed();
    if (id === "notifications") { loadNotifications(); refreshNotificationBadge(); }
    if (id === "messages") loadMessages();
    if (id === "pools") loadPools();
    if (id === "employer") loadEmployer();
    if (id === "orgadmin") loadOrgAdmin();
    if (id === "analytics") loadAnalytics();
    if (id === "calculator") loadPayCalculator();
    if (id === "outreach") loadOutreach();
    if (id === "credits") loadCredits();
    if (id === "applications") loadApplications();
    if (id === "submissions") loadSubmissions();
    if (id === "clients") loadClients();
    if (id === "placements") loadPlacements();
    if (id === "credentials") loadWallet();
    if (id === "admin") loadAdmin();
  }

  // ---- Admin console (platform super-admin) --------------------------------
  const ADMIN = { tab: "overview", uOffset: 0, uLimit: 25, oOffset: 0, oLimit: 25,
                  lOffset: 0, lLimit: 50, jOffset: 0, jLimit: 25, auOffset: 0, auLimit: 50 };
  const ADMIN_COLS = 8, ADMIN_ORG_COLS = 9, ADMIN_LOGIN_COLS = 5, ADMIN_JOB_COLS = 7, ADMIN_AUDIT_COLS = 5;
  const ROLE_LABEL = {job_seeker:"Job seeker", recruiter:"Recruiter", employer:"Employer", admin:"Admin"};
  const STATUS_LABEL = {active:"Active", suspended:"Suspended", pending_verify:"Pending", deleted:"Deleted"};
  const adminDate = iso => iso ? new Date(iso).toLocaleDateString([], {year:"numeric", month:"short", day:"numeric"}) : "—";

  function loadAdmin(){
    if (!isAdmin()) return;
    showAdminTab(ADMIN.tab || "overview");
  }
  function showAdminTab(name){
    ADMIN.tab = name;
    $$(".admin-tab").forEach(t => t.classList.toggle("active", t.dataset.atab === name));
    $$(".admin-panel").forEach(p => p.classList.toggle("active", p.id === "admin-" + (name === "orgs" ? "orgs" : name)));
    if (name === "overview") loadAdminOverview();
    if (name === "users") loadAdminUsers();
    if (name === "orgs") loadAdminOrgs();
    if (name === "jobs") loadAdminJobs();
    if (name === "logins") loadAdminLogins();
    if (name === "audit") loadAdminAudit();
  }

  async function loadAdminLogins(){
    const tb = $("#admin-login-rows");
    tb.innerHTML = loadingRow(ADMIN_LOGIN_COLS, "Loading login activity…");
    const params = new URLSearchParams({limit:ADMIN.lLimit, offset:ADMIN.lOffset});
    try {
      const data = await get("/api/admin/logins?" + params.toString());
      if (!data.logins.length){
        tb.innerHTML = emptyRow(ADMIN_LOGIN_COLS, "No logins recorded yet.", "", "fa-network-wired");
        renderPager("#admin-login-pager", data, () => loadAdminLogins(), "lOffset");
        return;
      }
      tb.innerHTML = data.logins.map(l => {
        const who = l.name ? `<b>${esc(l.name)}</b><span class="admin-sub">${esc(l.email || "")}</span>`
                           : `<b>${esc((l.email || "unknown").split("@")[0])}</b><span class="admin-sub">${esc(l.email || "")}</span>`;
        const when = l.created_at ? new Date(l.created_at).toLocaleString([], {month:"short", day:"numeric", hour:"numeric", minute:"2-digit"}) : "—";
        const sess = l.active ? `<span class="admin-badge ok">Active</span>` : `<span class="admin-badge">Expired</span>`;
        return `<tr>
          <td class="admin-user-cell">${who}</td>
          <td class="admin-ip">${esc(l.ip || "—")}</td>
          <td>${esc(l.device || "—")}</td>
          <td>${esc(when)}</td>
          <td>${sess}</td>
        </tr>`;
      }).join("");
      renderPager("#admin-login-pager", data, () => loadAdminLogins(), "lOffset");
    } catch(e) {
      tb.innerHTML = errorRow(ADMIN_LOGIN_COLS, "Could not load login activity.");
    }
  }

  const statCard = (label, value, sub="") =>
    `<div class="admin-stat"><span class="admin-stat-val">${esc(value)}</span>`
    + `<span class="admin-stat-label">${esc(label)}</span>`
    + (sub ? `<span class="admin-stat-sub">${esc(sub)}</span>` : "") + `</div>`;
  const statGroup = (title, cards) =>
    `<div class="admin-stat-group"><h3>${esc(title)}</h3><div class="admin-stat-row">${cards.join("")}</div></div>`;

  async function loadAdminOverview(){
    const box = $("#admin-stats");
    box.innerHTML = loading("Loading platform overview…");
    try {
      const o = await get("/api/admin/overview");
      const u = o.users, c = o.content, b = o.billing;
      const nf = n => (n == null ? "—" : Number(n).toLocaleString());
      box.innerHTML =
        statGroup("Users", [
          statCard("Total users", nf(u.total), `${nf(u.active)} active`),
          statCard("Job seekers", nf(u.job_seekers)),
          statCard("Recruiters", nf(u.recruiters)),
          statCard("Admins", nf(u.admins)),
          statCard("Suspended", nf(u.suspended)),
          statCard("New this week", nf(u.new_7d), `${nf(u.new_24h)} today · ${nf(u.new_30d)}/30d`),
        ])
        + statGroup("Content", [
          statCard("Providers", nf(c.profiles), `${nf(c.profiles_listable)} listable`),
          statCard("Jobs", nf(c.jobs), `${nf(c.jobs_active)} active · ${nf(c.jobs_featured)} featured`),
          statCard("Organizations", nf(c.organizations), `${nf(c.organizations_verified)} verified`),
          statCard("Applications", nf(c.applications)),
          statCard("Messages", nf(c.messages)),
        ])
        + statGroup("Billing · credits", [
          statCard("Credits in circulation", nf(b.credit_balance)),
          statCard("Credits spent", nf(b.credit_spent), "lifetime reveals"),
          statCard("Credits granted", nf(b.credit_granted), "lifetime"),
        ])
        + (o.recent_signups && o.recent_signups.length ? `<div class="admin-stat-group"><h3>Recent signups</h3>
          <div class="admin-recent">${o.recent_signups.map(s => `<div class="admin-recent-row">
            <span class="admin-recent-email">${esc(s.email)}</span>
            <span class="admin-badge">${esc(ROLE_LABEL[s.role] || s.role)}</span>
            <span class="admin-sub">${esc(adminDate(s.created_at))}</span></div>`).join("")}</div></div>` : "");
    } catch(e) {
      box.innerHTML = errorState("Could not load the overview.", e.message || "");
    }
  }

  async function loadAdminUsers(){
    const tb = $("#admin-user-rows");
    tb.innerHTML = loadingRow(ADMIN_COLS, "Loading users…");
    const q = clean($("#admin-user-q").value);
    const role = $("#admin-user-role").value;
    const status = $("#admin-user-status").value;
    const params = new URLSearchParams({limit:ADMIN.uLimit, offset:ADMIN.uOffset});
    if (q) params.set("q", q);
    if (role) params.set("role", role);
    if (status) params.set("status", status);
    try {
      const data = await get("/api/admin/users?" + params.toString());
      if (!data.users.length){
        tb.innerHTML = emptyRow(ADMIN_COLS, "No users match.", "Try clearing the filters.", "fa-user-slash");
        renderPager("#admin-user-pager", data, () => loadAdminUsers(), "uOffset");
        return;
      }
      tb.innerHTML = data.users.map(adminUserRow).join("");
      renderPager("#admin-user-pager", data, () => loadAdminUsers(), "uOffset");
    } catch(e) {
      tb.innerHTML = errorRow(ADMIN_COLS, "Could not load users.");
    }
  }

  function adminUserRow(u){
    const name = u.name ? `<b>${esc(u.name)}</b><span class="admin-sub">${esc(u.email)}</span>`
                        : `<b>${esc(u.email.split("@")[0])}</b><span class="admin-sub">${esc(u.email)}</span>`;
    const verified = u.email_verified ? "" : ` <span class="admin-flag" title="Email not verified"><i class="fas fa-circle-exclamation"></i></span>`;
    const statusCls = u.status === "active" ? "ok" : (u.status === "suspended" ? "err" : "warn");
    const roleSel = u.is_self ? ROLE_LABEL[u.role] || u.role
      : `<select class="admin-mini-sel" data-admin-role="${esc(u.user_id)}">`
        + ["job_seeker","recruiter","employer","admin"].map(r =>
            `<option value="${r}"${r === u.role ? " selected" : ""}>${ROLE_LABEL[r]}</option>`).join("")
        + `</select>`;
    let action;
    if (u.is_self) action = `<span class="admin-you">You</span>`;
    else if (u.status === "suspended")
      action = `<button class="btn ghost small" data-admin-activate="${esc(u.user_id)}"><i class="fas fa-unlock"></i>Reactivate</button>`;
    else
      action = `<button class="btn ghost small danger" data-admin-suspend="${esc(u.user_id)}"><i class="fas fa-ban"></i>Suspend</button>`;
    return `<tr>
      <td class="admin-user-cell">${name}${verified}</td>
      <td>${roleSel}</td>
      <td><span class="admin-badge ${statusCls}">${STATUS_LABEL[u.status] || u.status}</span></td>
      <td>${u.credit_balance == null ? "—" : Number(u.credit_balance).toLocaleString()}</td>
      <td>${u.last_login_at ? adminDate(u.last_login_at) : "Never"}</td>
      <td class="admin-ip">${esc(u.last_ip || "—")}</td>
      <td>${adminDate(u.created_at)}</td>
      <td class="td-actions"><button class="btn ghost small" data-admin-user="${esc(u.user_id)}"><i class="fas fa-sliders"></i>Manage</button>${action}</td>
    </tr>`;
  }

  async function adminUpdateUser(userId, body){
    try {
      await patch(`/api/admin/users/${userId}`, body);
      toast("User updated.", {title:"Admin"});
      loadAdminUsers();
    } catch(e) {
      toast(e.message || "Could not update the user.", {title:"Update failed", kind:"err"});
      loadAdminUsers();   // revert any optimistic select change
    }
  }

  async function loadAdminOrgs(){
    const tb = $("#admin-org-rows");
    tb.innerHTML = loadingRow(ADMIN_ORG_COLS, "Loading organizations…");
    const q = clean($("#admin-org-q").value);
    const params = new URLSearchParams({limit:ADMIN.oLimit, offset:ADMIN.oOffset});
    if (q) params.set("q", q);
    try {
      const data = await get("/api/admin/organizations?" + params.toString());
      if (!data.organizations.length){
        tb.innerHTML = emptyRow(ADMIN_ORG_COLS, "No organizations yet.", "", "fa-hospital");
        renderPager("#admin-org-pager", data, () => loadAdminOrgs(), "oOffset");
        return;
      }
      tb.innerHTML = data.organizations.map(o => {
        const loc = [o.city, o.state_code].filter(Boolean).join(", ") || "—";
        const verified = o.is_verified ? ` <i class="fas fa-circle-check admin-verified" title="Verified"></i>` : "";
        return `<tr>
          <td><b>${esc(o.org_name)}</b>${verified}</td>
          <td>${esc(o.org_type || "—")}</td>
          <td>${esc(loc)}</td>
          <td class="admin-sub">${esc(o.owner_email || "—")}</td>
          <td><b>${Number(o.credits || 0).toLocaleString()}</b></td>
          <td>${Number(o.members).toLocaleString()}</td>
          <td>${Number(o.jobs).toLocaleString()} <span class="admin-sub">(${Number(o.jobs_active).toLocaleString()} active)</span></td>
          <td>${adminDate(o.created_at)}</td>
          <td class="td-actions"><button class="btn ghost small" data-admin-org="${esc(o.employer_id)}"><i class="fas fa-sliders"></i>Manage</button></td>
        </tr>`;
      }).join("");
      renderPager("#admin-org-pager", data, () => loadAdminOrgs(), "oOffset");
    } catch(e) {
      tb.innerHTML = errorRow(ADMIN_ORG_COLS, "Could not load organizations.");
    }
  }

  // ---- Admin: jobs moderation ----------------------------------------------
  async function loadAdminJobs(){
    const tb = $("#admin-job-rows");
    tb.innerHTML = loadingRow(ADMIN_JOB_COLS, "Loading jobs…");
    const q = clean($("#admin-job-q").value), status = $("#admin-job-status").value;
    const params = new URLSearchParams({limit:ADMIN.jLimit, offset:ADMIN.jOffset});
    if (q) params.set("q", q);
    if (status) params.set("status", status);
    try {
      const data = await get("/api/admin/jobs?" + params.toString());
      if (!data.jobs.length){
        tb.innerHTML = emptyRow(ADMIN_JOB_COLS, "No jobs match.", "", "fa-briefcase");
        renderPager("#admin-job-pager", data, () => loadAdminJobs(), "jOffset");
        return;
      }
      tb.innerHTML = data.jobs.map(adminJobRow).join("");
      renderPager("#admin-job-pager", data, () => loadAdminJobs(), "jOffset");
    } catch(e) { tb.innerHTML = errorRow(ADMIN_JOB_COLS, "Could not load jobs."); }
  }
  function adminJobRow(j){
    const statusCls = j.status === "active" ? "ok" : (j.status === "closed" ? "err" : "warn");
    const feat = j.is_featured ? `<i class="fas fa-star admin-featured" title="Featured"></i> ` : "";
    const featBtn = j.is_featured
      ? `<button class="btn ghost small" data-admin-job-unfeature="${esc(j.job_id)}" title="Remove from featured"><i class="fas fa-star"></i></button>`
      : `<button class="btn ghost small" data-admin-job-feature="${esc(j.job_id)}" title="Feature this job"><i class="far fa-star"></i></button>`;
    const statusBtn = j.status === "active"
      ? `<button class="btn ghost small" data-admin-job-pause="${esc(j.job_id)}">Pause</button>`
      : `<button class="btn ghost small" data-admin-job-activate="${esc(j.job_id)}">Activate</button>`;
    return `<tr>
      <td class="admin-user-cell">${feat}<b>${esc(j.title)}</b><span class="admin-sub">${esc(j.specialty || j.source || "")}</span></td>
      <td>${esc(j.org_name || "—")}</td>
      <td>${esc(j.location || "—")}</td>
      <td><span class="admin-badge ${statusCls}">${esc(j.status)}</span></td>
      <td>${Number(j.applications || 0).toLocaleString()}</td>
      <td>${adminDate(j.created_at)}</td>
      <td class="td-actions">${featBtn}${statusBtn}<button class="btn ghost small danger" data-admin-job-delete="${esc(j.job_id)}" title="Delete job"><i class="fas fa-trash"></i></button></td>
    </tr>`;
  }
  async function adminModerateJob(jobId, body){
    try { await patch(`/api/admin/jobs/${jobId}`, body); loadAdminJobs(); }
    catch(e) { toast(e.message || "Could not update the job.", {title:"Job", kind:"err"}); }
  }
  async function adminDeleteJob(jobId){
    const ok = await confirmDialog({title:"Delete this job?",
      body:"This permanently deletes the job and its applications. This cannot be undone.",
      confirm:"Delete", danger:true});
    if (!ok) return;
    try { await del(`/api/admin/jobs/${jobId}`); toast("Job deleted.", {title:"Jobs"}); loadAdminJobs(); }
    catch(e) { toast(e.message || "Could not delete the job.", {title:"Delete failed", kind:"err"}); }
  }

  // ---- Admin: audit log ----------------------------------------------------
  async function loadAdminAudit(){
    const tb = $("#admin-audit-rows");
    tb.innerHTML = loadingRow(ADMIN_AUDIT_COLS, "Loading audit log…");
    const params = new URLSearchParams({limit:ADMIN.auLimit, offset:ADMIN.auOffset});
    try {
      const data = await get("/api/admin/audit?" + params.toString());
      if (!data.logs.length){
        tb.innerHTML = emptyRow(ADMIN_AUDIT_COLS, "No admin actions yet.", "", "fa-clock-rotate-left");
        renderPager("#admin-audit-pager", data, () => loadAdminAudit(), "auOffset");
        return;
      }
      tb.innerHTML = data.logs.map(l => {
        const action = (l.action || "").replace("admin.", "").replace(/_/g, " ");
        const when = l.created_at ? new Date(l.created_at).toLocaleString([], {month:"short", day:"numeric", hour:"numeric", minute:"2-digit"}) : "—";
        const meta = l.meta && Object.keys(l.meta).length
          ? Object.entries(l.meta).map(([k, v]) => `${k}: ${v}`).join(", ") : "";
        const target = [l.entity_type, l.entity_id ? l.entity_id.slice(0, 8) : ""].filter(Boolean).join(" ");
        return `<tr>
          <td><b>${esc(action)}</b></td>
          <td class="admin-sub">${esc(l.actor || "—")}</td>
          <td class="admin-sub">${esc(target || "—")}</td>
          <td class="admin-sub">${esc(meta)}</td>
          <td>${esc(when)}</td>
        </tr>`;
      }).join("");
      renderPager("#admin-audit-pager", data, () => loadAdminAudit(), "auOffset");
    } catch(e) { tb.innerHTML = errorRow(ADMIN_AUDIT_COLS, "Could not load the audit log."); }
  }

  // ---- Admin: detail modals + actions --------------------------------------
  function adminModal(title, bodyHtml){
    $("#modal-root").innerHTML = `<div class="modal"><div class="modal-card admin-modal">
      <div class="modal-head"><strong>${esc(title)}</strong>
        <button class="icon-btn" data-close-modal><i class="fas fa-xmark"></i></button></div>
      <div class="modal-body" id="admin-modal-body">${bodyHtml}</div>
    </div></div>`;
  }

  async function adminAdjustCredits({title, sign, url, after}){
    const v = await formDialog({title, submit: sign > 0 ? "Grant" : "Deduct",
      fields:[
        {name:"amount", label:"Amount (credits)", type:"number", required:true, value:"10"},
        {name:"note", label:"Note (optional)", wide:true, placeholder:"Reason for this adjustment"},
      ]});
    if (!v) return;
    const amt = Math.abs(parseInt(v.amount, 10) || 0) * sign;
    if (!amt){ toast("Enter a non-zero amount.", {kind:"err"}); return; }
    try {
      const r = await post(url, {amount: amt, note: v.note || null});
      toast(`Balance is now ${Number(r.balance).toLocaleString()} credits.`, {title:"Credits updated"});
      if (after) after();
    } catch(e){ toast(e.message || "Could not adjust credits.", {title:"Credits", kind:"err"}); }
  }

  async function openAdminUser(userId){
    adminModal("User", loading("Loading user…"));
    try {
      const u = await get(`/api/admin/users/${userId}`);
      const orgs = (u.organizations || []).map(o =>
        `<span class="admin-badge">${esc(o.org_name)} · ${esc(o.role)}</span>`).join(" ")
        || `<span class="admin-sub">Not in any organization</span>`;
      const logins = (u.recent_logins || []).map(l =>
        `<div class="admin-recent-row"><span class="admin-ip">${esc(l.ip || "—")}</span>
          <span class="admin-sub">${esc(adminDate(l.created_at))}</span>
          ${l.active ? `<span class="admin-badge ok">active</span>` : ""}</div>`).join("")
        || `<span class="admin-sub">No logins recorded</span>`;
      $("#admin-modal-body").innerHTML = `
        <div class="admin-detail">
          <div class="admin-detail-head">
            <div><h3>${esc(u.name || u.email.split("@")[0])}</h3><div class="admin-sub">${esc(u.email)}</div></div>
            <span class="admin-badge ${u.status === "active" ? "ok" : (u.status === "suspended" ? "err" : "warn")}">${esc(u.status)}</span>
          </div>
          <div class="admin-detail-grid">
            <div><label>Role</label><div>${esc(ROLE_LABEL[u.role] || u.role)}</div></div>
            <div><label>Email verified</label><div>${u.email_verified ? "Yes" : `No <button class="btn ghost small" id="au-verify">Verify now</button>`}</div></div>
            <div><label>Joined</label><div>${esc(adminDate(u.created_at))}</div></div>
            <div><label>Last IP</label><div class="admin-ip">${esc(u.last_ip || "—")}</div></div>
          </div>
          <div class="admin-credit-box">
            <div><span class="admin-stat-val">${Number(u.credits.balance).toLocaleString()}</span>
              <span class="admin-stat-label">Credits</span>
              <span class="admin-sub">${Number(u.credits.lifetime_granted).toLocaleString()} granted · ${Number(u.credits.lifetime_spent).toLocaleString()} spent</span></div>
            <div class="admin-credit-actions">
              <button class="btn ghost small" id="au-grant"><i class="fas fa-plus"></i>Grant</button>
              <button class="btn ghost small" id="au-deduct"><i class="fas fa-minus"></i>Deduct</button>
            </div>
          </div>
          <div><label>Organizations</label><div class="admin-chips">${orgs}</div></div>
          <div><label>Recent logins</label><div class="admin-recent">${logins}</div></div>
        </div>`;
      const vbtn = $("#au-verify");
      if (vbtn) vbtn.onclick = async () => {
        try { await post(`/api/admin/users/${userId}/verify`, {}); toast("Email verified.", {title:"User"});
          openAdminUser(userId); loadAdminUsers(); }
        catch(e){ toast(e.message || "Failed.", {kind:"err"}); }
      };
      $("#au-grant").onclick = () => adminAdjustCredits({title:`Grant credits · ${u.email}`, sign:1,
        url:`/api/admin/users/${userId}/credits`, after:() => openAdminUser(userId)});
      $("#au-deduct").onclick = () => adminAdjustCredits({title:`Deduct credits · ${u.email}`, sign:-1,
        url:`/api/admin/users/${userId}/credits`, after:() => openAdminUser(userId)});
    } catch(e){ $("#admin-modal-body").innerHTML = errorState("Could not load the user.", e.message || ""); }
  }

  async function openAdminOrg(orgId){
    adminModal("Organization", loading("Loading organization…"));
    try {
      const o = await get(`/api/admin/organizations/${orgId}`);
      const members = (o.members || []).map(m => `<tr>
        <td>${esc(m.name || m.email || "—")}<div class="admin-sub">${esc(m.email || "")}</div></td>
        <td>${m.is_owner ? `<span class="admin-badge accent">Owner</span>`
          : `<select class="admin-mini-sel" data-org-member-role="${esc(m.user_id)}">${
              ["recruiter", "manager", "admin"].map(r => `<option value="${r}"${r === m.role ? " selected" : ""}>${r}</option>`).join("")
            }</select>`}</td>
        <td class="td-actions">${m.is_owner ? "" : `<button class="btn ghost small danger" data-org-member-remove="${esc(m.user_id)}" title="Remove from org"><i class="fas fa-user-minus"></i></button>`}</td>
      </tr>`).join("");
      $("#admin-modal-body").innerHTML = `
        <div class="admin-detail">
          <div class="admin-detail-head">
            <div><h3>${esc(o.org_name)} ${o.is_verified ? `<i class="fas fa-circle-check admin-verified"></i>` : ""}</h3>
              <div class="admin-sub">${esc(o.org_type || "—")} · ${esc([o.city, o.state_code].filter(Boolean).join(", ") || "—")} · owner ${esc(o.owner_email || "—")}</div></div>
          </div>
          <div class="admin-detail-grid">
            <div><label>Jobs</label><div>${Number(o.jobs).toLocaleString()} <span class="admin-sub">(${Number(o.jobs_active).toLocaleString()} active)</span></div></div>
            <div><label>Plan</label><div>${esc(o.subscription_tier)}</div></div>
            <div><label>Verified</label><div><button class="btn ghost small" id="ao-verify">${o.is_verified ? "Un-verify" : "Verify"}</button></div></div>
            <div><label>Owner credits</label><div>${Number(o.owner_credits).toLocaleString()} <button class="btn ghost small" id="ao-grant"><i class="fas fa-plus"></i>Grant</button></div></div>
          </div>
          <div class="admin-members">
            <div class="admin-members-head"><label>Members</label><button class="btn ghost small" id="ao-add-member"><i class="fas fa-user-plus"></i>Add member</button></div>
            <div class="admin-table-wrap"><table class="admin-table"><thead><tr><th>Member</th><th>Org role</th><th></th></tr></thead><tbody>${members}</tbody></table></div>
          </div>
        </div>`;
      $("#ao-verify").onclick = async () => {
        try { await patch(`/api/admin/organizations/${orgId}`, {is_verified: !o.is_verified});
          toast("Organization updated.", {title:"Organization"}); openAdminOrg(orgId); loadAdminOrgs(); }
        catch(e){ toast(e.message || "Failed.", {kind:"err"}); }
      };
      $("#ao-grant").onclick = () => adminAdjustCredits({title:`Grant credits · ${o.org_name}`, sign:1,
        url:`/api/admin/organizations/${orgId}/credits`, after:() => openAdminOrg(orgId)});
      $("#ao-add-member").onclick = () => adminAddMember(orgId);
      $$("#admin-modal-body [data-org-member-role]").forEach(s => s.onchange = async () => {
        try { await patch(`/api/admin/organizations/${orgId}/members/${s.dataset.orgMemberRole}`, {role: s.value});
          toast("Member role updated.", {title:"Member"}); }
        catch(e){ toast(e.message || "Failed.", {kind:"err"}); openAdminOrg(orgId); }
      });
      $$("#admin-modal-body [data-org-member-remove]").forEach(b => b.onclick = async () => {
        try { await del(`/api/admin/organizations/${orgId}/members/${b.dataset.orgMemberRemove}`);
          toast("Member removed.", {title:"Member"}); openAdminOrg(orgId); }
        catch(e){ toast(e.message || "Failed.", {kind:"err"}); }
      });
    } catch(e){ $("#admin-modal-body").innerHTML = errorState("Could not load the organization.", e.message || ""); }
  }

  async function adminAddMember(orgId){
    const v = await formDialog({title:"Add member to organization",
      intro:"The user must already have a HealthBoard account. They'll be promoted to a recruiter account so they can use the workspace.",
      submit:"Add member",
      fields:[
        {name:"email", label:"User email", type:"email", required:true, wide:true, placeholder:"person@agency.com"},
        {name:"role", label:"Org role", type:"select", value:"recruiter", options:[
          {value:"recruiter", label:"Member — use the tools"},
          {value:"manager", label:"Manager — manage members"},
          {value:"admin", label:"Admin — manage roles & billing"}]},
      ]});
    if (!v) return;
    try { await post(`/api/admin/organizations/${orgId}/members`, {email:v.email.trim(), role:v.role});
      toast("Member added.", {title:"Organization"}); openAdminOrg(orgId); }
    catch(e){ toast(e.status === 404 ? "No user with that email." : (e.status === 409 ? "That user is already a member." : (e.message || "Could not add member.")),
      {title:"Add failed", kind:"err"}); }
  }

  async function adminNewOrg(){
    const v = await formDialog({title:"New organization",
      intro:"Leave owner email blank to own it yourself, or enter an existing user's email to make them the owner.",
      submit:"Create organization",
      fields:[
        {name:"org_name", label:"Organization name", required:true, wide:true, placeholder:"Radixsol Staffing"},
        {name:"org_type", label:"Type", type:"select", value:"agency", options:[
          {value:"agency", label:"Staffing agency"}, {value:"hospital", label:"Hospital"},
          {value:"health_system", label:"Health system"}, {value:"clinic", label:"Clinic"}]},
        {name:"owner_email", label:"Owner email (optional)", type:"email", wide:true, placeholder:"an existing user's email"},
      ]});
    if (!v) return;
    try {
      const r = await post("/api/admin/organizations", {org_name:v.org_name.trim(), org_type:v.org_type,
        owner_email: v.owner_email ? v.owner_email.trim() : null});
      toast("Organization created.", {title:"Admin"});
      loadAdminOrgs();
      openAdminOrg(r.employer_id);
    } catch(e){ toast(e.status === 404 ? "No user with that owner email." : (e.message || "Could not create the organization."),
      {title:"Create failed", kind:"err"}); }
  }

  function renderPager(sel, data, reload, offsetKey){
    const el = $(sel);
    if (!el) return;
    const {total, limit, offset} = data;
    const from = total ? offset + 1 : 0, to = Math.min(offset + limit, total);
    const prevDis = offset <= 0 ? " disabled" : "";
    const nextDis = offset + limit >= total ? " disabled" : "";
    el.innerHTML = `<span class="admin-pager-info">${from}–${to} of ${total.toLocaleString()}</span>`
      + `<button class="btn ghost small" data-pg="prev"${prevDis}><i class="fas fa-chevron-left"></i>Prev</button>`
      + `<button class="btn ghost small" data-pg="next"${nextDis}>Next<i class="fas fa-chevron-right"></i></button>`;
    el.querySelector('[data-pg="prev"]').onclick = () => { ADMIN[offsetKey] = Math.max(0, offset - limit); reload(); };
    el.querySelector('[data-pg="next"]').onclick = () => { if (offset + limit < total){ ADMIN[offsetKey] = offset + limit; reload(); } };
  }

  function jobRow(j){
    const loc = [j.city,j.state_code].filter(Boolean).join(", ") || "Flexible";
    // Show the real range, not just a ceiling — pay transparency is the hook.
    const payUnit = j.pay_unit === "hourly" ? "/hr" : "";
    const pay = j.pay_rate_max
      ? (j.pay_rate_min && Math.round(j.pay_rate_min) !== Math.round(j.pay_rate_max)
          ? `$${Math.round(j.pay_rate_min)}–$${Math.round(j.pay_rate_max)}${payUnit}`
          : `$${Math.round(j.pay_rate_max)}${payUnit}`)
      : "";
    // Agencies file one req per seat; the list is grouped, so say how many.
    const seats = (j.openings || 1) > 1 ? `<span class="badge accent openings">${j.openings} openings</span>` : "";
    const fit = j.fit_score > 0 ? `<span class="fit-badge">match</span>` : "";
    const sub = [j.facility, loc].filter(Boolean).join(" · ");
    return `<tr>
      <td>
        <div class="cell-name"><button class="linklike" data-jobview="${esc(j.job_id)}">${esc(j.title)}</button>${j.is_urgent ? `<span class="badge coral">Urgent</span>` : ""}${seats}${fit}</div>
        <div class="cell-sub">${esc(sub)}</div>
      </td>
      <td>${j.job_type ? `<span class="badge accent">${esc(j.job_type)}</span>` : `<span class="cell-none">—</span>`}</td>
      <td>${j.specialty ? `<span class="badge">${esc(j.specialty)}</span>` : `<span class="cell-none">—</span>`}</td>
      <td>${pay ? `<strong>${esc(pay)}</strong>` : `<span class="cell-none">—</span>`}</td>
      <td class="td-actions">${isRecruiter()
        ? `<button class="btn small primary" data-source="${j.job_id}" title="Find matching candidates"><i class="fas fa-bolt"></i>Source</button>`
        : `<button class="btn small" data-jobview="${j.job_id}">View</button><button class="btn small primary" data-apply="${j.job_id}">Apply</button>`}</td>
    </tr>`;
  }

  // One tile. `to` makes the whole tile a link to the page that explains it,
  // because a number you cannot act on is decoration.
  function metricTile(value, label, to){
    return `<div class="metric${to ? " is-link" : ""}"${to ? ` data-page="${to}"` : ""}>
      <span>${esc(String(value))}</span><small>${esc(label)}</small></div>`;
  }

  async function loadDashboard(){
    const rec = isRecruiter();
    $("#dashboard-jobs").innerHTML = loadingRow(JOB_COLS, "Loading jobs...");
    $("#dash-metrics").innerHTML = "";

    // The header and call to action belong to whoever is looking. A recruiter
    // being told to "Find Jobs" is the wrong job entirely.
    const cta = $("#dash-cta");
    if (cta){
      cta.innerHTML = rec ? '<i class="fas fa-user-doctor"></i>Search providers'
                          : '<i class="fas fa-briefcase"></i>Find Jobs';
      cta.dataset.page = rec ? "providers" : "jobs";
    }
    const title = $("#rec-title");
    if (title) title.textContent = rec ? "Live roles" : "Recommended Jobs";

    let total = 0;
    try {
      // A professional sees roles ranked against their own profile; a
      // recruiter just sees what is live on the board.
      const jobs = await get(rec ? "/api/jobs?limit=4"
                                 : "/api/jobs/recommended?limit=4");
      const sub = $("#rec-sub");
      if (sub) sub.textContent = rec
        ? "Pick a role to source ranked candidates against it"
        : ((jobs.items || []).some(j => j.fit_score > 0)
            ? "Matched to your licence, specialty and location"
            : "Complete your profile to get roles matched to you");
      total = jobs.total || 0;
      $("#dashboard-jobs").innerHTML = jobs.items.length ? jobs.items.map(jobRow).join("") : emptyRow(JOB_COLS, "No open roles yet",
               "New roles appear here as they are posted.", "fa-briefcase");
    } catch(e) { $("#dashboard-jobs").innerHTML = errorRow(JOB_COLS, "Could not load jobs"); }

    $("#dash-sub").textContent = S.profile
      ? `Welcome back, ${S.profile.first_name}.`
      : (rec ? "Recruiter workspace" : "Complete your profile to get matched.");

    if (rec) {
      // Everything a recruiter is measured on comes back in one call.
      $("#dash-metrics").innerHTML =
        metricTile(total.toLocaleString(), "Open roles", "providers")
        + metricTile("—", "Contacts revealed", "credits")
        + metricTile("—", "Shortlisted", "pools")
        + metricTile("—", "Conversations", "messages");
      try {
        const a = await get("/api/analytics/sourcing?days=30");
        $("#dash-metrics").innerHTML =
          metricTile(total.toLocaleString(), "Open roles", "providers")
          + metricTile(a.contacts.released_total, "Contacts revealed", "credits")
          + metricTile(a.pools.shortlisted, "Shortlisted", "pools")
          + metricTile(a.messaging.threads, "Conversations", "messages");
      } catch(e) { /* the tiles keep their placeholders */ }
      return;
    }

    const pct = `${(S.profile && S.profile.completion_score) || 0}%`;
    const seekerTiles = (open, apps, saved) =>
      metricTile(open, "Open roles", "jobs")
      + metricTile(apps, "Applications", "applications")
      + metricTile(saved, "Saved jobs", "applications")
      + metricTile(pct, "Profile", "profile");
    $("#dash-metrics").innerHTML = seekerTiles("—", "—", "—");
    if (!S.profile) return;
    // /api/jobs/recommended reports total = the page it returned, so the real
    // board count has to come from the unfiltered list.
    const [open, apps, saved] = await Promise.all([
      get("/api/jobs?limit=1").then(r => (r.total || 0).toLocaleString()).catch(() => "—"),
      get("/api/applications/mine").then(r => r.length).catch(() => "—"),
      get("/api/applications/saved").then(r => r.length).catch(() => "—"),
    ]);
    $("#dash-metrics").innerHTML = seekerTiles(open, apps, saved);
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
      $("#jobs-list").innerHTML = data.items.length ? data.items.map(jobRow).join("") : emptyRow(JOB_COLS, "No jobs match this search",
               "Try a broader title, or clear the state filter.", "fa-briefcase");
    } catch(e) { $("#jobs-list").innerHTML = errorRow(JOB_COLS, "Could not load jobs"); }
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
    $("#providers-grid").innerHTML = items.length ? items.map(providerRow).join("") : emptyRow(PROVIDER_COLS, "No providers match these filters",
               "Widen the radius, or drop a credential filter.", "fa-user-doctor");
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
        <button class="btn small" data-message="${p.profile_id}" title="Message this candidate"><i class="fas fa-comment-dots"></i></button>
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
    // A recruiter has no candidate profile — this page is their account, not a
    // "complete your profile to appear in search" flow (which would be wrong,
    // since recruiters aren't listed in the directory).
    if (isRecruiter()){ renderAccountView(); return; }
    if (!S.profile) {
      $("#profile-card").innerHTML = emptyState("No profile yet", "Add your details to appear in search.", "fa-id-card");
      loadPrivacy();   // the account-deletion danger zone still applies
      return;
    }
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
    renderSecurity();
    loadPrivacy();
  }
  // Recruiters see an Account page here, not a candidate profile: their sign-in
  // details plus the account-deletion controls (rendered by loadPrivacy).
  function renderAccountView(){
    const u = S.user || {};
    const head = $("#page-profile .section-head h1");
    if (head) head.textContent = "Account";
    const sub = $("#profile-sub");
    if (sub) sub.textContent = "Your sign-in and account settings";
    const edit = $("#profile-edit");
    if (edit) edit.classList.add("hidden");     // nothing candidate-shaped to edit
    const progress = $("#profile-progress");
    if (progress) progress.innerHTML = "";
    const creds = $("#profile-credentials");
    if (creds) creds.innerHTML = "";
    const since = u.created_at
      ? new Date(u.created_at).toLocaleDateString([], {year:"numeric", month:"short", day:"numeric"})
      : "";
    $("#profile-card").innerHTML = `<div class="profile-grid">
      ${fieldRow("Email", u.email)}
      ${fieldRow("Account type", "Recruiter")}
      ${fieldRow("Email verified", u.email_verified_at ? "Yes" : "Not verified")}
      ${fieldRow("Member since", since)}
    </div>`;
    renderSecurity();  // 2FA + change password
    loadPrivacy();     // account-deletion danger zone
  }

  // --- Security (2FA + password), for both roles ---------------------------
  async function refreshUser(){
    try { S.user = await get("/api/auth/me"); } catch(e) { /* keep the old copy */ }
  }
  function renderSecurity(){
    const box = $("#profile-security");
    if (!box) return;
    const on = !!(S.user && S.user.mfa_enabled);
    box.innerHTML = `<div class="privacy-wrap">
      <h3>Security</h3>
      <div class="sec-row">
        <div><strong>Two-factor authentication</strong>
          <div class="muted">${on
            ? "On — a code from your authenticator app is required when you sign in."
            : "Add a second step at sign-in using an authenticator app."}</div></div>
        <span class="spacer"></span>
        <span class="privacy-state ${on ? "on" : "off"}">${on ? "On" : "Off"}</span>
        <button class="btn ${on ? "danger" : "primary"} small" id="mfa-toggle">${on ? "Turn off" : "Enable"}</button>
      </div>
      <div class="sec-row">
        <div><strong>Password</strong>
          <div class="muted">Change the password you sign in with.</div></div>
        <span class="spacer"></span>
        <button class="btn small" id="pw-change">Change password</button>
      </div>
    </div>`;
    const mt = $("#mfa-toggle");
    if (mt) mt.onclick = on ? disableMfa : enableMfa;
    const pc = $("#pw-change");
    if (pc) pc.onclick = changePassword;
  }
  async function enableMfa(){
    let enroll;
    try { enroll = await post("/api/auth/mfa/enroll", {}); }
    catch(e) { return toast(e.message || "Could not start setup.", {kind:"err"}); }
    // No QR (the CSP blocks external libraries) — authenticator apps all accept
    // a setup key typed in by hand, so we show that.
    const v = await formDialog({
      title: "Turn on two-factor authentication",
      intro: "Add this setup key to an authenticator app (Google Authenticator, Authy, "
           + "1Password…), then enter the 6-digit code it shows. Setup key: " + enroll.secret,
      submit: "Verify & turn on",
      fields: [{name:"code", label:"6-digit code", required:true, wide:true,
                placeholder:"123456", max:6}],
    });
    if (!v) return;
    try {
      await post("/api/auth/mfa/verify", {code: (v.code || "").trim()});
      await refreshUser();
      toast("Two-factor authentication is on.", {title:"2FA enabled"});
      renderSecurity();
    } catch(e) {
      toast(e.status === 400 ? "That code didn't match — use the current one from your app."
          : (e.message || "That did not work."), {title:"Could not enable 2FA", kind:"err"});
    }
  }
  async function disableMfa(){
    const v = await formDialog({
      title: "Turn off two-factor authentication",
      intro: "Enter a current code from your authenticator app to confirm.",
      submit: "Turn off 2FA",
      fields: [{name:"code", label:"6-digit code", required:true, wide:true,
                placeholder:"123456", max:6}],
    });
    if (!v) return;
    try {
      await post("/api/auth/mfa/disable", {code: (v.code || "").trim()});
      await refreshUser();
      toast("Two-factor authentication is off.", {title:"2FA disabled"});
      renderSecurity();
    } catch(e) {
      toast(e.status === 400 ? "That code didn't match." : (e.message || "That did not work."),
            {title:"Could not disable 2FA", kind:"err"});
    }
  }
  async function changePassword(){
    const v = await formDialog({
      title: "Change your password",
      submit: "Change password",
      fields: [
        {name:"current_password", label:"Current password", type:"password", required:true, wide:true},
        {name:"new_password", label:"New password", type:"password", required:true, wide:true,
         placeholder:"At least 8 characters"},
        {name:"confirm", label:"Confirm new password", type:"password", required:true, wide:true},
      ],
    });
    if (!v) return;
    if ((v.new_password || "").length < 8)
      return toast("New password must be at least 8 characters.", {kind:"err"});
    if (v.new_password !== v.confirm)
      return toast("The new passwords don't match.", {kind:"err"});
    try {
      await post("/api/auth/change-password",
                 {current_password: v.current_password, new_password: v.new_password});
      toast("Your password has been changed.", {title:"Password updated"});
    } catch(e) {
      toast(e.status === 400 ? "Your current password is incorrect."
          : (e.message || "That did not work."), {title:"Could not change password", kind:"err"});
    }
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
        catch(e) { toast(e.message || "That did not work.", {title:"Something went wrong", kind:"err"}); }
      });
    } catch(e) { box.innerHTML = ""; }
  }
  async function addCredential(){
    const v = await formDialog({
      title: "Add a licence",
      intro: "Recruiters filter by licence and state. An expiry date lets us warn "
           + "you before it lapses.",
      submit: "Add licence",
      fields: [
        {name:"license_type", label:"Licence type", required:true, placeholder:"RN, LPN, MD, PT…"},
        {name:"state_code", label:"Issuing state", required:true, placeholder:"TX", max:2},
        {name:"license_number", label:"Licence number", hint:"optional"},
        {name:"expiry_date", label:"Expires", type:"date", hint:"optional", wide:true},
      ],
    });
    if (!v) return;
    try {
      await post("/api/profiles/me/licenses", {
        license_type: v.license_type.toUpperCase(),
        state_code: v.state_code.toUpperCase(),
        license_number: v.license_number || "",
        expiry_date: v.expiry_date || null});
      toast("Added to your profile.", {title:"Licence saved"});
      loadCredentials(); loadWallet();
    } catch(e) { toast(e.message, {title:"Could not add licence", kind:"err"}); }
  }

  // --- Credential Wallet (seeker) ------------------------------------------
  function walletStat(n, label, cls){
    return `<div class="wallet-stat ${cls || ""}"><b>${n}</b><span>${esc(label)}</span></div>`;
  }
  function expiryPill(status, days){
    const label = status === "expired" ? "Expired"
      : status === "expiring" ? `${days}d left`
      : status === "valid" ? "Valid" : "No expiry";
    return `<span class="cred-pill ${status}">${esc(label)}</span>`;
  }
  function licCard(l){
    const verif = l.verified
      ? `<span class="wallet-badge ok"><i class="fas fa-circle-check"></i>${esc((l.verification_status || "").replace(/_/g, " "))}</span>`
      : `<span class="wallet-badge">Unverified</span>`;
    return `<div class="wallet-card">
      <div class="wallet-main">
        <div class="wallet-title"><b>${esc(l.license_type)}</b><span class="wallet-state">${esc(l.state_code)}</span>
          ${l.is_compact ? `<span class="wallet-badge compact"><i class="fas fa-shield-halved"></i>Compact</span>` : ""}</div>
        <div class="wallet-sub">${esc(l.license_number || "No number on file")}${l.expiry_date ? ` · expires ${esc(String(l.expiry_date).slice(0,10))}` : ""}</div>
        <div class="wallet-badges">${verif}</div>
      </div>
      <div class="wallet-right">${expiryPill(l.status, l.days_left)}
        <button class="cred-del" data-lic-del="${esc(l.license_id)}" title="Remove"><i class="fas fa-xmark"></i></button></div>
    </div>`;
  }
  function certCard(x){
    return `<div class="wallet-card">
      <div class="wallet-main">
        <div class="wallet-title"><b>${esc(x.cert_name)}</b></div>
        <div class="wallet-sub">${x.expiry_date ? `expires ${esc(String(x.expiry_date).slice(0,10))}` : "No expiry on file"}</div>
      </div>
      <div class="wallet-right">${expiryPill(x.status, x.days_left)}
        <button class="cred-del" data-cert-del="${esc(x.cert_id)}" title="Remove"><i class="fas fa-xmark"></i></button></div>
    </div>`;
  }
  async function loadWallet(){
    const box = $("#wallet-body");
    if (!box) return;
    box.innerHTML = loading("Loading your credentials...");
    try {
      const c = await get("/api/profiles/me/credentials");
      S.wallet = c;
      const all = [...(c.licenses || []), ...(c.certifications || [])];
      const by = s => all.filter(x => x.status === s).length;
      box.innerHTML =
        `<div class="wallet-summary">
          ${walletStat(all.length, "Credentials")}
          ${walletStat(by("valid"), "Valid", "valid")}
          ${walletStat(by("expiring"), "Expiring soon", by("expiring") ? "expiring" : "")}
          ${walletStat(by("expired"), "Expired", by("expired") ? "expired" : "")}
        </div>`
        + ((c.alerts && c.alerts.length) ? `<div class="cred-alert"><i class="fas fa-triangle-exclamation"></i>
            ${c.alerts.map(a => `${esc(a.label)} ${a.status === "expired" ? "has expired" : `expires in ${a.days_left} days`}`).join(" · ")}</div>` : "")
        + (c.compact_eligible ? `<p class="pc-note"><i class="fas fa-shield-halved"></i>You hold a compact licence — eligible to practise in around 40 states.</p>` : "")
        + `<div class="wallet-section"><h2>Licences</h2>${
            (c.licenses || []).length ? `<div class="wallet-list">${c.licenses.map(licCard).join("")}</div>`
              : emptyState("No licences yet", "Add your licences so recruiters can find you and we can track expiry.", "fa-id-card")}</div>`
        + `<div class="wallet-section"><h2>Certifications</h2>${
            (c.certifications || []).length ? `<div class="wallet-list">${c.certifications.map(certCard).join("")}</div>`
              : emptyState("No certifications yet", "Add BLS, ACLS, CCRN and the rest to complete your profile.", "fa-certificate")}</div>`;
      $$("#wallet-body [data-lic-del]").forEach(b => b.onclick = async () => {
        try { await del(`/api/profiles/me/licenses/${b.dataset.licDel}`); loadWallet(); loadCredentials(); }
        catch(e) { toast(e.message || "That did not work.", {kind:"err"}); }
      });
      $$("#wallet-body [data-cert-del]").forEach(b => b.onclick = async () => {
        try { await del(`/api/profiles/me/certifications/${b.dataset.certDel}`); loadWallet(); }
        catch(e) { toast(e.message || "That did not work.", {kind:"err"}); }
      });
    } catch(e) { box.innerHTML = errorState("Could not load your credentials"); }
  }
  async function addCertification(){
    const v = await formDialog({
      title: "Add a certification",
      intro: "BLS, ACLS, PALS, CCRN, TNCC… An expiry date lets us warn you before it lapses.",
      submit: "Add certification",
      fields: [
        {name:"cert_name", label:"Certification", required:true, placeholder:"BLS, ACLS, CCRN…"},
        {name:"issuing_body", label:"Issuing body", hint:"optional", placeholder:"AHA, AACN…"},
        {name:"cert_number", label:"Number", hint:"optional"},
        {name:"expiry_date", label:"Expires", type:"date", hint:"optional", wide:true},
      ],
    });
    if (!v) return;
    try {
      await post("/api/profiles/me/certifications", {
        cert_name: v.cert_name, issuing_body: v.issuing_body || null,
        cert_number: v.cert_number || null, expiry_date: v.expiry_date || null});
      toast("Added to your credentials.", {title:"Certification saved"});
      loadWallet();
    } catch(e) { toast(e.message, {title:"Could not add certification", kind:"err"}); }
  }
  function copyCredentialSummary(){
    const c = S.wallet;
    if (!c || (!(c.licenses || []).length && !(c.certifications || []).length))
      return toast("Add a licence or certification first.", {kind:"err"});
    const name = S.profile ? `${S.profile.first_name} ${S.profile.last_name}`.trim() : "";
    const lines = [];
    if (name) lines.push(name);
    lines.push("Credentials (via HealthBoard)", "");
    if ((c.licenses || []).length){
      lines.push("Licences:");
      c.licenses.forEach(l => lines.push(`  - ${l.license_type} (${l.state_code})`
        + (l.is_compact ? " [compact]" : "") + (l.license_number ? " #" + l.license_number : "")
        + (l.expiry_date ? " - exp " + String(l.expiry_date).slice(0, 10) : "")));
    }
    if ((c.certifications || []).length){
      lines.push("Certifications:");
      c.certifications.forEach(x => lines.push(`  - ${x.cert_name}`
        + (x.expiry_date ? " - exp " + String(x.expiry_date).slice(0, 10) : "")));
    }
    const text = lines.join("\n");
    if (navigator.clipboard && navigator.clipboard.writeText){
      navigator.clipboard.writeText(text).then(
        () => toast("Paste it into an email or message to a recruiter.", {title:"Credential summary copied"}),
        () => toast("Could not copy automatically — select and copy manually.", {kind:"err"}));
    } else {
      toast("Copying isn't supported in this browser.", {kind:"err"});
    }
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
      $("#feed-list").innerHTML = (data.items || data || []).map(p => `<div class="list-row"><div><strong>${esc(p.author_name || "HealthBoard")}</strong><div class="muted">${esc(p.body || "")}</div></div></div>`).join("") || emptyState("Nothing posted yet", "This is where updates from the "
                 + "community will appear.", "fa-comments");
    } catch(e) { $("#feed-list").innerHTML = errorState("Could not load the feed"); }
  }
  async function loadNotifications(){
    const box = $("#notifications-list");
    box.innerHTML = loading("Loading notifications...");
    try {
      const data = await get("/api/notifications");
      if (!data || !data.length){
        box.innerHTML = emptyState("You are all caught up",
          "Replies, matches and licence reminders land here.", "fa-bell");
        return;
      }
      const unread = data.filter(n => !n.is_read).length;
      box.innerHTML =
        (unread ? `<div class="notif-head"><span class="muted">${unread} unread</span>
           <button class="btn ghost small" id="notif-read-all">Mark all as read</button></div>` : "")
        + data.map(n => `<div class="list-row notif-row ${n.is_read ? "" : "unread"}" data-notif="${esc(n.notification_id)}">
            <div><strong>${esc(n.title)}</strong><div class="muted">${esc(n.body || "")}</div>
              <div class="notif-time">${esc(shortTime(n.created_at))}</div></div>
            ${n.is_read ? "" : `<span class="notif-dot" title="Unread"></span>`}
          </div>`).join("");
      const all = $("#notif-read-all");
      if (all) all.onclick = async () => {
        try { await post("/api/notifications/read-all", {}); }
        catch(e) { return toast(e.message || "That did not work.", {kind:"err"}); }
        loadNotifications();
        refreshNotificationBadge();
      };
      $$("#notifications-list [data-notif]").forEach(row => row.onclick = async () => {
        if (!row.classList.contains("unread")) return;
        try {
          await post(`/api/notifications/${row.dataset.notif}/read`, {});
          row.classList.remove("unread");
          const dot = row.querySelector(".notif-dot"); if (dot) dot.remove();
          refreshNotificationBadge();
        } catch(e) { /* leave the row as-is on failure */ }
      });
    } catch(e) { box.innerHTML = errorState("Could not load notifications"); }
  }
  async function loadEmployer(){
    $("#employer-panel").innerHTML = loading("Loading recruiter dashboard...");
    try {
      const d = await get("/api/employers/me/dashboard");
      $("#employer-sub").textContent = d.employer ? d.employer.org_name : "Create an organization first";
      S.employer = d.employer || null;
      $("#employer-panel").innerHTML = d.employer
        ? `<div class="emp-summary">
             <div class="emp-head">
               <div class="emp-org">
                 <span class="emp-org-label">Organization</span>
                 <h2 class="emp-org-name">${esc(d.employer.org_name)}</h2>
                 ${d.employer.is_verified ? `<span class="emp-verified"><i class="fas fa-circle-check"></i>Verified</span>` : ""}
               </div>
               <button class="btn ghost small" id="org-manage"><i class="fas fa-building-user"></i>Manage organization</button>
             </div>
             <div class="emp-kpis">
               ${[["Open orders", d.kpis.jobs], ["Applications", d.kpis.applications],
                  ["Interviews", d.kpis.interviews], ["Offers", d.kpis.offers],
                  ["Hired", d.kpis.hired]].map(([l, n]) =>
                 `<div class="emp-kpi"><b>${esc(n ?? 0)}</b><span>${esc(l)}</span></div>`).join("")}
             </div>
           </div>`
        : `<div class="match-empty"><i class="fas fa-building"></i><h3>No organization yet</h3>
           <p>Create one to post job orders and source candidates against them.</p>
           <button class="btn primary" id="org-create"><i class="fas fa-plus"></i>Create organization</button></div>`;
      const oc = $("#org-create");
      if (oc) oc.onclick = createOrg;
      const om = $("#org-manage");
      if (om) om.onclick = () => showPage("orgadmin");
      renderEmployerJobs(d.jobs || []);
    } catch(e) { $("#employer-panel").innerHTML = errorState("Could not load the employer dashboard"); }
  }

  const ORG_ROLE_LABEL = {owner:"Owner", admin:"Admin", manager:"Manager", recruiter:"Member"};
  async function setMemberRole(userId, role){
    try {
      await patch(`/api/employers/${S.employer.employer_id}/members/${userId}`, {member_role: role});
      toast("Role updated.", {title:"Team"});
      afterTeamChange();
    } catch(e) { toast(e.message || "Could not change the role.", {title:"Role", kind:"err"}); afterTeamChange(); }
  }
  // Team management lives on the Organization page; refresh it after any change.
  function afterTeamChange(){
    if (S.employer) loadOrgAdmin();
  }

  // Dedicated organization-admin page: org info, members & roles, usage/billing —
  // all scoped to the user's own organization and gated by their org role.
  async function loadOrgAdmin(){
    const box = $("#orgadmin-panel");
    if (!box) return;
    box.innerHTML = loading("Loading your organization…");
    try {
      let kpis = {};
      try {
        const dash = await get("/api/employers/me/dashboard");
        S.employer = dash.employer || S.employer;
        kpis = dash.kpis || {};
      } catch(_){}
      const emp = S.employer;
      if (!emp || !emp.employer_id){
        $("#orgadmin-sub").textContent = "";
        box.innerHTML = `<div class="match-empty"><i class="fas fa-building"></i><h3>No organization yet</h3>
          <p>Create one from Job Orders to manage members, roles and usage.</p>
          <button class="btn primary" data-page="employer"><i class="fas fa-arrow-right"></i>Go to Job Orders</button></div>`;
        return;
      }
      const mem = await get(`/api/employers/${emp.employer_id}/members`);
      const perms = mem.permissions || {manage_members:false, manage_roles:false, analytics:false, settings:false};
      S.teamPerms = perms;
      const myRole = mem.my_role || "recruiter";
      let invites = [];
      if (perms.manage_members){
        try { invites = (await get(`/api/employers/${emp.employer_id}/invites`)).items || []; } catch(_){}
      }
      let usage = null;
      if (perms.analytics){
        try { usage = await get(`/api/employers/${emp.employer_id}/usage`); } catch(_){}
      }
      $("#orgadmin-sub").textContent = emp.org_name;

      const kpiCards = [["Members", (mem.items || []).length],
                        ["Open jobs", kpis.jobs || 0],
                        ["Applications", kpis.applications || 0]];
      if (usage) kpiCards.push(["Team credits", usage.totals.credits],
                               ["Contacts revealed", usage.totals.reveals]);

      const roleCell = m => m.is_owner
        ? `<span class="badge accent">Owner</span>`
        : perms.manage_roles
          ? `<select class="tm-role" data-member-role="${esc(m.user_id)}">${
              ["recruiter","manager","admin"].map(r =>
                `<option value="${r}"${r === m.member_role ? " selected" : ""}>${ORG_ROLE_LABEL[r]}</option>`).join("")
            }</select>`
          : `<span class="badge">${esc(m.role_label || ORG_ROLE_LABEL[m.member_role] || m.member_role)}</span>`;
      const memberRows = (mem.items || []).map(m => `<tr>
        <td><div class="cell-name">${esc(m.name || m.email || "Teammate")}</div><div class="cell-sub">${esc(m.email || "")}</div></td>
        <td>${roleCell(m)}</td>
        <td class="td-actions">${(perms.manage_members && !m.is_owner)
          ? `<button class="btn small" data-member-remove="${esc(m.user_id)}" title="Remove from organization"><i class="fas fa-user-minus"></i></button>` : ""}</td>
      </tr>`).join("");
      const inviteRows = invites.map(i => `<tr>
        <td><div class="cell-name">${esc(i.email)}</div><div class="cell-sub">Invitation pending</div></td>
        <td><span class="badge">${esc(ORG_ROLE_LABEL[i.role] || i.role)}</span></td>
        <td class="td-actions"><button class="btn small" data-invite-revoke="${esc(i.invite_id)}" title="Revoke"><i class="fas fa-xmark"></i></button></td>
      </tr>`).join("");

      box.innerHTML = `
        <div class="oa-head">
          <div class="oa-org">
            <span class="oa-label">Organization</span>
            <h2>${esc(emp.org_name)} ${emp.is_verified ? `<span class="emp-verified"><i class="fas fa-circle-check"></i>Verified</span>` : ""}</h2>
            <div class="oa-meta">${esc([emp.org_type, [emp.city, emp.state_code].filter(Boolean).join(", ")].filter(Boolean).join(" · ") || "—")}</div>
          </div>
          <div class="oa-roleplate">
            <span class="oa-label">Your access</span>
            <div><span class="badge accent">${esc(ORG_ROLE_LABEL[myRole] || myRole)}</span>
              ${perms.settings ? `<button class="btn ghost small" id="oa-edit"><i class="fas fa-pen"></i>Edit organization</button>` : ""}</div>
          </div>
        </div>
        <div class="emp-kpis" style="margin:16px 0 4px">${kpiCards.map(([l, n]) =>
          `<div class="emp-kpi"><b>${Number(n || 0).toLocaleString()}</b><span>${esc(l)}</span></div>`).join("")}</div>

        <div class="an-section" style="margin-top:22px">
          <div class="team-head"><h2>Members &amp; roles</h2>${perms.manage_members
            ? `<button class="btn ghost small" id="oa-invite"><i class="fas fa-user-plus"></i>Invite teammate</button>` : ""}</div>
          <p class="team-note">${perms.manage_roles
            ? "Admins manage roles &amp; billing; managers manage members; members use the tools."
            : "The people in your organization. Talent pools, submissions and jobs are shared across the team."}</p>
          <div class="table-wrap"><table class="table">
            <thead><tr><th>Member</th><th>Role</th><th class="th-actions"></th></tr></thead>
            <tbody>${memberRows}</tbody></table></div>
          ${invites.length ? `<div class="team-head" style="margin-top:20px"><h2>Pending invitations</h2></div>
            <div class="table-wrap"><table class="table"><thead><tr><th>Email</th><th>Role</th><th class="th-actions"></th></tr></thead>
            <tbody>${inviteRows}</tbody></table></div>` : ""}
        </div>

        ${usage ? `<div class="an-section" style="margin-top:22px"><h2>Usage &amp; billing</h2>
          <div class="table-wrap"><table class="table">
            <thead><tr><th>Member</th><th>Role</th><th>Credits left</th><th>Contacts revealed</th></tr></thead>
            <tbody>${usage.members.map(m => `<tr>
              <td><div class="cell-name">${esc(m.name || m.email || "—")}</div><div class="cell-sub">${esc(m.email || "")}</div></td>
              <td><span class="badge">${esc(m.role_label || ORG_ROLE_LABEL[m.role] || m.role)}</span></td>
              <td><b>${Number(m.credits).toLocaleString()}</b></td>
              <td>${Number(m.reveals).toLocaleString()}</td></tr>`).join("")}</tbody></table></div>
          <p class="team-note">${Number(usage.totals.credits).toLocaleString()} credits across the team · ${Number(usage.totals.reveals).toLocaleString()} contacts revealed. Need more credits? Contact your HealthBoard administrator.</p></div>` : ""}
      `;
      const ed = $("#oa-edit"); if (ed) ed.onclick = () => editOrg(emp);
      const iv = $("#oa-invite"); if (iv) iv.onclick = inviteTeammate;
      $$("#orgadmin-panel [data-member-remove]").forEach(b => b.onclick = () => removeTeammate(b.dataset.memberRemove));
      $$("#orgadmin-panel [data-invite-revoke]").forEach(b => b.onclick = () => revokeInvite(b.dataset.inviteRevoke));
      $$("#orgadmin-panel [data-member-role]").forEach(s => s.onchange = () => setMemberRole(s.dataset.memberRole, s.value));
    } catch(e) { box.innerHTML = errorState("Could not load your organization.", e.message || ""); }
  }

  async function inviteTeammate(){
    if (!S.employer) return;
    // Managers can invite plain members; only owners/admins can grant elevated roles.
    const canElevate = !!(S.teamPerms && S.teamPerms.manage_roles);
    const roleOptions = canElevate ? [
      {value:"recruiter", label:"Member — source and submit candidates"},
      {value:"manager", label:"Manager — also manage members"},
      {value:"admin", label:"Admin — also manage roles & billing"},
    ] : [{value:"recruiter", label:"Member — source and submit candidates"}];
    const v = await formDialog({
      title: "Invite a teammate",
      intro: "They'll get an email invitation to join your organisation. They don't "
           + "need a HealthBoard account yet — they can create one when they accept.",
      submit: "Send invitation",
      fields: [
        {name:"email", label:"Their email", type:"email", required:true, wide:true,
         placeholder:"colleague@youragency.com"},
        {name:"role", label:"Role", type:"select", value:"recruiter", options: roleOptions},
      ],
    });
    if (!v) return;
    try {
      await post(`/api/employers/${S.employer.employer_id}/invites`,
                 {email: v.email.trim(), role: v.role || "recruiter"});
      toast(`Invitation sent to ${v.email.trim()}.`, {title:"Teammate invited"});
      afterTeamChange();
    } catch(e) {
      toast(e.status === 409 ? "They're already on your team."
          : (e.message || "Could not send the invitation."),
          {title:"Could not invite", kind:"err"});
    }
  }
  async function revokeInvite(inviteId){
    try { await del(`/api/employers/${S.employer.employer_id}/invites/${inviteId}`); afterTeamChange(); }
    catch(e){ toast(e.message || "That did not work.", {kind:"err"}); }
  }
  async function removeTeammate(userId){
    if (!await confirmDialog({
      title: "Remove teammate",
      body: "They lose access to this organisation's shared pools, submissions and "
          + "jobs. Their own account is not affected.",
      confirm: "Remove", danger: true})) return;
    try { await del(`/api/employers/${S.employer.employer_id}/members/${userId}`); afterTeamChange(); }
    catch(e) { toast(e.message || "That did not work.", {title:"Something went wrong", kind:"err"}); }
  }
  function jobStatusPill(status){
    const cls = status === "active" ? "ok" : status === "closed" ? "no" : "";
    const label = status ? status[0].toUpperCase() + status.slice(1) : "—";
    return `<span class="status-pill ${cls}">${esc(label)}</span>`;
  }

  function renderEmployerJobs(jobs){
    const box = $("#employer-jobs");
    if (!box) return;
    if (!S.employer){ box.innerHTML = ""; return; }
    // Register titles so the applicant/detail views can label themselves.
    jobs.forEach(j => S.jobsById.set(j.job_id, j));
    box.innerHTML = `<div class="an-section">
      <div class="emp-jobs-head">
        <h2>Your job orders</h2>
        <div class="emp-jobs-tools">
          <button class="btn ghost small" id="jobs-template" title="Download an Excel template to fill in"><i class="fas fa-file-arrow-down"></i>Template</button>
          <button class="btn ghost small" id="jobs-upload" title="Post many jobs at once from Excel or CSV"><i class="fas fa-file-excel"></i>Upload Excel</button>
          ${jobs.length ? `<button class="btn ghost small danger" id="jobs-delete-all" title="Delete every job order"><i class="fas fa-trash"></i>Delete all</button>` : ""}
          <input type="file" id="jobs-file" accept=".xlsx,.xls,.csv" hidden>
        </div>
      </div>${
      jobs.length
        ? `<div class="table-wrap"><table class="table">
            <thead><tr><th>Role</th><th>Type</th><th>Location</th><th>Pay</th><th title="Inbound applicants, if you post this order publicly">Applied</th><th>Status</th><th class="th-actions"></th></tr></thead>
            <tbody>${jobs.map(j => {
              const closed = j.status !== "active";
              const loc = [j.city, j.state_code].filter(Boolean).join(", ") || "—";
              const n = j.application_count || 0;
              return `<tr class="${closed ? "row-muted" : ""}">
                <td><div class="cell-name"><button class="linklike" data-jobview="${esc(j.job_id)}">${esc(j.title)}</button></div>
                    <div class="cell-sub">${esc(j.specialty || j.profession_type || "")}</div></td>
                <td><span class="badge accent">${esc(j.job_type || "-")}</span></td>
                <td>${esc(loc)}</td>
                <td>${j.pay_rate_max ? `<strong>$${Math.round(j.pay_rate_max)}/hr</strong>` : "—"}</td>
                <td><button class="btn small ghost" data-applicants="${esc(j.job_id)}" title="Inbound applicants"><i class="fas fa-users"></i>${n}</button></td>
                <td>${jobStatusPill(j.status)}</td>
                <td class="td-actions">
                  ${closed ? "" : `<button class="btn small primary" data-source="${esc(j.job_id)}" title="Source matching candidates"><i class="fas fa-bolt"></i>Source</button>`}
                  <button class="btn small" data-job-edit="${esc(j.job_id)}" title="Edit order"><i class="fas fa-pen"></i></button>
                  ${closed ? "" : `<button class="btn small" data-job-close="${esc(j.job_id)}" title="Close order"><i class="fas fa-xmark"></i></button>`}
                </td>
              </tr>`;
            }).join("")}</tbody></table></div>`
        : `<p class="muted" style="font-size:13px">No job orders yet — create one to start sourcing candidates against it.</p>`}</div>`;
    $$("#employer-jobs [data-job-edit]").forEach(b => b.onclick = () => editJob(b.dataset.jobEdit));
    $$("#employer-jobs [data-job-close]").forEach(b => b.onclick = () => closeJob(b.dataset.jobClose));
    const jt = $("#jobs-template");
    if (jt) jt.onclick = downloadJobTemplate;
    const ju = $("#jobs-upload"), jf = $("#jobs-file");
    if (ju && jf){
      ju.onclick = () => jf.click();
      jf.onchange = () => { if (jf.files[0]) bulkUploadJobs(jf.files[0]); jf.value = ""; };
    }
    const jda = $("#jobs-delete-all");
    if (jda) jda.onclick = deleteAllJobs;
  }

  async function downloadJobTemplate(){
    try {
      const res = await fetch("/api/jobs/template", {headers:{Authorization:"Bearer " + token()}, cache:"no-store"});
      if (!res.ok) throw new Error("Could not generate the template");
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url; a.download = "healthboard-jobs-template.xlsx";
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 2000);
    } catch(e){ toast(e.message || "Download failed.", {title:"Template", kind:"err"}); }
  }

  async function bulkUploadJobs(file){
    if (!file || !S.employer) return;
    const fd = new FormData();
    fd.append("file", file);
    toast(`Reading ${file.name}…`, {title:"Bulk upload"});
    try {
      const r = await api("POST", `/api/jobs/bulk?employer_id=${S.employer.employer_id}`, fd);
      let msg = `${r.created} job order${r.created === 1 ? "" : "s"} created`;
      if (r.skipped) msg += ` · ${r.skipped} blank row${r.skipped === 1 ? "" : "s"} skipped`;
      if (r.failed) msg += ` · ${r.failed} with errors`;
      toast(msg, {title:"Bulk upload", kind: r.failed ? "err" : "ok"});
      loadEmployer();
      if (r.errors && r.errors.length)
        infoDialog("Some rows couldn't be imported",
          [["Created", r.created], ["Skipped", r.skipped], ["Errors", r.failed],
           ...r.errors.map((e, i) => [`Problem ${i + 1}`, e])]);
    } catch(e){
      toast(e.message || "Upload failed.", {title:"Bulk upload", kind:"err"});
    }
  }

  async function deleteAllJobs(){
    if (!S.employer) return;
    const ok = await confirmDialog({
      title: "Delete all job orders?",
      body: "This permanently deletes every job order for " + (S.employer.org_name || "your organization")
          + ", along with their applications and saved-job records. This cannot be undone.",
      confirm: "Delete everything", danger: true,
    });
    if (!ok) return;
    try {
      const r = await del(`/api/jobs/all?employer_id=${S.employer.employer_id}`);
      toast(`Deleted ${r.deleted} job order${r.deleted === 1 ? "" : "s"}.`, {title:"Job orders cleared"});
      loadEmployer();
    } catch(e){
      toast(e.message || "Could not delete the jobs.", {title:"Delete failed", kind:"err"});
    }
  }
  async function createOrg(){
    const v = await formDialog({
      title: "Create your organization",
      intro: "Job orders are posted under your organization, and it is what groups your team.",
      submit: "Create",
      fields: [{name:"org_name", label:"Organization name", required:true, wide:true,
                placeholder:"Radixsol Staffing"}],
    });
    if (!v) return;
    try {
      await post("/api/employers", {org_name: v.org_name});
      toast("You can post jobs now.", {title:"Organization created"});
      loadEmployer();
    } catch(e) { toast(e.message, {title:"Could not create", kind:"err"}); }
  }
  async function editOrg(emp){
    if (!emp || !emp.employer_id) return;
    const v = await formDialog({
      title: "Edit your organization",
      submit: "Save changes",
      fields: [
        {name:"org_name", label:"Organization name", required:true, wide:true, value:emp.org_name || ""},
        {name:"org_type", label:"Type", value:emp.org_type || "", placeholder:"Staffing agency, Hospital…"},
        {name:"city", label:"City", value:emp.city || ""},
        {name:"state_code", label:"State", value:emp.state_code || "", max:2},
        {name:"website_url", label:"Website", wide:true, value:emp.website_url || "", placeholder:"https://…"},
        {name:"description", label:"About", type:"textarea", wide:true, value:emp.description || "",
         placeholder:"What your organization does, and the roles you staff."},
      ],
    });
    if (!v) return;
    try {
      await patch(`/api/employers/${emp.employer_id}`, {
        org_name: v.org_name,
        org_type: v.org_type || null,
        city: v.city || null,
        state_code: v.state_code ? v.state_code.toUpperCase() : null,
        website_url: v.website_url || null,
        description: v.description || null,
      });
      toast("Your organization is updated.", {title:"Saved"});
      loadEmployer();
      const oa = $("#page-orgadmin");
      if (oa && oa.classList.contains("active")) loadOrgAdmin();
    } catch(e) { toast(e.message, {title:"Could not save", kind:"err"}); }
  }
  const JOB_TYPE_OPTIONS = [["travel","Travel"],["staff","Staff"],["per_diem","Per diem"],["contract","Contract"]];
  const SHIFT_OPTIONS = [["","Any shift"],["Day","Day"],["Night","Night"],["Evening","Evening"],["Rotating","Rotating"],["Weekend","Weekend"]];

  // One field set drives both posting and editing. A role with a licence,
  // specialty and description scores and reads far better than a bare title —
  // the licence and specialty are what the matching engine ranks on, and the
  // description is what a candidate reads before applying.
  function jobFields(j = {}){
    return [
      {name:"title", label:"Job title", required:true, wide:true,
       value:j.title || "", placeholder:"ICU Registered Nurse"},
      {name:"profession_type", label:"Licence required", value:j.profession_type || "", placeholder:"RN"},
      {name:"specialty", label:"Specialty", value:j.specialty || "", placeholder:"ICU"},
      {name:"city", label:"City", value:j.city || "", placeholder:"Austin"},
      {name:"state_code", label:"State", value:j.state_code || "", placeholder:"TX", max:2},
      {name:"pay_rate_min", label:"Pay from ($/hr)", type:"number", step:"1", value:j.pay_rate_min ?? ""},
      {name:"pay_rate_max", label:"Pay to ($/hr)", type:"number", step:"1", value:j.pay_rate_max ?? ""},
      {name:"job_type", label:"Type", type:"select", options:JOB_TYPE_OPTIONS, value:j.job_type || "travel"},
      {name:"shift_type", label:"Shift", type:"select", options:SHIFT_OPTIONS, value:j.shift_type || ""},
      {name:"description", label:"Description", type:"textarea", wide:true, value:j.description || "",
       placeholder:"The unit, the schedule, the caseload, and what you're looking for."},
      {name:"is_urgent", label:"Mark as urgent", type:"checkbox", value:!!j.is_urgent},
    ];
  }
  function jobBody(v){
    const lo = parseFloat(v.pay_rate_min), hi = parseFloat(v.pay_rate_max);
    return {
      title: v.title,
      job_type: v.job_type || "travel",
      pay_unit: "hourly",
      is_urgent: !!v.is_urgent,
      profession_type: v.profession_type ? v.profession_type.toUpperCase() : null,
      specialty: v.specialty || null,
      city: v.city || null,
      state_code: v.state_code ? v.state_code.toUpperCase() : null,
      shift_type: v.shift_type || null,
      description: v.description || null,
      pay_rate_min: isNaN(lo) ? (isNaN(hi) ? null : hi) : lo,
      pay_rate_max: isNaN(hi) ? (isNaN(lo) ? null : lo) : hi,
    };
  }

  async function postJob(){
    if (!S.employer) return toast("Create an organization first.", {kind:"err"});
    const v = await formDialog({title:"New job order", submit:"Create order", fields: jobFields()});
    if (!v) return;
    try {
      await post(`/api/jobs?employer_id=${encodeURIComponent(S.employer.employer_id)}`, jobBody(v));
      toast(`"${v.title}" is open — source candidates against it now.`, {title:"Job order created"});
      loadEmployer();
      loadJobs();
    } catch(e) { toast(e.message, {title:"Could not post job", kind:"err"}); }
  }

  async function editJob(jobId){
    let job;
    try { job = await get(`/api/jobs/${jobId}`); }
    catch(e) { return toast(e.message || "Could not load the role.", {kind:"err"}); }
    const v = await formDialog({title:"Edit job order", submit:"Save changes", fields: jobFields(job)});
    if (!v) return;
    try {
      await patch(`/api/jobs/${jobId}`, jobBody(v));
      toast("Your changes are live.", {title:"Order updated"});
      loadEmployer();
      loadJobs();
    } catch(e) { toast(e.message, {title:"Could not update order", kind:"err"}); }
  }

  async function closeJob(jobId){
    const job = S.jobsById.get(jobId);
    if (!await confirmDialog({
      title: "Close this job order",
      body: `"${(job && job.title) || "This order"}" will stop appearing for sourcing and stop `
          + "accepting applications. Anyone you've already sourced or received stays with you.",
      confirm: "Close order", danger: true})) return;
    try {
      await del(`/api/jobs/${jobId}`);
      toast("The order is closed.", {title:"Order closed"});
      loadEmployer();
      loadJobs();
    } catch(e) { toast(e.message || "That did not work.", {title:"Could not close order", kind:"err"}); }
  }

  // Full role detail — what a candidate reads before applying, and what a
  // recruiter opens to check a posting. Rows across the app link here.
  async function openJobDetail(jobId){
    $("#modal-root").innerHTML = `<div class="modal"><div class="modal-card job-detail-card">
      <div class="dlg-body">${loading("Loading role…")}</div></div></div>`;
    let job;
    try { job = await get(`/api/jobs/${jobId}`); }
    catch(e) { $("#modal-root").innerHTML = ""; return toast(e.message || "Could not load the role.", {kind:"err"}); }
    S.jobsById.set(jobId, job);
    const rec = isRecruiter();
    const loc = [job.city, job.state_code].filter(Boolean).join(", ") || "Location flexible";
    const pay = job.pay_rate_max
      ? (job.pay_rate_min && job.pay_rate_min !== job.pay_rate_max
          ? `$${Math.round(job.pay_rate_min)}–$${Math.round(job.pay_rate_max)}/hr`
          : `$${Math.round(job.pay_rate_max)}/hr`)
      : "Pay not listed";
    const facts = [
      ["Type", job.job_type], ["Shift", job.shift_type], ["Specialty", job.specialty],
      ["Licence", job.profession_type], ["Location", loc], ["Pay", pay],
    ].filter(([, val]) => val);
    const reqs = job.requirements && typeof job.requirements === "object"
      ? Object.entries(job.requirements) : [];
    $("#modal-root").innerHTML = `
      <div class="modal"><div class="modal-card job-detail-card">
        <div class="modal-head">
          <div><strong>${esc(job.title)}</strong>${job.is_urgent ? ` <span class="badge coral">Urgent</span>` : ""}
            <div class="muted small">${esc(loc)}</div></div>
          <button class="icon-btn" data-dlg-x><i class="fas fa-xmark"></i></button>
        </div>
        <div class="dlg-body">
          <div class="job-detail-facts">${facts.map(([k, val]) =>
            `<div><span class="muted">${esc(k)}</span><strong>${esc(val)}</strong></div>`).join("")}</div>
          <div id="jd-pay"></div>
          ${job.description
            ? `<div class="job-detail-desc"><h4>About this role</h4><p>${esc(job.description)}</p></div>`
            : `<p class="muted">No description was added for this role.</p>`}
          ${(job.benefits && job.benefits.length)
            ? `<div class="job-detail-desc"><h4>Benefits</h4><ul>${job.benefits.map(b => `<li>${esc(b)}</li>`).join("")}</ul></div>` : ""}
          ${reqs.length
            ? `<div class="job-detail-desc"><h4>Requirements</h4><ul>${reqs.map(([k, val]) => `<li>${esc(k)}: ${esc(val)}</li>`).join("")}</ul></div>` : ""}
        </div>
        <div class="dlg-foot"><span class="spacer"></span>
          <button type="button" class="btn ghost" data-dlg-x>Close</button>
          ${rec
            ? `<button type="button" class="btn primary" id="jd-source"><i class="fas fa-bolt"></i>Source candidates</button>`
            : `<button type="button" class="btn" id="jd-save"><i class="far fa-bookmark"></i>Save</button>
               <button type="button" class="btn" id="jd-message"><i class="fas fa-comment-dots"></i>Message recruiter</button>
               <button type="button" class="btn primary" id="jd-apply">Apply</button>`}
        </div>
      </div></div>`;
    $$("#modal-root [data-dlg-x]").forEach(b => b.onclick = () => { $("#modal-root").innerHTML = ""; });
    $("#modal-root .modal").addEventListener("click", e => {
      if (e.target.classList.contains("modal")) $("#modal-root").innerHTML = "";
    });
    const src = $("#jd-source");
    if (src) src.onclick = () => { $("#modal-root").innerHTML = ""; sourceForJob(jobId); };
    const ap = $("#jd-apply");
    if (ap) ap.onclick = () => { $("#modal-root").innerHTML = ""; applyJob(jobId); };
    const jmsg = $("#jd-message");
    if (jmsg) jmsg.onclick = () => { $("#modal-root").innerHTML = ""; messageAboutJob(jobId); };
    const sv = $("#jd-save");
    if (sv) sv.onclick = async () => {
      try { await post(`/api/jobs/${jobId}/save`, {}); toast("Saved to your list.", {title:"Job saved"}); }
      catch(e) { toast(e.message || "Could not save.", {kind:"err"}); }
    };
    loadJobPayEstimate(jobId);
  }
  async function loadJobPayEstimate(jobId){
    const box = $("#jd-pay");
    if (!box) return;
    try {
      const p = await get(`/api/jobs/${jobId}/pay-estimate`);
      if (!p || !p.available){ box.innerHTML = ""; return; }
      const ot = p.overtime || {};
      const caNote = ot.mode === "california"
        ? `<div class="jd-pay-ca"><i class="fas fa-scale-balanced"></i>Includes California daily overtime — ${ot.straight_hours}h regular + ${ot.ot_hours}h at 1.5×${ot.dt_hours ? ` + ${ot.dt_hours}h at 2×` : ""}.</div>`
        : "";
      box.innerHTML = `<div class="jd-pay-card">
        <div class="jd-pay-head"><i class="fas fa-money-bill-trend-up"></i>Estimated take-home
          <span class="jd-pay-src ${p.gsa_live ? "live" : ""}">${p.gsa_live ? "Live GSA rate" : "GSA estimate"}</span></div>
        <div class="jd-pay-big">${payMoney(p.weekly_net)}<small>/week net</small></div>
        <div class="jd-pay-rows">
          <span>Weekly gross</span><b>${payMoney(p.weekly_total)}</b>
          <span>of which tax-free per-diem</span><b>${payMoney(p.weekly_tax_free)}</b>
          <span>Taxable pay</span><b>${payMoney(p.weekly_taxable_gross)}</b>
        </div>
        ${caNote}
        <div class="jd-pay-foot">Estimate for ${p.hours_per_week}h/wk at $${p.hourly}/hr in ${esc([p.city,p.state_code].filter(Boolean).join(", "))}. Tax-free stipend needs a qualifying tax home. Not tax advice.</div>
      </div>`;
    } catch(e){ box.innerHTML = ""; }
  }

  async function messageAboutJob(jobId){
    const v = await formDialog({
      title: "Message the recruiter",
      intro: "Ask a question about this role. This starts a conversation with the "
           + "recruiter who posted it.",
      submit: "Send",
      fields: [{name:"body", label:"Your message", type:"textarea", required:true, wide:true,
                placeholder:"Hi — I'm interested in this role and wanted to ask…"}],
    });
    if (!v) return;
    try {
      const thread = await post("/api/messages/threads", {job_id: jobId, body: v.body});
      showPage("messages");
      setTimeout(() => openThread(thread.thread_id), 400);
    } catch(e){ toast(e.message || "Could not message the recruiter.", {kind:"err", ms:6500}); }
  }
  async function applyJob(id){
    // A candidate applies with their profile + résumé, so make sure they exist
    // and let them add a note — applying used to fire silently with nothing.
    if (!S.profile){
      toast("Add your details before applying to roles.", {title:"Complete your profile", kind:"err"});
      return showPage("profile");
    }
    const job = (S.jobsById && S.jobsById.get(id)) || null;
    const hasResume = !!S.profile.resume_url;
    const v = await formDialog({
      title: job ? `Apply — ${job.title}` : "Apply to this role",
      intro: hasResume
        ? "Your profile and résumé on file are sent with this application. Add a note if you'd like."
        : "Your profile is sent with this application. Upload a résumé first (Resume tab) so the employer sees your full background.",
      submit: "Submit application",
      fields: [
        {name:"cover_letter", label:"Message to the employer", type:"textarea", wide:true,
         placeholder:"Why you're a strong fit for this role (optional)"},
      ],
    });
    if (!v) return;
    try {
      await post(`/api/jobs/${id}/apply`, {cover_letter: v.cover_letter || null});
      toast("The employer has your application — track it under My Applications.",
            {title:"Application submitted"});
      loadDashboard();
      if ($("#page-applications").classList.contains("active")) loadApplications();
    } catch(e) {
      toast(e.status === 409 ? "You already applied to this job." : (e.message || "That did not work."),
            {kind:"err"});
    }
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
    } catch(e) { toast(e.message || "The file could not be uploaded.",
                       {title:"Upload failed", kind:"err"}); }
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
        toast(e.message || "That did not work.", {title:"Something went wrong", kind:"err"});
        refreshCredits();
      } else {
        toast(e.message || "The contact details were not released.",
              {title:"Reveal failed", kind:"err"});
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
    // A query typed from the hero (no results on screen yet — e.g. right after
    // "New search") is a fresh search: never inherit a previous conversation's
    // filters. Refinement only applies once results are already showing.
    const fresh = !$("#page-ai").classList.contains("has-results")
               || !$("#ai-thread").children.length;
    if (fresh) S.aiContext = null;
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

  // ---- Job AI: natural-language job search for job seekers -----------------
  function jaiPay(j){
    if (!j.pay_rate_max) return "";
    const unit = j.pay_unit === "hourly" ? "/hr" : (j.pay_unit === "weekly" ? "/wk" : "");
    const lo = j.pay_rate_min, hi = j.pay_rate_max;
    return (lo && Math.round(lo) !== Math.round(hi))
      ? `$${Math.round(lo)}–$${Math.round(hi)}${unit}`
      : `$${Math.round(hi)}${unit}`;
  }
  function jaiJobCard(j){
    const loc = [j.city, j.state_code].filter(Boolean).join(", ") || "Flexible";
    const type = (j.job_type || "").replace("_", " ");
    const seats = (j.openings || 1) > 1 ? ` · ${j.openings} openings` : "";
    const sub = [j.specialty || j.profession_type, loc, type].filter(Boolean).join(" · ");
    const pay = jaiPay(j);
    return `<div class="jai-job">
      <div class="jai-job-main">
        <div class="jai-job-title">${esc(j.title)}</div>
        <div class="jai-job-meta">${esc(sub)}${esc(seats)}</div>
      </div>
      ${pay ? `<div class="jai-job-pay">${esc(pay)}</div>` : ""}
      <button class="btn small primary" data-jobview="${esc(j.job_id)}"><i class="fas fa-arrow-right"></i>View</button>
    </div>`;
  }
  async function jobAiSearch(message){
    message = (message || "").trim();
    if (!message || S.jaiBusy) return;
    const fresh = !$("#page-jobai").classList.contains("has-results")
               || !$("#jai-thread").children.length;
    if (fresh) S.jaiContext = null;
    S.jaiBusy = true;
    $("#page-jobai").classList.add("has-results");
    $("#jai-input").value = "";
    const thread = $("#jai-thread");
    const turn = document.createElement("div");
    turn.className = "ai-turn";
    turn.innerHTML = `<div class="ai-q">${esc(message)}</div>
      <div class="ai-answer"><i class="fas fa-wand-magic-sparkles ai-spark"></i>
        <div class="ai-answer-body">Searching<span class="copilot-dots"><i></i><i></i><i></i></span></div></div>`;
    thread.appendChild(turn);
    turn.scrollIntoView({behavior:"smooth", block:"start"});
    const body = turn.querySelector(".ai-answer-body");
    try {
      const r = await post("/api/jobs/copilot", {message, context: S.jaiContext || null});
      S.jaiContext = r.filters || {};
      $("#jai-input").placeholder = "Refine — e.g. only nights, under $2,600, or add California";
      body.textContent = r.answer || "Here's what I found.";
      const items = r.items || [];
      items.forEach(j => S.jobsById.set(j.job_id, j));
      if (items.length){
        const results = document.createElement("div");
        results.className = "jai-results";
        results.innerHTML = items.map(jaiJobCard).join("");
        turn.appendChild(results);
        if ((r.total || 0) > items.length){
          const more = document.createElement("button");
          more.className = "ai-viewall";
          more.innerHTML = `<i class="fas fa-list"></i>See all ${r.total.toLocaleString()} in Find Jobs`;
          more.onclick = () => showPage("jobs");
          turn.appendChild(more);
        }
      }
    } catch(e) {
      body.textContent = "Sorry — I couldn't complete that search. Please try again.";
    } finally {
      S.jaiBusy = false;
      $("#jai-input").focus();
    }
  }
  function jobAiNew(){
    S.jaiContext = null;
    $("#jai-thread").innerHTML = "";
    $("#page-jobai").classList.remove("has-results");
    $("#jai-input").value = "";
    $("#jai-input").focus();
  }
  function wireJobAi(){
    const form = $("#jai-form");
    if (!form) return;
    form.addEventListener("submit", e => { e.preventDefault(); jobAiSearch($("#jai-input").value); });
    $$("#jai-suggestions .ai-chip").forEach(c => c.onclick = () => jobAiSearch(c.textContent));
    $("#jai-new").onclick = jobAiNew;
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
      if (!await confirmDialog({
        title: "Regenerate capture token",
        body: "Your current token stops working immediately. Any browser "
            + "extension still using it will need the new one pasted in.",
        confirm: "Regenerate", danger: true})) return;
      try { const r = await post("/api/extension/token", {}); $("#ext-token").value = r.capture_token; }
      catch(e) { toast(e.message || "The token was not changed.",
                       {title:"Could not regenerate", kind:"err"}); }
    };
    $$("#page-extension [data-copy]").forEach(b => b.onclick = () => {
      const el = $("#" + b.dataset.copy); if (!el) return;
      if (navigator.clipboard) navigator.clipboard.writeText(el.value);
      const i = b.querySelector("i"), prev = i.className;
      i.className = "fas fa-check"; setTimeout(() => { i.className = prev; }, 1200);
    });
  }

  // --- Travel pay calculator ---------------------------------------------
  const payMoney = value => new Intl.NumberFormat("en-US", {
    style:"currency", currency:"USD", maximumFractionDigits:0,
  }).format(Number(value) || 0);
  const payRate = value => new Intl.NumberFormat("en-US", {
    style:"currency", currency:"USD", minimumFractionDigits:2,
    maximumFractionDigits:2,
  }).format(Number(value) || 0);

  function payNumber(id, optional=false){
    const raw = ($(id).value || "").trim();
    if (optional && raw === "") return null;
    const value = Number(raw);
    if (!Number.isFinite(value)) throw new Error("Complete every required number before calculating.");
    return value;
  }

  function collectPayInputs(){
    const city = clean($("#calc-city").value);
    const state = clean($("#calc-state").value).toUpperCase();
    if (!city) throw new Error("Enter the assignment city.");
    if (!/^[A-Z]{2}$/.test(state)) throw new Error("Enter a two-letter US state code.");
    $("#calc-state").value = state;
    return {
      bill_rate: payNumber("#calc-bill-rate"),
      contract_weeks: payNumber("#calc-weeks"),
      hours_per_week: payNumber("#calc-hours"),
      ot_hours_per_week: payNumber("#calc-ot-hours"),
      shift_length_hours: payNumber("#calc-shift", true) || 12,
      margin_pct: payNumber("#calc-margin"),
      burden_multiplier: 1 + payNumber("#calc-burden") / 100,
      benefits_cost_per_hr: payNumber("#calc-benefits"),
      city, state_code:state,
      gsa_lodging_override: payNumber("#calc-lodging-override", true),
      mie_override: payNumber("#calc-mie-override", true),
      completion_bonus: payNumber("#calc-bonus"),
      travel_allowance: payNumber("#calc-travel"),
      reimbursements: payNumber("#calc-reimbursements"),
      tax_rate: payNumber("#calc-tax") / 100,
      travel_start: ($("#calc-start").value || "").trim() || null,
      travel_end: ($("#calc-end").value || "").trim() || null,
    };
  }

  function payLine(label, value, strong=false){
    return `<div class="calc-line${strong ? " is-total" : ""}"><span>${esc(label)}</span><b>${esc(value)}</b></div>`;
  }

  function payOption(option, featured=false){
    return `<article class="calc-option${featured ? " featured" : ""}">
      <div class="calc-option-head"><div><span>${featured ? "Recommended comparison" : "Baseline"}</span>
        <h3>${esc(option.label)}</h3></div>${featured ? '<i class="fas fa-sparkles"></i>' : ""}</div>
      ${payLine("Taxable hourly rate", `${payRate(option.taxable_rate)}/hr`)}
      ${payLine("Overtime rate", `${payRate(option.ot_rate)}/hr`)}
      ${payLine("Weekly taxable gross", payMoney(option.weekly_taxable_gross))}
      ${payLine("Weekly tax-free stipend", payMoney(option.weekly_tax_free))}
      ${payLine("Weekly package total", payMoney(option.weekly_total), true)}
      <div class="calc-net"><span>Estimated weekly take-home</span><b>${payMoney(option.est_weekly_net)}</b></div>
      <div class="calc-contract"><span>Full contract</span><b>${payMoney(option.contract_total)}</b></div>
    </article>`;
  }

  function seasonalNote(d){
    const sl = d.breakdown && d.breakdown.seasonal_lodging;
    if (!sl) return "";
    const vals = Object.values(d.gsa.monthly || {});
    const flat = vals.length && Math.max(...vals) === Math.min(...vals);
    if (flat)
      return `<p class="calc-rate-note flat"><i class="fas fa-circle-info"></i>${esc(d.gsa.city)}'s GSA lodging is <b>${payMoney(d.gsa.lodging)}/night every month</b> — so changing the travel dates won't change the stipend for this location. Seasonal cities (e.g. Aspen, CO) do.</p>`;
    return `<p class="calc-rate-note season"><i class="fas fa-calendar-check"></i>Seasonal rate for these dates: <b>${payMoney(sl.daily)}/night</b> lodging (${esc((sl.months || []).join(", "))}), used in place of the annual maximum.</p>`;
  }
  function overtimeNote(d){
    const ot = d.breakdown && d.breakdown.overtime;
    if (!ot || ot.mode !== "california") return "";
    const parts = [`${ot.straight_hours}h regular`];
    if (ot.ot_hours) parts.push(`${ot.ot_hours}h at 1.5×`);
    if (ot.dt_hours) parts.push(`${ot.dt_hours}h at 2×`);
    return `<p class="calc-rate-note ca"><i class="fas fa-scale-balanced"></i>California overtime `
         + `applied (${ot.shift_length}h shifts): <b>${parts.join(" + ")}</b> per week. `
         + `CA pays 1.5× after 8h/day and 2× after 12h/day, unlike the federal weekly-only rule.</p>`;
  }
  function gsaTripTotal(p){
    return `<div class="gsa-trip">
      <div class="gsa-trip-head"><i class="fas fa-building-columns"></i>
        <span>Max GSA per-diem · ${esc(String(p.start))} to ${esc(String(p.end))}</span>
        <b>${payMoney(p.per_diem_total)}</b></div>
      <div class="gsa-trip-rows">
        <span>Lodging — ${p.nights} night${p.nights === 1 ? "" : "s"}</span><b>${payMoney(p.lodging_total)}</b>
        <span>Meals &amp; incidentals — ${p.days} day${p.days === 1 ? "" : "s"} <em>(first & last at 75%)</em></span><b>${payMoney(p.mie_total)}</b>
      </div>
      <p class="gsa-trip-foot"><i class="fas fa-circle-check"></i>Matches the official gsa.gov per-diem calculator for these dates.</p>
    </div>`;
  }
  function renderPayPackage(d){
    const box = $("#pay-calc-results");
    const live = d.gsa.source === "api.gsa.gov";
    const advantage = Number(d.perdiem_advantage) || 0;
    const source = live ? "Live GSA rate" : "Offline GSA estimate";
    box.innerHTML = `<div class="calc-rate-card">
        <div class="calc-rate-head"><div><i class="fas fa-location-dot"></i>
          <strong>${esc(d.gsa.city)}, ${esc(d.gsa.state_code)}</strong></div>
          <span class="calc-source ${live ? "live" : "estimate"}">${esc(source)} · FY${esc(d.gsa.fiscal_year)}</span></div>
        <div class="calc-rate-grid">
          <div><span>Lodging</span><b>${payMoney((d.breakdown && d.breakdown.seasonal_lodging) ? d.breakdown.seasonal_lodging.daily : d.gsa.lodging)}</b><small>${(d.breakdown && d.breakdown.seasonal_lodging) ? "per night · your dates" : "per night"}</small></div>
          <div><span>Meals &amp; incidentals</span><b>${payMoney(d.gsa.mie)}</b><small>per day</small></div>
          <div><span>Weekly tax-free</span><b>${payMoney((d.breakdown && d.breakdown.weekly_tax_free_total != null) ? d.breakdown.weekly_tax_free_total : d.gsa.weekly_max_tax_free)}</b><small>per week</small></div>
        </div>
        ${seasonalNote(d)}
        ${overtimeNote(d)}
        ${(d.breakdown && d.breakdown.gsa_perdiem) ? gsaTripTotal(d.breakdown.gsa_perdiem) : ""}
        ${live ? "" : `<p class="calc-rate-note"><i class="fas fa-triangle-exclamation"></i>Using built-in locality estimates${
            d.gsa.fallback_reason ? ` (${esc(d.gsa.fallback_reason)})` : ""}. Confirm the official rate before quoting a package.</p>`}
      </div>
      <div class="calc-options">${payOption(d.option_w2)}${payOption(d.option_perdiem, true)}</div>
      <div class="calc-advantage ${advantage < 0 ? "negative" : ""}">
        <div><span>Estimated take-home difference over the contract</span>
          <b>${advantage >= 0 ? "+" : ""}${payMoney(advantage)}</b></div>
        <button class="btn primary" id="pay-calc-save" type="button"><i class="fas fa-bookmark"></i>Save comparison</button>
      </div>
      <div class="calc-breakdown">
        <span>Pay pool ${payRate(d.breakdown.pool_per_hr)}/hr</span>
        <span>Margin ${payRate(d.breakdown.agency_margin_per_hr)}/hr</span>
        <span>Weekly stipend ${payMoney(d.breakdown.weekly_tax_free_total)}/wk</span>
      </div>`;
    $("#pay-calc-save").onclick = savePayPackage;
  }

  async function calculatePayPackage(e){
    if (e) e.preventDefault();
    const form = $("#pay-calc-form"), error = $("#calc-form-error");
    if (!form.reportValidity()) return;
    const button = $("#pay-calc-submit"), original = button.innerHTML;
    error.textContent = "";
    try {
      const inputs = collectPayInputs();
      button.disabled = true;
      button.innerHTML = '<span class="spinner sm"></span>Calculating…';
      $("#pay-calc-results").innerHTML = loading("Looking up the location and calculating both packages...");
      const result = await post("/api/gsa/pay-package/calculate", inputs);
      S.payInputs = inputs;
      S.payPackage = result;
      renderPayPackage(result);
    } catch(e) {
      const message = e.status === 422
        ? "Those assumptions do not produce a valid pay package. Check the bill rate, margin, burden and benefits."
        : (e.message || "The pay package could not be calculated.");
      error.textContent = message;
      $("#pay-calc-results").innerHTML = errorState("Could not calculate this package", message);
    } finally {
      button.disabled = false;
      button.innerHTML = original;
    }
  }

  async function savePayPackage(){
    if (!S.payInputs || !S.payPackage) return;
    const values = await formDialog({
      title:"Save pay comparison", submit:"Save comparison",
      intro:"Give this package a recognizable name for your recruiter workspace.",
      fields:[{name:"label", label:"Comparison name", required:true, wide:true,
               value:`${S.payPackage.gsa.city}, ${S.payPackage.gsa.state_code} · ${S.payInputs.contract_weeks} weeks`}],
    });
    if (!values) return;
    try {
      await post("/api/gsa/pay-package/save", {
        label:values.label, inputs:S.payInputs, result:S.payPackage,
      });
      toast("The pay comparison is available below.", {title:"Comparison saved"});
      loadSavedPayPackages();
    } catch(e) {
      toast(e.message || "The comparison was not saved.", {title:"Could not save", kind:"err"});
    }
  }

  async function loadSavedPayPackages(){
    const box = $("#pay-calc-saved");
    if (!box || !isRecruiter()) return;
    box.innerHTML = '<div class="calc-saved-empty"><span class="spinner sm dark"></span>Loading saved comparisons…</div>';
    try {
      const rows = await get("/api/gsa/pay-package/saved");
      box.innerHTML = rows.length ? rows.slice(0, 8).map(row => {
        const result = row.result || {}, gsa = result.gsa || {}, pd = result.option_perdiem || {};
        const when = row.created_at ? new Date(row.created_at).toLocaleDateString([], {month:"short", day:"numeric", year:"numeric"}) : "";
        return `<article class="calc-saved-row"><div class="calc-saved-icon"><i class="fas fa-calculator"></i></div>
          <div><strong>${esc(row.label || "Saved pay package")}</strong>
            <span>${esc([gsa.city, gsa.state_code].filter(Boolean).join(", ") || "Location not recorded")} · ${esc(when)}</span></div>
          <div class="calc-saved-value"><b>${payMoney(pd.est_weekly_net)}</b><span>est. weekly take-home</span></div></article>`;
      }).join("") : '<div class="calc-saved-empty"><i class="fas fa-bookmark"></i>No comparisons saved yet.</div>';
    } catch(e) {
      box.innerHTML = '<div class="calc-saved-empty"><i class="fas fa-triangle-exclamation"></i>Saved comparisons could not be loaded.</div>';
    }
  }

  function loadPayCalculator(){
    if (isRecruiter()) loadSavedPayPackages();
  }

  // --- Seeker Pay Tools (candidate-framed pay calculator) ------------------
  function renderPayTools(d){
    const box = $("#pt-results");
    const live = d.gsa.source === "api.gsa.gov";
    const advantage = Number(d.perdiem_advantage) || 0;
    const source = live ? "Live GSA rate" : "Offline GSA estimate";
    box.innerHTML = `<div class="calc-rate-card">
        <div class="calc-rate-head"><div><i class="fas fa-location-dot"></i>
          <strong>${esc(d.gsa.city)}, ${esc(d.gsa.state_code)}</strong></div>
          <span class="calc-source ${live ? "live" : "estimate"}">${esc(source)} · FY${esc(d.gsa.fiscal_year)}</span></div>
        <div class="calc-rate-grid">
          <div><span>Lodging</span><b>${payMoney((d.breakdown && d.breakdown.seasonal_lodging) ? d.breakdown.seasonal_lodging.daily : d.gsa.lodging)}</b><small>${(d.breakdown && d.breakdown.seasonal_lodging) ? "tax-free / night · your dates" : "tax-free / night"}</small></div>
          <div><span>Meals &amp; incidentals</span><b>${payMoney(d.gsa.mie)}</b><small>tax-free / day</small></div>
          <div><span>Weekly tax-free</span><b>${payMoney((d.breakdown && d.breakdown.weekly_tax_free_total != null) ? d.breakdown.weekly_tax_free_total : d.gsa.weekly_max_tax_free)}</b><small>per week</small></div>
        </div>
        ${seasonalNote(d)}
        ${overtimeNote(d)}
        ${(d.breakdown && d.breakdown.gsa_perdiem) ? gsaTripTotal(d.breakdown.gsa_perdiem) : ""}
        ${live ? "" : `<p class="calc-rate-note"><i class="fas fa-triangle-exclamation"></i>Using built-in locality estimates${
            d.gsa.fallback_reason ? ` (${esc(d.gsa.fallback_reason)})` : ""}. Confirm the figure with the agency before you sign.</p>`}
      </div>
      <div class="calc-options">${payOption(d.option_w2)}${payOption(d.option_perdiem, true)}</div>
      <div class="calc-advantage ${advantage < 0 ? "negative" : ""}">
        <div><span>Take-home difference over the contract (per-diem vs straight W2)</span>
          <b>${advantage >= 0 ? "+" : ""}${payMoney(advantage)}</b></div>
      </div>
      <p class="calc-hint" style="margin-top:14px"><i class="fas fa-circle-info"></i>The tax-free portion applies only if you keep a qualifying tax home away from the assignment. Confirm your eligibility — this is not tax advice.</p>`;
  }
  async function calcPayTools(e){
    if (e) e.preventDefault();
    const form = $("#pt-form"), error = $("#pt-error");
    if (!form.reportValidity()) return;
    error.textContent = "";
    const inputs = {
      city: ($("#pt-city").value || "").trim(),
      state_code: ($("#pt-state").value || "").trim().toUpperCase(),
      bill_rate: parseFloat($("#pt-rate").value),
      margin_pct: 0,           // a candidate's package IS their pay pool
      burden_multiplier: 1.0,  // no employer burden from the candidate's side
      hours_per_week: parseFloat($("#pt-hours").value) || 36,
      shift_length_hours: parseFloat($("#pt-shift").value) || 12,
      contract_weeks: parseInt($("#pt-weeks").value, 10) || 13,
      tax_rate: (parseFloat($("#pt-tax").value) || 22) / 100,
    };
    const ts = ($("#pt-start").value || "").trim(), te = ($("#pt-end").value || "").trim();
    if (ts) inputs.travel_start = ts;
    if (te) inputs.travel_end = te;
    const btn = $("#pt-submit"), orig = btn.innerHTML;
    btn.disabled = true; btn.innerHTML = '<span class="spinner sm"></span>Calculating…';
    $("#pt-results").innerHTML = loading("Looking up local rates and estimating your take-home...");
    try {
      const result = await post("/api/gsa/pay-package/calculate", inputs);
      renderPayTools(result);
    } catch(err) {
      const msg = err.status === 422
        ? "Check the package rate and hours — those numbers don't produce a valid estimate."
        : (err.message || "Could not calculate.");
      error.textContent = msg;
      $("#pt-results").innerHTML = errorState("Could not estimate this package", msg);
    } finally { btn.disabled = false; btn.innerHTML = orig; }
  }
  // A live note reconciling the travel-date span against the contract length.
  function updateDatesNote(startSel, endSel, weeksSel, noteSel){
    const note = $(noteSel);
    if (!note) return;
    const s = ($(startSel).value || "").trim(), e = ($(endSel).value || "").trim();
    if (!s || !e){ note.innerHTML = ""; note.className = "calc-dates-note"; return; }
    const d1 = new Date(s + "T00:00:00"), d2 = new Date(e + "T00:00:00");
    if (isNaN(d1.getTime()) || isNaN(d2.getTime()) || d2 < d1){
      note.innerHTML = `<i class="fas fa-triangle-exclamation"></i>The end date must be on or after the start date.`;
      note.className = "calc-dates-note warn"; return;
    }
    const days = Math.round((d2 - d1) / 86400000) + 1;
    const wkR = Math.round((days / 7) * 10) / 10;
    const wkStr = Number.isInteger(wkR) ? String(wkR) : wkR.toFixed(1);
    const contract = parseFloat($(weeksSel).value) || 0;
    if (contract && days !== contract * 7){
      note.innerHTML = `<i class="fas fa-circle-info"></i>These dates span <b>${wkStr} weeks</b> (${days} days) — but the contract length is set to <b>${contract} weeks</b> (${contract * 7} days). Adjust one so they match.`;
      note.className = "calc-dates-note warn";
    } else {
      note.innerHTML = `<i class="fas fa-circle-check"></i>These dates span <b>${wkStr} weeks</b> (${days} days).`;
      note.className = "calc-dates-note ok";
    }
  }
  function wireDatesNote(startSel, endSel, weeksSel, noteSel){
    [startSel, endSel, weeksSel].forEach(sel => {
      const el = $(sel);
      if (el) el.addEventListener("input", () => updateDatesNote(startSel, endSel, weeksSel, noteSel));
    });
    updateDatesNote(startSel, endSel, weeksSel, noteSel);
  }
  function wirePayTools(){
    const form = $("#pt-form");
    if (!form) return;
    form.addEventListener("submit", calcPayTools);
    const st = $("#pt-state");
    if (st) st.addEventListener("input", e => {
      e.target.value = e.target.value.replace(/[^a-z]/gi, "").toUpperCase().slice(0, 2); });
    wireDatesNote("#pt-start", "#pt-end", "#pt-weeks", "#pt-dates-note");
  }

  function wirePayCalculator(){
    const form = $("#pay-calc-form");
    if (!form) return;
    form.addEventListener("submit", calculatePayPackage);
    $("#calc-state").addEventListener("input", e => { e.target.value = e.target.value.replace(/[^a-z]/gi, "").toUpperCase().slice(0, 2); });
    wireDatesNote("#calc-start", "#calc-end", "#calc-weeks", "#calc-dates-note");
    form.addEventListener("reset", () => setTimeout(() => {
      S.payInputs = null; S.payPackage = null;
      $("#calc-form-error").textContent = "";
      $("#pay-calc-results").innerHTML = `<div class="calc-empty"><i class="fas fa-chart-column"></i>
        <strong>Your package comparison will appear here</strong>
        <span>Enter the contract terms and calculate to compare weekly and full-contract pay.</span></div>`;
      updateDatesNote("#calc-start", "#calc-end", "#calc-weeks", "#calc-dates-note");
    }, 0));
  }

  function wire(){
    // Landing → sign-in, and back. Any element with data-auth-open opens the
    // auth screen in the requested mode.
    $$("[data-auth-open]").forEach(b => b.addEventListener("click", e => {
      e.preventDefault(); showAuth(b.dataset.authOpen);
    }));
    const authBack = $("#auth-back");
    if (authBack) authBack.onclick = showLanding;
    $$(".auth-tab").forEach(b => b.onclick = () => showAuthMode(b.dataset.authMode));
    $("#auth-submit").onclick = submitAuth;
    $("#auth-password").addEventListener("keydown", e => { if (e.key === "Enter") submitAuth(); });
    $("#auth-email").addEventListener("keydown", e => { if (e.key === "Enter") $("#auth-password").focus(); });
    const forgot = $("#auth-forgot");
    if (forgot) forgot.onclick = async () => {
      const v = await formDialog({
        title: "Reset your password",
        intro: "We'll email you a link if that address has an account.",
        submit: "Send reset link",
        fields: [{name:"email", label:"Email address", type:"email", required:true,
                  wide:true, value:($("#auth-email").value || "").trim()}],
      });
      if (!v) return;
      try {
        await post("/api/auth/password-reset/request", {email: v.email});
      } catch(e) { /* never reveal whether the address exists */ }
      toast("If that address has an account, a reset link is on its way.",
            {title:"Check your email", ms:6000});
    };
    const pwt = $("#auth-pw-toggle");
    if (pwt) pwt.onclick = () => { const i = $("#auth-password"); const show = i.type === "password"; i.type = show ? "text" : "password"; pwt.innerHTML = show ? '<i class="fas fa-eye-slash"></i>' : '<i class="fas fa-eye"></i>'; };
    $$(".nav-item,.top-user,.top-actions .icon-btn,.credit-chip,.hero-band .btn,.logo").forEach(el => el.addEventListener("click", e => { const page = el.dataset.page; if (page) { e.preventDefault(); showPage(page); } }));
    // The dashboard tiles are rewritten per role after this runs, so they are
    // delegated rather than bound directly.
    const metrics = $("#dash-metrics");
    if (metrics) metrics.addEventListener("click", e => {
      const tile = e.target.closest(".metric[data-page]");
      if (tile) showPage(tile.dataset.page);
    });
    $("#logout-btn").onclick = () => { setToken(""); setRefresh(""); location.reload(); };
    // Verification gate (only reached when email delivery is on).
    const vCont = $("#verify-continue");
    if (vCont) vCont.onclick = async () => {
      verifyMsg("Checking…", "");
      const ok = await loadMe();
      if (ok === true) enterAppPages();
      else if (ok === "pending") verifyMsg("Not verified yet — open the link in your email, then try again.", "err");
      else { setToken(""); setRefresh(""); location.reload(); }
    };
    const vResend = $("#verify-resend");
    if (vResend) vResend.onclick = async () => {
      verifyMsg("Sending…", "");
      try { await post("/api/auth/email/request-verification", {}); verifyMsg("Sent — check your inbox.", "ok"); }
      catch(e) { verifyMsg(e.message || "Could not resend the email.", "err"); }
    };
    const vSignout = $("#verify-signout");
    if (vSignout) vSignout.onclick = () => { setToken(""); setRefresh(""); location.reload(); };
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
    const resetBtn = $("#search-reset");
    if (resetBtn) resetBtn.onclick = () => {
      const p = S.provider;
      ["q", "license_title", "state_code", "licensed_state", "worked_at",
       "travel_experience", "city", "zip", "radius_mi", "contact_available",
       "compact", "min_experience", "max_experience", "category"]
        .forEach(k => { p[k] = ""; });
      S.providerOffset = 0;
      $("#provider-q").value = "";
      $("#provider-zip").value = "";
      ["#provider-license-title", "#provider-state", "#provider-experience", "#provider-contact"]
        .forEach(s => { const el = $(s); if (el) el.value = ""; });
      radiusEl.value = 25; radiusEl.disabled = true; paintRadius();
      $$(".provider-toggles .ptoggle").forEach(t => t.classList.remove("active"));
      $$("#provider-tabs .tab").forEach(t => t.classList.toggle("active", t.dataset.category === ""));
      loadProviders();
      toast("Filters cleared.", {title: "Reset"});
    };
    document.body.addEventListener("click", e => {
      const apply = e.target.closest("[data-apply]"); if (apply) applyJob(apply.dataset.apply);
      const resume = e.target.closest("[data-resume]"); if (resume) viewResume(resume.dataset.resume);
      const release = e.target.closest("[data-release]"); if (release) releaseContact(release.dataset.release);
      const sub = e.target.closest("[data-submit]"); if (sub) submitCandidate(sub.dataset.submit);
      const message = e.target.closest("[data-message]"); if (message) messageCandidate(message.dataset.message);
      const pick = e.target.closest("[data-pick]");
      if (pick) togglePick(pick.dataset.pick, pick.checked);
      const source = e.target.closest("[data-source]"); if (source) sourceForJob(source.dataset.source);
      const applicants = e.target.closest("[data-applicants]"); if (applicants) reviewApplicants(applicants.dataset.applicants);
      const jobview = e.target.closest("[data-jobview]"); if (jobview) openJobDetail(jobview.dataset.jobview);
      const save = e.target.closest("[data-pool-save]");
      if (save){ e.stopPropagation(); openPoolMenu(save, save.dataset.poolSave); }
      else if (!e.target.closest(".pool-menu")) closePoolMenu();
      const aSusp = e.target.closest("[data-admin-suspend]");
      if (aSusp) adminUpdateUser(aSusp.dataset.adminSuspend, {status:"suspended"});
      const aAct = e.target.closest("[data-admin-activate]");
      if (aAct) adminUpdateUser(aAct.dataset.adminActivate, {status:"active"});
      const aTab = e.target.closest(".admin-tab");
      if (aTab) showAdminTab(aTab.dataset.atab);
      const aUser = e.target.closest("[data-admin-user]");
      if (aUser) openAdminUser(aUser.dataset.adminUser);
      const aOrg = e.target.closest("[data-admin-org]");
      if (aOrg) openAdminOrg(aOrg.dataset.adminOrg);
      const jFeat = e.target.closest("[data-admin-job-feature]");
      if (jFeat) adminModerateJob(jFeat.dataset.adminJobFeature, {is_featured:true});
      const jUnfeat = e.target.closest("[data-admin-job-unfeature]");
      if (jUnfeat) adminModerateJob(jUnfeat.dataset.adminJobUnfeature, {is_featured:false});
      const jPause = e.target.closest("[data-admin-job-pause]");
      if (jPause) adminModerateJob(jPause.dataset.adminJobPause, {status:"paused"});
      const jAct = e.target.closest("[data-admin-job-activate]");
      if (jAct) adminModerateJob(jAct.dataset.adminJobActivate, {status:"active"});
      const jDel = e.target.closest("[data-admin-job-delete]");
      if (jDel) adminDeleteJob(jDel.dataset.adminJobDelete);
      if (e.target.closest("[data-close-modal]") || e.target.classList.contains("modal")) $("#modal-root").innerHTML = "";
    });
    // Admin: role dropdown change → update the user's role.
    document.body.addEventListener("change", e => {
      const rs = e.target.closest("[data-admin-role]");
      if (rs) adminUpdateUser(rs.dataset.adminRole, {role: rs.value});
    });
    // Admin: search + filter controls (debounced search, immediate selects).
    let adminUserT, adminOrgT;
    const auq = $("#admin-user-q");
    if (auq) auq.addEventListener("input", () => {
      clearTimeout(adminUserT);
      adminUserT = setTimeout(() => { ADMIN.uOffset = 0; loadAdminUsers(); }, 300);
    });
    const aur = $("#admin-user-role");
    if (aur) aur.onchange = () => { ADMIN.uOffset = 0; loadAdminUsers(); };
    const aus = $("#admin-user-status");
    if (aus) aus.onchange = () => { ADMIN.uOffset = 0; loadAdminUsers(); };
    const aoq = $("#admin-org-q");
    if (aoq) aoq.addEventListener("input", () => {
      clearTimeout(adminOrgT);
      adminOrgT = setTimeout(() => { ADMIN.oOffset = 0; loadAdminOrgs(); }, 300);
    });
    const aOrgNew = $("#admin-org-new");
    if (aOrgNew) aOrgNew.onclick = adminNewOrg;
    let adminJobT;
    const ajq = $("#admin-job-q");
    if (ajq) ajq.addEventListener("input", () => {
      clearTimeout(adminJobT);
      adminJobT = setTimeout(() => { ADMIN.jOffset = 0; loadAdminJobs(); }, 300);
    });
    const ajs = $("#admin-job-status");
    if (ajs) ajs.onchange = () => { ADMIN.jOffset = 0; loadAdminJobs(); };
    const poolNew = $("#pool-new");
    if (poolNew) poolNew.onclick = createPool;
    const matchBack = $("#match-back");
    if (matchBack) matchBack.onclick = () => showPage("jobs");
    const applicantsBack = $("#applicants-back");
    if (applicantsBack) applicantsBack.onclick = () => showPage("employer");
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
    const alertNew = $("#alert-new");
    if (alertNew) alertNew.onclick = newJobAlert;
    const searchSave = $("#search-save");
    if (searchSave) searchSave.onclick = saveCurrentSearch;
    const jobNew = $("#job-new");
    if (jobNew) jobNew.onclick = postJob;
    const clientNew = $("#client-new");
    if (clientNew) clientNew.onclick = () => newClient();
    const msgNew = $("#msg-new");
    if (msgNew) msgNew.onclick = () => {
      showPage("providers");
      toast("Find a candidate and use the message icon to start a conversation.",
            {kind:"info", ms:6000});
    };
    const walletAddLic = $("#wallet-add-lic");
    if (walletAddLic) walletAddLic.onclick = addCredential;
    const walletAddCert = $("#wallet-add-cert");
    if (walletAddCert) walletAddCert.onclick = addCertification;
    const credShare = $("#cred-share");
    if (credShare) credShare.onclick = copyCredentialSummary;
    const tmplNew = $("#tmpl-new");
    if (tmplNew) tmplNew.onclick = () => newTemplate();
    const campNew = $("#camp-new");
    if (campNew) campNew.onclick = newCampaign;
    wireMessages();
    $("#resume-drop").onclick = () => $("#resume-file").click();
    $("#resume-file").onchange = () => { if ($("#resume-file").files[0]) uploadResume($("#resume-file").files[0]); };
    wireAi();
    wireJobAi();
    wireExtension();
    wirePayCalculator();
    wirePayTools();
  }
  // --- Dialogs & toasts ----------------------------------------------------
  // Browser prompt() chains were the worst of the interface: adding a licence
  // meant four sequential dialogs with no way back and no validation. These
  // collect a whole form at once and report the result without blocking.

  function toast(message, {title = "", kind = "ok", ms = 4200} = {}){
    const root = $("#toast-root");
    if (!root) return;
    const icon = kind === "err" ? "fa-circle-exclamation"
               : kind === "info" ? "fa-circle-info" : "fa-circle-check";
    const el = document.createElement("div");
    el.className = "toast " + kind;
    el.innerHTML = `<i class="fas ${icon}"></i><div>${
      title ? `<b>${esc(title)}</b>` : ""}<span>${esc(message)}</span></div>`;
    root.appendChild(el);
    setTimeout(() => {
      el.style.transition = "opacity .2s"; el.style.opacity = "0";
      setTimeout(() => el.remove(), 220);
    }, ms);
  }

  function formDialog({title, intro = "", fields = [], submit = "Save"}){
    return new Promise(resolve => {
      const control = f => {
        const common = `name="${f.name}" class="input"${f.required ? " required" : ""}` +
                       `${f.placeholder ? ` placeholder="${esc(f.placeholder)}"` : ""}`;
        const val = f.value == null ? "" : String(f.value);
        if (f.type === "select")
          return `<select ${common}>${(f.options || []).map(o => {
            const v = Array.isArray(o) ? o[0] : (o && typeof o === "object" ? o.value : o);
            const label = Array.isArray(o) ? o[1] : (o && typeof o === "object" ? o.label : o);
            return `<option value="${esc(v)}"${String(val) === String(v) ? " selected" : ""}>${esc(label)}</option>`;
          }).join("")}</select>`;
        if (f.type === "textarea") return `<textarea ${common} rows="3">${esc(val)}</textarea>`;
        if (f.type === "checkbox") return `<input type="checkbox" name="${f.name}"${f.value ? " checked" : ""}>`;
        return `<input type="${f.type || "text"}" ${common} value="${esc(val)}"${
          f.step ? ` step="${f.step}"` : ""}${f.max ? ` maxlength="${f.max}"` : ""}>`;
      };
      $("#modal-root").innerHTML = `
        <div class="modal"><div class="modal-card form-card">
          <div class="modal-head"><strong>${esc(title)}</strong>
            <button class="icon-btn" data-dlg-x><i class="fas fa-xmark"></i></button></div>
          <form id="dlg-form"><div class="dlg-body">
            ${intro ? `<p class="dlg-intro">${esc(intro)}</p>` : ""}
            <div class="dlg-grid">${fields.map(f => f.type === "checkbox"
              ? `<label class="dlg-check">${control(f)}${esc(f.label)}</label>`
              : `<label class="${f.wide ? "dlg-wide" : ""}">${esc(f.label)}${
                  f.hint ? ` <span class="dlg-hint">${esc(f.hint)}</span>` : ""}${control(f)}</label>`
            ).join("")}</div></div>
          <div class="dlg-foot">
            <span class="dlg-error" id="dlg-error"></span><span class="spacer"></span>
            <button type="button" class="btn ghost" data-dlg-x>Cancel</button>
            <button type="submit" class="btn primary">${esc(submit)}</button>
          </div></form>
        </div></div>`;
      let settled = false;
      const finish = value => {
        if (settled) return;
        settled = true;
        document.removeEventListener("keydown", onKey);
        $("#modal-root").innerHTML = "";
        resolve(value);
      };
      const onKey = e => { if (e.key === "Escape") finish(null); };
      document.addEventListener("keydown", onKey);
      $$("#modal-root [data-dlg-x]").forEach(b => b.onclick = () => finish(null));
      $("#modal-root .modal").addEventListener("click", e => {
        if (e.target.classList.contains("modal")) finish(null);
      });
      $("#dlg-form").addEventListener("submit", e => {
        e.preventDefault();
        const out = {};
        fields.forEach(f => {
          const el = e.target.elements[f.name];
          if (el) out[f.name] = f.type === "checkbox" ? el.checked : (el.value || "").trim();
        });
        const missing = fields.find(f => f.required && !out[f.name]);
        if (missing){ $("#dlg-error").textContent = missing.label + " is required."; return; }
        finish(out);
      });
      const first = $("#dlg-form .input");
      if (first) setTimeout(() => first.focus(), 40);
    });
  }

  // Destructive actions get the same styled dialog as everything else, and say
  // what will actually happen rather than "Are you sure?". `danger` colours the
  // confirm button red so an erase never looks like an ordinary save.
  function confirmDialog({title, body = "", confirm: label = "Confirm", danger = false}){
    return new Promise(resolve => {
      $("#modal-root").innerHTML = `
        <div class="modal"><div class="modal-card confirm-card">
          <div class="modal-head"><strong>${esc(title)}</strong>
            <button class="icon-btn" data-dlg-x><i class="fas fa-xmark"></i></button></div>
          <div class="dlg-body"><p class="dlg-intro">${esc(body)}</p></div>
          <div class="dlg-foot"><span class="spacer"></span>
            <button type="button" class="btn ghost" data-dlg-x>Cancel</button>
            <button type="button" class="btn ${danger ? "danger" : "primary"}"
                    id="dlg-ok">${esc(label)}</button>
          </div>
        </div></div>`;
      let settled = false;
      const finish = v => {
        if (settled) return;
        settled = true;
        document.removeEventListener("keydown", onKey);
        $("#modal-root").innerHTML = "";
        resolve(v);
      };
      const onKey = e => { if (e.key === "Escape") finish(false); };
      document.addEventListener("keydown", onKey);
      $$("#modal-root [data-dlg-x]").forEach(b => b.onclick = () => finish(false));
      $("#modal-root .modal").addEventListener("click", e => {
        if (e.target.classList.contains("modal")) finish(false);
      });
      $("#dlg-ok").onclick = () => finish(true);
      setTimeout(() => { const b = $("#dlg-ok"); if (b) b.focus(); }, 40);
    });
  }

  // A read-only record: figures you want to sit and look at, rather than a
  // toast that disappears while you are still reading it.
  function infoDialog(title, rows){
    return new Promise(resolve => {
      $("#modal-root").innerHTML = `
        <div class="modal"><div class="modal-card confirm-card">
          <div class="modal-head"><strong>${esc(title)}</strong>
            <button class="icon-btn" data-dlg-x><i class="fas fa-xmark"></i></button></div>
          <div class="dlg-body"><dl class="dlg-facts">${rows.map(([k, v]) =>
            `<dt>${esc(k)}</dt><dd>${esc(v == null || v === "" ? "—" : String(v))}</dd>`
          ).join("")}</dl></div>
          <div class="dlg-foot"><span class="spacer"></span>
            <button type="button" class="btn primary" data-dlg-x>Close</button></div>
        </div></div>`;
      let settled = false;
      const finish = () => {
        if (settled) return;
        settled = true;
        document.removeEventListener("keydown", onKey);
        $("#modal-root").innerHTML = "";
        resolve();
      };
      const onKey = e => { if (e.key === "Escape") finish(); };
      document.addEventListener("keydown", onKey);
      $$("#modal-root [data-dlg-x]").forEach(b => b.onclick = finish);
      $("#modal-root .modal").addEventListener("click", e => {
        if (e.target.classList.contains("modal")) finish();
      });
    });
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
      if (box) box.innerHTML = errorState("Could not load conversations");
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
    const older = detail.has_more
      ? `<button class="btn ghost small" id="msg-older" data-before="${
           (detail.messages[0] || {}).message_id || ""}">Load earlier messages</button>`
      : "";
    box.innerHTML = (older ? `<div class="msg-older-wrap">${older}</div>` : "")
                  + (html || `<div class="msg-system">No messages yet — say hello.</div>`);
    box.scrollTop = box.scrollHeight;
    const btn = $("#msg-older");
    if (btn) btn.onclick = () => loadOlderMessages(btn.dataset.before);
  }

  // Older history is prepended, keeping the reader where they were rather than
  // jumping them to the top of the thread.
  // Starting a conversation existed in the API and had no caller. Reachability
  // is checked first: almost every profile is an imported résumé with no
  // account, and offering a button that always fails is worse than none.
  async function emailCandidate(profileId){
    // The candidate has no HealthBoard account — reach them by email instead.
    const card = S.providerCards.get(profileId);
    if (card && !card.is_released && !S.releasedContacts.has(profileId)){
      return toast("Reveal this candidate's contact first, then you can email them.",
                   {title:"Reveal needed", kind:"err", ms:6000});
    }
    const v = await formDialog({
      title: "Email this candidate",
      intro: "This candidate isn't on HealthBoard yet, so your message reaches them by "
           + "email. Their reply comes straight to your inbox.",
      submit: "Send email",
      fields: [
        {name:"subject", label:"Subject", required:true,
         placeholder:"An opportunity that fits your background"},
        {name:"body", label:"Message", type:"textarea", required:true, wide:true,
         placeholder:"Hi — I'm recruiting for a role that looks like a strong match for your experience…"},
      ],
    });
    if (!v) return;
    try {
      await post("/api/messages/email-outreach",
                 {profile_id: profileId, subject: v.subject, body: v.body});
      toast("Your email is on its way — replies land in your inbox.", {title:"Email sent"});
    } catch(e){
      if (e.status === 402)
        toast("Reveal this candidate's contact first, then you can email them.",
              {title:"Reveal needed", kind:"err"});
      else toast(e.message || "Could not send the email.", {kind:"err"});
    }
  }
  async function messageCandidate(profileId){
    try {
      const check = await get(`/api/messages/can-message/${profileId}`);
      // No in-app account → fall back to the email bridge (cold outreach).
      if (!check.can_message) return emailCandidate(profileId);
      if (check.thread_id){
        showPage("messages");
        setTimeout(() => openThread(check.thread_id), 400);
        return;
      }
      const v = await formDialog({
        title: "Start a conversation",
        submit: "Send",
        fields: [{name:"body", label:"First message", type:"textarea", required:true,
                  wide:true,
                  placeholder:"Hi — I'm recruiting for a role that looks like a fit…"}],
      });
      if (!v) return;
      const thread = await post("/api/messages/threads",
                                {profile_id: profileId, body: v.body});
      showPage("messages");
      setTimeout(() => openThread(thread.thread_id), 400);
    } catch(e) { toast(e.message || "Could not start a conversation.", {kind:"err"}); }
  }
  // Small shim so this works before the toast system lands in the polish pass.
  async function loadOlderMessages(beforeId){
    if (!S.activeThread || !beforeId) return;
    const box = $("#msg-bubbles");
    const heightBefore = box.scrollHeight;
    try {
      const d = await get(`/api/messages/threads/${S.activeThread}?before=${beforeId}&limit=50`);
      const me = S.user && S.user.user_id;
      const older = (d.messages || []).map(m => {
        const mine = m.sender_id === me;
        if (m.kind && m.kind !== "text")
          return `<div class="msg-system"><i class="fas fa-circle-info"></i> ${esc(m.body || m.kind)}</div>`;
        return `<div class="msg-row ${mine ? "me" : "them"}">
          <div><div class="msg-bubble">${esc(m.body || "")}</div>
          <div class="msg-meta">${esc(shortTime(m.created_at))}</div></div></div>`;
      }).join("");
      const wrap = $(".msg-older-wrap");
      if (wrap){
        wrap.outerHTML = (d.has_more
          ? `<div class="msg-older-wrap"><button class="btn ghost small" id="msg-older" data-before="${
              (d.messages[0] || {}).message_id || ""}">Load earlier messages</button></div>`
          : "") + older;
        const btn = $("#msg-older");
        if (btn) btn.onclick = () => loadOlderMessages(btn.dataset.before);
      }
      box.scrollTop = box.scrollHeight - heightBefore;
    } catch(e) { /* leave what is already shown */ }
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
      $("#msg-bubbles").innerHTML = errorState("Could not load this conversation");
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
      toast(e.message || "The message was not sent.",
            {title:"Send failed", kind:"err"});
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
      catch(e) { toast(e.message || "That did not work.", {title:"Something went wrong", kind:"err"}); }
    };
  }

  // --- My applications (professional) --------------------------------------
  function appCard(a){
    const steps = (a.stages || []).map((s, i) => {
      const done = a.stage_index != null && i <= a.stage_index;
      return `<span class="app-step ${a.is_closed && i === 0 ? "closed" : done ? "done" : ""}">${esc(s)}</span>`;
    }).join("");
    const where = [a.facility, a.location].filter(Boolean).join(" · ");
    return `<div class="app-card">
      <div class="app-head">
        <h3>${esc(a.title)}</h3>
        <span class="status-pill ${a.status === "hired" ? "ok" : a.is_closed ? "no" : ""}">${esc(a.status)}</span>
        <span class="spacer"></span>
        ${!a.is_closed ? `<button class="btn small" data-withdraw="${a.application_id}">Withdraw</button>` : ""}
      </div>
      <div class="app-meta">${esc(where || "Location not listed")}
        ${a.pay_rate_max ? ` · $${Math.round(a.pay_rate_max)}/hr` : ""}
        · applied ${esc(shortTime(a.applied_at))}</div>
      ${a.is_closed ? "" : `<div class="app-steps">${steps}</div>`}
    </div>`;
  }
  async function loadApplications(){
    const box = $("#apps-body");
    box.innerHTML = loading("Loading your applications...");
    loadJobAlerts();
    loadSavedJobs();
    try {
      const d = await get("/api/applications/mine/detail");
      const n = d.items.length;
      $("#apps-sub").textContent = n
        ? `${n} application${n === 1 ? "" : "s"} · ` +
          Object.entries(d.by_status).map(([s,c]) => `${c} ${s}`).join(" · ")
        : "Where each application stands";
      box.innerHTML = n ? d.items.map(appCard).join("")
        : `<div class="match-empty"><i class="fas fa-list-check"></i><h3>No applications yet</h3>
           <p>Roles you apply to appear here, with where each one stands.</p></div>`;
      $$("#apps-body [data-withdraw]").forEach(b => b.onclick = async () => {
        if (!await confirmDialog({
          title: "Withdraw application",
          body: "The employer will see this application as withdrawn. You "
              + "can apply again while the role is open.",
          confirm: "Withdraw", danger: true})) return;
        try { await post(`/api/applications/${b.dataset.withdraw}/withdraw`, {}); loadApplications(); }
        catch(e) { toast(e.message || "That did not work.", {title:"Something went wrong", kind:"err"}); }
      });
    } catch(e) { box.innerHTML = errorState("Could not load your applications"); }
  }

  // Jobs the professional bookmarked — previously savable but with nowhere to
  // see them. Shown beneath the applications list on the same page.
  async function loadSavedJobs(){
    const box = $("#saved-jobs");
    if (!box) return;
    try {
      const jobs = await get("/api/applications/saved");
      (jobs || []).forEach(j => S.jobsById.set(j.job_id, j));
      box.innerHTML = (jobs && jobs.length)
        ? `<div class="section-head saved-head"><div><h2>Saved jobs</h2>
             <p>${jobs.length} role${jobs.length === 1 ? "" : "s"} you bookmarked</p></div></div>
           <div class="table-wrap"><table class="table">
             <thead><tr><th>Role</th><th>Type</th><th>Location</th><th>Pay</th><th class="th-actions"></th></tr></thead>
             <tbody>${jobs.map(j => `<tr>
               <td><div class="cell-name"><button class="linklike" data-jobview="${esc(j.job_id)}">${esc(j.title)}</button></div>
                   <div class="cell-sub">${esc(j.specialty || j.profession_type || "")}</div></td>
               <td>${j.job_type ? `<span class="badge accent">${esc(j.job_type)}</span>` : `<span class="cell-none">—</span>`}</td>
               <td>${esc([j.city, j.state_code].filter(Boolean).join(", ") || "—")}</td>
               <td>${j.pay_rate_max ? `<strong>$${Math.round(j.pay_rate_max)}/hr</strong>` : "—"}</td>
               <td class="td-actions">
                 <button class="btn small primary" data-apply="${esc(j.job_id)}">Apply</button>
                 <button class="btn small" data-unsave="${esc(j.job_id)}" title="Remove from saved"><i class="fas fa-bookmark"></i></button>
               </td>
             </tr>`).join("")}</tbody></table></div>`
        : "";   // nothing saved — stay quiet; the applications area owns the empty state
      $$("#saved-jobs [data-unsave]").forEach(b => b.onclick = async () => {
        try { await del(`/api/jobs/${b.dataset.unsave}/save`); loadSavedJobs(); loadDashboard(); }
        catch(e) { toast(e.message || "That did not work.", {title:"Something went wrong", kind:"err"}); }
      });
    } catch(e) { box.innerHTML = ""; }
  }

  // --- Job alerts (professional saved searches) ----------------------------
  async function loadJobAlerts(){
    const box = $("#alerts-strip");
    if (!box) return;
    try {
      const d = await get("/api/saved-searches?kind=jobs");
      S.jobAlerts = d.items || [];
      box.innerHTML = S.jobAlerts.length ? `<div class="alert-strip">${
        S.jobAlerts.map(a => `<span class="alert-chip"><b>${esc(a.name)}</b>
          ${a.new ? `<span class="new">+${a.new}</span>` : ""}
          <button class="drop" data-alert-del="${a.search_id}" title="Delete"><i class="fas fa-xmark"></i></button>
        </span>`).join("")}</div>` : "";
      $$("#alerts-strip [data-alert-del]").forEach(b => b.onclick = async () => {
        try { await del(`/api/saved-searches/${b.dataset.alertDel}`); loadJobAlerts(); }
        catch(e) { toast(e.message || "That did not work.", {title:"Something went wrong", kind:"err"}); }
      });
    } catch(e) { box.innerHTML = ""; }
  }
  async function newJobAlert(){
    const v = await formDialog({
      title: "New job alert",
      intro: "We re-check your criteria and tell you when new roles appear.",
      submit: "Create alert",
      fields: [
        {name:"name", label:"Alert name", required:true, wide:true,
         placeholder:"ICU roles in Texas"},
        {name:"specialty", label:"Specialty", placeholder:"ICU"},
        {name:"state_code", label:"State", placeholder:"TX", max:2},
        {name:"pay_min", label:"Minimum $/hr", type:"number", step:"1"},
      ],
    });
    if (!v) return;
    const params = {};
    if (v.specialty) params.specialty = v.specialty;
    if (v.state_code) params.state_code = v.state_code.toUpperCase();
    if (v.pay_min && !isNaN(Number(v.pay_min))) params.pay_min = Number(v.pay_min);
    const name = v.name;
    try {
      const r = await post("/api/saved-searches", {name:name.trim(), kind:"jobs", params, notify:true});
      toast(`${r.matches} role${r.matches === 1 ? "" : "s"} match right now. `
            + `We'll tell you when new ones appear.`, {title:"Alert saved"});
      loadJobAlerts();
    } catch(e) { toast(e.status === 409 ? "You already have an alert with that name. Pick another."
                                        : (e.message || "The alert was not saved."),
                       {title:"Could not save alert", kind:"err"}); }
  }

  // --- Submissions (recruiter) ---------------------------------------------
  function subRow(s){
    const margin = s.margin != null
      ? `<span class="margin-pos">$${s.margin.toFixed(0)}</span>` : `<span class="cell-none">—</span>`;
    return `<tr>
      <td><div class="cell-name">${esc(s.candidate)}</div>
          <div class="cell-sub">${esc([s.profession_type, s.specialty].filter(Boolean).join(" · "))}</div></td>
      <td>${esc(s.job_title || "—")}<div class="cell-sub">${esc(s.facility || "")}</div></td>
      <td><select class="stage-select" data-sub-status="${s.submission_id}">
        ${(S.subStatuses || []).map(x => `<option value="${x}"${s.status === x ? " selected" : ""}>${x.replace("_"," ")}</option>`).join("")}
      </select></td>
      <td class="rate">${s.bill_rate ? `$${s.bill_rate.toFixed(0)}` : "—"}</td>
      <td class="rate">${s.pay_rate ? `$${s.pay_rate.toFixed(0)}` : "—"}</td>
      <td class="rate">${margin}</td>
      <td>${esc(s.submitted_by || "")}<div class="cell-sub">${esc(shortTime(s.submitted_at))}</div></td>
      <td class="td-actions"><button class="btn small" data-sub-del="${s.submission_id}"><i class="fas fa-xmark"></i></button></td>
    </tr>`;
  }
  async function loadSubmissions(){
    const box = $("#subs-body");
    box.innerHTML = loading("Loading submissions...");
    try {
      const d = await get("/api/submissions");
      S.subStatuses = d.statuses || [];
      const n = d.items.length;
      $("#subs-sub").textContent = n
        ? `${n} submission${n === 1 ? "" : "s"} · ` +
          Object.entries(d.by_status).map(([s,c]) => `${c} ${s.replace("_"," ")}`).join(" · ")
          + (d.team_size > 1 ? ` · shared across ${d.team_size} recruiters` : "")
        : "Candidates put forward to client facilities";
      box.innerHTML = n ? `<div class="table-wrap"><table class="table">
          <thead><tr><th>Candidate</th><th>Role</th><th>Status</th><th>Bill</th><th>Pay</th><th>Margin</th><th>Submitted</th><th></th></tr></thead>
          <tbody>${d.items.map(subRow).join("")}</tbody></table></div>`
        : `<div class="match-empty"><i class="fas fa-share-from-square"></i><h3>No submissions yet</h3>
           <p>Submit a candidate from a talent pool to start tracking what you've put to clients.</p></div>`;
      $$("#subs-body [data-sub-status]").forEach(sel => sel.onchange = async () => {
        try { await patch(`/api/submissions/${sel.dataset.subStatus}`, {status: sel.value}); loadSubmissions(); }
        catch(e) { toast(e.message || "That did not work.", {title:"Something went wrong", kind:"err"}); }
      });
      $$("#subs-body [data-sub-del]").forEach(b => b.onclick = async () => {
        if (!await confirmDialog({
          title: "Remove submission",
          body: "This drops the candidate from the client submission list. "
              + "Their profile and your notes stay in the directory.",
          confirm: "Remove", danger: true})) return;
        try { await del(`/api/submissions/${b.dataset.subDel}`); loadSubmissions(); }
        catch(e) { toast(e.message || "That did not work.", {title:"Something went wrong", kind:"err"}); }
      });
    } catch(e) { box.innerHTML = errorState("Could not load submissions"); }
  }
  async function submitCandidate(profileId){
    if (!S.clients) { try { S.clients = (await get("/api/clients")).items || []; } catch(e) { S.clients = []; } }
    const clientOptions = [["", "— No client / type a facility below —"],
      ...S.clients.map(c => [c.client_id, c.name + (c.location ? ` (${c.location})` : "")])];
    const v = await formDialog({
      title: "Submit to a client",
      intro: "Records what you put forward so the desk can see it and the margin "
           + "is tracked. Pick a client to fill the facility and default bill rate.",
      submit: "Submit candidate",
      fields: [
        {name:"client_id", label:"Client", type:"select", options: clientOptions, wide:true},
        {name:"facility", label:"Or facility name", wide:true, placeholder:"Genesis - Mid-America"},
        {name:"bill_rate", label:"Bill rate ($/hr)", type:"number", step:"1"},
        {name:"pay_rate", label:"Pay rate ($/hr)", type:"number", step:"1"},
        {name:"note", label:"Note", type:"textarea", hint:"optional", wide:true},
      ],
    });
    if (!v) return;
    const body = {profile_id: profileId};
    if (v.client_id) body.client_id = v.client_id;
    if (v.facility) body.facility = v.facility;
    if (v.bill_rate && !isNaN(Number(v.bill_rate))) body.bill_rate = Number(v.bill_rate);
    if (v.pay_rate && !isNaN(Number(v.pay_rate))) body.pay_rate = Number(v.pay_rate);
    if (v.note) body.note = v.note;
    try {
      await post("/api/submissions", body);
      toast("Track it on the Submissions page.", {title:"Candidate submitted"});
    } catch(e) { toast(e.message || "That did not work.", {title:"Something went wrong", kind:"err"}); }
  }

  // --- Clients (agency book of business) -----------------------------------
  function clientRow(c){
    const contact = [c.contact_name, c.contact_email, c.contact_phone].filter(Boolean).join(" · ");
    return `<tr class="${c.is_active ? "" : "row-muted"}">
      <td><div class="cell-name"><button class="linklike" data-client-view="${esc(c.client_id)}">${esc(c.name)}</button></div>
          ${c.placed ? `<div class="cell-sub">${c.placed} placed</div>` : ""}</td>
      <td>${c.facility_type ? `<span class="badge">${esc(c.facility_type)}</span>` : `<span class="cell-none">—</span>`}</td>
      <td>${esc(c.location || "—")}</td>
      <td class="cell-sub">${esc(contact || "—")}</td>
      <td>${c.default_bill_rate ? `<strong>$${Math.round(c.default_bill_rate)}/hr</strong>` : "—"}</td>
      <td>${c.submissions || 0}</td>
      <td class="td-actions">
        <button class="btn small" data-client-edit="${esc(c.client_id)}" title="Edit"><i class="fas fa-pen"></i></button>
        <button class="btn small" data-client-del="${esc(c.client_id)}" title="Delete"><i class="fas fa-xmark"></i></button>
      </td>
    </tr>`;
  }
  async function loadClients(){
    const box = $("#clients-body");
    box.innerHTML = loading("Loading clients...");
    try {
      const d = await get("/api/clients");
      S.clients = d.items || [];
      const n = S.clients.length;
      $("#clients-sub").textContent = n ? `${n} client${n === 1 ? "" : "s"}` : "Facilities you place candidates into";
      box.innerHTML = n
        ? `<div class="table-wrap"><table class="table">
            <thead><tr><th>Client</th><th>Type</th><th>Location</th><th>Contact</th><th>Bill rate</th><th>Subs</th><th class="th-actions"></th></tr></thead>
            <tbody>${S.clients.map(clientRow).join("")}</tbody></table></div>`
        : `<div class="match-empty"><i class="fas fa-hospital"></i><h3>No clients yet</h3>
           <p>Add the hospitals and facilities you place candidates into, so submissions link to a real client instead of a typed name.</p>
           <button class="btn primary" id="client-empty-new"><i class="fas fa-plus"></i>Add your first client</button></div>`;
      const en = $("#client-empty-new");
      if (en) en.onclick = () => newClient();
      $$("#clients-body [data-client-view]").forEach(b => b.onclick = () => openClient(b.dataset.clientView));
      $$("#clients-body [data-client-edit]").forEach(b => b.onclick = () => editClient(b.dataset.clientEdit));
      $$("#clients-body [data-client-del]").forEach(b => b.onclick = () => deleteClient(b.dataset.clientDel));
    } catch(e) { box.innerHTML = errorState("Could not load clients"); }
  }
  function clientFields(c = {}){
    return [
      {name:"name", label:"Client name", required:true, wide:true, value:c.name || "", placeholder:"St. David's Medical Center"},
      {name:"facility_type", label:"Type", value:c.facility_type || "", placeholder:"Hospital, SNF, Clinic…"},
      {name:"city", label:"City", value:c.city || ""},
      {name:"state_code", label:"State", value:c.state_code || "", max:2},
      {name:"default_bill_rate", label:"Default bill rate ($/hr)", type:"number", step:"1", value:c.default_bill_rate ?? ""},
      {name:"website_url", label:"Website", value:c.website_url || "", placeholder:"https://…"},
      {name:"contact_name", label:"Contact name", value:c.contact_name || ""},
      {name:"contact_email", label:"Contact email", type:"email", value:c.contact_email || ""},
      {name:"contact_phone", label:"Contact phone", value:c.contact_phone || ""},
      {name:"notes", label:"Notes", type:"textarea", wide:true, value:c.notes || ""},
    ];
  }
  function clientBody(v){
    const br = parseFloat(v.default_bill_rate);
    return {
      name: v.name,
      facility_type: v.facility_type || null,
      city: v.city || null,
      state_code: v.state_code ? v.state_code.toUpperCase() : null,
      website_url: v.website_url || null,
      contact_name: v.contact_name || null,
      contact_email: v.contact_email || null,
      contact_phone: v.contact_phone || null,
      notes: v.notes || null,
      default_bill_rate: isNaN(br) ? null : br,
    };
  }
  async function newClient(){
    const v = await formDialog({title:"New client", submit:"Add client", fields: clientFields()});
    if (!v) return;
    try { await post("/api/clients", clientBody(v)); toast("Client added.", {title:"Saved"});
          S.clients = null; loadClients(); }
    catch(e) { toast(e.message, {title:"Could not add client", kind:"err"}); }
  }
  async function editClient(id){
    const c = (S.clients || []).find(x => x.client_id === id);
    const v = await formDialog({title:"Edit client", submit:"Save changes", fields: clientFields(c || {})});
    if (!v) return;
    try { await patch(`/api/clients/${id}`, clientBody(v)); toast("Client updated.", {title:"Saved"});
          S.clients = null; loadClients(); }
    catch(e) { toast(e.message, {title:"Could not save", kind:"err"}); }
  }
  async function deleteClient(id){
    const c = (S.clients || []).find(x => x.client_id === id);
    if (!await confirmDialog({
      title: "Delete client",
      body: `"${(c && c.name) || "This client"}" will be removed. Submissions already sent keep their facility name.`,
      confirm: "Delete", danger: true})) return;
    try { await del(`/api/clients/${id}`); S.clients = null; loadClients(); }
    catch(e) { toast(e.message || "That did not work.", {kind:"err"}); }
  }
  async function openClient(id){
    const box = $("#clients-body");
    box.innerHTML = loading("Loading client...");
    try {
      const d = await get(`/api/clients/${id}`);
      const c = d.client, subs = d.submissions || [];
      box.innerHTML = `
        <div class="pool-detail-head">
          <button class="btn ghost" id="client-back"><i class="fas fa-arrow-left"></i>All clients</button>
          <h2>${esc(c.name)}</h2><div class="spacer"></div>
          <button class="btn" id="client-edit-detail"><i class="fas fa-pen"></i>Edit</button>
        </div>
        <div class="profile-panel"><div class="profile-grid">
          ${fieldRow("Type", c.facility_type)}
          ${fieldRow("Location", c.location)}
          ${fieldRow("Default bill rate", c.default_bill_rate ? `$${Math.round(c.default_bill_rate)}/hr` : "")}
          ${fieldRow("Contact", c.contact_name)}
          ${fieldRow("Email", c.contact_email)}
          ${fieldRow("Phone", c.contact_phone)}
          ${fieldRow("Website", c.website_url)}
          ${fieldRow("Submissions", String(c.submissions || 0))}
        </div>${c.notes ? `<p class="muted" style="margin-top:12px">${esc(c.notes)}</p>` : ""}</div>
        <div class="an-section"><h2>Submissions to this client</h2>${
          subs.length ? `<div class="table-wrap"><table class="table">
            <thead><tr><th>Candidate</th><th>Status</th><th>Bill</th><th>Pay</th><th>Margin</th></tr></thead>
            <tbody>${subs.map(s => `<tr>
              <td>${esc(s.candidate)}</td>
              <td><span class="badge">${esc(s.status.replace("_", " "))}</span></td>
              <td>${s.bill_rate ? `$${Math.round(s.bill_rate)}` : "—"}</td>
              <td>${s.pay_rate ? `$${Math.round(s.pay_rate)}` : "—"}</td>
              <td>${s.margin != null ? `<span class="margin-pos">$${Math.round(s.margin)}</span>` : "—"}</td>
            </tr>`).join("")}</tbody></table></div>`
          : `<p class="muted" style="font-size:13px">No submissions to this client yet.</p>`}</div>`;
      $("#client-back").onclick = loadClients;
      $("#client-edit-detail").onclick = () => editClient(id);
    } catch(e) { box.innerHTML = errorState("Could not load this client"); }
  }

  // --- Placements (submissions that reached "placed") ----------------------
  async function loadPlacements(){
    const box = $("#placements-body");
    box.innerHTML = loading("Loading placements...");
    try {
      const d = await get("/api/submissions?status=placed");
      const items = d.items || [];
      const totalMargin = items.reduce((a, s) => a + (s.margin || 0), 0);
      $("#placements-sub").textContent = items.length
        ? `${items.length} placement${items.length === 1 ? "" : "s"}`
          + (totalMargin ? ` · $${Math.round(totalMargin)}/hr total margin` : "")
        : "Candidates placed with clients";
      box.innerHTML = items.length
        ? `<div class="table-wrap"><table class="table">
            <thead><tr><th>Candidate</th><th>Client</th><th>Bill</th><th>Pay</th><th>Margin</th><th>Placed</th></tr></thead>
            <tbody>${items.map(s => `<tr>
              <td><div class="cell-name">${esc(s.candidate)}</div>
                  <div class="cell-sub">${esc([s.profession_type, s.specialty].filter(Boolean).join(" · "))}</div></td>
              <td>${esc(s.facility || "—")}</td>
              <td>${s.bill_rate ? `$${Math.round(s.bill_rate)}/hr` : "—"}</td>
              <td>${s.pay_rate ? `$${Math.round(s.pay_rate)}/hr` : "—"}</td>
              <td>${s.margin != null ? `<span class="margin-pos">$${Math.round(s.margin)}/hr</span>` : "—"}</td>
              <td>${esc(shortTime(s.status_updated_at || s.submitted_at))}</td>
            </tr>`).join("")}</tbody></table></div>`
        : `<div class="match-empty"><i class="fas fa-user-check"></i><h3>No placements yet</h3>
           <p>When a submission reaches the <b>placed</b> stage it appears here with its realized margin. Move a submission to "placed" on the Submissions page.</p></div>`;
    } catch(e) { box.innerHTML = errorState("Could not load placements"); }
  }

  // --- Privacy (professional) ----------------------------------------------
  async function loadPrivacy(){
    const box = $("#profile-privacy");
    if (!box) return;
    let html = "";
    // Directory privacy applies only to professionals (recruiters aren't listed).
    if (!isRecruiter()){
      try {
        const p = await get("/api/privacy/me/status");
        if (p.has_profile){
          html += `<div class="privacy-wrap">
            <h3>Your privacy</h3>
            <p>Recruiters can search this directory, but your name and contact details
               stay hidden until one deliberately releases your profile — which is
               recorded.${p.times_contact_released
                 ? ` Your details have been released <b>${p.times_contact_released}</b> time${p.times_contact_released === 1 ? "" : "s"}.`
                 : " Nobody has released your details yet."}</p>
            <div class="privacy-actions">
              <span class="privacy-state ${p.listed ? "on" : "off"}">${p.listed ? "Listed in the directory" : "Not listed"}</span>
              <span class="spacer"></span>
              <button class="btn ghost small" id="privacy-export"><i class="fas fa-download"></i>Download my data</button>
              ${p.listed
                ? `<button class="btn danger small" id="privacy-delist"><i class="fas fa-eye-slash"></i>Remove me from the directory</button>`
                : `<button class="btn small" id="privacy-relist"><i class="fas fa-eye"></i>List me again</button>`}
            </div>
          </div>`;
        }
      } catch(e) { /* skip the directory section if status can't load */ }
    }
    // Deleting the account is available to everyone.
    html += `<div class="privacy-wrap danger-zone">
      <h3>Delete account</h3>
      <p>Permanently close your account and erase your personal details — your
         profile, contact information and résumé. This cannot be undone.</p>
      <div class="privacy-actions"><span class="spacer"></span>
        <button class="btn danger small" id="account-delete"><i class="fas fa-trash"></i>Delete my account</button>
      </div>
    </div>`;
    box.innerHTML = html;

    const ex = $("#privacy-export");
    if (ex) ex.onclick = exportMyData;
    const dl = $("#privacy-delist");
    if (dl) dl.onclick = async () => {
      if (!await confirmDialog({
        title: "Remove yourself from the directory",
        body: "Your email, phone and résumé are erased and "
            + "recruiters stop seeing you in search. Your account stays open — "
            + "you can list yourself again later.",
        confirm: "Erase my details", danger: true})) return;
      try { const r = await post("/api/privacy/me/delist", {});
            toast(r.message, {title:"Removed from the directory", ms:7000});
            loadPrivacy(); loadProfile(); }
      catch(e) { toast(e.message || "That did not work.", {title:"Something went wrong", kind:"err"}); }
    };
    const rl = $("#privacy-relist");
    if (rl) rl.onclick = async () => {
      try { const r = await post("/api/privacy/me/relist", {});
            toast(r.message, {title:"Back in the directory", ms:7000});
            loadPrivacy(); loadProfile(); }
      catch(e) { toast(e.message || "That did not work.", {title:"Something went wrong", kind:"err"}); }
    };
    const ad = $("#account-delete");
    if (ad) ad.onclick = deleteAccount;
  }
  async function deleteAccount(){
    const v = await formDialog({
      title: "Delete your account",
      intro: "This permanently closes your account and erases your personal "
           + "details. It cannot be undone. Enter your password to confirm.",
      submit: "Delete my account",
      fields: [{name:"password", label:"Your password", type:"password",
                required:true, wide:true, placeholder:"Confirm with your password"}],
    });
    if (!v) return;
    try {
      await post("/api/privacy/me/delete", {password: v.password});
      toast("Your account has been deleted.", {title:"Account closed", ms:3000});
      setTimeout(() => { setToken(""); setRefresh(""); location.href = "/"; }, 1400);
    } catch(e) {
      toast(e.status === 400 ? "That password is incorrect." : (e.message || "That did not work."),
            {title:"Could not delete account", kind:"err"});
    }
  }
  async function exportMyData(){
    try {
      const data = await get("/api/privacy/me/export");
      const blob = new Blob([JSON.stringify(data, null, 2)], {type:"application/json"});
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = "my-healthboard-data.json";
      document.body.appendChild(a); a.click();
      setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 0);
    } catch(e) { toast(e.message || "The export did not run.",
                       {title:"Export failed", kind:"err"}); }
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
      const [c, txns, usage, packs] = await Promise.all([
        get("/api/credits"), get("/api/credits/transactions?limit=40"),
        get("/api/credits/usage"),
        get("/api/credits/packs").catch(() => ({enabled:false, packs:[]}))]);
      S.credits = c;
      $("#credits-sub").textContent = c.enabled
        ? `${c.lifetime_spent} spent of ${c.lifetime_granted} granted`
        : "Credits are currently switched off — nothing is being charged.";
      const money = cents => `$${Math.round(cents / 100)}`;
      const packSection = `
        <div class="an-section"><h2>Buy credits</h2>
          ${packs.enabled && packs.packs.length
            ? `<div class="pack-grid">${packs.packs.map(p => `
                <div class="pack-card">
                  <div class="pack-label">${esc(p.label)}</div>
                  <div class="pack-credits"><b>${p.credits}</b><span>credits</span></div>
                  <div class="pack-price">${money(p.price_cents)}</div>
                  <div class="pack-unit">$${(p.price_cents / 100 / p.credits).toFixed(2)} / credit</div>
                  <button class="btn primary full" data-buy="${esc(p.pack_id)}">Buy</button>
                </div>`).join("")}</div>
              <p class="merge-hint">Secure checkout by Stripe. Credits land in your balance the moment payment completes.</p>`
            : `<p class="muted" style="font-size:13px">Self-service purchasing isn't switched on yet — ask your administrator to top up your balance for now.</p>`}
        </div>`;
      box.innerHTML = `
        <div class="credit-hero"><b>${c.balance}</b><span>credits remaining</span></div>
        ${packSection}
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
      $$("#credits-body [data-buy]").forEach(b => b.onclick = () => buyCredits(b.dataset.buy, b));
    } catch(e) { box.innerHTML = errorState("Could not load credits"); }
  }
  async function buyCredits(packId, btn){
    if (btn){ btn.disabled = true; btn.textContent = "Starting…"; }
    try {
      const r = await post("/api/credits/checkout", {pack_id: packId});
      if (r.url) { location.href = r.url; return; }   // hand off to Stripe
      throw new Error("No checkout URL");
    } catch(e) {
      if (btn){ btn.disabled = false; btn.textContent = "Buy"; }
      toast(e.status === 503 ? "Purchasing isn't set up yet — ask your administrator to top you up."
          : (e.message || "Could not start checkout."), {title:"Checkout unavailable", kind:"err"});
    }
  }
  // Stripe redirects back with ?purchase=success|cancel after checkout.
  function handlePurchaseReturn(){
    const params = new URLSearchParams(location.search);
    const purchase = params.get("purchase");
    if (!purchase) return;
    if (purchase === "success"){
      toast("Payment received — your credits will appear here in a moment.",
            {title:"Thanks for your purchase", ms:7000});
      // The webhook grants a beat after the redirect; refresh once it's landed.
      setTimeout(() => { refreshCredits();
        if ($("#page-credits").classList.contains("active")) loadCredits(); }, 2500);
    } else if (purchase === "cancel"){
      toast("Checkout cancelled — no charge was made.", {kind:"info"});
    }
    params.delete("purchase");   // don't re-toast on refresh
    const qs = params.toString();
    history.replaceState({}, "", location.pathname + (qs ? "?" + qs : ""));
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
        <div class="an-section"><h2>Starter templates</h2>
          <p class="muted" style="font-size:13px;margin:0 0 12px">Proven outreach messages — click <b>Use</b> to add one to your templates, then tweak it.</p>
          <div class="starter-grid">
            ${BUILTIN_TEMPLATES.map((t, i) => `<div class="starter-card">
              <div class="starter-name">${esc(t.name)}</div>
              <div class="starter-desc">${esc(t.desc)}</div>
              <div class="starter-subj"><i class="fas fa-envelope"></i>${esc(t.subject)}</div>
              <button class="btn ghost small" data-builtin="${i}"><i class="fas fa-plus"></i>Use</button>
            </div>`).join("")}
          </div>
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
        catch(err) { toast(err.message || "That did not work.", {title:"Something went wrong", kind:"err"}); }
      });
      $$("#outreach-body [data-builtin]").forEach(b => b.onclick = () =>
        newTemplate(BUILTIN_TEMPLATES[+b.dataset.builtin]));
      $$("#outreach-body .camp-card").forEach(c => c.onclick = () => openCampaign(c.dataset.camp));
    } catch(e) { box.innerHTML = errorState("Could not load outreach"); }
  }
  async function newTemplate(preset){
    const DEFAULT_BODY = ["Hi {{first_name}},", "",
      "I'm recruiting for {{specialty}} roles near {{city}}. With your "
      + "{{years_experience}} years of experience I thought it might be a fit.", "",
      "Would you be open to a quick chat?"].join("\n");
    const v = await formDialog({
      title: preset ? "Use starter template" : "New email template",
      intro: "Merge fields like {{first_name}}, {{specialty}} and {{city}} are filled "
           + "per recipient when the campaign sends. Edit anything before saving.",
      submit: "Save template",
      fields: [
        {name:"name", label:"Template name", required:true, wide:true, placeholder:"ICU intro",
         value: preset ? preset.name : ""},
        {name:"subject", label:"Subject line", required:true, wide:true,
         value: preset ? preset.subject : "{{specialty}} roles near {{city}}"},
        {name:"body", label:"Message", type:"textarea", required:true, wide:true,
         value: preset ? preset.body : DEFAULT_BODY},
      ],
    });
    if (!v) return;
    try {
      await post("/api/outreach/templates", {name:v.name, subject:v.subject, body:v.body});
      toast("Ready to use in a campaign.", {title:"Template saved"});
      loadOutreach();
    } catch(e) {
      toast(e.status === 409 ? "You already have a template with that name." : e.message,
            {title:"Could not save", kind:"err"});
    }
  }

  // Built-in starter templates recruiters can drop in and tweak. Merge fields
  // ({{first_name}}, {{specialty}}, {{city}}, {{state_code}}, {{years_experience}},
  // {{profession_type}}) are filled per recipient when a campaign sends.
  const BUILTIN_TEMPLATES = [
    {name: "First-touch intro",
     desc: "Warm opener for a candidate you've just sourced.",
     subject: "{{specialty}} roles near {{city}}",
     body: ["Hi {{first_name}},", "",
       "I'm a healthcare recruiter working with facilities near {{city}}, {{state_code}}, "
       + "and your {{specialty}} background stood out. With {{years_experience}} years in, "
       + "you'd be a strong fit for a few roles I'm filling right now.", "",
       "Open to a quick chat this week to hear the details?", "",
       "Best,"].join("\n")},
    {name: "Travel contract offer",
     desc: "Pitch a specific travel assignment with pay.",
     subject: "13-week {{specialty}} travel contract — great pay",
     body: ["Hi {{first_name}},", "",
       "I have a 13-week {{specialty}} travel contract opening up near {{city}} with "
       + "competitive weekly pay plus a housing stipend. Given your experience, I can "
       + "likely fast-track you.", "",
       "Want me to send the full pay package and details?", "",
       "Thanks,"].join("\n")},
    {name: "Gentle follow-up",
     desc: "Second touch when your first note went quiet.",
     subject: "Following up — {{specialty}} roles",
     body: ["Hi {{first_name}},", "",
       "Just floating this back to the top of your inbox. I'm still working on {{specialty}} "
       + "roles near {{city}} and would love to share what's open.", "",
       "Even if the timing isn't right now, happy to keep you posted on the best ones.", "",
       "Best,"].join("\n")},
    {name: "Compact / multi-state",
     desc: "For clinicians who can work across states.",
     subject: "Multi-state {{specialty}} opportunities",
     body: ["Hi {{first_name}},", "",
       "If you hold a compact license, I have {{specialty}} roles across several states — "
       + "including some well beyond {{city}} — with quick starts and strong pay.", "",
       "Would you like me to line up options that match where you'd want to go next?", "",
       "Thanks,"].join("\n")},
    {name: "Re-engage past candidate",
     desc: "Reconnect with someone you spoke to before.",
     subject: "New {{specialty}} openings since we last talked",
     body: ["Hi {{first_name}},", "",
       "It's been a while! Some new {{specialty}} roles near {{city}} just landed on my "
       + "desk, and I immediately thought of you.", "",
       "Are you open to hearing what's changed? Even a quick catch-up would be great.", "",
       "Best,"].join("\n")},
  ];
  async function newCampaign(){
    if (!S.pools.length){
      try { S.pools = (await get("/api/pools")).items || []; } catch(e) { S.pools = []; }
    }
    if (!S.pools.length)
      return toast("Create a talent pool first — a campaign sends to a pool.", {kind:"err"});
    if (!S.templates.length) return toast("Create a template first.", {kind:"err"});
    const v = await formDialog({
      title: "New campaign",
      intro: "Only candidates whose contact you have released can be emailed — the "
           + "rest are skipped automatically.",
      submit: "Build campaign",
      fields: [
        {name:"name", label:"Campaign name", required:true, wide:true,
         value:S.pools[0].name + " — " + S.templates[0].name},
        {name:"pool_id", label:"Send to pool", type:"select",
         options:S.pools.map(p => [p.pool_id, p.name + " (" + p.member_count + ")"])},
        {name:"template_id", label:"Template", type:"select",
         options:S.templates.map(t => [t.template_id, t.name])},
      ],
    });
    if (!v) return;
    try {
      const r = await post("/api/outreach/campaigns",
        {name:v.name, pool_id:v.pool_id, template_id:v.template_id});
      toast(r.ready_to_send + " ready to send, " + r.skipped
            + " skipped (no released contact, no email, or opted out).",
            {title:"Campaign built", ms:6500});
      loadOutreach();
    } catch(e) { toast(e.message, {title:"Could not build campaign", kind:"err"}); }
  }
  async function sendCampaign(id){
    if (!await confirmDialog({
      title: "Send campaign",
      body: "Emails go out immediately to everyone on the list who has not "
          + "opted out. Sending cannot be recalled.",
      confirm: "Send now"})) return;
    try {
      const r = await post(`/api/outreach/campaigns/${id}/send`, {});
      if (r.simulated) {
        toast(`${r.would_send} recipient${r.would_send === 1 ? "" : "s"} ready to send. `
              + "Nothing was delivered — no email provider is connected yet.",
              {title:"Preview only", kind:"info", ms:8000});
      } else {
        toast(`Sent ${r.sent}, skipped ${r.skipped}, failed ${r.failed}.`,
              {title:"Campaign sent", ms:7000});
      }
      loadOutreach();
    } catch(e) { toast(e.message || "That did not work.", {title:"Something went wrong", kind:"err"}); }
  }
  async function openCampaign(id){
    try {
      const d = await get(`/api/outreach/campaigns/${id}`);
      const reasons = Object.entries(d.skip_reasons || {})
        .map(([r,n]) => `${n} ${r}`).join(" · ") || "none";
      await infoDialog(d.name, [
        ["Subject", d.subject],
        ["Recipients", `${d.total}`],
        ["Sent", `${d.sent}`],
        ["Opened", `${d.opened}`],
        ["Replied", `${d.replied}`],
        ["Skipped", reasons],
      ]);
    } catch(e) { toast(e.message || "That did not work.", {title:"Something went wrong", kind:"err"}); }
  }

  // --- Analytics -----------------------------------------------------------
  // Validated categorical palette (dataviz skill reference order — passes the
  // adjacent-pair CVD gates in light mode). Fixed order, never cycled.
  const VIZ_CAT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"];
  // Blue ordinal ramp for ordered funnel stages (steps 250→700; light end ≥2:1).
  const VIZ_FUNNEL = ["#86b6ef", "#5598e7", "#3987e5", "#256abf", "#184f95", "#0d366b"];
  const POOL_STAGE_ORDER = ["sourced", "contacted", "screening", "submitted", "hired", "rejected"];
  const STAGE_COLORS = Object.fromEntries(POOL_STAGE_ORDER.map((s, i) => [s, VIZ_CAT[i % VIZ_CAT.length]]));

  // --- SVG chart primitives (self-contained, no library) -------------------
  function svgDonut(segs, opt = {}){
    const size = opt.size || 172, thick = opt.thick || 26;
    const clean = segs.filter(s => s.value > 0);
    const total = clean.reduce((a, s) => a + s.value, 0);
    const r = (size - thick) / 2, cx = size / 2, cy = size / 2, C = 2 * Math.PI * r;
    let angle = 0;
    const arcs = clean.map(s => {
      const frac = s.value / total, len = frac * C;
      const gap = clean.length > 1 ? Math.min(3.5, len * 0.4) : 0;
      const dash = Math.max(0.5, len - gap);
      const seg = `<circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="${s.color}" stroke-width="${thick}"
        stroke-dasharray="${dash} ${C - dash}" transform="rotate(${angle - 90} ${cx} ${cy})"><title>${esc(s.label)}: ${s.value.toLocaleString()} (${Math.round(frac * 100)}%)</title></circle>`;
      angle += frac * 360;
      return seg;
    }).join("");
    const centerVal = opt.centerValue != null ? opt.centerValue : total;
    return `<svg viewBox="0 0 ${size} ${size}" class="viz-donut" role="img" aria-label="${esc(opt.centerLabel || "donut")}">
      <circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="var(--line)" stroke-width="${thick}"/>
      ${arcs}
      <text x="${cx}" y="${cy - 1}" text-anchor="middle" class="viz-big">${Number(centerVal).toLocaleString()}</text>
      <text x="${cx}" y="${cy + 18}" text-anchor="middle" class="viz-cap">${esc(opt.centerLabel || "total")}</text>
    </svg>`;
  }
  const vizLegend = segs => `<div class="viz-legend">${segs.filter(s => s.value > 0).map(s =>
    `<span class="viz-leg"><i style="background:${s.color}"></i>${esc(s.label)}<b>${s.value.toLocaleString()}</b></span>`).join("")}</div>`;

  const compactNum = n => n >= 1e6 ? (n / 1e6).toFixed(n >= 1e7 ? 0 : 1) + "M"
    : n >= 1000 ? (n / 1000).toFixed(n >= 1e5 ? 0 : 1) + "k" : String(n);
  function hbars(items, opt = {}){
    const fmt = opt.fmt || (v => v.toLocaleString());
    const max = Math.max(1, ...items.map(i => i.value));
    return `<div class="viz-bars">${items.map((it, idx) => {
      const color = it.color || (opt.ramp ? opt.ramp[Math.min(idx, opt.ramp.length - 1)] : "var(--accent)");
      const pct = 100 * it.value / max;
      return `<div class="viz-brow" title="${esc(it.label)}: ${it.value.toLocaleString()}">
        <span class="viz-blbl" title="${esc(it.label)}">${esc(it.label)}</span>
        <span class="viz-btrack"><i class="viz-bfill" style="width:${it.value ? Math.max(3, pct) : 0}%;background:${color}"></i></span>
        <span class="viz-bval">${fmt(it.value)}</span></div>`;
    }).join("")}</div>`;
  }

  function svgGauge(pct, opt = {}){
    const size = opt.size || 156, thick = 15, r = (size - thick) / 2, cx = size / 2, cy = size / 2, C = 2 * Math.PI * r;
    const frac = Math.max(0, Math.min(100, pct)) / 100;
    return `<svg viewBox="0 0 ${size} ${size}" class="viz-gauge" role="img" aria-label="${esc((opt.label || "") + " " + Math.round(pct) + "%")}">
      <circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="var(--line)" stroke-width="${thick}"/>
      <circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="${opt.color || "var(--accent)"}" stroke-width="${thick}" stroke-linecap="round"
        stroke-dasharray="${frac * C} ${C}" transform="rotate(-90 ${cx} ${cy})"/>
      <text x="${cx}" y="${cy - 1}" text-anchor="middle" class="viz-big">${Math.round(pct)}%</text>
      <text x="${cx}" y="${cy + 18}" text-anchor="middle" class="viz-cap">${esc(opt.label || "")}</text>
    </svg>`;
  }
  const vizCard = (title, body, sub = "") =>
    `<div class="viz-card"><div class="viz-head"><h3>${esc(title)}</h3>${sub ? `<span class="viz-sub">${esc(sub)}</span>` : ""}</div>${body}</div>`;

  function stat(value, label, sub, accent){
    return `<div class="an-stat"><b${accent ? ' class="accent"' : ""}>${esc(value)}</b>
      <span>${esc(label)}</span>${sub ? `<small>${esc(sub)}</small>` : ""}</div>`;
  }
  // Ordered ATS pipeline for the hiring funnel (terminal states shown apart).
  const FUNNEL_ORDER = [["applied","Applied"],["screening","Screening"],
                        ["interview","Interview"],["offer","Offer"],["hired","Hired"]];

  async function loadAnalytics(){
    const box = $("#analytics-panel");
    box.innerHTML = loading("Loading your activity...");
    try {
      // Market = real supply/demand (impressive, populated); sourcing = this
      // recruiter's own activity. The application funnel is intentionally not
      // shown — with no submissions/applications yet it would be all zeros.
      const [mk, d, convo] = await Promise.all([
        get("/api/analytics/market"),
        get("/api/analytics/sourcing?days=30"),
        get("/api/analytics/conversations").catch(() => null),
      ]);
      const P = mk.providers, pools = d.pools, runs = d.sourcing_runs, msg = d.messaging, con = d.contacts;
      const stages = pools.by_stage || {};
      const poolSegs = POOL_STAGE_ORDER.map(s => ({
        label: s.charAt(0).toUpperCase() + s.slice(1), value: stages[s] || 0, color: STAGE_COLORS[s]}));
      const totalStaged = poolSegs.reduce((a, s) => a + s.value, 0);
      const cn = compactNum;

      const glance = `<div class="an-section"><h2>At a glance</h2><div class="an-grid">
        ${stat(cn(P.listable), "Providers", "screened & listable", true)}
        ${stat(mk.jobs_active.toLocaleString(), "Open roles", "live on the board")}
        ${stat(P.states, "States covered")}
        ${stat(con.released_total, "Contacts revealed", `${con.released_recent} in ${d.window_days} days`, true)}
        ${stat(pools.shortlisted, "Shortlisted", `${pools.pools} pool${pools.pools === 1 ? "" : "s"}`)}
        ${stat(mk.credits.spent, "Credits spent", `${mk.credits.balance} remaining`)}
      </div></div>`;

      // --- Talent market (supply & demand) ---
      const reachBody = `<div class="viz-gauge-wrap">${svgGauge(P.reachable_pct, {label: "reachable"})}
        <div class="viz-side">
          <div class="viz-fig"><b>${cn(P.listable)}</b><span>listable providers</span></div>
          <div class="viz-fig"><b>${cn(P.reachable)}</b><span>have email or phone</span></div>
        </div></div>`;
      const supplyBody = mk.supply.length
        ? hbars(mk.supply.map(x => ({label: x.label, value: x.count})), {fmt: cn})
        : emptyState("No providers yet", "", "fa-user-doctor");
      // Specialty demand as a composition donut (with an "Other" remainder).
      const specSegs = mk.demand_specialty.map((x, i) => ({label: x.label, value: x.count, color: VIZ_CAT[i % 7]}));
      const specShown = specSegs.reduce((a, s) => a + s.value, 0);
      if (mk.jobs_active - specShown > 0)
        specSegs.push({label: "Other", value: mk.jobs_active - specShown, color: "#c3d0e6"});
      const specBody = mk.demand_specialty.length
        ? `<div class="viz-donut-wrap">${svgDonut(specSegs, {centerValue: mk.jobs_active, centerLabel: "open roles"})}${vizLegend(specSegs)}</div>`
        : emptyState("No open roles yet", "", "fa-briefcase");
      const stateBody = mk.demand_state.length
        ? hbars(mk.demand_state.map(x => ({label: x.label, value: x.count})))
        : emptyState("No open roles yet", "", "fa-map-location-dot");
      // Two circular charts on top (reach + specialty mix), two bar sets below
      // (supply + state) — a balanced, scannable 2×2.
      const marketGrid = `<div class="viz-grid">
        ${vizCard("Directory reach", reachBody, "reachable share")}
        ${vizCard("Open roles by specialty", specBody, `${mk.jobs_active} live roles`)}
        ${vizCard("Talent supply", supplyBody, "providers by profession")}
        ${vizCard("Open roles by state", stateBody, "top locations hiring")}
      </div>`;

      // --- Your sourcing (personal activity) ---
      const donutBody = totalStaged
        ? `<div class="viz-donut-wrap">${svgDonut(poolSegs, {centerValue: totalStaged, centerLabel: "shortlisted"})}${vizLegend(poolSegs)}</div>`
        : emptyState("No candidates shortlisted yet", "Add candidates to a talent pool to see your pipeline.", "fa-layer-group");
      const outBody = (msg.sent || msg.received || msg.threads)
        ? hbars([
            {label: "Messages sent", value: msg.sent, color: VIZ_CAT[0]},
            {label: "Messages received", value: msg.received, color: VIZ_CAT[1]},
            {label: "Conversations", value: msg.threads, color: VIZ_CAT[2]}])
        : emptyState("No outreach yet", "Message revealed candidates to start conversations.", "fa-paper-plane");
      const respRate = msg.sent ? Math.round(100 * msg.received / msg.sent) : 0;
      const sourceGrid = `<div class="viz-grid">
          ${vizCard("Your talent-pool pipeline", donutBody, "where your shortlist sits")}
          ${vizCard("Your outreach", outBody, msg.sent ? `${respRate}% reply rate` : "")}
        </div>
        <div class="an-grid" style="margin-top:14px">
          ${stat(runs.runs, "Sourcing runs")}
          ${stat(runs.candidates_ranked.toLocaleString(), "Candidates ranked", `avg score ${runs.avg_match_score}`)}
          ${stat(d.saved_searches, "Saved searches")}
          ${stat(pools.worked_pct + "%", "Shortlist worked", `${pools.worked} past sourced`)}
        </div>`;

      const convoRows = (convo && convo.conversations || []);
      const convoSection = convoRows.length
        ? `<div class="an-section"><h2>Conversations</h2>
            <div class="table-wrap"><table class="table">
              <thead><tr><th>Contact</th><th>Stage</th><th>Sent</th><th>Received</th><th>Reply rate</th><th>Last activity</th></tr></thead>
              <tbody>${convoRows.slice(0, 25).map(c => `<tr>
                <td><div class="cell-name">${esc(c.candidate || "—")}</div></td>
                <td>${c.ats_stage ? `<span class="badge">${esc(c.ats_stage)}</span>` : `<span class="cell-none">—</span>`}</td>
                <td>${c.messages_sent}</td>
                <td>${c.messages_received}</td>
                <td>${Math.round((c.response_rate || 0) * 100)}%</td>
                <td>${c.last_message_at ? esc(shortTime(c.last_message_at)) : "—"}</td>
              </tr>`).join("")}</tbody></table></div></div>` : "";

      box.innerHTML = glance
        + `<div class="an-section"><h2>Talent market</h2>${marketGrid}</div>`
        + `<div class="an-section"><h2>Your sourcing</h2>${sourceGrid}</div>`
        + convoSection;
      $("#analytics-sub").textContent = `${cn(P.listable)} providers · ${mk.jobs_active} open roles · last ${d.window_days} days`;
    } catch(e) { box.innerHTML = errorState("Could not load analytics"); }
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
      catch(err) { toast(err.message || "That did not work.", {title:"Something went wrong", kind:"err"}); }
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
    const v = await formDialog({
      title: "Save this search",
      intro: "We'll alert you when new candidates match these filters.",
      submit: "Save search",
      fields: [{name:"name", label:"Name", required:true, wide:true,
                placeholder:"ICU RNs in Texas"}],
    });
    if (!v) return;
    const name = v.name;
    try {
      await post("/api/saved-searches", {name: name.trim(), params: currentSearchParams(), notify: true});
      loadSavedSearches();
    } catch(e) { toast(e.status === 409 ? "You already have a search with that name. Pick another."
                                        : (e.message || "The search was not saved."),
                       {title:"Could not save search", kind:"err"}); }
  }
  // Re-count every saved search; growth since the last check becomes a
  // notification. This is what makes the Notifications page live — and it runs
  // for professionals (job alerts) as well as recruiters (candidate alerts),
  // on app load, since there is no scheduler yet.
  async function checkSavedSearches(){
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

  // --- Applicant review (recruiter) ----------------------------------------
  // Who actually applied to a role, versus sourceForJob's cold ranking of the
  // whole directory. Applying reveals the candidate to this employer, so their
  // real name and contact are shown here.
  const APPLICANT_STATUS_LABEL = {
    applied:"Applied", screening:"Screening", interview:"Interview",
    offer:"Offer", hired:"Hired", rejected:"Rejected", withdrawn:"Withdrawn"};

  async function reviewApplicants(jobId){
    S.applicantsJobId = jobId;
    const job = (S.jobsById && S.jobsById.get(jobId)) || null;
    showPage("applicants");
    $("#applicants-title").textContent = job ? job.title : "Applicants";
    $("#applicants-sub").textContent = "Loading applicants…";
    const box = $("#applicants-body");
    box.innerHTML = loading("Loading applicants…");
    try {
      const d = await get(`/api/jobs/${jobId}/applicants`);
      renderApplicants(d);
    } catch(e) {
      box.innerHTML = e.status === 403
        ? `<div class="match-empty"><i class="fas fa-lock"></i><h3>Not your role</h3>
           <p>You can only review applicants for jobs your organisation posted.</p></div>`
        : errorState("Could not load applicants");
    }
  }

  function renderApplicants(d){
    const box = $("#applicants-body");
    const items = d.items || [];
    if (d.job) $("#applicants-title").textContent = d.job.title;
    $("#applicants-sub").textContent = items.length
      ? `${items.length} applicant${items.length === 1 ? "" : "s"} · ` +
        Object.entries(d.by_status).map(([s,c]) => `${c} ${APPLICANT_STATUS_LABEL[s] || s}`).join(" · ")
      : "No applicants yet";
    if (!items.length){
      box.innerHTML = `<div class="match-empty"><i class="fas fa-inbox"></i>
        <h3>No applicants yet</h3>
        <p>When candidates apply to this role they appear here, with their
           details and where each one stands. Source the directory to invite
           candidates to apply.</p>
        <button class="btn primary" data-source="${esc(d.job ? d.job.job_id : "")}"><i class="fas fa-bolt"></i>Source candidates</button></div>`;
      return;
    }
    box.innerHTML = `<div class="applicant-list">${items.map(applicantCard).join("")}</div>`;
    // Stage changes move a candidate through the pipeline and notify them.
    $$("#applicants-body [data-app-stage]").forEach(sel => sel.onchange = async () => {
      const id = sel.dataset.appStage, status = sel.value;
      sel.disabled = true;
      try {
        await patch(`/api/applications/${id}/stage`, {status});
        toast(`Moved to ${APPLICANT_STATUS_LABEL[status] || status}.`, {title:"Applicant updated"});
        reviewApplicants(S.applicantsJobId);
      } catch(e) {
        sel.disabled = false;
        toast(e.message || "That did not work.", {title:"Could not update", kind:"err"});
      }
    });
  }

  function applicantCard(a){
    const meta = [a.profession_type, a.specialty,
                  a.years_experience != null ? `${a.years_experience} yrs` : "",
                  a.location].filter(Boolean).join(" · ");
    const contact = [
      a.email ? `<a href="mailto:${esc(a.email)}"><i class="fas fa-envelope"></i>${esc(a.email)}</a>` : "",
      a.phone ? `<a href="tel:${esc(a.phone)}"><i class="fas fa-phone"></i>${esc(a.phone)}</a>` : "",
    ].filter(Boolean).join("");
    const options = [...(a.stages || []), "rejected"].map(s =>
      `<option value="${esc(s)}"${a.status === s ? " selected" : ""}>${esc(APPLICANT_STATUS_LABEL[s] || s)}</option>`).join("");
    const pillCls = a.status === "hired" ? "ok" : a.is_closed ? "no" : "";
    return `<div class="applicant-card">
      <div class="app-head">
        <h3>${esc(a.name)}</h3>
        <span class="status-pill ${pillCls}">${esc(APPLICANT_STATUS_LABEL[a.status] || a.status)}</span>
        <span class="spacer"></span>
        <span class="muted small">applied ${esc(shortTime(a.applied_at))}</span>
      </div>
      <div class="app-meta">${esc(meta || "Details not listed")}</div>
      ${contact ? `<div class="applicant-contact">${contact}</div>` : ""}
      ${a.cover_letter ? `<details class="applicant-cover"><summary>Cover letter</summary><p>${esc(a.cover_letter)}</p></details>` : ""}
      <div class="applicant-actions">
        ${a.is_closed
          ? `<span class="muted small">This application is ${esc(APPLICANT_STATUS_LABEL[a.status] || a.status).toLowerCase()}.</span>`
          : `<label class="applicant-stage">Stage
              <select class="input small" data-app-stage="${esc(a.application_id)}">${options}</select></label>`}
        ${a.resume_url ? `<button class="btn small" data-resume="${esc(a.profile_id)}"><i class="fas fa-file-lines"></i>Résumé</button>` : ""}
        <button class="btn small primary" data-message="${esc(a.profile_id)}"><i class="fas fa-comment-dots"></i>Message</button>
      </div>
    </div>`;
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
    // Picking a pool by typing its number in a list was the worst of the
    // prompt flows — this is a dropdown of real pools, with "new" as an option.
    const options = [...S.pools.map(p => [p.pool_id, p.name + " (" + p.member_count + ")"]),
                     ["__new__", "+ Create a new pool"]];
    const v = await formDialog({
      title: "Add " + profileIds.length + " candidate"
             + (profileIds.length === 1 ? "" : "s") + " to a pool",
      submit: "Add to pool",
      fields: [
        {name:"pool_id", label:"Pool", type:"select", options, wide:true},
        {name:"new_name", label:"New pool name", hint:"only if creating one", wide:true},
      ],
    });
    if (!v) return;
    try {
      let pool = S.pools.find(p => p.pool_id === v.pool_id);
      if (!pool){
        if (!v.new_name) return toast("Give the new pool a name.", {kind:"err"});
        pool = await post("/api/pools", {name: v.new_name, color:"blue"});
        S.pools.unshift(pool);
      }
      const res = await post(`/api/pools/${pool.pool_id}/members`, {profile_ids: profileIds});
      profileIds.forEach(pid => S.poolMembership.set(pid,
        [...new Set([...(S.poolMembership.get(pid) || []), pool.pool_id])]));
      paintPoolButtons();
      toast("Added " + res.added + " candidate" + (res.added === 1 ? "" : "s")
            + (res.skipped ? " (" + res.skipped + " already there)." : "."),
            {title: pool.name});
    } catch(e) {
      toast(e.status === 409 ? "You already have a pool with that name." : e.message,
            {kind:"err"});
    }
  }

  // --- Talent pools --------------------------------------------------------
  function poolCard(p){
    const stages = Object.entries(p.stages || {})
      .map(([s,n]) => `<span class="pool-stage-chip">${esc(s)} ${esc(n)}</span>`).join("");
    return `<div class="pool-card" data-pool="${p.pool_id}">
      <button class="pool-del" data-pool-del="${p.pool_id}" title="Delete pool"><i class="fas fa-trash"></i></button>
      <h3>${esc(p.name)}</h3>
      ${p.job_title ? `<div class="pool-job"><i class="fas fa-clipboard-list"></i>${esc(p.job_title)}</div>` : ""}
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
        if (!await confirmDialog({
          title: `Delete ${pool ? pool.name : "this pool"}`,
          body: "The list and its notes are deleted. The candidates "
              + "themselves stay in the directory.",
          confirm: "Delete pool", danger: true})) return;
        try { await del(`/api/pools/${b.dataset.poolDel}`); loadPools(); }
        catch(err) { toast(err.message || "That did not work.", {title:"Something went wrong", kind:"err"}); }
      });
    } catch(e) { box.innerHTML = errorState("Could not load talent pools"); }
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
        <button class="btn small" data-message="${m.profile_id}" title="Message this candidate"><i class="fas fa-comment-dots"></i></button>
        <button class="btn small" data-submit="${m.profile_id}" title="Submit to a client"><i class="fas fa-share-from-square"></i></button>
        <button class="btn small" data-pool-remove="${m.profile_id}" title="Remove from pool"><i class="fas fa-xmark"></i></button>
      </td>
    </tr>`;
  }

  async function emailPool(poolId){
    const v = await formDialog({
      title: "Email this pool",
      intro: "Send one message to every candidate in this pool whose contact you've "
           + "revealed. Replies come to your inbox; candidates you haven't revealed are skipped.",
      submit: "Send to pool",
      fields: [
        {name:"subject", label:"Subject", required:true,
         placeholder:"A role that fits your background"},
        {name:"body", label:"Message", type:"textarea", required:true, wide:true,
         placeholder:"Hi — I'm reaching out about an opportunity that matches your experience…"},
      ],
    });
    if (!v) return;
    try {
      const r = await post(`/api/pools/${poolId}/email`, {subject: v.subject, body: v.body});
      const parts = [`${r.sent} sent`];
      if (r.skipped_not_revealed) parts.push(`${r.skipped_not_revealed} skipped — reveal contact first`);
      if (r.skipped_no_email) parts.push(`${r.skipped_no_email} skipped — no email`);
      toast(parts.join(" · "), {title:"Pool emailed", ms:6500});
    } catch(e){ toast(e.message || "Could not email the pool.", {kind:"err"}); }
  }
  async function openPool(poolId, stage=""){
    S.activePool = poolId; S.poolStage = stage;
    const box = $("#pools-body");
    box.innerHTML = loading("Loading pool...");
    try {
      const d = await get(`/api/pools/${poolId}/members${stage ? `?stage=${stage}` : ""}`);
      const p = d.pool;
      const listed = (S.pools || []).find(x => x.pool_id === poolId);
      const poolJob = p.job_title || (listed && listed.job_title) || null;
      const counts = p.stages || {};
      const tabs = [["", `All ${p.member_count}`]].concat(
        ATS_STAGES.map(s => [s, `${s[0].toUpperCase()+s.slice(1)} ${counts[s] || 0}`]));
      box.innerHTML = `
        <div class="pool-detail-head">
          <button class="btn ghost" id="pool-back"><i class="fas fa-arrow-left"></i>All pools</button>
          <div><h2>${esc(p.name)}</h2>${poolJob ? `<div class="pool-detail-job"><i class="fas fa-clipboard-list"></i>Sourcing for ${esc(poolJob)}</div>` : ""}</div>
          <div class="spacer"></div>
          <a class="btn" id="pool-export" href="#"><i class="fas fa-file-csv"></i>Export CSV</a>
          <button class="btn primary" id="pool-email"><i class="fas fa-envelope"></i>Email pool</button>
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
      $("#pool-email").onclick = () => emailPool(poolId);
      $$("#pools-body [data-pstage]").forEach(b => b.onclick = () => openPool(poolId, b.dataset.pstage));
      $$("#pools-body [data-stage-for]").forEach(sel => sel.onchange = async () => {
        try { await patch(`/api/pools/${poolId}/members/${sel.dataset.stageFor}`, {stage: sel.value}); openPool(poolId, S.poolStage); }
        catch(err) { toast(err.message || "That did not work.", {title:"Something went wrong", kind:"err"}); }
      });
      $$("#pools-body [data-note-for]").forEach(b => b.onclick = async () => {
        const row = (d.items || []).find(x => x.profile_id === b.dataset.noteFor);
        const v = await formDialog({
          title: "Note about this candidate",
          submit: "Save note",
          fields: [{name:"note", label:"Note", type:"textarea", wide:true,
                    value:(row && row.note) || ""}],
        });
        if (!v) return;
        const note = v.note;
        try { await patch(`/api/pools/${poolId}/members/${b.dataset.noteFor}`, {note}); openPool(poolId, S.poolStage); }
        catch(err) { toast(err.message || "That did not work.", {title:"Something went wrong", kind:"err"}); }
      });
      $$("#pools-body [data-pool-remove]").forEach(b => b.onclick = async () => {
        try { await del(`/api/pools/${poolId}/members/${b.dataset.poolRemove}`); openPool(poolId, S.poolStage); }
        catch(err) { toast(err.message || "That did not work.", {title:"Something went wrong", kind:"err"}); }
      });
      // [data-resume] is handled by the global click listener in wire().
    } catch(e) {
      console.error("openPool failed", e);
      box.innerHTML = errorState("Could not load this pool");
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
    } catch(e) { toast(e.message || "The export did not run.",
                       {title:"Export failed", kind:"err"}); }
  }

  async function createPool(){
    // Offer the agency's open job orders so a pool says which role it's for.
    const jobOpts = [["", "— Not tied to a specific role —"]];
    try {
      const d = await get("/api/employers/me/dashboard");
      (d.jobs || []).filter(j => j.status === "active").forEach(j =>
        jobOpts.push([j.job_id, j.title + (j.city ? ` — ${j.city}${j.state_code ? ", " + j.state_code : ""}` : "")]));
    } catch(e) { /* no org yet — pool can still be created untied */ }
    const v = await formDialog({
      title: "New talent pool",
      intro: "A shortlist you can move through stages, export, and email as a campaign. "
           + "Link it to a job order so the desk knows what it's for.",
      submit: "Create pool",
      fields: [
        {name:"name", label:"Pool name", required:true, wide:true,
         placeholder:"ICU travel RNs — Q3"},
        {name:"job_id", label:"Sourcing for (job order)", type:"select", options: jobOpts, wide:true},
        {name:"description", label:"Description", hint:"optional", wide:true},
        {name:"visibility", label:"Who can work it", type:"select",
         options:[["private","Only me"],["team","Everyone at my agency"]]},
      ],
    });
    if (!v) return;
    try {
      await post("/api/pools", {name:v.name, description:v.description || null,
                                job_id: v.job_id || null, visibility:v.visibility, color:"blue"});
      toast("Save candidates to it from the directory.", {title:v.name});
      loadPools();
    } catch(e) { toast(e.status === 409 ? "You already have a pool with that name. Pick another."
                            : (e.message || "The pool was not saved."),
            {title:"Could not save pool", kind:"err"}); }
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
        toast(e.message || "That did not work.", {title:"Something went wrong", kind:"err"});
      }
    });
    menu.querySelector("[data-new-pool]").onclick = async () => {
      closePoolMenu();
      const v = await formDialog({
        title: "New talent pool",
        submit: "Create and add",
        fields: [
          {name:"name", label:"Pool name", required:true, wide:true},
          {name:"visibility", label:"Who can work it", type:"select",
           options:[["private","Only me"],["team","Everyone at my agency"]]},
        ],
      });
      if (!v) return;
      try {
        const pool = await post("/api/pools", {name:v.name, visibility:v.visibility,
                                               color:"blue"});
        S.pools.unshift(pool);
        S.poolMembership.set(profileId, [...(S.poolMembership.get(profileId) || []), pool.pool_id]);
        paintPoolButtons();
        await post(`/api/pools/${pool.pool_id}/members`, {profile_id: profileId});
        pool.member_count = (pool.member_count || 0) + 1;   // keep the card count honest
      } catch(e) { toast(e.status === 409 ? "You already have a pool with that name. Pick another."
                            : (e.message || "The pool was not saved."),
            {title:"Could not save pool", kind:"err"}); }
    };
  }

  // Real figures on the signed-out page. Fails quietly: a marketing number is
  // never worth blocking the login form for.
  async function loadPublicStats(){
    const fmt = n => n >= 1000 ? (n / 1000).toFixed(n >= 10000 ? 0 : 1) + "k" : String(n);
    try {
      const res = await fetch("/api/public/stats", {cache:"no-store"});
      if (!res.ok) return;
      const d = await res.json();
      const set = (id, v) => { const el = $(id); if (el && v != null) el.textContent = fmt(v); };
      set("#stat-providers", d.providers);
      set("#stat-jobs", d.jobs);
      set("#stat-states", d.states);
    } catch(e) { /* leave the placeholders */ }
  }

  // Everything that runs once a verified user is in: restore their page and
  // prime the badges/searches. Shared by first load and the verify gate.
  function enterAppPages(){
    const requested = new URLSearchParams(location.search).get("page");
    const saved = localStorage.getItem("hb_page");
    const initial = requested && document.getElementById("page-" + requested)
      ? requested : (saved && document.getElementById("page-" + saved) ? saved : "dashboard");
    showPage(initial);
    handlePurchaseReturn();
    loadJobs();
    refreshUnreadBadge();
    refreshNotificationBadge();
    refreshCredits();
    if (isRecruiter()){
      loadProviderFacets();
      // Standing searches are re-counted on entry; anything that grew since
      // the last visit turns into a notification.
      loadSavedSearches().then(checkSavedSearches);
    } else {
      // Professionals' job alerts fire the same way, on load.
      checkSavedSearches();
    }
  }

  async function startApp(){
    // Stash a team-invite token from the URL so it survives the sign-in flow.
    try {
      const it = new URLSearchParams(location.search).get("invite");
      if (it) sessionStorage.setItem("hb_invite", it);
    } catch(_){}
    // If we were signed out because the account was used elsewhere, say so once.
    try {
      if (localStorage.getItem("hb_signout_reason") === "superseded"){
        localStorage.removeItem("hb_signout_reason");
        setTimeout(() => toast(
          "You were signed out because this account was signed in somewhere else. "
          + "Only one active session is allowed per account.",
          {title:"Signed out", kind:"err", ms:9000}), 400);
      }
    } catch(_){}
    // A splash covers the screen while we validate an existing token, so a
    // logged-in user never sees the login form flash on refresh.
    if (token()) {
      const ok = await loadMe();
      if (ok === "pending") return;   // verify gate is showing
      if (ok) {
        $("#boot-splash").classList.add("hidden");
        enterAppPages();
        acceptStashedInvite();
        return;
      }
    }
    // No token (or it was rejected): show the public home page, not a raw login.
    $("#boot-splash").classList.add("hidden");
    let invited = false;
    try { invited = !!sessionStorage.getItem("hb_invite"); } catch(_){}
    if (invited){
      showAuth("signup");
      toast("Create an account or sign in to accept your team invitation.",
            {kind:"info", ms:7000});
    } else {
      showLanding();
    }
    loadPublicStats();
  }
  async function acceptStashedInvite(){
    let tok = null;
    try { tok = sessionStorage.getItem("hb_invite"); } catch(_){}
    if (!tok) return;
    try { sessionStorage.removeItem("hb_invite"); } catch(_){}
    try { history.replaceState(null, "", location.pathname); } catch(_){}
    try {
      const r = await post("/api/employers/invites/accept", {token: tok});
      toast(r.already ? `You're already part of ${esc(r.org_name)}.`
                      : `You've joined ${esc(r.org_name)}.`,
            {title:"Team joined", ms:6000});
      if (typeof refreshUser === "function") refreshUser();
    } catch(e){
      toast(e.message || "This invitation link is no longer valid.",
            {title:"Invitation", kind:"err", ms:6000});
    }
  }

  // Public views: the marketing home and the sign-in screen it leads to.
  function showLanding(){
    $("#auth-gate").classList.add("hidden");
    $("#landing").classList.remove("hidden");
    window.scrollTo(0, 0);
  }
  function showAuth(mode){
    $("#landing").classList.add("hidden");
    $("#auth-gate").classList.remove("hidden");
    showAuthMode(mode === "signup" ? "signup" : "login");
    window.scrollTo(0, 0);
    setTimeout(() => { const el = $("#auth-email"); if (el) el.focus(); }, 60);
  }
  wire();
  startApp();
})();
