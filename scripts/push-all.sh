#!/bin/bash
# Push everything not yet in Nextcloud, for all authorized accounts:
#   usermap → build all boards → per board: import (new) or --update (existing)
#   → final link pass → notes per account (in place, group-covered members pruned).
# Safe to stop between boards; re-run continues.
set -u
cd "$(dirname "$0")/.."
LOG=data/push-all.log
log() { echo "$(date '+%H:%M:%S') $*" >> "$LOG"; }

PIDS=$(python3 - <<'EOF'
import json, glob
ids = set()
for f in glob.glob("data/raw/accounts/*/manifest.json"):
    for pid, p in json.load(open(f)).get("projects", {}).items():
        if not p.get("limit"):
            ids.add(pid)
print(" ".join(sorted(ids, key=int)))
EOF
)
MAPPED=$(python3 -c "import json; print(' '.join(json.load(open('data/state/deck_mapping.json'))['projects']))")
log "projects: $(echo $PIDS | wc -w) (already mapped: $(echo $MAPPED | wc -w))"
python3 -m meister2nc usermap >> "$LOG" 2>&1
python3 -m meister2nc build deck --project "$(echo $PIDS | tr ' ' ',')" >> "$LOG" 2>&1 || { log "BUILD FAILED"; exit 1; }

for pid in $PIDS; do
  if echo " $MAPPED " | grep -q " $pid "; then
    python3 -m meister2nc import deck --project "$pid" --update --no-link-pass >> "$LOG" 2>&1 || log "UPDATE FAILED $pid"
  else
    python3 -m meister2nc import deck --project "$pid" --no-link-pass >> "$LOG" 2>&1 || log "IMPORT FAILED $pid"
  fi
done
log "final link pass"
python3 -m meister2nc import deck-links >> "$LOG" 2>&1
for label in $(python3 -m meister2nc auth list | grep "refresh=yes" | cut -d: -f1); do
  [ -f "data/raw/accounts/$label/notes/manifest.json" ] || continue
  log "notes $label"
  python3 -m meister2nc import notes --label "$label" --prune-covered >> "$LOG" 2>&1 || log "NOTES FAILED $label"
done
log "push done"
