"""AI candidate-matching engine.

A transparent, explainable scoring model (not a black-box embedding search) so
results are reproducible and easy to reason about. Scores four dimensions —
skills, experience, location, pay — and combines them using caller-supplied
weights, mirroring the healthboard-ai-matching.html prototype.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..models import JobPosting, License, LicenseStatus, Profile
from ..schemas.matching import (
    CandidateMatch,
    MatchRequest,
    MatchSummary,
    ScoreBreakdown,
)


# Upper bound on how many profiles are pulled into memory for one scoring run.
POOL_CAP = 4000


@dataclass
class _Spec:
    specialty: str | None
    profession_type: str | None
    skills: set[str]
    certs: set[str]
    years_min: int
    years_pref: int
    state_code: str | None
    city: str | None
    pay_min: float | None
    pay_max: float | None
    # False when the req never stated an experience requirement, which is
    # different from deliberately asking for zero years.
    years_declared: bool = False


def _spec_from_request(req: MatchRequest, job: JobPosting | None) -> _Spec:
    if job is not None:
        return _Spec(
            specialty=job.specialty,
            profession_type=job.profession_type,
            skills={s.lower() for s in (job.required_skills or [])},
            certs={c.lower() for c in (job.required_certifications or [])},
            years_min=job.years_exp_min or 0,
            years_pref=(job.years_exp_min or 0) + 3,
            state_code=job.state_code,
            city=job.city,
            pay_min=float(job.pay_rate_min) if job.pay_rate_min is not None else None,
            pay_max=float(job.pay_rate_max) if job.pay_rate_max is not None else None,
            years_declared=job.years_exp_min is not None,
        )
    return _Spec(
        specialty=req.specialty,
        profession_type=req.profession_type,
        skills={s.lower() for s in req.required_skills},
        certs={c.lower() for c in req.required_certifications},
        years_min=req.years_exp_min or 0,
        years_pref=req.years_exp_preferred or (req.years_exp_min or 0) + 3,
        state_code=req.location.state_code,
        city=req.location.city,
        pay_min=req.pay_rate_min,
        pay_max=req.pay_rate_max,
        years_declared=req.years_exp_min is not None or req.years_exp_preferred is not None,
    )


def _score_skills(spec: _Spec, profile_skill_names: set[str], cert_names: set[str],
                  profile_specialty: str | None = None) -> float:
    """Fraction of required skills+certs the candidate has (0-100).

    Most imported reqs carry no explicit skill list, so falling back to a flat
    score would tie every candidate. Without requirements we rank on specialty
    alignment first, then on how much evidence the profile actually carries.
    """
    required = spec.skills | spec.certs
    if not required:
        have = len(profile_skill_names) + len(cert_names)
        depth = min(15.0, 3.0 * have)          # 0-15 for documented skills/certs
        if spec.specialty and profile_specialty:
            if profile_specialty.strip().lower() == spec.specialty.strip().lower():
                return round(85.0 + depth, 2)  # exact specialty match
            return round(55.0 + depth, 2)      # different specialty, right credential
        return round(65.0 + depth, 2)          # req names no specialty
    have_all = profile_skill_names | cert_names
    matched = sum(1 for r in required if any(r in h or h in r for h in have_all))
    return round(100.0 * matched / len(required), 2)


def _score_experience(spec: _Spec, years: int) -> float:
    if spec.years_min <= 0 and not spec.years_declared:
        # The req states no requirement: rank on seniority with diminishing
        # returns, so a 15-year nurse outranks a 3-year one instead of tying.
        if years <= 0:
            return 45.0
        return round(min(100.0, 45.0 + 55.0 * (min(years, 20) / 20) ** 0.6), 2)
    if spec.years_pref <= 0:
        return 100.0 if years > 0 else 60.0
    if years >= spec.years_pref:
        return 100.0
    if years < spec.years_min:
        # Below the hard minimum — heavy penalty but not zero.
        return round(40.0 * (years / spec.years_min), 2) if spec.years_min else 40.0
    # Between min and preferred: linear ramp 70 -> 100.
    span = max(spec.years_pref - spec.years_min, 1)
    return round(70.0 + 30.0 * (years - spec.years_min) / span, 2)


def _score_location(spec: _Spec, profile: Profile, travel_boost: bool) -> float:
    if not spec.state_code and not spec.city:
        return 85.0
    score = 50.0
    if profile.state_code and spec.state_code and profile.state_code.upper() == spec.state_code.upper():
        score = 80.0
        if profile.city and spec.city and profile.city.lower() == spec.city.lower():
            score = 100.0
    # If both have coordinates, refine using distance.
    if (profile.lat is not None and profile.lng is not None
            and getattr(spec, "lat", None) is not None):
        pass  # spec coords not modelled here; state/city heuristic is enough
    # Travel-experienced candidates are flexible on location.
    if travel_boost and "travel" in (profile.job_type_prefs or []):
        score = min(100.0, score + 15.0)
    return round(score, 2)


def _score_pay(spec: _Spec, profile: Profile) -> float:
    """Higher when the candidate's pay floor fits within the job's band."""
    if spec.pay_min is None and spec.pay_max is None:
        return 85.0
    if profile.pay_min_hourly is None:
        return 75.0  # unknown expectation — neutral-positive
    want = float(profile.pay_min_hourly)
    ceiling = spec.pay_max or spec.pay_min or want
    if want <= ceiling:
        return 100.0
    # Candidate wants more than the job pays — decay with the overshoot.
    overshoot = (want - ceiling) / ceiling
    return round(max(0.0, 100.0 - overshoot * 120.0), 2)


def _masked(p: Profile) -> str:
    """'Ta'Nyah Hoskins' -> 'T. H.' — matches the directory's masking exactly."""
    parts = [(p.first_name or "").strip(), (p.last_name or "").strip()]
    return " ".join(f"{x[0].upper()}." for x in parts if x) or "—"


def _initials(first: str, last: str) -> str:
    return (first[:1] + last[:1]).upper()


def _reason(spec: _Spec, profile: Profile, b: ScoreBreakdown) -> str:
    bits = []
    if b.skills >= 80 and spec.specialty:
        bits.append(f"Strong {spec.specialty} skills match")
    elif profile.specialty:
        bits.append(f"{profile.specialty} background")
    if b.experience >= 90:
        bits.append(f"{profile.years_experience}+ yrs experience")
    elif (profile.years_experience or 0) >= 3:
        bits.append(f"{profile.years_experience} yrs experience")
    if b.location >= 90:
        bits.append("local to the role")
    elif b.location >= 70 and "travel" in (profile.job_type_prefs or []):
        bits.append("open to travel")
    if b.pay >= 90:
        bits.append("pay-aligned")
    return " · ".join(bits) or "General match on specialty and availability"


def run_matching(db: Session, req: MatchRequest,
                 released: set[str] | None = None) -> tuple[list[CandidateMatch], MatchSummary]:
    """Score and rank candidates for a req.

    `released` is the set of profile ids whose identity this recruiter has
    already paid to reveal; everyone else is returned masked, exactly as in the
    provider directory.
    """
    released = released or set()
    job = db.get(JobPosting, req.job_id) if req.job_id else None
    spec = _spec_from_request(req, job)

    # Candidate pool: open-to-work profiles, narrowed by specialty/profession
    # where specified. (At true 2M scale this would be a vector / FTS prefilter.)
    stmt = (
        select(Profile)
        .options(selectinload(Profile.skills), selectinload(Profile.certifications),
                 selectinload(Profile.licenses))
        .where(Profile.open_to_work.is_(True))
    )
    if spec.profession_type:
        stmt = stmt.where(Profile.profession_type == spec.profession_type)
    if spec.specialty:
        stmt = stmt.where(Profile.specialty == spec.specialty)
    # Same state first when the req has one: location is a scored dimension, but
    # it is also the cheapest way to keep the shortlist locally relevant.
    if spec.state_code:
        stmt = stmt.where(Profile.state_code == spec.state_code)

    # Hard cap the pool. Without it an unspecific req (no profession, no
    # specialty) would load every open-to-work profile — ~160k rows with their
    # skills, certifications and licenses eagerly loaded.
    stmt = stmt.order_by(Profile.completion_score.desc().nullslast(),
                         Profile.profile_id).limit(POOL_CAP)
    profiles = db.scalars(stmt).all()
    # If a state filter left too little to rank, retry nationwide.
    if spec.state_code and len(profiles) < 25:
        wide = (
            select(Profile)
            .options(selectinload(Profile.skills), selectinload(Profile.certifications),
                     selectinload(Profile.licenses))
            .where(Profile.open_to_work.is_(True))
        )
        if spec.profession_type:
            wide = wide.where(Profile.profession_type == spec.profession_type)
        if spec.specialty:
            wide = wide.where(Profile.specialty == spec.specialty)
        profiles = db.scalars(
            wide.order_by(Profile.completion_score.desc().nullslast(),
                          Profile.profile_id).limit(POOL_CAP)
        ).all()

    w = req.weights
    w_total = max(w.skills + w.experience + w.location + w.pay, 1)

    scored: list[CandidateMatch] = []
    for p in profiles:
        skill_names = {s.name.lower() for s in p.skills}
        cert_names = {c.cert_name.lower() for c in p.certifications}

        # Hard filters
        if req.filters.verified_license_only:
            has_verified = any(
                lic.status == LicenseStatus.active and lic.verified_at is not None
                for lic in p.licenses
            )
            if not has_verified:
                continue
        if req.filters.immediately_available_only and not p.open_to_work:
            continue

        s_skills = _score_skills(spec, skill_names, cert_names, p.specialty)
        s_exp = _score_experience(spec, p.years_experience or 0)
        s_loc = _score_location(spec, p, req.filters.travel_experienced_boost)
        s_pay = _score_pay(spec, p)

        total = (
            s_skills * w.skills + s_exp * w.experience
            + s_loc * w.location + s_pay * w.pay
        ) / w_total
        total = round(total, 2)

        breakdown = ScoreBreakdown(skills=s_skills, experience=s_exp,
                                   location=s_loc, pay=s_pay)
        travel_exp = "travel" in (p.job_type_prefs or [])
        scored.append(
            CandidateMatch(
                rank=0,
                profile_id=p.profile_id,
                # Identity is withheld until the recruiter releases the profile,
                # the same rule the directory enforces.
                name=(f"{p.first_name or ''} {p.last_name or ''}".strip() or "Unnamed")
                     if p.profile_id in released else _masked(p),
                is_released=p.profile_id in released,
                initials=_initials(p.first_name, p.last_name),
                title=p.headline or (f"{p.specialty} {p.profession_type}"
                                     if p.specialty else p.profession_type),
                specialty=p.specialty,
                location=", ".join(x for x in [p.city, p.state_code] if x) or None,
                city=p.city,
                state_code=p.state_code,
                years_experience=p.years_experience or 0,
                pay_min=float(p.pay_min_hourly) if p.pay_min_hourly is not None else None,
                pay_max=float(p.pay_min_hourly) + 15 if p.pay_min_hourly is not None else None,
                skills=[s.name for s in p.skills],
                certifications=[c.cert_name for c in p.certifications],
                completion_score=p.completion_score or 0,
                score_total=total,
                score_breakdown=breakdown,
                travel_experienced=travel_exp,
                immediately_available=bool(p.open_to_work),
                match_reason=_reason(spec, p, breakdown),
            )
        )

    scored.sort(key=lambda c: c.score_total, reverse=True)
    scored = scored[: req.top_n]
    for i, c in enumerate(scored, start=1):
        c.rank = i

    summary = _summarize(scored)
    return scored, summary


def _summarize(cands: list[CandidateMatch]) -> MatchSummary:
    n = len(cands)
    avg = round(sum(c.score_total for c in cands) / n, 2) if n else 0.0
    return MatchSummary(
        total=n,
        excellent_90plus=sum(1 for c in cands if c.score_total >= 90),
        great_80_89=sum(1 for c in cands if 80 <= c.score_total < 90),
        good_70_79=sum(1 for c in cands if 70 <= c.score_total < 80),
        avg_score=avg,
        immediately_available=sum(1 for c in cands if c.immediately_available),
    )
