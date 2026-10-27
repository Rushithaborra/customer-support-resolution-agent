from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api import customers, orders, tickets
from app.db.session import get_db
from app.services.errors import NotFound

app = FastAPI(title="Support Resolution Agent", version="0.1.0")
app.include_router(customers.router)
app.include_router(orders.router)
app.include_router(tickets.router)


@app.exception_handler(NotFound)
def not_found(_: Request, exc: NotFound):
    # Same body whether the record is missing or belongs to someone else.
    return JSONResponse(status_code=404, content={"detail": f"{exc.resource} not found"})


@app.get("/health")
def health(db: Session = Depends(get_db)):
    """Liveness + DB connectivity. A health check that ignores the DB
    would report 'ok' while every real request fails."""
    try:
        db.execute(text("SELECT 1"))
        return {"status": "ok", "database": "ok"}
    except Exception:
        return JSONResponse(status_code=503, content={"status": "degraded", "database": "unreachable"})
