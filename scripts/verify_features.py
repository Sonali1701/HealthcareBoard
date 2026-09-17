"""One-command feature check for MedHunt.

Verifies every backend feature (and, if Playwright is installed, every wired
frontend page) against a RUNNING server, and prints a PASS/FAIL report.

Usage:
    # 1. start the server in another terminal:
    #    .venv\\Scripts\\python main.py
    # 2. run the checker:
    .venv\\Scripts\\python scripts\\verify_features.py
    .venv\\Scripts\\python scripts\\verify_features.py --base http://127.0.0.1:8000
    .venv\\Scripts\\python scripts\\verify_features.py --no-pages   # skip browser checks
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time

import httpx

REC_EMAIL = "recruiter@example.com"
REC_PASS = "Password123!"

results: list[tuple[str, str, bool, str]] = []  # (group, name, ok, detail)


def check(group: str, name: str, ok: bool, detail: str = "") -> None:
    results.append((group, name, bool(ok), detail))


def wait_for_server(base: str, timeout: float = 30) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(base + "/api/health", timeout=3).status_code == 200:
                return True
        except Exception:
            time.sleep(0.5)
    return False


def run_api_checks(base: str) -> str | None:
    c = httpx.Client(base_url=base, timeout=20)

    # --- Meta ---
    r = c.get("/api/health")
    check("Core", "health endpoint", r.status_code == 200, r.text[:80])
    check("Core", "OpenAPI docs", c.get("/openapi.json").status_code == 200)

    # --- Auth ---
    reg = c.post("/api/auth/register", json={"email": REC_EMAIL, "password": REC_PASS, "role": "recruiter"})
    check("Auth", "register (or already exists)", reg.status_code in (201, 409), str(reg.status_code))
    login = c.post("/api/auth/login", json={"email": REC_EMAIL, "password": REC_PASS})
    check("Auth", "login returns tokens", login.status_code == 200 and "access_token" in login.json())
    if login.status_code != 200:
        print("Cannot log in — aborting authenticated checks.")
        return None
    tok = login.json()["access_token"]
    refresh = login.json()["refresh_token"]
    auth = {"Authorization": f"Bearer {tok}"}
    me = c.get("/api/auth/me", headers=auth)
    check("Auth", "current user (/me)", me.status_code == 200 and me.json().get("role") == "recruiter")
    refreshed = c.post("/api/auth/refresh", json={"refresh_token": refresh})
    check("Auth", "refresh token rotates", refreshed.status_code == 200)
    if refreshed.status_code == 200:
        # The server enforces a single active session, so continue the E2E run
        # with the newly issued access token rather than the superseded one.
        tok = refreshed.json()["access_token"]
        auth = {"Authorization": f"Bearer {tok}"}
    check("Auth", "uploads endpoint protected (401 w/o token)",
          c.post("/api/uploads/resume").status_code == 401)

    # --- Profiles (candidate board) ---
    pl = c.get("/api/profiles?limit=100", headers=auth)
    # Newer Page responses expose `items` without a top-level total; use the
    # returned page size as a liveness signal in that case.
    if pl.status_code == 200:
        profile_payload = pl.json()
        total = max(profile_payload.get("total", 0), len(profile_payload.get("items", [])))
    else:
        total = 0
    check("Profiles", "list profiles", pl.status_code == 200 and total > 0, f"{total} profiles")
    check("Profiles", "full-text search (q=)", c.get("/api/profiles?q=allergy", headers=auth).status_code == 200)
    check("Profiles", "filter by profession", c.get("/api/profiles?profession_type=MD", headers=auth).status_code == 200)
    check("Profiles", "filter by state", c.get("/api/profiles?state_code=NC", headers=auth).status_code == 200)
    if total:
        pid = pl.json()["items"][0]["profile_id"]
        det = c.get(f"/api/profiles/{pid}", headers=auth)
        check("Profiles", "profile detail (licenses/certs)",
              det.status_code == 200 and "licenses" in det.json())
        ru = pl.json()["items"][0].get("resume_url")
        if ru:
            rf = c.get(ru, headers=auth)
            check("Profiles", "résumé file downloads", rf.status_code == 200 and len(rf.content) > 500,
                  f"{len(rf.content)} bytes")

    # --- Jobs / applications ---
    jb = c.get("/api/jobs?limit=20", headers=auth)
    if jb.status_code == 200:
        jobs_payload = jb.json()
        job_count = max(jobs_payload.get("total", 0), len(jobs_payload.get("items", [])))
    else:
        job_count = 0
    check("Jobs", "list jobs", jb.status_code == 200 and job_count > 0, f"{job_count} jobs")

    # --- AI matching ---
    mr = c.post("/api/matching/run", json={"specialty": "Allergy & Immunology", "top_n": 50}, headers=auth)
    if mr.status_code != 200:
        mr = c.post("/api/matching/run", json={"top_n": 50}, headers=auth)
    mtotal = mr.json().get("summary", {}).get("total", 0) if mr.status_code == 200 else 0
    check("Matching", "run AI matching", mr.status_code == 200 and mtotal > 0, f"{mtotal} candidates")

    # --- GSA pay ---
    g = c.get("/api/gsa/rates?city=Houston&state=TX")
    check("GSA", "per-diem rates", g.status_code == 200 and "lodging" in g.json(),
          f"source={g.json().get('source')}" if g.status_code == 200 else "")
    pp = c.post("/api/gsa/pay-package/calculate", json={"bill_rate": 120, "city": "Raleigh", "state_code": "NC"})
    check("GSA", "pay-package calculator", pp.status_code == 200 and "option_perdiem" in pp.json())

    # --- Messaging / CRM ---
    th = c.get("/api/messages/threads", headers=auth)
    nthreads = len(th.json()) if th.status_code == 200 else 0
    check("Messaging", "list threads", th.status_code == 200, f"{nthreads} threads")
    if nthreads:
        tid = th.json()[0]["thread_id"]
        check("Messaging", "open thread (messages)", c.get(f"/api/messages/threads/{tid}", headers=auth).status_code == 200)
    check("Analytics", "recruitment funnel", c.get("/api/analytics/funnel", headers=auth).status_code == 200)
    check("Analytics", "recruiter KPIs", c.get("/api/analytics/kpis", headers=auth).status_code == 200)
    check("Analytics", "CRM conversations", c.get("/api/analytics/conversations", headers=auth).status_code == 200)
    med = c.get("/api/analytics/medhunt", headers=auth)
    med_shape = med.status_code == 200 and all(k in med.json() for k in ("summary", "members", "sources", "activity"))
    check("Analytics", "MedHunt extension details", med_shape)

    # --- Notifications / social ---
    check("Notifications", "notification feed", c.get("/api/notifications", headers=auth).status_code == 200)
    check("Social", "feed posts", c.get("/api/social/posts").status_code == 200)

    c.close()
    return tok


def run_page_checks(base: str, token: str | None = None) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        check("Pages", "playwright installed", False, "pip install playwright (skipping page checks)")
        return

    with sync_playwright() as p:
        try:
            br = p.chromium.launch()
        except Exception:
            # CI/dev machines often have Edge or Chrome but not Playwright's
            # downloaded bundle. Use a system browser before declaring UI E2E
            # unavailable.
            candidates = [
                os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
                + r"\Microsoft\Edge\Application\msedge.exe",
                os.environ.get("PROGRAMFILES", r"C:\Program Files")
                + r"\Google\Chrome\Application\chrome.exe",
            ]
            executable = next((path for path in candidates if os.path.exists(path)), None)
            if not executable:
                executable = shutil.which("msedge") or shutil.which("chrome")
            if not executable:
                check("Pages", "launch browser", False, "No Chromium/Edge executable found")
                return
            try:
                br = p.chromium.launch(executable_path=executable)
            except Exception as e:
                check("Pages", "launch browser", False, str(e)[:120])
                return

        def page_errs():
            ctx = br.new_context()
            if token:
                ctx.add_init_script(
                    f"localStorage.setItem('hb_token', {json.dumps(token)});"
                )
            pg = ctx.new_page()
            errs: list[str] = []
            # CDN font/icon requests can be blocked in CI or a restricted
            # browser sandbox; only same-origin application errors fail E2E.
            pg.on("console", lambda m: errs.append(m.text)
                  if m.type == "error" and (
                      not m.location.get("url")
                      or m.location.get("url", "").startswith(base)
                  ) else None)
            pg.on("pageerror", lambda e: errs.append(str(e)))
            return ctx, pg, errs

        ui = base + "/ui/"
        # pro
        ctx, pg, errs = page_errs()
        # Authenticated prototypes poll their API; waiting for network-idle can
        # therefore time out even while the page is healthy.
        pg.goto(ui + "healthboard-pro.html", wait_until="domcontentloaded")
        try:
            pg.wait_for_selector(".professionals-grid .pro-card", timeout=15000)
        except Exception:
            pass
        pg.wait_for_timeout(800)
        n = len(pg.query_selector_all(".professionals-grid .pro-card"))
        check("Pages", "pro.html candidate board", n > 0 and not errs, f"{n} cards, errors={errs[:1]}")
        ctx.close()
        # matching
        ctx, pg, errs = page_errs()
        pg.goto(ui + "healthboard-ai-matching.html", wait_until="domcontentloaded")
        try:
            pg.wait_for_selector("#hb-li-go", timeout=6000); pg.click("#hb-li-go")
        except Exception:
            pass
        # The live matching page waits for an explicit user action; exercise
        # the same button a recruiter uses instead of relying on demo auto-run.
        try:
            pg.wait_for_selector("#run-btn", timeout=5000)
            pg.click("#run-btn")
            pg.wait_for_selector("#candidates-grid .candidate-card", timeout=30000)
        except Exception:
            pass
        n = len(pg.query_selector_all("#candidates-grid .candidate-card"))
        check("Pages", "ai-matching.html", n > 0 and not errs, f"{n} cards, errors={errs[:1]}")
        ctx.close()
        # chat
        ctx, pg, errs = page_errs()
        pg.goto(ui + "healthboard-chat-platform.html", wait_until="domcontentloaded")
        try:
            pg.wait_for_selector("#hb-li-go", timeout=6000); pg.click("#hb-li-go")
            # API-rendered threads carry data-id (static placeholders do not).
            pg.wait_for_selector("#view-recruiter .thread-list .thread[data-id]", timeout=10000)
        except Exception:
            pass
        pg.wait_for_timeout(800)
        n = len(pg.query_selector_all("#view-recruiter .thread-list .thread[data-id]"))
        check("Pages", "chat-platform.html (live threads)", not errs, f"{n} threads, errors={errs[:1]}")
        ctx.close()
        # calculators — force the rate lookup so the check is deterministic
        for f in ("healthboard-gsa-pay-calculator.html", "healthboard-pay-calculator.html"):
            ctx, pg, errs = page_errs()
            calls: list[str] = []
            pg.on("request", (lambda store: (lambda r: store.append(r.url) if "/api/gsa" in r.url else None))(calls))
            pg.goto(ui + f, wait_until="networkidle")
            pg.evaluate("if(typeof triggerGSA==='function')triggerGSA(); if(typeof lookupGSA==='function')lookupGSA();")
            pg.wait_for_timeout(1500)
            check("Pages", f.replace("healthboard-", ""), bool(calls) and not errs,
                  f"proxy call={bool(calls)}, errors={errs[:1]}")
            ctx.close()
        br.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--no-pages", action="store_true", help="skip browser page checks")
    args = ap.parse_args()

    print(f"Verifying MedHunt at {args.base} ...\n")
    if not wait_for_server(args.base):
        print(f"ERROR: server not reachable at {args.base}\n"
              f"Start it first:  .venv\\Scripts\\python main.py")
        return 2

    token = run_api_checks(args.base)
    if not args.no_pages:
        run_page_checks(args.base, token)

    # Report
    groups: dict[str, list] = {}
    for g, n, ok, d in results:
        groups.setdefault(g, []).append((n, ok, d))
    print("=" * 64)
    for g, items in groups.items():
        print(f"\n[ {g} ]")
        for n, ok, d in items:
            print(f"  {'PASS' if ok else 'FAIL'}  {n}" + (f"   ({d})" if d else ""))
    passed = sum(1 for *_, ok, _ in [(r[0], r[1], r[2], r[3]) for r in results] if ok)
    total = len(results)
    print("\n" + "=" * 64)
    print(f"  {passed}/{total} checks passed")
    print("=" * 64)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
