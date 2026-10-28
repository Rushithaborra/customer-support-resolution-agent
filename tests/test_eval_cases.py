"""The eval cases are well formed, and their labels are right.

Label check: each case runs through the real graph with the *oracle* (gold triage, right tools).
With a perfect model, code must reach the case's expected route, reason and amount. If it
doesn't, the label is wrong, and the case would silently skew every report.
"""

from collections import Counter

import pytest
from sqlalchemy import select

from app.db.models import Order, Ticket
from app.db.session import SessionLocal
from evals.cases import load_cases
from evals.grade import grade
from evals.oracle import OracleLLM
from evals.run import run_case

CASES = load_cases()


def test_sixty_cases_split_forty_twenty_with_every_group_in_both():
    assert len(CASES) == 60
    assert Counter(c.split for c in CASES) == {"dev": 40, "heldout": 20}
    groups = {c.group for c in CASES}
    assert len(groups) == 7
    for split in ("dev", "heldout"):
        assert {c.group for c in CASES if c.split == split} == groups, split


def test_eval_orders_never_reuse_seed_ids():
    ids = [fx.id for c in CASES for fx in c.fixtures]
    assert all(i.startswith("ord_e") for i in ids) and len(ids) == len(set(ids))


@pytest.mark.parametrize("mode", ["single", "specialist"])
@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
def test_labels_hold_with_a_perfect_model(case, mode, service_client):
    got = run_case(case, OracleLLM(case), mode, service_client)
    g = grade(case, got)
    assert got["error"] is None, got["error"]
    assert g["reason_ok"], f"expected {case.expect.route}/{case.expect.reasons}, code said {got['route']}/{got['reason']}"
    assert g["amount_ok"] in (True, None)
    assert not g["unsafe_proposal"] and not g["leak"]


def test_cases_leave_nothing_behind(service_client):
    run_case(CASES[0], OracleLLM(CASES[0]), "single", service_client)
    with SessionLocal() as s:
        assert s.scalars(select(Order).where(Order.id.like("ord_e%"))).first() is None
        assert s.scalars(select(Ticket).where(Ticket.customer_message == CASES[0].message)).first() is None
