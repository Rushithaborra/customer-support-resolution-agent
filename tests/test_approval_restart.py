"""Part 9 done-when: approve, reject and expire each follow their path after a restart.

"Restart" = the graph and checkpointer objects that paused the run are thrown away, and the
decision is handled by a brand-new PostgresSaver on new connections, the way a fresh process would.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.agent.checkpoint import get_checkpointer, open_checkpointer
from app.agent.graph import AgentDeps, build_graph
from app.agent.resume import resume_for_proposal
from app.db.models import ActionProposal, RefundOperation, Ticket, WorkflowRun
from app.main import app
from app.services.approvals import expire_stale
from tests.agent_scripts import draft, open_ticket, refund_script
from tests.conftest import as_reviewer, as_staff, inject

REVIEWER = as_reviewer()
STAFF = as_staff()


def graph_next(db, thread_id: str) -> tuple:
    """Where the workflow stands, read through a fresh saver (nothing shared with the run)."""
    saver, pool = open_checkpointer()
    try:
        return build_graph(AgentDeps(db=db), checkpointer=saver).get_state(
            {"configurable": {"thread_id": thread_id}}).next
    finally:
        pool.close()


@pytest.fixture
def restart():
    """Swap the app's checkpointer for a brand-new one, like a process restart."""
    pools = []

    def do():
        saver, pool = open_checkpointer()
        pools.append(pool)
        app.dependency_overrides[get_checkpointer] = lambda: saver
        return saver
    yield do
    for p in pools:
        p.close()


@pytest.fixture
def paused(agent, client, db):
    """An agent run for ord_1001, paused at await_approval with a pending proposal."""
    run = agent(open_ticket(client, "I returned my headphones, where's my refund?", "ord_1001"),
                refund_script(draft("ord_1001")))
    assert run.status == "paused"
    assert graph_next(db, run.thread_id) == ("await_approval",)
    return run, db.get(ActionProposal, run.outcome["proposal_id"])


def refunds_at_provider(mock_admin, proposal_id: str) -> list:
    return mock_admin.get("/payment/refunds", params={"idempotency_key": f"refund-{proposal_id}"}).json()["refunds"]


# ---------------------------------------------------------------- the three paths


def test_approve_after_restart_executes_once(paused, restart, staff_client, mock_admin, db):
    run, p = paused
    restart()
    r = staff_client.post(f"/staff/proposals/{p.id}/approve", headers=REVIEWER, json={"payload_hash": p.payload_hash})
    assert r.status_code == 200, r.json()
    body = r.json()
    assert (body["run_status"], body["refund"]["status"], body["proposal"]["status"]) == \
        ("completed", "succeeded", "executed")
    assert len(refunds_at_provider(mock_admin, p.id)) == 1
    assert db.get(Ticket, run.ticket_id).status == "resolved"
    assert db.get(WorkflowRun, run.id).outcome["decision"] == "approved"
    assert graph_next(db, run.thread_id) == ()  # workflow finished


def test_reject_after_restart_finishes_without_refund(paused, restart, staff_client, mock_admin, db):
    run, p = paused
    restart()
    r = staff_client.post(f"/staff/proposals/{p.id}/reject", headers=REVIEWER)
    assert (r.json()["proposal"]["status"], r.json()["run_status"], r.json()["refund"]) == ("rejected", "completed", None)
    assert refunds_at_provider(mock_admin, p.id) == []
    assert db.get(Ticket, run.ticket_id).status == "open"
    assert db.get(WorkflowRun, run.id).outcome["decision"] == "rejected"
    assert graph_next(db, run.thread_id) == ()


def test_expire_after_restart_escalates(paused, restart, staff_client, mock_admin, db):
    run, p = paused
    p.created_at = datetime.now(timezone.utc) - timedelta(hours=49)  # nobody reviewed it for 49h
    db.commit()
    restart()
    r = staff_client.post("/staff/maintenance/expire-proposals", headers=STAFF)
    assert r.json() == {"expired": [p.id]}
    assert db.get(ActionProposal, p.id).status == "expired"
    assert refunds_at_provider(mock_admin, p.id) == []
    assert db.get(Ticket, run.ticket_id).status == "escalated"
    finished = db.get(WorkflowRun, run.id)
    assert (finished.status, finished.outcome["decision"]) == ("escalated", "expired")
    assert graph_next(db, run.thread_id) == ()


# ---------------------------------------------------------------- safety around resume


def test_approving_twice_and_resuming_a_finished_run_refund_once(paused, staff_client, mock_admin, db, checkpointer):
    run, p = paused
    staff_client.post(f"/staff/proposals/{p.id}/approve", headers=REVIEWER, json={"payload_hash": p.payload_hash})
    again = staff_client.post(f"/staff/proposals/{p.id}/approve", headers=REVIEWER, json={"payload_hash": p.payload_hash})
    assert again.status_code == 409 and again.json()["detail"]["code"] == "not_pending"
    resume_for_proposal(db, p.id, "approved", checkpointer=checkpointer, payment=None)  # no-op: not paused
    assert len(refunds_at_provider(mock_admin, p.id)) == 1
    assert db.query(RefundOperation).filter_by(proposal_id=p.id).count() == 1


def test_forged_resume_without_approval_executes_nothing(paused, db, mock_admin, checkpointer):
    run, p = paused
    resume_for_proposal(db, p.id, "approved", checkpointer=checkpointer, payment=None)  # nobody approved
    assert graph_next(db, run.thread_id) == ("await_approval",)  # back to waiting
    assert db.get(ActionProposal, p.id).status == "pending_approval"
    assert refunds_at_provider(mock_admin, p.id) == []


def test_provider_down_during_execution_then_reconciled_once(paused, staff_client, mock_admin, db):
    run, p = paused
    inject(mock_admin, "error_503", "payment.create_refund")
    body = staff_client.post(f"/staff/proposals/{p.id}/approve", headers=REVIEWER,
                             json={"payload_hash": p.payload_hash}).json()
    assert body["refund"]["status"] == "submitted"
    assert db.get(WorkflowRun, run.id).outcome["execution"] == {"status": "provider_unavailable"}
    assert db.get(Ticket, run.ticket_id).status == "awaiting_provider"
    op = staff_client.post(f"/staff/refunds/{body['refund']['id']}/refresh", headers=STAFF).json()
    assert op["status"] == "succeeded"
    assert len(refunds_at_provider(mock_admin, p.id)) == 1


def test_superseded_agent_proposal_is_invalidated(paused, staff_client, mock_admin, db):
    run, p = paused
    r = staff_client.post(f"/staff/tickets/{run.ticket_id}/proposals", headers=STAFF)  # a human re-proposes
    assert r.status_code == 201
    assert db.get(ActionProposal, p.id).status == "superseded"
    old = db.get(WorkflowRun, run.id)
    assert (old.status, old.outcome["decision"]) == ("completed", "invalidated")
    assert graph_next(db, run.thread_id) == ()
    assert refunds_at_provider(mock_admin, p.id) == []


# ---------------------------------------------------------------- expiry edges


def test_47_hours_is_not_expired(paused, db):
    _, p = paused
    assert expire_stale(db, datetime.now(timezone.utc) + timedelta(hours=47)) == []
    assert db.get(ActionProposal, p.id).status == "pending_approval"


def test_unused_approval_expires(staff_client, db):
    tid = open_ticket(staff_client, "refund?", "ord_1001")
    p = staff_client.post(f"/staff/tickets/{tid}/proposals", headers=STAFF).json()  # manual: not executed on approve
    staff_client.post(f"/staff/proposals/{p['id']}/approve", headers=REVIEWER, json={"payload_hash": p["payload_hash"]})
    assert expire_stale(db, datetime.now(timezone.utc) + timedelta(hours=25)) == [p["id"]]
    assert db.get(ActionProposal, p["id"]).status == "expired"
    assert db.get(Ticket, tid).status == "escalated"  # an approval nobody used goes back to a human
