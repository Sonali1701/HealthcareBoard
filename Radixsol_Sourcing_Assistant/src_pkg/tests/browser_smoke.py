"""Browser smoke test for the unpacked extension.

Prerequisites:
- backend on 127.0.0.1:8090
- fixture server on 127.0.0.1:8091
- Chrome with the extension loaded and remote debugging enabled
"""
from __future__ import annotations

import asyncio
import html
import json
import os
import re
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright


async def main() -> None:
    cdp_url = os.getenv("RADIXSOL_CDP_URL", "http://127.0.0.1:9225")
    screenshot = Path(__file__).with_name("extension_smoke.png")
    result_screenshot = Path(__file__).with_name("extension_results_smoke.png")
    launch_browser = os.getenv("RADIXSOL_LAUNCH_BROWSER") == "1"
    backend_url = os.getenv("RADIXSOL_BACKEND_URL", "")
    provider_requests: list[str] = []
    browser_profile = os.getenv(
        "RADIXSOL_BROWSER_PROFILE",
        "C:/tmp/radixsol-playwright-extension",
    )

    async def mock_usphonebook(route) -> None:
        provider_requests.append(route.request.url)
        requested_url = urlparse(route.request.url)
        parts = [part for part in requested_url.path.split("/") if part]
        profile_page = len(parts) >= 4 and parts[0:2] == ["find", "person"]
        slug = (
            parts[2] if profile_page
            else parts[0] if parts
            else "unknown-person"
        ).lower()
        profile_id = parts[3].lower() if profile_page else ""
        name = " ".join(part.capitalize() for part in slug.split("-"))
        safe_name = html.escape(name)
        safe_email = html.escape(f"{slug}@example.test")
        if not profile_page:
            excluded_cards = "".join(
                f"""<div class="card-summary"
                         data-detail-link="/find/person/{html.escape(slug)}/excluded-{index}">
                      <div class="content-header">{safe_name}, Age {43 + index}</div>
                      <p>Lives in Jacksonville, FL</p>
                      <a class="ls_contacts-btn"
                         href="/find/person/{html.escape(slug)}/excluded-{index}?via=button">View Full Address &amp; Phone</a>
                    </div>"""
                for index in range(1, 7)
            )
            await route.fulfill(
                status=200,
                content_type="text/html",
                body=f"""<!doctype html>
                  <title>{safe_name} search results</title>
                  <main>
                    <h1>We've found 8 records for {safe_name}</h1>
                    <div class="card-summary"
                         data-detail-link="/find/person/{html.escape(slug)}/right">
                      <div class="content-header">{safe_name}, Age 40</div>
                      <p>Lives in Atlanta, GA</p>
                      <a class="ls_contacts-btn"
                         href="/find/person/{html.escape(slug)}/right?via=button">View Full Address &amp; Phone</a>
                    </div>
                    <div class="card-summary"
                         data-detail-link="/find/person/{html.escape(slug)}/wrong">
                      <div class="content-header">{safe_name}, Age 42</div>
                      <p>Lives in Atlanta, GA</p>
                      <a class="ls_contacts-btn"
                         href="/find/person/{html.escape(slug)}/wrong?via=button">View Full Address &amp; Phone</a>
                    </div>
                    {excluded_cards}
                  </main>""",
            )
            return
        right_profile = profile_id == "right"
        address_heading = "Current Address"
        address = (
            "100 Main St, Atlanta, GA 30303"
            if right_profile
            else "200 Ocean Dr, Jacksonville, FL 32202"
        )
        role = "Radiologic Technologist" if right_profile else "Registered Nurse"
        work_location = "Atlanta, GA" if right_profile else "Jacksonville, FL"
        phone = "(404) 555-0187" if right_profile else "(904) 555-0120"
        contact_html = (
            f"""<section><h2>Current Phone Number</h2>
                  <a href="tel:+14045550187">{phone}</a>
                </section>
                <section><h2>Email Addresses</h2>
                  <a href="mailto:{safe_email}">{safe_email}</a>
                </section>"""
            if "via=button" in requested_url.query
            else ""
        )
        await route.fulfill(
            status=200,
            content_type="text/html",
            body=f"""<!doctype html>
              <title>{safe_name}</title>
              <main>
                <nav>
                  <a role="button" href="/phone">Phone</a>
                  <a role="button" href="/name">Name</a>
                  <a role="button" href="/address">Address</a>
                </nav>
                <div id="personDetails" data-fn="{html.escape(name.split()[0])}"
                     data-ln="{html.escape(name.split()[-1])}"></div>
                <h1>{safe_name}</h1>
                <button type="button">Search Background Report</button>
                <section><h2>{address_heading}</h2>
                  <a href="/address/test">{address}</a>
                </section>
                {contact_html}
                <section><h2>Workplace</h2>
                  <div class="relative-card workplace">
                    <p>{role}</p><p>Regional Medical Center</p>
                    <p>{work_location}</p><p>Current</p>
                  </div>
                </section>
              </main>""",
        )

    async with async_playwright() as playwright:
        if launch_browser:
            extension = Path(__file__).parents[1] / "frontend"
            context = await playwright.chromium.launch_persistent_context(
                browser_profile,
                headless=False,
                args=[
                    f"--disable-extensions-except={extension}",
                    f"--load-extension={extension}",
                    "--host-resolver-rules=MAP indeed.com 127.0.0.1",
                    "--no-first-run",
                    "--no-default-browser-check",
                ],
            )
            await context.route(
                re.compile(r"https?://(?:www\.)?usphonebook\.com/.*"),
                mock_usphonebook,
            )
            browser = context.browser
            indeed_page = context.pages[0] if context.pages else await context.new_page()
            await indeed_page.goto("http://indeed.com:8091/indeed_results.html")
            worker = await context.wait_for_event(
                "serviceworker",
                predicate=lambda item: item.url.startswith("chrome-extension://"),
                timeout=10_000,
            ) if not context.service_workers else context.service_workers[0]
            targets = []
        else:
            browser = await playwright.chromium.connect_over_cdp(cdp_url)
            context = browser.contexts[0]
            indeed_page = next(page for page in context.pages if "indeed.com:8091" in page.url)
            with urllib.request.urlopen(f"{cdp_url}/json", timeout=5) as response:
                targets = json.load(response)
            worker = next(
                (
                    item for item in targets
                    if item.get("type") == "service_worker"
                    and item.get("url", "").startswith("chrome-extension://")
                ),
                None,
            )
        extension_id = os.getenv("RADIXSOL_EXTENSION_ID", "")
        if launch_browser and worker:
            extension_id = worker.url.split("/")[2]
        if not extension_id:
            extensions_page = await context.new_page()
            await extensions_page.goto("chrome://extensions")
            await extensions_page.wait_for_timeout(500)
            installed = await extensions_page.evaluate(
                """() => {
                  const manager = document.querySelector('extensions-manager');
                  const list = manager?.shadowRoot?.querySelector('extensions-item-list');
                  const items = list?.shadowRoot?.querySelectorAll('extensions-item') || [];
                  return Array.from(items).map((item) => ({
                    id: item.id,
                    name: item.shadowRoot?.querySelector('#name')?.textContent?.trim() || ''
                  }));
                }"""
            )
            await extensions_page.close()
            match = next(
                (item for item in installed if "Radixsol" in item.get("name", "")),
                None,
            )
            extension_id = match["id"] if match else ""
        if not extension_id and isinstance(worker, dict) and "radixsol" in worker.get("title", "").lower():
            extension_id = worker["url"].split("/")[2]
        if not extension_id:
            raise RuntimeError("Radixsol extension is not loaded in the test browser.")
        if backend_url and launch_browser and worker:
            await worker.evaluate(
                """async (url) => {
                  await chrome.storage.local.set({ backendUrl: url });
                }""",
                backend_url,
            )

        panel = await context.new_page()
        await panel.set_viewport_size({"width": 420, "height": 900})
        await panel.goto(f"chrome-extension://{extension_id}/index.html")

        # A side panel leaves Indeed as the active tab. Reproduce that state
        # while keeping the extension page available for DOM assertions.
        await indeed_page.bring_to_front()
        await panel.evaluate(
            "document.querySelector('[data-action=\"refresh-indeed\"]')?.click()"
        )
        await panel.locator(".capture-toolbar strong").wait_for(timeout=20_000)
        captured_text = await panel.locator("body").inner_text()
        assert "50 candidates captured" in captured_text
        await panel.locator('[data-action="toggle-all-indeed"]').click()
        await panel.locator(".indeed-select").first.check()
        assert "Look up 1 candidate" in await panel.locator("body").inner_text()
        await panel.screenshot(path=str(screenshot), full_page=True)

        await panel.evaluate(
            "document.querySelector('[data-action=\"lookup-indeed\"]')?.click()"
        )
        await panel.wait_for_selector(".result-summary", timeout=45_000)
        result_text = await panel.locator("body").inner_text()
        assert "matches" in result_text
        assert "batch actions" in result_text.lower()
        summary_text = (
            await panel.locator(".result-summary").inner_text()
        ).replace("\n", " ")
        await panel.screenshot(path=str(result_screenshot), full_page=True)
        if summary_text != "1 matches 0 no match":
            diagnostics = {}
            if launch_browser and worker:
                diagnostics = await worker.evaluate(
                    """async () => {
                      const stored = await chrome.storage.local.get(
                        ['radixsolUSPhoneBookDiagnosticsV1']
                      );
                      return stored.radixsolUSPhoneBookDiagnosticsV1 || [];
                    }"""
                )
            raise AssertionError({
                "summary": summary_text,
                "panel": result_text,
                "provider_requests": provider_requests,
                "diagnostics": diagnostics[:3],
            })
        visited_profiles = [
            urlparse(url)
            for url in provider_requests
            if "/find/person/" in urlparse(url).path
        ]
        assert {
            parsed.path.rsplit("/", 1)[-1]
            for parsed in visited_profiles
        } == {"right", "wrong"}, provider_requests
        right_visits = [
            parsed for parsed in visited_profiles
            if parsed.path.endswith("/right")
        ]
        assert right_visits and all(
            "via=button" in parsed.query for parsed in right_visits
        ), provider_requests
        assert not any(
            urlparse(url).path in {"/address", "/phone", "/name"}
            for url in provider_requests
        ), provider_requests
        print({
            "extension_id": extension_id,
            "captured": 50,
            "looked_up": 1,
            "result_summary": summary_text,
            "screenshot": str(screenshot),
            "result_screenshot": str(result_screenshot),
        })
        await panel.close()
        if launch_browser:
            await context.close()
        else:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
