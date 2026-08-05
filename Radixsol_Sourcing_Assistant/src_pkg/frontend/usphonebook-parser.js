(function exposeUSPhoneBookParser(root, factory) {
  const parser = factory();
  if (typeof module === "object" && module.exports) module.exports = parser;
  root.RadixsolUSPhoneBookParser = parser;
})(typeof globalThis !== "undefined" ? globalThis : this, function parserFactory() {
  "use strict";

  const STATE_NAMES = {
    AL: "alabama", AK: "alaska", AZ: "arizona", AR: "arkansas",
    CA: "california", CO: "colorado", CT: "connecticut", DE: "delaware",
    DC: "district-of-columbia", FL: "florida", GA: "georgia", HI: "hawaii",
    ID: "idaho", IL: "illinois", IN: "indiana", IA: "iowa", KS: "kansas",
    KY: "kentucky", LA: "louisiana", ME: "maine", MD: "maryland",
    MA: "massachusetts", MI: "michigan", MN: "minnesota", MS: "mississippi",
    MO: "missouri", MT: "montana", NE: "nebraska", NV: "nevada",
    NH: "new-hampshire", NJ: "new-jersey", NM: "new-mexico", NY: "new-york",
    NC: "north-carolina", ND: "north-dakota", OH: "ohio", OK: "oklahoma",
    OR: "oregon", PA: "pennsylvania", RI: "rhode-island",
    SC: "south-carolina", SD: "south-dakota", TN: "tennessee", TX: "texas",
    UT: "utah", VT: "vermont", VA: "virginia", WA: "washington",
    WV: "west-virginia", WI: "wisconsin", WY: "wyoming",
  };
  const NAME_PREFIXES = new Set(["dr", "mr", "mrs", "ms", "miss"]);
  const NAME_SUFFIXES = new Set([
    "jr", "sr", "ii", "iii", "iv", "v", "md", "do", "phd", "rn", "np",
    "pa", "dds", "dmd", "lpn", "lvn", "cna", "bsn", "msn",
  ]);
  const TRAILING_ROLE_PHRASES = [
    ["registered", "nurse"],
    ["licensed", "practical", "nurse"],
    ["licensed", "vocational", "nurse"],
    ["certified", "nursing", "assistant"],
    ["radiologic", "technologist"],
    ["radiology", "technologist"],
    ["mri", "technologist"],
    ["x", "ray", "technologist"],
  ];
  const BLOCK_TERMS = [
    "just a moment",
    "performing security verification",
    "verify you are human",
    "complete the captcha",
    "security challenge",
    "enable javascript and cookies to continue",
    "access denied",
  ];

  function clean(value) {
    return String(value || "").replace(/\s+/g, " ").trim();
  }

  function normalize(value) {
    return clean(value)
      .normalize("NFKD")
      .replace(/[\u0300-\u036f]/g, "")
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, " ")
      .trim();
  }

  function slug(value) {
    return normalize(value).replace(/\s+/g, "-");
  }

  function nameParts(value) {
    const parts = normalize(value).split(" ").filter(Boolean);
    while (parts.length > 1 && NAME_PREFIXES.has(parts[0])) parts.shift();
    for (const phrase of TRAILING_ROLE_PHRASES) {
      if (
        parts.length > phrase.length &&
        phrase.every((token, index) => parts[parts.length - phrase.length + index] === token)
      ) {
        parts.splice(parts.length - phrase.length, phrase.length);
        break;
      }
    }
    while (parts.length > 1 && NAME_SUFFIXES.has(parts.at(-1))) parts.pop();
    return {
      parts,
      first: parts[0] || "",
      last: parts.at(-1) || "",
    };
  }

  function namesMatch(expectedName, candidateName) {
    const expected = nameParts(expectedName);
    const candidate = nameParts(candidateName);
    return Boolean(
      expected.first &&
      expected.last &&
      expected.first === candidate.first &&
      expected.last === candidate.last
    );
  }

  function providerSearchName(value) {
    const identity = nameParts(value);
    if (!identity.first) return "";
    return identity.first === identity.last
      ? identity.first
      : `${identity.first} ${identity.last}`;
  }

  function stateParts(value) {
    const normalized = normalize(value);
    for (const [code, stateSlug] of Object.entries(STATE_NAMES)) {
      const stateName = stateSlug.replace(/-/g, " ");
      if (
        new RegExp(`(^| )${code.toLowerCase()}($| )`).test(normalized) ||
        normalized.includes(stateName)
      ) {
        return { code, name: stateName, slug: stateSlug };
      }
    }
    return { code: "", name: "", slug: "" };
  }

  function locationParts(value) {
    const raw = clean(value).replace(/\b\d{5}(?:-\d{4})?\b/g, "").trim();
    const state = stateParts(raw);
    const beforeState = raw.split(",")[0] || "";
    let city = normalize(beforeState);
    if (state.name && city === state.name) city = "";
    return { city, ...state };
  }

  function locationEvidence(location, candidateText) {
    const expected = locationParts(location);
    const candidate = normalize(candidateText);
    if (!expected.city && !expected.code) {
      return { score: 0.75, relevance: "name", cityMatch: false, stateMatch: false };
    }
    const stateMatch = Boolean(
      expected.code &&
      (
        new RegExp(`(^| )${expected.code.toLowerCase()}($| )`).test(candidate) ||
        candidate.includes(expected.name)
      )
    );
    const cityMatch = Boolean(expected.city && candidate.includes(expected.city));
    const score = cityMatch && stateMatch ? 1 : stateMatch ? 0.85 : 0;
    return {
      score,
      relevance: score === 1 ? "city" : score === 0.85 ? "state" : "unverified",
      cityMatch,
      stateMatch,
    };
  }

  function buildProviderUrls(name, location) {
    const normalizedName = providerSearchName(name);
    if (!nameParts(normalizedName).last) throw new Error("A first and last name are required.");
    const base = `https://www.usphonebook.com/${slug(normalizedName)}`;
    const requested = locationParts(location);
    const urls = [base];
    if (requested.slug) urls.push(`${base}/${requested.slug}`);
    if (requested.slug && requested.city) {
      urls.push(`${base}/${requested.slug}/${slug(requested.city)}`);
    }
    return [...new Set(urls)];
  }

  function normalizePhone(value) {
    const raw = String(value || "");
    if (/[•*xX]{2,}/.test(raw)) return "";
    let digits = raw.replace(/\D/g, "");
    if (digits.length === 11 && digits.startsWith("1")) digits = digits.slice(1);
    if (digits.length !== 10 || /^0+$/.test(digits)) return "";
    return `(${digits.slice(0, 3)}) ${digits.slice(3, 6)}-${digits.slice(6)}`;
  }

  function normalizeEmail(value) {
    const candidate = clean(value)
      .replace(/^mailto:/i, "")
      .split("?")[0]
      .toLowerCase();
    return /^[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)+$/.test(candidate)
      ? candidate
      : "";
  }

  function jobTitleEvidence(jobTitle, candidateText) {
    function roleTokens(value) {
      const expanded = normalize(value)
        .replace(/\brn\b/g, " registered nurse ")
        .replace(/\blpn\b/g, " licensed practical nurse ")
        .replace(/\blvn\b/g, " licensed vocational nurse ")
        .replace(/\bcna\b/g, " certified nursing assistant ");
      return new Set(
        expanded.split(" ").filter((token) => (
          token.length > 2 &&
          !["and", "the", "current", "previous", "professional"].includes(token)
        ))
      );
    }
    const expected = roleTokens(jobTitle);
    if (!expected.size) return 0;
    const candidate = roleTokens(candidateText);
    const matches = [...expected].filter((token) => candidate.has(token)).length;
    return Math.round((matches / expected.size) * 100) / 100;
  }

  function unique(values, transform = clean, limit = 20) {
    const output = [];
    const seen = new Set();
    for (const value of values || []) {
      const normalized = transform(value);
      const key = normalize(normalized);
      if (!normalized || !key || seen.has(key)) continue;
      seen.add(key);
      output.push(normalized);
      if (output.length >= limit) break;
    }
    return output;
  }

  function blockingStatus(snapshot) {
    const text = normalize(`${snapshot?.title || ""} ${snapshot?.bodyText || ""}`);
    if (snapshot?.hasCaptcha || BLOCK_TERMS.some((term) => text.includes(normalize(term)))) {
      return text.includes("access denied") ? "blocked" : "captcha_required";
    }
    return "";
  }

  function cardName(card) {
    const supplied = clean(card?.name);
    const firstLine = supplied || (
      String(card?.text || "").split(/\r?\n/).map(clean).find(Boolean) || ""
    );
    return clean(firstLine.replace(/,\s*Age\b.*$/i, "").replace(/\s+Age\b.*$/i, ""));
  }

  function chooseProfile(snapshot, expectedName, location, jobTitle = "") {
    const block = blockingStatus(snapshot);
    if (block) return { action: "result", result: result(block, snapshot.url) };
    const nameMatches = (snapshot.cards || [])
      .map((card) => {
        const name = cardName(card);
        const evidence = locationEvidence(location, card.text || "");
        const titleScore = jobTitleEvidence(jobTitle, card.text || "");
        return {
          ...card,
          name,
          evidence,
          titleScore,
          rank: (evidence.score * 100) + (titleScore * 20),
        };
      })
      .filter((card) => namesMatch(expectedName, card.name) && card.href);

    if (!nameMatches.length) {
      return {
        action: "result",
        result: {
          ...result("no_match", snapshot.url),
          confirmed_no_match: Boolean(snapshot.cards?.length || /\bfound \d+ records?\b/i.test(snapshot.bodyText || "")),
          message: "No exact first/last-name profile was found.",
        },
      };
    }

    const requestedLocation = locationParts(location);
    const requiresLocation = Boolean(requestedLocation.city || requestedLocation.code);
    const exactLocationMatches = requiresLocation
      ? nameMatches.filter((card) => (
          requestedLocation.city && requestedLocation.code
            ? card.evidence.cityMatch && card.evidence.stateMatch
            : requestedLocation.city
              ? card.evidence.cityMatch
              : card.evidence.stateMatch
        ))
      : nameMatches;

    if (!exactLocationMatches.length) {
      return {
        action: "result",
        result: {
          ...result("no_match", snapshot.url),
          confirmed_no_match: true,
          message: `${nameMatches.length} exact-name profile${nameMatches.length === 1 ? "" : "s"} found, but none matched ${clean(location) || "the requested location"}.`,
        },
      };
    }

    if (exactLocationMatches.length === 1) {
      return { action: "follow_profile", profileUrl: exactLocationMatches[0].href };
    }

    const highestJobScore = Math.max(
      0,
      ...exactLocationMatches.map((card) => card.titleScore)
    );
    const jobMatches = highestJobScore > 0
      ? exactLocationMatches.filter((card) => card.titleScore === highestJobScore)
      : exactLocationMatches;
    if (highestJobScore > 0 && jobMatches.length === 1) {
      return { action: "follow_profile", profileUrl: jobMatches[0].href };
    }

    const profiles = jobMatches.slice(0, 10).map((card) => ({
      profile_url: card.href,
      matched_name: card.name,
      card_text: clean(card.text).slice(0, 1000),
      card_location_score: card.evidence.score,
      card_location_relevance: card.evidence.relevance,
      card_job_score: card.titleScore,
    }));
    return {
      action: "result",
      result: {
        ...result("multiple_matches", snapshot.url),
        profiles,
        confirmed_no_match: false,
        message: `${exactLocationMatches.length} profiles matched the exact name and location and require job-title review.`,
      },
    };
  }

  function result(status, profileUrl = "", extra = {}) {
    return {
      source: "usphonebook",
      status,
      matched_name: null,
      phones: [],
      emails: [],
      addresses: [],
      confidence: 0,
      profile_url: profileUrl || "",
      ...extra,
    };
  }

  function parseProfile(snapshot, expectedName, location, jobTitle = "") {
    const block = blockingStatus(snapshot);
    if (block) {
      return result(block, snapshot.url, {
        message: block === "blocked"
          ? "USPhoneBook blocked this browser session."
          : "USPhoneBook requires manual browser verification.",
      });
    }

    const matchedName = clean(snapshot.profileName);
    if (!matchedName || !namesMatch(expectedName, matchedName)) {
      return result("no_match", snapshot.url, {
        confirmed_no_match: Boolean(matchedName),
        message: matchedName
          ? `${matchedName} does not match ${clean(expectedName)}.`
          : "The profile name could not be verified.",
      });
    }

    const currentAddresses = unique(snapshot.currentAddresses || []);
    const previousAddresses = unique(snapshot.previousAddresses || []);
    const addresses = unique([...currentAddresses, ...previousAddresses]);
    const locationMatch = locationEvidence(location, currentAddresses.join(" "));
    const phones = unique(snapshot.phones || [], normalizePhone);
    const emails = unique(snapshot.emails || [], normalizeEmail);
    const workHistory = Array.isArray(snapshot.workHistory) ? snapshot.workHistory.slice(0, 20) : [];
    const education = Array.isArray(snapshot.education) ? snapshot.education.slice(0, 20) : [];
    const historyText = [...workHistory, ...education]
      .map((item) => clean(item?.summary || `${item?.title || ""} ${item?.organization || ""} ${item?.institution || ""} ${item?.location || ""}`))
      .join(" ");
    const reviewProfile = {
      profile_url: snapshot.url || "",
      matched_name: matchedName,
      phones,
      emails,
      current_addresses: currentAddresses,
      previous_addresses: previousAddresses,
      addresses,
      work_history: workHistory,
      education,
      current_location_score: locationMatch.score,
      job_score: jobTitleEvidence(jobTitle, historyText),
    };
    const hasRequestedLocation = Boolean(locationParts(location).city || locationParts(location).code);
    const locationVerified = !hasRequestedLocation || locationMatch.score >= 0.85;
    const confidence = Math.min(1, Math.round(
      (0.65 + (locationVerified ? 0.25 : 0) + ((phones.length || emails.length) ? 0.1 : 0)) * 100
    ) / 100);

    if (!locationVerified) {
      return result("location_unverified", snapshot.url, {
        matched_name: matchedName,
        confidence: 0.65,
        review_profile: reviewProfile,
        confirmed_no_match: false,
        message: "The name matched, but the current city/state could not be verified. Contacts were withheld.",
      });
    }
    if (!phones.length && !emails.length) {
      return result("contact_incomplete", snapshot.url, {
        matched_name: matchedName,
        addresses,
        confidence,
        review_profile: reviewProfile,
        confirmed_no_match: false,
        message: "The identity matched, but no complete phone number or email was displayed.",
      });
    }
    return result("success", snapshot.url, {
      matched_name: matchedName,
      phones,
      emails,
      addresses,
      current_address: currentAddresses[0] || null,
      previous_addresses: previousAddresses,
      confidence,
      review_profile: reviewProfile,
      confirmed_no_match: false,
      message: null,
    });
  }

  function reviewedProfileEvidence(profile, location, jobTitle) {
    const current = locationEvidence(location, (profile.current_addresses || []).join(" "));
    const historyLocations = [
      ...(profile.previous_addresses || []),
      ...(profile.work_history || []).map((item) => item?.location || item?.summary || ""),
      ...(profile.education || []).map((item) => item?.location || item?.summary || ""),
    ].join(" ");
    const historical = locationEvidence(location, historyLocations);
    const workText = [
      ...(profile.work_history || []).map((item) => (
        item?.summary || `${item?.title || ""} ${item?.organization || ""}`
      )),
      ...(profile.education || []).map((item) => item?.summary || item?.institution || ""),
    ].join(" ");
    const title = Math.max(
      Number(profile.job_score) || 0,
      jobTitleEvidence(jobTitle, workText)
    );
    const cardLocation = Number(profile.card_location_score) || 0;
    const cardTitle = Number(profile.card_job_score) || 0;
    const hasContact = Boolean((profile.phones || []).length || (profile.emails || []).length);
    const rank = (
      (current.score * 100) +
      (historical.score * 55) +
      (title * 35) +
      (cardLocation * 15) +
      (cardTitle * 10) +
      (hasContact ? 5 : 0)
    );
    const qualified = hasContact && (
      current.score >= 0.85 ||
      cardLocation >= 0.85 ||
      (historical.score >= 0.85 && title >= 0.5)
    );
    return { current, historical, title, cardLocation, cardTitle, hasContact, rank, qualified };
  }

  function selectReviewedProfile(profiles, location, jobTitle) {
    const ranked = (profiles || []).map((profile) => ({
      profile,
      evidence: reviewedProfileEvidence(profile, location, jobTitle),
    }));
    const requested = locationParts(location);
    const exactLocation = ranked.filter((item) => {
      const current = item.evidence.current;
      const cardScore = item.evidence.cardLocation;
      if (requested.city && requested.code) {
        return (current.cityMatch && current.stateMatch) || cardScore === 1;
      }
      if (requested.city) return current.cityMatch || cardScore === 1;
      if (requested.code) return current.stateMatch || cardScore >= 0.85;
      return true;
    });
    if (!exactLocation.length) return { selected: null, ranked, reason: "location" };
    if (exactLocation.length === 1) {
      const only = exactLocation[0];
      return only.evidence.hasContact
        ? { selected: only, ranked, reason: "single_location_match" }
        : { selected: null, ranked, reason: "contact_incomplete" };
    }

    const highestTitleScore = Math.max(
      0,
      ...exactLocation.map((item) => Math.max(
        item.evidence.title,
        item.evidence.cardTitle
      ))
    );
    if (highestTitleScore <= 0) {
      return { selected: null, ranked, reason: "job_title_missing" };
    }
    const titleMatches = exactLocation.filter((item) => (
      Math.max(item.evidence.title, item.evidence.cardTitle) === highestTitleScore
    ));
    if (titleMatches.length !== 1) {
      return { selected: null, ranked, reason: "job_title_tie" };
    }
    const selected = titleMatches[0];
    return selected.evidence.hasContact
      ? { selected, ranked, reason: "job_title" }
      : { selected: null, ranked, reason: "contact_incomplete" };
  }

  function resultFromReviewedProfile(selection) {
    const profile = selection?.profile;
    const evidence = selection?.evidence;
    if (!profile || !evidence?.qualified) return null;
    const confidence = Math.min(1, Math.round((
      0.65 +
      (Math.max(evidence.current.score, evidence.historical.score) * 0.2) +
      (evidence.title * 0.1) +
      0.05
    ) * 100) / 100);
    return result("success", profile.profile_url, {
      matched_name: profile.matched_name,
      phones: profile.phones || [],
      emails: profile.emails || [],
      addresses: profile.addresses || [],
      current_address: profile.current_addresses?.[0] || null,
      previous_addresses: profile.previous_addresses || [],
      confidence,
      match_evidence: {
        current_location: evidence.current,
        historical_location: evidence.historical,
        job_title_score: evidence.title,
        reviewed_profiles: true,
      },
      confirmed_no_match: false,
      message: null,
    });
  }

  function parseSnapshot(snapshot, options) {
    if (snapshot?.pageKind === "results") {
      return chooseProfile(
        snapshot,
        options.expectedName,
        options.location,
        options.jobTitle || ""
      );
    }
    return {
      action: "result",
      result: parseProfile(
        snapshot || {},
        options.expectedName,
        options.location,
        options.jobTitle || ""
      ),
    };
  }

  return Object.freeze({
    buildProviderUrls,
    clean,
    jobTitleEvidence,
    locationEvidence,
    locationParts,
    namesMatch,
    normalizeEmail,
    normalizePhone,
    parseProfile,
    parseSnapshot,
    providerSearchName,
    resultFromReviewedProfile,
    selectReviewedProfile,
  });
});
