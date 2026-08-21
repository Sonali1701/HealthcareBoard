"""GSA per-diem and pay-package calculator schemas."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator


class GSARates(BaseModel):
    city: str
    state_code: str
    fiscal_year: int
    lodging: float            # max $/night
    mie: float                # meals & incidentals $/day
    weekly_lodging: float     # lodging * 7
    weekly_mie: float         # mie * 7
    weekly_max_tax_free: float
    monthly: dict[str, float] = Field(default_factory=dict)  # month -> lodging rate
    source: str               # "api.gsa.gov" | "fallback"
    fallback_reason: Optional[str] = None


class PayPackageRequest(BaseModel):
    # Bill / contract terms
    bill_rate: float = Field(gt=0, description="What the client is billed, $/hr")
    contract_weeks: int = Field(default=13, gt=0, le=104)
    hours_per_week: float = Field(default=36, gt=0, le=168)
    ot_hours_per_week: float = Field(default=0, ge=0, le=100)
    # Shift length drives California daily overtime (hours 8-12/day at 1.5x,
    # >12/day at 2x). Defaults to a 12h shift when California overtime applies.
    shift_length_hours: Optional[float] = Field(default=None, gt=0, le=24)
    # None = auto (California daily OT turns on when the assignment is in CA);
    # set False to force the standard weekly-only model even for a CA role.
    ca_overtime: Optional[bool] = None
    margin_pct: float = Field(default=20, ge=0, le=80, description="Agency margin %")
    burden_multiplier: float = Field(default=1.20, ge=1.0, le=2.0)
    benefits_cost_per_hr: float = Field(default=0, ge=0, le=100)
    # Location (drives GSA lookup)
    city: str = Field(default="Houston", min_length=1, max_length=120)
    state_code: str = Field(default="TX", pattern=r"^[A-Za-z]{2}$")
    # Travel dates — GSA lodging rates are seasonal (per-month), so the dates
    # decide which month's rate applies. Optional: if omitted we use the
    # highest monthly rate. If only a start is given, contract_weeks sets the end.
    travel_start: Optional[date] = None
    travel_end: Optional[date] = None
    # Optional overrides (skip GSA lookup if both provided)
    gsa_lodging_override: Optional[float] = Field(default=None, ge=0)
    mie_override: Optional[float] = Field(default=None, ge=0)
    # Extras
    completion_bonus: float = Field(default=0, ge=0)
    travel_allowance: float = Field(default=0, ge=0)
    reimbursements: float = Field(default=0, ge=0)
    # Tax assumption for net estimate
    tax_rate: float = Field(default=0.28, ge=0, le=0.6)

    @field_validator("city")
    @classmethod
    def _clean_city(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("city is required")
        return value

    @field_validator("state_code")
    @classmethod
    def _normalise_state(cls, value: str) -> str:
        return value.upper()

    @model_validator(mode="after")
    def _pay_pool_must_be_positive(self):
        """Reject assumptions that would create a negative clinician pay rate."""
        available = self.bill_rate * (1 - self.margin_pct / 100) - self.benefits_cost_per_hr
        if available <= 0:
            raise ValueError(
                "bill rate after margin and hourly benefits must leave a positive pay pool"
            )
        return self


class PayOption(BaseModel):
    label: str
    taxable_rate: float
    weekly_taxable_gross: float
    weekly_tax_free: float
    weekly_total: float
    ot_rate: float
    dt_rate: float = 0.0          # 2x double-time rate (California)
    est_weekly_net: float
    contract_total: float


class PayPackageResponse(BaseModel):
    gsa: GSARates
    option_w2: PayOption
    option_perdiem: PayOption
    perdiem_advantage: float  # contract_total difference (B - A)
    breakdown: dict


class PayPackageSaveRequest(BaseModel):
    label: Optional[str] = None
    profile_id: Optional[str] = None
    job_id: Optional[str] = None
    inputs: PayPackageRequest
    result: PayPackageResponse
