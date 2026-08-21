/**
 * MedHunt Background Service Worker
 * PDL Person Enrichment API integration
 *
 * IMPORTANT NOTES ON THE PDL API (these were wrong in v4.0.0):
 *  - Endpoint is /v5/person/enrich  (NOT /v5/person)
 *  - Location params are `locality` (city) and `region` (state). There is no
 *    `city` or `state` param. Sending them is ignored -> request looks like
 *    name-only -> HTTP 400.
 *  - Enrich requires first_name AND last_name PLUS at least one of:
 *    company, school, location, street_address, locality, region, country,
 *    postal_code, birth_date. Or a strong identifier: profile, email, phone,
 *    email_hash, lid, pdl_id.
 *  - Enrich is ONE-TO-ONE. `data` is a single object, not an array.
 *  - HTTP 404 = no match found (normal, not an error). 402 = out of credits.
 *  - Response includes `likelihood` (1-10). Use min_likelihood to control it.
 */

const CONFIG = {
  PDL_API_KEY: 'f47086602b33a0382b1f7d1605306d6f6c29793c2a5e524e5a6c15ceb58e3f1c',
  ENRICH_URL: 'https://api.peopledatalabs.com/v5/person/enrich',
  MIN_LIKELIHOOD: 6
};

console.log('MedHunt: background service worker loaded');

// ============================================================================
// NORMALIZATION
// ============================================================================

const STATE_MAP = {
  'alabama': 'alabama', 'al': 'alabama', 'alaska': 'alaska', 'ak': 'alaska',
  'arizona': 'arizona', 'az': 'arizona', 'arkansas': 'arkansas', 'ar': 'arkansas',
  'california': 'california', 'ca': 'california', 'colorado': 'colorado', 'co': 'colorado',
  'connecticut': 'connecticut', 'ct': 'connecticut', 'delaware': 'delaware', 'de': 'delaware',
  'florida': 'florida', 'fl': 'florida', 'georgia': 'georgia', 'ga': 'georgia',
  'hawaii': 'hawaii', 'hi': 'hawaii', 'idaho': 'idaho', 'id': 'idaho',
  'illinois': 'illinois', 'il': 'illinois', 'indiana': 'indiana', 'in': 'indiana',
  'iowa': 'iowa', 'ia': 'iowa', 'kansas': 'kansas', 'ks': 'kansas',
  'kentucky': 'kentucky', 'ky': 'kentucky', 'louisiana': 'louisiana', 'la': 'louisiana',
  'maine': 'maine', 'me': 'maine', 'maryland': 'maryland', 'md': 'maryland',
  'massachusetts': 'massachusetts', 'ma': 'massachusetts', 'michigan': 'michigan', 'mi': 'michigan',
  'minnesota': 'minnesota', 'mn': 'minnesota', 'mississippi': 'mississippi', 'ms': 'mississippi',
  'missouri': 'missouri', 'mo': 'missouri', 'montana': 'montana', 'mt': 'montana',
  'nebraska': 'nebraska', 'ne': 'nebraska', 'nevada': 'nevada', 'nv': 'nevada',
  'new hampshire': 'new hampshire', 'nh': 'new hampshire',
  'new jersey': 'new jersey', 'nj': 'new jersey',
  'new mexico': 'new mexico', 'nm': 'new mexico',
  'new york': 'new york', 'ny': 'new york',
  'north carolina': 'north carolina', 'nc': 'north carolina',
  'north dakota': 'north dakota', 'nd': 'north dakota',
  'ohio': 'ohio', 'oh': 'ohio', 'oklahoma': 'oklahoma', 'ok': 'oklahoma',
  'oregon': 'oregon', 'or': 'oregon', 'pennsylvania': 'pennsylvania', 'pa': 'pennsylvania',
  'rhode island': 'rhode island', 'ri': 'rhode island',
  'south carolina': 'south carolina', 'sc': 'south carolina',
  'south dakota': 'south dakota', 'sd': 'south dakota',
  'tennessee': 'tennessee', 'tn': 'tennessee', 'texas': 'texas', 'tx': 'texas',
  'utah': 'utah', 'ut': 'utah', 'vermont': 'vermont', 'vt': 'vermont',
  'virginia': 'virginia', 'va': 'virginia', 'washington': 'washington', 'wa': 'washington',
  'west virginia': 'west virginia', 'wv': 'west virginia',
  'wisconsin': 'wisconsin', 'wi': 'wisconsin', 'wyoming': 'wyoming', 'wy': 'wyoming',
  'district of columbia': 'district of columbia', 'dc': 'district of columbia'
};

const NICKNAMES = {
  robert: ['bob', 'rob', 'bobby'], james: ['jim', 'jimmy'],
  william: ['bill', 'will', 'billy'], richard: ['rick', 'rich', 'dick'],
  michael: ['mike', 'mick'], christopher: ['chris'], charles: ['charlie', 'chuck'],
  joseph: ['joe', 'joey'], thomas: ['tom', 'tommy'], daniel: ['dan', 'danny'],
  matthew: ['matt'], anthony: ['tony'], donald: ['don'], steven: ['steve'],
  stephen: ['steve'], andrew: ['andy', 'drew'], kenneth: ['ken', 'kenny'],
  joshua: ['josh'], edward: ['ed', 'eddie', 'ted'], benjamin: ['ben'],
  alexander: ['alex'], nicholas: ['nick'], david: ['dave'], jonathan: ['jon'],
  timothy: ['tim'], samuel: ['sam'], gregory: ['greg'], patrick: ['pat'],
  elizabeth: ['liz', 'beth', 'betsy', 'eliza'], jennifer: ['jen', 'jenny'],
  margaret: ['maggie', 'meg', 'peggy'], patricia: ['pat', 'patty', 'tricia'],
  katherine: ['kate', 'katie', 'kathy'], catherine: ['cate', 'cathy', 'kate'],
  deborah: ['deb', 'debbie'], barbara: ['barb'], susan: ['sue', 'susie'],
  jessica: ['jess'], rebecca: ['becky', 'becca'], stephanie: ['steph'],
  christina: ['chris', 'tina'], victoria: ['vicky'], samantha: ['sam'],
  alexandra: ['alex', 'sasha'], danielle: ['dani'], veronica: ['ronnie']
};

// Reverse map so "Bob" also tries "Robert"
const REVERSE_NICKNAMES = {};
for (const [full, nicks] of Object.entries(NICKNAMES)) {
  for (const n of nicks) {
    (REVERSE_NICKNAMES[n] = REVERSE_NICKNAMES[n] || []).push(full);
  }
}

function clean(v) {
  return (v || '').toString().trim().replace(/\s+/g, ' ');
}

function normalize(raw) {
  const firstNameRaw = clean(raw.firstName);
  const lastNameRaw = clean(raw.lastName);

  // Handle "John Michael Smith" arriving in firstName
  const firstParts = firstNameRaw.split(' ');
  const firstName = firstParts[0] || '';
  const middleName = firstParts.length > 1 ? firstParts.slice(1).join(' ') : '';

  const lower = firstName.toLowerCase();
  const variations = new Set([lower]);
  (NICKNAMES[lower] || []).forEach(v => variations.add(v));
  (REVERSE_NICKNAMES[lower] || []).forEach(v => variations.add(v));
  if (middleName) variations.add(middleName.split(' ')[0].toLowerCase());

  const regionRaw = clean(raw.state).toLowerCase();

  return {
    firstName,
    lastName: lastNameRaw,
    middleName,
    nameVariations: [...variations].filter(Boolean),
    locality: clean(raw.city).toLowerCase().replace(/\s+(city|town|county|township)$/, ''),
    region: STATE_MAP[regionRaw] || regionRaw,
    company: clean(raw.company),
    title: clean(raw.title),
    profile: clean(raw.profileUrl),   // LinkedIn/FB URL — highest-value input
    email: clean(raw.email),
    phone: clean(raw.phone)
  };
}

// ============================================================================
// PDL CLIENT
// ============================================================================

/**
 * Calls /v5/person/enrich.
 * Returns { status, person|null, likelihood, error }
 * status 200 = match, 404 = no match (normal), anything else = real error.
 */
async function pdlEnrich(params) {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== '') qs.append(k, v);
  }
  qs.append('min_likelihood', CONFIG.MIN_LIKELIHOOD);

  const url = `${CONFIG.ENRICH_URL}?${qs.toString()}`;
  console.log('MedHunt -> PDL', Object.fromEntries(qs));

  const res = await fetch(url, {
    method: 'GET',
    headers: {
      'X-Api-Key': CONFIG.PDL_API_KEY,
      'Accept': 'application/json'
    }
  });

  const text = await res.text();

  if (res.status === 404) {
    console.log('MedHunt <- PDL 404 (no match for these params)');
    return { status: 404, person: null };
  }

  if (!res.ok) {
    console.warn(`MedHunt <- PDL ${res.status}: ${text}`);
    return { status: res.status, person: null, error: text };
  }

  const body = JSON.parse(text);
  // Enrich returns a SINGLE object under `data`
  const person = body.data || null;
  console.log(`MedHunt <- PDL 200, likelihood=${body.likelihood}`, person);
  return { status: 200, person, likelihood: body.likelihood };
}

/**
 * Builds the ordered list of attempts. Each entry must satisfy PDL's rule:
 * first_name + last_name + at least one qualifying field, OR a strong
 * identifier (profile / email / phone).
 */
function buildAttempts(c) {
  const attempts = [];
  const base = { first_name: c.firstName, last_name: c.lastName };

  // 1. Strong identifiers first — these match near-perfectly and cost one call.
  if (c.profile) attempts.push({ label: 'profile_url', params: { profile: c.profile } });
  if (c.email)   attempts.push({ label: 'email',       params: { email: c.email } });
  if (c.phone)   attempts.push({ label: 'phone',       params: { phone: c.phone } });

  if (!c.firstName || !c.lastName) return attempts;

  // 2. Name + full location (tightest name-based attempt)
  if (c.locality && c.region) {
    attempts.push({ label: 'name_city_state', params: { ...base, locality: c.locality, region: c.region } });
  }

  // 3. Name + company — survives relocation entirely
  if (c.company) {
    attempts.push({ label: 'name_company', params: { ...base, company: c.company } });
  }

  // 4. Name + state only — survives a move within the state
  if (c.region) {
    attempts.push({ label: 'name_state', params: { ...base, region: c.region } });
  }

  // 5. Name variations (Bob/Robert, middle-name-as-first) against best anchor
  const anchor = c.company
    ? { company: c.company }
    : (c.region ? { region: c.region } : null);

  if (anchor) {
    for (const v of c.nameVariations) {
      if (v === c.firstName.toLowerCase()) continue;
      attempts.push({
        label: `variation:${v}`,
        params: { first_name: v, last_name: c.lastName, ...anchor }
      });
    }
  }

  // 6. Last resort: name + country. Weak, so require high likelihood.
  attempts.push({ label: 'name_country', params: { ...base, country: 'united states' } });

  return attempts;
}

async function findPerson(candidate) {
  const attempts = buildAttempts(candidate);

  if (attempts.length === 0) {
    return { matched: false, reason: 'Not enough data to query PDL (need name + city/state/company, or a profile URL)' };
  }

  for (const attempt of attempts) {
    try {
      const res = await pdlEnrich(attempt.params);

      if (res.status === 200 && res.person) {
        // Weakest attempt needs a stronger likelihood to be trusted
        const required = attempt.label === 'name_country' ? 8 : CONFIG.MIN_LIKELIHOOD;
        if ((res.likelihood || 0) >= required) {
          return {
            matched: true,
            person: res.person,
            likelihood: res.likelihood,
            strategy: attempt.label
          };
        }
        console.log(`MedHunt: likelihood ${res.likelihood} below ${required} for ${attempt.label}, continuing`);
        continue;
      }

      // Hard stops — retrying other params won't help
      if (res.status === 401) return { matched: false, fatal: true, reason: 'Invalid PDL API key (401)' };
      if (res.status === 402) return { matched: false, fatal: true, reason: 'PDL credits exhausted (402)' };
      if (res.status === 429) return { matched: false, fatal: true, reason: 'PDL rate limit hit (429) — slow down' };
      if (res.status === 400) {
        // Param combo invalid; log and move on rather than aborting
        console.warn(`MedHunt: 400 on ${attempt.label} — bad param combo, skipping`);
      }
    } catch (err) {
      console.error(`MedHunt: attempt ${attempt.label} threw`, err);
    }
  }

  return { matched: false, reason: 'No match in PDL for any parameter combination' };
}

// ============================================================================
// MESSAGE HANDLER
// ============================================================================

chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  // Stats are recorded here so they persist whether or not the popup is open.
  if (request.action === 'recordStat') {
    chrome.storage.local.get(['stats'], items => {
      const s = items.stats || { enriched: 0, failed: 0 };
      request.success ? s.enriched++ : s.failed++;
      chrome.storage.local.set({ stats: s });
    });
    return;
  }

  if (request.action !== 'enrichCandidate') return;

  (async () => {
    try {
      const candidate = normalize(request.candidate);
      console.log('MedHunt: normalized candidate', candidate);

      const result = await findPerson(candidate);

      if (result.matched) {
        const p = result.person;
        sendResponse({
          success: true,
          name: p.full_name,
          email: p.work_email || p.recommended_personal_email ||
                 (p.emails && p.emails[0] && p.emails[0].address) || null,
          phone: p.mobile_phone ||
                 (p.phone_numbers && p.phone_numbers[0]) || null,
          location: p.location_name,
          likelihood: result.likelihood,
          strategy: result.strategy
        });
      } else {
        sendResponse({ success: false, error: result.reason });
      }
    } catch (err) {
      console.error('MedHunt: enrichment failed', err);
      sendResponse({ success: false, error: err.message });
    }
  })();

  return true; // keep the message channel open for the async response
});
