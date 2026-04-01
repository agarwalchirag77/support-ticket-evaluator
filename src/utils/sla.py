"""Authoritative SLA calculation and metric rating patch.

This module is the single source of truth for SLA computation.  After every LLM
evaluation, call ``patch_sla_and_ratings()`` to:

1. Compute FRT and TTR from Zendesk's authoritative Ticket_Metrics fields.
2. Override the LLM's sla_status with the authoritative values.
3. Override METRIC_8 (FRT) and METRIC_10 (TTR) ratings so they are always
   consistent with the authoritative SLA data.

Fixes applied vs old codebase:
- Rating 0 is clamped to 1 (Rating 0 does not exist in the rubric).
- METRIC_8/10 ratings are always derived from authoritative data; LLM cannot
  contradict the Zendesk-measured durations.
- If LLM assigned a lower rating (e.g. 1 for escalation), we keep the worse of
  the two — so a genuine escalation-level breach is not upgraded to 2.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional, Union

import pytz

from src.config import AppConfig, EvaluationConfig
from src.models.evaluation import EvaluationResult, MetricResult, SLAEntry, SLAStatus
from src.models.ticket import TicketMetric, ZendeskTicket

logger = logging.getLogger(__name__)

_RATING_LABELS: dict[Union[int, str], str] = {
    4: "Excellent",
    3: "Good",
    2: "Needs Improvement",
    1: "Poor",
    "N/A": "Not Applicable",
}


def _frt_rating(
    frt_seconds: Optional[float],
    threshold_seconds: float,
    breach_minor_multiplier: float,
) -> Union[int, str]:
    """Compute authoritative METRIC_8 rating from FRT seconds."""
    if frt_seconds is None:
        return "N/A"
    if frt_seconds <= threshold_seconds:
        return 4
    if frt_seconds <= threshold_seconds * breach_minor_multiplier:
        return 3
    return 2


def _ttr_rating(
    ttr_minutes: Optional[float],
    threshold_minutes: float,
    breach_minor_multiplier: float,
) -> Union[int, str]:
    """Compute authoritative METRIC_10 rating from TTR minutes."""
    if ttr_minutes is None:
        return "N/A"
    if ttr_minutes <= threshold_minutes:
        return 4
    if ttr_minutes <= threshold_minutes * breach_minor_multiplier:
        return 3
    return 2


def _conservative_rating(
    authoritative: Union[int, str], llm_rating: Union[int, str]
) -> Union[int, str]:
    """Return the more conservative (lower/worse) of two ratings.

    If authoritative says MET (4), always trust that over the LLM.
    For breach ratings, take the min so we don't upgrade a justified Rating 1.
    """
    if authoritative == "N/A":
        return "N/A"
    if authoritative == 4:
        # Authoritative data says SLA was MET — always use 4
        return 4
    # For breach: take min(authoritative, llm) so Rating 1 escalation is preserved
    if isinstance(llm_rating, int) and llm_rating > 0:
        return min(int(authoritative), llm_rating)  # type: ignore[arg-type]
    return authoritative


def _patch_metric(
    result: EvaluationResult,
    metric_id: str,
    new_rating: Union[int, str],
) -> None:
    """Update a metric's rating and label in-place, using conservative logic."""
    for metric in result.metrics:
        if metric.metric_id == metric_id:
            old = metric.rating
            # Clamp 0 → 1 on old rating before comparison
            if isinstance(old, int) and old == 0:
                old = 1

            final = _conservative_rating(new_rating, old)

            # Clamp: final must be 1–4 or N/A, never 0
            if isinstance(final, int) and final == 0:
                final = 1

            if final != old:
                logger.debug(
                    "Patching %s rating: %s → %s (authoritative SLA override)",
                    metric_id, old, final,
                )
            metric.rating = final
            metric.rating_label = _RATING_LABELS.get(final, str(final))
            return

    # Metric not found — this shouldn't happen but log it
    logger.warning("Metric %s not found in evaluation result for ticket %s", metric_id, result.ticket_id)


def _apply_weekend_exclusion(
    calendar_minutes: float,
    assigned_at: str,
    solved_at: str,
    tz_name: str,
) -> float:
    """Subtract the 48-hour weekend window (Sat 03:00–Mon 03:00 in tz_name) from
    calendar_minutes for each complete weekend window that falls within
    [assigned_at, solved_at].  Returns adjusted business minutes (≥ 0).

    A weekend window is only subtracted if it falls *entirely* within the ticket
    window (assigned < Sat 03:00 AND Mon 03:00 ≤ solved).
    """
    tz = pytz.timezone(tz_name)

    def _parse(ts: str) -> datetime:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(tz)

    assigned = _parse(assigned_at)
    solved = _parse(solved_at)

    if solved <= assigned:
        return calendar_minutes

    # Walk backwards from solved to find the most-recent past Saturday
    days_since_sat = (assigned.weekday() - 5) % 7   # Mon=0 … Sat=5
    last_sat = assigned - timedelta(days=days_since_sat)
    sat_3am = last_sat.replace(hour=3, minute=0, second=0, microsecond=0)

    # Advance to the first Saturday 03:00 that is *strictly after* assigned
    while sat_3am <= assigned:
        sat_3am += timedelta(weeks=1)

    deduction = 0
    while sat_3am < solved:
        mon_3am = sat_3am + timedelta(hours=48)  # Sat 03:00 + 48h = Mon 03:00
        if mon_3am <= solved:
            deduction += 2880
        sat_3am += timedelta(weeks=1)

    return max(0.0, calendar_minutes - deduction)


def _extract_severity(ticket: ZendeskTicket, field_id: Optional[str]) -> Optional[str]:
    """Return the severity/priority key for tier resolution.

    If field_id is set, reads the matching Zendesk custom field value.
    Falls back to ticket.priority (normal/low/high/urgent).
    """
    if field_id:
        for cf in ticket.custom_fields:
            if str(cf.id) == field_id:
                return str(cf.value) if cf.value is not None else None
    return ticket.priority


def patch_sla_and_ratings(
    result: EvaluationResult,
    ticket_metric: TicketMetric,
    ticket: ZendeskTicket,
    eval_config: EvaluationConfig,
) -> EvaluationResult:
    """Compute authoritative SLA, update sla_status, patch METRIC_8 and METRIC_10.

    This MUST be called on every evaluation result before saving.
    """
    channel = ticket.channel()
    mult = eval_config.breach_minor_multiplier

    # --- Resolve SLA tier for this ticket's severity/priority ---
    severity = _extract_severity(ticket, eval_config.sla.severity_field_id)
    if channel == "chat":
        tier = eval_config.sla.chat.resolve(severity)
    else:
        tier = eval_config.sla.email.resolve(severity)

    # --- FRT ---
    frt_sec = None
    frt_threshold_min: float
    if ticket_metric.reply_time_in_seconds:
        frt_sec = ticket_metric.reply_time_in_seconds.calendar

    if channel == "chat":
        frt_threshold_sec = float(tier.frt_seconds or 30)
        frt_threshold_min = frt_threshold_sec / 60
    else:
        frt_threshold_sec = float(tier.frt_minutes or 30) * 60
        frt_threshold_min = float(tier.frt_minutes or 30)

    frt_min = (frt_sec / 60) if frt_sec is not None else None

    if frt_sec is None:
        frt_status = "NOT_APPLICABLE"
    elif frt_sec <= frt_threshold_sec:
        frt_status = "MET"
    else:
        frt_status = "BREACHED"

    frt_entry = SLAEntry(
        value_minutes=round(frt_min, 4) if frt_min is not None else None,
        threshold_minutes=frt_threshold_min,
        status=frt_status,
    )

    # --- TTR ---
    ttr_min: Optional[float] = None
    if ticket_metric.full_resolution_time_in_minutes:
        ttr_min = ticket_metric.full_resolution_time_in_minutes.calendar

    ttr_threshold = float(tier.ttr_minutes)

    # Apply weekend exclusion for email TTR
    if channel != "chat" and tier.weekend_exclusion and ttr_min is not None:
        assigned_ts = ticket_metric.assigned_at
        solved_ts = ticket_metric.solved_at
        if assigned_ts and solved_ts:
            ttr_min = _apply_weekend_exclusion(ttr_min, assigned_ts, solved_ts, tier.timezone)
            logger.debug(
                "Weekend exclusion applied for ticket %s: TTR adjusted to %.1f min",
                result.ticket_id, ttr_min,
            )
        else:
            logger.warning(
                "Weekend exclusion skipped for ticket %s — assigned_at or solved_at is None",
                result.ticket_id,
            )

    if ttr_min is None:
        ttr_status = "NOT_APPLICABLE"
    elif ttr_min <= ttr_threshold:
        ttr_status = "MET"
    else:
        ttr_status = "BREACHED"

    ttr_entry = SLAEntry(
        value_minutes=round(ttr_min, 2) if ttr_min is not None else None,
        threshold_minutes=ttr_threshold,
        status=ttr_status,
    )

    # --- Update sla_status ---
    result.sla_status = SLAStatus(
        first_response_time=frt_entry,
        resolution_time=ttr_entry,
    )

    # --- Update flags ---
    flags = [f for f in result.flags if "SLA" not in f.upper()]
    if frt_status == "BREACHED":
        flags.append(f"SLA_FRT_BREACHED ({frt_min:.1f} min, threshold {frt_threshold_min} min)")
    if ttr_status == "BREACHED":
        flags.append(f"SLA_TTR_BREACHED ({ttr_min:.1f} min, threshold {ttr_threshold} min)")
    result.flags = flags

    # --- Patch METRIC_8 (FRT) ---
    auth_frt_rating = _frt_rating(frt_sec, frt_threshold_sec, mult)
    _patch_metric(result, "METRIC_8", auth_frt_rating)

    # --- Patch METRIC_10 (TTR) ---
    auth_ttr_rating = _ttr_rating(ttr_min, ttr_threshold, mult)
    _patch_metric(result, "METRIC_10", auth_ttr_rating)

    # --- Final clamp: ensure no metric has rating 0 ---
    for m in result.metrics:
        if isinstance(m.rating, int) and m.rating == 0:
            logger.warning(
                "Clamping rating 0 → 1 on %s for ticket %s", m.metric_id, result.ticket_id
            )
            m.rating = 1

    # --- Fill rating_label for all metrics (authoritative; LLM value discarded) ---
    for m in result.metrics:
        m.rating_label = _RATING_LABELS.get(m.rating, str(m.rating))

    # --- Recompute aggregate score ---
    numeric_ratings = [m.rating for m in result.metrics if isinstance(m.rating, int)]
    na_count = sum(1 for m in result.metrics if m.rating == "N/A")
    if numeric_ratings:
        avg = round(sum(numeric_ratings) / len(numeric_ratings), 2)
        band = _score_band(avg)
        from src.models.evaluation import AggregateScore
        result.aggregate_score = AggregateScore(
            numeric=avg,
            out_of=4.0,
            band=band,
            metrics_rated=len(numeric_ratings),
            metrics_na=na_count,
        )

    return result


def _score_band(score: float) -> str:
    if score >= 3.5:
        return "Excellent"
    if score >= 2.5:
        return "Good"
    if score >= 1.5:
        return "Needs Improvement"
    return "Poor"
