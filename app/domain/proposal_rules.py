"""Code-side validation of an agent-drafted refund proposal. Pure: no DB, no LLM.

Every violation is collected (not just the first), so a reviewer and the eval
see everything that was wrong with the draft."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from app.domain.eligibility import RefundDecision

REQUIRED_EVIDENCE_TOOL = "check_refund_eligibility"  # code's decision must be cited


@dataclass(frozen=True)
class Violation:
    code: str
    detail: str


def _successful_refs(trace: Sequence[Mapping]) -> set[str]:
    """Refs the run can actually back: tool calls that happened AND succeeded."""
    refs = set()
    for call in trace:
        if not call.get("ok"):
            continue
        inp = call.get("input") or {}
        order = inp.get("order_id") if isinstance(inp, Mapping) else None
        refs.add(f"{call['tool']}:{order}" if order else call["tool"])
    return refs


def validate_refund_draft(draft: Mapping, *, resolved_order_id: str, decision: RefundDecision,
                          currency: str, trace: Sequence[Mapping]) -> list[Violation]:
    v: list[Violation] = []
    if not decision.eligible:
        v.append(Violation("not_eligible", decision.reason_code))
    if draft["order_id"] != resolved_order_id:
        v.append(Violation("wrong_order", f"draft {draft['order_id']} != run {resolved_order_id}"))
    if draft["currency"] != currency:
        v.append(Violation("wrong_currency", f"draft {draft['currency']} != order {currency}"))
    if draft["amount_minor"] != decision.refundable_minor:
        # v1 refunds the full refundable amount only: over AND under are both wrong.
        v.append(Violation("amount_mismatch",
                           f"draft {draft['amount_minor']} != refundable {decision.refundable_minor}"))

    backed = _successful_refs(trace)
    unsupported = [r for r in draft["evidence_refs"] if r not in backed]
    if unsupported:
        v.append(Violation("unsupported_evidence", ", ".join(unsupported)))
    if f"{REQUIRED_EVIDENCE_TOOL}:{resolved_order_id}" not in draft["evidence_refs"]:
        v.append(Violation("missing_required_evidence", f"{REQUIRED_EVIDENCE_TOOL}:{resolved_order_id}"))
    return v
