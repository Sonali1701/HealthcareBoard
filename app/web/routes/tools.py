"""In-app tools: travel-nurse pay-package calculator (server-rendered)."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request

from ..core import current_user, render
from ...services.gsa import calculate_pay_package, get_gsa_rates
from ...schemas.gsa import PayPackageRequest

router = APIRouter(prefix="/tools", tags=["web-tools"])


@router.get("/pay-calculator")
def calculator(request: Request, user=Depends(current_user)):
    return render(request, "tools/pay_calculator.html",
                  {"active": "tools", "result": None,
                   "f": {"bill_rate": 82, "contract_weeks": 13, "hours_per_week": 36,
                         "shift_length_hours": 12,
                         "margin_pct": 20, "city": "Houston", "state_code": "TX",
                         "travel_start": "", "travel_end": ""}, "user": user})


@router.post("/pay-calculator")
async def calculate(request: Request, user=Depends(current_user),
                    bill_rate: Annotated[float, Form()] = 82,
                    contract_weeks: Annotated[int, Form()] = 13,
                    hours_per_week: Annotated[float, Form()] = 36,
                    shift_length_hours: Annotated[float, Form()] = 12,
                    margin_pct: Annotated[float, Form()] = 20,
                    city: Annotated[str, Form()] = "Houston",
                    state_code: Annotated[str, Form()] = "TX",
                    travel_start: Annotated[str, Form()] = "",
                    travel_end: Annotated[str, Form()] = ""):
    req = PayPackageRequest(bill_rate=bill_rate, contract_weeks=contract_weeks,
                            hours_per_week=hours_per_week, shift_length_hours=shift_length_hours,
                            margin_pct=margin_pct,
                            city=city.strip() or "Houston", state_code=state_code.strip().upper() or "TX",
                            travel_start=(travel_start.strip() or None),
                            travel_end=(travel_end.strip() or None))
    rates = await get_gsa_rates(req.city, req.state_code)
    result = calculate_pay_package(req, rates)
    return render(request, "tools/pay_calculator.html",
                  {"active": "tools", "result": result,
                   "f": {"bill_rate": bill_rate, "contract_weeks": contract_weeks,
                         "hours_per_week": hours_per_week, "shift_length_hours": shift_length_hours,
                         "margin_pct": margin_pct,
                         "city": req.city, "state_code": req.state_code,
                         "travel_start": travel_start.strip(), "travel_end": travel_end.strip()}, "user": user})
