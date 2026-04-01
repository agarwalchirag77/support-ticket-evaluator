"""Token estimation and ticket truncation for LLM input limits."""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

# Chars-per-token estimate for JSON content (conservative)
_CHARS_PER_TOKEN = 3.5


def estimate_tokens(text: str) -> int:
    """Rough token count estimate — good enough for truncation decisions."""
    return max(1, int(len(text) / _CHARS_PER_TOKEN))


def truncate_ticket_json(ticket_data: dict, max_tokens: int) -> dict:
    """Truncate ticket comments to fit within the token budget.

    Strategy:
    1. Estimate tokens for the full ticket JSON.
    2. If within budget, return as-is.
    3. Otherwise, progressively reduce the comments array:
       - Keep first 5 + last 5 comments
       - If still over, keep first 3 + last 3
       - If still over, keep first 2 + last 2
    """
    full_json = json.dumps(ticket_data)
    if estimate_tokens(full_json) <= max_tokens:
        return ticket_data

    comments_key = "Ticket_Comments"
    if comments_key not in ticket_data:
        logger.warning("Cannot truncate: Ticket_Comments key missing")
        return ticket_data

    comments = ticket_data[comments_key].get("comments", [])
    total = len(comments)

    for keep_each_end in [5, 3, 2, 1]:
        truncated = _truncate_comments(ticket_data, comments, keep_each_end)
        truncated_json = json.dumps(truncated)
        tokens = estimate_tokens(truncated_json)
        logger.debug(
            "Truncation trial: keep %d+%d comments → ~%d tokens (limit: %d)",
            keep_each_end, keep_each_end, tokens, max_tokens,
        )
        if tokens <= max_tokens:
            omitted = total - (keep_each_end * 2)
            if omitted > 0:
                logger.info(
                    "Truncated ticket comments: kept %d+%d of %d (omitted %d)",
                    keep_each_end, keep_each_end, total, omitted,
                )
            return truncated

    # Last resort: keep only metadata and metrics, strip comments entirely
    logger.warning("Ticket still too large after truncation; removing all comments")
    minimal = {k: v for k, v in ticket_data.items() if k != comments_key}
    minimal[comments_key] = {
        "comments": [],
        "_truncation_note": f"All {total} comments removed — ticket too large for LLM context window.",
    }
    return minimal


def _truncate_comments(ticket_data: dict, comments: list, keep_each_end: int) -> dict:
    total = len(comments)
    if total <= keep_each_end * 2:
        return ticket_data

    kept = comments[:keep_each_end] + comments[-keep_each_end:]
    omitted = total - len(kept)
    separator = {
        "_note": f"[{omitted} comments omitted for length — showing first {keep_each_end} and last {keep_each_end}]"
    }

    import copy
    result = copy.deepcopy(ticket_data)
    result["Ticket_Comments"]["comments"] = (
        comments[:keep_each_end] + [separator] + comments[-keep_each_end:]
    )
    return result
