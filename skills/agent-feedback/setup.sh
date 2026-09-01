#!/usr/bin/env bash
# One-command setup for the read-only agent-feedback skill on a teammate's machine.
# Creates a venv, installs the few deps the fetch needs, points the app at Snowflake,
# checks the reader creds, and runs a connection smoke test. Never writes secrets.
#
#   bash skills/agent-feedback/setup.sh
#
# Prereqs: python3 (>=3.11), and the reader creds from your admin in a repo-root .env
# (see skills/agent-feedback/SETUP.md).
set -euo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SKILL_DIR/../.." && pwd)"
cd "$ROOT"

echo "==> Project: $ROOT"

# 1. venv + minimal deps -----------------------------------------------------
if [[ ! -x ".venv/bin/python" ]]; then
    echo "==> Creating virtualenv (.venv)"
    python3 -m venv .venv
fi
echo "==> Installing fetch dependencies"
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet snowflake-connector-python pyyaml python-dotenv pydantic
PY=".venv/bin/python"

# 2. Point storage at Snowflake (idempotent, OS-aware sed) --------------------
echo "==> Setting storage.backend: snowflake in config/config.yaml"
if [[ "$(uname)" == "Darwin" ]]; then
    sed -i '' 's/^\([[:space:]]*backend:\).*/\1 snowflake/' config/config.yaml
else
    sed -i 's/^\([[:space:]]*backend:\).*/\1 snowflake/' config/config.yaml
fi
grep -A1 '^storage:' config/config.yaml || true

# 3. Check reader creds in .env (do NOT write secrets) ------------------------
REQUIRED=(SNOWFLAKE_ACCOUNT SNOWFLAKE_WAREHOUSE SNOWFLAKE_DATABASE SNOWFLAKE_SCHEMA \
          SNOWFLAKE_READER_USER SNOWFLAKE_READER_PASSWORD)
missing=()
if [[ -f .env ]]; then
    for k in "${REQUIRED[@]}"; do
        grep -qE "^${k}=." .env || missing+=("$k")
    done
else
    echo "!! No .env found at $ROOT/.env"
    missing=("${REQUIRED[@]}")
fi

if (( ${#missing[@]} > 0 )); then
    echo ""
    echo "!! Missing reader credentials in .env — add these keys (values from your admin):"
    for k in "${missing[@]}"; do echo "     $k=..."; done
    echo ""
    echo "   Then re-run: bash skills/agent-feedback/setup.sh"
    echo "   (see skills/agent-feedback/SETUP.md for details)"
    exit 1
fi
chmod 600 .env 2>/dev/null || true

# 4. Smoke test --------------------------------------------------------------
echo ""
echo "==> Connection smoke test (read-only)"
if "$PY" skills/agent-feedback/fetch_qc_data.py --self-check; then
    echo ""
    echo "✓ Setup complete. Open this repo in Claude Code / Cowork and ask, e.g.:"
    echo "    \"Generate the monthly QC feedback for <agent> for 2026-06\""
    echo "    \"Why was ticket 72247 rated low?\""
else
    echo ""
    echo "!! Smoke test failed — check the reader creds / network and re-run."
    exit 1
fi
