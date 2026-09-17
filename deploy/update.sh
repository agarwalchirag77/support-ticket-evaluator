#!/usr/bin/env bash
# Quick redeploy on the remote VM after a change: pull latest, refresh deps if
# requirements changed, reload the systemd unit if its definition changed, and
# (optionally) trigger a run now.
#
#   bash deploy/update.sh              # pull + refresh deps; report if a restart is needed
#   bash deploy/update.sh --run        # ...and trigger a pipeline run immediately
#   bash deploy/update.sh --run-only   # skip git pull, just run the pipeline now
#
# Why there's usually nothing to "restart": the pipeline is a scheduled batch job
# (systemd timer / cron), NOT a persistent daemon — every scheduled fire launches a
# fresh `python src/main.py run`, so code/config changes take effect on the next run
# automatically. A restart only matters when the systemd UNIT itself changed, or when
# you want to run right now (--run). git pull uses --autostash so a local config.yaml
# edit (e.g. backend: snowflake) is preserved.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"
BRANCH="${BRANCH:-main}"
PY="$PROJECT_DIR/.venv/bin/python"
PIP="$PROJECT_DIR/.venv/bin/pip"

DO_RUN=false
DO_RUN_ONLY=false
for arg in "$@"; do
    case "$arg" in
        --run)      DO_RUN=true ;;
        --run-only) DO_RUN=true; DO_RUN_ONLY=true ;;
        -h|--help)  awk 'NR>1 && /^#/{sub(/^# ?/,"");print;next} NR>1{exit}' "$0"; exit 0 ;;
        *) echo "Unknown argument: $arg (see --help)" >&2; exit 2 ;;
    esac
done

_timer_installed() { systemctl list-unit-files 2>/dev/null | grep -q '^ticket-evaluator\.'; }

echo "==> Project: $PROJECT_DIR  (branch: $BRANCH)"

if ! $DO_RUN_ONLY; then
    git rev-parse --is-inside-work-tree >/dev/null 2>&1 || { echo "!! Not a git repo." >&2; exit 1; }
    before="$(git rev-parse HEAD)"
    echo "==> Pulling latest…"
    git pull --autostash origin "$BRANCH"
    after="$(git rev-parse HEAD)"

    if [[ "$before" == "$after" ]]; then
        echo "==> Already up to date ($after)."
    else
        echo "==> Updated: ${before:0:9} → ${after:0:9}"
        changed="$(git diff --name-only "$before" "$after")"
        echo "    Changed files:"; echo "$changed" | sed 's/^/      /'

        # 1. Python dependencies
        if echo "$changed" | grep -qx 'requirements.txt'; then
            if [[ -x "$PIP" ]]; then
                echo "==> requirements.txt changed — installing deps…"
                "$PIP" install -q -r requirements.txt
            else
                echo "!! requirements.txt changed but $PIP not found — create/activate the venv, then re-run."
            fi
        fi

        # 2. systemd unit definition (only reinstall/reload when it actually changed)
        if _timer_installed && echo "$changed" | grep -qx 'deploy/install_systemd_timer.sh'; then
            echo "!! The systemd unit installer changed. Re-apply it (this resets the schedule to the"
            echo "   time you pass) and reload:"
            echo "     bash deploy/install_systemd_timer.sh 08:00 && sudo systemctl daemon-reload"
        fi
    fi
fi

# 3. Optional: trigger a run now (verifies the deploy end-to-end)
if $DO_RUN; then
    if _timer_installed; then
        echo "==> Triggering a run now via systemd…"
        sudo systemctl start ticket-evaluator.service
        echo "    Watch:  journalctl -u ticket-evaluator.service -f"
    elif [[ -x "$PY" ]]; then
        echo "==> Running the pipeline directly…"
        "$PY" src/main.py run
    else
        echo "!! No systemd timer and no .venv python found — cannot run." >&2; exit 1
    fi
fi

# 4. Show health so you can confirm the deploy landed
if [[ -x "$PY" ]]; then
    echo ""; echo "==> Current status:"
    "$PY" src/main.py status 2>/dev/null | sed -n '1,14p' || echo "   (status unavailable)"
fi
echo "==> Done."
