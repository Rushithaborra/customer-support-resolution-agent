"""Local demo helpers, only when DEV_MODE=true. Otherwise every route here is a 404.

- Persona login: the UI picks Asha, Vikram, staff or reviewer and gets a signed token,
  the same kind `scripts/mint_token.py` prints. A real deployment's tokens come from its IdP.
- Provider faults: forwarded to the mock's admin API, so the browser never holds the mock key.
"""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from app.auth import REVIEWER_ROLE, STAFF_ROLE, issue_token
from app.config import settings
from app.integrations.deps import get_service_client
from app.integrations.http import ServiceClient
from app.integrations.results import Ok
from app.llm.deps import describe_llm


def require_dev_mode() -> None:
    if not settings.dev_mode:
        raise HTTPException(status_code=404, detail="Not Found")


router = APIRouter(prefix="/dev", tags=["dev"], dependencies=[Depends(require_dev_mode)])

PERSONAS = {
    "asha": ("auth|asha", None, "Asha (customer)"),
    "vikram": ("auth|vikram", None, "Vikram (customer)"),
    "priya": ("staff|priya", STAFF_ROLE, "Priya (support staff)"),
    "ravi": ("reviewer|ravi", REVIEWER_ROLE, "Ravi (refund reviewer)"),
}


class TokenIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    persona: Literal["asha", "vikram", "priya", "ravi"]


class FaultIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # The provider failures the demos show. Each applies to the next refund call only.
    kind: Literal["timeout_after_apply", "timeout_before_apply", "error_503", "slow"]


@router.get("/info")
def info():
    return {"model": describe_llm(),
            "personas": [{"id": k, "label": label, "role": role or "customer"}
                         for k, (_, role, label) in PERSONAS.items()]}


@router.post("/token")
def token(body: TokenIn):
    subject, role, label = PERSONAS[body.persona]
    return {"token": issue_token(subject, role=role), "persona": body.persona, "label": label,
            "role": role or "customer"}


def _admin(http: ServiceClient, path: str, json: dict | None = None) -> dict:
    result = http.call("POST", f"/_admin{path}", json=json)
    if not isinstance(result, Ok):
        raise HTTPException(status_code=502, detail=f"mock services: {result}")
    return {"ok": True}


@router.post("/faults")
def add_fault(body: FaultIn, http: ServiceClient = Depends(get_service_client)):
    delay = 6.0 if body.kind in ("timeout_after_apply", "timeout_before_apply", "slow") else 0.0
    return _admin(http, "/faults", {"kind": body.kind, "operation": "payment.create_refund",
                                    "delay_s": delay, "count": 1})


@router.post("/reset")
def reset(http: ServiceClient = Depends(get_service_client)):
    """Clear injected faults and the mock provider's state (not our database)."""
    return _admin(http, "/reset")
