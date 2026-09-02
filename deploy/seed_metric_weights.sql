-- Metric registry + QC weighting — single source of truth for the feedback methodology.
-- 12 metrics carry weight (summing to 100); the remaining 7 are recorded but excluded
-- from the weighted score (they measure things outside written ticket handling, or are
-- SLA/derived). Mirrors skills/agent-feedback/fetch_qc_data.py (WEIGHTS) and METHODOLOGY.md.
--
-- Run once against the Snowflake schema (as the pipeline/admin role, not the reader):
--   snowsql -f deploy/seed_metric_weights.sql
-- Safe to re-run (CREATE OR REPLACE + full re-seed).

-- Target the QC schema explicitly (edit if your database/schema differ).
USE DATABASE SUPPORT_ANALYTICS;
USE SCHEMA SUPPORT_ANALYTICS.ZENDESK_AUDIT;

CREATE TABLE IF NOT EXISTS metric_weights (
    metric_id   VARCHAR PRIMARY KEY,
    metric_name VARCHAR,
    weight      FLOAT
);

DELETE FROM metric_weights;

INSERT INTO metric_weights (metric_id, metric_name, weight) VALUES
    ('METRIC_1',  'Clarifying Questions',            7.5),
    ('METRIC_2',  'Roadmap to Resolution',           0),
    ('METRIC_3',  'Correct SLA Expectations',        0),
    ('METRIC_4',  'Root Cause Analysis',             10),
    ('METRIC_5',  'Resolution Accuracy',             10),
    ('METRIC_6',  'Detailed Resolution Steps',       10),
    ('METRIC_7',  'All Concerns Addressed',          10),
    ('METRIC_8',  'Timely First Response',           0),
    ('METRIC_9',  'Proactive Updates',               5),
    ('METRIC_10', 'Resolution On Time',              0),
    ('METRIC_11', 'Clear Communication',             10),
    ('METRIC_12', 'Empathetic & Professional Tone',  10),
    ('METRIC_13', 'Resolution Status Set Correctly', 5),
    ('METRIC_14', 'Custom Attributes Filled',        0),
    ('METRIC_15', 'Workaround Provided',             7.5),
    ('METRIC_16', 'Escalation Judgment',             0),
    ('METRIC_17', 'KB / Docs Referenced',            5),
    ('METRIC_18', 'Internal Notes Quality',          10),
    ('METRIC_19', 'QC Reopen Reason',                0);

-- Sanity check: the weighted metrics must sum to 100.
SELECT SUM(weight) AS total_weight FROM metric_weights;  -- expect 100
