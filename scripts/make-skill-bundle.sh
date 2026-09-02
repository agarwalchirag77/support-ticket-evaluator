#!/usr/bin/env bash
# Package the agent-feedback skill into a shareable archive for teammates.
#
# Excludes secrets (.env) and local artifacts (.venv, __pycache__, *.pyc), so the
# output is always safe to hand out. Teammates unzip it into ~/.claude/skills/ and
# run setup.sh (see skills/agent-feedback/SETUP.md).
#
#   bash scripts/make-skill-bundle.sh
#
# Produces dist/agent-feedback-skill-<YYYYMMDD>.zip (or .tar.gz if zip is absent).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SKILL="agent-feedback"
SRC="$ROOT/skills/$SKILL"
[[ -d "$SRC" ]] || { echo "!! skill folder not found: $SRC" >&2; exit 1; }

DIST="$ROOT/dist"
mkdir -p "$DIST"
BASENAME="agent-feedback-skill-$(date +%Y%m%d)"

cd "$ROOT/skills"
if command -v zip >/dev/null 2>&1; then
    OUT="$DIST/$BASENAME.zip"
    rm -f "$OUT"
    zip -rq "$OUT" "$SKILL" \
        -x "$SKILL/.env" "$SKILL/.venv/*" "$SKILL/__pycache__/*" "*.pyc" "$SKILL/.DS_Store" "$SKILL/*.bak"
    listing="$(unzip -Z1 "$OUT")"
else
    OUT="$DIST/$BASENAME.tar.gz"
    rm -f "$OUT"
    tar --exclude="$SKILL/.env" --exclude="$SKILL/.venv" --exclude="$SKILL/__pycache__" \
        --exclude='*.pyc' --exclude="$SKILL/.DS_Store" -czf "$OUT" "$SKILL"
    listing="$(tar -tzf "$OUT")"
fi

# Safety assertion: never ship secrets or a venv.
if echo "$listing" | grep -qE '(^|/)\.env$|/\.venv/'; then
    echo "!! ABORT: archive contains .env or .venv — not shareable. Check the excludes." >&2
    rm -f "$OUT"
    exit 1
fi

echo "✓ Bundle: $OUT"
echo ""
echo "Contents:"
echo "$listing" | sed 's/^/  /'
echo ""
echo "Safe to share — no .env / .venv included."
echo "Teammate steps: unzip into ~/.claude/skills/, add a .env with reader creds,"
echo "then run: bash ~/.claude/skills/$SKILL/setup.sh   (details in $SKILL/SETUP.md)"
