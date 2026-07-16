"""GSA per-diem and pay-package calculator schemas."""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


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
    contract_weeks: int = Field(default=13, gt=0)
    hours_per_week: float = Field(default=36, gt=0)
    ot_hours_per_week: float = 0
    margin_pct: float = Field(default=20, ge=0, le=80, description="Agency margin %")
    burden_multiplier: float = Field(default=1.20, ge=1.0, le=2.0)
    benefits_cost_per_hr: float = 0
    # Location (drives GSA lookup)
    city: str = "Houston"
    state_code: str = "TX"
    # Optional overrides (skip GSA lookup if both provided)
    gsa_lodging_override: Optional[float] = None
    mie_override: Optional[float] = None
    # Extras
    completion_bonus: float = 0
    travel_allowance: float = 0
    reimbursements: float = 0
    # Tax assumption for net estimate
    tax_rate: float = Field(default=0.28, ge=0, le=0.6)


class PayOption(BaseModel):
    label: str
    taxable_rate: float
    weekly_taxable_gross: float
    weekly_tax_free: float
    weekly_total: float
    ot_rate: float
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
