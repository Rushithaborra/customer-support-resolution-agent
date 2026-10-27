"""Helpdesk client: customer-visible replies, sent with an idempotency key so a
retry never messages the customer twice."""

from datetime import datetime

from pydantic import BaseModel

from app.integrations.http import ServiceClient, parse
from app.integrations.results import Ok, Result


class HelpdeskReply(BaseModel):
    reply_id: str
    ticket_external_id: str
    message: str
    created_at: datetime


class HelpdeskClient:
    def __init__(self, http: ServiceClient):
        self._http = http

    def post_reply(self, *, ticket_external_id: str, message: str,
                   idempotency_key: str) -> Result[HelpdeskReply]:
        result = self._http.call("POST", f"/helpdesk/tickets/{ticket_external_id}/replies",
                                 json={"message": message}, idempotency_key=idempotency_key)
        return parse(result, HelpdeskReply, is_write=True)

    def list_replies(self, ticket_external_id: str) -> Result[list[HelpdeskReply]]:
        result = self._http.call("GET", f"/helpdesk/tickets/{ticket_external_id}/replies")
        if not isinstance(result, Ok):
            return result
        return Ok([HelpdeskReply.model_validate(r) for r in result.value.get("replies", [])])
