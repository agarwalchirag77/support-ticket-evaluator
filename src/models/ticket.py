"""Pydantic models for raw Zendesk ticket data."""

from __future__ import annotations

from typing import Any, Optional
from pydantic import BaseModel, Field


class TicketVia(BaseModel):
    channel: Optional[str] = None


class CustomField(BaseModel):
    id: Any
    value: Any = None
    resolved_value: Any = None
    raw_title: Optional[str] = None


class ZendeskTicket(BaseModel):
    id: int
    subject: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    type: Optional[str] = None
    priority: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    solved_at: Optional[str] = None
    assignee_id: Optional[int] = None
    requester_id: Optional[int] = None
    group_id: Optional[int] = None
    organization_id: Optional[int] = None
    tags: list[str] = Field(default_factory=list)
    via: Optional[TicketVia] = None
    custom_fields: list[CustomField] = Field(default_factory=list)

    def channel(self) -> str:
        """Return normalised channel: 'chat', 'email', or 'other'."""
        raw = (self.via.channel or "") if self.via else ""
        if raw in ("native_messaging", "chat", "chat_transcript"):
            return "chat"
        if raw == "email":
            return "email"
        return "other"


class TimeValue(BaseModel):
    calendar: Optional[float] = None
    business: Optional[float] = None


class TicketMetric(BaseModel):
    id: Optional[int] = None
    ticket_id: Optional[int] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    assigned_at: Optional[str] = None
    initially_assigned_at: Optional[str] = None
    solved_at: Optional[str] = None
    latest_comment_added_at: Optional[str] = None
    replies: Optional[int] = None
    reopens: Optional[int] = None
    reply_time_in_seconds: Optional[TimeValue] = None
    reply_time_in_minutes: Optional[TimeValue] = None
    full_resolution_time_in_minutes: Optional[TimeValue] = None
    first_resolution_time_in_minutes: Optional[TimeValue] = None
    agent_wait_time_in_minutes: Optional[TimeValue] = None
    requester_wait_time_in_minutes: Optional[TimeValue] = None
    on_hold_time_in_minutes: Optional[TimeValue] = None
    requester_wait_time_in_minutes: Optional[TimeValue] = None


class TicketComment(BaseModel):
    id: Optional[int] = None
    author_id: Optional[int] = None
    body: Optional[str] = None
    plain_body: Optional[str] = None
    public: Optional[bool] = None
    created_at: Optional[str] = None
    via: Optional[TicketVia] = None


class RawTicket(BaseModel):
    """Composite ticket object matching the JSON file schema."""

    Ticket_Metadata: dict
    Ticket_Metrics: dict
    Ticket_Comments: dict

    def get_ticket(self) -> ZendeskTicket:
        return ZendeskTicket(**self.Ticket_Metadata.get("ticket", {}))

    def get_metrics(self) -> TicketMetric:
        return TicketMetric(**self.Ticket_Metrics.get("ticket_metric", {}))

    def get_comments(self) -> list[TicketComment]:
        return [TicketComment(**c) for c in self.Ticket_Comments.get("comments", [])]
