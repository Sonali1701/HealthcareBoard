"""Healthcare profile endpoints + nested licenses, certs, work history, skills."""
from __future__ import annotations

import io
import json
import re
import time
from collections import OrderedDict
from html import escape, unescape
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel
from sqlalchemy import and_, func, literal, or_, select, text as sa_text
from sqlalchemy.orm import selectinload

from ..config import settings
from ..database import utcnow
from ..deps import CurrentUser, DbSession
from ..importers.parsing import (
    NAME_PLACEHOLDERS,
    SECTION_HEADERS,
    classify_provider,
    is_real_name,
)
from ..services import storage
from ..models import (
    AuditLog,
    Certification,
    License,
    Profile,
    ProfileSkill,
    WorkHistory,
)
from ..models.enums import LicenseStatus
from ..schemas.common import Page
from ..schemas.profile import (
    CertificationCreate,
    CertificationOut,
    LicenseCreate,
    LicenseOut,
    ProfileCreate,
    ProfileCardOut,
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


class CopilotQuery(BaseModel):
    message: str
    # Filters from the previous turn, so a follow-up refines instead of restarting
    # ("RN nurses" → then "only in California"). Client-supplied, so re-validated.
    context: Optional[dict] = None


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


def _geocode_city(db, city: str, state_code: str | None = None):
    """Look up a city's centroid (lat, lng) from the city_centroids table.

    Prefers an exact city+state match; falls back to the city in any state so a
    bare "50 miles from Folsom" still resolves (to one representative Folsom)."""
    c = (city or "").strip().lower()
    if not c:
        return None
    if state_code:
        row = db.execute(sa_text(
            "SELECT lat, lng FROM city_centroids "
            "WHERE city_lower = :c AND state_code = :s"),
            {"c": c, "s": state_code.strip().upper()}).first()
        if row and row[0] is not None:
            return (row[0], row[1])
    row = db.execute(sa_text(
        "SELECT lat, lng FROM city_centroids WHERE city_lower = :c "
        "ORDER BY state_code LIMIT 1"), {"c": c}).first()
    return (row[0], row[1]) if row and row[0] is not None else None


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
    # Common real-world headings. Listed explicitly rather than inferred from
    # shape, because a bare Title Case line is usually a job title
    # ("Registered Nurse"), not a section heading.
    "specialty",
    "specialty information",
    "additional information",
    "areas of expertise",
    "core competencies",
    "professional profile",
    "employment history",
    "employment",
    "work history",
    "objective",
    "profile",
    "qualifications",
    "professional summary",
    "languages",
    "references",
    "awards",
    "honors",
    "publications",
    "training",
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


def _deglue(text: str) -> str:
    """Split run-together words: 'SpecialtyInformation' -> 'Specialty Information'.

    PDFs that lose their space glyphs turn headings into single tokens, which
    otherwise hides them from the heading match below (and dumps the whole
    résumé into whatever section came before).
    """
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", text)


def _norm_heading(text: str) -> str:
    """Normalise a heading for comparison: letters only, '&' as a word break."""
    cleaned = re.sub(r"[^A-Za-z& ]", " ", text).replace("&", " ")
    return re.sub(r"\s+", " ", cleaned).strip().lower()


def _is_resume_heading(line: str) -> bool:
    raw = (line or "").strip()
    if not raw:
        return False
    # Try the line as-is and de-glued, so headings survive lost PDF spacing.
    for candidate in (raw, _deglue(raw)):
        if _norm_heading(candidate) in _ALL_SECTION_ALIASES:
            return True
    return raw.isupper() and 3 <= len(raw) <= 45 and sum(ch.isdigit() for ch in raw) == 0


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


# Every heading spelling we recognise, normalised once for O(1) lookup.
_ALL_SECTION_ALIASES = frozenset(
    _norm_heading(alias)
    for alias in (
        *(a for _, aliases in _CANON_SECTIONS for a in aliases),
        *_RESUME_EXTRA_HEADERS,
        *SECTION_HEADERS,
    )
)


def _canon_section(heading_line: str) -> str:
    """Map any résumé heading to a canonical section name (or Title-case it)."""
    for candidate in (heading_line, _deglue(heading_line)):
        key = _norm_heading(candidate)
        if not key:
            continue
        for canon, aliases in _CANON_SECTIONS:
            if key in aliases or any(key.startswith(a) or a in key for a in aliases):
                return canon
    return _deglue(heading_line.strip()).title()


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


# --- Identity masking ------------------------------------------------------
# A provider's name and contact are withheld until the recruiter deliberately
# releases the profile. The release is recorded in audit_logs, so that table is
# also the source of truth for "has this user already unlocked this profile?" —
# which means a release survives logout and can be attributed to a person.

RELEASE_ACTION = "provider_contact_released"


def _initials(profile: Profile) -> str:
    parts = [(profile.first_name or "").strip(), (profile.last_name or "").strip()]
    return "".join(p[0].upper() for p in parts if p) or "?"


def _masked_name(profile: Profile) -> str:
    """'Ta'Nyah Hoskins' -> 'T. H.' — enough to tell rows apart, not to identify."""
    parts = [(profile.first_name or "").strip(), (profile.last_name or "").strip()]
    out = [f"{p[0].upper()}." for p in parts if p]
    return " ".join(out) or "—"


def _released_profile_ids(db, user, profile_ids: list[str]) -> set[str]:
    """Which of these profiles has this user already released?"""
    ids = [p for p in profile_ids if p]
    if not ids:
        return set()
    rows = db.scalars(
        select(AuditLog.entity_id).where(
            AuditLog.actor_user_id == user.user_id,
            AuditLog.action == RELEASE_ACTION,
            AuditLog.entity_id.in_(ids),
        )
    ).all()
    return {r for r in rows if r}


def _may_see_identity(db, user, profile: Profile) -> bool:
    """True when the caller is entitled to this provider's real name/contact."""
    if profile.user_id and profile.user_id == user.user_id:
        return True                       # your own profile
    return bool(_released_profile_ids(db, user, [profile.profile_id]))


def _profile_card(profile: Profile, *, released: bool) -> dict:
    """Serialise a directory row, including identity only when released."""
    card = {
        "profile_id": profile.profile_id,
        "masked_name": _masked_name(profile),
        "initials": _initials(profile),
        "is_released": released,
        "has_email": bool(profile.email),
        "has_phone": bool(profile.phone),
        "headline": profile.headline,
        "specialty": profile.specialty,
        "profession_type": profile.profession_type,
        "provider_category": profile.provider_category,
        "american_board": profile.american_board,
        "years_experience": profile.years_experience or 0,
        "city": profile.city,
        "state_code": profile.state_code,
        "completion_score": profile.completion_score or 0,
    }
    if released:
        card.update(
            first_name=profile.first_name,
            last_name=profile.last_name,
            email=profile.email,
            phone=profile.phone,
            contact_updated_by_email=profile.contact_updated_by_email,
        )
    return card


def _redact_name(text: str, profile: Profile) -> str:
    """Replace the candidate's own name inside résumé text with initials."""
    tokens = [t for t in ((profile.first_name or "").strip(),
                          (profile.last_name or "").strip()) if len(t) >= 3]
    if not tokens:
        return text
    pattern = "|".join(re.escape(t) for t in tokens)
    return re.sub(rf"(?<!\w)({pattern})(?!\w)",
                  lambda m: m.group(1)[0].upper() + ".", text, flags=re.IGNORECASE)


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


# Nurse Licensure Compact (eNLC) member states — a compact/multistate license
# grants a practice privilege in ALL of these, so "who can work in <state>"
# includes every compact-license holder when the state is a member.
_COMPACT_STATES = {
    "AL","AZ","AR","CO","DE","FL","GA","GU","ID","IN","IA","KS","KY","LA","ME",
    "MD","MS","MO","MT","NE","NH","NJ","NM","NC","ND","OH","OK","PA","SC","SD",
    "TN","TX","UT","VT","VA","WA","WV","WI","WY",
}


def _provider_conditions(
    db, *, q=None, providers_only=False, specialty=None, license_title=None,
    profession_type=None, state_code=None, city=None, zip=None, radius_mi=None,
    american_board=None, open_to_work=None, min_experience=None,
    max_experience=None, contact_available=None, compact=None, licensed_state=None,
    worked_at=None, travel_experience=None,
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
    if compact:
        # Holds at least one compact / multistate (eNLC) license.
        conds.append(Profile.licenses.any(License.is_compact.is_(True)))
    if licensed_state:
        # "Can work in <state>": licensed directly there, OR — if it is a compact
        # member state — holds a compact license that grants a privilege there.
        ls = licensed_state.strip().upper()
        cond = Profile.licenses.any(func.upper(License.state_code) == ls)
        if ls in _COMPACT_STATES:
            cond = or_(cond, Profile.licenses.any(License.is_compact.is_(True)))
        conds.append(cond)
    if worked_at and worked_at.strip():
        # Has a past role at this employer / health system (résumé work history).
        emp = worked_at.strip().lower()
        conds.append(Profile.work_history.any(
            func.lower(WorkHistory.employer_name).like(f"%{emp}%")))
    if travel_experience:
        conds.append(Profile.work_history.any(
            WorkHistory.employment_type == "travel"))
    if state_code:
        conds.append(Profile.state_code == state_code.upper())
    # Location: a radius search centres on a ZIP or a city; without a radius,
    # `city` is a plain prefix match.
    if radius_mi and (zip or city):
        center = _geocode_zip(db, zip) if zip else _geocode_city(db, city, state_code)
        if center is None:
            # Unknown ZIP is a hard miss; an unknown city degrades to a name match
            # rather than silently returning zero results.
            if city:
                conds.append(func.lower(Profile.city).like(f"{city.strip().lower()}%"))
            else:
                conds.append(literal(False))
        else:
            clat, clng = center
            radius_m = radius_mi * 1609.344
            ec = func.ll_to_earth(clat, clng)
            ep = func.ll_to_earth(Profile.lat, Profile.lng)
            conds.append(Profile.lat.isnot(None))
            conds.append(func.earth_box(ec, radius_m).op("@>")(ep))
            conds.append(func.earth_distance(ec, ep) <= radius_m)
    elif city:
        conds.append(func.lower(Profile.city).like(f"{city.strip().lower()}%"))
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


_PROVIDER_CATS = ["Physicians", "Nursing", "Allied", "APP", "Others"]


@router.get("", response_model=Page[ProfileCardOut])
def search_profiles(
    db: DbSession,
    user: CurrentUser,
    q: Optional[str] = Query(None, description="Full-text search"),
    category: Optional[str] = Query(None, description="Physicians|Nursing|Allied|APP|Others"),
    providers_only: bool = Query(False, description="Only listable provider profiles"),
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
    compact: Optional[bool] = Query(None, description="Only providers with a compact/multistate license"),
    licensed_state: Optional[str] = Query(None, description="Can legally work in this state (licensed there, or compact)"),
    worked_at: Optional[str] = Query(None, description="Has a past role at this employer/health system"),
    travel_experience: Optional[bool] = Query(None, description="Has prior travel assignments"),
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
        contact_available=contact_available, compact=compact,
        licensed_state=licensed_state, worked_at=worked_at,
        travel_experience=travel_experience)
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
    page_rows = rows[:limit]
    # Identity is withheld unless this recruiter already released the profile
    # (or it is their own) — one extra lookup for the whole page, not per row.
    released = _released_profile_ids(db, user, [r.profile_id for r in page_rows])
    items = [
        _profile_card(
            r,
            released=(r.profile_id in released
                      or bool(r.user_id and r.user_id == user.user_id)),
        )
        for r in page_rows
    ]
    return Page(items=items, total=total, limit=limit, offset=offset,
                has_next=has_next)


# --- AI copilot ------------------------------------------------------------
# A recruiter types a request in plain English; an LLM turns it into the SAME
# structured filters the directory already supports, and we run the SAME query
# (so results inherit the identity masking). The model never sees provider data
# and never writes SQL — it only fills a small, validated JSON of filters.

_COPILOT_CATS = {"physicians": "Physicians", "nursing": "Nursing",
                 "allied": "Allied", "app": "APP", "others": "Others"}
_COPILOT_LICENSES = {"RN", "MD", "NP", "PA", "LPN", "CNA", "PT", "DO", "CRNA",
                     "RT", "OT", "PHARMD", "CNM", "DNP", "FNP", "LVN", "LCSW"}
# US state + territory codes, so "California" -> "CA" is validated, not trusted.
_US_STATES = {
    "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN","IA",
    "KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ",
    "NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN","TX","UT","VT",
    "VA","WA","WV","WI","WY","DC","PR","GU","VI",
}

_COPILOT_SYSTEM = (
    "You convert a healthcare recruiter's natural-language request into search "
    "filters for a provider database. Respond with ONLY one JSON object, no prose."
)
_COPILOT_INSTR = (
    "Return JSON with exactly these keys (use null when the request does not "
    "specify one):\n"
    '{"q":null,"category":null,"license_title":null,"state_code":null,'
    '"city":null,"radius_mi":null,"compact":null,"min_experience":null,'
    '"max_experience":null,"contact_available":null}\n\n'
    "- category: one of \"Physicians\",\"Nursing\",\"Allied\",\"APP\",\"Others\". "
    "Nurse/RN/LPN/CNA -> Nursing. Doctor/physician/MD/DO -> Physicians. "
    "NP/PA/CRNA/nurse practitioner -> APP. Therapist/PT/OT/RT/tech/allied -> "
    "Allied. null if unclear.\n"
    "- license_title: a credential ONLY if named: RN, MD, NP, PA, LPN, CNA, PT, "
    "DO, CRNA, RT, OT, PharmD. Else null.\n"
    "- state_code: where the candidate LIVES — 2-letter US code (California -> "
    "CA). null if none named.\n"
    "- licensed_state: where they can WORK / are licensed, when the request says "
    "'licensed in', 'can work in', 'eligible to work in' a state. 2-letter code. "
    "null otherwise.\n"
    "- city: the CENTRE city for the search, name only, no state (e.g. 'around "
    "Folsom, California' -> \"Folsom\"). null if none.\n"
    "- radius_mi: whole miles when a distance is given ('within 50 miles of', "
    "'around 25 mi from'). null if none.\n"
    "- compact: true when they ask for a compact / multistate / eNLC nursing "
    "license; else null.\n"
    "- worked_at: an employer / health-system name when they ask for someone who "
    "'worked at', 'has experience at', or 'was employed by' it (e.g. 'worked at "
    "Kaiser' -> \"Kaiser\"). null otherwise.\n"
    "- travel_experience: true when they ask for prior travel assignments / "
    "travel experience; else null.\n"
    "- min_experience / max_experience: whole years. 'more than 5 years' -> "
    "min_experience 5. 'at least 3' -> min 3. 'under 2' -> max_experience 2.\n"
    "- contact_available: \"any\" if they ask for reachable candidates / with "
    "contact info; else null.\n"
    "- q: any remaining descriptive keywords (specialty like 'ICU','telemetry', "
    "'med-surg'; qualifiers like 'compact license','bilingual'; certifications). "
    "Lowercase, space-separated. null if none.\n\n"
    "Examples:\n"
    "'Find ICU nurses in California with compact licenses' -> "
    '{"q":"icu compact license","category":"Nursing","license_title":null,'
    '"state_code":"CA","city":null,"radius_mi":null,"min_experience":null,'
    '"max_experience":null,"contact_available":null}\n'
    "'RN ICU around 50 miles from Folsom California' -> "
    '{"q":"icu","category":"Nursing","license_title":"RN","state_code":"CA",'
    '"city":"Folsom","radius_mi":50,"min_experience":null,"max_experience":null,'
    '"contact_available":null}'
)


_STATE_NAMES = {
    "alabama":"AL","alaska":"AK","arizona":"AZ","arkansas":"AR","california":"CA",
    "colorado":"CO","connecticut":"CT","delaware":"DE","florida":"FL","georgia":"GA",
    "hawaii":"HI","idaho":"ID","illinois":"IL","indiana":"IN","iowa":"IA","kansas":"KS",
    "kentucky":"KY","louisiana":"LA","maine":"ME","maryland":"MD","massachusetts":"MA",
    "michigan":"MI","minnesota":"MN","mississippi":"MS","missouri":"MO","montana":"MT",
    "nebraska":"NE","nevada":"NV","new hampshire":"NH","new jersey":"NJ","new mexico":"NM",
    "new york":"NY","north carolina":"NC","north dakota":"ND","ohio":"OH","oklahoma":"OK",
    "oregon":"OR","pennsylvania":"PA","rhode island":"RI","south carolina":"SC",
    "south dakota":"SD","tennessee":"TN","texas":"TX","utah":"UT","vermont":"VT",
    "virginia":"VA","washington":"WA","west virginia":"WV","wisconsin":"WI","wyoming":"WY",
    "district of columbia":"DC","washington dc":"DC","puerto rico":"PR",
}
# Category cues, most specific first (so "nurse practitioner" -> APP, not Nursing).
_CATEGORY_CUES = [
    (("nurse practitioner", "physician assistant", "crna", "aprn"), "APP"),
    (("physician", "doctor", "surgeon"), "Physicians"),
    (("nurse", "nursing"), "Nursing"),
    (("therapist", "therapy", "technologist", "technician", "pathologist",
      "pharmacist", "dietitian", "paramedic"), "Allied"),
]
_STOP_TOKENS = {
    "find","show","me","us","get","list","all","any","the","a","an","in","with",
    "of","for","and","or","to","near","who","have","has","that","are","is","looking",
    "need","want","candidates","candidate","providers","provider","people","someone",
    "please","give","more","than","over","least","at","least","min","minimum","under",
    "less","below","years","year","yrs","experience","exp","located","based","around",
    "some","find","up","having",
    # Refinement / filler words, so a follow-up like "only profiles based in CA"
    # doesn't turn "only"/"profiles" into required search keywords.
    "only","just","profile","profiles","filter","narrow","refine","results","result",
    "them","these","those","it","now","also","instead","from","on","by","within",
    "state","city","area","region","location","jobs","job","role","roles","position",
    "positions","working","work","live","living","reside","residing","new","search",
}


def _set_center(out: dict, place: str) -> None:
    """Parse a captured location phrase ("folsom california", "austin tx") into a
    centre city (+ state), splitting a trailing state name or 2-letter code off."""
    toks = re.sub(r"\s+", " ", place).strip(" ,.-").split()
    st = None
    if len(toks) >= 3 and " ".join(toks[-2:]) in _STATE_NAMES:      # "new york"
        st = _STATE_NAMES[" ".join(toks[-2:])]; toks = toks[:-2]
    elif len(toks) >= 2 and toks[-1] in _STATE_NAMES:               # "california"
        st = _STATE_NAMES[toks[-1]]; toks = toks[:-1]
    elif len(toks) >= 2 and toks[-1].upper() in _US_STATES:         # "tx"
        st = toks[-1].upper(); toks = toks[:-1]
    city = " ".join(toks).strip()
    # A leftover bare state code (e.g. "New York NY" → state grabbed "new york",
    # leaving "ny") is a state, not a city.
    if len(city) == 2 and city.upper() in _US_STATES:
        out.setdefault("state_code", city.upper())
        city = ""
    if st and "state_code" not in out:
        out["state_code"] = st
    if city and len(city) >= 2 and city not in _STOP_TOKENS:
        out["city"] = city


def _copilot_rule_filters(message: str) -> dict:
    """Best-effort filter extraction without an LLM — keyword/regex based, so the
    copilot still works when the model is unavailable (down or out of quota)."""
    msg = " " + message.lower().strip() + " "
    out: dict = {}

    # Licensure reach — "licensed in / can work in / eligible to work in <state>"
    # is about where they can PRACTICE, not where they live. Capture it first and
    # strip it, so the residence-state pass below doesn't also grab that state.
    lm = re.search(r"\b(?:licen[sc]ed (?:in|to work in)|can work in|eligible to "
                   r"work in|authorized to work in|licensure in|license in)\s+"
                   r"([a-z][a-z .]{1,20}?)\b", msg)
    if lm:
        place = lm.group(1).strip()
        code = _STATE_NAMES.get(place) or (place.upper() if place.upper() in _US_STATES else None)
        if code:
            out["licensed_state"] = code
            msg = msg.replace(lm.group(0), " ")

    for name, code in sorted(_STATE_NAMES.items(), key=lambda kv: -len(kv[0])):
        if f" {name} " in msg or f" {name}," in msg:
            out["state_code"] = code
            msg = msg.replace(name, " ")
            break

    # Distance + optional centre: "within 50 miles of Folsom California",
    # "25 mi from Austin TX". Capture the number and the place together, and cut
    # the whole span out of `msg` so neither pollutes the keyword search.
    rm = re.search(
        r"\b(?:within|around|about|near|under|<=?)?\s*(\d{1,3})\s*"
        r"(?:mi|mile|miles|mi\.)\b"
        r"(?:\s*(?:of|from|around|to|near|outside of|outside)\s+"
        r"([a-z][a-z.'\- ]{1,38}?))?"
        r"(?=\s*(?:,|\.|$| with | who | that | in ))",
        msg + " ")
    if rm:
        out["radius_mi"] = max(1, min(500, int(rm.group(1))))
        if rm.group(2):
            _set_center(out, rm.group(2))
        msg = msg[:rm.start()] + " " + msg[rm.end():]
    # A bare "near/around <city>" with no distance still names a centre.
    if "city" not in out:
        nm = re.search(r"\b(?:near|around|close to|nearby|outside of|outside)\s+"
                       r"([a-z][a-z.'\- ]{1,38}?)(?=\s*(?:,|\.|$| with | who | that ))",
                       msg + " ")
        if nm:
            _set_center(out, nm.group(1))

    for tokens, cat in _CATEGORY_CUES:
        if any(t in msg for t in tokens):
            out["category"] = cat
            break

    for lic in sorted(_COPILOT_LICENSES, key=len, reverse=True):
        if re.search(rf"\b{lic.lower()}s?\b", msg):     # allow "MDs", "RNs"
            out["license_title"] = "PharmD" if lic == "PHARMD" else lic
            if "category" not in out:
                if lic in {"RN", "LPN", "LVN", "CNA"}:
                    out["category"] = "Nursing"
                elif lic in {"MD", "DO"}:
                    out["category"] = "Physicians"
                elif lic in {"NP", "PA", "CRNA"}:
                    out["category"] = "APP"
            break

    m = re.search(r"(?:more than|over|at least|minimum(?:\s+of)?|min|greater than|"
                  r">=?|above)\s*(\d{1,2})\s*\+?\s*year", msg)
    if not m:
        m = re.search(r"(\d{1,2})\s*\+\s*year", msg)      # "5+ years"
    if m:
        out["min_experience"] = _clean_int(m.group(1))
    m = re.search(r"(?:under|less than|below|<=?|at most|max(?:imum)?|fewer than)\s*"
                  r"(\d{1,2})\s*year", msg)
    if m:
        out["max_experience"] = _clean_int(m.group(1))

    if re.search(r"\b(with contact|reachable|has (?:email|phone|contact)|contactable)\b", msg):
        out["contact_available"] = "any"

    # Compact / multistate (eNLC) licence — a real, filterable field now.
    if re.search(r"\b(compact|multi[\s-]?state|enlc|nlc)\b", msg):
        out["compact"] = True

    # Prior travel assignments (work-history employment_type).
    if re.search(r"\b(travel(?:ed)? (?:experience|assignment|background|nurse|nursing|rn|history)"
                 r"|has travel(?:ed)?|worked travel|travel contracts?)\b", msg):
        out["travel_experience"] = True

    # Worked at a specific employer / health system (work history).
    wm = re.search(r"\b(?:worked (?:at|for)|experience at|employed (?:at|by))\s+"
                   r"([a-z0-9][a-z0-9 .&'/-]{1,40}?)"
                   r"(?=\s*(?:,|\.|$| who | that | with | and | in | as ))", msg + " ")
    if wm:
        emp = re.sub(r"\s+", " ", wm.group(1)).strip(" ,.-")
        if emp and len(emp) >= 2 and emp not in _STOP_TOKENS:
            out["worked_at"] = emp
            msg = msg.replace(wm.group(0).strip(), " ")

    # Whatever descriptive keywords remain (specialty, "compact", "bilingual" …).
    # Skip role/credential words already captured as category/license, so a
    # leftover plural like "practitioners" can't become a required keyword.
    role_words = {"nurse", "physician", "doctor", "surgeon", "therapist",
                  "practitioner", "assistant", "technologist", "technician",
                  "pathologist", "pharmacist", "dietitian", "paramedic", "aprn",
                  "provider", "candidate", "clinician"}
    licenses_lc = {l.lower() for l in _COPILOT_LICENSES}
    consumed = {out.get("license_title", "").lower(),
                (out.get("state_code") or "").lower()}
    consumed |= set((out.get("city") or "").lower().split())   # centre-city words
    kws = []
    for tok in re.findall(r"[a-z][a-z/&-]{1,}", msg):
        singular = tok[:-1] if tok.endswith("s") else tok
        if (tok in _STOP_TOKENS or tok in consumed or tok in licenses_lc
                or singular in licenses_lc or singular in role_words
                or tok in role_words or len(tok) < 2):
            continue
        if tok not in kws:
            kws.append(tok)
    if kws:
        out["q"] = " ".join(kws[:6])
    return out


def _clean_int(value, lo=0, hi=80):
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return None
    return max(lo, min(hi, n)) if n >= lo else None


def _copilot_filters(raw: dict) -> dict:
    """Validate the model's JSON down to filters the search actually supports."""
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    q = str(raw.get("q") or "").strip().lower()
    if q and q != "null":
        out["q"] = q[:120]
    cat = str(raw.get("category") or "").strip().lower()
    if cat in _COPILOT_CATS:
        out["category"] = _COPILOT_CATS[cat]
    lic = str(raw.get("license_title") or "").strip().upper().replace(".", "")
    if lic in _COPILOT_LICENSES:
        out["license_title"] = "PharmD" if lic == "PHARMD" else lic
    st = str(raw.get("state_code") or "").strip().upper()
    if st in _US_STATES:
        out["state_code"] = st
    ls = str(raw.get("licensed_state") or "").strip().upper()
    if ls in _US_STATES:
        out["licensed_state"] = ls
    city = str(raw.get("city") or "").strip()
    if city and city.lower() != "null":
        out["city"] = city[:60]
    rad = _clean_int(raw.get("radius_mi"), lo=1, hi=500)
    if rad:
        out["radius_mi"] = rad
    mn = _clean_int(raw.get("min_experience"))
    if mn:
        out["min_experience"] = mn
    mx = _clean_int(raw.get("max_experience"))
    if mx is not None:
        out["max_experience"] = mx
    if str(raw.get("contact_available") or "").strip().lower() == "any":
        out["contact_available"] = "any"
    if raw.get("compact") in (True, "true", "True", 1, "1"):
        out["compact"] = True
    if raw.get("travel_experience") in (True, "true", "True", 1, "1"):
        out["travel_experience"] = True
    wa = str(raw.get("worked_at") or "").strip()
    if wa and wa.lower() != "null":
        out["worked_at"] = wa[:60]
    return out


# Qualifiers we don't hold as structured, filterable data. Requiring them would
# wrongly zero out results, so they're dropped from the query and surfaced to
# the recruiter ("couldn't filter by …") instead of silently ignored.
_SOFT_TERMS = {"bilingual", "remote", "weekend", "night", "nights", "prn"}
# Pure filler that carries no search value as a required keyword: generic words,
# role words already captured as category/license, and seniority descriptors the
# LLM should map to experience (so "new grad RN" filters on ≤1yr, not a literal
# "%grad%" text match that returns nothing).
_FILLER_TERMS = {
    "license", "licenses", "licensed", "certification", "certifications",
    "new", "grad", "grads", "graduate", "graduates", "senior", "junior",
    "entry", "level", "experienced", "seasoned", "veteran",
    "nurse", "nurses", "nursing", "physician", "physicians", "doctor", "doctors",
    "therapist", "therapists", "provider", "providers", "candidate", "candidates",
    "staff", "charge", "float", "per", "diem",
    # Captured as the structured `compact` filter, not a keyword.
    "compact", "multistate", "multi-state", "enlc", "nlc", "license", "licence",
    # Captured as travel_experience, not a keyword.
    "travel", "traveled", "travelling", "traveling", "assignment", "assignments",
}


# Phrases that mean "forget the previous filters and start fresh".
_RESET_RE = re.compile(
    r"\b(start over|start again|new search|reset|clear (all|filters|everything|it)?"
    r"|from scratch|forget (that|it|everything)|never mind)\b", re.IGNORECASE)

# Fields carried between turns. `q` is merged token-wise; the rest override.
_MERGE_FIELDS = ("category", "license_title", "state_code", "licensed_state",
                 "city", "radius_mi", "min_experience", "max_experience",
                 "contact_available", "compact", "worked_at", "travel_experience")


def _merge_copilot_filters(context: dict, delta: dict) -> dict:
    """Refine prior filters with a follow-up: a field the new turn specifies wins,
    everything it stays silent on is inherited, and keywords accumulate."""
    merged = {k: context[k] for k in _MERGE_FIELDS if context.get(k) not in (None, "")}
    for k in _MERGE_FIELDS:
        if delta.get(k) not in (None, ""):
            merged[k] = delta[k]
    tokens: list[str] = []
    for tok in (str(context.get("q") or "") + " " + str(delta.get("q") or "")).split():
        if tok and tok not in tokens:
            tokens.append(tok)
    if tokens:
        merged["q"] = " ".join(tokens[:6])
    return merged


def _copilot_summary(filters: dict, total: int, soft: list | None = None,
                     refined: bool = False) -> str:
    """A transparent, deterministic recap of how the request was interpreted."""
    bits = []
    if filters.get("category"):
        bits.append(filters["category"])
    if filters.get("license_title"):
        bits.append(filters["license_title"])
    if filters.get("q"):
        bits.append(f"“{filters['q']}”")
    place = ", ".join(x for x in (
        (filters.get("city") or "").title() or None, filters.get("state_code")) if x)
    if filters.get("radius_mi") and filters.get("city"):
        bits.append(f"within {filters['radius_mi']} mi of {place}")
    elif place:
        bits.append(place)
    if filters.get("min_experience") and filters.get("max_experience"):
        bits.append(f"{filters['min_experience']}–{filters['max_experience']} yrs")
    elif filters.get("min_experience"):
        bits.append(f"{filters['min_experience']}+ yrs")
    elif filters.get("max_experience") is not None:
        bits.append(f"≤{filters['max_experience']} yrs")
    if filters.get("licensed_state"):
        bits.append(f"can work in {filters['licensed_state']}")
    if filters.get("compact"):
        bits.append("compact license")
    if filters.get("worked_at"):
        bits.append(f"worked at {filters['worked_at']}")
    if filters.get("travel_experience"):
        bits.append("travel experience")
    if filters.get("contact_available"):
        bits.append("with contact")
    phrase = " · ".join(bits) if bits else "all providers"
    note = (f" I can’t filter by {', '.join(soft)} yet, so that wasn’t applied."
            if soft else "")
    if not filters and not soft:
        return ("I can search the provider directory — try “ICU nurses in "
                "California with 5+ years” or “RN with more than 5 years”.")
    if total == 0:
        return (f"No providers matched {phrase}. Try removing a filter or "
                f"broadening the location.{note}")
    verb = "Refined to" if refined else "Found"
    return f"{verb} {total:,} providers matching {phrase}.{note}"


@router.post("/copilot")
def copilot_search(body: CopilotQuery, user: CurrentUser, db: DbSession):
    """Natural-language provider search for the recruiter copilot.

    Results are the same masked ProfileCardOut rows the directory returns, so
    the copilot can never reveal a name/contact the recruiter hasn't released.
    """
    _require_provider_directory_access(user)
    message = (body.message or "").strip()
    if not message:
        return {"answer": _copilot_summary({}, 0), "filters": {}, "items": [], "total": 0}

    # Two extractors, merged: the rule pass reliably catches precise structured
    # signals (licensed_state, compact, radius, city, license), while the LLM
    # handles fuzzy phrasing. The LLM wins on any shared field; the rules fill
    # the gaps it misses — and the rules alone keep things working if the LLM is
    # down or out of quota.
    rule_delta = _copilot_rule_filters(message)
    llm_delta: dict = {}
    if settings.llm_enabled and settings.llm_api_key and settings.llm_model:
        from ..clean_names_llm import _llm
        try:
            # Interactive path: one attempt, short timeout — never make the
            # recruiter wait on retries if the key is down or out of quota.
            raw = _llm(message, system=_COPILOT_SYSTEM, instr=_COPILOT_INSTR,
                       max_chars=600, retries=1, timeout=8)
            llm_delta = _copilot_filters(raw or {})
        except Exception:  # noqa: BLE001
            llm_delta = {}
    delta = {**rule_delta, **llm_delta}

    # Refine the previous turn's filters unless the user asked to start over.
    # The client-supplied context is re-validated, never trusted as-is.
    prior = {} if _RESET_RE.search(message) else _copilot_filters(body.context or {})
    refined = bool(prior)
    filters = _merge_copilot_filters(prior, delta)

    category = filters.get("category")
    # Keyword search: AND one substring match per token (the single-LIKE `q`
    # column can't match a multi-word phrase), so "icu telemetry" needs both.
    # Split off qualifiers we don't hold as structured data — requiring them
    # would wrongly zero out an otherwise-good search — and note them instead.
    raw_tokens = [t for t in (filters.get("q") or "").split() if len(t) >= 2]
    soft = [t for t in raw_tokens if t in _SOFT_TERMS]
    # Also drop generic stopwords the LLM sometimes dumps into q ("experience",
    # "with", role words) so they can't become spurious required keywords.
    hard = [t for t in raw_tokens if t not in _SOFT_TERMS and t not in _FILLER_TERMS
            and t not in _STOP_TOKENS][:6]
    filters["q"] = " ".join(hard) or None
    if not filters["q"]:
        filters.pop("q")

    search_filters = {k: v for k, v in filters.items()
                      if k not in ("category", "q")}
    conds = _provider_conditions(db, providers_only=True, **search_filters)
    for tok in hard:
        conds.append(Profile.search_text.like(f"%{tok}%"))
    stmt = select(Profile).where(*conds)
    if category:
        stmt = stmt.where(func.lower(Profile.provider_category) == category.lower())

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(
        stmt.order_by(Profile.completion_score.desc(), Profile.profile_id).limit(12)
    ).all()
    released = _released_profile_ids(db, user, [r.profile_id for r in rows])
    items = [
        _profile_card(r, released=(r.profile_id in released
                                   or bool(r.user_id and r.user_id == user.user_id)))
        for r in rows
    ]
    return {"answer": _copilot_summary(filters, total, soft, refined),
            "filters": filters, "items": items, "total": total}


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
    compact: Optional[bool] = None,
    licensed_state: Optional[str] = None,
    worked_at: Optional[str] = None,
    travel_experience: Optional[bool] = None,
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
        contact_available=contact_available, compact=compact,
        licensed_state=licensed_state, worked_at=worked_at,
        travel_experience=travel_experience)
    rows = db.execute(
        select(Profile.provider_category, func.count())
        .where(*conds, Profile.provider_category.in_(_PROVIDER_CATS))
        .group_by(Profile.provider_category)
    ).all()
    return {cat: 0 for cat in _PROVIDER_CATS} | {c: n for c, n in rows}


@router.get("/screening-report")
def screening_report(user: CurrentUser, db: DbSession, limit: int = Query(10, ge=1, le=50)):
    """What the directory screen hid, and why.

    Hiding a profile is a heuristic judgement, so it has to be inspectable:
    this reports the counts per reason plus a sample, and every row keeps the
    score that drove the decision (`python -m app.screen_directory --restore`
    puts them all back).
    """
    _require_provider_directory_access(user)
    rows = db.execute(
        select(Profile.screen_reason, func.count())
        .where(Profile.screen_reason.isnot(None))
        .group_by(Profile.screen_reason).order_by(func.count().desc())
    ).all()
    hidden = db.scalars(
        select(Profile)
        .where(Profile.is_listable.is_(False), Profile.screen_reason.isnot(None))
        .order_by(Profile.screened_at.desc()).limit(limit)
    ).all()
    return {
        "by_reason": {r or "unknown": n for r, n in rows},
        "listable_total": db.scalar(
            select(func.count()).select_from(Profile)
            .where(Profile.is_listable.is_(True))) or 0,
        "hidden_total": db.scalar(
            select(func.count()).select_from(Profile)
            .where(Profile.is_listable.is_(False))) or 0,
        "sample": [{
            "profile_id": p.profile_id,
            "masked_name": _masked_name(p),
            "headline": p.headline,
            "reason": p.screen_reason,
            "healthcare_terms_found": p.screen_score,
        } for p in hidden],
    }


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
    # NULL categories fold into "Others" — accumulate, don't overwrite, or the
    # handful of uncategorised profiles silently replaces the real Others count.
    categories: dict[str, int] = {}
    for c, n in cat_rows:
        key = c or "Others"
        categories[key] = categories.get(key, 0) + n
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


@router.patch("/me", response_model=ProfileDetail)
def update_my_profile(body: ProfileUpdate, user: CurrentUser, db: DbSession):
    """Let a healthcare professional maintain their own profile.

    Until this existed a self-registered nurse could apply for jobs but never
    say what they do, so they carried no specialty, licence or experience and
    were invisible to the matching engine — able to apply, impossible to find.

    Creates the profile on first save, so a new account does not have to call
    POST first.
    """
    profile = db.scalar(
        select(Profile)
        .options(selectinload(Profile.licenses), selectinload(Profile.certifications),
                 selectinload(Profile.work_history), selectinload(Profile.skills))
        .where(Profile.user_id == user.user_id)
    )
    changes = body.model_dump(exclude_unset=True)
    # These are set by the platform, never by the person being described.
    for locked in ("is_listable", "screen_reason", "merged_into", "completion_score"):
        changes.pop(locked, None)

    if not profile:
        profile = Profile(user_id=user.user_id, **changes)
        db.add(profile)
    else:
        for field, value in changes.items():
            setattr(profile, field, value)

    # A professional maintaining their own profile is asserting they are a
    # provider, so classify them the same way the importer would.
    if profile.profession_type or profile.specialty:
        profile.provider_category = classify_provider(
            profile.profession_type, profile.specialty, profile.headline)
    profile.rebuild_search_text()
    profile.completion_score = _compute_completion(profile)
    db.commit()
    db.refresh(profile)
    return profile


@router.get("/me/completion")
def my_profile_completion(user: CurrentUser, db: DbSession):
    """What is still missing, so the UI can tell them what to fill in next."""
    profile = db.scalar(select(Profile).where(Profile.user_id == user.user_id))
    if not profile:
        return {"score": 0, "missing": ["profile"], "complete": False}
    wanted = {
        "profession_type": "Your licence or title (RN, LPN, MD…)",
        "specialty": "Your specialty",
        "years_experience": "Years of experience",
        "city": "City",
        "state_code": "State",
        "phone": "Phone number",
        "headline": "A short headline",
        "resume_url": "Your résumé",
    }
    missing = [label for field, label in wanted.items() if not getattr(profile, field, None)]
    return {"score": profile.completion_score or 0, "missing": missing,
            "complete": not missing}


@router.get("/me/credentials")
def my_credentials(user: CurrentUser, db: DbSession, expiring_days: int = 90):
    """A professional's licences and certifications, with expiry status.

    Keeping a licence current is the one piece of admin every clinician has to
    do, and an expired one makes them unplaceable. Surfacing the dates is the
    reason a healthcare professional would come back to this platform rather
    than only visiting when job-hunting.
    """
    from datetime import date as _date, timedelta

    profile = db.scalar(
        select(Profile)
        .options(selectinload(Profile.licenses), selectinload(Profile.certifications))
        .where(Profile.user_id == user.user_id)
    )
    if not profile:
        return {"licenses": [], "certifications": [], "alerts": []}

    today = _date.today()
    soon = today + timedelta(days=max(1, expiring_days))

    def state(expiry, verified_status=None):
        """A board's verdict outranks the printed date.

        A licence whose expiry has not yet passed can still be expired,
        suspended or absent from the register — showing "valid" because the
        date looks fine is exactly the optimism this feature exists to stop.
        """
        if verified_status in {"expired", "disciplined", "not_found"}:
            return "expired"
        if not expiry:
            return "unknown"
        if expiry < today:
            return "expired"
        return "expiring" if expiry <= soon else "valid"

    licenses = [{
        "license_id": lic.license_id,
        "license_type": lic.license_type,
        "state_code": lic.state_code,
        "license_number": lic.license_number,
        "expiry_date": lic.expiry_date,
        "is_compact": bool(lic.is_compact),
        "status": state(lic.expiry_date, lic.verification_status),
        "days_left": (lic.expiry_date - today).days if lic.expiry_date else None,
        # A claim until a source says otherwise — surfaced so it is never
        # mistaken for a checked fact.
        "verification_status": lic.verification_status or "never_checked",
        "verified": lic.verification_status in {"active", "expired",
                                                "disciplined", "not_found"},
    } for lic in profile.licenses]

    certs = [{
        "cert_id": c.cert_id,
        "cert_name": c.cert_name,
        "expiry_date": c.expiry_date,
        "status": state(c.expiry_date),
        "days_left": (c.expiry_date - today).days if c.expiry_date else None,
    } for c in profile.certifications]

    alerts = [
        {"kind": "license" if "license_id" in item else "certification",
         "label": item.get("license_type") or item.get("cert_name"),
         "status": item["status"], "days_left": item["days_left"]}
        for item in licenses + certs if item["status"] in {"expired", "expiring"}
    ]
    return {"licenses": licenses, "certifications": certs, "alerts": alerts,
            "compact_eligible": any(lic["is_compact"] for lic in licenses)}


@router.post("/me/licenses", response_model=LicenseOut, status_code=201)
def add_my_license(body: LicenseCreate, user: CurrentUser, db: DbSession):
    """Add a licence to your own profile without needing your profile id."""
    profile = db.scalar(select(Profile).where(Profile.user_id == user.user_id))
    if not profile:
        raise HTTPException(status_code=404, detail="Create your profile first")
    lic = License(profile_id=profile.profile_id, **body.model_dump())
    # A compact licence is what makes a nurse placeable across state lines, so
    # derive it rather than relying on the person to know the term.
    if lic.state_code and lic.license_type:
        lic.is_compact = bool(lic.is_compact) or (
            lic.state_code.upper() in _COMPACT_STATES
            and lic.license_type.upper() in {"RN", "LPN", "LVN"})
    db.add(lic)
    db.commit()
    db.refresh(lic)
    return lic


@router.delete("/me/licenses/{license_id}", status_code=204)
def delete_my_license(license_id: str, user: CurrentUser, db: DbSession):
    profile = db.scalar(select(Profile).where(Profile.user_id == user.user_id))
    lic = db.get(License, license_id)
    if profile and lic and lic.profile_id == profile.profile_id:
        db.delete(lic)
        db.commit()


@router.post("/licenses/{license_id}/verify")
def verify_license(license_id: str, user: CurrentUser, db: DbSession,
                   body: dict | None = None):
    """Check a licence against its issuing board and record what came back.

    A licence a candidate typed in is a claim; this is what turns it into
    evidence. With no source configured the answer is an honest "unverified" —
    never a pass on no evidence — and a recruiter can instead record that they
    checked the board themselves, which at least makes the check attributable.
    """
    from ..services import license_verify as lv

    lic = db.get(License, license_id)
    if not lic:
        raise HTTPException(status_code=404, detail="Licence not found")
    profile = db.get(Profile, lic.profile_id)
    owns = profile and profile.user_id == user.user_id
    if not owns and not _is_recruiter_or_admin(user):
        raise HTTPException(status_code=403, detail="Not your licence")

    body = body or {}
    extra = {}
    if body.get("provider") == "manual" or body.get("manual"):
        extra = {"status": body.get("status", lv.STATUS_ACTIVE),
                 "checked_by": user.email}
        if body.get("expiry_date"):
            from datetime import date as _d
            try:
                extra["expiry_date"] = _d.fromisoformat(str(body["expiry_date"])[:10])
            except ValueError:
                pass

    result = lv.verify(
        license_type=lic.license_type, state_code=lic.state_code,
        license_number=lic.license_number,
        first_name=profile.first_name if profile else "",
        last_name=profile.last_name if profile else "",
        provider="manual" if extra else None, **extra)

    lic.verification_status = result.status
    lic.verification_source = result.source
    lic.verification_detail = result.detail
    if result.is_verified:
        lic.verified_at = utcnow()
        lic.verified_by_user_id = user.user_id
        if result.expiry_date:
            lic.expiry_date = result.expiry_date
        if result.is_compact is not None:
            lic.is_compact = result.is_compact
        # A board saying "expired" or "disciplined" must move the licence out
        # of active, or a recruiter could still submit on it.
        if result.status != lv.STATUS_ACTIVE:
            lic.status = LicenseStatus.expired
    db.commit()
    return {
        "license_id": lic.license_id,
        "status": result.status,
        "verified": result.is_verified,
        "placeable": result.is_placeable,
        "source": result.source,
        "detail": result.detail,
        "expiry_date": lic.expiry_date,
    }


@router.get("/licenses/verification-status")
def verification_coverage(user: CurrentUser, db: DbSession):
    """How much of the directory rests on checked licences rather than claims."""
    _require_provider_directory_access(user)
    from ..services import license_verify as lv

    total = db.scalar(select(func.count()).select_from(License)) or 0
    rows = dict(db.execute(
        select(License.verification_status, func.count())
        .group_by(License.verification_status)).all())
    verified = sum(n for s, n in rows.items() if s in lv.VERIFIED_STATUSES)
    return {
        "provider": lv.get_provider().name,
        "licenses_total": total,
        "verified": verified,
        "unverified": total - verified,
        "by_status": {(s or "never_checked"): n for s, n in rows.items()},
        "note": ("No verification source is configured, so licences are "
                 "candidate claims rather than checked facts."
                 if lv.get_provider().name == "unavailable" else None),
    }


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
    own = bool(profile.user_id and profile.user_id == user.user_id)
    if own:
        return profile
    # Same rule as the directory: this endpoint must not become a way to read a
    # name/contact that the listing deliberately withholds.
    _require_provider_directory_access(user)
    if _may_see_identity(db, user, profile):
        return profile
    # Mask on the serialised copy, never on the ORM object — mutating the entity
    # would risk an autoflush writing the masked name back to the database.
    masked = _masked_name(profile).split(" ")
    detail = ProfileDetail.model_validate(profile)
    detail.first_name = masked[0] if masked else "?"
    detail.last_name = masked[1] if len(masked) > 1 else ""
    detail.email = None
    detail.phone = None
    return detail


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


def _stored_sections(profile: Profile, drop: set) -> "OrderedDict[str, list[str]] | None":
    """LLM-extracted sections for this profile, or None if there aren't any.

    Returns them in the same canonical order the parser uses, and re-applies the
    contact-redaction filter so a stale extraction can never leak an email or
    phone number that the caller isn't allowed to see.
    """
    blob = getattr(profile, "resume_sections", None)
    if not isinstance(blob, dict):
        return None
    raw = blob.get("sections")
    if not isinstance(raw, dict) or not raw:
        return None

    kept: "OrderedDict[str, list[str]]" = OrderedDict()
    known = [canon for canon, _ in _CANON_SECTIONS]
    for canon in known + [k for k in raw if k not in known]:
        lines = raw.get(canon)
        if not isinstance(lines, list):
            continue
        clean = [ln for ln in (_clean_resume_line(str(x), drop) for x in lines) if ln]
        if clean:
            kept[canon] = clean
    return kept or None


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

    # Metered: one credit per contact, charged once. The idempotency key is the
    # recruiter+profile pair, so re-opening a contact you already paid for is
    # free however many times you click it.
    charge_result = {"charged": False, "cost": 0}
    if settings.credits_enabled:
        from ..models import COST_REVEAL_CONTACT
        from ..services import credits as credit_service
        try:
            charge_result = credit_service.charge(
                db, user.user_id, COST_REVEAL_CONTACT,
                entity_type="profile", entity_id=profile.profile_id,
                idempotency_key=f"reveal:{user.user_id}:{profile.profile_id}",
                note="Contact release",
            )
        except credit_service.InsufficientCredits as exc:
            raise HTTPException(
                status_code=402,
                detail=(f"You need {exc.needed} credit"
                        f"{'' if exc.needed == 1 else 's'} to reveal this contact "
                        f"and have {exc.balance}. Top up to continue."),
            ) from exc

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

    # The release is what unlocks identity, so the real name comes back here —
    # it is deliberately absent from the directory listing until this point.
    return {
        "profile_id": profile.profile_id,
        "first_name": profile.first_name,
        "last_name": profile.last_name,
        "name": _display_name(profile),
        "email": profile.email,
        "phone": profile.phone,
        "contact_updated_by_email": profile.contact_updated_by_email,
        "is_released": True,
        "released_at": now.isoformat(),
        "released_by_email": user.email,
        "credits_charged": charge_result.get("cost", 0),
        "credits_remaining": charge_result.get("balance"),
    }


@router.get("/{profile_id}/resume")
def view_resume(profile_id: str, user: CurrentUser, db: DbSession):
    """Return a candidate's résumé as safe, view-only structured data (no download)
    — grouped into sections + a skills list so the UI can show it in tabs."""
    profile = db.scalar(
        select(Profile)
        .options(selectinload(Profile.skills), selectinload(Profile.certifications),
                 selectinload(Profile.licenses), selectinload(Profile.work_history))
        .where(Profile.profile_id == profile_id)
    )
    if not profile or not profile.resume_url:
        raise HTTPException(status_code=404, detail="No résumé on file")
    if profile.user_id != user.user_id and not _is_recruiter_or_admin(user):
        raise HTTPException(status_code=403, detail="Providers are available to recruiters only")
    can_view_contact = profile.user_id == user.user_id
    drop = {v.strip().lower() for v in (profile.email, profile.phone) if v}

    # Prefer sections extracted once by the LLM (python -m app.extract_resume_sections):
    # they survive PDFs that lost their spacing and multi-column layouts, and skip
    # re-downloading the file entirely. Fall back to parsing when absent.
    sections = _stored_sections(profile, drop)
    llm_skills: list[str] = []
    if sections is None:
        try:
            lines = _resume_lines(profile.resume_url)
        except Exception:  # noqa: BLE001 — fall back to a header-only view
            lines = []
        sections = _group_resume_sections(lines, drop)
    else:
        llm_skills = [s for s in (profile.resume_sections or {}).get("skills", [])
                      if isinstance(s, str)]

    # Skill chips: specialty + parsed skills + certifications, de-duplicated.
    skills, seen = [], set()
    for s in ([profile.specialty] + llm_skills + [sk.name for sk in profile.skills]
              + [c.cert_name for c in profile.certifications]):
        s = (s or "").strip()
        if s and 2 <= len(s) <= 48 and s.lower() not in seen:
            seen.add(s.lower())
            skills.append(s)

    role = " · ".join(b for b in (profile.provider_category, profile.specialty) if b)

    # Identity stays withheld until the profile is released — including inside
    # the résumé body, where the candidate's own name usually appears.
    revealed = can_view_contact or _may_see_identity(db, user, profile)
    if revealed:
        name = _display_name(profile) or "Provider résumé"
    else:
        name = _masked_name(profile)
        sections = OrderedDict(
            (heading, [_redact_name(line, profile) for line in lines])
            for heading, lines in sections.items()
        )
        skills = [_redact_name(s, profile) for s in skills]

    # Structured, enriched data (python -m app.enrich_profiles). Contains no
    # identity, so it's shown even while the name is withheld — and it's the
    # healthcare-specific payoff: licenses, compact reach, work history.
    licenses = [{"type": l.license_type, "state": l.state_code,
                 "compact": bool(l.is_compact),
                 "expiry": l.expiry_date.isoformat() if l.expiry_date else None}
                for l in profile.licenses]
    work_history = [{"employer": w.employer_name, "title": w.job_title,
                     "specialty": w.specialty, "type": w.employment_type,
                     "location": ", ".join(x for x in (w.city, w.state_code) if x),
                     "start": w.start_date.isoformat()[:7] if w.start_date else None,
                     "end": w.end_date.isoformat()[:7] if w.end_date else None}
                    for w in profile.work_history]
    licensed_states = sorted({l["state"] for l in licenses if l["state"]})

    return {
        "name": name,
        "withheld": not revealed,
        "credential": profile.profession_type,
        "role": role,
        "location": ", ".join(b for b in (profile.city, profile.state_code) if b),
        "email": profile.email if revealed else None,
        "phone": profile.phone if revealed else None,
        "board": profile.american_board,
        "years_experience": profile.years_experience,
        "sections": sections,
        "skills": skills[:20],
        # --- enriched structured data ---
        "licenses": licenses,
        "licensed_states": licensed_states,
        "has_compact": any(l["compact"] for l in licenses),
        "work_history": work_history,
        "education": profile.education or [],
        "work_authorization": profile.work_authorization,
        "available_date": (profile.available_date.isoformat()
                           if profile.available_date else None),
    }


# --- AI candidate summary --------------------------------------------------
# A recruiter-facing briefing generated from the ENRICHED structured data
# (licenses + compact reach, work history, education, specialty). Role-focused
# and never names the person, so it can't leak an identity that's still masked.

_SUMMARY_SYSTEM = ("You write a concise, factual recruiter briefing about a "
                   "healthcare candidate from structured data. Respond with ONLY "
                   "one JSON object.")
_SUMMARY_INSTR = (
    'Return {"summary":"...","highlights":["...","..."]}\n'
    "- summary: 2-3 sentences, recruiter-facing, third person. Refer to the "
    "person as 'This <role>' or 'This candidate' — NEVER use a name. Cover "
    "specialty + experience, licensure (call out compact / multistate reach when "
    "present), and a standout employer or credential.\n"
    "- highlights: 3-5 short chips (max ~7 words each): e.g. 'Compact RN — 40 "
    "states', '8 yrs ICU', 'Travel experience', 'BSN, Arizona State'.\n"
    "- Use ONLY the facts provided. Never invent. Keep it short if data is thin.\n"
)


@router.get("/{profile_id}/summary")
def profile_summary(profile_id: str, user: CurrentUser, db: DbSession):
    """One-tap AI briefing of a candidate, built from their enriched data."""
    _require_provider_directory_access(user)
    profile = db.scalar(
        select(Profile)
        .options(selectinload(Profile.licenses), selectinload(Profile.work_history))
        .where(Profile.profile_id == profile_id)
    )
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")

    licenses = [{"type": l.license_type, "state": l.state_code, "compact": bool(l.is_compact)}
                for l in profile.licenses][:12]
    work = [{"employer": w.employer_name, "title": w.job_title,
             "specialty": w.specialty, "type": w.employment_type}
            for w in profile.work_history][:8]
    compact_states = sorted({l["state"] for l in licenses if not l["compact"]}
                            | ({"(compact/multistate)"} if any(l["compact"] for l in licenses) else set()))
    facts = {
        "role": profile.profession_type, "category": profile.provider_category,
        "specialty": profile.specialty, "years_experience": profile.years_experience or None,
        "location": ", ".join(x for x in (profile.city, profile.state_code) if x) or None,
        "licenses": licenses, "licensed_in": compact_states,
        "has_compact_license": any(l["compact"] for l in licenses),
        "travel_experience": any(w.get("type") == "travel" for w in work),
        "work_history": work, "education": profile.education or [],
    }
    if not (licenses or work or profile.specialty or profile.years_experience
            or profile.education):
        return {"summary": None, "highlights": [],
                "reason": "This candidate isn't enriched yet — run résumé enrichment."}
    if not (settings.llm_enabled and settings.llm_api_key and settings.llm_model):
        return {"summary": None, "highlights": [], "reason": "AI is not configured."}

    from ..clean_names_llm import _llm
    try:
        raw = _llm(json.dumps(facts, default=str), system=_SUMMARY_SYSTEM,
                   instr=_SUMMARY_INSTR, max_chars=2500, retries=1, timeout=10)
    except Exception:  # noqa: BLE001
        raw = None
    if not isinstance(raw, dict):
        return {"summary": None, "highlights": [], "reason": "Couldn't generate a summary."}
    summary = str(raw.get("summary") or "").strip()[:700]
    highlights = [str(h).strip()[:80] for h in (raw.get("highlights") or [])
                  if str(h).strip()][:6]
    return {"summary": summary or None, "highlights": highlights}


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
