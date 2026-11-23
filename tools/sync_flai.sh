#!/bin/bash
# Kept so the old command still works: the sync is now two-way and lives in sync.pl (flai_sync.pl in the repo).
DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$DIR/sync.pl" ]; then exec perl "$DIR/sync.pl" "$@"; else exec perl "$DIR/flai_sync.pl" "$@"; fi
