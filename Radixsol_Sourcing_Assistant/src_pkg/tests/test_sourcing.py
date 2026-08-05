"""End-to-end sourcing tests in demo mode (no live Enformion key needed)."""
import asyncio
import base64
import json
import os
import sys
import tempfile
from io import BytesIO
from pathlib import Path

os.environ["ENFORMION_DEMO"] = "1"
os.environ["SOURCING_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["DATABASE_URL"] = ""
os.environ["STORAGE_ENABLED"] = "0"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx

import api as api_module
from sourcing import (
    store,
    intake,
    enrich,
    ranking,
    outreach,
    enformion_client as ef,
    verification,
    config,
    storage,
    resume_enrichment,
)

# Tests always use isolated SQLite and mocked object storage, regardless of the
# developer's active .env.local cloud configuration.
config.DATABASE_URL = ""
config.STORAGE_ENABLED = False


def test_intake_paste_formats():
    rows = intake.parse(
        "Jane Doe, Atlanta, GA\nJohn Smith - Dallas TX\n"
        "Maria Lopez | Chicago, IL\nAda Lovelace – London\nBob"
    )
    assert len(rows) == 5
    assert rows[0]["name"] == "Jane Doe" and "Atlanta" in rows[0]["location"]
    assert rows[3] == {"name": "Ada Lovelace", "location": "London"}
    assert rows[4]["name"] == "Bob" and rows[4]["location"] == ""


def test_intake_csv():
    rows = intake.parse("name,location\nJane Doe,Atlanta GA\nJohn Smith,Dallas TX")
    assert len(rows) == 2 and rows[1]["name"] == "John Smith"


def test_enrichment_demo_returns_contact():
    r = ef.enrich("Jane Doe", "Atlanta, GA")
    assert r["status"] == "success"
    assert r["phones"] and r["emails"]
    # deterministic
    assert ef.enrich("Jane Doe", "Atlanta, GA")["emails"] == r["emails"]


def test_full_pipeline():
    store.reset()
    job = store.create_job("Radiologic Technologist", "Atlanta, GA", "radiologic technologist imaging xray CT")
    ids = store.add_candidates_bulk(
        [{"name": "Jane Doe", "location": "Atlanta, GA"},
         {"name": "John Smith", "location": "Dallas, TX"}], job_id=job)
    assert len(ids) == 2
    res = enrich.enrich_batch(job_id=job)
    assert res["matched"] == 2
    ranking.rank_job(job)
    cands = store.list_candidates(job_id=job)
    assert all(c["enrich_status"] == "success" for c in cands)
    assert all(c["emails"] for c in cands)
    assert all(c["verification"]["identity_status"] == "provider_match" for c in cands)
    resume = store.attach_resume(cands[0]["id"], "candidate-resume.pdf", b"%PDF-1.4 fixture")
    assert store.list_resumes(cands[0]["id"])[0]["id"] == resume["id"]
    assert store.get_resume(cands[0]["id"], resume["id"])["data"].startswith(b"%PDF")
    # outreach draft respects compliance + advances stage
    d = outreach.draft_for_candidate(cands[0]["id"], job=store.get_job(job))
    assert d["status"] == "draft" and d["body"]
    assert store.get_candidate(cands[0]["id"])["stage"] == "contacted"


def test_do_not_contact_suppresses():
    store.reset()
    cid = store.add_candidate("Blocked Person", "Atlanta, GA")
    r = ef.enrich("Blocked Person", "Atlanta, GA")
    store.add_dnc(r["emails"][0], "opted out")
    out = enrich.enrich_candidate(cid)
    assert r["emails"][0] not in out["emails"]  # suppressed
    # outreach refuses when no usable contact remains
    store.update_candidate(cid, emails=[], phones=[])
    d = outreach.draft_for_candidate(cid)
    assert "error" in d


def test_ranking_orders_contactable_and_location():
    store.reset()
    job = store.create_job("Nurse", "Atlanta, GA", "registered nurse healthcare")
    c1 = store.add_candidate("A Local", "Atlanta, GA", job)
    c2 = store.add_candidate("B Far", "Seattle, WA", job)
    enrich.enrich_batch(job_id=job)
    ranking.rank_job(job)
    ranked = store.list_candidates(job_id=job)
    assert ranked[0]["fit_score"] >= ranked[-1]["fit_score"]


def test_verification_keeps_identity_and_deliverability_separate():
    result = verification.assess(
        {"name": "Jane Doe", "location": "Atlanta, GA", "notes": "Registered Nurse"},
        {
            "status": "success",
            "matched_name": "Jane Doe",
            "addresses": ["100 Main St, Atlanta, GA"],
            "emails": ["jane@example.org"],
            "phones": ["(404) 555-1212"],
            "confidence": 0.9,
        },
    )
    assert result["identity_status"] == "provider_match"
    assert result["emails"][0]["format_valid"] is True
    assert result["emails"][0]["deliverability"] == "not_checked"
    assert result["phones"][0]["identity_owner"] == "not_checked"


def test_resume_contact_sheet_preserves_source_pages():
    from pypdf import PdfReader, PdfWriter

    source = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.write(source)
    enriched, embedded = resume_enrichment.add_contact_sheet(source.getvalue(), {
        "id": 42,
        "name": "Alex Morgan",
        "location": "Atlanta, GA",
        "emails": ["alex.morgan@example.test"],
        "phones": ["(404) 555-0187"],
        "confidence": 0.94,
        "verification": {"source": "usphonebook"},
    })

    reader = PdfReader(BytesIO(enriched))
    contact_text = reader.pages[0].extract_text()
    assert embedded is True
    assert len(reader.pages) == 2
    assert "Alex Morgan" in contact_text
    assert "alex.morgan@example.test" in contact_text
    assert "(404) 555-0187" in contact_text
    assert reader.metadata["/RadixsolCandidateId"] == "42"
    repeated, repeated_embedded = resume_enrichment.add_contact_sheet(source.getvalue(), {
        "id": 42,
        "name": "Alex Morgan",
        "location": "Atlanta, GA",
        "emails": ["alex.morgan@example.test"],
        "phones": ["(404) 555-0187"],
        "confidence": 0.94,
        "verification": {"source": "usphonebook"},
    })
    assert repeated_embedded is True
    assert repeated == enriched


def test_api_workflow_and_extension_cors():
    store.reset()

    async def exercise_api():
        transport = httpx.ASGITransport(app=api_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            health = await client.get("/health")
            assert health.status_code == 200
            assert health.json()["database"] == "sqlite"

            job_response = await client.post("/jobs", json={
                "title": "Nurse",
                "location": "Atlanta, GA",
                "description": "registered nurse healthcare",
            })
            assert job_response.status_code == 200
            job_id = job_response.json()["id"]

            intake_response = await client.post("/candidates/intake", json={
                "text": "Jane Doe, Atlanta, GA",
                "job_id": job_id,
            })
            assert intake_response.status_code == 200
            candidate_id = intake_response.json()["ids"][0]
            assert (await client.post(f"/candidates/{candidate_id}/enrich")).status_code == 200
            assert (await client.post(f"/jobs/{job_id}/rank")).json()["ranked"] == 1

            draft = await client.post("/outreach/draft", json={
                "candidate_id": candidate_id,
                "job_id": job_id,
            })
            assert draft.status_code == 200
            outreach_id = draft.json()["outreach_id"]
            approved = await client.post(f"/outreach/{outreach_id}/approve")
            assert approved.json()["status"] == "approved"
            assert (await client.post("/outreach/999999/approve")).status_code == 404

            indeed_profile = {
                "name": "Alex Morgan",
                "location": "Atlanta, GA",
                "headline": "Registered Nurse",
                "notes": "Registered Nurse\nEmergency care\nBLS certification",
                "source": "indeed",
                "source_url": "https://employers.indeed.com/smartsourcing?candidateId=abc123",
                "source_id": "abc123",
                "job_id": job_id,
            }
            first_import = await client.post("/candidates/import", json=indeed_profile)
            assert first_import.status_code == 200
            assert first_import.json()["imported"] is True
            imported_id = first_import.json()["id"]
            assert first_import.json()["candidate"]["source"] == "indeed"
            assert "BLS certification" in first_import.json()["candidate"]["notes"]
            assert (await client.post(f"/candidates/{imported_id}/enrich")).status_code == 200
            provider_result = await client.post(
                f"/candidates/{imported_id}/provider-result",
                json={
                    "source": "usphonebook",
                    "status": "success",
                    "matched_name": "Alex Morgan",
                    "phones": ["(404) 555-0187"],
                    "emails": ["alex.morgan@example.test"],
                    "addresses": ["100 Main St, Atlanta, GA 30303"],
                    "confidence": 1,
                    "profile_url": "https://www.usphonebook.com/alex-morgan/example-id",
                },
            )
            assert provider_result.status_code == 200
            assert provider_result.json()["enrich_status"] == "success"
            assert provider_result.json()["verification"]["source"] == "usphonebook"
            stored_provider_result = await client.get(f"/candidates/{imported_id}")
            assert "(404) 555-0187" in stored_provider_result.json()["phones"]

            resume_dir = Path(tempfile.mkdtemp())
            resume_path = resume_dir / "Alex-Morgan-resume.pdf"
            resume_path.write_bytes(b"%PDF-1.4 test resume")
            original_resume_dir = config.RESUME_DOWNLOAD_DIR
            config.RESUME_DOWNLOAD_DIR = resume_dir.resolve()
            try:
                attached = await client.post(
                    f"/candidates/{imported_id}/resume/from-download",
                    json={"path": str(resume_path), "filename": resume_path.name},
                )
                assert attached.status_code == 200
                resume_id = attached.json()["resume"]["id"]
                downloaded = await client.get(
                    f"/candidates/{imported_id}/resumes/{resume_id}"
                )
                assert downloaded.status_code == 200
                assert downloaded.content.startswith(b"%PDF")
            finally:
                config.RESUME_DOWNLOAD_DIR = original_resume_dir

            captured = await client.post(
                f"/candidates/{imported_id}/resume/from-browser",
                json={
                    "content_base64": base64.b64encode(
                        b"leading bytes%PDF-1.4 browser-captured resume"
                    ).decode("ascii"),
                    "filename": "browser-captured-resume.pdf",
                },
            )
            assert captured.status_code == 200
            captured_resume_id = captured.json()["resume"]["id"]
            captured_download = await client.get(
                f"/candidates/{imported_id}/resumes/{captured_resume_id}"
            )
            assert captured_download.status_code == 200
            assert captured_download.content.startswith(b"%PDF")

            duplicate_capture = await client.post(
                f"/candidates/{imported_id}/resume/from-browser",
                json={
                    "content_base64": base64.b64encode(
                        b"leading bytes%PDF-1.4 browser-captured resume"
                    ).decode("ascii"),
                    "filename": "browser-captured-resume.pdf",
                },
            )
            assert duplicate_capture.status_code == 200
            assert duplicate_capture.json()["resume"]["id"] == captured_resume_id
            assert duplicate_capture.json()["resume"]["deduplicated"] is True

            second_import = await client.post("/candidates/import", json=indeed_profile)
            assert second_import.status_code == 200
            assert second_import.json()["imported"] is False
            assert second_import.json()["id"] == imported_id

            batch_profile = {
                **indeed_profile,
                "name": "Taylor Reed",
                "source_id": "xyz789",
                "source_url": "https://employers.indeed.com/smartsourcing?candidateId=xyz789",
            }
            batch = await client.post("/candidates/import/batch", json={
                "profiles": [indeed_profile, batch_profile],
                "job_id": job_id,
                "search_url": "https://employers.indeed.com/smartsourcing",
            })
            assert batch.status_code == 200
            assert batch.json()["saved"] == 2
            assert batch.json()["imported"] == 1
            assert batch.json()["existing"] == 1
            assert batch.json()["database"] == "sqlite"

            preflight = await client.options("/health", headers={
                "Origin": f"chrome-extension://{'a' * 32}",
                "Access-Control-Request-Method": "GET",
            })
            assert preflight.status_code == 200
            assert preflight.headers["access-control-allow-origin"].startswith("chrome-extension://")

    asyncio.run(exercise_api())


def test_cloud_resume_api_stores_r2_metadata(monkeypatch):
    store.reset()
    candidate_id = store.add_candidate("Cloud Resume", "Atlanta, GA", source="indeed")
    resume_dir = Path(tempfile.mkdtemp()).resolve()
    resume_path = resume_dir / "cloud-resume.pdf"
    pdf = b"%PDF-1.4 cloud fixture"
    resume_path.write_bytes(pdf)

    monkeypatch.setattr(config, "RESUME_DOWNLOAD_DIR", resume_dir)
    monkeypatch.setattr(config, "STORAGE_ENABLED", True)
    monkeypatch.setattr(storage, "upload_resume", lambda cid, filename, data: {
        "storage_provider": "r2",
        "object_key": f"resumes/{cid}/fixture-{filename}",
        "bucket": "radixsol-test-resumes",
        "public_url": "",
        "checksum_sha256": "fixture-checksum",
        "etag": "fixture-etag",
    })
    monkeypatch.setattr(storage, "download_resume", lambda key: pdf)

    async def exercise_cloud_resume():
        transport = httpx.ASGITransport(app=api_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            attached = await client.post(
                f"/candidates/{candidate_id}/resume/from-download",
                json={"path": str(resume_path), "filename": resume_path.name},
            )
            assert attached.status_code == 200
            metadata = attached.json()["resume"]
            assert metadata["storage_provider"] == "r2"
            assert metadata["bucket"] == "radixsol-test-resumes"
            stored = store.get_resume(candidate_id, metadata["id"])
            assert stored["data"] == b""
            downloaded = await client.get(
                f"/candidates/{candidate_id}/resumes/{metadata['id']}"
            )
            assert downloaded.status_code == 200
            assert downloaded.content == pdf

    asyncio.run(exercise_cloud_resume())


def test_frontend_is_manifest_v3_compatible():
    frontend = Path(__file__).parents[1] / "frontend"
    manifest = json.loads((frontend / "manifest.json").read_text(encoding="utf-8"))
    index = (frontend / "index.html").read_text(encoding="utf-8")
    app_script = (frontend / "app.js").read_text(encoding="utf-8")
    content_script = (frontend / "indeed-content.js").read_text(encoding="utf-8")

    assert manifest["manifest_version"] == 3
    assert manifest["version"] == "2.5.2"
    assert manifest["side_panel"]["default_path"] == "index.html"
    assert "http://127.0.0.1/*" in manifest["host_permissions"]
    assert "*://*.indeed.com/*" in manifest["host_permissions"]
    assert "https://www.usphonebook.com/*" in manifest["host_permissions"]
    assert "scripting" in manifest["permissions"]
    assert "downloads" in manifest["permissions"]
    assert "debugger" in manifest["permissions"]
    assert any(
        "indeed-content.js" in script["js"]
        for script in manifest["content_scripts"]
    )
    assert any(
        "inject.js" in script["js"] and script.get("world") == "MAIN"
        for script in manifest["content_scripts"]
    )
    inject_script = (frontend / "inject.js").read_text(encoding="utf-8")
    assert "URL.createObjectURL" in inject_script
    assert "XMLHttpRequest" in inject_script
    assert "response.clone().arrayBuffer()" in inject_script
    assert any(
        "usphonebook-content.js" in script["js"]
        for script in manifest["content_scripts"]
    )
    assert "RADIXSOL_CAPTURE_INDEED_PROFILE" in content_script
    assert "RADIXSOL_LIST_INDEED_CANDIDATES" in content_script
    assert "RADIXSOL_SCAN_INDEED_CANDIDATES" in content_script
    assert "RADIXSOL_INDEED_SCAN_PROGRESS" in content_script
    assert "RADIXSOL_OPEN_INDEED_CANDIDATE" in content_script
    assert "RADIXSOL_DOWNLOAD_INDEED_RESUME" in content_script
    assert "exact-selected-card+changed-profile-panel" in content_script
    assert "realClick" in content_script
    assert "RADIXSOL_TRUSTED_INDEED_CLICK" in content_script
    assert "text.length > 40 || !/^download\\b/i.test(text)" in content_script
    assert "trigger?.contains?.(element)" not in content_script
    assert "a[href$='.pdf']" in content_script
    assert "hasCompleteIndeedContact" in app_script
    assert "timeout: 300000" in app_script
    assert "recoverStoredResume" in app_script
    assert "RADIXSOL_INDEED_RESULTS_CHANGED" in content_script
    assert "Indeed Profiles" in index
    assert "bulk-enrich-indeed" in app_script
    assert "lookup-indeed" in app_script
    assert "RADIXSOL_USPHONEBOOK_LOOKUP" in app_script
    assert "50 candidates captured" not in app_script
    assert "onclick=" not in index
    assert '<script src="app.js"></script>' in index
