-- Convenience views that encode the feedback methodology in SQL, so anyone with the
-- read-only role can query agent QC data directly (the Cowork skill uses the Python
-- fetch script, but these mirror its logic for ad-hoc analysis).
--
-- Depends on metric_weights (run deploy/seed_metric_weights.sql first).
-- Run once as the pipeline/admin role:  snowsql -f deploy/snowflake_views.sql
-- Safe to re-run (CREATE OR REPLACE).
--
-- Scoring rules (match fetch_qc_data.py / METHODOLOGY.md):
--   * rating is VARCHAR; TRY_CAST(rating AS FLOAT) yields NULL for 'N/A' → auto-excluded.
--   * per-ticket weighted = SUM(weight*rating)/SUM(weight) over non-N/A weighted metrics.
--   * agent-month weighted = AVG of the per-ticket weighted scores (each ticket equal).
--   * "low" rating = 2 or below.

-- Target the QC schema explicitly (edit if your database/schema differ).
USE DATABASE SUPPORT_ANALYTICS;
USE SCHEMA SUPPORT_ANALYTICS.ZENDESK_AUDIT;

-- 1. Latest evaluations, one row per ticket, with agent + close-month + group label.
CREATE OR REPLACE VIEW v_eval_latest AS
SELECT
    e.id                                   AS evaluation_id,
    t.ticket_id,
    COALESCE(e.agent_name, t.agent_name)   AS agent_name,
    SUBSTR(t.closed_at, 1, 7)              AS close_month,
    t.group_id,
    CASE t.group_id
        WHEN 44897999201817 THEN 'L1'
        WHEN 6338786491161  THEN 'L2'
        ELSE 'other'
    END                                    AS group_label,
    t.channel,
    e.aggregate_score,
    e.performance_band,
    e.evaluator_confidence,
    e.ticket_summary,
    e.flags,
    t.closed_at
FROM evaluations e
JOIN tickets t ON t.ticket_id = e.ticket_id
WHERE e.is_latest = 1;

-- 2. Per agent x close-month x metric: average (N/A excluded), rated count, low count.
CREATE OR REPLACE VIEW v_agent_month_metric AS
SELECT
    v.agent_name,
    v.close_month,
    v.group_label,
    m.metric_id,
    ANY_VALUE(m.metric_name)                                  AS metric_name,
    ANY_VALUE(w.weight)                                       AS weight,
    AVG(TRY_CAST(m.rating AS FLOAT))                          AS avg_rating,
    COUNT(TRY_CAST(m.rating AS FLOAT))                        AS rated,
    COUNT(*) - COUNT(TRY_CAST(m.rating AS FLOAT))             AS na,
    COUNT_IF(TRY_CAST(m.rating AS FLOAT) <= 2)               AS low
FROM v_eval_latest v
JOIN metric_results m ON m.evaluation_id = v.evaluation_id
LEFT JOIN metric_weights w ON w.metric_id = m.metric_id
GROUP BY v.agent_name, v.close_month, v.group_label, m.metric_id;

-- 3a. Per-ticket weighted score (over non-N/A weighted metrics).
CREATE OR REPLACE VIEW v_ticket_weighted AS
SELECT
    v.evaluation_id,
    v.ticket_id,
    v.agent_name,
    v.close_month,
    v.group_label,
    v.performance_band,
    SUM(w.weight * TRY_CAST(m.rating AS FLOAT))
        / NULLIF(SUM(CASE WHEN TRY_CAST(m.rating AS FLOAT) IS NOT NULL THEN w.weight END), 0)
                                                             AS weighted_score
FROM v_eval_latest v
JOIN metric_results m ON m.evaluation_id = v.evaluation_id
JOIN metric_weights w ON w.metric_id = m.metric_id AND w.weight > 0
GROUP BY v.evaluation_id, v.ticket_id, v.agent_name, v.close_month, v.group_label, v.performance_band;

-- 3b. Per agent x month weighted score = average of per-ticket weighted scores.
CREATE OR REPLACE VIEW v_agent_month_weighted AS
SELECT
    agent_name,
    close_month,
    group_label,
    COUNT(*)                       AS n_tickets,
    ROUND(AVG(weighted_score), 3)  AS weighted_score
FROM v_ticket_weighted
GROUP BY agent_name, close_month, group_label;

-- 4. Low-scored tickets with the narrative needed to write feedback: ticket_summary +
--    each low weighted metric's name/rating/reasoning/improvement_note.
CREATE OR REPLACE VIEW v_low_tickets AS
SELECT
    v.agent_name,
    v.close_month,
    v.group_label,
    v.ticket_id,
    v.performance_band,
    v.ticket_summary,
    v.flags,
    m.metric_id,
    m.metric_name,
    m.rating,
    m.reasoning,
    m.improvement_note
FROM v_eval_latest v
JOIN metric_results m ON m.evaluation_id = v.evaluation_id
JOIN metric_weights w ON w.metric_id = m.metric_id AND w.weight > 0
WHERE TRY_CAST(m.rating AS FLOAT) <= 2
   OR v.performance_band IN ('Poor', 'Needs Improvement');
