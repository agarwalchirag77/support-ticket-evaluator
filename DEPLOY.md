# Deployment — Ubuntu 24.04 server, daily cron, Snowflake storage

The app runs unattended once a day on a Linux VM: it fetches only newly-closed tickets
(incremental), evaluates them, publishes QC scores to Zendesk, and records the structured data +
Zendesk cursor + per-run log in **Snowflake**. Raw ticket/eval JSON blobs stay on the VM's
persistent disk. Local development is unchanged (SQLite; `storage.backend: sqlite`).

**Assumptions:** SSH + sudo on an **Ubuntu 24.04** VM (ships Python 3.12); Snowflake provisioned
(account / user / password / warehouse / database / schema / role) with `CREATE TABLE|SEQUENCE` +
DML on the schema; outbound HTTPS (443) to `*.zendesk.com`, `api.openai.com`,
`*.snowflakecomputing.com`; and your local machine still has `data/evaluations.db`,
`data/state.json`, and a filled `.env`. The app **auto-creates** its Snowflake tables/sequences on
first connect — no manual DDL.

## 1. System packages
```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip git
python3 --version   # expect 3.12.x  (>=3.11 required)
```

## 2. Get the code
```bash
cd /opt
sudo git clone https://github.com/agarwalchirag77/support-ticket-evaluator.git
sudo chown -R "$USER":"$USER" support-ticket-evaluator
cd support-ticket-evaluator
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt      # includes snowflake-connector-python[pandas]
```

## 3. Secrets + backend + data to migrate
From your **local** machine:
```bash
scp .env                                 USER@VM:/opt/support-ticket-evaluator/.env
scp data/evaluations.db data/state.json  USER@VM:/opt/support-ticket-evaluator/data/
```
On the **VM**:
```bash
cd /opt/support-ticket-evaluator
chmod 600 .env                                  # secrets — lock it down
# ensure .env has the SNOWFLAKE_* vars (account,user,password,warehouse,database,schema,role)
sed -i 's/^\(\s*backend:\).*/\1 snowflake/' config/config.yaml
grep -A1 '^storage:' config/config.yaml         # verify -> backend: snowflake
```

## 4. Timezone (makes cron time intuitive; matches the SLA config TZ)
```bash
sudo timedatectl set-timezone Asia/Kolkata
timedatectl
```
(Or keep the VM on UTC and set `CRON_TIME` in UTC — 08:00 IST = 02:30 UTC.)

## 5. One-time migration (history + cursor → Snowflake)
```bash
.venv/bin/python scripts/migrate_sqlite_to_snowflake.py \
    --sqlite data/evaluations.db --state data/state.json
```
Confirm the printed `SQLite -> Snowflake` counts match, `*_id_seq` reset, `pipeline_state cursor = set`.

## 6. Log rotation (optional but recommended)
```bash
sudo cp deploy/logrotate-ticket-evaluator /etc/logrotate.d/ticket-evaluator
# edit the path inside if the project isn't at /opt/support-ticket-evaluator
sudo logrotate --debug /etc/logrotate.d/ticket-evaluator   # dry-run check
```

## 7. Schedule the daily run
```bash
# edit CRON_TIME in scripts/setup_cron.sh (default 0 8 * * * = 08:00 server-local), then:
bash scripts/setup_cron.sh
crontab -l    # entry should use .venv/bin/python (auto-detected)
```

---

## Testing on the remote (run in this order)

1. **Snowflake connectivity** (read-only; auto-creates tables):
   ```bash
   .venv/bin/python src/main.py status
   ```
   Expect the migrated counts (~7,292 tickets / ~6,544 evals) — proves creds/DB/schema.

2. **Publish safety check** (no Zendesk writes):
   ```bash
   .venv/bin/python src/main.py publish --unpublished --dry-run
   ```

3. **Safe first evaluate — no Zendesk publish yet.** Temporarily set
   `zendesk_write_back.enabled: false` in `config.yaml`, then:
   ```bash
   .venv/bin/python src/main.py run
   .venv/bin/python -c "from src.config import load_config; from src.storage.factory import make_database; import json; print(json.dumps(make_database(load_config()).get_summary_stats()['recent_runs'][0], indent=2, default=str))"
   ```
   Confirm the newest `runs` row has `window_from/window_to`, `fetched/evaluated`, `completed_at`,
   `errors=0`, and the cursor advanced. **Re-enable `zendesk_write_back.enabled: true`.**

4. **Full live run** (publishes to Zendesk):
   ```bash
   .venv/bin/python src/main.py run
   ```
   Spot-check a ticket's QC fields in Zendesk; confirm a `runs` row with `published>0`.

5. **Incremental proof:** run `run` again immediately → `fetched` ≈ 0.

6. **Failure logging:** run once with a bad `ZENDESK_API_TOKEN` → `runs` row `errors>0` +
   `error_details`; non-zero exit (in `logs/cron.log`); failure notification fires. Restore the token.

7. **Cron fires:** set `CRON_TIME` ~2 min ahead, `bash scripts/setup_cron.sh`, wait, check
   `tail logs/cron.log` + a fresh Snowflake `runs` row. Reset `CRON_TIME` to the real slot and re-run.

## Querying in Snowflake
```sql
-- run history: when, window covered, counts, failures
SELECT started_at, completed_at, mode, window_from, window_to,
       fetched, evaluated, published, excluded, errors, error_details
FROM runs ORDER BY started_at DESC LIMIT 10;
```

## Notes
- **Secrets:** `.env` holds `SNOWFLAKE_PASSWORD` + API keys → `chmod 600` and restrict VM access.
  Key-pair auth is an easy future hardening.
- **Recovery:** the purge command skips the local `.db` backup on Snowflake — use Time Travel.
- **Missed runs:** plain cron won't re-run if the VM was down at the scheduled minute; the next day's
  incremental covers the gap via the cursor. (Switch to a systemd timer with `Persistent=true` if that
  matters.)
- **Disk:** `data/` (~700 MB) + `logs/` on persistent disk; grows ~tens of MB/month.
- **Never commit** `.env` or `data/` (already gitignored).
