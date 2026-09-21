"""Release-level browser checks for the real MedHunt UI.

Run against an isolated/local server before deployment::

    python scripts/e2e_release_check.py --base http://127.0.0.1:8000

The check exercises both roles, verifies that authentication survives a hard
reload, opens every visible workspace page, and fails on same-origin browser or
request errors. It intentionally targets ``/`` rather than the retired
prototype pages under ``/ui``.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time

import httpx
from playwright.sync_api import Browser, Page, sync_playwright


def browser_executable() -> str | None:
    candidates = [
        os.path.join(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
                     r"Microsoft\Edge\Application\msedge.exe"),
        os.path.join(os.environ.get("PROGRAMFILES", r"C:\Program Files"),
                     r"Google\Chrome\Application\chrome.exe"),
    ]
    return next((p for p in candidates if os.path.exists(p)), None) or \
        shutil.which("msedge") or shutil.which("chrome")


class Check:
    def __init__(self) -> None:
        self.results: list[tuple[bool, str, str]] = []

    def add(self, ok: bool, name: str, detail: str = "") -> None:
        self.results.append((bool(ok), name, detail))
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))

    def finish(self) -> int:
        passed = sum(ok for ok, _, _ in self.results)
        print(f"\n{passed}/{len(self.results)} checks passed")
        return 0 if passed == len(self.results) else 1


def login_api(base: str, email: str, password: str) -> dict:
    response = httpx.post(
        base + "/api/auth/login",
        json={"email": email, "password": password, "mfa_code": None},
        timeout=20,
    )
    response.raise_for_status()
    return response.json()


def page_errors(page: Page, base: str) -> list[str]:
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(f"page: {exc}"))
    page.on(
        "console",
        lambda msg: errors.append(f"console: {msg.text}")
        if msg.type == "error" and (
            not msg.location.get("url") or msg.location.get("url", "").startswith(base)
        ) else None,
    )
    page.on(
        "requestfailed",
        lambda req: errors.append(f"request: {req.method} {req.url} {req.failure}")
        if req.url.startswith(base) and "ERR_ABORTED" not in (req.failure or "") else None,
    )
    page.on(
        "response",
        lambda response: errors.append(
            f"response: {response.status} {response.request.method} {response.url}"
        ) if response.url.startswith(base) and response.status >= 500 else None,
    )
    return errors


def authenticate_context(browser: Browser, base: str, email: str, password: str,
                         viewport: dict[str, int] | None = None):
    pair = login_api(base, email, password)
    context = browser.new_context(viewport=viewport or {"width": 1366, "height": 900})
    context.add_init_script(
        "if (!sessionStorage.getItem('e2e_auth_bootstrapped')) {"
        "localStorage.setItem('hb_token', %s);"
        "localStorage.setItem('hb_refresh', %s);"
        "sessionStorage.setItem('e2e_auth_bootstrapped', '1');"
        "}"
        % (json.dumps(pair["access_token"]), json.dumps(pair["refresh_token"]))
    )
    return context


def exercise_role(check: Check, browser: Browser, base: str, role: str,
                  email: str, password: str, *, ui_login: bool = False,
                  viewport: dict[str, int] | None = None) -> None:
    size = viewport or {"width": 1366, "height": 900}
    context = (browser.new_context(viewport=size)
               if ui_login else authenticate_context(
                   browser, base, email, password, viewport=size
               ))
    page = context.new_page()
    errors = page_errors(page, base)
    page.goto(base + "/", wait_until="domcontentloaded")
    if ui_login:
        page.locator("#landing:not(.hidden)").wait_for(timeout=20_000)
        page.locator('[data-auth-open="login"]').first.click()
        page.locator("#auth-email").fill(email)
        page.locator("#auth-password").fill(password)
        page.locator("#auth-submit").click()
    page.locator("#app-shell:not(.hidden)").wait_for(timeout=20_000)
    check.add(True, f"{role}: authenticated workspace opens",
              "real login form" if ui_login else "token restoration")

    before = page.evaluate("localStorage.getItem('hb_refresh')")
    page.reload(wait_until="domcontentloaded")
    page.locator("#app-shell:not(.hidden)").wait_for(timeout=20_000)
    after = page.evaluate("localStorage.getItem('hb_refresh')")
    check.add(bool(before and after), f"{role}: session survives hard refresh")

    nav_pages = page.locator(".nav-item:not(.hidden)[data-page]").evaluate_all(
        "els => [...new Set(els.map(e => e.dataset.page))]"
    )
    opened: list[str] = []
    overflow_pages: list[str] = []
    for name in nav_pages:
        item = page.locator(f'.nav-item:not(.hidden)[data-page="{name}"]').first
        item.click()
        page.locator(f"#page-{name}.active").wait_for(timeout=10_000)
        page.wait_for_timeout(120)
        if page.locator("body").evaluate("el => el.scrollWidth > innerWidth + 1"):
            overflow_pages.append(name)
        opened.append(name)
    check.add(len(opened) == len(nav_pages), f"{role}: every visible workspace page opens",
              ", ".join(opened))
    check.add(not errors, f"{role}: no same-origin browser errors",
              " | ".join(errors[:3]))
    if size["width"] <= 820:
        check.add(not overflow_pages, f"{role}: mobile workspace has no page overflow",
                  ", ".join(overflow_pages))

    refresh = page.evaluate("localStorage.getItem('hb_refresh')")
    page.locator("#logout-btn").click()
    page.locator("#landing:not(.hidden)").wait_for(timeout=20_000)
    storage_cleared = page.evaluate(
        "!localStorage.getItem('hb_token') && !localStorage.getItem('hb_refresh')"
    )
    rejected = httpx.post(
        base + "/api/auth/refresh", json={"refresh_token": refresh}, timeout=20
    ).status_code == 401
    check.add(storage_cleared and rejected, f"{role}: logout revokes and clears session")
    context.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--recruiter-email", default="recruiter@healthboard.dev")
    parser.add_argument("--seeker-email", default="jessica@healthboard.dev")
    parser.add_argument("--password", default="Password123!")
    args = parser.parse_args()
    base = args.base.rstrip("/")
    check = Check()

    started = time.perf_counter()
    health = httpx.get(base + "/api/health", timeout=10)
    check.add(health.status_code == 200, "API health and database readiness",
              f"{(time.perf_counter() - started) * 1000:.0f} ms")
    check.add("no-store" in health.headers.get("cache-control", ""),
              "dynamic API responses are not cached")
    asset = httpx.get(base + "/assets/css/launch-board.css?v=release-check", timeout=10)
    policy = asset.headers.get("cache-control", "")
    check.add(asset.status_code == 200 and "immutable" in policy,
              "versioned assets use long-lived browser caching", policy)
    font = httpx.get(
        base + "/assets/vendor/fontawesome/webfonts/fa-solid-900.woff2", timeout=10
    )
    check.add(font.status_code == 200 and
              "immutable" in font.headers.get("cache-control", ""),
              "bundled icon font uses long-lived browser caching")

    executable = browser_executable()
    if not executable:
        check.add(False, "system Chromium/Edge available")
        return check.finish()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=executable, headless=True)
        public = browser.new_context(viewport={"width": 390, "height": 844})
        page = public.new_page()
        errors = page_errors(page, base)
        nav_started = time.perf_counter()
        page.goto(base + "/", wait_until="domcontentloaded")
        page.locator("#landing:not(.hidden)").wait_for(timeout=20_000)
        load_ms = (time.perf_counter() - nav_started) * 1000
        timing = page.evaluate("""() => {
          const nav = performance.getEntriesByType('navigation')[0];
          const resources = performance.getEntriesByType('resource')
            .sort((a,b) => b.duration-a.duration).slice(0,4)
            .map(r => ({name:r.name.split('/').pop(), ms:Math.round(r.duration),
                        bytes:r.transferSize || 0}));
          return {dom:Math.round(nav.domContentLoadedEventEnd), resources};
        }""")
        slow = ", ".join(f"{r['name']} {r['ms']}ms" for r in timing["resources"])
        check.add(True, "mobile public landing renders",
                  f"wall {load_ms:.0f} ms; DOM {timing['dom']} ms; slow: {slow}")
        check.add(timing["dom"] < 2_000, "public landing meets local DOM budget",
                  f"{timing['dom']} ms < 2000 ms")
        check.add(page.locator("body").evaluate("el => el.scrollWidth <= innerWidth + 1"),
                  "mobile landing has no horizontal overflow")
        check.add(not errors, "public page has no same-origin browser errors",
                  " | ".join(errors[:3]))
        public.close()

        exercise_role(check, browser, base, "recruiter", args.recruiter_email,
                      args.password, ui_login=True)
        exercise_role(check, browser, base, "job seeker", args.seeker_email,
                      args.password, viewport={"width": 390, "height": 844})
        browser.close()

    return check.finish()


if __name__ == "__main__":
    sys.exit(main())
