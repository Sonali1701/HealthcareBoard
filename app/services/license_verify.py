"""Primary-source licence verification.

A licence typed in by a candidate, or scraped off a résumé, is a claim. A
licence checked against the issuing board is a fact — and for healthcare
staffing that difference is the product: an expired or disciplined licence
makes someone unplaceable, and the agency carries the liability for submitting
them.

No provider is wired up yet, because that is a commercial decision (Nursys
e-Notify covers the nursing compact; several state boards publish free lookup
endpoints; the rest are paid aggregators). So this defines the contract and
ships two providers that work today:

* ``manual``    — a recruiter confirms they checked the board themselves, and
                  that judgement is recorded with their name against it.
* ``unavailable`` — the default. Returns "not verified" honestly rather than
                  guessing, so nothing anywhere claims a licence is good on no
                  evidence.

Adding a real source means implementing ``VerificationProvider.check`` and
naming it in settings. Nothing else in the app changes.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Optional, Protocol

logger = logging.getLogger("healthboard.license")

# What a check can conclude. "unverified" is not a failure — it means we did
# not get an answer, which must never be shown as a pass.
STATUS_ACTIVE = "active"
STATUS_EXPIRED = "expired"
STATUS_DISCIPLINED = "disciplined"
STATUS_NOT_FOUND = "not_found"
STATUS_UNVERIFIED = "unverified"

VERIFIED_STATUSES = {STATUS_ACTIVE, STATUS_EXPIRED, STATUS_DISCIPLINED, STATUS_NOT_FOUND}


@dataclass
class VerificationResult:
    status: str = STATUS_UNVERIFIED
    source: str = "unavailable"
    expiry_date: Optional[date] = None
    is_compact: Optional[bool] = None
    licensee_name: Optional[str] = None
    detail: Optional[str] = None
    raw: dict = field(default_factory=dict)

    @property
    def is_verified(self) -> bool:
        """True only when a source actually answered."""
        return self.status in VERIFIED_STATUSES

    @property
    def is_placeable(self) -> bool:
        """Safe to submit to a client on this licence."""
        return self.status == STATUS_ACTIVE


class VerificationProvider(Protocol):
    name: str

    def check(self, *, license_type: str, state_code: str,
              license_number: str, first_name: str = "",
              last_name: str = "") -> VerificationResult: ...


class UnavailableProvider:
    """The default. Says "we don't know" rather than inventing an answer."""

    name = "unavailable"

    def check(self, **kwargs) -> VerificationResult:
        return VerificationResult(
            status=STATUS_UNVERIFIED, source=self.name,
            detail="No verification source is configured. Set "
                   "license_verify_provider once you have one.")


class ManualProvider:
    """Records a human check against the issuing board.

    Worth having on its own: it turns "someone probably looked" into a dated,
    attributable record, which is what an audit actually needs.
    """

    name = "manual"

    def check(self, *, license_type: str, state_code: str, license_number: str,
              first_name: str = "", last_name: str = "",
              status: str = STATUS_ACTIVE, expiry_date: Optional[date] = None,
              checked_by: str = "", **_) -> VerificationResult:
        if status not in VERIFIED_STATUSES:
            status = STATUS_UNVERIFIED
        return VerificationResult(
            status=status, source=self.name, expiry_date=expiry_date,
            licensee_name=f"{first_name} {last_name}".strip() or None,
            detail=f"Checked against the {state_code} board"
                   + (f" by {checked_by}" if checked_by else ""),
            raw={"manual": True, "checked_by": checked_by})


_PROVIDERS: dict[str, VerificationProvider] = {
    UnavailableProvider.name: UnavailableProvider(),
    ManualProvider.name: ManualProvider(),
}


def register(provider: VerificationProvider) -> None:
    """Plug in a real source (Nursys, a state board, an aggregator)."""
    _PROVIDERS[provider.name] = provider


def get_provider(name: str | None = None) -> VerificationProvider:
    from ..config import settings

    key = name or getattr(settings, "license_verify_provider", "") or "unavailable"
    provider = _PROVIDERS.get(key)
    if provider is None:
        logger.warning("Unknown licence provider %r; falling back to unavailable", key)
        return _PROVIDERS["unavailable"]
    return provider


def verify(*, license_type: str, state_code: str, license_number: str,
           first_name: str = "", last_name: str = "",
           provider: str | None = None, **extra) -> VerificationResult:
    """Check one licence. Never raises — a source being down is an unknown, not
    an error the caller has to handle."""
    impl = get_provider(provider)
    try:
        return impl.check(license_type=(license_type or "").upper().strip(),
                          state_code=(state_code or "").upper().strip(),
                          license_number=(license_number or "").strip(),
                          first_name=first_name, last_name=last_name, **extra)
    except Exception as exc:                       # noqa: BLE001
        logger.exception("Licence check failed via %s", impl.name)
        return VerificationResult(status=STATUS_UNVERIFIED, source=impl.name,
                                  detail=f"Verification failed: {exc}"[:200])
