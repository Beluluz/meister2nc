#!/bin/bash
# Full refresh: re-fetch every task and note of every authorized account
# (--refresh; cached attachment files are kept), then push everything with
# push-all.sh (new boards, --update for existing ones, link pass, notes per account).
# Run from anywhere; paths are relative to the repository root.
set -u
cd "$(dirname "$0")/.."
LOG=data/full-refresh.log
log() { echo "$(date '+%H:%M:%S') $*" >> "$LOG"; }

log "extract tasks --label all --refresh"
python3 -m meister2nc extract tasks --label all --refresh --workers 4 >> "$LOG" 2>&1 || log "EXTRACT TASKS FAILED (continuing with what was fetched)"
log "extract notes --label all --refresh"
python3 -m meister2nc extract notes --label all --refresh >> "$LOG" 2>&1 || log "EXTRACT NOTES FAILED (continuing with what was fetched)"
log "push (see data/push-all.log)"
bash scripts/push-all.sh
log "full refresh done"
