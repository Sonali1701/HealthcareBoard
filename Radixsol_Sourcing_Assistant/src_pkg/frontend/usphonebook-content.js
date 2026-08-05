(function initializeUSPhoneBookContentScript() {
  "use strict";

  if (globalThis.__radixsolUSPhoneBookContentLoaded) return;
  globalThis.__radixsolUSPhoneBookContentLoaded = true;
  const parser = globalThis.RadixsolUSPhoneBookParser;
  if (!parser) return;

  function text(element) {
    return String(element?.innerText || element?.textContent || "").replace(/\s+/g, " ").trim();
  }

  function absoluteUrl(value) {
    try {
      const url = new URL(value, location.href);
      return ["usphonebook.com", "www.usphonebook.com"].includes(url.hostname.toLowerCase())
        ? url.href
        : "";
    } catch {
      return "";
    }
  }

  function visible(element) {
    if (!element) return false;
    const style = getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
  }

  function hasCaptcha() {
    if (/just a moment|security verification|verify you are human|complete the captcha/i.test(
      `${document.title} ${text(document.body).slice(0, 1200)}`
    )) {
      return true;
    }
    return Array.from(document.querySelectorAll(
      'iframe[src*="captcha"], iframe[src*="challenge"], .cf-turnstile, .g-recaptcha, [class*="captcha"], [id*="captcha"]'
    )).some(visible);
  }

  function profileAction(anchor) {
    const href = absoluteUrl(anchor.getAttribute("href") || anchor.href || "");
    if (!href) return false;
    const path = new URL(href).pathname.toLowerCase();
    if (/^\/(?:phone-search|address|terms|privacy|contact)(?:\/|$)/.test(path)) return false;
    return (
      /view.*(?:address|phone)|full.*(?:profile|details)/i.test(text(anchor)) ||
      path.includes("/find/person/") ||
      Boolean(anchor.closest("[data-detail-link]"))
    );
  }

  function profileUrlKey(value) {
    try {
      const url = new URL(value);
      return `${url.hostname.toLowerCase()}${url.pathname.replace(/\/+$/, "").toLowerCase()}`;
    } catch {
      return "";
    }
  }

  function resultCards() {
    const cards = [];
    const seen = new Set();
    document.querySelectorAll(
      'div.card-summary[data-detail-link], [data-detail-link*="/find/person/"]'
    ).forEach((container) => {
      const href = absoluteUrl(container.getAttribute("data-detail-link") || "");
      const key = profileUrlKey(href);
      if (!href || !key || seen.has(key)) return;
      const heading = container.querySelector(
        ".content-header, h1, h2, h3, [itemprop='name']"
      );
      cards.push({ href, name: text(heading), text: text(container) });
      seen.add(key);
    });
    for (const anchor of document.querySelectorAll("a[href]")) {
      if (!profileAction(anchor)) continue;
      const href = absoluteUrl(anchor.getAttribute("href") || anchor.href || "");
      const key = profileUrlKey(href);
      if (!href || !key || seen.has(key)) continue;
      const container = anchor.closest(
        "article, li, .person-result-card, .card-summary, [data-detail-link], [class*='result'], [class*='person']"
      ) || anchor.parentElement;
      const cardText = text(container);
      const heading = container?.querySelector("h1, h2, h3, [itemprop='name']");
      cards.push({ href, name: text(heading), text: cardText });
      seen.add(key);
      if (cards.length >= 50) break;
    }
    return cards;
  }

  async function expandContactSections() {
    const expandableText = /^(?:show more(?:\.\.\.)?|view more|load more|expand|more phones?)$/i;
    const targets = Array.from(document.querySelectorAll(
      'button, a[href], [role="button"]'
    )).filter((element) => {
      const label = text(element).replace(/\s+/g, " ").trim();
      if (!visible(element) || !expandableText.test(label)) return false;
      const context = text(element.closest(
        "section, article, li, [class*='phone'], [class*='contact'], [class*='email'], [class*='address']"
      ) || element.parentElement);
      if (
        !/\b(?:phone|contact|email|address)\b/i.test(context) ||
        /background report|people search report/i.test(context)
      ) {
        return false;
      }
      if (element.matches("a[href]")) {
        const href = element.getAttribute("href") || "";
        try {
          const target = new URL(href, location.href);
          if (target.pathname !== location.pathname) return false;
        } catch {
          return false;
        }
      }
      return true;
    }).slice(0, 12);
    let clicked = 0;
    for (const target of targets) {
      try {
        target.click();
        clicked += 1;
        await new Promise((resolve) => setTimeout(resolve, 180));
      } catch {
        // A provider control can disappear as another section expands.
      }
    }
    if (clicked) {
      await new Promise((resolve) => setTimeout(resolve, 700));
    }
    return clicked;
  }

  function sectionValues(pattern) {
    const output = [];
    for (const section of document.querySelectorAll("section, article, div, li")) {
      const heading = section.querySelector(":scope > h1, :scope > h2, :scope > h3, :scope > h4, :scope > div > h3");
      if (!heading || !pattern.test(text(heading))) continue;
      output.push(text(section));
      if (output.length >= 20) break;
    }
    return output;
  }

  function sectionContainers(pattern) {
    const containers = [];
    const headings = document.querySelectorAll(
      ".ls_contacts__title, main h2, main h3, main h4, article h2, article h3, article h4, main [role='heading'], article [role='heading'], main dt"
    );
    for (const heading of headings) {
      if (!pattern.test(text(heading))) continue;
      const candidates = [
        heading.closest("section, article, li, [class*='contact'], [class*='work'], [class*='education']"),
        heading.parentElement,
        heading.nextElementSibling,
      ];
      for (const candidate of candidates) {
        if (
          candidate &&
          candidate !== document.body &&
          candidate !== document.documentElement &&
          !containers.includes(candidate)
        ) {
          containers.push(candidate);
        }
      }
    }
    return containers;
  }

  function phoneValues() {
    const values = [];
    document.querySelectorAll(
      'a[href^="tel:"], [itemprop="telephone"], a[href*="/phone-search/"]'
    ).forEach((element) => {
      values.push(element.getAttribute("href") || "", text(element));
    });
    for (const value of sectionValues(/\b(?:current|previous)?\s*phone(?: number)?s?\b/i)) {
      values.push(...value.match(/\+?1?[\s.(-]*\d{3}[\s).-]*\d{3}[\s.-]*\d{4}\b/g) || []);
    }
    return values;
  }

  function emailValues() {
    const values = [];
    document.querySelectorAll('a[href^="mailto:"], [itemprop="email"]').forEach((element) => {
      values.push(element.getAttribute("href") || "", text(element));
    });
    for (const value of sectionValues(/\bemail(?: address)?(?:es)?\b/i)) {
      values.push(...value.match(/\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b/gi) || []);
    }
    return values;
  }

  function cleanAddress(value) {
    return String(value || "")
      .replace(/\([^)]*(?:\d{4}|present)[^)]*\)/gi, "")
      .replace(/\s+/g, " ")
      .trim();
  }

  function addressValues(kind) {
    const pattern = kind === "current"
      ? /\bcurrent address\b/i
      : /\b(?:previous|prior) addresses?\b/i;
    const values = [];
    for (const section of document.querySelectorAll("section, article, div")) {
      const heading = section.querySelector(":scope > h1, :scope > h2, :scope > h3, :scope > h4, :scope > div > h3");
      if (!heading || !pattern.test(text(heading))) continue;
      const elements = section.querySelectorAll(
        '[itemprop="address"], address, a[href*="/address/"], .ls_contacts__text, li'
      );
      elements.forEach((element) => {
        const value = cleanAddress(text(element));
        if (/\b[A-Z]{2}\s+\d{5}\b/i.test(value) || /,\s*[A-Z]{2}\b/i.test(value)) {
          values.push(value);
        }
      });
    }
    return values;
  }

  function historyLocation(lines) {
    return (lines || []).find((line) => (
      /,\s*[A-Z]{2}(?:\s*,?\s*\d{5}(?:-\d{4})?)?\b/.test(line)
    )) || "";
  }

  function workHistory() {
    const records = Array.from(document.querySelectorAll(
      ".workplace-expandable-list .relative-card.workplace, .relative-card.workplace"
    ));
    if (!records.length) {
      for (const container of sectionContainers(/\b(?:workplace|employment|occupation|work history)\b/i)) {
        const candidates = container.querySelectorAll(
          "li, .relative-card, [itemprop='worksFor']"
        );
        records.push(...(candidates.length ? candidates : [container]));
      }
    }
    return records.flatMap((record) => {
      const lines = Array.from(record.querySelectorAll("p, span"))
        .map(text)
        .filter(Boolean);
      const summary = text(record);
      if (!summary || summary.length > 1500 || /comprehensive view|background report/i.test(summary)) {
        return [];
      }
      const nonMeta = lines.filter((line) => !/^(?:current|previous)$/i.test(line));
      return [{
        title: nonMeta[0] || "",
        organization: nonMeta[1] || "",
        location: historyLocation(lines),
        current: lines.some((line) => /^current$/i.test(line)),
        summary: summary.slice(0, 500),
      }];
    }).slice(0, 20);
  }

  function educationHistory() {
    const records = [];
    for (const container of sectionContainers(/\beducation\b/i)) {
      const candidates = Array.from(container.querySelectorAll(
        "li, .relative-card, [itemprop='alumniOf']"
      ));
      for (const record of candidates.length ? candidates : [container]) {
        const summary = text(record);
        if (!summary || summary.length > 1000 || /comprehensive view|background report/i.test(summary)) {
          continue;
        }
        const lines = Array.from(record.querySelectorAll("p, span"))
          .map(text)
          .filter(Boolean);
        records.push({
          institution: lines[0] || summary.slice(0, 200),
          location: historyLocation(lines),
          summary: summary.slice(0, 500),
        });
      }
    }
    return records.slice(0, 20);
  }

  function profileName(expectedName) {
    const details = document.querySelector("#personDetails");
    const structured = [
      details?.dataset?.fn,
      details?.dataset?.mn,
      details?.dataset?.ln,
    ].filter(Boolean).join(" ");
    if (structured) return structured;
    const choices = [
      ...Array.from(document.querySelectorAll('[itemprop="name"], main h1, main h2, main h3')).map(text),
      document.title,
    ];
    const matching = choices.find((value) => parser.namesMatch(expectedName, value));
    if (matching) return matching.replace(/,\s*Age\b.*$/i, "").replace(/\s+Age\b.*$/i, "");
    const top = text(document.body).slice(0, 700);
    return top.toLowerCase().includes(String(expectedName || "").toLowerCase())
      ? expectedName
      : "";
  }

  function collectSnapshot(expectedName) {
    const bodyText = text(document.body);
    const cards = resultCards();
    const resultsPage = cards.length > 0 || /\b(?:we(?:'|’)ve found|we uncovered|found)\s+\d+\s+records?\b/i.test(bodyText);
    return {
      url: location.href,
      title: document.title,
      bodyText,
      pageKind: resultsPage ? "results" : "profile",
      hasCaptcha: hasCaptcha(),
      cards,
      profileName: profileName(expectedName),
      phones: phoneValues(),
      emails: emailValues(),
      currentAddresses: addressValues("current"),
      previousAddresses: addressValues("previous"),
      workHistory: workHistory(),
      education: educationHistory(),
    };
  }

  function clickProfileAction(profileUrl) {
    const expectedUrl = absoluteUrl(profileUrl);
    if (!expectedUrl) return { ok: false, clicked: false, error: "Invalid USPhoneBook profile URL." };
    function sameProfilePath(left, right) {
      try {
        const leftUrl = new URL(left);
        const rightUrl = new URL(right);
        return (
          leftUrl.hostname.toLowerCase() === rightUrl.hostname.toLowerCase() &&
          leftUrl.pathname.replace(/\/+$/, "") === rightUrl.pathname.replace(/\/+$/, "")
        );
      } catch {
        return false;
      }
    }
    const anchors = Array.from(document.querySelectorAll("a[href]"));
    const matching = anchors.filter((element) => {
      const href = absoluteUrl(
        element.getAttribute("href") ||
        element.href ||
        ""
      );
      return href && sameProfilePath(href, expectedUrl);
    });
    const matchingCard = Array.from(document.querySelectorAll(
      '[data-detail-link*="/find/person/"]'
    )).find((element) => {
      const href = absoluteUrl(element.getAttribute("data-detail-link") || "");
      return href && sameProfilePath(href, expectedUrl);
    });
    const action = (
      matching.find((element) => element.matches("a.ls_contacts-btn")) ||
      matching.find((element) => (
        /view\s+(?:full\s+)?address(?:\s*&\s*phone)?|view.*phone/i.test(text(element))
      )) ||
      matchingCard?.querySelector(
        'a.ls_contacts-btn, a[href*="/find/person/"]'
      ) ||
      matching[0]
    );
    if (!action) return { ok: false, clicked: false, error: "Profile action was not found on the page." };
    action.scrollIntoView({ block: "center", inline: "nearest" });
    action.removeAttribute("target");
    const actionUrl = absoluteUrl(action.getAttribute("href") || action.href || "");
    setTimeout(() => action.click(), 75);
    return {
      ok: true,
      clicked: true,
      actionUrl,
      actionText: text(action).slice(0, 200),
    };
  }

  function showManualVerificationBanner() {
    if (document.querySelector("#radixsol-uspb-verification")) return;
    const banner = document.createElement("div");
    banner.id = "radixsol-uspb-verification";
    banner.textContent = "Radixsol lookup is paused. Complete the USPhoneBook verification in this tab; lookup will resume automatically.";
    Object.assign(banner.style, {
      position: "fixed",
      inset: "0 0 auto 0",
      zIndex: "2147483647",
      padding: "12px 16px",
      color: "#fff",
      background: "#7132ed",
      font: "600 14px/1.4 system-ui, sans-serif",
      textAlign: "center",
      boxShadow: "0 2px 8px rgba(0,0,0,.25)",
    });
    document.documentElement.appendChild(banner);
  }

  globalThis.RadixsolUSPhoneBookContent = Object.freeze({ collectSnapshot });
  if (typeof chrome === "undefined" || !chrome.runtime?.onMessage) return;

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message?.type === "RADIXSOL_USPHONEBOOK_CLICK_PROFILE") {
      try {
        sendResponse(clickProfileAction(message.profileUrl || ""));
      } catch (error) {
        sendResponse({ ok: false, clicked: false, error: String(error?.message || error) });
      }
      return false;
    }
    if (message?.type !== "RADIXSOL_USPHONEBOOK_EXTRACT") return false;
    (async () => {
      let snapshot = collectSnapshot(message.expectedName || "");
      if (
        snapshot.pageKind === "profile" &&
        snapshot.profileName &&
        !snapshot.hasCaptcha
      ) {
        const clicked = await expandContactSections();
        if (clicked) snapshot = collectSnapshot(message.expectedName || "");
      }
      const response = parser.parseSnapshot(snapshot, {
        expectedName: message.expectedName || "",
        location: message.location || "",
        jobTitle: message.jobTitle || "",
      });
      if (response?.result?.status === "captcha_required") showManualVerificationBanner();
      sendResponse({ ok: true, ...response });
    })().catch((error) => {
      sendResponse({ ok: false, error: String(error?.message || error) });
    });
    return true;
  });
})();
