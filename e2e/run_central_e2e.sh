#!/usr/bin/env bash
# E2E në shfletues i konsolës Central mbi një stack të përkohshëm LOKAL: SQLite, uvicorn, Vite, Chromium.
# Përdoret nga CI (job central-e2e) dhe lokalisht — i njëjti skript. VETËM kredenciale sintetike të gjeneruara
# në çdo ekzekutim; asnjë provider/SMTP/pagesë; asnjë shërbim i jashtëm.
#
#   E2E_OUT=/path/to/dir e2e/run_central_e2e.sh     # parazgjedhja: direktori e përkohshme (mktemp)
#   (opsional) E2E_CHROME=/rruga/chromium           # përndryshe përdoret Chromium i Playwright
#
# Dalje:  $E2E_OUT/logs/*  dhe  $E2E_OUT/artifacts/*.png (vetëm në dështim) — këto ngarkohen si artefakte.
#         $E2E_OUT/work/*  (DB, çelës, ids.json) NUK ngarkohet kurrë.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PY="${PYTHON:-python}"
OUT="${E2E_OUT:-$(mktemp -d)}"
API_PORT=8101 UI_PORT=5174
mkdir -p "$OUT/logs" "$OUT/artifacts" "$OUT/work"
export PYTHONPATH="$ROOT"

# Roja "pa ofrues të vërtetë": refuzon nëse mjedisi ka kredenciale SMS/email/pagese.
"$PY" -c "
import os, sys
from e2e.pilot.guard import violations
v = violations(os.environ)
sys.exit('REFUSING TO RUN: ' + '; '.join(v)) if v else None"

rand() { "$PY" -c "import secrets;print(secrets.token_urlsafe(24))"; }
export CENTRAL_ENV=development
export CENTRAL_DATABASE_URL="sqlite:///$OUT/work/central.db"
export CENTRAL_AUTH_SECRET="$(rand)$(rand)"
export E2E_ADMIN_PASSWORD="$(rand)" E2E_OPERATOR_PASSWORD="$(rand)"
export E2E_KEY_OUT="$OUT/work/ent_key.pem"
export E2E_ENT_DB="$OUT/work/enterprise.db"
export E2E_ARTIFACTS="$OUT/artifacts"
export E2E_BASE="http://127.0.0.1:$UI_PORT" E2E_CENTRAL_URL="http://127.0.0.1:$API_PORT"
export E2E_IDS="$OUT/work/ids.json" E2E_KEY="$E2E_KEY_OUT"
export E2E_ADMIN_PW="$E2E_ADMIN_PASSWORD" E2E_OP_PW="$E2E_OPERATOR_PASSWORD"

port_busy() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }
for p in "$API_PORT" "$UI_PORT"; do
  if port_busy "$p"; then echo "port $p already in use: refusing to run against a foreign stack" >&2; exit 1; fi
done

PIDS=()
cleanup() {
  for p in "${PIDS[@]:-}"; do [ -n "$p" ] && kill -TERM -- "-$p" 2>/dev/null || true; done
  for _ in $(seq 1 10); do port_busy "$API_PORT" || port_busy "$UI_PORT" || break; sleep 0.5; done
}
trap cleanup EXIT

start() { # emri, komanda... ; çdo proces në grupin e vet që të mbyllet me gjithë fëmijët
  local n=$1; shift
  setsid "$@" > "$OUT/logs/$n.log" 2>&1 < /dev/null &
  PIDS+=("$!")
}
wait_http() { # url, emri
  for _ in $(seq 1 60); do curl -sf -o /dev/null "$1" && return 0; sleep 1; done
  echo "TIMEOUT waiting for $2 ($1); see $OUT/logs/" >&2; return 1
}

"$PY" -m alembic -c apps/central/alembic.ini upgrade head > "$OUT/logs/alembic_central.log" 2>&1
SMS_ENV=development SMS_DATABASE_URL="sqlite:///$E2E_ENT_DB" \
  "$PY" -m alembic upgrade head > "$OUT/logs/alembic_enterprise.log" 2>&1
"$PY" e2e/central_seed.py "$E2E_IDS" > "$OUT/logs/seed.log" 2>&1
"$PY" e2e/commercial_seed.py "$E2E_IDS" >> "$OUT/logs/seed.log" 2>&1

start central_api "$PY" -m uvicorn apps.central.main:app --host 127.0.0.1 --port "$API_PORT"
start vite_central env CENTRAL_API_URL="$E2E_CENTRAL_URL" \
  node frontend-central/node_modules/vite/bin/vite.js frontend-central \
  --port "$UI_PORT" --host 127.0.0.1 --strictPort
wait_http "http://127.0.0.1:$API_PORT/healthz" "Central API"
wait_http "http://127.0.0.1:$UI_PORT/healthz" "Vite (proxy drejt Central API)"

rc=0
"$PY" e2e/test_central_console.py 2>&1 | tee "$OUT/logs/test_central_console.out" || rc=1
"$PY" e2e/test_central_audit.py 2>&1 | tee "$OUT/logs/test_central_audit.out" || rc=1
echo "central e2e exit=$rc (logs: $OUT/logs)"
exit "$rc"
