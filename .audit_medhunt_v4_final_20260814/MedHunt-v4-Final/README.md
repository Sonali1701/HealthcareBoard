# MedHunt v4.1.0 — what changed and why

## The 400 error: two bugs in v4.0.0, both mine

**1. Wrong endpoint.** The code called `https://api.peopledatalabs.com/v5/person`.
That endpoint does not exist. The Person Enrichment endpoint is
`https://api.peopledatalabs.com/v5/person/enrich`.

**2. Wrong parameter names.** The code sent `state=CA` (and `city=...`).
PDL has no `state` or `city` parameter — they are `region` and `locality`.
Unrecognised params are ignored, so PDL saw a request carrying only
`first_name` + `last_name`.

PDL requires `first_name` **and** `last_name` **plus at least one** of:
`company`, `school`, `location`, `street_address`, `locality`, `region`,
`country`, `postal_code`, `birth_date` — or a strong identifier
(`profile`, `email`, `phone`, `email_hash`, `lid`, `pdl_id`).

Name-only fails that rule, so every single call returned **400 Bad Request**.
Your API key was never the problem.

**3. Wrong response shape.** Enrichment is one-to-one: `data` is a single
object, not an array. The old code did `data.data || []` then `results[0]`,
which would have broken even if the request had succeeded. Also, **404 means
"no match found"** — a normal outcome, not an error.

## The button never appearing: third bug

Your console screenshot showed `bundle_indeed_resume.js` and **no MedHunt log
lines at all** — the content script wasn't running. The old manifest matched
only `https://www.indeed.com/*`, but Indeed's resume/employer views sit on
other subdomains. Matches are now `*://*.indeed.com/*` and equivalents, and the
button re-injects itself on SPA navigation.

## Correction on the numbers I gave you

The "20% → 90%", the per-strategy hit rates (40% / 35% / 15% / 7% / 3%), and
the confidence-distribution table in the earlier docs were **invented**. I had
no access to your original extension and ran no measurements. Please discard
them. The only honest statement is: the old code returned 400 on every call, so
its name-based PDL yield was 0%. What the fixed version achieves has to be
measured against your own candidate list.

## What actually drives match rate

Ordered by real impact, based on how the PDL enrich API works:

1. **A profile URL.** If you can scrape a LinkedIn URL, pass it as `profile`.
   PDL matches on it near-exactly and it needs no other field. This is worth
   more than every fuzzy-name trick combined. The extension now looks for one.
2. **An email or phone already on the page.** Same idea — strong identifier,
   one call.
3. **Company name.** Survives relocation entirely, which name+location does not.
4. **Location.** Useful, but the weakest of the qualifying anchors.
5. **Nickname / middle-name variations.** Real but marginal. They only help
   when a strong anchor (company or region) is already present.

The `min_likelihood` parameter (1–10) is how you trade coverage against
accuracy — not a scoring formula written in the extension. It's set to 6 in
`CONFIG`; raise it for outreach-grade data, lower it for research.

## Attempt order in v4.1.0

Each attempt is a separate PDL call and each costs a credit **only on a match**.

| # | Attempt | Params sent |
|---|---------|-------------|
| 1 | `profile_url` | `profile` |
| 2 | `email` | `email` |
| 3 | `phone` | `phone` |
| 4 | `name_city_state` | `first_name` + `last_name` + `locality` + `region` |
| 5 | `name_company` | `first_name` + `last_name` + `company` |
| 6 | `name_state` | `first_name` + `last_name` + `region` |
| 7 | `variation:*` | nickname/middle-name + `last_name` + company or region |
| 8 | `name_country` | `first_name` + `last_name` + `country` (requires likelihood ≥ 8) |

It stops at the first match meeting the likelihood floor.

## Install

1. Put `manifest.json`, `background.js`, `content.js`, `popup.html`,
   `popup.js` in one folder.
2. `chrome://extensions/` → Developer mode on → **Load unpacked** → pick the folder.
3. Click the extension icon → **Test API**. This now makes a real, correctly
   formed enrich call and reports the exact status code.

## Where the logs are

This tripped you up last time. There are **two separate consoles**:

- **Content script logs** (`MedHunt: content script loaded`, extracted data) —
  F12 on the job board page.
- **Background logs** (the actual PDL request and response) —
  `chrome://extensions/` → MedHunt → click **"service worker"**.

The PDL request/response you need for debugging is in the *second* one.

## Still to verify on your side

I could not test against live pages or a live API key, so two things remain
unverified and you should expect to adjust them:

- **The CSS selectors in `content.js` are guesses.** Job board markup changes
  often and differs per view. The panel now prints which fields it scraped —
  if it says "Couldn't read a name", the selectors need updating for that page,
  and no amount of PDL logic will help until they do.
- **Field names on the PDL response.** `work_email`, `mobile_phone`,
  `recommended_personal_email` are what the docs describe; confirm against a
  real 200 response in the service worker console and adjust the mapping at the
  bottom of `background.js` if needed.

## Security

The API key is hardcoded in `background.js` and `popup.js` for testing. Anyone
you share this folder with gets your key. Before it goes to your team, move it
into `chrome.storage` via the popup and rotate the current key — it has now
been shared in a chat transcript.
