/* HealthBoard shared frontend API client.
   Served at /static/hb-api.js ; pages are served same-origin at /ui/<page>.html
   so relative API calls (base = "") work. Handles JWT storage + a login modal. */
(function () {
  const HB = {
    base: "",

    token() { return localStorage.getItem("hb_token") || ""; },
    setToken(t) { t ? localStorage.setItem("hb_token", t) : localStorage.removeItem("hb_token"); },
    setRefresh(t) { t ? localStorage.setItem("hb_refresh", t) : localStorage.removeItem("hb_refresh"); },
    refresh() { return localStorage.getItem("hb_refresh") || ""; },
    isAuthed() { return !!this.token(); },
    async validateAuth() {
      if (!this.token()) return false;
      try {
        await this.get("/api/auth/me");
        return true;
      } catch (e) {
        if (e.status === 401 || e.status === 403) this.logout();
        return false;
      }
    },

    async api(method, path, body) {
      const headers = {};
      if (body !== undefined) headers["Content-Type"] = "application/json";
      const tok = this.token();
      if (tok) headers["Authorization"] = "Bearer " + tok;
      const res = await fetch(this.base + path, {
        method,
        headers,
        body: body !== undefined ? JSON.stringify(body) : undefined,
      });
      const text = await res.text();
      let data = null;
      try { data = text ? JSON.parse(text) : null; } catch (e) { data = text; }
      if (!res.ok) {
        const detail = data && data.detail ? data.detail : res.statusText;
        const err = new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
        err.status = res.status; err.data = data;
        throw err;
      }
      return data;
    },
    get(p) { return this.api("GET", p); },
    post(p, b) { return this.api("POST", p, b === undefined ? {} : b); },
    patch(p, b) { return this.api("PATCH", p, b === undefined ? {} : b); },
    del(p) { return this.api("DELETE", p); },

    async login(email, password, mfa) {
      const r = await this.post("/api/auth/login", { email, password, mfa_code: mfa || null });
      this.setToken(r.access_token); this.setRefresh(r.refresh_token);
      return r;
    },
    async register(payload) {
      const r = await this.post("/api/auth/register", payload);
      this.setToken(r.access_token); this.setRefresh(r.refresh_token);
      return r;
    },
    logout() { this.setToken(""); this.setRefresh(""); },

    initials(f, l) { return (((f || " ")[0]) + ((l || " ")[0])).toUpperCase(); },
    esc(s) {
      return (s == null ? "" : String(s)).replace(/[&<>"']/g, (c) => (
        { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
      ));
    },

    // --- Minimal login modal (used by pages needing auth) ---
    async ensureAuth() {
      if (await this.validateAuth()) return true;
      return new Promise((resolve) => this._showLogin(resolve));
    },
    _showLogin(resolve) {
      if (document.getElementById("hb-auth-ov")) return;
      const teal = "#1261D8";
      const inp = "width:100%;padding:11px 12px;margin-bottom:9px;border:1px solid #D5DCE8;border-radius:9px;box-sizing:border-box;font-size:14px;font-family:inherit;color:#1A202C";
      const ov = document.createElement("div");
      ov.id = "hb-auth-ov";
      ov.style.cssText = "position:fixed;inset:0;background:rgba(8,15,30,.72);z-index:99999;display:flex;align-items:center;justify-content:center;font-family:'DM Sans',system-ui,sans-serif;padding:16px";
      ov.innerHTML = `
        <div style="background:#fff;color:#1A202C;width:400px;max-width:100%;border-radius:16px;padding:26px;box-shadow:0 24px 70px rgba(0,0,0,.45);position:relative">
          <div id="hb-au-close" style="position:absolute;top:16px;right:18px;color:#8A97AB;cursor:pointer;font-size:18px;line-height:1">&times;</div>
          <div style="display:flex;align-items:center;gap:9px;margin-bottom:4px">
            <div style="width:32px;height:32px;background:${teal};border-radius:9px;display:flex;align-items:center;justify-content:center;color:#fff;font-weight:800">H</div>
            <div style="font-size:18px;font-weight:700">HealthBoard</div>
          </div>
          <div id="hb-au-title" style="font-size:21px;font-weight:700;margin:14px 0 2px">Welcome back</div>
          <div id="hb-au-sub" style="font-size:13px;color:#64748b;margin-bottom:16px">Sign in to your account</div>
          <div id="hb-au-signup" style="display:none">
            <div style="font-size:12.5px;font-weight:600;color:#4A5568;margin-bottom:6px">I am a…</div>
            <div style="display:flex;gap:8px;margin-bottom:11px">
              <label style="flex:1;border:1px solid #D5DCE8;border-radius:10px;padding:10px;text-align:center;cursor:pointer;font-size:13px"><input type="radio" name="hb-role" value="job_seeker" checked style="margin-right:5px">Healthcare Pro</label>
              <label style="flex:1;border:1px solid #D5DCE8;border-radius:10px;padding:10px;text-align:center;cursor:pointer;font-size:13px"><input type="radio" name="hb-role" value="recruiter" style="margin-right:5px">Recruiter</label>
            </div>
            <div style="display:flex;gap:8px"><input id="hb-au-first" placeholder="First name" style="${inp}"><input id="hb-au-last" placeholder="Last name" style="${inp}"></div>
          </div>
          <input id="hb-au-email" placeholder="Email" type="email" autocomplete="email" style="${inp}">
          <input id="hb-au-pass" placeholder="Password" type="password" style="${inp}">
          <div id="hb-au-err" style="color:#dc2626;font-size:12.5px;min-height:16px;margin:2px 0 8px"></div>
          <button id="hb-au-go" style="width:100%;padding:12px;background:${teal};color:#fff;border:none;border-radius:10px;font-weight:700;font-size:14px;cursor:pointer">Sign in</button>
          <div style="text-align:center;font-size:13px;color:#64748b;margin-top:13px">
            <span id="hb-au-switch-text">New to HealthBoard?</span>
            <a id="hb-au-switch" href="#" style="color:${teal};font-weight:600;text-decoration:none">Create an account</a>
          </div>
        </div>`;
      document.body.appendChild(ov);
      const $ = (id) => ov.querySelector("#" + id);
      let mode = "login";
      const setMode = (m) => {
        mode = m;
        $("hb-au-signup").style.display = m === "signup" ? "block" : "none";
        $("hb-au-title").textContent = m === "signup" ? "Create your account" : "Welcome back";
        $("hb-au-sub").textContent = m === "signup" ? "Join HealthBoard in seconds" : "Sign in to your account";
        $("hb-au-go").textContent = m === "signup" ? "Create account" : "Sign in";
        $("hb-au-switch-text").textContent = m === "signup" ? "Already have an account?" : "New to HealthBoard?";
        $("hb-au-switch").textContent = m === "signup" ? "Sign in" : "Create an account";
        $("hb-au-err").textContent = "";
      };
      $("hb-au-switch").onclick = (e) => { e.preventDefault(); setMode(mode === "login" ? "signup" : "login"); };
      $("hb-au-close").onclick = () => ov.remove();
      const submit = async () => {
        const err = $("hb-au-err"); err.textContent = "";
        const email = $("hb-au-email").value.trim();
        const pass = $("hb-au-pass").value;
        if (!email || !pass) { err.textContent = "Email and password are required."; return; }
        try {
          if (mode === "signup") {
            if (pass.length < 8) { err.textContent = "Password must be at least 8 characters."; return; }
            const role = ov.querySelector('input[name="hb-role"]:checked').value;
            await HB.register({ email, password: pass, role,
              first_name: $("hb-au-first").value.trim(), last_name: $("hb-au-last").value.trim() });
          } else {
            await HB.login(email, pass);
          }
          ov.remove();
          resolve(true);
        } catch (e) {
          err.textContent = e.message || (mode === "signup" ? "Could not create account." : "Sign in failed.");
        }
      };
      $("hb-au-go").onclick = submit;
      ov.addEventListener("keydown", (e) => { if (e.key === "Enter") submit(); });
      $("hb-au-email").focus();
    },

    // --- Full-screen auth gate: blocks the whole board until signed in ---
    async gate() {
      if (await this.validateAuth()) return true;
      return new Promise((resolve) => this._showGate(resolve));
    },
    _showGate(resolve) {
      if (document.getElementById("hb-gate")) return;
      const teal = "#1261D8", dark = "#082452";
      const inp = "width:100%;padding:12px 13px;margin-bottom:10px;border:1px solid #D5DCE8;border-radius:10px;box-sizing:border-box;font-size:14px;font-family:inherit;color:#1A202C;background:#fff;outline:none";
      const st = document.createElement("style");
      st.textContent = "@media(max-width:820px){#hb-gate .hb-gate-brand{display:none!important}}"
        + "#hb-gate input:focus{border-color:" + teal + "!important}"
        + "#hb-gate .hb-tab{transition:all .15s}";
      document.head.appendChild(st);
      const wrap = document.createElement("div");
      wrap.id = "hb-gate";
      wrap.style.cssText = "position:fixed;inset:0;z-index:100000;display:flex;align-items:stretch;font-family:'DM Sans',system-ui,sans-serif;background:#0b1220";
      wrap.innerHTML = `
        <div class="hb-gate-brand" style="flex:1;background:linear-gradient(150deg,${teal},${dark});color:#fff;padding:56px 48px;display:flex;flex-direction:column;justify-content:center;gap:20px">
          <div style="display:flex;align-items:center;gap:11px">
            <div style="width:44px;height:44px;background:#fff;color:${teal};border-radius:12px;display:flex;align-items:center;justify-content:center;font-weight:800;font-size:22px">H</div>
            <div style="font-size:24px;font-weight:800">HealthBoard</div>
          </div>
          <div style="font-size:33px;font-weight:800;line-height:1.15;max-width:430px">The staffing network for healthcare professionals</div>
          <div style="font-size:15px;opacity:.85;max-width:430px;line-height:1.6">Browse live jobs, connect with recruiters, and manage your credentials — all in one place. Sign in or create an account to continue.</div>
        </div>
        <div style="width:466px;max-width:100%;background:#fff;color:#1A202C;padding:48px 44px;display:flex;flex-direction:column;justify-content:center;overflow:auto">
          <div style="display:flex;background:#F1F5F9;border-radius:11px;padding:4px;margin-bottom:22px">
            <button id="hb-tab-login" class="hb-tab" style="flex:1;padding:10px;border:none;border-radius:8px;font-weight:700;font-size:14px;cursor:pointer;font-family:inherit;background:#fff;color:#1A202C;box-shadow:0 1px 3px rgba(0,0,0,.08)">Sign in</button>
            <button id="hb-tab-signup" class="hb-tab" style="flex:1;padding:10px;border:none;border-radius:8px;font-weight:700;font-size:14px;cursor:pointer;font-family:inherit;background:transparent;color:#64748b">Create account</button>
          </div>
          <div id="hb-g-title" style="font-size:23px;font-weight:800;margin-bottom:4px">Welcome back</div>
          <div id="hb-g-sub" style="font-size:13.5px;color:#64748b;margin-bottom:20px">Sign in to access the board</div>
          <div id="hb-g-signup" style="display:none">
            <div style="font-size:12.5px;font-weight:700;color:#4A5568;margin-bottom:7px">I am a…</div>
            <div style="display:flex;gap:9px;margin-bottom:12px">
              <label style="flex:1;border:1.5px solid #D5DCE8;border-radius:11px;padding:11px;text-align:center;cursor:pointer;font-size:13px;font-weight:600"><input type="radio" name="hb-grole" value="job_seeker" checked style="margin-right:6px">Healthcare Pro</label>
              <label style="flex:1;border:1.5px solid #D5DCE8;border-radius:11px;padding:11px;text-align:center;cursor:pointer;font-size:13px;font-weight:600"><input type="radio" name="hb-grole" value="recruiter" style="margin-right:6px">Recruiter</label>
            </div>
            <div style="display:flex;gap:9px"><input id="hb-g-first" placeholder="First name" style="${inp}"><input id="hb-g-last" placeholder="Last name" style="${inp}"></div>
          </div>
          <input id="hb-g-email" placeholder="Email address" type="email" autocomplete="email" style="${inp}">
          <input id="hb-g-pass" placeholder="Password" type="password" autocomplete="current-password" style="${inp}">
          <div id="hb-g-err" style="color:#dc2626;font-size:12.5px;min-height:17px;margin:3px 0 9px"></div>
          <button id="hb-g-go" style="width:100%;padding:13px;background:${teal};color:#fff;border:none;border-radius:11px;font-weight:700;font-size:15px;cursor:pointer;font-family:inherit">Sign in</button>
          <div style="text-align:center;font-size:12px;color:#94a3b8;margin-top:16px">Protected board · your data stays private</div>
        </div>`;
      document.body.appendChild(wrap);
      const $ = (id) => wrap.querySelector("#" + id);
      let mode = "login";
      const setMode = (m) => {
        mode = m;
        const on = "background:#fff;color:#1A202C;box-shadow:0 1px 3px rgba(0,0,0,.08)";
        const off = "background:transparent;color:#64748b;box-shadow:none";
        $("hb-tab-login").style.cssText += ";" + (m === "login" ? on : off);
        $("hb-tab-signup").style.cssText += ";" + (m === "signup" ? on : off);
        $("hb-g-signup").style.display = m === "signup" ? "block" : "none";
        $("hb-g-title").textContent = m === "signup" ? "Create your account" : "Welcome back";
        $("hb-g-sub").textContent = m === "signup" ? "Join HealthBoard in seconds" : "Sign in to access the board";
        $("hb-g-go").textContent = m === "signup" ? "Create account" : "Sign in";
        $("hb-g-pass").setAttribute("autocomplete", m === "signup" ? "new-password" : "current-password");
        $("hb-g-err").textContent = "";
      };
      $("hb-tab-login").onclick = () => setMode("login");
      $("hb-tab-signup").onclick = () => setMode("signup");
      const submit = async () => {
        const err = $("hb-g-err"); err.textContent = "";
        const email = $("hb-g-email").value.trim();
        const pass = $("hb-g-pass").value;
        if (!email || !pass) { err.textContent = "Email and password are required."; return; }
        const btn = $("hb-g-go"); const label = btn.textContent;
        btn.disabled = true; btn.textContent = "Please wait…";
        try {
          if (mode === "signup") {
            if (pass.length < 8) throw new Error("Password must be at least 8 characters.");
            const role = wrap.querySelector('input[name="hb-grole"]:checked').value;
            await HB.register({ email, password: pass, role,
              first_name: $("hb-g-first").value.trim(), last_name: $("hb-g-last").value.trim() });
          } else {
            await HB.login(email, pass);
          }
          st.remove(); wrap.remove();
          resolve(true);
        } catch (e) {
          btn.disabled = false; btn.textContent = label;
          err.textContent = e.message || (mode === "signup" ? "Could not create account." : "Sign in failed.");
        }
      };
      $("hb-g-go").onclick = submit;
      wrap.addEventListener("keydown", (e) => { if (e.key === "Enter") submit(); });
      $("hb-g-email").focus();
    },
  };
  window.HB = HB;
})();
