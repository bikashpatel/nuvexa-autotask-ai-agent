from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app import __version__
from app.api.deps import SessionDep
from app.schemas.api import HealthOut

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live", response_model=HealthOut)
async def live() -> HealthOut:
    return HealthOut(status="ok", version=__version__)


@router.get("/ready", response_model=HealthOut, responses={503: {"model": HealthOut}})
async def ready(session: SessionDep) -> HealthOut | JSONResponse:
    try:
        await session.execute(text("SELECT 1"))
    except Exception:
        body = HealthOut(status="error", version=__version__, checks={"database": "unavailable"})
        return JSONResponse(status_code=503, content=body.model_dump())
    return HealthOut(status="ok", version=__version__, checks={"database": "ok"})
