#!/bin/bash
set -e
#
# Usage:
#   ./run.sh doctor                  — check config & paths
#   ./run.sh plan                    — scan + write plan.jsonl (nothing touched)
#   ./run.sh plan --skip-exiftool    — same, without ExifTool
#   ./run.sh run                     — dry-run: show what would happen
#   ./run.sh run --execute           — for real: copy/move files
#   ./run.sh revert                  — move ALL sorted files back to SOURCE_ROOT (flat) + reset state
#
#   Prepend --bg to any command to run it in the background (survives SSH disconnect):
#   ./run.sh --bg plan
#   ./run.sh --bg run --execute
#
# All other flags are forwarded to the CLI (e.g. --env-file, --config, --mode).

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$SCRIPT_DIR/venv"
LOG_DIR="$SCRIPT_DIR/logs"

# Handle --bg: re-launch this script via nohup and exit
if [[ "${1:-}" == "--bg" ]]; then
    shift
    mkdir -p "$LOG_DIR"
    LOGFILE="$LOG_DIR/run_$(date +%Y%m%d_%H%M%S).log"
    nohup "$0" "$@" > "$LOGFILE" 2>&1 &
    PID=$!
    echo "Started in background  PID=$PID"
    echo "Log file: $LOGFILE"
    echo ""
    echo "Follow live:  tail -f $LOGFILE"
    echo "Check status: kill -0 $PID 2>/dev/null && echo running || echo finished"
    exit 0
fi

# Handle revert: move all media files from TARGET_ROOT back to SOURCE_ROOT (flat).
if [[ "${1:-}" == "revert" ]]; then
    ENV_FILE="${2:-.env}"
    if [[ ! -f "$ENV_FILE" ]]; then
        echo "Error: $ENV_FILE not found. Usage: ./run.sh revert [path/to/.env]"
        exit 1
    fi

    # Parse SOURCE_ROOT, TARGET_ROOT, OUTPUT_ROOT from .env (ignore comments/empty lines).
    _env_val() { grep -E "^$1=" "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '"'"'" ; }
    SOURCE_ROOT="$(_env_val SOURCE_ROOT)"
    TARGET_ROOT="$(_env_val TARGET_ROOT)"
    OUTPUT_ROOT="$(_env_val OUTPUT_ROOT)"
    OUTPUT_ROOT="${OUTPUT_ROOT:-$TARGET_ROOT/reports}"

    if [[ -z "$SOURCE_ROOT" || -z "$TARGET_ROOT" ]]; then
        echo "Error: SOURCE_ROOT and TARGET_ROOT must be set in $ENV_FILE"
        exit 1
    fi

    MEDIA_EXTS="jpg|jpeg|heic|png|mov|mp4"
    FILE_COUNT=$(find "$TARGET_ROOT" -type f | grep -iE "\.($MEDIA_EXTS)$" | wc -l)
    STATE_DB="$OUTPUT_ROOT/state.sqlite"
    PLAN_FILE="$OUTPUT_ROOT/plan.jsonl"

    echo "=========================================="
    echo "  REVERT — this will:"
    echo "  • move $FILE_COUNT media files from:"
    echo "      $TARGET_ROOT"
    echo "    back to (flat, no subfolders):"
    echo "      $SOURCE_ROOT"
    echo "  • delete $STATE_DB"
    echo "  • delete $PLAN_FILE (if exists)"
    echo "=========================================="
    echo ""
    read -r -p "Type YES to confirm: " CONFIRM
    if [[ "$CONFIRM" != "YES" ]]; then
        echo "Aborted."
        exit 0
    fi

    echo "Checking for filename conflicts..."
    CONFLICTS=0
    while IFS= read -r f; do
        BASENAME="$(basename "$f")"
        if [[ -e "$SOURCE_ROOT/$BASENAME" ]]; then
            echo "  CONFLICT: $BASENAME already exists in SOURCE_ROOT"
            echo "    from: $f"
            CONFLICTS=$((CONFLICTS + 1))
        fi
    done < <(find "$TARGET_ROOT" -type f | grep -iE "\.($MEDIA_EXTS)$")

    if [[ $CONFLICTS -gt 0 ]]; then
        echo ""
        echo "WARNING: $CONFLICTS filename conflict(s) detected."
        echo "Resolve them manually before reverting (rename or remove the duplicates)."
        exit 1
    fi

    echo "No conflicts. Moving files..."
    find "$TARGET_ROOT" -type f | grep -iE "\.($MEDIA_EXTS)$" | while read -r f; do
        mv "$f" "$SOURCE_ROOT/$(basename "$f")"
    done

    echo "Removing empty directories in TARGET_ROOT..."
    find "$TARGET_ROOT" -mindepth 1 -type d -empty -delete 2>/dev/null || true

    echo "Deleting state database..."
    rm -f "$STATE_DB"
    rm -f "$PLAN_FILE"

    echo ""
    echo "Done. All files are back in $SOURCE_ROOT."
    echo "You can now re-run: ./run.sh --bg plan && ./run.sh --bg run --execute"
    exit 0
fi

if [ ! -d "$VENV" ]; then
    echo "Creating virtual environment..."
    python3 -m venv "$VENV"
fi

if ! "$VENV/bin/pip" show photo-sorter > /dev/null 2>&1; then
    echo "Installing photo-sorter..."
    "$VENV/bin/pip" install -e "$SCRIPT_DIR" --quiet
fi

# Default to the 'run' subcommand when no subcommand is given (but keep --help at top level).
case "${1:-}" in
    doctor|plan|run|--help|-h|"") ;;
    *) set -- run "$@" ;;
esac

exec "$VENV/bin/python" -m photo_sorter.cli "$@"
