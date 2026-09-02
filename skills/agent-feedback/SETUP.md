# Agent-Feedback Skill — Handover & Local Setup

How to hand the QC agent-feedback skill to a teammate so they can run it from their **own
local Claude app** (Claude Code / Cowork). The skill reads the shared QC data from Snowflake
**read-only** — the consumer can generate monthly check-ins and ask ad-hoc questions
("why was ticket X rated low", "what can {agent} improve"), but can never write, publish, or
change anything.

There are two roles below: the **admin** (one-time, has Snowflake access) and each
**consumer** (per person who wants to use the skill).

---

## A. Admin — one-time (do this once, then share creds)

You need the Snowflake data loaded and the read-only user created. If you followed
[`DEPLOY.md`](../../DEPLOY.md) this is already done; otherwise:

1. **Load the QC data into Snowflake** — DEPLOY.md steps 3 + 5 (backfill locally, migrate).
2. **Create the read-only role/user + weights + views** (as `ACCOUNTADMIN`):
   ```bash
   snowsql -f deploy/snowflake_reader.sql       # SELECT-only role + user (edit {READER_PASSWORD})
   snowsql -f deploy/seed_metric_weights.sql     # metric registry + weights (sum = 100)
   snowsql -f deploy/snowflake_views.sql         # convenience views
   ```
   `snowflake_reader.sql` creates `TICKET_EVALUATOR_READER_USER`. One shared read-only user is
   fine for the whole team (it can only SELECT). If you prefer one user per person, copy the
   `CREATE USER … GRANT ROLE` block and rename.

3. **Hand each consumer these 6 values** (the reader password is a secret — send it over a
   secure channel, not in a doc/ticket):

   | Variable | Example | Notes |
   |----------|---------|-------|
   | `SNOWFLAKE_ACCOUNT` | `ab12345.us-east-1` | account locator |
   | `SNOWFLAKE_WAREHOUSE` | `COMPUTE_WH` | |
   | `SNOWFLAKE_DATABASE` | `SUPPORT_ANALYTICS` | |
   | `SNOWFLAKE_SCHEMA` | `ZENDESK_QC` | |
   | `SNOWFLAKE_READER_USER` | `TICKET_EVALUATOR_READER_USER` | SELECT-only |
   | `SNOWFLAKE_READER_PASSWORD` | *(secret)* | share securely |

4. **Share the skill folder** — the whole `agent-feedback/` directory (zip it, drop it in shared
   storage, or point them at a repo checkout). It is **self-contained**: teammates do **not** need
   the app repo, `src/`, or `config/`. Share this `SETUP.md` with it.

---

## B. Consumer — set up on your local machine (~3 min)

**Prerequisites:** the Claude app you already use (Claude Code CLI, or Cowork), Python 3.11+,
and the 6 values above from your admin. **No repo clone needed.**

1. **Install the skill folder** into your personal skills directory so any Claude session finds it:
   ```bash
   mkdir -p ~/.claude/skills
   cp -R /path/to/agent-feedback ~/.claude/skills/agent-feedback   # the folder your admin shared
   cd ~/.claude/skills/agent-feedback
   ```
   (If you already have the app repo, the folder is `skills/agent-feedback/` — just `cd` into it;
   you can skip the copy.)

2. **Add your reader creds** to a `.env` **inside this folder** (reader creds only — no pipeline
   user, no API keys):
   ```bash
   cat > .env <<'EOF'
   SNOWFLAKE_ACCOUNT=ab12345.us-east-1
   SNOWFLAKE_WAREHOUSE=COMPUTE_WH
   SNOWFLAKE_DATABASE=SUPPORT_ANALYTICS
   SNOWFLAKE_SCHEMA=ZENDESK_QC
   SNOWFLAKE_READER_USER=TICKET_EVALUATOR_READER_USER
   SNOWFLAKE_READER_PASSWORD=paste-secret-here
   EOF
   chmod 600 .env
   ```

3. **Run the one-command setup** (from inside the folder) — it makes a local `.venv`, installs the
   two deps, checks your creds, and runs a read-only smoke test:
   ```bash
   bash setup.sh
   ```
   A clean run ends with `self-check PASSED` and example questions. If it reports missing keys, add
   them to `.env` and re-run.

   **Manual steps** (if you'd rather not use the script):
   ```bash
   python3 -m venv .venv
   .venv/bin/pip install snowflake-connector-python python-dotenv
   .venv/bin/python fetch_qc_data.py --self-check
   ```

4. **Verify** (the smoke test already did this; run it any time):
   ```bash
   python fetch_qc_data.py --self-check
   ```
   You should see `backend: snowflake …`, `connection: OK`, `narrative columns: OK`, a roster, and
   `self-check PASSED`. (The script auto-uses the `.venv` from step 3, so plain `python` is fine.) A
   permissions error on
   anything other than SELECT is expected — the user is SELECT-only by design.

5. **Use it from your Claude app.** The skill is auto-discovered from `~/.claude/skills/`. Just ask,
   e.g.:
   - "Generate the monthly QC feedback for **{agent}** for June 2026."
   - "Review **L1** for June 2026 and give me a check-in per agent."
   - "Give me the **L2 leaderboard** for June 2026."
   - "**Compare {agent} to the team** for June — where are they above/below peers?"
   - "**Who improved or slipped** in L1 this month?"
   - "Review **{agent} for Q2** (April–June)."
   - "**Why was ticket 72247 rated low?**"
   - "What can **{agent}** improve on ticket 71921?"
   - "Why did **{agent}** score low on RCA this month?"

   Claude runs `fetch_qc_data.py` for you and writes the answer per
   [`SKILL.md`](SKILL.md) + [`METHODOLOGY.md`](METHODOLOGY.md). If your app can't auto-run the
   skill, you can always run the fetch commands above by hand and paste the JSON.

---

## Notes & guardrails

- **Read-only, always.** The reader user has `USAGE` + `SELECT` only. The skill cannot write to
  Snowflake or touch Zendesk. Nothing you do here changes production data.
- **Secrets.** The reader password is a shared credential — keep it in `.env` (git-ignored,
  `chmod 600`), never commit it, never paste it into a chat/ticket.
- **Fresh data.** The skill always reads the latest QC data; it's as current as the daily
  pipeline's last run on the VM.
- **Names are exact.** Some agents have variant spellings (e.g. "Dimple M K" vs "Dimple MK") —
  always let `--list-agents` confirm the exact string first.
- **What QC measures.** Written ticket handling only (replies, notes, resolution, status) — not
  live/verbal work. Keep that framing when sharing feedback.
- **Updating the skill.** Re-copy the `agent-feedback/` folder your admin shares (your `.env` and
  `.venv` stay — they're git-ignored and not part of the shared folder). Re-run `bash setup.sh` if
  the deps changed.
