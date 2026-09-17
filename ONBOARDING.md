# Ticket Evaluator — Maintainer Handover

Everything the next person needs to **develop locally**, **deploy to the remote server**, and **keep it
running**. This is the map; the deep detail lives in [README.md](README.md) (app + CLI + config) and
[DEPLOY.md](DEPLOY.md) (server). Read this first, then follow the links.

---

## 1. What this is (60 seconds)

An AI QC system for Hevo's Zendesk support. Once a day it: **fetches** newly-closed tickets from the
configured Zendesk groups (incremental cursor API) → **evaluates** each across 19 quality metrics with an
LLM (OpenAI/Claude) → **publishes** scores back to Zendesk custom fields, and records structured results
+ the cursor + a per-run log.

- **Pipeline:** `Fetch → Evaluate → Publish`, orchestrated in [src/pipeline/orchestrator.py](src/pipeline/orchestrator.py).
- **Storage is pluggable** ([src/storage/factory.py](src/storage/factory.py)): **SQLite** locally
  (`data/evaluations.db`), **Snowflake** on the server — chosen by `storage.backend` in
  [config/config.yaml](config/config.yaml).
- **Two consumers of the data:** (a) the daily pipeline itself; (b) the **agent-feedback skill**
  ([skills/agent-feedback/](skills/agent-feedback/)) that teammates run from Claude to generate monthly
  per-agent QC feedback (read-only).

---

## 2. Repo map

| Path | What it is |
|------|-----------|
| [src/main.py](src/main.py) | CLI entry point — `run`, `re-evaluate`, `publish`, `purge-excluded`, `status`, `audit`, `export`. |
| [src/pipeline/](src/pipeline/) | `fetcher` (Zendesk incremental), `evaluator` (LLM + skip logic), `publisher` (Zendesk write-back + CSV export), `orchestrator`, `purger`. |
| [src/storage/](src/storage/) | `database.py` (SQLite), `snowflake_database.py` (Snowflake), `factory.py` (`make_database`), `state.py` (incremental cursor), `file_store.py` (ticket/eval JSON blobs). |
| [src/utils/sla.py](src/utils/sla.py) | Authoritative FRT/TTR computation + rating patch (channel- and severity-aware). |
| [src/config.py](src/config.py) | Loads `config.yaml`, expands `${ENV}` from `.env`. |
| [config/config.yaml](config/config.yaml) | All runtime config (Zendesk groups, exclusions, LLM, SLA, write-back fields). |
| [config/prompts/evaluation_v1.md](config/prompts/evaluation_v1.md) | The LLM evaluation prompt (the 19 metric definitions + SLA rules). |
| [scripts/](scripts/) | One-off/ops scripts (migration, backfills, cron, SLA re-patch, skill bundling) — see §6. |
| [deploy/](deploy/) | Server assets: `install_systemd_timer.sh`, `snowflake_reader.sql`, `seed_metric_weights.sql`, `snowflake_views.sql`, `logrotate-*`. |
| [skills/agent-feedback/](skills/agent-feedback/) | Standalone read-only skill for monthly agent feedback ([its SETUP.md](skills/agent-feedback/SETUP.md)). |
| `data/` (gitignored) | Ticket + eval JSON blobs, the SQLite DB, `state.json`. ~700 MB. **Never commit.** |
| `.env` (gitignored) | Secrets. **Never commit.** Template: [.env.example](.env.example). |

---

## 3. Local development setup

Local runs default to **SQLite** and touch neither Snowflake nor (with write-back off) Zendesk.

```bash
git clone https://github.com/agarwalchirag77/support-ticket-evaluator.git
cd support-ticket-evaluator
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env          # fill in ZENDESK_*, one LLM key; SNOWFLAKE_* only if using Snowflake
```

- Keep `storage.backend: sqlite` in `config.yaml` for local work.
- To experiment without writing to Zendesk, set `zendesk_write_back.enabled: false`.
- Sanity check: `.venv/bin/python src/main.py status`.
- Full CLI + config reference: **[README.md](README.md)**.

> **macOS Apple Silicon gotcha:** if you hit a `pydantic_core` architecture error, the interpreter is
> running under Rosetta/x86_64. Prefix commands with `arch -arm64` (e.g. `arch -arm64 python3 …`) or use a
> native-arm venv.

---

## 4. How the scoring works (the mental model that avoids mistakes)

- **The LLM's SLA ratings are overwritten.** [sla.py](src/utils/sla.py) `patch_sla_and_ratings` runs after
  every evaluation and **authoritatively** sets METRIC_8 (FRT), METRIC_10 (TTR), METRIC_19 (reopen), the
  FRT/TTR statuses, `SLA_*_BREACHED` flags, and the `aggregate_score`/band — from **config**, not the LLM.
  So SLA behavior changes are **config changes**, not prompt changes.
- **Scoring keys on channel, not group.** chat vs email selects the SLA tier and some metric applicability.
  `email` (L2) SLA is **severity-based** (see §7).
- **The feedback skill's weighted score uses 12 weighted metrics (→100).** METRIC_8/10 are **weight-0** —
  they surface as SLA breach flags but don't move the weighted QC score. Weights live in one place per
  consumer: `WEIGHTS` in [fetch_qc_data.py](skills/agent-feedback/fetch_qc_data.py) and
  [deploy/seed_metric_weights.sql](deploy/seed_metric_weights.sql) (keep them in sync).
- **`prompt_version` gates re-evaluation.** `skip_if_evaluated` skips tickets already scored under the
  current `evaluation.prompt_version`. **Bumping the version re-evaluates everything on the next run**
  (LLM cost). Only bump when you *want* a full re-score; otherwise edit prompt text without bumping.

### Making common changes
| Change | Where | Re-eval needed? |
|--------|-------|-----------------|
| SLA thresholds (FRT/TTR, severity) | `config.yaml → evaluation.sla` | No — use `scripts/repatch_sla.py` for history |
| QC exclusions (spam/dupe/etc.) | `config.yaml → zendesk.exclusions` | No; purge past ones with `purge-excluded` |
| Metric wording / rubric | `config/prompts/evaluation_v1.md` | Bump `prompt_version` if you want history re-scored |
| Metric weights (feedback) | `WEIGHTS` + `seed_metric_weights.sql` | No |
| Add/relabel a Zendesk group | `config.yaml → zendesk.groups` + `GROUP_LABELS` in the skill | No |

---

## 5. Deployment

### 5a. Deploying a change (the everyday flow)

The remote box (`ubuntu@…:~/QC_agent/support-ticket-evaluator`) is a normal clone. Deploying a change =
push to `main`, then pull on the VM. **Nothing to restart** for ordinary code/config changes — the
pipeline is a scheduled batch (each run launches a fresh `python src/main.py run`), so the next run picks
up new code automatically.

1. **Local:** make the change, test (`arch -arm64 python3 src/main.py status`, run the skill selftest if
   relevant), then commit and push:
   ```bash
   git add -A && git commit -m "…"
   git push origin main
   ```
2. **On the VM:** pull + refresh + verify with the helper:
   ```bash
   cd ~/QC_agent/support-ticket-evaluator
   bash deploy/update.sh --run     # git pull --autostash, pip install if reqs changed, run now, show status
   ```
   > First time only (before `update.sh` exists on the box): `git pull --autostash origin main` by hand,
   > then use `deploy/update.sh` for every future deploy.
3. **Verify:** `deploy/update.sh` prints `status` at the end; also watch the run with
   `journalctl -u ticket-evaluator.service -f`, confirm a fresh `runs` row in Snowflake, and spot-check a
   ticket's QC fields in Zendesk.

**Change-specific follow-ups** (run on the VM after the pull):

| You changed… | Extra step |
|--------------|-----------|
| SLA thresholds / severity (`config.yaml`) | `python scripts/repatch_sla.py --from YYYY-MM-DD --execute` to fix history (no LLM) |
| Prompt **with** a `prompt_version` bump | next `run` re-evaluates everything (LLM cost) — expected |
| Prompt **without** a version bump | only new tickets get the new prompt; no backfill |
| `requirements.txt` | `update.sh` installs it automatically |
| `zendesk.exclusions` | applies on next run; purge already-scored ones with `python src/main.py purge-excluded --execute` |
| The **feedback skill** (`skills/agent-feedback/`) | `bash scripts/make-skill-bundle.sh` and reshare to teammates |
| `deploy/install_systemd_timer.sh` (unit definition) | `bash deploy/install_systemd_timer.sh 08:00` (re-applies + reloads) |

Roll back a bad deploy: `git reset --hard <good-sha> && bash deploy/update.sh` (data is untouched — it
lives in Snowflake + `data/`).

### 5b. First-time server provisioning (once)

Full runbook: **[DEPLOY.md](DEPLOY.md)**. Summary:

1. Ubuntu VM, Python 3.11+, clone the repo, `python -m venv .venv && pip install -r requirements.txt`.
2. `scp` your `.env` + `data/evaluations.db` + `data/state.json`; `chmod 600 .env`. **Backfill the DB
   narrative first** (`scripts/backfill_eval_text.py`) so it travels inside the `.db`.
3. Set `storage.backend: snowflake`; ensure `SNOWFLAKE_*` in `.env`
   (DB `SUPPORT_ANALYTICS`, schema `ZENDESK_QC`).
4. One-time migration: `scripts/migrate_sqlite_to_snowflake.py` (loads history + seeds the cursor).
5. **Schedule with a systemd timer** (reliable; survives reboots, catches up missed runs):
   ```bash
   bash deploy/install_systemd_timer.sh 08:00      # server-local time
   systemctl list-timers ticket-evaluator.timer
   ```
   (Cron via `scripts/setup_cron.sh` is the fallback but proved unreliable here — see §8.)
6. For the read-only **feedback skill**, run the SQL in `deploy/` (`snowflake_reader.sql`,
   `seed_metric_weights.sql`, `snowflake_views.sql`) and hand teammates
   [skills/agent-feedback/SETUP.md](skills/agent-feedback/SETUP.md).

---

## 6. Day-2 operations & scripts

Run everything on the server via the venv (`.venv/bin/python …`), from the project dir.

| Task | Command |
|------|---------|
| Health / last run / cursor | `python src/main.py status` |
| Watch a scheduled run | `journalctl -u ticket-evaluator.service -f` |
| Find gaps (unevaluated/unpublished) | `python src/main.py audit --from YYYY-MM-DD` |
| Re-push failed Zendesk writes | `python src/main.py publish --unpublished` |
| Re-score a window / tickets | `python src/main.py re-evaluate --from … --to …` (LLM cost) |
| Apply an SLA config change to history (no LLM) | `python scripts/repatch_sla.py --from … [--execute]` |
| Remove QC-excluded tickets | `python src/main.py purge-excluded [--execute]` |
| Migrate SQLite → Snowflake (one-time) | `python scripts/migrate_sqlite_to_snowflake.py` |
| Backfill eval narrative columns | `python scripts/backfill_eval_text.py` |
| Package the feedback skill to share | `bash scripts/make-skill-bundle.sh` |

**Where the incremental cursor lives:** `data/state.json` (SQLite) or the `pipeline_state` table
(Snowflake). `status` shows whether it's set + the resume point. If it's empty, the next run tries a full
re-fetch from `state.initial_fetch_from` — which the fetcher now **refuses** when the DB already has data
(override only with `ALLOW_FULL_REFETCH=1`). If you ever need to re-seed it, copy the `zendesk_cursor`
from a good `state.json` into `pipeline_state`.

---

## 7. Key facts (current state)

- **Groups:** the former **Chat L1 Support** group `44897999201817` was **converted to email-only L2**;
  both it and **General Escalation** `6338786491161` are now one **L2** cohort. The feedback skill's
  `--group` flag is a retired no-op.
- **L2 (email) SLA is severity-based** — Zendesk custom field **`8415300134041`** (values `sev_0`..`sev_4`):
  FR 30 min (sev_0/1) or 60 min (sev_2–4); TTR 12 / 24 / 48 / 72 / 96 h for sev_0→sev_4; weekend excluded
  only for **sev_3/sev_4**. Config: `evaluation.sla` in `config.yaml`.
- **Snowflake:** database `SUPPORT_ANALYTICS`, schema `ZENDESK_QC`, warehouse `COMPUTE_WH`. Pipeline user
  writes; a separate SELECT-only reader user serves the skill.
- **Current `prompt_version`:** `v2`.
- **Chat (L1) history** predates the conversion; those tickets now report under L2 (accepted trade-off).

---

## 8. Gotchas & lessons learned (read before touching prod)

- **Cron was unreliable** on the VM (vanished / never fired). Use the **systemd timer**
  (`deploy/install_systemd_timer.sh`). If the *instance itself* is ever replaced (spot/ASG), the timer is
  lost with it — bake scheduling into user-data/AMI in that case.
- **Empty cursor = mass re-fetch.** Always confirm `status` shows the cursor set before/after deploys. The
  guard prevents accidental full re-fetches; don't set `ALLOW_FULL_REFETCH=1` unless you truly mean it.
- **`skip_if_evaluated` needs the eval blobs OR the DB record** — already handled so the blob-less
  Snowflake VM skips correctly (it won't re-run the LLM on already-scored tickets). Keep that behavior if
  you touch [evaluator.py](src/pipeline/evaluator.py).
- **SLA changes don't need a re-eval** — change `config.yaml` and run `scripts/repatch_sla.py` (no LLM).
- **Secrets:** never commit `.env` or `data/` (both gitignored). `chmod 600 .env` on the server. The skill
  reader user is SELECT-only.
- **Failure alerts:** email-to-Slack notifier; if creds are stale you get silent failures. Verify it after
  any secret rotation (a bad `NOTIFICATION_EMAIL_PASSWORD` throws `BadCredentials`).
- **Verify after deploy:** `status` (counts + cursor), one manual `run`, a fresh `runs` row in Snowflake,
  and spot-check a ticket's QC fields in Zendesk.

---

## 9. Handy references
- App + CLI + config + troubleshooting: [README.md](README.md)
- Server runbook: [DEPLOY.md](DEPLOY.md)
- Feedback skill (methodology + consumer setup): [skills/agent-feedback/METHODOLOGY.md](skills/agent-feedback/METHODOLOGY.md), [skills/agent-feedback/SETUP.md](skills/agent-feedback/SETUP.md)
- Repo: `https://github.com/agarwalchirag77/support-ticket-evaluator`
