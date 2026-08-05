"""
Radixsol Sourcing Assistant — FastAPI backend.

Flow: add candidate names you're entitled to work with (manual / CSV / paste) →
enrich via Enformion → rank vs a job → draft outreach (human-approved) → pipeline.

Run:  uvicorn api:app --port 8090     then open http://localhost:8090
"""
from __future__ import annotations

import base64
import binascii
import hashlib
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse, Response
from pydantic import BaseModel, Field

from sourcing import (
    store, intake, enrich as enrich_mod, ranking, outreach, config, storage,
    resume_enrichment,
)
from sourcing import enformion_client as ef

app = FastAPI(title="Radixsol Sourcing Assistant", version="2.5.2")
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^(chrome-extension|moz-extension)://[a-z0-9-]+$",
    allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
    allow_headers=["Content-Type"],
)

_FRONTEND_DIR = Path(__file__).parent / "frontend"
_FRONTEND = _FRONTEND_DIR / "index.html"


class JobIn(BaseModel):
    title: str
    location: str = ""
    description: str = ""

class IntakeIn(BaseModel):
    text: str
    job_id: int | None = None


class ProfileImportIn(BaseModel):
    name: str
    location: str = ""
    headline: str = ""
    notes: str = ""
    source: str = "indeed"
    source_url: str = ""
    source_id: str = ""
    job_id: int | None = None


class ProfileBatchImportIn(BaseModel):
    profiles: list[ProfileImportIn]
    job_id: int | None = None
    search_url: str = ""


class ResumeDownloadIn(BaseModel):
    path: str
    filename: str = ""


class ResumeCaptureIn(BaseModel):
    content_base64: str
    filename: str = "resume.pdf"


class ProviderResultIn(BaseModel):
    source: str
    status: str
    matched_name: str = ""
    phones: list[str] = Field(default_factory=list)
    emails: list[str] = Field(default_factory=list)
    addresses: list[str] = Field(default_factory=list)
    confidence: float = 0
    profile_url: str = ""


class OutreachIn(BaseModel):
    candidate_id: int
    job_id: int | None = None
    channel: str | None = None

class StageIn(BaseModel):
    stage: str

class DncIn(BaseModel):
    value: str
    reason: str = ""


@app.get("/", response_class=HTMLResponse)
def home():
    if _FRONTEND.exists():
        return FileResponse(str(_FRONTEND))
    return HTMLResponse("<h1>Radixsol Sourcing Assistant API</h1>")


@app.get("/styles.css", include_in_schema=False)
def styles():
    return FileResponse(str(_FRONTEND_DIR / "styles.css"), media_type="text/css")


@app.get("/app.js", include_in_schema=False)
def frontend_script():
    return FileResponse(
        str(_FRONTEND_DIR / "app.js"),
        media_type="application/javascript",
    )


@app.get("/health")
def health():
    from sourcing import llm
    return {"status": "ok", "mode": "demo" if config.DEMO_MODE else "live",
            "llm": "gemini" if llm.available() else "template",
            "identity_matching": (
                "provider-or-directory+rules+ai"
                if config.AI_MATCH_ENABLED and llm.available()
                else "provider-or-directory+rules"
            ),
            "provider": "enformion",
            "browser_provider": "usphonebook",
            "database": store.backend_name(),
            "resume_storage": "r2" if storage.enabled() else "database"}


@app.get("/stats")
def stats():
    return store.stats()

# ---- jobs ----
@app.post("/jobs")
def create_job(j: JobIn):
    title = j.title.strip()
    if not title:
        raise HTTPException(400, "Job title is required.")
    return {"id": store.create_job(title, j.location, j.description)}

@app.get("/jobs")
def jobs():
    return store.list_jobs()

# ---- candidates ----
@app.post("/candidates/intake")
def intake_candidates(body: IntakeIn):
    rows = intake.parse(body.text)
    if not rows:
        raise HTTPException(400, "No candidate names found. Paste one per line or upload a CSV with a 'name' column.")
    ids = store.add_candidates_bulk(rows, job_id=body.job_id)
    return {"added": len(ids), "ids": ids, "parsed": rows}


def _profile_row(body: ProfileImportIn, default_job_id: int | None = None):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "Candidate name is required.")
    job_id = body.job_id if body.job_id is not None else default_job_id
    source = body.source.strip().lower()[:50] or "indeed"
    location = body.location.strip()[:500]
    notes = body.notes.strip()
    if body.headline.strip() and body.headline.strip().lower() not in notes.lower():
        notes = f"{body.headline.strip()}\n\n{notes}".strip()
    return {
        "name": name,
        "location": location,
        "job_id": job_id,
        "notes": notes[:20000],
        "source": source,
        "source_url": body.source_url.strip()[:2000],
        "source_id": body.source_id.strip()[:500],
    }


def _import_profile(body: ProfileImportIn):
    row = _profile_row(body)
    if row["job_id"] is not None and not store.get_job(row["job_id"]):
        raise HTTPException(404, "job not found")
    return store.upsert_candidate_profiles([row])[0]


@app.post("/candidates/import")
def import_candidate(body: ProfileImportIn):
    return _import_profile(body)


@app.post("/candidates/import/batch")
def import_candidate_batch(body: ProfileBatchImportIn):
    if not body.profiles:
        raise HTTPException(400, "At least one profile is required.")
    if len(body.profiles) > 100:
        raise HTTPException(400, "A maximum of 100 displayed profiles can be saved at once.")
    if body.job_id is not None and not store.get_job(body.job_id):
        raise HTTPException(404, "job not found")

    rows = [_profile_row(profile, default_job_id=body.job_id) for profile in body.profiles]
    upserted = store.upsert_candidate_profiles(rows, default_job_id=body.job_id)
    imported = sum(int(result["imported"]) for result in upserted)
    existing = len(upserted) - imported
    results = [
        {
            "id": result["id"],
            "imported": result["imported"],
            "candidate": result["candidate"],
        }
        for result in upserted
    ]
    return {
        "saved": len(results),
        "imported": imported,
        "existing": existing,
        "results": results,
        "search_url": body.search_url[:2000],
        "database": store.backend_name(),
    }


@app.get("/candidates")
def candidates(job_id: int | None = None, stage: str | None = None):
    return store.list_candidates(job_id=job_id, stage=stage)

@app.get("/candidates/{cid}")
def candidate(cid: int):
    c = store.get_candidate(cid)
    if not c:
        raise HTTPException(404, "not found")
    c["outreach"] = store.list_outreach(cid)
    c["resumes"] = store.list_resumes(cid)
    return c

@app.patch("/candidates/{cid}/stage")
def set_stage(cid: int, s: StageIn):
    if not store.get_candidate(cid):
        raise HTTPException(404, "candidate not found")
    try:
        store.set_stage(cid, s.stage)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


def _store_resume_pdf(cid: int, filename: str, data: bytes):
    candidate = store.get_candidate(cid)
    if not candidate:
        raise HTTPException(404, "candidate not found")
    data, contact_sheet_embedded = resume_enrichment.add_contact_sheet(data, candidate)
    if contact_sheet_embedded:
        filename = resume_enrichment.enriched_filename(filename)
    size = len(data)
    checksum = hashlib.sha256(data).hexdigest()
    existing = store.get_resume_by_checksum(cid, checksum)
    if existing:
        return {
            **existing,
            "contact_sheet_embedded": contact_sheet_embedded,
            "contacts_saved": bool(candidate.get("phones") or candidate.get("emails")),
            "deduplicated": True,
        }
    if storage.enabled():
        try:
            uploaded = storage.upload_resume(cid, filename, data)
        except Exception as exc:
            raise HTTPException(502, f"Resume upload to private object storage failed: {exc}")
        resume = store.attach_resume(
            cid,
            filename,
            b"",
            size=size,
            **uploaded,
        )
    else:
        resume = store.attach_resume(cid, filename, data, checksum_sha256=checksum)
    return {
        **resume,
        "contact_sheet_embedded": contact_sheet_embedded,
        "contacts_saved": bool(candidate.get("phones") or candidate.get("emails")),
    }


@app.post("/candidates/{cid}/resume/from-download")
def attach_downloaded_resume(cid: int, body: ResumeDownloadIn):
    if not store.get_candidate(cid):
        raise HTTPException(404, "candidate not found")
    try:
        path = Path(body.path).resolve(strict=True)
    except (OSError, RuntimeError):
        raise HTTPException(400, "Downloaded resume file was not found.")
    try:
        path.relative_to(config.RESUME_DOWNLOAD_DIR)
    except ValueError:
        raise HTTPException(400, "Resume must be inside the configured Downloads directory.")
    if path.suffix.lower() != ".pdf":
        raise HTTPException(400, "Only PDF resumes can be attached automatically.")
    size = path.stat().st_size
    if size <= 0 or size > config.RESUME_MAX_BYTES:
        raise HTTPException(400, "Resume PDF is empty or exceeds the configured size limit.")
    data = path.read_bytes()
    if not data.startswith(b"%PDF"):
        raise HTTPException(400, "The downloaded file is not a valid PDF.")
    filename = Path(body.filename or path.name).name[:255] or "resume.pdf"
    resume = _store_resume_pdf(cid, filename, data)
    return {"attached": True, "resume": resume}


@app.post("/candidates/{cid}/resume/from-browser")
def attach_captured_resume(cid: int, body: ResumeCaptureIn):
    candidate = store.get_candidate(cid)
    if not candidate:
        raise HTTPException(404, "candidate not found")
    encoded = body.content_base64.strip()
    if encoded.lower().startswith("data:") and "," in encoded:
        encoded = encoded.split(",", 1)[1]
    if not encoded or len(encoded) > (config.RESUME_MAX_BYTES * 2):
        raise HTTPException(400, "Captured resume is empty or exceeds the configured size limit.")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(400, "Captured resume data is not valid base64.")
    pdf_start = data.find(b"%PDF-")
    if pdf_start < 0:
        raise HTTPException(400, "Captured resume is not a valid PDF.")
    if pdf_start:
        data = data[pdf_start:]
    if not data or len(data) > config.RESUME_MAX_BYTES:
        raise HTTPException(400, "Captured resume is empty or exceeds the configured size limit.")
    filename = Path(body.filename or "resume.pdf").name[:255] or "resume.pdf"
    if not filename.lower().endswith(".pdf"):
        filename = f"{filename}.pdf"
    resume = _store_resume_pdf(cid, filename, data)
    refreshed = store.get_candidate(cid)
    return {
        "attached": True,
        "resume": resume,
        "candidate": {
            "id": cid,
            "phones": refreshed.get("phones", []),
            "emails": refreshed.get("emails", []),
            "database_saved": bool(refreshed.get("phones") or refreshed.get("emails")),
        },
    }


@app.get("/candidates/{cid}/resumes/{resume_id}")
def get_resume(cid: int, resume_id: int):
    resume = store.get_resume(cid, resume_id)
    if not resume:
        raise HTTPException(404, "resume not found")
    safe_name = Path(resume["filename"]).name.replace('"', "")
    data = bytes(resume.get("data") or b"")
    if not data and resume.get("storage_provider") == "r2":
        try:
            data = storage.download_resume(resume.get("object_key") or "")
        except Exception as exc:
            raise HTTPException(502, f"Stored resume could not be retrieved: {exc}")
    if not data:
        raise HTTPException(404, "resume file is unavailable")
    return Response(
        content=data,
        media_type=resume["mime_type"] or "application/pdf",
        headers={"Content-Disposition": f'inline; filename="{safe_name}"'},
    )

# ---- enrichment ----
@app.post("/candidates/{cid}/enrich")
def enrich_one(cid: int):
    result = enrich_mod.enrich_candidate(cid)
    if result.get("error") == "candidate not found":
        raise HTTPException(404, result["error"])
    return result


@app.post("/candidates/{cid}/provider-result")
def save_provider_result(cid: int, body: ProviderResultIn):
    if not store.get_candidate(cid):
        raise HTTPException(404, "candidate not found")
    if body.source.strip().lower() != "usphonebook":
        raise HTTPException(400, "unsupported browser lookup provider")
    if body.status != "success":
        raise HTTPException(400, "only successful browser lookup results can be saved")
    if len(body.phones) > 20 or len(body.emails) > 20 or len(body.addresses) > 20:
        raise HTTPException(400, "provider result contains too many contact values")
    if body.profile_url:
        hostname = (urlparse(body.profile_url).hostname or "").lower()
        if hostname not in {"usphonebook.com", "www.usphonebook.com"}:
            raise HTTPException(400, "provider profile URL must be on USPhoneBook")
    result = enrich_mod.save_provider_result(cid, {
        "source": "usphonebook",
        "status": "success",
        "matched_name": body.matched_name[:200],
        "phones": [value[:100] for value in body.phones],
        "emails": [value[:320] for value in body.emails],
        "addresses": [value[:500] for value in body.addresses],
        "confidence": body.confidence,
        "profile_url": body.profile_url[:2000],
    })
    if result.get("error") == "candidate not found":
        raise HTTPException(404, result["error"])
    return result


@app.post("/enrich/batch")
def enrich_batch(job_id: int | None = None):
    return enrich_mod.enrich_batch(job_id=job_id)

# ---- ranking ----
@app.post("/jobs/{job_id}/rank")
def rank(job_id: int):
    r = ranking.rank_job(job_id)
    if r.get("error"):
        raise HTTPException(404, r["error"])
    return r

# ---- outreach ----
@app.post("/outreach/draft")
def draft(o: OutreachIn):
    job = store.get_job(o.job_id) if o.job_id else None
    if o.job_id and not job:
        raise HTTPException(404, "job not found")
    r = outreach.draft_for_candidate(o.candidate_id, job=job, channel=o.channel)
    if r.get("error"):
        raise HTTPException(400, r["error"])
    return r

@app.post("/outreach/{oid}/approve")
def approve(oid: int):
    r = outreach.approve_and_mark_sent(oid)
    if r.get("error"):
        raise HTTPException(404, r["error"])
    return r

# ---- do-not-contact ----
@app.post("/dnc")
def add_dnc(d: DncIn):
    value = d.value.strip()
    if not value:
        raise HTTPException(400, "Email or phone is required.")
    store.add_dnc(value, d.reason)
    return {"ok": True}

@app.get("/dnc")
def dnc():
    return store.list_dnc()

@app.get("/compliance")
def compliance():
    return {"notice": config.COMPLIANCE_NOTICE,
            "human_approval_required": config.REQUIRE_HUMAN_APPROVAL,
            "default_channel": config.DEFAULT_CHANNEL}
