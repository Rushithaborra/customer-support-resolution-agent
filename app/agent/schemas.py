"""What the model must return. Validators here enforce what a JSON schema can't."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ORDER_ID_PATTERN = r"^ord_[a-z0-9]+$"
EVIDENCE_REF_PATTERN = r"^(list_my_orders|[a-z_]+:ord_[a-z0-9]+)$"

Category = Literal["order_status", "refund_request", "damaged_item", "unclassified"]
ORDER_SPECIFIC: set[str] = {"order_status", "refund_request", "damaged_item"}


class TicketTriage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: Category
    order_id: str | None = Field(
        pattern=ORDER_ID_PATTERN,
        description="The order the customer refers to, e.g. ord_1001, or null if none is given")
    customer_intent: str = Field(min_length=1, max_length=300, description="One sentence: what the customer wants")
    needs_human: bool = Field(description="True if this can't be handled by the standard flows")
    rationale: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def order_specific_needs_order(self) -> "TicketTriage":
        if self.category in ORDER_SPECIFIC and self.order_id is None and not self.needs_human:
            raise ValueError(f"category '{self.category}' needs an order_id, or needs_human must be true")
        return self


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    statement: str = Field(min_length=1, max_length=300)
    evidence_ref: str = Field(pattern=EVIDENCE_REF_PATTERN,
                              description="The tool call this rests on, e.g. get_return_status:ord_1001")


class InvestigationSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    findings: list[Finding] = Field(min_length=1, max_length=10)
    recommended_next_step: Literal["propose_refund", "explain_policy", "report_order_status", "escalate"]
    customer_reply_draft: str = Field(min_length=1, max_length=1500)
