#!/bin/bash
# Runbook M10 (§4–§6) i ekzekutuar mbi stack-un pilot: bootstrap → readiness → provë → ACK → authority=central.  VETËM zhvillim.
set -eu
: "${PILOT_DIR:?}"; ROOT="$(cd "$(dirname "$0")/../.." && pwd)"; cd "$ROOT"; . "$PILOT_DIR/pilot.env"; P=.venv/bin/python
REV=$(git rev-parse --short HEAD)
STEP=${1:-all}
if [ "$STEP" = all ] || [ "$STEP" = bootstrap ]; then
$P -m scripts.sender_bootstrap export --out "$PILOT_DIR/senders.json" --source-revision "$REV"
$P -m apps.central.tools.sender_import --artifact "$PILOT_DIR/senders.json" --json > "$PILOT_DIR/dry.json"
H=$($P -c "import json;print(json.load(open('$PILOT_DIR/dry.json'))['artifact_hash'])")
$P -c "import json;d=json.load(open('$PILOT_DIR/dry.json'));print('dry-run summary:',d['summary'],'unresolved:',d['unresolved'])"
$P -m apps.central.tools.sender_import --artifact "$PILOT_DIR/senders.json" --apply --actor-email admin@pilot.test --ack-artifact-hash "$H" --json > "$PILOT_DIR/apply.json"
sleep 8
$P -m scripts.sender_bootstrap reconcile --central-report "$PILOT_DIR/apply.json" --record
fi
if [ "$STEP" = all ] || [ "$STEP" = evidence ]; then
$P -m apps.central.tools.sender_central_readiness --json > "$PILOT_DIR/central.json"
$P -m scripts.sender_policy_readiness --target central --central-readiness "$PILOT_DIR/central.json" --json > "$PILOT_DIR/pol.json" || true
$P -c "import json;d=json.load(open('$PILOT_DIR/pol.json'));print('readiness:',[(c['name'],c['level'],c['reason'][:70]) for c in d['checks'] if c['level']!='PASS'])"
$P -m scripts.sender_cutover evidence --actor pilot-operator --code-revision "$REV" --central-readiness "$PILOT_DIR/central.json" | tee "$PILOT_DIR/evidence.out"
fi
