#!/bin/bash
# Përgatit stack-un pilot në authority=central duke ndjekur runbook-un M10 (bootstrap → shadow me trafik → provë → ACK → central).
set -eu
: "${PILOT_DIR:?}"; HERE="$(cd "$(dirname "$0")" && pwd)"; ROOT="$HERE/../.."; cd "$ROOT"
"$HERE/stack.sh" up
"$HERE/cutover.sh" bootstrap
run() { ( . "$PILOT_DIR/pilot.env"; .venv/bin/python -m "e2e.pilot.$1" "${@:2}" ); }
run flow_shadow
run traffic 8
"$HERE/cutover.sh" evidence
HASH=$(grep -o '"evidence_hash": "[0-9a-f]*"' "$PILOT_DIR/evidence.out" | grep -o '[0-9a-f]\{64\}')
"$HERE/stack.sh" authority central "$HASH"
echo "prepared: authority=central"
