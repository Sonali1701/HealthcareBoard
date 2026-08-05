(() => {
  "use strict";

  if (window.__radixsolIndeedCaptureLoaded) return;
  window.__radixsolIndeedCaptureLoaded = true;

  const NAME_SELECTORS = [
    "[data-cauto-id='candidate-name']",
    "[data-cauto-id*='candidate-name']",
    "[data-testid*='candidate-name']",
    "[data-testid*='profile'] h1",
    "[role='dialog'] h1",
    "[role='dialog'] h2",
  ];
  const DOWNLOAD_SELECTORS = [
    "button[aria-label='Download actions']",
    "button[aria-label*='ownload']",
    "a[href$='.pdf']",
    "a[href*='.pdf?']",
    "[data-testid*='download']",
  ];
  const PROFILE_WORDS = /\b(insights|resume|work experience|experience|certifications?|skills|education)\b/i;
  const ACTION_WORDS = /^(message|save|more|find|search|next|previous|download|close)$/i;
  let scanInProgress = false;

  function all(selector, root = document) {
    try {
      return Array.from(root.querySelectorAll(selector));
    } catch {
      return [];
    }
  }

  function isVisible(element) {
    if (!element) return false;
    const style = getComputedStyle(element);
    if (style.display === "none" || style.visibility === "hidden" || Number(style.opacity) === 0) {
      return false;
    }
    const rect = element.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0 && rect.bottom > 0 && rect.right > 0;
  }

  function visibleText(element) {
    return String(element?.innerText || element?.textContent || "")
      .replace(/\u00a0/g, " ")
      .replace(/[ \t]+/g, " ")
      .trim();
  }

  function textLines(element) {
    const seen = new Set();
    return visibleText(element)
      .split(/\r?\n/)
      .map((line) => line.replace(/\s+/g, " ").trim())
      .filter((line) => {
        const key = line.toLowerCase();
        if (!line || seen.has(key)) return false;
        seen.add(key);
        return true;
      });
  }

  function sleep(milliseconds) {
    return new Promise((resolve) => setTimeout(resolve, milliseconds));
  }

  // Indeed's React handlers sometimes listen to pointer/mouse events rather
  // than a bare HTMLElement.click(). This mirrors a normal user click closely
  // enough to open candidate panels and their action menus.
  function realClick(element) {
    if (!element) return;
    const options = { bubbles: true, cancelable: true, view: window };
    try { element.dispatchEvent(new PointerEvent("pointerdown", options)); } catch {}
    try { element.dispatchEvent(new MouseEvent("mousedown", options)); } catch {}
    try { element.dispatchEvent(new PointerEvent("pointerup", options)); } catch {}
    try { element.dispatchEvent(new MouseEvent("mouseup", options)); } catch {}
    try { element.click(); } catch {}
  }

  // Resume bytes posted by inject.js from Indeed's MAIN JavaScript world.
  // Keep the largest capture because the real resume is normally much larger
  // than unrelated blobs created by the page.
  let capturedResume = null;
  let capturedTentativeResume = null;
  let resumeCaptureEvents = [];
  window.addEventListener("message", (event) => {
    if (event.source !== window || !event.data?.__radixsolResume) return;
    const incoming = {
      base64: String(event.data.base64 || ""),
      contentType: String(event.data.contentType || ""),
    };
    incoming.size = incoming.base64.length;
    resumeCaptureEvents.push(
      `${incoming.contentType || "unknown"}:${incoming.size}${event.data.tentative ? ":tentative" : ""}`,
    );
    if (event.data.tentative) {
      if (!capturedTentativeResume || incoming.size > capturedTentativeResume.size) {
        capturedTentativeResume = incoming;
      }
    } else if (!capturedResume || incoming.size > capturedResume.size) {
      capturedResume = incoming;
    }
  });

  function bytesToBase64(bytes) {
    let binary = "";
    const chunkSize = 0x8000;
    for (let index = 0; index < bytes.length; index += chunkSize) {
      binary += String.fromCharCode.apply(null, bytes.subarray(index, index + chunkSize));
    }
    return btoa(binary);
  }

  async function fetchResumeUrl(url) {
    try {
      const resolved = url.startsWith("/") ? `${location.origin}${url}` : url;
      const response = await fetch(resolved, { credentials: "include" });
      if (!response.ok) return null;
      const bytes = new Uint8Array(await response.arrayBuffer());
      if (!bytes.length) return null;
      return {
        base64: bytesToBase64(bytes),
        contentType: response.headers.get("content-type") || "application/pdf",
      };
    } catch {
      return null;
    }
  }

  async function beginResumeCapture() {
    capturedResume = null;
    capturedTentativeResume = null;
    resumeCaptureEvents = [];
    await chrome.storage.local.set({ radixsolResumeCapturing: true });
    await chrome.storage.local.remove(["radixsolLastResumeDownload"]);
  }

  async function endResumeCapture() {
    await chrome.storage.local.set({ radixsolResumeCapturing: false });
  }

  async function lastCapturedDownload() {
    try {
      const stored = await chrome.storage.local.get(["radixsolLastResumeDownload"]);
      return stored.radixsolLastResumeDownload || null;
    } catch {
      return null;
    }
  }

  async function waitForResumeBytes(retryDownload, timeoutMs = 45000) {
    const started = Date.now();
    let retries = 0;
    let captureObservedAt = 0;
    while (Date.now() - started < timeoutMs) {
      if (capturedResume) {
        if (!captureObservedAt) captureObservedAt = Date.now();
        // Give Chrome a brief chance to report its own visible download. If it
        // does not, the side panel explicitly saves the captured PDF.
        if (Date.now() - captureObservedAt >= 900) {
          return {
            ...capturedResume,
            via: "main-world-hook",
            browserDownloadSeen: Boolean(await lastCapturedDownload()),
          };
        }
      }

      const download = await lastCapturedDownload();
      if (download?.url) {
        const fetched = await fetchResumeUrl(download.url);
        if (fetched) {
          return {
            ...fetched,
            via: "download-url",
            browserDownloadSeen: true,
          };
        }
      }

      const elapsed = Date.now() - started;
      if ((retries === 0 && elapsed > 8000) || (retries === 1 && elapsed > 20000)) {
        retries += 1;
        await retryDownload();
      }
      await sleep(250);
    }
    if (capturedTentativeResume) {
      return {
        ...capturedTentativeResume,
        via: "main-world-hook-tentative",
        browserDownloadSeen: Boolean(await lastCapturedDownload()),
      };
    }
    return {
      error: "The Download resume action was clicked, but Indeed produced no capturable PDF bytes.",
      diagnostics: resumeCaptureEvents.slice(-8),
    };
  }

  function looksLikeName(value) {
    const text = String(value || "").trim();
    if (text.length < 3 || text.length > 90 || /\d|@|http/i.test(text)) return false;
    const parts = text.split(/\s+/);
    return parts.length >= 2 && parts.length <= 6 &&
      parts.every((part) => /^[A-Za-zÀ-ÖØ-öø-ÿ.'’\-]+$/.test(part));
  }

  function commonAncestor(first, second) {
    if (!first || !second) return null;
    const ancestors = new Set();
    let node = first;
    while (node) {
      ancestors.add(node);
      node = node.parentElement;
    }
    node = second;
    while (node) {
      if (ancestors.has(node)) return node;
      node = node.parentElement;
    }
    return null;
  }

  function profileNameElements() {
    const elements = [];
    const seen = new Set();
    for (const selector of NAME_SELECTORS) {
      for (const element of all(selector)) {
        const name = visibleText(element).split("\n")[0].trim();
        if (!seen.has(element) && isVisible(element) && looksLikeName(name)) {
          seen.add(element);
          elements.push(element);
        }
      }
    }
    return elements;
  }

  function profileDownloadElements() {
    const score = (element) => {
      const label = element.getAttribute?.("aria-label") || "";
      if (/^download actions?$/i.test(label)) return 1000;
      if (element.matches?.("a[href$='.pdf'], a[href*='.pdf?']")) return 950;
      if (element.matches?.("button") && /download/i.test(label)) return 900;
      if (element.matches?.("button")) return 500;
      return 100;
    };
    // A data-testid wrapper may contain both the icon and its popup. Keep it as
    // a fallback, but rank the actual aria-labelled button/direct PDF link last
    // because currentProfileControlState intentionally selects the final item.
    return all(DOWNLOAD_SELECTORS.join(","))
      .filter(isVisible)
      .sort((first, second) => score(first) - score(second));
  }

  function chooseProfileContext() {
    const names = profileNameElements();
    const downloads = profileDownloadElements();
    let best = null;

    for (const nameElement of names) {
      const name = visibleText(nameElement).split("\n")[0].trim();
      if (downloads.length) {
        for (const download of downloads) {
          const root = commonAncestor(nameElement, download);
          const text = visibleText(root);
          if (!root || text.length < 120 || text.length > 50000) continue;
          const score =
            (PROFILE_WORDS.test(text) ? 5000 : 0) +
            (root.matches?.("[role='dialog'], aside, [data-testid*='profile']") ? 2500 : 0) -
            text.length;
          if (!best || score > best.score) best = { nameElement, name, root, score };
        }
      }

      const profileRoot = nameElement.closest(
        "[role='dialog'], [data-testid*='profile'], [data-cauto-id*='PROFILE'], [data-cauto-id*='profile'], aside"
      );
      const profileText = visibleText(profileRoot);
      if (profileRoot && profileText.length >= 120 && PROFILE_WORDS.test(profileText)) {
        const score = 6000 - profileText.length;
        if (!best || score > best.score) {
          best = { nameElement, name, root: profileRoot, score };
        }
      }
    }

    if (!best && names.length) {
      const nameElement = names[names.length - 1];
      let root = nameElement.parentElement;
      while (root && root !== document.body) {
        const text = visibleText(root);
        if (text.length >= 150 && text.length <= 25000 && PROFILE_WORDS.test(text)) {
          best = {
            nameElement,
            name: visibleText(nameElement).split("\n")[0].trim(),
            root,
            score: 1,
          };
          break;
        }
        root = root.parentElement;
      }
    }

    return best;
  }

  function extractLocation(root, name) {
    const selectors = [
      "[data-cauto-id*='location']",
      "[data-testid*='location']",
      "[class*='location']",
    ];
    const candidates = [];
    for (const selector of selectors) {
      for (const element of all(selector, root)) {
        if (isVisible(element)) candidates.push(visibleText(element).split("\n")[0]);
      }
    }
    candidates.push(...textLines(root));

    const locationPattern = /\b([A-ZÀ-ÖØ-Ý][A-Za-zÀ-ÖØ-öø-ÿ.'’\- ]{1,48},\s*[A-Z]{2}(?:\s+\d{5}(?:-\d{4})?)?)\b/;
    for (const candidate of candidates) {
      const match = candidate.match(locationPattern);
      if (match && match[1].toLowerCase() !== name.toLowerCase()) return match[1].trim();
    }
    return "";
  }

  function extractHeadline(root, name, location) {
    const selectors = [
      "[data-cauto-id*='job-title']",
      "[data-testid*='headline']",
      "[data-testid*='title']",
      "h2",
      "h3",
    ];
    const candidates = [];
    for (const selector of selectors) {
      for (const element of all(selector, root)) {
        if (isVisible(element)) candidates.push(visibleText(element).split("\n")[0]);
      }
    }
    candidates.push(...textLines(root).slice(0, 12));

    for (const candidate of candidates) {
      const value = candidate.trim();
      if (
        value.length >= 3 &&
        value.length <= 140 &&
        value.toLowerCase() !== name.toLowerCase() &&
        value.toLowerCase() !== location.toLowerCase() &&
        !ACTION_WORDS.test(value) &&
        !PROFILE_WORDS.test(value)
      ) {
        return value;
      }
    }
    return "";
  }

  function extractSourceIdentity(nameElement, root) {
    const card = nameElement.closest("[data-candidate-id]") ||
      all("[data-candidate-id]", root).find(isVisible);
    const sourceId = card?.getAttribute("data-candidate-id") || "";

    const link = nameElement.closest("a[href]") ||
      all("a[href]", root).find((element) => {
        const href = element.getAttribute("href") || "";
        return /candidate|resume|profile/i.test(href);
      });
    let sourceUrl = "";
    if (link?.getAttribute("href")) {
      try {
        sourceUrl = new URL(link.getAttribute("href"), location.origin).href;
      } catch {
        sourceUrl = "";
      }
    }
    if (!sourceUrl) sourceUrl = location.href;

    let derivedId = sourceId;
    if (!derivedId && sourceUrl) {
      try {
        const url = new URL(sourceUrl);
        derivedId =
          url.searchParams.get("candidateId") ||
          url.searchParams.get("candidate_id") ||
          url.searchParams.get("resumeId") ||
          url.searchParams.get("id") ||
          "";
      } catch {
        derivedId = "";
      }
    }
    return { sourceId: derivedId, sourceUrl };
  }

  let lastResultElements = [];

  function scanDisplayedCandidates() {
    const cardSelector = "[data-cauto-id^='MATCH_CARD_BASE-'], [data-candidate-id]";
    const nameSelector = "[data-cauto-id='candidate-name'], [data-cauto-id*='candidate-name'], [data-testid*='candidate-name']";
    const cards = all(cardSelector);
    let items = [];

    if (cards.length) {
      items = cards.map((card) => {
        const nameElement =
          card.querySelector(nameSelector) ||
          card.querySelector("a[href*='candidate'], a[href*='resume'], h2, h3");
        return { card, nameElement };
      });
    } else {
      items = all(nameSelector)
        .filter((element) => {
          const name = visibleText(element).split("\n")[0].trim();
          return looksLikeName(name);
        })
        .map((nameElement) => ({
          nameElement,
          card: nameElement.closest(
            "[data-candidate-id], [data-cauto-id^='MATCH_CARD_BASE-'], article, li, [class*='card'], [class*='result']"
          ) || nameElement.parentElement,
        }));
    }

    const profiles = [];
    const elements = [];
    const seen = new Set();

    for (const item of items) {
      if (!item.card || !item.nameElement) continue;
      const name = visibleText(item.nameElement).split("\n")[0].trim();
      if (!looksLikeName(name)) continue;

      const locationText = extractLocation(item.card, name);
      const identity = extractSourceIdentity(item.nameElement, item.card);
      const cardAutoId = item.card.getAttribute("data-cauto-id") || "";
      const sourceId = identity.sourceId || (
        cardAutoId.startsWith("MATCH_CARD_BASE-")
          ? cardAutoId.slice("MATCH_CARD_BASE-".length)
          : ""
      );
      const key = sourceId || `${name.toLowerCase()}|${locationText.toLowerCase()}`;
      if (seen.has(key)) continue;
      seen.add(key);

      const headline = extractHeadline(item.card, name, locationText);
      const notes = textLines(item.card)
        .filter((line) => !ACTION_WORDS.test(line))
        .join("\n")
        .slice(0, 4000);
      profiles.push({
        name,
        location: locationText,
        headline,
        notes,
        source: "indeed",
        source_url: identity.sourceUrl,
        source_id: sourceId,
        result_index: profiles.length,
      });
      elements.push(item.nameElement.closest("a[href]") || item.nameElement || item.card);
      if (profiles.length >= 100) break;
    }

    lastResultElements = elements;
    return {
      ok: true,
      profiles,
      count: profiles.length,
      raw: { cards: cards.length, names: all(nameSelector).length },
      page_url: location.href,
    };
  }

  function candidateKey(profile) {
    return profile.source_id ||
      `${String(profile.name || "").toLowerCase()}|${String(profile.location || "").toLowerCase()}`;
  }

  function expectedCandidateCount(scan) {
    const cardCount = Number(scan?.raw?.cards) || 0;
    const capturedCount = Number(scan?.count) || 0;
    const pageText = visibleText(document.body).slice(0, 30000);
    const range = pageText.match(/\b(\d{1,3})\s*[-–]\s*(\d{1,3})\s+of\s+[\d,]+/i);
    const rangeCount = range ? Math.max(0, Number(range[2]) - Number(range[1]) + 1) : 0;
    return Math.min(100, Math.max(cardCount, capturedCount, rangeCount));
  }

  function candidateScrollContainer() {
    const card = all("[data-cauto-id^='MATCH_CARD_BASE-'], [data-candidate-id]")[0];
    let element = card?.parentElement;
    while (element && element !== document.body) {
      const style = getComputedStyle(element);
      if (
        /(auto|scroll)/.test(style.overflowY) &&
        element.scrollHeight > element.clientHeight + 80
      ) {
        return element;
      }
      element = element.parentElement;
    }
    return document.scrollingElement || document.documentElement;
  }

  function reportScanProgress(found, total, profiles) {
    chrome.runtime.sendMessage({
      type: "RADIXSOL_INDEED_SCAN_PROGRESS",
      found,
      total,
      preview: profiles.slice(-5).map((profile) => ({
        name: profile.name,
        location: profile.location,
        headline: profile.headline,
      })),
    }, () => void chrome.runtime.lastError);
  }

  async function scanDisplayedCandidatesProgressively() {
    if (scanInProgress) {
      return { ok: false, error: "A candidate scan is already running." };
    }
    scanInProgress = true;
    const captured = new Map();
    const capturedElements = new Map();
    let expected = 0;
    const container = candidateScrollContainer();
    const originalTop = Number(container?.scrollTop) || 0;

    const mergeSnapshot = async (animate = false) => {
      const snapshot = scanDisplayedCandidates();
      expected = Math.max(expected, expectedCandidateCount(snapshot));
      const snapshotElements = [...lastResultElements];
      for (let index = 0; index < snapshot.profiles.length; index += 1) {
        const profile = snapshot.profiles[index];
        const key = candidateKey(profile);
        if (!captured.has(key)) {
          captured.set(key, profile);
          capturedElements.set(key, snapshotElements[index]);
          if (animate && (captured.size <= 5 || captured.size % 5 === 0)) {
            reportScanProgress(captured.size, expected, Array.from(captured.values()));
            await sleep(35);
          }
        }
      }
      reportScanProgress(captured.size, expected, Array.from(captured.values()));
      return snapshot;
    };

    try {
      await mergeSnapshot(true);
      let stableRounds = 0;
      let previousCount = captured.size;
      const maxTop = Math.max(0, (container?.scrollHeight || 0) - (container?.clientHeight || 0));

      for (let step = 1; step <= 18 && captured.size < expected && stableRounds < 3; step += 1) {
        const nextTop = Math.min(maxTop, Math.round((maxTop * step) / 18));
        if (typeof container?.scrollTo === "function") {
          container.scrollTo({ top: nextTop, behavior: "auto" });
        } else if (container) {
          container.scrollTop = nextTop;
        }
        await sleep(220);
        await mergeSnapshot();
        if (captured.size === previousCount) stableRounds += 1;
        else stableRounds = 0;
        previousCount = captured.size;
      }

      const profiles = Array.from(captured.values()).slice(0, 100);
      lastResultElements = profiles.map((profile) => capturedElements.get(candidateKey(profile)));
      return {
        ok: true,
        profiles: profiles.map((profile, index) => ({ ...profile, result_index: index })),
        count: profiles.length,
        expected_count: expected || profiles.length,
        raw: { cards: expected || profiles.length, names: profiles.length },
        page_url: location.href,
      };
    } finally {
      if (container) container.scrollTop = originalTop;
      scanInProgress = false;
    }
  }

  function openDisplayedCandidate(index) {
    const element = lastResultElements[Number(index)];
    if (!element) return { ok: false, error: "Candidate result is no longer available. Refresh the list." };
    try {
      element.scrollIntoView({ behavior: "smooth", block: "center" });
      realClick(element);
      return { ok: true };
    } catch (error) {
      return { ok: false, error: String(error) };
    }
  }

  function normalizedIdentity(value) {
    return String(value || "")
      .normalize("NFKD")
      .replace(/[\u0300-\u036f]/g, "")
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, " ")
      .trim();
  }

  function downloadActionText(element) {
    return [
      element?.getAttribute?.("aria-label") || "",
      element?.getAttribute?.("title") || "",
      visibleText(element),
    ].join(" ").replace(/\s+/g, " ").trim();
  }

  function isFinalResumeDownload(element) {
    const text = downloadActionText(element);
    if (!/download|pdf/i.test(text)) return false;
    if (element?.matches?.("a[href][download]")) return true;
    const label = [
      element?.getAttribute?.("aria-label") || "",
      element?.getAttribute?.("title") || "",
    ].join(" ").trim();
    return !/download actions?/i.test(label) && !/^download actions?$/i.test(text);
  }

  function visibleDownloadControls(root = document) {
    const selector = [
      ...DOWNLOAD_SELECTORS,
      "a[download]",
      "[role='menuitem']",
      "[role='option']",
      "button",
      "a[href]",
    ].join(",");
    return all(selector, root).filter((element) => (
      isVisible(element) && /download|pdf/i.test(downloadActionText(element))
    ));
  }

  function findDownloadMenuAction(trigger) {
    const scopes = [];
    const menuId = trigger?.getAttribute?.("aria-controls") || "";
    if (menuId) {
      const controlled = document.getElementById(menuId);
      if (controlled) scopes.push(controlled);
    }
    for (const menu of all(
      "[role='menu'], [role='listbox'], ul[id], [class*='menu'], " +
      "[class*='popover'], [class*='opover']"
    )) {
      if (isVisible(menu) && menu !== trigger && !trigger?.contains?.(menu)) scopes.push(menu);
    }
    scopes.push(document);

    for (const scope of scopes) {
      let best = null;
      let bestLength = Infinity;
      for (const element of all(
        "[role='menuitem'], [role='option'], [role='button'], button, a, li, div, span",
        scope,
      )) {
        // Indeed sometimes renders the popup inside a data-testid download
        // wrapper. Do not reject descendants of the trigger: that excluded the
        // real menu rows in the three-option Download profile/resume layout.
        if (!isVisible(element) || element === trigger) continue;
        const text = visibleText(element).replace(/\s+/g, " ").trim();
        if (!text || text.length > 40 || !/^download\b/i.test(text)) continue;
        if (/^download actions?$/i.test(text)) continue;
        if (text.length < bestLength) {
          best = element;
          bestLength = text.length;
        }
      }
      if (best) {
        return best.closest(
          "[role='menuitem'], [role='option'], [role='button'], button, a, li"
        ) || best;
      }
    }
    return null;
  }

  async function trustedClick(element) {
    if (!element) return { ok: false, error: "Download menu action was not found." };
    const rect = element.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) {
      return { ok: false, error: "Download menu action is not visible." };
    }
    const x = Math.max(2, Math.min(window.innerWidth - 2, Math.round(rect.left + rect.width / 2)));
    const y = Math.max(2, Math.min(window.innerHeight - 2, Math.round(rect.top + rect.height / 2)));
    return new Promise((resolve) => {
      chrome.runtime.sendMessage(
        { type: "RADIXSOL_TRUSTED_INDEED_CLICK", x, y },
        (response) => {
          const error = chrome.runtime.lastError;
          if (error) resolve({ ok: false, error: error.message });
          else resolve(response || { ok: false, error: "Trusted click returned no response." });
        },
      );
    });
  }

  function profileRootForControl(control) {
    if (!control) return null;
    const semantic = control.closest(
      "[role='dialog'], aside, [data-testid*='profile'], [data-testid*='resume'], " +
      "[data-cauto-id*='PROFILE'], [data-cauto-id*='profile'], [class*='profile']"
    );
    if (semantic) return semantic;
    let root = control.parentElement;
    while (root && root !== document.body) {
      const text = visibleText(root);
      if (text.length >= 80 && text.length <= 50000) return root;
      root = root.parentElement;
    }
    return control.parentElement || document.body;
  }

  function currentProfileControlState() {
    const controls = profileDownloadElements();
    const control = controls.length ? controls[controls.length - 1] : null;
    const root = profileRootForControl(control);
    return {
      control,
      root,
      signature: root
        ? visibleText(root).replace(/\s+/g, " ").slice(0, 4000)
        : "",
    };
  }

  function exactRenderedProfile(expectedName, selectedElement) {
    const expected = normalizedIdentity(expectedName);
    const selectedCard = selectedElement?.closest?.(
      "[data-cauto-id^='MATCH_CARD_BASE-'], [data-candidate-id], article, li"
    );
    const state = currentProfileControlState();
    if (!state.control) return null;

    const headingSelectors = [
      ...NAME_SELECTORS,
      "[role='heading']",
      "h1",
      "h2",
      "h3",
    ].join(",");
    const candidates = Array.from(new Set([
      ...all(headingSelectors),
      ...all("span, div", state.root || document),
    ]));
    const matches = candidates
      .filter((element) => (
        isVisible(element) &&
        (!selectedCard || !selectedCard.contains(element)) &&
        normalizedIdentity(visibleText(element)) === expected
      ))
      .map((element) => {
        const root = commonAncestor(element, state.control);
        const nameRect = element.getBoundingClientRect();
        const controlRect = state.control.getBoundingClientRect();
        const distance = Math.abs(nameRect.top - controlRect.top) + Math.abs(nameRect.left - controlRect.left);
        const semantic = element.closest(
          "[role='dialog'], aside, [data-testid*='profile'], [data-testid*='resume'], " +
          "[data-cauto-id*='PROFILE'], [data-cauto-id*='profile'], [class*='profile']"
        );
        const score =
          (semantic && semantic.contains(state.control) ? 100000 : 0) +
          (root && root !== document.body && root !== document.documentElement ? 50000 : 0) -
          distance - visibleText(root).length;
        return { element, root: semantic || root || state.root, score };
      })
      .sort((first, second) => second.score - first.score);

    if (!matches.length) return null;
    return {
      name: expectedName,
      nameElement: matches[0].element,
      root: matches[0].root || state.root,
      downloadControl: state.control,
      identityEvidence: "exact-rendered-name",
    };
  }

  async function waitForExpectedProfile(expectedName, selectedElement, beforeState, timeoutMs = 20000) {
    const expected = normalizedIdentity(expectedName);
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      const context = chooseProfileContext();
      if (context && normalizedIdentity(context.name) === expected) {
        const state = currentProfileControlState();
        return {
          ...context,
          downloadControl: state.control,
          identityEvidence: "profile-context-name",
        };
      }
      const rendered = exactRenderedProfile(expectedName, selectedElement);
      if (rendered) return rendered;

      // Some Smart Sourcing layouts do not render the candidate name inside
      // the profile pane. In that case, accept only if clicking the exact saved
      // result produced a new Download control or changed the profile pane.
      const state = currentProfileControlState();
      const panelChanged = state.control && (
        !beforeState.control ||
        state.control !== beforeState.control ||
        (state.signature && state.signature !== beforeState.signature)
      );
      if (panelChanged && normalizedIdentity(visibleText(selectedElement).split("\n")[0]) === expected) {
        return {
          name: expectedName,
          nameElement: selectedElement,
          root: state.root,
          downloadControl: state.control,
          identityEvidence: "exact-selected-card+changed-profile-panel",
        };
      }
      await sleep(250);
    }
    return null;
  }

  async function downloadDisplayedCandidateResume(index, expectedName) {
    const selectedElement = lastResultElements[Number(index)];
    if (!selectedElement) {
      return { ok: false, error: "Candidate result is no longer available. Refresh the list." };
    }
    const beforeState = currentProfileControlState();
    const opened = openDisplayedCandidate(index);
    if (!opened.ok) return opened;

    const context = await waitForExpectedProfile(expectedName, selectedElement, beforeState);
    if (!context) {
      return {
        ok: false,
        error: `Indeed did not open the exact profile for ${expectedName}; resume download was skipped.`,
      };
    }

    const scopedControls = visibleDownloadControls(context.root);
    if (context.downloadControl && !scopedControls.includes(context.downloadControl)) {
      scopedControls.push(context.downloadControl);
    }
    const direct = scopedControls.find(isFinalResumeDownload);
    const trigger = scopedControls.find((element) => (
      /download actions?/i.test([
        element?.getAttribute?.("aria-label") || "",
        element?.getAttribute?.("title") || "",
        downloadActionText(element),
      ].join(" "))
    ));
    if (!direct && !trigger) {
      return { ok: false, error: "The exact Indeed profile opened, but its Download actions button was not found." };
    }

    await beginResumeCapture();
    try {
      const activateDownload = async () => {
        if (direct) {
          direct.scrollIntoView({ behavior: "auto", block: "center" });
          realClick(direct);
          return {
            ok: true,
            trustedClick: false,
            clickedText: downloadActionText(direct).slice(0, 60),
          };
        }

        // Keep the verified profile's original control. Re-scanning while its
        // menu is open can return a popup wrapper instead of the icon button.
        const currentTrigger = context.downloadControl || trigger;
        currentTrigger.scrollIntoView({ behavior: "auto", block: "center" });
        let menuAction = findDownloadMenuAction(currentTrigger);
        if (!menuAction) realClick(currentTrigger);
        const deadline = Date.now() + 8000;
        let attempts = 0;
        while (Date.now() < deadline) {
          menuAction = findDownloadMenuAction(currentTrigger);
          if (menuAction) {
            const clicked = await trustedClick(menuAction);
            if (!clicked?.ok) realClick(menuAction);
            return {
              ok: true,
              trustedClick: Boolean(clicked?.ok),
              clickedText: visibleText(menuAction).slice(0, 60),
            };
          }
          attempts += 1;
          if (
            (attempts === 8 || attempts === 20 || attempts === 36) &&
            currentTrigger.getAttribute("aria-expanded") !== "true"
          ) realClick(currentTrigger);
          await sleep(150);
        }
        return {
          ok: false,
          error: "Indeed opened Download actions, but no Download resume option appeared.",
        };
      };

      const activated = await activateDownload();
      if (!activated.ok) return activated;
      const captured = await waitForResumeBytes(activateDownload);
      if (!captured?.base64) {
        return {
          ok: false,
          error: captured?.error || "Indeed resume bytes could not be captured.",
          diagnostics: captured?.diagnostics || [],
        };
      }
      return {
        ok: true,
        clicked: true,
        trustedClick: activated.trustedClick,
        clickedText: activated.clickedText,
        candidate: context.name,
        identityEvidence: context.identityEvidence,
        base64: captured.base64,
        contentType: captured.contentType || "application/pdf",
        captureVia: captured.via,
        browserDownloadSeen: Boolean(captured.browserDownloadSeen),
      };
    } finally {
      await endResumeCapture();
    }
  }

  function captureProfile() {
    if (!/(^|\.)indeed\.com$/i.test(location.hostname)) {
      return { ok: false, error: "The active tab is not an Indeed page." };
    }

    const context = chooseProfileContext();
    if (!context) {
      return {
        ok: false,
        error: "Open a candidate profile in Indeed Smart Sourcing, then try Capture again.",
      };
    }

    const name = context.name;
    const locationText = extractLocation(context.root, name);
    const headline = extractHeadline(context.root, name, locationText);
    const identity = extractSourceIdentity(context.nameElement, context.root);
    const notes = textLines(context.root)
      .filter((line) => !ACTION_WORDS.test(line))
      .join("\n")
      .slice(0, 12000);

    return {
      ok: true,
      profile: {
        name,
        location: locationText,
        headline,
        notes,
        source: "indeed",
        source_url: identity.sourceUrl,
        source_id: identity.sourceId,
        captured_at: new Date().toISOString(),
      },
    };
  }

  chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (message?.type === "RADIXSOL_INDEED_PING") {
      sendResponse({ ok: true, url: location.href, version: "3" });
      return false;
    }
    if (message?.type === "RADIXSOL_CAPTURE_INDEED_PROFILE") {
      sendResponse(captureProfile());
      return false;
    }
    if (message?.type === "RADIXSOL_LIST_INDEED_CANDIDATES") {
      sendResponse(scanDisplayedCandidates());
      return false;
    }
    if (message?.type === "RADIXSOL_SCAN_INDEED_CANDIDATES") {
      scanDisplayedCandidatesProgressively()
        .then(sendResponse)
        .catch((error) => sendResponse({ ok: false, error: String(error) }));
      return true;
    }
    if (message?.type === "RADIXSOL_OPEN_INDEED_CANDIDATE") {
      sendResponse(openDisplayedCandidate(message.index));
      return false;
    }
    if (message?.type === "RADIXSOL_DOWNLOAD_INDEED_RESUME") {
      downloadDisplayedCandidateResume(message.index, message.expectedName)
        .then(sendResponse)
        .catch((error) => sendResponse({ ok: false, error: String(error?.message || error) }));
      return true;
    }
    return false;
  });

  let resultsChangeTimer = null;
  let lastResultsSignature = "";

  function resultsSignature() {
    const cards = all("[data-cauto-id^='MATCH_CARD_BASE-'], [data-candidate-id]");
    return cards.slice(0, 100).map((card) => [
      card.getAttribute("data-candidate-id") || "",
      card.getAttribute("data-cauto-id") || "",
      visibleText(card).slice(0, 100),
    ].join(":")).join("|");
  }

  function detectResultsChange() {
    if (scanInProgress) return;
    clearTimeout(resultsChangeTimer);
    resultsChangeTimer = setTimeout(() => {
      const signature = resultsSignature();
      if (!signature || signature === lastResultsSignature) return;
      lastResultsSignature = signature;
      chrome.runtime.sendMessage(
        { type: "RADIXSOL_INDEED_RESULTS_CHANGED", count: signature.split("|").length },
        () => void chrome.runtime.lastError,
      );
    }, 1200);
  }

  const resultsObserver = new MutationObserver(detectResultsChange);
  resultsObserver.observe(document.documentElement, { childList: true, subtree: true });
  detectResultsChange();
})();
