"""Shared QC-exclusion rules.

Single source of truth for deciding whether a ticket is excluded from QC
(duplicates, alert tickets, spam, side-conversation reviews, unassigned/bot
tickets, feature-request forms).

Used in two places with the SAME raw ticket dict shape:
  - Fetch time: the raw stub from the Zendesk incremental export
    (src/clients/zendesk.py fetch_tickets_since)
  - Re-evaluate time: Ticket_Metadata.ticket from an on-disk ticket JSON
    (src/pipeline/orchestrator.py)

Both carry: tags, ticket_form_id, via.channel, assignee_id.
"""

from __future__ import annotations

from typing import Optional

from src.config import ExclusionsConfig


def exclusion_reason(ticket: dict, excl: ExclusionsConfig) -> Optional[str]:
    """Return "category (detail)" if the ticket matches an exclusion rule, else None.

    Check order (first match wins — for overlap tickets the earlier category
    in the config dict order is reported):
      1. tag categories        2. form categories
      3. excluded channels     4. unassigned (no assignee)
    """
    tag_set = set(ticket.get("tags") or [])
    for category, cat_tags in excl.tag_categories.items():
        hit = tag_set.intersection(cat_tags)
        if hit:
            return f"{category} (tag: {sorted(hit)[0]})"

    form_id = ticket.get("ticket_form_id")
    if form_id is not None:
        for category, form_ids in excl.form_categories.items():
            if form_id in form_ids:
                return f"{category} (form: {form_id})"

    channel = ((ticket.get("via") or {}).get("channel") or "")
    if channel and channel in excl.exclude_channels:
        return f"review (channel: {channel})"

    if excl.exclude_unassigned and ticket.get("assignee_id") is None:
        return "unassigned (assignee_id is null)"

    return None
