# Ticket Evaluation Tool

An AI-powered support quality evaluation system for Hevo Data's Zendesk tickets. Runs as a daily scheduled job: fetches closed tickets from configured Zendesk groups, evaluates each ticket across 18 quality metrics using Claude or OpenAI, and pushes scores back to Zendesk custom fields.

---

## Table of Contents

- [How It Works](#how-it-works)
- [Prerequisites](#prerequisites)
- [Setup](#setup)
  - [1. Clone and install dependencies](#1-clone-and-install-dependencies)
  - [2. Configure environment variables](#2-configure-environment-variables)
  - [3. Configure the tool](#3-configure-the-tool)
  - [4. Set up Zendesk custom fields](#4-set-up-zendesk-custom-fields)
  - [5. Set up Slack notifications](#5-set-up-slack-notifications)
  - [6. Verify configuration](#6-verify-configuration)
- [CLI Reference](#cli-reference)
  - [run](#run--full-pipeline)
  - [re-evaluate](#re-evaluate--historical-re-scoring)
  - [publish](#publish--push-to-zendesk)
- [Daily Scheduling](#daily-scheduling)
- [Configuration Reference](#configuration-reference)
- [Evaluation Metrics](#evaluation-metrics)
- [Output Files](#output-files)
- [Data Directory Layout](#data-directory-layout)
- [Switching LLM Provider](#switching-llm-provider)
- [Updating the Evaluation Prompt](#updating-the-evaluation-prompt)
- [Troubleshooting](#troubleshooting)
- [Architecture Overview](#architecture-overview)

---

## How It Works

```
Zendesk (incremental API)
        │
        ▼
┌─────────────────┐     tickets saved to
│   Stage 1       │ ──► data/tickets/YYYY-MM-DD/Ticket_<id>.json
│   Fetcher       │
└─────────────────┘
        │
        ▼
┌─────────────────┐     evaluations saved to
│   Stage 2       │ ──► data/evaluations/YYYY-MM-DD/eval_<id>_v1.json
│   Evaluator     │     + SQLite database
│  (Claude/OpenAI)│
└─────────────────┘
        │
        ▼
┌─────────────────┐     scores pushed to
│   Stage 3       │ ──► Zendesk custom fields
│   Publisher     │     + CSV exports
└─────────────────┘
        │
        ▼
  Slack notification
  (email-to-channel)
```

Each stage writes its output to disk before proceeding. This means any stage can be re-run independently without repeating earlier work.

---

## Prerequisites

- **Python 3.11+**
- **Zendesk account** with API access (email + API token)
- **API key** for Claude (`ANTHROPIC_API_KEY`) or OpenAI (`OPENAI_API_KEY`)
- **macOS** (for the built-in cron scheduling — Linux also works)

---

## Setup

### 1. Clone and install dependencies

```bash
cd "ticket-evaluator"
pip install -r requirements.txt
```

### 2. Configure environment variables

Copy the example file and fill in your credentials:

```bash
cp .env.example .env
```

Edit `.env`:

```env
# Zendesk
ZENDESK_EMAIL=you@hevodata.com
ZENDESK_API_TOKEN=your_api_token

# Zendesk group IDs (see "Finding Group IDs" below)
CHAT_L1_GROUP_ID=123456789
GENERAL_ESCALATION_GROUP_ID=987654321

# LLM — fill only the one you use
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...

# Slack notifications (email-to-channel)
NOTIFICATION_EMAIL=alerts@hevodata.com
NOTIFICATION_EMAIL_PASSWORD=your_gmail_app_password
SLACK_CHANNEL_EMAIL=abc123@hevodata.slack.com
```

> **Finding Zendesk Group IDs:**
> In Zendesk, go to **Admin → People → Groups**, click a group, and the ID is in the URL:
> `https://hevodata.zendesk.com/admin/people/teams/groups/**123456789**/edit`
> Alternatively call: `curl -u email/token:TOKEN https://hevodata.zendesk.com/api/v2/groups.json`

> **Gmail App Password:**
> If using Gmail, enable 2FA then go to Google Account → Security → App Passwords to generate a dedicated password for this tool. Do NOT use your main Google account password.

### 3. Configure the tool

Open `config/config.yaml` and review the settings. The most important ones:

```yaml
llm:
  provider: claude            # ← set to "claude" or "openai"

evaluation:
  prompt_version: "v1"        # ← bump when you update the prompt

state:
  initial_fetch_from: "2025-01-01T00:00:00Z"   # ← start date for first-ever run
```

Everything else has sensible defaults. See [Configuration Reference](#configuration-reference) for all options.

### 4. Set up Zendesk custom fields

The tool writes evaluation scores back to Zendesk custom fields. You need to create these fields in Zendesk first, then add their IDs to `config.yaml`.

**Fields to create** (Admin → Objects and rules → Tickets → Fields):

| Field name | Type | Purpose |
|---|---|---|
| AI Aggregate Score | Decimal | Overall score (e.g. `3.6`) |
| AI Performance Band | Text | `Excellent` / `Good` / `Needs Improvement` / `Poor` |
| AI Evaluation Date | Date | Date the evaluation was run |
| AI Evaluator Confidence | Text | `HIGH` / `MEDIUM` / `LOW` |
| AI Prompt Version | Text | Which prompt version was used (e.g. `v1`) |
| AI FRT Status | Text | `MET` / `BREACHED` / `NOT_APPLICABLE` |
| AI TTR Status | Text | `MET` / `BREACHED` / `NOT_APPLICABLE` |
| AI LLM Provider | Text | Model used (e.g. `claude-opus-4-6`) |

After creating each field, find its numeric ID in the URL and add it to `config.yaml`:

```yaml
zendesk_write_back:
  custom_fields:
    aggregate_score: 12345678       # ← Zendesk field ID
    performance_band: 23456789
    evaluation_date: 34567890
    # ... etc.
```

Leave any field as `null` to skip writing it.

> **Optionally**, you can also create per-metric fields (`AI Metric 1 Rating` through `AI Metric 18 Rating`, type Integer) and add their IDs under `metric_fields` in the config.

### 5. Set up Slack notifications

The tool sends run summaries and error alerts to a Slack channel via email (no admin approval required).

1. Open the Slack channel you want alerts in
2. Click the channel name → **Settings** → **Integrations** → **Send emails to this channel**
3. Copy the generated email address (looks like `abc123xyz@hevodata.slack.com`)
4. Set `SLACK_CHANNEL_EMAIL=abc123xyz@hevodata.slack.com` in your `.env`

### 6. Verify configuration

Run a fetch-only test to confirm Zendesk credentials and group IDs are correct:

```bash
python src/main.py run --fetch-only
```

You should see ticket JSONs appear in `data/tickets/YYYY-MM-DD/`. If no tickets appear, check that tickets are actually closed in the configured groups within the `initial_fetch_from` date range.

---

## CLI Reference

All commands are run from the project root directory:

```bash
python src/main.py <command> [options]
```

Use `--config path/to/config.yaml` to specify an alternate config file (default: `config/config.yaml`).

---

### `run` — Full pipeline

Fetches new closed tickets, evaluates them, and pushes results to Zendesk.

```bash
python src/main.py run
```

**Options:**

| Flag | Description |
|---|---|
| `--fetch-only` | Only fetch and save tickets to disk. Skip evaluation and write-back. Useful for pre-loading tickets before evaluating. |
| `--force` | Ignore the "already evaluated" cache. Re-evaluates every ticket even if it was previously processed with the same prompt version. |

**Examples:**

```bash
# Standard daily run
python src/main.py run

# Fetch tickets today but don't evaluate yet
python src/main.py run --fetch-only

# Force re-run everything (e.g. after a bug fix)
python src/main.py run --force
```

**How offset tracking works:**
Each run saves a Zendesk cursor to `data/state.json`. The next run picks up exactly where the last left off — no duplicate processing. On the very first run, it fetches from `state.initial_fetch_from` in `config.yaml`.

---

### `re-evaluate` — Historical re-scoring

Re-runs the evaluation on tickets already fetched (no Zendesk API calls). Use this after:
- Updating the evaluation prompt (`config/prompts/`)
- Changing the `prompt_version` in config
- Fixing a scoring bug

```bash
python src/main.py re-evaluate --from DATE [--to DATE]
python src/main.py re-evaluate --tickets ID1,ID2,ID3
python src/main.py re-evaluate --all
```

**Options:**

| Flag | Description |
|---|---|
| `--from DATE` | Re-evaluate tickets fetched on or after this date (`YYYY-MM-DD`) |
| `--to DATE` | Upper date bound, used with `--from` |
| `--tickets IDS` | Comma-separated list of specific ticket IDs |
| `--all` | Re-evaluate every ticket currently on disk |
| `--force-fetch` | Re-fetch ticket data from Zendesk before re-evaluating |

**Examples:**

```bash
# Re-evaluate everything from Q1 after updating the prompt
python src/main.py re-evaluate --from 2025-01-01 --to 2025-03-31

# Re-evaluate specific tickets
python src/main.py re-evaluate --tickets 67207,67258,64557

# Re-score all tickets on disk with the new prompt
python src/main.py re-evaluate --all
```

**How history is preserved:**
Old evaluation records are marked `is_latest=0` in the SQLite database — they are not deleted. You can query score changes across prompt versions:

```sql
SELECT ticket_id, prompt_version, aggregate_score, evaluated_at
FROM evaluations
WHERE ticket_id = 67258
ORDER BY evaluated_at;
```

---

### `publish` — Push to Zendesk

Pushes evaluation results to Zendesk custom fields. This normally runs automatically as part of `run`, but can be triggered separately.

```bash
python src/main.py publish --unpublished
```

**Options:**

| Flag | Description |
|---|---|
| `--unpublished` | Re-push all evaluations that failed to write to Zendesk (e.g. due to a previous API error or missing field IDs) |

**Example:**

```bash
# After adding Zendesk field IDs to config.yaml for the first time
python src/main.py publish --unpublished
```

---

## Daily Scheduling

Install the cron job with the provided setup script:

```bash
bash scripts/setup_cron.sh
```

This schedules `python src/main.py run` to run every day at **8:00 AM**. Cron output is appended to `logs/cron.log`.

**Change the schedule:**
Edit `CRON_TIME` in `scripts/setup_cron.sh` before running it. Uses standard cron syntax:

```
CRON_TIME="0 8 * * *"    # 8:00 AM daily (default)
CRON_TIME="0 9 * * 1-5"  # 9:00 AM weekdays only
CRON_TIME="0 6,18 * * *" # Twice daily: 6 AM and 6 PM
```

**Verify the cron job is installed:**

```bash
crontab -l
```

**Remove the cron job:**

```bash
bash scripts/setup_cron.sh --remove
```

---

## Configuration Reference

All settings live in `config/config.yaml`. Environment variable references (`${VAR_NAME}`) are automatically expanded from `.env`.

### `zendesk`

| Key | Description | Default |
|---|---|---|
| `subdomain` | Your Zendesk subdomain (e.g. `hevodata`) | — |
| `email` | Agent email used for API auth | `${ZENDESK_EMAIL}` |
| `api_token` | Zendesk API token | `${ZENDESK_API_TOKEN}` |
| `groups` | List of `{id, name}` groups to fetch tickets from | — |
| `ticket_status` | Only fetch tickets with this status | `closed` |
| `rate_limit.regular_requests_per_minute` | Cap for comments/metrics/write API calls | `400` |
| `rate_limit.export_requests_per_minute` | Cap for incremental export (Zendesk limit: 10/min) | `8` |

### `llm`

| Key | Description | Default |
|---|---|---|
| `provider` | Active LLM: `claude` or `openai` | `claude` |
| `claude.model` | Claude model ID | `claude-opus-4-6` |
| `claude.max_tokens` | Max output tokens | `8000` |
| `claude.max_input_tokens` | Ticket JSON is truncated if larger | `90000` |
| `openai.model` | OpenAI model ID | `gpt-4o` |
| `rate_limit.requests_per_minute` | LLM call rate ceiling | `50` |

### `evaluation`

| Key | Description | Default |
|---|---|---|
| `prompt_file` | Path to the evaluation prompt | `config/prompts/evaluation_v1.md` |
| `prompt_version` | Version string recorded in every evaluation | `v1` |
| `skip_if_evaluated` | Skip tickets already in the DB with this prompt version | `true` |
| `sla.chat.frt_seconds` | Chat FRT threshold in seconds | `30` |
| `sla.chat.ttr_minutes` | Chat TTR threshold in minutes | `120` |
| `sla.email.frt_minutes` | Email FRT threshold in minutes | `30` |
| `sla.email.ttr_minutes` | Email TTR threshold in minutes | `2880` |
| `sla.email.weekend_exclusion` | Exclude Sat 03:00–Mon 03:00 IST from email TTR | `true` |
| `breach_minor_multiplier` | Breach within this multiplier of threshold = Rating 3 (minor) | `1.2` |

### `zendesk_write_back`

| Key | Description |
|---|---|
| `enabled` | Set to `false` to disable all Zendesk write-back | `true` |
| `custom_fields.*` | Zendesk field IDs for each evaluation output. Set to `null` to skip. | all `null` |
| `metric_fields.METRIC_N` | Optional per-metric rating field IDs | all `null` |

### `notifications`

| Key | Description | Default |
|---|---|---|
| `method` | Notification method: `email` or `slack_webhook` | `email` |
| `email.smtp_host` | SMTP server | `smtp.gmail.com` |
| `email.smtp_port` | SMTP port | `587` |
| `email.to_address` | Destination (Slack channel email address) | `${SLACK_CHANNEL_EMAIL}` |
| `on_completion` | Notify on successful run | `true` |
| `on_failure` | Notify on fatal pipeline failure | `true` |
| `on_partial_failure` | Notify when some tickets fail | `true` |

### `state`

| Key | Description | Default |
|---|---|---|
| `file` | Path to the run state JSON file | `data/state.json` |
| `initial_fetch_from` | ISO 8601 start timestamp used on the very first run | `2025-01-01T00:00:00Z` |

---

## Evaluation Metrics

Each ticket is scored across 18 metrics on a **1–4 scale** (or N/A):

| # | Metric | What it measures |
|---|---|---|
| 1 | Clarifying Questions | Did the agent ask targeted questions before solving? |
| 2 | Roadmap to Resolution | Was a clear action plan communicated upfront? |
| 3 | Correct SLA Expectations Set | Were realistic timelines communicated? |
| 4 | Root Cause Analysis | Was the WHY of the issue explained? |
| 5 | Resolution Accuracy | Was the solution correct and complete? |
| 6 | Detailed Resolution Steps | Were steps clear, numbered, and actionable? |
| 7 | All Concerns Addressed | Were all customer questions answered? |
| 8 | Timely First Response | Was FRT SLA met? (**overridden from Zendesk metrics**) |
| 9 | Proactive Updates | Did the agent update the customer without being chased? |
| 10 | Resolution Shared on Time | Was TTR SLA met? (**overridden from Zendesk metrics**) |
| 11 | Grammar & Clear Communication | Were responses professional and error-free? |
| 12 | Empathetic & Professional Tone | Was the tone warm and appropriate? |
| 13 | Resolution Status Set Correctly | Was the correct solved sub-status applied? |
| 14 | Custom Attributes Filled | Were all Zendesk custom fields completed accurately? |
| 15 | Workaround Provided | Was a temporary workaround offered when needed? |
| 16 | Escalation Judgment | Was the escalation decision correct? |
| 17 | KB / Documentation Referenced | Were help docs linked where applicable? |
| 18 | Internal Notes Quality | Are notes complete enough for a handoff? |

**Rating scale:**

| Rating | Label |
|---|---|
| 4 | Excellent |
| 3 | Good |
| 2 | Needs Improvement |
| 1 | Poor |
| N/A | Not Applicable |

**Aggregate score bands:**

| Score | Band |
|---|---|
| 3.5 – 4.0 | Excellent |
| 2.5 – 3.4 | Good |
| 1.5 – 2.4 | Needs Improvement |
| 1.0 – 1.4 | Poor |

> **Note on Metrics 8 and 10:** The LLM's SLA rating is always overridden by the tool using Zendesk's authoritative `reply_time_in_seconds` and `full_resolution_time_in_minutes` fields. This prevents the model from using wrong timestamps (e.g. `created_at` instead of `assigned_at`) and ensures the SLA status and metric rating are always consistent.

---

## Output Files

### Per-ticket evaluation JSON

`data/evaluations/YYYY-MM-DD/eval_<ticket_id>_v1.json`

Full structured evaluation with evidence, reasoning, and scores for all 18 metrics. The `v1` suffix corresponds to the prompt version.

### SQLite database

`data/evaluations.db`

Queryable history of all evaluations. Useful for reporting and historical comparisons.

**Useful queries:**

```sql
-- Latest scores for all tickets
SELECT ticket_id, aggregate_score, performance_band, evaluated_at
FROM evaluations WHERE is_latest = 1
ORDER BY evaluated_at DESC;

-- Score distribution
SELECT performance_band, COUNT(*) as count
FROM evaluations WHERE is_latest = 1
GROUP BY performance_band;

-- Tickets with SLA breaches
SELECT ticket_id, frt_status, ttr_status, aggregate_score
FROM evaluations WHERE is_latest = 1
  AND (frt_status = 'BREACHED' OR ttr_status = 'BREACHED');

-- Score history for a single ticket (across prompt versions)
SELECT ticket_id, prompt_version, aggregate_score, evaluated_at
FROM evaluations WHERE ticket_id = 67258
ORDER BY evaluated_at;

-- Average score per agent
SELECT t.agent_name, AVG(e.aggregate_score) as avg_score, COUNT(*) as tickets
FROM evaluations e JOIN tickets t ON e.ticket_id = t.ticket_id
WHERE e.is_latest = 1
GROUP BY t.agent_name ORDER BY avg_score DESC;
```

### CSV exports

Generated after each run in `data/exports/`:

| File | Format | Use case |
|---|---|---|
| `YYYY-MM-DD_evaluations_wide.csv` | 1 row per ticket, metrics as columns | Quick analysis, Excel/Sheets |
| `YYYY-MM-DD_evaluations_long.csv` | 1 row per metric per ticket | Pivot tables, BI tools |

---

## Data Directory Layout

```
data/
├── state.json                        # Run cursor — do not edit manually
├── evaluations.db                    # SQLite database
├── tickets/
│   ├── 2026-03-30/
│   │   ├── Ticket_67207.json         # Raw Zendesk ticket (metadata + metrics + comments)
│   │   └── Ticket_67258.json
│   └── 2026-03-31/
│       └── Ticket_67301.json
├── evaluations/
│   ├── 2026-03-30/
│   │   ├── eval_67207_v1.json        # Evaluation result with all 18 metric scores
│   │   └── eval_67258_v1.json
│   └── 2026-03-31/
│       └── eval_67301_v1.json
└── exports/
    ├── 2026-03-30_evaluations_wide.csv
    └── 2026-03-30_evaluations_long.csv
```

---

## Switching LLM Provider

Change one line in `config/config.yaml` — no code changes required:

```yaml
llm:
  provider: openai    # was: claude
```

Make sure the corresponding API key is set in `.env`. Both providers are configured independently so you can switch back and forth freely.

**Supported models:**

| Provider | Recommended model | Config key |
|---|---|---|
| Anthropic | `claude-opus-4-6` | `llm.claude.model` |
| OpenAI | `gpt-4o` | `llm.openai.model` |

---

## Updating the Evaluation Prompt

1. Create a new versioned prompt file:
   ```bash
   cp config/prompts/evaluation_v1.md config/prompts/evaluation_v2.md
   ```

2. Edit `evaluation_v2.md` with your changes (add/modify metrics, update rubrics, etc.)

3. Update `config.yaml` to point to the new version:
   ```yaml
   evaluation:
     prompt_file: config/prompts/evaluation_v2.md
     prompt_version: "v2"
   ```

4. Re-evaluate historical tickets with the new prompt:
   ```bash
   python src/main.py re-evaluate --all
   ```

Old evaluations (v1) remain in the database with `is_latest=0`. You can compare scores before and after the prompt change using the SQLite query shown in [Output Files](#output-files).

---

## Troubleshooting

### No tickets are being fetched

- Check `data/state.json` — if `zendesk_cursor` is set to a recent date, there may genuinely be no new closed tickets.
- Verify group IDs: `curl -u email/token:TOKEN https://hevodata.zendesk.com/api/v2/groups.json`
- Check `initial_fetch_from` in config — if it's set to today, there's nothing to fetch.
- Try `python src/main.py run --fetch-only` and watch the logs.

### Zendesk write-back is silently skipped

- Verify `zendesk_write_back.enabled: true` in config.
- Check that at least one field ID is set (not `null`) under `custom_fields`.
- If field IDs were added after tickets were already evaluated, run `python src/main.py publish --unpublished`.

### LLM returns invalid JSON

The tool automatically retries up to 3 times with a stricter "return only JSON" instruction. If it still fails, check `logs/evaluator.log` for the raw response. The ticket is skipped and marked as failed — it will be retried on the next run.

### Rate limit errors

- For Zendesk 429s: the tool reads the `Retry-After` header and waits automatically.
- For LLM 429s: exponential backoff is applied automatically.
- If rate limits are persistent, reduce `llm.rate_limit.requests_per_minute` in config.

### Evaluation scores changed after re-running with the same prompt

- Ensure `evaluation.prompt_version` matches what was used before — if the version string changed, the DB treats it as a new evaluation.
- LLMs are non-deterministic even at `temperature=0` — small variations are expected.
- Check `evaluator_confidence` in the eval JSON — `LOW` indicates the model found the ticket ambiguous.

### Cron job not running

```bash
crontab -l                            # verify cron entry exists
tail -50 logs/cron.log               # check for Python errors
/usr/bin/python3 --version           # ensure python3 is accessible
```

If the cron job runs but `src/main.py` can't find modules, the Python path may be wrong. Edit `scripts/setup_cron.sh` and set `PYTHON` to the absolute path from `which python3`.

---

## Architecture Overview

```
ticket-evaluator/
├── config/
│   ├── config.yaml               All settings — edit this, not the code
│   └── prompts/
│       └── evaluation_v1.md      Versioned evaluation prompt (18 metrics + rubrics)
├── src/
│   ├── main.py                   CLI entry point (argparse)
│   ├── config.py                 Config loader: YAML + ${ENV_VAR} expansion
│   ├── pipeline/
│   │   ├── orchestrator.py       Coordinates all 3 stages; handles run lifecycle
│   │   ├── fetcher.py            Stage 1: Zendesk incremental export + enrichment
│   │   ├── evaluator.py          Stage 2: LLM call, response validation, SLA patch
│   │   └── publisher.py          Stage 3: Zendesk write-back + CSV export
│   ├── clients/
│   │   ├── zendesk.py            Async Zendesk API client (rate-limited)
│   │   ├── claude_client.py      Anthropic Claude client
│   │   └── openai_client.py      OpenAI client
│   ├── models/
│   │   ├── ticket.py             Pydantic models for raw Zendesk ticket data
│   │   └── evaluation.py         Pydantic models for evaluation results
│   ├── storage/
│   │   ├── database.py           SQLite schema + queries
│   │   ├── file_store.py         JSON file read/write for tickets + evaluations
│   │   └── state.py              Persistent run cursor (Zendesk offset)
│   └── utils/
│       ├── sla.py                Authoritative SLA computation + metric rating override
│       ├── rate_limiter.py       Async token-bucket rate limiter
│       ├── retry.py              Exponential backoff + Retry-After header support
│       ├── token_counter.py      Token estimation + comment truncation
│       ├── notifier.py           Email-to-Slack (+ optional webhook)
│       └── logger.py             Rotating file + console logging setup
├── data/                         Runtime data (tickets, evaluations, DB, state)
├── logs/                         Rotating application logs
├── scripts/
│   └── setup_cron.sh             Installs macOS/Linux cron job
├── .env                          API keys — never commit this
├── .env.example                  Template — copy to .env
└── requirements.txt
```

### Key design decisions

- **Single async process, 3 logical stages:** All stages run as async coroutines. Concurrency within each stage is controlled by `asyncio.Semaphore` (configurable in `config.yaml`). No separate processes or message queues — one cron job, one Python process.

- **Idempotent storage:** Every stage writes to disk before the next begins. If the process crashes mid-run, the next run picks up from the last successful point. Tickets already on disk are not re-fetched; evaluations already in the DB are not re-evaluated (unless `--force` is used).

- **Authoritative SLA override:** The LLM's FRT/TTR ratings (METRIC_8 and METRIC_10) are always overridden by `src/utils/sla.py` using Zendesk's pre-computed `reply_time_in_seconds` and `full_resolution_time_in_minutes` fields. This eliminates a class of LLM errors where the model uses the wrong start timestamp.

- **Prompt versioning:** Every evaluation records which prompt version was used. Historical re-evaluation with a new prompt version doesn't delete old scores — it marks them as `is_latest=0` and inserts fresh records. Full score history is always queryable.
