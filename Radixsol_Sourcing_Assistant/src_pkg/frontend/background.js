importScripts("usphonebook-parser.js");

async function enableActionClick() {
  await chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true });
}

chrome.runtime.onInstalled.addListener(() => {
  enableActionClick().catch(console.error);
});

chrome.runtime.onStartup.addListener(() => {
  enableActionClick().catch(console.error);
});

chrome.action.onClicked.addListener(async (tab) => {
  try {
    await chrome.sidePanel.open({ windowId: tab.windowId });
  } catch {
    // Older browser versions use the setPanelBehavior configuration above.
  }
});

enableActionClick().catch(console.error);

const USPHONEBOOK_CACHE_KEY = "radixsolUSPhoneBookCacheV5";
const USPHONEBOOK_DIAGNOSTIC_KEY = "radixsolUSPhoneBookDiagnosticsV1";
const USPHONEBOOK_MIN_NAVIGATION_MS = 8000;
const USPHONEBOOK_SUCCESS_TTL_MS = 6 * 60 * 60 * 1000;
const USPHONEBOOK_NO_MATCH_TTL_MS = 30 * 60 * 1000;
let usPhoneBookLookupQueue = Promise.resolve();
let lastUSPhoneBookNavigation = 0;

function sleep(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function normalizedSearchValue(value) {
  return String(value || "")
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, " ")
    .trim();
}

function usPhoneBookCacheKey(name, location, jobTitle) {
  return [
    normalizedSearchValue(name),
    normalizedSearchValue(location),
    normalizedSearchValue(jobTitle),
  ].join("|");
}

async function cachedUSPhoneBookResult(name, location, jobTitle) {
  const stored = await chrome.storage.local.get([USPHONEBOOK_CACHE_KEY]);
  const entries = Array.isArray(stored[USPHONEBOOK_CACHE_KEY])
    ? stored[USPHONEBOOK_CACHE_KEY]
    : [];
  const now = Date.now();
  const current = entries.filter((entry) => Number(entry?.expiresAt) > now);
  if (current.length !== entries.length) {
    await chrome.storage.local.set({ [USPHONEBOOK_CACHE_KEY]: current });
  }
  const match = current.find((entry) => entry.key === usPhoneBookCacheKey(name, location, jobTitle));
  return match?.result ? { ...match.result, cached: true } : null;
}

async function cacheUSPhoneBookResult(name, location, jobTitle, result) {
  const ttl = result?.status === "success"
    ? USPHONEBOOK_SUCCESS_TTL_MS
    : result?.status === "no_match" && result?.confirmed_no_match
      ? USPHONEBOOK_NO_MATCH_TTL_MS
      : 0;
  if (!ttl) return;
  const stored = await chrome.storage.local.get([USPHONEBOOK_CACHE_KEY]);
  const now = Date.now();
  const key = usPhoneBookCacheKey(name, location, jobTitle);
  const entries = (Array.isArray(stored[USPHONEBOOK_CACHE_KEY])
    ? stored[USPHONEBOOK_CACHE_KEY]
    : []).filter((entry) => entry.key !== key && Number(entry?.expiresAt) > now);
  entries.unshift({
    key,
    expiresAt: now + ttl,
    result: { ...result, cached: false },
  });
  await chrome.storage.local.set({ [USPHONEBOOK_CACHE_KEY]: entries.slice(0, 100) });
}

async function waitForProviderNavigationSlot() {
  const remaining = USPHONEBOOK_MIN_NAVIGATION_MS - (Date.now() - lastUSPhoneBookNavigation);
  if (remaining > 0) await sleep(remaining);
  lastUSPhoneBookNavigation = Date.now();
}

async function waitForTabComplete(tabId, timeout = 30000) {
  const completed = await new Promise((resolve, reject) => {
    let settled = false;
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      chrome.tabs.onUpdated.removeListener(listener);
      reject(new Error("USPhoneBook took too long to load."));
    }, timeout);
    function finish(tab) {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      chrome.tabs.onUpdated.removeListener(listener);
      resolve(tab);
    }
    function listener(updatedTabId, changeInfo, tab) {
      if (updatedTabId !== tabId || changeInfo.status !== "complete") return;
      finish(tab);
    }
    chrome.tabs.onUpdated.addListener(listener);
    chrome.tabs.get(tabId).then((tab) => {
      if (tab.status === "complete") finish(tab);
    }).catch((error) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      chrome.tabs.onUpdated.removeListener(listener);
      reject(error);
    });
  });
  await sleep(600);
  return completed;
}

async function sendUSPhoneBookExtract(tabId, payload) {
  let lastError = null;
  for (let attempt = 0; attempt < 8; attempt += 1) {
    try {
      return await chrome.tabs.sendMessage(tabId, {
        type: "RADIXSOL_USPHONEBOOK_EXTRACT",
        expectedName: payload.name,
        location: payload.location,
        jobTitle: payload.jobTitle,
      });
    } catch (error) {
      lastError = error;
      await sleep(350);
    }
  }
  try {
    await chrome.scripting.executeScript({
      target: { tabId },
      files: ["usphonebook-parser.js", "usphonebook-content.js"],
    });
    return await chrome.tabs.sendMessage(tabId, {
      type: "RADIXSOL_USPHONEBOOK_EXTRACT",
      expectedName: payload.name,
      location: payload.location,
      jobTitle: payload.jobTitle,
    });
  } catch (error) {
    throw lastError || error;
  }
}

function providerExtractionReady(response) {
  if (!response?.ok) return false;
  if (response.action === "follow_profile" && response.profileUrl) return true;
  if (response.action !== "result") return false;
  const status = response.result?.status;
  if (status === "no_match" && !response.result?.confirmed_no_match) return false;
  if (status === "contact_incomplete") return false;
  return Boolean(status);
}

async function extractUSPhoneBookWithSettling(tabId, payload) {
  let response = null;
  for (let attempt = 0; attempt < 6; attempt += 1) {
    response = await sendUSPhoneBookExtract(tabId, payload);
    if (providerExtractionReady(response)) return response;
    await sleep(900);
  }
  return response;
}

async function recordUSPhoneBookDiagnostic(payload, result) {
  const stored = await chrome.storage.local.get([USPHONEBOOK_DIAGNOSTIC_KEY]);
  const entries = Array.isArray(stored[USPHONEBOOK_DIAGNOSTIC_KEY])
    ? stored[USPHONEBOOK_DIAGNOSTIC_KEY]
    : [];
  entries.unshift({
    at: new Date().toISOString(),
    name: String(payload?.name || "").slice(0, 200),
    location: String(payload?.location || "").slice(0, 500),
    job_title: String(payload?.jobTitle || "").slice(0, 500),
    status: String(result?.status || "error"),
    message: String(result?.message || result?.error || "").slice(0, 1000),
    profile_url: String(result?.profile_url || "").slice(0, 2000),
    cached: Boolean(result?.cached),
  });
  await chrome.storage.local.set({
    [USPHONEBOOK_DIAGNOSTIC_KEY]: entries.slice(0, 100),
  });
}

async function waitForManualUSPhoneBookVerification(tabId, payload) {
  await chrome.tabs.update(tabId, { active: true });
  const deadline = Date.now() + (2 * 60 * 1000);
  while (Date.now() < deadline) {
    await sleep(3000);
    try {
      const response = await extractUSPhoneBookWithSettling(tabId, payload);
      if (
        response?.ok &&
        response.action === "result" &&
        !["captcha_required", "blocked"].includes(response.result?.status)
      ) {
        return response;
      }
      if (response?.ok && response.action === "follow_profile") return response;
    } catch {
      // The user may still be completing a provider navigation.
    }
  }
  return null;
}

async function closeProviderTab(tabId) {
  try {
    await chrome.tabs.remove(tabId);
  } catch {
    // The user may already have closed the temporary provider tab.
  }
}

async function sendUSPhoneBookProgress(payload, message) {
  await chrome.runtime.sendMessage({
    type: "RADIXSOL_USPHONEBOOK_PROGRESS",
    profileKey: String(payload?.profileKey || ""),
    name: String(payload?.name || ""),
    location: String(payload?.location || ""),
    message: String(message || ""),
  }).catch(() => {});
}

function safeUSPhoneBookUrl(value) {
  try {
    const url = new URL(value);
    if (
      url.protocol !== "https:" ||
      !["usphonebook.com", "www.usphonebook.com"].includes(url.hostname.toLowerCase())
    ) {
      return "";
    }
    return url.href;
  } catch {
    return "";
  }
}

async function waitForTabUrlChange(tabId, previousUrl, timeout = 12000) {
  const previous = String(previousUrl || "").replace(/\/+$/, "");
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    const tab = await chrome.tabs.get(tabId);
    const current = String(tab.url || "").replace(/\/+$/, "");
    if (current && current !== previous) return tab;
    await sleep(250);
  }
  throw new Error("The USPhoneBook profile button did not navigate.");
}

async function openUSPhoneBookProfile(tabId, profileUrl, preferClick = true) {
  const url = safeUSPhoneBookUrl(profileUrl);
  if (!url) throw new Error("USPhoneBook returned an invalid profile URL.");
  const previousTab = await chrome.tabs.get(tabId);
  const previousUrl = previousTab.url || "";
  let clicked = false;
  if (preferClick) {
    try {
      await chrome.tabs.update(tabId, { active: true });
      const response = await chrome.tabs.sendMessage(tabId, {
        type: "RADIXSOL_USPHONEBOOK_CLICK_PROFILE",
        profileUrl: url,
      });
      clicked = Boolean(response?.ok && response?.clicked);
    } catch {
      // A direct same-provider navigation remains available as a fallback.
    }
  }
  if (clicked) {
    try {
      await waitForTabUrlChange(tabId, previousUrl);
      try {
        await waitForTabComplete(tabId, 15000);
      } catch {
        // Some provider analytics keep the tab loading after the profile DOM is usable.
        await sleep(600);
      }
      return;
    } catch {
      // Some provider actions suppress scripted clicks; direct navigation is the fallback.
    }
  }
  await chrome.tabs.update(tabId, { url, active: true });
  await waitForTabComplete(tabId);
}

async function reviewAmbiguousUSPhoneBookProfiles(tabId, ambiguousResult, payload) {
  const parser = globalThis.RadixsolUSPhoneBookParser;
  const profiles = Array.isArray(ambiguousResult?.profiles)
    ? ambiguousResult.profiles.slice(0, 10)
    : [];
  if (!profiles.length || !parser?.selectReviewedProfile) return ambiguousResult;

  const reviewed = [];
  for (let index = 0; index < profiles.length; index += 1) {
    const candidate = profiles[index];
    await sendUSPhoneBookProgress(
      payload,
      `Reviewing profile ${index + 1} of ${profiles.length} for address and work history…`
    );
    await waitForProviderNavigationSlot();
    await openUSPhoneBookProfile(tabId, candidate.profile_url, index === 0);
    let response = await extractUSPhoneBookWithSettling(tabId, payload);
    if (
      response?.action === "result" &&
      ["captcha_required", "blocked"].includes(response.result?.status)
    ) {
      response = await waitForManualUSPhoneBookVerification(tabId, payload);
      if (!response) {
        return {
          ...ambiguousResult,
          status: "captcha_required",
          message: "Manual USPhoneBook verification was not completed within two minutes.",
          provider_tab_id: tabId,
        };
      }
    }
    const profile = response?.action === "result"
      ? response.result?.review_profile
      : null;
    if (profile) {
      reviewed.push({
        ...profile,
        card_location_score: candidate.card_location_score,
        card_location_relevance: candidate.card_location_relevance,
        card_job_score: candidate.card_job_score,
        card_text: candidate.card_text,
      });
    }
  }

  const decision = parser.selectReviewedProfile(
    reviewed,
    payload.location || "",
    payload.jobTitle || ""
  );
  const matched = parser.resultFromReviewedProfile(decision.selected);
  if (matched) return matched;
  const reasonMessages = {
    location: "none retained the exact requested location",
    job_title_missing: "no profile displayed the requested job title",
    job_title_tie: "the job title still matched more than one profile",
    contact_incomplete: "the selected profile did not display a complete contact",
  };
  return {
    ...ambiguousResult,
    profiles: undefined,
    reviewed_profile_count: reviewed.length,
    message: `Reviewed ${reviewed.length} exact-name/location profile${reviewed.length === 1 ? "" : "s"}; ${reasonMessages[decision.reason] || "job-title evidence did not identify one unique match"}.`,
  };
}

async function runUSPhoneBookLookup(payload) {
  const parser = globalThis.RadixsolUSPhoneBookParser;
  const name = String(payload?.name || "").trim().slice(0, 200);
  const location = String(payload?.location || "").trim().slice(0, 500);
  const jobTitle = String(payload?.jobTitle || "").trim().slice(0, 500);
  const lookupPayload = {
    name,
    location,
    jobTitle,
    profileKey: String(payload?.profileKey || "").slice(0, 500),
  };
  if (!parser || !name) throw new Error("A candidate name is required for USPhoneBook.");

  const cached = await cachedUSPhoneBookResult(name, location, jobTitle);
  if (cached) return cached;

  const urls = parser.buildProviderUrls(name, location);
  let tab = null;
  let lastResult = null;
  try {
    for (let index = 0; index < urls.length; index += 1) {
      await waitForProviderNavigationSlot();
      if (!tab) {
        tab = await chrome.tabs.create({ url: urls[index], active: false });
      } else {
        await chrome.tabs.update(tab.id, { url: urls[index], active: false });
      }
      await waitForTabComplete(tab.id);
      await sendUSPhoneBookProgress(lookupPayload, "Reading USPhoneBook search results…");
      let response = await extractUSPhoneBookWithSettling(tab.id, lookupPayload);
      if (!response?.ok) throw new Error(response?.error || "USPhoneBook extraction failed.");

      if (response.action === "follow_profile" && response.profileUrl) {
        await sendUSPhoneBookProgress(lookupPayload, "Opening “View Full Address & Phone”…");
        await waitForProviderNavigationSlot();
        await openUSPhoneBookProfile(tab.id, response.profileUrl, true);
        response = await extractUSPhoneBookWithSettling(tab.id, lookupPayload);
      }

      if (
        response?.action === "result" &&
        ["captcha_required", "blocked"].includes(response.result?.status)
      ) {
        const recovered = await waitForManualUSPhoneBookVerification(tab.id, lookupPayload);
        if (!recovered) {
          return {
            ...response.result,
            message: "Manual USPhoneBook verification was not completed within two minutes.",
            provider_tab_id: tab.id,
          };
        }
        response = recovered;
        if (response.action === "follow_profile" && response.profileUrl) {
          await sendUSPhoneBookProgress(lookupPayload, "Opening “View Full Address & Phone”…");
          await waitForProviderNavigationSlot();
          await openUSPhoneBookProfile(tab.id, response.profileUrl, true);
          response = await extractUSPhoneBookWithSettling(tab.id, lookupPayload);
        }
      }

      if (!response?.ok || response.action !== "result") {
        throw new Error(response?.error || "USPhoneBook returned an invalid result.");
      }
      lastResult = response.result;
      if (lastResult.status === "multiple_matches") {
        lastResult = await reviewAmbiguousUSPhoneBookProfiles(
          tab.id,
          lastResult,
          lookupPayload
        );
      }
      if (
        lastResult.status !== "no_match" ||
        lastResult.confirmed_no_match ||
        index === urls.length - 1
      ) {
        await cacheUSPhoneBookResult(name, location, jobTitle, lastResult);
        await closeProviderTab(tab.id);
        return lastResult;
      }
    }
    return lastResult || {
      source: "usphonebook",
      status: "no_match",
      phones: [],
      emails: [],
      addresses: [],
      confidence: 0,
      message: "No USPhoneBook result was found.",
    };
  } catch (error) {
    if (tab?.id) await closeProviderTab(tab.id);
    return {
      source: "usphonebook",
      status: "error",
      phones: [],
      emails: [],
      addresses: [],
      confidence: 0,
      message: String(error?.message || error),
    };
  }
}

async function dispatchTrustedIndeedClick(tabId, x, y) {
  const target = { tabId };
  const point = { x, y, button: "left" };
  let attached = false;
  try {
    await chrome.debugger.attach(target, "1.3");
    attached = true;
    await chrome.debugger.sendCommand(target, "Input.dispatchMouseEvent", {
      type: "mouseMoved",
      ...point,
    });
    await chrome.debugger.sendCommand(target, "Input.dispatchMouseEvent", {
      type: "mousePressed",
      ...point,
      clickCount: 1,
    });
    await chrome.debugger.sendCommand(target, "Input.dispatchMouseEvent", {
      type: "mouseReleased",
      ...point,
      clickCount: 1,
    });
    return { ok: true };
  } catch (error) {
    return { ok: false, error: String(error?.message || error) };
  } finally {
    if (attached) await chrome.debugger.detach(target).catch(() => {});
  }
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.type === "RADIXSOL_TRUSTED_INDEED_CLICK") {
    const tabId = Number(sender?.tab?.id);
    const tabUrl = String(sender?.tab?.url || "");
    const x = Number(message.x);
    const y = Number(message.y);
    if (
      !tabId ||
      !/(^|\.)indeed\.com$/i.test((() => {
        try { return new URL(tabUrl).hostname; } catch { return ""; }
      })()) ||
      !Number.isFinite(x) || !Number.isFinite(y) ||
      x < 0 || y < 0 || x > 10000 || y > 10000
    ) {
      sendResponse({ ok: false, error: "Trusted clicks are restricted to visible Indeed content." });
      return false;
    }
    dispatchTrustedIndeedClick(tabId, x, y).then(sendResponse);
    return true;
  }
  if (message?.type === "RADIXSOL_SET_ACTIVE_CANDIDATE") {
    chrome.storage.session.set({
      radixsolResumeCandidate: {
        candidateId: Number(message.candidateId),
        name: String(message.name || ""),
        sourceId: String(message.sourceId || ""),
        expiresAt: Date.now() + (10 * 60 * 1000),
      },
    }).then(() => sendResponse({ ok: true })).catch((error) => {
      sendResponse({ ok: false, error: String(error) });
    });
    return true;
  }
  if (message?.type === "RADIXSOL_CLEAR_ACTIVE_CANDIDATE") {
    chrome.storage.session.remove(["radixsolResumeCandidate"])
      .then(() => sendResponse({ ok: true }))
      .catch((error) => sendResponse({ ok: false, error: String(error) }));
    return true;
  }
  if (message?.type === "RADIXSOL_USPHONEBOOK_LOOKUP") {
    const task = usPhoneBookLookupQueue.then(() => runUSPhoneBookLookup(message));
    usPhoneBookLookupQueue = task.catch(() => {});
    task.then(async (result) => {
      await recordUSPhoneBookDiagnostic(message, result).catch(() => {});
      sendResponse({ ok: true, result });
    }).catch(async (error) => {
      await recordUSPhoneBookDiagnostic(message, {
        status: "error",
        message: String(error?.message || error),
      }).catch(() => {});
      sendResponse({ ok: false, error: String(error?.message || error) });
    });
    return true;
  }
  return false;
});

function isIndeedResumeDownload(download) {
  const origin = [
    download.url,
    download.finalUrl,
    download.referrer,
  ].filter(Boolean).join(" ");
  return /(^|[./])indeed\.com(?=[:/ ])/i.test(origin) &&
    /\.pdf(?:$|[?#])/i.test(download.filename || download.url || "");
}

// Indeed_automator fallback: remember a download URL created while the
// MAIN-world hook is armed. The content script can re-fetch this URL using the
// logged-in Indeed session when no Blob/fetch bytes were observed directly.
chrome.downloads.onCreated.addListener((download) => {
  (async () => {
    const capture = await chrome.storage.local.get(["radixsolResumeCapturing"]);
    if (!capture.radixsolResumeCapturing) return;
    const url = download.finalUrl || download.url || "";
    if (!url) return;
    await chrome.storage.local.set({
      radixsolLastResumeDownload: {
        id: download.id,
        url,
        mime: download.mime || "",
        filename: download.filename || "",
        createdAt: Date.now(),
      },
    });
  })().catch(() => {});
});

chrome.downloads.onChanged.addListener((delta) => {
  if (delta.state?.current !== "complete") return;
  (async () => {
    const [download] = await chrome.downloads.search({ id: delta.id });
    if (!download || !isIndeedResumeDownload(download)) return;
    const stored = await chrome.storage.session.get(["radixsolResumeCandidate"]);
    const candidate = stored.radixsolResumeCandidate;
    if (
      !candidate?.candidateId ||
      Number(candidate.expiresAt) < Date.now()
    ) {
      return;
    }
    await chrome.runtime.sendMessage({
      type: "RADIXSOL_RESUME_DOWNLOADED",
      candidateId: Number(candidate.candidateId),
      candidateName: candidate.name,
      path: download.filename,
      filename: String(download.filename || "").split(/[\\/]/).pop() || "resume.pdf",
    }).catch(() => {});
    await chrome.storage.session.remove(["radixsolResumeCandidate"]);
  })().catch(console.error);
});
