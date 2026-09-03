#!/usr/bin/env bash
# Install a systemd timer that runs the daily QC pipeline — a reliable replacement for
# cron. Unlike cron it survives reboots, and Persistent=true runs a *missed* job on the
# next boot (so a VM that was down at the scheduled minute still catches up).
#
#   bash deploy/install_systemd_timer.sh [HH:MM]     # default 08:00, server-local time
#
# Re-runnable. Needs sudo (writes unit files to /etc/systemd/system). Uses the project's
# .venv python and reads secrets from the repo-root .env (via WorkingDirectory), same as a
# manual run.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_AT="${1:-08:00}"
SVC_USER="$(id -un)"
if [[ -x "$PROJECT_DIR/.venv/bin/python" ]]; then
    PY="$PROJECT_DIR/.venv/bin/python"
else
    PY="$(command -v python3)"
fi
TZ_NAME="$(timedatectl show -p Timezone --value 2>/dev/null || echo 'unknown')"

echo "Project : $PROJECT_DIR"
echo "User    : $SVC_USER"
echo "Python  : $PY"
echo "Schedule: daily at $RUN_AT  (server timezone: $TZ_NAME)"
echo ""

sudo tee /etc/systemd/system/ticket-evaluator.service >/dev/null <<EOF
[Unit]
Description=Ticket Evaluator daily QC run
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=$SVC_USER
WorkingDirectory=$PROJECT_DIR
ExecStart=$PY src/main.py run
EOF

sudo tee /etc/systemd/system/ticket-evaluator.timer >/dev/null <<EOF
[Unit]
Description=Run Ticket Evaluator daily at $RUN_AT

[Timer]
OnCalendar=*-*-* $RUN_AT:00
Persistent=true

[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now ticket-evaluator.timer

# Avoid double-runs: if an old cron entry exists, point out how to remove it.
if crontab -l 2>/dev/null | grep -q ticket-evaluator; then
    echo ""
    echo "NOTE: an old cron entry still exists — remove it to avoid double runs:"
    echo "      bash scripts/setup_cron.sh --remove"
fi

echo ""
echo "Installed. Next scheduled runs:"
systemctl list-timers ticket-evaluator.timer --no-pager || true
echo ""
echo "Test now:      sudo systemctl start ticket-evaluator.service"
echo "Watch live:    journalctl -u ticket-evaluator.service -f"
echo "Last run logs: journalctl -u ticket-evaluator.service --no-pager | tail -n 40"
