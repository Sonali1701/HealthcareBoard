# HealthBoard — Backend API

FastAPI backend for the HealthBoard healthcare-staffing platform. It powers the
existing static HTML frontends (`healthboard-*.html`): the careers platform, the
AI matching engine, the recruiter chat/CRM, and the GSA per-diem pay calculators.

- **Framework:** FastAPI + SQLAlchemy 2 (ORM)
- **Database:** SQLite by default (zero setup); portable to PostgreSQL
- **Auth:** JWT access/refresh tokens, MFA (TOTP), password reset, email verification
- **Docs:** interactive OpenAPI/Swagger at `/docs`

## Quick start

```bash
# 1. Install dependencies (into the existing .venv)
.venv\Scripts\python -m pip install -r requirements.txt

# 2. (Optional) configure environment
copy .env.example .env       # edit JWT_SECRET, GSA_API_KEY, etc.

# 3. Seed demo data (creates healthboard.db)
.venv\Scripts\python -m app.seed

# 4. Run the server
.venv\Scripts\python -m uvicorn app.main:app --reload
#   ...or just:  .venv\Scripts\python main.py
```

Then open:

- **API docs (Swagger):** http://127.0.0.1:8000/docs
- **Health check:** http://127.0.0.1:8000/api/health
- **Static frontends:** http://127.0.0.1:8000/ui/healthboard-pro.html
  (any `healthboard-*.html` in the project root is served under `/ui/`)

### Load your own candidate data (resumes)

Instead of (or in addition to) the demo seed, import a folder of real resumes:

```bash
.venv\Scripts\python -m alembic upgrade head                 # create the DB
.venv\Scripts\python -m app.importers.resumes "data" --dry-run   # preview parse
.venv\Scripts\python -m app.importers.resumes "data"             # import for real
```

Each `.pdf` / `.docx` becomes a candidate **profile** (name, specialty, profession,
licenses, certifications, state) with the original file stored and linked as
`resume_url`. View the imported candidates on the **live board**:

> http://127.0.0.1:8000/ui/live-candidates.html

A recruiter login for the matching / recruiter / chat features:
`recruiter@example.com` / `Password123!`

### Wired frontend pages (live data, served at /ui/)

All pages now read the real API via `hb-api.js` (a shared client with JWT login).
Verified end-to-end with a headless browser.

| Page | Reads from | Shows |
|------|-----------|-------|
| `healthboard-pro.html` | `/api/profiles`, `/api/jobs` | candidate board (your imported physicians) + jobs |
| `healthboard-ai-matching.html` | `/api/matching/run` | ranked matches against your data (login required) |
| `healthboard-chat-platform.html` | `/api/messages/*`, `/api/analytics/*` | recruiter inbox, conversations, CRM funnel (login required) |
| `healthboard-gsa-pay-calculator.html` | `/api/gsa/rates` | live/fallback GSA per-diem (no API key needed client-side) |
| `healthboard-pay-calculator.html` | `/api/gsa/rates` | same, simpler UI |
| `live-candidates.html` | `/api/profiles` | minimal candidate board |

The matching and chat pages prompt for login (demo creds prefilled). To populate
the chat inbox + CRM with demo conversations built from your imported physicians:

```bash
python -m app.seed_demo_conversations          # add demo threads/applications
python -m app.seed_demo_conversations --clear  # remove just that demo data
```

### Demo logins (after seeding)

| Role      | Email                        | Password       |
|-----------|------------------------------|----------------|
| Recruiter | `recruiter@healthboard.dev`  | `Password123!` |
| Nurse     | `jessica@healthboard.dev`    | `Password123!` |

(plus `alex@`, `priya@`, `marcus@`, `sara@healthboard.dev`)

## The application

The product UI is the original `healthboard-*.html` design, served at clean routes
and fully wired to the API (via `hb-api.js`, login modal + JWT). Open `/` and use it.

| Route | Page | Wired features |
|-------|------|----------------|
| `/` | Pro app | Dashboard, Find Jobs + job-detail Apply/Save, Professionals, Community Feed (post/like), Notifications, My Profile (view/edit/résumé upload), Messages, Employer Portal, Analytics, Settings |
| `/match` | AI Matching | run matching on real candidates + shortlist |
| `/chat` | Recruiter CRM | inbox, conversation, send, ATS stage, funnel, job-card/offer/schedule |
| `/calculator` | Pay calculator | GSA per-diem via backend proxy |

Demo logins: recruiter `recruiter@example.com` / `Password123!` · candidate
`seeker@example.com` / `Password123!` (both seeded). Seed a clean demo with:
`alembic upgrade head` → `python -m app.importers.resumes data` →
`python -m app.seed_jobs` → `python -m app.seed_demo_conversations` → `python -m app.seed_social`.

A simpler server-rendered (Jinja2 + HTMX) variant of the app also exists under `/app/*`.

### (legacy) Jinja variant

| URL | Who | What |
|-----|-----|------|
| `/` | everyone | landing + job search |
| `/jobs`, `/jobs/{id}` | everyone | browse/filter jobs, view detail, **Apply** |
| `/talent`, `/talent/{id}` | everyone | browse candidates, view profile |
| `/signup`, `/login`, `/logout` | everyone | real accounts (job-seeker or recruiter) |
| `/dashboard` | job seeker | applications, saved jobs, profile completeness |
| `/profile`, `/profile/edit` | job seeker | build profile, **upload résumé**, add skills/licenses |
| `/recruiter` | recruiter | org dashboard + KPIs |
| `/recruiter/jobs/new` | recruiter | **post a job** |
| `/recruiter/jobs/{id}` | recruiter | applicants + move through ATS pipeline |
| `/matching` | recruiter | run AI matching on the talent pool |
| `/messages` | both | recruiter ↔ candidate conversations |
| `/tools/pay-calculator` | everyone | W2 vs per-diem pay package |
| `/docs` | dev | the JSON API (still fully available) |

Working end-to-end flows: candidate **sign up → build profile → upload résumé →
search → apply**; recruiter **sign up → create org → post job → review applicants →
move ATS stage → AI-match → message**.

The old standalone mockups remain at `/ui/healthboard-*.html` but the real product
is the app at `/`.

## Run it & check every feature

```bash
# 1. install + create DB + import your résumés + demo conversations
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m alembic upgrade head
.venv\Scripts\python -m app.importers.resumes "data"
.venv\Scripts\python -m app.seed_demo_conversations

# 2. start the server
.venv\Scripts\python main.py            # http://127.0.0.1:8000

# 3. in a SECOND terminal, verify EVERY feature (backend + all pages)
.venv\Scripts\python scripts\verify_features.py
```

`verify_features.py` hits the running server and prints a PASS/FAIL line for
every feature — auth, profiles/search, jobs, AI matching, GSA pay, messaging,
CRM analytics, notifications — and (via a headless browser) every wired page.
A green `29/29 checks passed` means the whole app works. Pass `--no-pages` to
skip the browser checks.

Or check by eye in the browser:

| Open | What to look for |
|------|------------------|
| `/ui/healthboard-pro.html` → Professionals tab | your 14 imported physicians |
| `/ui/healthboard-ai-matching.html` | login (prefilled) → ranked candidates |
| `/ui/healthboard-chat-platform.html` | login → 3 conversations + CRM funnel |
| `/ui/healthboard-gsa-pay-calculator.html` | per-diem rates load (no key needed) |
| `/docs` | every API endpoint, try-it-out |

## Deploy — only Postgres + Cloudflare R2 credentials left

Everything is wired; you fill **two credential blocks** in `.env` and go.

```bash
cp .env.production.example .env     # then edit the two <<< FILL >>> blocks:
#   1) DATABASE_URL  = your PostgreSQL URL
#   2) S3_* (5 lines) = your Cloudflare R2 bucket + keys
#   also: generate JWT_SECRET, set CORS_ORIGINS / FRONTEND_BASE_URL
```

- **Render:** push the repo; `render.yaml` provisions the web service **and a
  managed Postgres** (auto-wires `DATABASE_URL` + generates `JWT_SECRET`). Add the
  R2 secrets in the dashboard. Migrations run automatically on deploy.
- **Vultr / any Docker host:** `docker compose up --build` (brings up the API +
  Postgres together).

The code already: normalizes Render's `postgres://` URL to the psycopg driver,
serves résumés from R2 when `STORAGE_ENABLED=true` (and from local `./uploads`
otherwise), and runs `alembic upgrade head` on container start. No code changes
needed to switch — just the credentials.

## Architecture

```
app/
  config.py         Settings (env / .env), CORS, GSA + JWT config
  database.py       Engine, session, Base, TZDateTime, column factories
  security.py       Password hashing, JWT, MFA (TOTP), token hashing
  deps.py           Auth dependencies (current user, role guards)
  main.py           FastAPI app, router wiring, static frontend serving
  seed.py           Demo data loader  (python -m app.seed)
  models/           SQLAlchemy ORM — 29 tables across 6 domains
  schemas/          Pydantic request/response models
  routers/          API endpoints (see below)
  services/
    gsa.py          GSA per-diem proxy + pay-package math
    matching.py     Explainable candidate-matching engine
```

### Data model (29 tables, 6 domains)

| Domain        | Tables |
|---------------|--------|
| Identity/Auth | `users`, `oauth_accounts`, `sessions`, `password_reset_tokens`, `email_verification_tokens` |
| Profiles      | `profiles`, `licenses`, `work_history`, `certifications`, `profile_skills` |
| Jobs          | `employers`, `employer_members`, `job_postings`, `applications`, `application_events`, `saved_jobs` |
| Social        | `connections`, `posts`, `post_likes`, `post_comments` |
| Messaging     | `message_threads`, `messages`, `notifications`, `interviews`, `offers` |
| Analytics     | `match_runs`, `match_results`, `pay_packages`, `audit_logs` |

Portability notes (SQLite ↔ Postgres): UUIDs are stored as strings, arrays/JSONB
as JSON, geo points as `lat`/`lng` floats, and full-text search uses a
denormalised lowercase `search_text` column with `LIKE`. A custom `TZDateTime`
type guarantees timezone-aware UTC datetimes on both backends.

## API surface

| Prefix                 | What it covers |
|------------------------|----------------|
| `/api/auth`            | register, login, refresh, logout, MFA, password reset, email verify |
| `/api/profiles`        | profile CRUD + search, licenses, certs, work history, skills |
| `/api/employers`       | employer org CRUD |
| `/api/jobs`            | job search/CRUD, apply, list applicants, save/unsave |
| `/api/applications`    | my applications, saved jobs, ATS stage transitions, event history |
| `/api/social`          | connections, posts, likes, comments |
| `/api/messages`        | threads, messages, ATS stage, interviews, offers |
| `/api/notifications`   | notification feed, unread count, mark read |
| `/api/matching`        | run AI matching, fetch a run, shortlist candidates |
| `/api/gsa`             | live GSA per-diem rates, pay-package calculator, save packages |
| `/api/analytics`       | recruitment funnel, KPIs, conversation/CRM table |

Full request/response schemas are documented interactively at `/docs`.

### Notable endpoints

```
POST /api/auth/login                     {email, password, mfa_code?} -> tokens
POST /api/matching/run                   {job_id | spec, weights, filters, top_n}
GET  /api/gsa/rates?city=Houston&state=TX
POST /api/gsa/pay-package/calculate      W2 vs W2+per-diem comparison
GET  /api/jobs?specialty=ICU&state_code=TX&pay_min=50&job_type=travel
PATCH /api/applications/{id}/stage       ATS pipeline transitions
```

## AI matching engine

`POST /api/matching/run` scores open-to-work profiles against a job spec on four
weighted dimensions (defaults: skills 35%, experience 25%, location 20%, pay 20%)
and returns ranked, **explainable** matches with a per-dimension breakdown and a
human-readable reason. It is transparent and reproducible (no opaque embeddings),
and persists each run to `match_runs` / `match_results` for later retrieval and
shortlisting. Supports hard filters (verified-license-only, travel boost,
immediately-available).

## GSA pay calculator

`get_gsa_rates()` proxies `api.gsa.gov/travel/perdiem/v2` **server-side** so the
`api.data.gov` key (set `GSA_API_KEY`) is never exposed to the browser — the
fix the frontend prototype flagged as a production TODO. Without a key it falls
back to a baked-in FY2025 rate table. `calculate_pay_package()` models the
bill-rate → margin → burden → W2 pay / tax-free stipend allocation and compares
a pure-W2 package against a W2 + per-diem package.

## Production notes

- Set a strong `JWT_SECRET` (`python -c "import secrets; print(secrets.token_urlsafe(48))"`).
- Switch `DATABASE_URL` to PostgreSQL and run migrations (Alembic recommended).
- Set `CORS_ORIGINS` to your real frontend origin(s) instead of `*`.
- Password-reset / email-verification tokens are returned as `dev_token` in the
  response for local testing — replace that with an email send before shipping.
- Registration auto-activates accounts in dev; flip to `pending_verify` for prod.
