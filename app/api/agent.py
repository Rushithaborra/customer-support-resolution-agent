"""Staff routes to run the agent on a ticket and read the result."""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.agent.runner import run_agent
from app.auth import Staff, get_current_staff
from app.db.models import WorkflowRun
from app.db.session import get_db
from app.integrations.deps import get_shipping_client
from app.integrations.shipping import ShippingClient
from app.llm.deps import get_llm
from app.llm.types import LLM
from app.policy.embedder import Embedder, get_embedder
from app.schemas import AgentRunOut
from app.services.errors import NotFound

router = APIRouter(prefix="/staff", tags=["agent"])


@router.post("/tickets/{ticket_id}/agent-runs", response_model=AgentRunOut, status_code=201)
def start(ticket_id: str, staff: Staff = Depends(get_current_staff), db: Session = Depends(get_db),
          llm: LLM = Depends(get_llm), shipping: ShippingClient = Depends(get_shipping_client),
          embedder: Embedder = Depends(get_embedder)):
    return run_agent(db, llm, shipping, ticket_id, actor=staff.actor, embedder=embedder)


@router.get("/agent-runs/{run_id}", response_model=AgentRunOut)
def get(run_id: str, _: Staff = Depends(get_current_staff), db: Session = Depends(get_db)):
    run = db.get(WorkflowRun, run_id)
    if run is None:
        raise NotFound("Run")
    return run
