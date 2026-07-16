"""GSA per-diem rates + pay package calculator endpoints.

The api.data.gov key lives server-side (settings.gsa_api_key) so the frontend
never exposes it — the prototype's TODO ("route through your backend proxy").
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select

from ..deps import CurrentUser, DbSession
from ..models import PayPackage
from ..schemas.gsa import (
    GSARates,
    PayPackageRequest,
    PayPackageResponse,
    PayPackageSaveRequest,
)
from ..services.gsa import calculate_pay_package, get_gsa_rates

router = APIRouter(prefix="/api/gsa", tags=["gsa-pay"])


@router.get("/rates", response_model=GSARates)
async def gsa_rates(
    city: str = Query(..., examples=["Houston"]),
    state: str = Query(..., min_length=2, max_length=2, examples=["TX"]),
):
    return await get_gsa_rates(city, state)


@router.post("/pay-package/calculate", response_model=PayPackageResponse)
async def calculate(req: PayPackageRequest):
    rates = await get_gsa_rates(req.city, req.state_code)
    return calculate_pay_package(req, rates)


@router.post("/pay-package/save")
def save_package(body: PayPackageSaveRequest, user: CurrentUser, db: DbSession):
    pkg = PayPackage(
        created_by_user_id=user.user_id,
        profile_id=body.profile_id,
        job_id=body.job_id,
        label=body.label,
        inputs=body.inputs.model_dump(mode="json"),
        result=body.result.model_dump(mode="json"),
    )
    db.add(pkg)
    db.commit()
    db.refresh(pkg)
    return {"package_id": pkg.package_id}


@router.get("/pay-package/saved")
def list_saved(user: CurrentUser, db: DbSession):
    rows = db.scalars(
        select(PayPackage).where(PayPackage.created_by_user_id == user.user_id)
        .order_by(PayPackage.created_at.desc())
    ).all()
    return [
        {"package_id": p.package_id, "label": p.label, "created_at": p.created_at,
         "result": p.result}
        for p in rows
    ]
