#!/usr/bin/env bash
# One-command setup for the read-only agent-feedback skill — standalone, no repo needed.
# Works whether this folder lives in the repo (skills/agent-feedback/) or was copied to
# ~/.claude/skills/agent-feedback/. Creates a venv inside this folder, installs the two
# deps the fetch needs, and runs a read-only smoke test. Never writes secrets.
#
#   bash setup.sh          # from inside the skill folder
#
# Prereq: python3 (>=3.11) and the reader creds from your admin in a .env in THIS folder
# (see SETUP.md). fetch_qc_data.py auto-uses the venv this creates.
set -euo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SKILL_DIR"
echo "==> Skill folder: $SKILL_DIR"

# 1. venv + the two runtime deps (inside the skill folder) -------------------
if [[ ! -x ".venv/bin/python" ]]; then
    echo "==> Creating virtualenv (.venv)"
    python3 -m venv .venv
fi
echo "==> Installing dependencies (snowflake-connector-python, python-dotenv)"
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet snowflake-connector-python python-dotenv
PY=".venv/bin/python"

# 2. Check reader creds in .env (this folder) — never write secrets ----------
REQUIRED=(SNOWFLAKE_ACCOUNT SNOWFLAKE_WAREHOUSE SNOWFLAKE_DATABASE SNOWFLAKE_SCHEMA \
          SNOWFLAKE_READER_USER SNOWFLAKE_READER_PASSWORD)
missing=()
if [[ -f .env ]]; then
    for k in "${REQUIRED[@]}"; do
        grep -qE "^${k}=." .env || missing+=("$k")
    done
else
    echo "!! No .env in this folder ($SKILL_DIR/.env)"
    missing=("${REQUIRED[@]}")
fi

if (( ${#missing[@]} > 0 )); then
    echo ""
    echo "!! Missing reader credentials — add these to $SKILL_DIR/.env (values from your admin):"
    for k in "${missing[@]}"; do echo "     $k=..."; done
    echo ""
    echo "   Then re-run: bash $SKILL_DIR/setup.sh   (see SETUP.md)"
    exit 1
fi
chmod 600 .env 2>/dev/null || true

# 3. Smoke test --------------------------------------------------------------
echo ""
echo "==> Connection smoke test (read-only)"
if "$PY" fetch_qc_data.py --self-check; then
    echo ""
    echo "✓ Setup complete. Open your Claude app and ask, e.g.:"
    echo "    \"Generate the monthly QC feedback for <agent> for 2026-06\""
    echo "    \"Give me the L2 leaderboard for 2026-06\""
    echo "    \"Why was ticket 72247 rated low?\""
else
    echo ""
    echo "!! Smoke test failed — check the reader creds / network and re-run."
    exit 1
fi
