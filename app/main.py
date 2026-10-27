from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api import agent, customers, orders, staff, tickets
from app.db.session import get_db
from app.services.errors import Conflict, NotEligible, NotFound, ProviderUnavailable

app = FastAPI(title="Support Resolution Agent", version="0.1.0")
app.include_router(customers.router)
app.include_router(orders.router)
app.include_router(tickets.router)
app.include_router(staff.router)
app.include_router(agent.router)


@app.exception_handler(NotFound)
def not_found(_: Request, exc: NotFound):
    # Same body whether the record is missing or belongs to someone else.
    return JSONResponse(status_code=404, content={"detail": f"{exc.resource} not found"})


@app.exception_handler(Conflict)
def conflict(_: Request, exc: Conflict):
    return JSONResponse(status_code=409, content={"detail": {"code": exc.code, "message": exc.message}})


@app.exception_handler(NotEligible)
def not_eligible(_: Request, exc: NotEligible):
    return JSONResponse(status_code=422, content={"detail": {"code": exc.reason_code}})


@app.exception_handler(ProviderUnavailable)
def provider_unavailable(_: Request, exc: ProviderUnavailable):
    return JSONResponse(status_code=503, headers={"Retry-After": "5"},
                        content={"detail": {"code": "provider_unavailable", "message": "Try again shortly"}})


@app.get("/health")
def health(db: Session = Depends(get_db)):
    """Liveness + DB connectivity. A health check that ignores the DB
    would report 'ok' while every real request fails."""
    try:
        db.execute(text("SELECT 1"))
        return {"status": "ok", "database": "ok"}
    except Exception:
        return JSONResponse(status_code=503, content={"status": "degraded", "database": "unreachable"})
