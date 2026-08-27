# Deployment — Daily scheduled run on a VM with Snowflake storage

The app runs unattended once a day on a Linux VM: it fetches only newly-closed tickets
(incremental), evaluates them, publishes QC scores to Zendesk, and records the structured data +
per-run log in **Snowflake**. Raw ticket/eval JSON blobs stay on the VM's persistent disk.

Local development is unchanged — it still uses SQLite (`storage.backend: sqlite`).

## 1. Snowflake prerequisites
Have ready (an admin creates these once): an `account` locator, a login `user` + `password`, a
`warehouse`, a `database`, a `schema`, and a `role` with `USAGE` on the warehouse/db and
`CREATE TABLE`/`CREATE SEQUENCE` + DML on the schema. The app **auto-creates** its tables and
sequences (`tickets`, `evaluations`, `metric_results`, `runs`, `pipeline_state`,
`evaluations_id_seq`, `runs_id_seq`) on first connect — no manual DDL needed.

## 2. VM setup
```bash
git clone <repo> ticket-evaluator && cd ticket-evaluator
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt          # includes snowflake-connector-python[pandas]

cp .env.example .env && chmod 600 .env              # fill ALL secrets incl. SNOWFLAKE_*
# In config/config.yaml set:   storage:\n  backend: snowflake
```
Ensure `data/` and `logs/` live on persistent disk (they're in the project dir — persistent on a VM).

## 3. One-time data migration (SQLite → Snowflake)
Copy the existing history + cursor so incremental continues seamlessly (otherwise the first run
re-fetches from `state.initial_fetch_from`):
```bash
.venv/bin/python scripts/migrate_sqlite_to_snowflake.py \
    --sqlite data/evaluations.db --state data/state.json
```
Verify the printed `SQLite -> Snowflake` row counts match, `*_id_seq` were reset, and
`pipeline_state` cursor = set. (Copy your current `data/evaluations.db` + `data/state.json` to the VM
first, or run the migration from the machine that has them, pointed at the VM's Snowflake.)

## 4. Smoke test
```bash
.venv/bin/python src/main.py status                 # reads counts from Snowflake
.venv/bin/python src/main.py run                    # one incremental cycle
```
In Snowflake confirm: new `evaluations`/`metric_results` rows, a `runs` row with
`window_from/window_to`, counts, `completed_at`, and `pipeline_state.zendesk_cursor` advanced.
Run `run` again immediately → it should fetch ~0 new (cursor honored).

## 5. Schedule daily
```bash
# Edit CRON_TIME in scripts/setup_cron.sh for the desired local time, then:
bash scripts/setup_cron.sh          # installs: cd PROJECT && .venv/bin/python src/main.py run >> logs/cron.log
crontab -l                          # verify
# remove with: bash scripts/setup_cron.sh --remove
```
Failures are logged to Snowflake `runs` (`errors>0`, `error_details`), to `logs/cron.log`, and via the
existing email/Slack notifier.

## 6. Querying in Snowflake
Everything is now queryable directly, e.g.:
```sql
-- last 10 runs (what ran, window, counts, failures)
SELECT started_at, completed_at, mode, window_from, window_to,
       fetched, evaluated, published, excluded, errors, error_details
FROM runs ORDER BY started_at DESC LIMIT 10;

-- monthly weighted inputs live in evaluations + metric_results (join on evaluation_id)
```

## Notes
- **Secrets:** `SNOWFLAKE_PASSWORD` sits in `.env` — `chmod 600 .env` and lock down the VM. Key-pair
  auth is an easy future hardening.
- **Recovery:** the purge command skips the local `.db` backup on Snowflake — use Snowflake Time
  Travel to recover.
- **Blobs:** `data/tickets` + `data/evaluations` grow ~tens of MB/month; add retention later if needed.
