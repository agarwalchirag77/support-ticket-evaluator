#!/bin/bash
# Setup script: installs a daily cron job for the Ticket Evaluation Tool.
# Run once: bash scripts/setup_cron.sh
# Remove: bash scripts/setup_cron.sh --remove

set -e

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="$(which python3)"
CRON_LOG="$PROJECT_DIR/logs/cron.log"
CRON_TIME="0 8 * * *"   # Daily at 8:00 AM — edit to change schedule
CRON_CMD="$CRON_TIME cd \"$PROJECT_DIR\" && $PYTHON src/main.py run >> \"$CRON_LOG\" 2>&1"
CRON_MARKER="# ticket-evaluator"

if [[ "$1" == "--remove" ]]; then
    echo "Removing ticket-evaluator cron job..."
    (crontab -l 2>/dev/null | grep -v "$CRON_MARKER") | crontab -
    echo "Done. Remaining cron jobs:"
    crontab -l 2>/dev/null || echo "(empty)"
    exit 0
fi

# Create logs dir
mkdir -p "$PROJECT_DIR/logs"

# Check .env exists
if [[ ! -f "$PROJECT_DIR/.env" ]]; then
    echo "Warning: .env not found at $PROJECT_DIR/.env"
    echo "Copy .env.example → .env and fill in your API keys before the first run."
fi

# Install cron entry (idempotent: remove old entry first)
(
    crontab -l 2>/dev/null | grep -v "$CRON_MARKER"
    echo "$CRON_CMD $CRON_MARKER"
) | crontab -

echo "✓ Cron job installed:"
echo "  Schedule: $CRON_TIME (daily at 8:00 AM)"
echo "  Project:  $PROJECT_DIR"
echo "  Log:      $CRON_LOG"
echo ""
echo "To verify: crontab -l"
echo "To remove: bash scripts/setup_cron.sh --remove"
echo "To change time: edit CRON_TIME in this script and re-run"
