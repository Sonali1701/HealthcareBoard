"""Healthcare profile endpoints + nested licenses, certs, work history, skills."""
from __future__ import annotations

import io
import re
import time
from collections import OrderedDict
from html import escape, unescape
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel
from sqlalchemy import and_, func, literal, or_, select, text as sa_text
from sqlalchemy.orm import selectinload

from ..database import utcnow
from ..deps import CurrentUser, DbSession
from ..importers.parsing import NAME_PLACEHOLDERS, SECTION_HEADERS, is_real_name
from ..services import storage
from ..models import (
    AuditLog,
    Certification,
    License,
    Profile,
    ProfileSkill,
    WorkHistory,
)
from ..schemas.common import Page
from ..schemas.profile import (
    CertificationCreate,
    CertificationOut,
    LicenseCreate,
    LicenseOut,
    ProfileCreate,
    ProfileDetail,
    ProfileOut,
    ProfileUpdate,
    SkillCreate,
    SkillOut,
    WorkHistoryCreate,
    WorkHistoryOut,
)

router = APIRouter(prefix="/api/profiles", tags=["profiles"])


class ProfileContactUpdate(BaseModel):
    email: Optional[str] = None
    phone: Optional[str] = None


def _is_recruiter_or_admin(user: CurrentUser) -> bool:
    return user.role.value in {"recruiter", "admin"}


def _require_provider_directory_access(user: CurrentUser) -> None:
    if not _is_recruiter_or_admin(user):
        raise HTTPException(status_code=403, detail="Providers are available to recruiters only")


def _compute_completion(p: Profile) -> int:
    score = 0
    if p.headline:
        score += 10
    if p.bio:
        score += 10
    if p.specialty:
        score += 15
    if p.profession_type:
        score += 10
    if p.years_experience:
        score += 10
    if p.city and p.state_code:
        score += 10
    if p.email or p.phone:
        score += 5
    if p.pay_min_hourly:
        score += 5
    if p.resume_url:
        score += 10
    if p.licenses:
        score += 10
    if p.certifications:
        score += 5
    if p.skills:
        score += 5
    return min(score, 100)


def _get_owned_profile(db: DbSession, profile_id: str, user: CurrentUser) -> Profile:
    profile = db.get(Profile, profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    if profile.user_id != user.user_id and user.role.value != "admin":
        raise HTTPException(status_code=403, detail="Not your profile")
    return profile


def _geocode_zip(db, zip_code: str):
    """Look up a ZIP's centroid (lat, lng) from the zip_centroids table."""
    z = str(zip_code).strip()[:10]
    if not z:
        return None
    row = db.execute(
        sa_text("SELECT lat, lng FROM zip_centroids WHERE zip = :z"), {"z": z}
    ).first()
    return (row[0], row[1]) if row and row[0] is not None else None


def _resume_key(resume_url: str) -> str:
    """Map a stored résumé URL back to its storage key."""
    return storage.key_from_url(resume_url)[0]


_RESUME_EXTRA_HEADERS = {
    "summary",
    "clinical experience",
    "professional experience",
    "work experience",
    "healthcare experience",
    "experience",
    "education",
    "certifications",
    "certification",
    "licensure",
    "licensure & certifications",
    "licenses",
    "skills",
    "membership",
    "memberships",
    "organizational",
}


def _normalise_resume_lines(lines: list[str]) -> list[str]:
    """Make extracted PDF/DOCX text render with consistent resume structure."""
    chunks: list[str] = []
    heading_words = (
        "SUMMARY", "CLINICAL EXPERIENCE", "PROFESSIONAL EXPERIENCE",
        "WORK EXPERIENCE", "HEALTHCARE EXPERIENCE", "EXPERIENCE", "EDUCATION",
        "CERTIFICATIONS", "CERTIFICATION", "LICENSURE & CERTIFICATIONS",
        "LICENSURE", "LICENSES", "SKILLS", "MEMBERSHIP", "MEMBERSHIPS",
        "ORGANIZATIONAL",
    )
    heading_pat = re.compile(r"\s+(" + "|".join(re.escape(h) for h in heading_words) + r")\b")

    for raw in lines:
        text = re.sub(r"[_\-]{5,}", "\n", (raw or "").strip())
        text = heading_pat.sub(r"\n\1", text)
        for part in text.splitlines():
            part = re.sub(r"\s+", " ", part).strip(" -|")
            if not part:
                continue
            if len(part) <= 180:
                chunks.append(part)
                continue
            pieces = re.split(r"(?<=[.;:])\s+|\s+•\s+", part)
            buf = ""
            for piece in pieces:
                piece = piece.strip()
                if not piece:
                    continue
                if buf and len(buf) + len(piece) > 180:
                    chunks.append(buf)
                    buf = piece
                else:
                    buf = f"{buf} {piece}".strip()
            if buf:
                chunks.append(buf)
    return chunks


def _looks_like_resume_name(line: str) -> bool:
    text = line.strip()
    if len(text) > 90 or "@" in text or "|" in text:
        return False
    if sum(ch.isdigit() for ch in text) > 2:
        return False
    return 1 <= len(text.split()) <= 8


def _is_resume_heading(line: str) -> bool:
    text = re.sub(r"[^A-Za-z& ]", "", line).strip().lower()
    if text in _RESUME_EXTRA_HEADERS or text in SECTION_HEADERS:
        return True
    return line.isupper() and 3 <= len(line) <= 45 and sum(ch.isdigit() for ch in line) == 0


# Canonical résumé sections, in the fixed display order every résumé uses.
_CANON_SECTIONS = [
    ("Professional Summary", ("summary", "objective", "profile",
                              "professional summary", "about")),
    ("Education & Training", ("education", "education training",
                              "education  training", "training")),
    ("Certifications & Licensure", ("certifications licensure", "licensure certifications",
        "certification", "certifications", "licensure", "licenses", "license",
        "board certification", "board certifications")),
    ("Experience", ("experience", "clinical experience", "professional experience",
        "healthcare experience", "work experience", "employment", "work history")),
    ("Professional Memberships", ("professional memberships", "memberships", "membership",
        "organizational", "professional affiliations", "affiliations")),
    ("Publications & Presentations", ("publications", "publications presentations",
        "presentations", "research", "research publications")),
    ("Awards & Honors", ("awards", "honors", "awards honors")),
    ("Skills", ("skills", "clinical skills", "competencies", "areas of expertise")),
    ("Languages", ("languages", "language")),
    ("References", ("references",)),
]


def _canon_section(heading_line: str) -> str:
    """Map any résumé heading to a canonical section name (or Title-case it)."""
    key = re.sub(r"[^a-z& ]", " ", heading_line.lower())
    key = re.sub(r"\s+", " ", key.replace("&", " ")).strip()
    for canon, aliases in _CANON_SECTIONS:
        if key in aliases or any(key.startswith(a) or a in key for a in aliases):
            return canon
    return heading_line.strip().title()


def _resume_lines(resume_url: str) -> list[str]:
    """Fetch the résumé file and return cleaned text lines."""
    key, is_local_upload_url = storage.key_from_url(resume_url)
    data = storage.download_bytes(key, prefer_local=is_local_upload_url)
    suffix = key.rsplit(".", 1)[-1].lower() if "." in key else ""
    if suffix == "docx":
        from docx import Document
        doc = Document(io.BytesIO(data))
        lines = [p.text.strip() for p in doc.paragraphs]
    elif suffix == "pdf":
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
        lines = [ln.strip() for ln in text.splitlines()]
    else:
        raise ValueError(f"Unsupported résumé type: .{suffix}")
    return _normalise_resume_lines(lines)


_BAD_NAME_TOKENS = NAME_PLACEHOLDERS
_RESUME_JUNK_RE = re.compile(
    r"^(page\s*\d+(\s*(of|/)\s*\d+)?|\d+\s*(of|/)\s*\d+|confidential|"
    r"references available.*|curriculum vitae|r[ée]sum[ée]|resume)$",
    re.IGNORECASE,
)


def _display_name(profile: Profile) -> Optional[str]:
    """A presentable name, dropping parser placeholders like 'Unknown'/'Provider'."""
    parts = []
    for part in (profile.first_name, profile.last_name):
        p = (part or "").strip()
        if p and p.lower().strip(".") not in _BAD_NAME_TOKENS:
            parts.append(p)
    return " ".join(parts) or None


def _clean_resume_line(raw: str, drop: set) -> Optional[str]:
    """Clean one résumé line for display; return None to drop obvious junk."""
    ln = unescape(raw)
    ln = (ln.replace("•", " ").replace(" ", " ").replace("�", "")
            .replace("â€¢", " ").replace("â€“", "-")
            .replace("â€”", "-").replace("â€™", "'"))
    ln = re.sub(r"[-]", " ", ln)
    ln = re.sub(r"^[\s•·\-–—*|>]+", "", ln)
    ln = re.sub(r"\s+", " ", ln).strip(" 	|;,")
    if len(ln) < 2 or _RESUME_JUNK_RE.match(ln):
        return None
    if not re.search(r"[A-Za-z0-9]", ln):
        return None
    low = ln.lower()
    # Drop contact-footer lines that repeat the header's email/phone.
    if any(d and d in low for d in drop):
        return None
    return ln


def _render_uniform_resume(profile: Profile, lines: list[str]) -> str:
    """Render EVERY résumé in one standardized layout: a header built from the
    profile's structured fields + the file's content grouped under canonical
    sections in a fixed order. Source files vary; the output never does.

    Lines are cleaned (mojibake / bullets / junk removed) and HTML-escaped, and
    wrapped only in our own tags — so no markup injection and no download link.
    """
    e = escape
    role = " · ".join(b for b in (profile.provider_category, profile.specialty) if b)
    name = _display_name(profile)
    cred = (profile.profession_type or "").strip()
    if name:
        title = f"{name}, {cred}" if cred and cred.upper() not in name.upper() else name
    else:
        title = role or "Healthcare Provider"
    loc = ", ".join(b for b in (profile.city, profile.state_code) if b)

    meta = []
    if loc:
        meta.append(f'<i class="fas fa-location-dot"></i> {e(loc)}')
    if profile.years_experience:
        meta.append(f'{profile.years_experience} yrs experience')
    if getattr(profile, "npi_number", None):
        meta.append(f'NPI {e(profile.npi_number)}')
    contact = []
    if profile.email:
        contact.append(f'<i class="fas fa-envelope"></i> {e(profile.email)}')
    if profile.phone:
        contact.append(f'<i class="fas fa-phone"></i> {e(profile.phone)}')

    head = ['<div class="hb-r-head">', f'<h2 class="hb-r-name">{e(title)}</h2>']
    if role:
        head.append(f'<div class="hb-r-role">{e(role)}</div>')
    if meta:
        head.append(f'<div class="hb-r-meta">{"  ·  ".join(meta)}</div>')
    if contact:
        head.append(f'<div class="hb-r-meta">{"  ·  ".join(contact)}</div>')
    if profile.american_board:
        head.append(f'<div class="hb-r-board"><i class="fas fa-award"></i> {e(profile.american_board)}</div>')
    head.append('</div>')

    # Contact values already shown in the header — don't repeat them in the body.
    drop = {v.strip().lower() for v in (profile.email, profile.phone) if v}

    # Group the (cleaned) body into canonical sections; everything before the
    # first heading is preamble (name/specialty/location) already in the header.
    sections: "OrderedDict[str, list[str]]" = OrderedDict()
    current, started = None, False
    for raw in lines:
        ln = _clean_resume_line(raw, drop)
        if not ln:
            continue
        if _is_resume_heading(ln):
            current = _canon_section(ln)
            sections.setdefault(current, [])
            started = True
            continue
        if not started:
            continue
        sections[current].append(ln)

    body: list[str] = []
    if not sections:
        # No recognizable headings — show the cleaned content uniformly, dropping
        # the leading name line which the header already carries.
        tail = [c for c in (_clean_resume_line(x, drop) for x in lines) if c]
        if tail and _looks_like_resume_name(tail[0]):
            tail = tail[1:]
        body = [f'<p>{e(x)}</p>' for x in tail]
    else:
        order = [c for c, _ in _CANON_SECTIONS]
        for canon in order + [c for c in sections if c not in order]:
            ls = sections.get(canon)
            if not ls:
                continue
            body.append(f'<h3 class="hb-r-sec">{e(canon)}</h3>')
            body.extend(f'<p>{e(x)}</p>' for x in ls)

    return "\n".join(head) + '<div class="hb-r-body">' + "\n".join(body) + '</div>'


def _provider_conditions(
    db, *, q=None, providers_only=False, specialty=None, license_title=None,
    profession_type=None, state_code=None, city=None, zip=None, radius_mi=None,
    american_board=None, open_to_work=None, min_experience=None,
    max_experience=None, contact_available=None,
) -> list:
    """Every Providers-directory filter EXCEPT the category selection, as a list
    of WHERE conditions — shared by the search and the faceted category counts,
    so the tab counts always reflect the same filters as the results."""
    conds = []
    if providers_only:
        conds.append(Profile.is_listable.is_(True))
    if q:
        conds.append(Profile.search_text.like(f"%{q.lower()}%"))
    if specialty:
        conds.append(Profile.specialty == specialty)
    if profession_type:
        conds.append(Profile.profession_type == profession_type)
    if license_title and license_title.strip():
        title = license_title.strip()
        conds.append(or_(
            func.lower(Profile.profession_type) == title.lower(),
            Profile.licenses.any(func.lower(License.license_type) == title.lower()),
        ))
    if state_code:
        conds.append(Profile.state_code == state_code.upper())
    if city:
        conds.append(func.lower(Profile.city).like(f"{city.strip().lower()}%"))
    if zip and radius_mi:
        center = _geocode_zip(db, zip)
        if center is None:
            conds.append(literal(False))
        else:
            clat, clng = center
            radius_m = radius_mi * 1609.344
            ec = func.ll_to_earth(clat, clng)
            ep = func.ll_to_earth(Profile.lat, Profile.lng)
            conds.append(Profile.lat.isnot(None))
            conds.append(func.earth_box(ec, radius_m).op("@>")(ep))
            conds.append(func.earth_distance(ec, ep) <= radius_m)
    if american_board:
        conds.append(Profile.american_board == american_board)
    if open_to_work is not None:
        conds.append(Profile.open_to_work.is_(open_to_work))
    if min_experience is not None:
        conds.append(Profile.years_experience >= min_experience)
    if max_experience is not None:
        conds.append(Profile.years_experience <= max_experience)
    if contact_available:
        cf = contact_available.strip().lower()
        has_email = Profile.email.isnot(None) & (func.length(func.trim(Profile.email)) > 0)
        has_phone = Profile.phone.isnot(None) & (func.length(func.trim(Profile.phone)) > 0)
        if cf == "any":
            conds.append(or_(has_email, has_phone))
        elif cf == "both":
            conds.append(and_(has_email, has_phone))
        elif cf == "email":
            conds.append(has_email)
        elif cf == "phone":
            conds.append(has_phone)
        elif cf == "missing":
            conds.append(~or_(has_email, has_phone))
    return conds


_PROVIDER_CATS = ["Physicians", "Nursing", "Allied", "APP"]


@router.get("", response_model=Page[ProfileOut])
def search_profiles(
    db: DbSession,
    user: CurrentUser,
    q: Optional[str] = Query(None, description="Full-text search"),
    category: Optional[str] = Query(None, description="Physicians|Nursing|Allied|APP"),
    providers_only: bool = Query(False, description="Only classified providers (exclude uncategorised)"),
    specialty: Optional[str] = None,
    license_title: Optional[str] = Query(None, description="License/title such as RN, MD, NP, PA"),
    profession_type: Optional[str] = None,
    state_code: Optional[str] = None,
    city: Optional[str] = None,
    zip: Optional[str] = Query(None, description="Center ZIP for a radius search"),
    radius_mi: Optional[float] = Query(None, ge=1, le=500, description="Miles from the ZIP"),
    american_board: Optional[str] = None,
    open_to_work: Optional[bool] = None,
    min_experience: Optional[int] = Query(None, ge=0),
    max_experience: Optional[int] = Query(None, ge=0),
    contact_available: Optional[str] = Query(
        None, description="any|both|email|phone|missing"
    ),
    count: bool = Query(True, description="Include exact total (skip on huge result sets)"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    _require_provider_directory_access(user)
    conds = _provider_conditions(
        db, q=q, providers_only=providers_only, specialty=specialty,
        license_title=license_title, profession_type=profession_type,
        state_code=state_code, city=city, zip=zip, radius_mi=radius_mi,
        american_board=american_board, open_to_work=open_to_work,
        min_experience=min_experience, max_experience=max_experience,
        contact_available=contact_available)
    stmt = select(Profile).where(*conds)
    if category:
        stmt = stmt.where(func.lower(Profile.provider_category) == category.lower())
    elif providers_only:
        stmt = stmt.where(Profile.provider_category.in_(_PROVIDER_CATS))

    total = None
    if count:
        total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    # Fetch one extra row to know if there's a next page without a full COUNT.
    # Sort has a unique tiebreaker (profile_id) so pages never skip/duplicate.
    rows = db.scalars(
        stmt.order_by(Profile.completion_score.desc(), Profile.profile_id)
        .limit(limit + 1).offset(offset)
    ).all()
    has_next = len(rows) > limit
    return Page(items=rows[:limit], total=total, limit=limit, offset=offset,
                has_next=has_next)


@router.get("/category-counts")
def category_counts(
    db: DbSession,
    user: CurrentUser,
    q: Optional[str] = None,
    specialty: Optional[str] = None,
    license_title: Optional[str] = None,
    profession_type: Optional[str] = None,
    state_code: Optional[str] = None,
    city: Optional[str] = None,
    zip: Optional[str] = None,
    radius_mi: Optional[float] = Query(None, ge=1, le=500),
    american_board: Optional[str] = None,
    open_to_work: Optional[bool] = None,
    min_experience: Optional[int] = Query(None, ge=0),
    max_experience: Optional[int] = Query(None, ge=0),
    contact_available: Optional[str] = None,
):
    """Provider counts per category for the CURRENT filters, so the tab numbers
    (Physicians / Nursing / Allied / APP) reflect the applied filters."""
    _require_provider_directory_access(user)
    conds = _provider_conditions(
        db, providers_only=True, q=q, specialty=specialty,
        license_title=license_title, profession_type=profession_type,
        state_code=state_code, city=city, zip=zip, radius_mi=radius_mi,
        american_board=american_board, open_to_work=open_to_work,
        min_experience=min_experience, max_experience=max_experience,
        contact_available=contact_available)
    rows = db.execute(
        select(Profile.provider_category, func.count())
        .where(*conds, Profile.provider_category.in_(_PROVIDER_CATS))
        .group_by(Profile.provider_category)
    ).all()
    return {cat: 0 for cat in _PROVIDER_CATS} | {c: n for c, n in rows}


_FACETS_CACHE: dict = {"data": None, "at": 0.0}
_FACETS_TTL = 300.0  # seconds — distinct/group-by over millions of rows is costly


@router.get("/facets")
def profile_facets(user: CurrentUser, db: DbSession):
    """Distinct filter values + category counts for the Providers directory.

    Cached in-process for a few minutes: on a multi-million-row table these
    DISTINCT / GROUP BY scans are expensive, and the values change slowly.
    """
    _require_provider_directory_access(user)
    now = time.monotonic()
    if _FACETS_CACHE["data"] is not None and now - _FACETS_CACHE["at"] < _FACETS_TTL:
        return _FACETS_CACHE["data"]

    def distinct(col, exclude=None, *, limit=300, max_len=100):
        rows = db.scalars(
            select(col).where(col.isnot(None)).distinct().order_by(col).limit(limit)
        ).all()
        vals = sorted({str(v).strip() for v in rows if str(v).strip()})
        vals = [v for v in vals if len(v) <= max_len]
        if exclude:
            ex = {e.lower() for e in exclude}
            vals = [v for v in vals if v.lower() not in ex]
        return vals

    license_titles = sorted({
        *distinct(Profile.profession_type),
        *distinct(License.license_type),
    })

    cat_rows = db.execute(
        select(Profile.provider_category, func.count())
        .where(Profile.is_listable.is_(True))
        .group_by(Profile.provider_category)
    ).all()
    categories = {(c or "Other"): n for c, n in cat_rows}
    data = {
        "categories": categories,
        "license_titles": license_titles,
        "professions": distinct(Profile.profession_type),
        "states": distinct(Profile.state_code),
        "cities": distinct(Profile.city, limit=250, max_len=80),
        "specialties": distinct(Profile.specialty),
        # American boards, excluding American Board of Physician Specialties.
        "boards": distinct(Profile.american_board,
                           exclude=["American Board of Physician Specialties"],
                           max_len=150),
    }
    _FACETS_CACHE["data"] = data
    _FACETS_CACHE["at"] = now
    return data


@router.get("/me", response_model=ProfileDetail)
def my_profile(user: CurrentUser, db: DbSession):
    profile = db.scalar(
        select(Profile)
        .options(
            selectinload(Profile.licenses),
            selectinload(Profile.certifications),
            selectinload(Profile.work_history),
            selectinload(Profile.skills),
        )
        .where(Profile.user_id == user.user_id)
    )
    if not profile:
        raise HTTPException(status_code=404, detail="No profile for this account")
    return profile


@router.post("", response_model=ProfileOut, status_code=status.HTTP_201_CREATED)
def create_profile(body: ProfileCreate, user: CurrentUser, db: DbSession):
    existing = db.scalar(select(Profile).where(Profile.user_id == user.user_id))
    if existing:
        raise HTTPException(status_code=409, detail="Profile already exists; use PATCH")
    profile = Profile(user_id=user.user_id, **body.model_dump())
    profile.rebuild_search_text()
    profile.completion_score = _compute_completion(profile)
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


@router.get("/{profile_id}", response_model=ProfileDetail)
def get_profile(profile_id: str, db: DbSession, user: CurrentUser):
    profile = db.scalar(
        select(Profile)
        .options(
            selectinload(Profile.licenses),
            selectinload(Profile.certifications),
            selectinload(Profile.work_history),
            selectinload(Profile.skills),
        )
        .where(Profile.profile_id == profile_id)
    )
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    return profile


def _group_resume_sections(lines: list[str], drop: set) -> "OrderedDict[str, list[str]]":
    """Clean résumé lines and group them under canonical section headings, in a
    fixed order — so every résumé renders in the same tabbed structure."""
    sections: "OrderedDict[str, list[str]]" = OrderedDict()
    current, started = None, False
    for raw in lines:
        ln = _clean_resume_line(raw, drop)
        if not ln:
            continue
        if _is_resume_heading(ln):
            current = _canon_section(ln)
            sections.setdefault(current, [])
            started = True
            continue
        if not started:
            continue
        sections[current].append(ln)
    if not sections:   # no recognizable headings — put content under Overview
        tail = [c for c in (_clean_resume_line(x, drop) for x in lines) if c]
        if tail and _looks_like_resume_name(tail[0]):
            tail = tail[1:]
        if tail:
            sections["Professional Summary"] = tail
    ordered: "OrderedDict[str, list[str]]" = OrderedDict()
    for canon, _ in _CANON_SECTIONS:
        if sections.get(canon):
            ordered[canon] = sections[canon]
    for k, v in sections.items():
        if k not in ordered and v:
            ordered[k] = v
    return ordered


@router.post("/{profile_id}/contact-release")
def release_profile_contact(
    profile_id: str,
    request: Request,
    user: CurrentUser,
    db: DbSession,
):
    """Deliberately reveal provider contact details and record the action."""
    _require_provider_directory_access(user)
    profile = db.get(Profile, profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")

    now = utcnow()
    db.add(
        AuditLog(
            actor_user_id=user.user_id,
            action="provider_contact_released",
            entity_type="profile",
            entity_id=profile.profile_id,
            meta={
                "email_available": bool(profile.email),
                "phone_available": bool(profile.phone),
            },
            ip_address=request.client.host if request.client else None,
        )
    )
    db.commit()

    return {
        "profile_id": profile.profile_id,
        "email": profile.email,
        "phone": profile.phone,
        "released_at": now.isoformat(),
        "released_by_email": user.email,
    }


@router.get("/{profile_id}/resume")
def view_resume(profile_id: str, user: CurrentUser, db: DbSession):
    """Return a candidate's résumé as safe, view-only structured data (no download)
    — grouped into sections + a skills list so the UI can show it in tabs."""
    profile = db.scalar(
        select(Profile)
        .options(selectinload(Profile.skills), selectinload(Profile.certifications),
                 selectinload(Profile.licenses))
        .where(Profile.profile_id == profile_id)
    )
    if not profile or not profile.resume_url:
        raise HTTPException(status_code=404, detail="No résumé on file")
    if profile.user_id != user.user_id and not _is_recruiter_or_admin(user):
        raise HTTPException(status_code=403, detail="Providers are available to recruiters only")
    can_view_contact = profile.user_id == user.user_id
    try:
        lines = _resume_lines(profile.resume_url)
    except Exception:  # noqa: BLE001 — fall back to a header-only view
        lines = []

    drop = {v.strip().lower() for v in (profile.email, profile.phone) if v}
    sections = _group_resume_sections(lines, drop)

    # Skill chips: specialty + parsed skills + certifications, de-duplicated.
    skills, seen = [], set()
    for s in ([profile.specialty] + [sk.name for sk in profile.skills]
              + [c.cert_name for c in profile.certifications]):
        s = (s or "").strip()
        if s and 2 <= len(s) <= 48 and s.lower() not in seen:
            seen.add(s.lower())
            skills.append(s)

    role = " · ".join(b for b in (profile.provider_category, profile.specialty) if b)
    return {
        "name": _display_name(profile) or "Provider résumé",
        "credential": profile.profession_type,
        "role": role,
        "location": ", ".join(b for b in (profile.city, profile.state_code) if b),
        "email": profile.email if can_view_contact else None,
        "phone": profile.phone if can_view_contact else None,
        "board": profile.american_board,
        "years_experience": profile.years_experience,
        "sections": sections,
        "skills": skills[:20],
    }


@router.patch("/{profile_id}", response_model=ProfileOut)
def update_profile(profile_id: str, body: ProfileUpdate, user: CurrentUser, db: DbSession):
    profile = _get_owned_profile(db, profile_id, user)
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(profile, field, value)
    profile.rebuild_search_text()
    profile.completion_score = _compute_completion(profile)
    db.commit()
    db.refresh(profile)
    return profile


@router.patch("/{profile_id}/contact", response_model=ProfileOut)
def update_profile_contact(
    profile_id: str,
    body: ProfileContactUpdate,
    user: CurrentUser,
    db: DbSession,
):
    """Update contact details from the Providers tab.

    Recruiters/admins can add or overwrite provider email/phone values. Each
    change records who last edited the contact data for recruiter visibility.
    """
    _require_provider_directory_access(user)
    profile = db.get(Profile, profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")

    updates = body.model_dump(exclude_unset=True)
    changed = False
    for field in ("email", "phone"):
        if field not in updates:
            continue
        value = (updates[field] or "").strip() or None
        if field == "email" and value:
            value = value[:255]
        if field == "phone" and value:
            value = value[:30]
        current = getattr(profile, field)
        if current != value:
            setattr(profile, field, value)
            changed = True

    if changed:
        profile.contact_updated_by_user_id = user.user_id
        profile.contact_updated_by_email = user.email
        profile.contact_updated_at = utcnow()
        profile.rebuild_search_text()
        profile.completion_score = _compute_completion(profile)
        db.commit()
        db.refresh(profile)
    return profile


# --- Licenses -------------------------------------------------------------

@router.post("/{profile_id}/licenses", response_model=LicenseOut, status_code=201)
def add_license(profile_id: str, body: LicenseCreate, user: CurrentUser, db: DbSession):
    _get_owned_profile(db, profile_id, user)
    lic = License(profile_id=profile_id, **body.model_dump())
    db.add(lic)
    db.commit()
    db.refresh(lic)
    return lic


@router.delete("/{profile_id}/licenses/{license_id}", status_code=204)
def delete_license(profile_id: str, license_id: str, user: CurrentUser, db: DbSession):
    _get_owned_profile(db, profile_id, user)
    lic = db.get(License, license_id)
    if lic and lic.profile_id == profile_id:
        db.delete(lic)
        db.commit()


# --- Certifications -------------------------------------------------------

@router.post("/{profile_id}/certifications", response_model=CertificationOut, status_code=201)
def add_certification(profile_id: str, body: CertificationCreate, user: CurrentUser, db: DbSession):
    _get_owned_profile(db, profile_id, user)
    cert = Certification(profile_id=profile_id, **body.model_dump())
    db.add(cert)
    db.commit()
    db.refresh(cert)
    return cert


@router.delete("/{profile_id}/certifications/{cert_id}", status_code=204)
def delete_certification(profile_id: str, cert_id: str, user: CurrentUser, db: DbSession):
    _get_owned_profile(db, profile_id, user)
    cert = db.get(Certification, cert_id)
    if cert and cert.profile_id == profile_id:
        db.delete(cert)
        db.commit()


# --- Work history ---------------------------------------------------------

@router.post("/{profile_id}/work-history", response_model=WorkHistoryOut, status_code=201)
def add_work_history(profile_id: str, body: WorkHistoryCreate, user: CurrentUser, db: DbSession):
    _get_owned_profile(db, profile_id, user)
    wh = WorkHistory(profile_id=profile_id, **body.model_dump())
    db.add(wh)
    db.commit()
    db.refresh(wh)
    return wh


@router.delete("/{profile_id}/work-history/{work_id}", status_code=204)
def delete_work_history(profile_id: str, work_id: str, user: CurrentUser, db: DbSession):
    _get_owned_profile(db, profile_id, user)
    wh = db.get(WorkHistory, work_id)
    if wh and wh.profile_id == profile_id:
        db.delete(wh)
        db.commit()


# --- Skills ---------------------------------------------------------------

@router.post("/{profile_id}/skills", response_model=SkillOut, status_code=201)
def add_skill(profile_id: str, body: SkillCreate, user: CurrentUser, db: DbSession):
    profile = _get_owned_profile(db, profile_id, user)
    skill = ProfileSkill(profile_id=profile_id, **body.model_dump())
    db.add(skill)
    profile.rebuild_search_text()
    db.commit()
    db.refresh(skill)
    return skill


@router.delete("/{profile_id}/skills/{skill_id}", status_code=204)
def delete_skill(profile_id: str, skill_id: str, user: CurrentUser, db: DbSession):
    _get_owned_profile(db, profile_id, user)
    skill = db.get(ProfileSkill, skill_id)
    if skill and skill.profile_id == profile_id:
        db.delete(skill)
        db.commit()
