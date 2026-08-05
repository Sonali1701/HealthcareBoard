# Radixsol Sourcing Assistant

A Chrome/Edge side-panel extension backed by FastAPI and Neon/PostgreSQL. It scans
candidate cards in an authorized Indeed Smart Sourcing result page,
automatically saves captured profiles, supports reviewed contact
enrichment, ranks candidates against jobs, drafts compliant outreach, and
tracks the recruiting pipeline. SQLite remains available as the local test and
offline fallback.

Candidate data and API credentials are handled by the Python service. Secrets
are never placed in the extension.

## Features

- Guided scan and display of up to 100 candidate cards loaded in Indeed results
- Scan progress, select-all/individual selection, and sequential lookup progress
- Automatic batch persistence of every detected result to SQLite
- Deduplication by Indeed candidate ID, with name and location fallback
- Curately-style match/no-match results and CSV export
- Selection and reviewed licensed contact enrichment
- Evidence-based identity confidence kept separate from email/phone verification
- Optional Gemini identity second opinion, disabled unless explicitly enabled
- Indeed PDF Blob/fetch/XHR capture with visible download and private R2 storage
- One-click capture and review of a complete open Indeed profile
- Manual, pasted-text, or CSV intake
- Licensed Enformion/Endato enrichment
- Deterministic demo enrichment when credentials are not configured
- Job ranking using captured profile text
- Human-reviewed outreach drafts with no automatic sending
- Pipeline and do-not-contact management

The extension reads the result cards currently loaded in the page. It does not
advance to another results page automatically. Use it only with an account and
candidate information you are authorized to access.

## 1. Environment

The service automatically loads `.env` from the workspace root. The supplied
`.gitignore` excludes this file from source control.

It then loads `.env.local` with higher priority. The current local override
selects resources dedicated to this application:

```text
DATABASE_BACKEND=postgresql
DATABASE_NAME=radixsol_sourcing
STORAGE_ENABLED=1
S3_BUCKET=radixsol-sourcing-resumes
```

Important settings:

```text
DATABASE_URL=<pooled Neon PostgreSQL connection>
ENFORMION_AP_NAME=<licensed access profile>
ENFORMION_AP_PASSWORD=<licensed password>
GEMINI_API_KEY=<optional>
AI_MATCH_ENABLED=0
IDENTITY_MATCH_THRESHOLD=0.72
VERIFY_EMAILS=0
NEVERBOUNCE_API_KEY=<optional>
VERIFY_PHONES=0
TWILIO_ACCOUNT_SID=<optional>
TWILIO_AUTH_TOKEN=<optional>
```

When `DATABASE_BACKEND=sqlite`, the application ignores `DATABASE_URL` even if
the operating system or `.env` contains one. To enable Neon later, replace the
local override only after creating the dedicated Radixsol database.
When `STORAGE_ENABLED=1`, downloaded resume PDFs are uploaded to the configured
private Cloudflare R2 bucket. The database stores the R2 object key, checksum,
MIME type, and size alongside the candidate record. Keep it disabled until the
bucket name and `DATABASE_URL` both point to resources dedicated to Radixsol.

## 2. Start the backend

From `src_pkg`:

```powershell
python -m pip install -r requirements.txt
python -m uvicorn api:app --host 127.0.0.1 --port 8090
```

The web interface is available at
[http://127.0.0.1:8090](http://127.0.0.1:8090).

## 3. Load the extension

1. Open `chrome://extensions` or `edge://extensions`.
2. Enable **Developer mode**.
3. Choose **Load unpacked**.
4. Select `src_pkg/frontend`.
5. Pin Radixsol Sourcing Assistant and open its side panel.

After updating the source, use **Reload** on the extensions page and reload the
Indeed tab.

## 4. Save displayed Indeed candidates

1. Sign in to your authorized Indeed employer/Smart Sourcing account.
2. Run a candidate search and wait for its result cards to load.
3. Open **Indeed Profiles** in the Radixsol side panel.
4. The panel lists every detected card with name, location, and headline.
5. All captured profiles are selected by default and automatically batch-saved.
   The status reports:

   ```text
   50 saved to local database · 48 new · 2 already present
   ```

6. The extension may scroll the result list while scanning and restores the
   original scroll position when finished. Use the refresh icon to rescan.

Automatic saving stores visible profile metadata and text but does not call
USPhoneBook or Enformion. This prevents an ordinary Indeed search from silently
opening people-search pages. While the **Indeed Profiles** view remains open,
changes to the loaded result cards trigger a debounced rescan and database
upsert automatically.

## 5. Enrich selected candidates

1. Select individual profiles or use **Select all**.
2. Select **Look up candidates**.
3. Confirm a multi-candidate batch. Searches run sequentially and can take
   several minutes.

The extension opens USPhoneBook in a temporary background tab and searches by
name. For a single exact-name result, it clicks the provider's **View Full
Address & Phone** action before extracting the displayed profile. Search cards
that do not match both the candidate's exact first/last name and requested
city/state are excluded before any profile is opened. If several exact
name/location results remain, the extension reviews only those profiles and
uses the Indeed job title against their work history as the tie-breaker. A
missing or tied job-title match remains ambiguous. The candidate row reports
stages such as `Reviewing profile 2 of 2` instead of showing an
undifferentiated search state.

On a person page, only genuine in-section controls such as **Show more** or
**More phones** may be expanded. The global Phone/Name/Address navigation and
background-report promotions are never clicked, and ambiguous review does not
return to the provider landing page between shortlisted profiles.

Complete publicly displayed phones and emails from the selected profile are
sent to the local backend, checked against do-not-contact rules, and stored in
SQLite. Matches appear progressively and the final view provides match/no-match
filters plus CSV export.

Successful searches are cached for six hours; confirmed no-match results for
30 minutes. Provider navigations are serialized and spaced by at least eight
seconds. If USPhoneBook shows a browser challenge, the extension focuses that
tab and waits up to two minutes for the user to complete it. The extension does
not bypass or automatically solve CAPTCHAs.

## How contact verification works

Contact lookup and verification are different operations:

1. Indeed provides the visible candidate identity signals that the employer is
   authorized to view: name, location, role, employers, education, and skills.
2. The side-panel Lookup button searches USPhoneBook. A contact is accepted
   only when the first/last name and current city/state evidence match.
   Ambiguous or location-unverified profiles are not attributed to the
   candidate.
3. Radixsol checks email/phone format, applies the do-not-contact list, and
   stores the directory URL, evidence, and identity confidence.
4. When `AI_MATCH_ENABLED=1` and a Gemini key is configured, Gemini can provide
   a bounded second opinion on whether the two identity records refer to the
   same person. It never creates email addresses or phone numbers.
5. A directory identity match is not the same as email deliverability, active
   phone-line status, or phone ownership. Set `VERIFY_EMAILS=1` with a
   NeverBounce key for deliverability results. Set `VERIFY_PHONES=1` with
   Twilio credentials for basic number-range validation. Basic Twilio Lookup
   still does not prove ownership; ownership requires a separately enabled
   Identity Match product and appropriate legal basis.

AI matching is opt-in because candidate identity data is sent to the configured
AI provider. Without that opt-in, deterministic evidence scoring remains active.

For a more complete profile, select **Open**, then use **Capture Indeed profile**
after the full Insights/resume view appears.

## Matched resume capture

After USPhoneBook returns both a phone number and an email address, the
extension opens that exact candidate in Indeed and trusted-clicks the normal
**Download resume** action. A MAIN-world hook captures resume bytes produced by
Blob, fetch, or XHR, with the browser download URL as a fallback. This avoids
depending only on Chrome's download-complete event. The PDF is uploaded to the
private R2 bucket and its metadata is saved in Neon. If Indeed did not create a
visible file itself, the extension explicitly saves the captured PDF under
`Downloads/RadixsolResumes`. Profiles with only a phone or only an email are
saved, but their resume is not captured automatically.

SQLite is not encrypted by this project. Protect the workstation and database,
apply an appropriate retention period, and delete candidate data when it is no
longer needed.

## Test

```powershell
python -m pytest -q
```

The tests cover SQLite fallback, batch import, deduplication, enrichment,
identity evidence, suppression, ranking, outreach, API behavior, extension
CORS, and Manifest V3 packaging. A browser smoke test and its 50-candidate
fixture are available in `tests/browser_smoke.py` and `tests/fixtures`.

## Project layout

```text
sourcing/               Neon/PostgreSQL and SQLite backend modules
api.py                  FastAPI routes and extension CORS
frontend/
  manifest.json         Manifest V3 extension definition
  background.js         Opens the side panel
  indeed-content.js     Reads loaded Indeed results and open profiles
  index.html            Extension/web shell
  app.js                Side-panel client and automatic database sync
  styles.css            Desktop and side-panel layouts
tests/                  Demo-mode backend and extension checks
```

## API

`POST /jobs` · `POST /candidates/intake` · `POST /candidates/import` ·
`POST /candidates/import/batch` · `GET /candidates` ·
`POST /candidates/{id}/enrich` · `POST /enrich/batch` ·
`POST /jobs/{id}/rank` · `POST /outreach/draft` ·
`POST /outreach/{id}/approve` · `PATCH /candidates/{id}/stage` ·
`POST /dnc` · `GET /stats` · `GET /health`

## Compliance defaults

- Use the Indeed integration only with candidate data you are authorized to
  access and retain.
- Email-first; phone and SMS require appropriate TCPA consent.
- Human approval is required before outreach is used.
- Nothing is sent automatically.
- Do-not-contact entries are enforced during enrichment and outreach.
- Honor opt-outs and applicable retention/deletion requirements.
