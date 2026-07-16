"""GSA per-diem rate service + travel-nurse pay package calculator.

Mirrors the logic the frontend prototypes (healthboard-gsa-pay-calculator.html)
but runs server-side so the api.data.gov key is never exposed to the browser.

Rates are fetched live from https://api.gsa.gov/travel/perdiem/v2 when a key is
configured; otherwise a baked-in FY2025 fallback table is used.
"""
from __future__ import annotations

import httpx

from ..config import settings
from ..schemas.gsa import (
    GSARates,
    PayOption,
    PayPackageRequest,
    PayPackageResponse,
)

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# Standard CONUS fallback (FY2025): lodging $107/night, M&IE $68/day.
STANDARD_CONUS = {"lodging": 107.0, "mie": 68.0}

# A small offline table of common travel-nurse destinations (FY2025-ish).
FALLBACK_CITY_RATES: dict[str, dict] = {
    "houston-TX": {"lodging": 132.0, "mie": 68.0},
    "dallas-TX": {"lodging": 138.0, "mie": 74.0},
    "austin-TX": {"lodging": 165.0, "mie": 80.0},
    "los angeles-CA": {"lodging": 182.0, "mie": 86.0},
    "san francisco-CA": {"lodging": 270.0, "mie": 92.0},
    "new york-NY": {"lodging": 297.0, "mie": 92.0},
    "chicago-IL": {"lodging": 218.0, "mie": 80.0},
    "seattle-WA": {"lodging": 201.0, "mie": 86.0},
    "phoenix-AZ": {"lodging": 140.0, "mie": 74.0},
    "denver-CO": {"lodging": 199.0, "mie": 80.0},
    "miami-FL": {"lodging": 192.0, "mie": 80.0},
    "boston-MA": {"lodging": 285.0, "mie": 92.0},
}

FALLBACK_STATE_RATES: dict[str, dict] = {
    "TX": {"lodging": 110.0, "mie": 68.0},
    "CA": {"lodging": 160.0, "mie": 80.0},
    "NY": {"lodging": 150.0, "mie": 74.0},
    "FL": {"lodging": 120.0, "mie": 68.0},
}


def _fallback_rates(city: str, state_code: str, reason: str) -> GSARates:
    key = f"{city.strip().lower()}-{state_code.strip().upper()}"
    rate = (
        FALLBACK_CITY_RATES.get(key)
        or FALLBACK_STATE_RATES.get(state_code.strip().upper())
        or STANDARD_CONUS
    )
    return _build_rates(city, state_code, rate["lodging"], rate["mie"],
                        source="fallback", fallback_reason=reason, monthly={})


def _build_rates(city, state_code, lodging, mie, *, source, fallback_reason=None,
                 monthly=None) -> GSARates:
    return GSARates(
        city=city,
        state_code=state_code.upper(),
        fiscal_year=settings.gsa_fiscal_year,
        lodging=round(lodging, 2),
        mie=round(mie, 2),
        weekly_lodging=round(lodging * 7, 2),
        weekly_mie=round(mie * 7, 2),
        weekly_max_tax_free=round((lodging + mie) * 7, 2),
        monthly=monthly or {},
        source=source,
        fallback_reason=fallback_reason,
    )


async def get_gsa_rates(city: str, state_code: str) -> GSARates:
    """Fetch per-diem rates, falling back gracefully on any error."""
    if not settings.gsa_api_key:
        return _fallback_rates(city, state_code, "no API key configured")

    base = settings.gsa_base_url.rstrip("/")
    year = settings.gsa_fiscal_year
    url = (
        f"{base}/rates/city/{city.strip()}/state/{state_code.strip().upper()}"
        f"/year/{year}?api_key={settings.gsa_api_key}"
    )
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            res = await client.get(url)
            res.raise_for_status()
            data = res.json()
        return _parse_gsa_payload(data, city, state_code)
    except (httpx.HTTPError, KeyError, ValueError, IndexError) as exc:
        return _fallback_rates(city, state_code, f"GSA API error: {exc}")


def _parse_gsa_payload(data: dict, city: str, state_code: str) -> GSARates:
    """Parse the GSA v2 response shape into our GSARates model.

    GSA returns: {"rates": [{"rate": [{"months": {"month": [{"short": "Jan",
    "value": 132}, ...]}, "meals": 68, "city": "Houston", ...}]}]}
    """
    rates = data.get("rates") or []
    if not rates:
        return _fallback_rates(city, state_code, "empty GSA response")

    rate_block = rates[0]["rate"][0]
    meals = float(rate_block.get("meals", STANDARD_CONUS["mie"]))

    monthly: dict[str, float] = {}
    months = rate_block.get("months", {}).get("month", [])
    for m in months:
        short = m.get("short")
        val = m.get("value")
        if short and val is not None:
            monthly[short] = float(val)

    # Annual lodging = max monthly rate (or first available).
    lodging = max(monthly.values()) if monthly else STANDARD_CONUS["lodging"]
    resolved_city = rate_block.get("city") or city
    return _build_rates(resolved_city, state_code, lodging, meals,
                        source="api.gsa.gov", monthly=monthly)


def calculate_pay_package(req: PayPackageRequest, rates: GSARates) -> PayPackageResponse:
    """Compute W2-only vs W2+per-diem pay options for a contract.

    Mirrors the prototype's allocation model:
      bill_rate -> agency margin -> burden -> W2 pay pool / tax-free stipends.
    """
    weekly_lodging = (
        req.gsa_lodging_override * 7
        if req.gsa_lodging_override is not None
        else rates.weekly_lodging
    )
    weekly_mie = (
        req.mie_override * 7
        if req.mie_override is not None
        else rates.weekly_mie
    )
    weekly_tax_free = round(weekly_lodging + weekly_mie, 2)

    reg_hours = req.hours_per_week
    ot_hours = req.ot_hours_per_week

    # Pool available for candidate pay after margin + benefits, per hour.
    margin_per_hr = req.bill_rate * (req.margin_pct / 100.0)
    pool_per_hr = req.bill_rate - margin_per_hr - req.benefits_cost_per_hr

    # ---- Option A: pure W2 ----
    w2_taxable_rate = round(pool_per_hr / req.burden_multiplier, 2)
    w2_ot_rate = round(w2_taxable_rate * 1.5, 2)
    w2_weekly_gross = round(
        w2_taxable_rate * reg_hours + w2_ot_rate * ot_hours, 2
    )
    w2_weekly_net = round(w2_weekly_gross * (1 - req.tax_rate), 2)
    w2_extras = req.completion_bonus + req.travel_allowance + req.reimbursements
    w2_contract_total = round(w2_weekly_gross * req.contract_weeks + w2_extras, 2)

    option_w2 = PayOption(
        label="Pure W2",
        taxable_rate=w2_taxable_rate,
        weekly_taxable_gross=w2_weekly_gross,
        weekly_tax_free=0.0,
        weekly_total=w2_weekly_gross,
        ot_rate=w2_ot_rate,
        est_weekly_net=w2_weekly_net,
        contract_total=w2_contract_total,
    )

    # ---- Option B: W2 + per-diem (tax-free stipends carved out of the pool) ----
    # Convert weekly tax-free stipend to an effective per-hour reduction.
    stipend_per_hr = weekly_tax_free / reg_hours if reg_hours else 0
    pd_taxable_rate = round(
        max(pool_per_hr - stipend_per_hr, 0) / req.burden_multiplier, 2
    )
    pd_ot_rate = round(pd_taxable_rate * 1.5, 2)
    pd_weekly_taxable = round(
        pd_taxable_rate * reg_hours + pd_ot_rate * ot_hours, 2
    )
    pd_weekly_total = round(pd_weekly_taxable + weekly_tax_free, 2)
    # Net: only the taxable portion is taxed; stipend is tax-free.
    pd_weekly_net = round(
        pd_weekly_taxable * (1 - req.tax_rate) + weekly_tax_free, 2
    )
    pd_contract_total = round(pd_weekly_total * req.contract_weeks + w2_extras, 2)

    option_perdiem = PayOption(
        label="W2 + Per Diem (GSA)",
        taxable_rate=pd_taxable_rate,
        weekly_taxable_gross=pd_weekly_taxable,
        weekly_tax_free=weekly_tax_free,
        weekly_total=pd_weekly_total,
        ot_rate=pd_ot_rate,
        est_weekly_net=pd_weekly_net,
        contract_total=pd_contract_total,
    )

    advantage = round(
        pd_weekly_net * req.contract_weeks - w2_weekly_net * req.contract_weeks, 2
    )

    breakdown = {
        "bill_rate": req.bill_rate,
        "agency_margin_per_hr": round(margin_per_hr, 2),
        "benefits_per_hr": req.benefits_cost_per_hr,
        "pool_per_hr": round(pool_per_hr, 2),
        "burden_multiplier": req.burden_multiplier,
        "weekly_lodging_stipend": round(weekly_lodging, 2),
        "weekly_mie_stipend": round(weekly_mie, 2),
        "weekly_tax_free_total": weekly_tax_free,
        "contract_weeks": req.contract_weeks,
        "extras": {
            "completion_bonus": req.completion_bonus,
            "travel_allowance": req.travel_allowance,
            "reimbursements": req.reimbursements,
        },
    }

    return PayPackageResponse(
        gsa=rates,
        option_w2=option_w2,
        option_perdiem=option_perdiem,
        perdiem_advantage=advantage,
        breakdown=breakdown,
    )
