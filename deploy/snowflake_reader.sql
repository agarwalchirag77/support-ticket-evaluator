-- Read-only Snowflake access for the Cowork agent-feedback skill.
--
-- Creates a least-privilege role + user that can ONLY SELECT the QC data — it can
-- never write, publish, or alter anything. The Cowork skill authenticates as this
-- user (SNOWFLAKE_READER_USER / SNOWFLAKE_READER_PASSWORD) to fetch feedback evidence.
--
-- Run ONCE as ACCOUNTADMIN (or a role with the grants below), after the pipeline has
-- created its tables. Replace the placeholders first:
--   <READER_PASSWORD>  a strong password (store it in the Cowork env, not here)
--   SUPPORT_QC / PUBLIC / COMPUTE_WH  → your actual database / schema / warehouse
--
--   snowsql -f deploy/snowflake_reader.sql
-- Idempotent: re-running only re-applies grants.

USE ROLE ACCOUNTADMIN;

CREATE ROLE IF NOT EXISTS TICKET_EVALUATOR_READER;

-- Warehouse: USAGE + OPERATE so the reader can run queries (but not resize/suspend config).
GRANT USAGE ON WAREHOUSE COMPUTE_WH TO ROLE TICKET_EVALUATOR_READER;

-- Database + schema: traversal only.
GRANT USAGE ON DATABASE SUPPORT_QC TO ROLE TICKET_EVALUATOR_READER;
GRANT USAGE ON SCHEMA SUPPORT_QC.PUBLIC TO ROLE TICKET_EVALUATOR_READER;

-- SELECT on every current + future table and view in the schema.
GRANT SELECT ON ALL TABLES    IN SCHEMA SUPPORT_QC.PUBLIC TO ROLE TICKET_EVALUATOR_READER;
GRANT SELECT ON FUTURE TABLES IN SCHEMA SUPPORT_QC.PUBLIC TO ROLE TICKET_EVALUATOR_READER;
GRANT SELECT ON ALL VIEWS     IN SCHEMA SUPPORT_QC.PUBLIC TO ROLE TICKET_EVALUATOR_READER;
GRANT SELECT ON FUTURE VIEWS  IN SCHEMA SUPPORT_QC.PUBLIC TO ROLE TICKET_EVALUATOR_READER;

-- Dedicated user for the skill.
CREATE USER IF NOT EXISTS TICKET_EVALUATOR_READER_USER
    PASSWORD = '<READER_PASSWORD>'
    DEFAULT_ROLE = TICKET_EVALUATOR_READER
    DEFAULT_WAREHOUSE = COMPUTE_WH
    DEFAULT_NAMESPACE = SUPPORT_QC.PUBLIC
    MUST_CHANGE_PASSWORD = FALSE
    COMMENT = 'Read-only user for the Cowork agent-feedback skill';

GRANT ROLE TICKET_EVALUATOR_READER TO USER TICKET_EVALUATOR_READER_USER;

-- Verify (run as the reader): should list tables and succeed on SELECT, fail on INSERT.
--   USE ROLE TICKET_EVALUATOR_READER;
--   SELECT COUNT(*) FROM evaluations;
--   SELECT * FROM v_agent_month_weighted LIMIT 5;
