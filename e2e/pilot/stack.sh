#!/bin/bash
# Stack M11-H: PostgreSQL + 2 API uvicorn + 3 punonjës Enterprise + 2 Vite. VETËM zhvillim, FakeProvider, asnjë kredencial i vërtetë.
#   PILOT_DIR=/path/to/scratch e2e/pilot/stack.sh up|down|status|reseed
set -u
: "${PILOT_DIR:?set PILOT_DIR to a scratch directory}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PY="$ROOT/.venv/bin/python"; PGU="postgresql+psycopg://sms:sms@localhost:5432"
ENVF="$PILOT_DIR/pilot.env"; mkdir -p "$PILOT_DIR/logs"
ports() { echo 8100 8101 5173 5174; }
stop_all() {
  for f in "$PILOT_DIR"/pids/*; do [ -f "$f" ] && kill "$(cat "$f")" 2>/dev/null; rm -f "$f"; done
  for p in $(ports); do fuser -k "$p"/tcp >/dev/null 2>&1; done
  pkill -f "[.]venv/bin/python -m (e2e[.]pilot[.]|app[.])worker" 2>/dev/null; pkill -f "[.]venv/bin/.*uvicorn (app|apps[.]central)" 2>/dev/null; sleep 1
}
mkenv() {
  [ -f "$ENVF" ] && return
  H() { python3 -c "import secrets;print(secrets.token_hex(32))"; }
  cat > "$ENVF" <<E
export SMS_ENV=development SMS_DATABASE_URL=$PGU/h_ent SMS_PII_HMAC_KEY=$(H) SMS_SECRETS_KEY=$(python3 -c "import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())")
export SMS_ADMIN_API_KEY=$(H) SMS_DLR_SECRETS='{"fake":"pilot-dlr-secret"}'
export SMS_CP_BASE_URL=http://127.0.0.1:8101 SMS_CP_CLIENT_ID=ent-h SMS_CP_KEY_ID=k1 SMS_CP_PRIVATE_KEY_PATH=$PILOT_DIR/ent_key.pem
export SMS_SENDER_SYNC_ENABLED=true SMS_SENDER_REQUEST_REPORTING=true SMS_SENDER_OPS_REPORTING=true SMS_SENDER_POLL_INTERVAL_SECONDS=5 SMS_SENDER_REQUEST_INTERVAL_SECONDS=5 SMS_SENDER_OPS_INTERVAL_SECONDS=15
export SMS_SENDER_AUTHORITY=shadow SMS_SENDER_AUTHORITY_ACK= SMS_SENDER_DISPATCH_RECHECK=true SMS_SENDER_EVIDENCE_MIN_SAMPLES=5 SMS_SENDER_SHADOW_SAMPLE_PCT=100
export CENTRAL_ENV=development CENTRAL_DATABASE_URL=$PGU/h_cen CENTRAL_AUTH_SECRET=$(H)
export PILOT_ADMIN_PASSWORD=$(python3 -c "import secrets;print(secrets.token_urlsafe(18))") PILOT_OPERATOR_PASSWORD=$(python3 -c "import secrets;print(secrets.token_urlsafe(18))")
export PILOT_KEY_OUT=$PILOT_DIR/ent_key.pem PYTHONPATH=$ROOT
E
}
start() { # name, command...
  n=$1; shift; mkdir -p "$PILOT_DIR/pids"
  ( (cd "$ROOT" && setsid nohup "$@" > "$PILOT_DIR/logs/$n.log" 2>&1 < /dev/null & echo $! > "$PILOT_DIR/pids/$n") ) > /dev/null 2>&1 < /dev/null
}
start_ent() {
  . "$ENVF"
  start ent "$ROOT/.venv/bin/uvicorn" app.main:app --port 8100
  start w_sms $PY -m e2e.pilot.worker --role sms
  start w_sender_sync $PY -m e2e.pilot.worker --role sender_control_plane
  start w_sender_req $PY -m e2e.pilot.worker --role sender_request_reporter
}
stop_ent() { for n in ent w_sms w_sender_sync w_sender_req; do [ -f "$PILOT_DIR/pids/$n" ] && kill "$(cat "$PILOT_DIR/pids/$n")" 2>/dev/null; rm -f "$PILOT_DIR/pids/$n"; done; fuser -k 8100/tcp >/dev/null 2>&1; pkill -f "[.]venv/bin/python -m (e2e[.]pilot[.]|app[.])worker" 2>/dev/null; sleep 1; }
worker_cmd() { case "$1" in w_sms) echo "$PY -m e2e.pilot.worker --role sms";; w_sender_sync) echo "$PY -m e2e.pilot.worker --role sender_control_plane";; w_sender_req) echo "$PY -m e2e.pilot.worker --role sender_request_reporter";; esac; }
case "${1:-}" in
  stop-worker) role=$(worker_cmd "$2" | sed 's/.*--role //'); pkill -f "[.]venv/bin/python -m e2e[.]pilot[.]worker --role $role\$" 2>/dev/null; rm -f "$PILOT_DIR/pids/$2"; sleep 1; echo "stopped $2" ;;
  start-worker) . "$ENVF"; start "$2" $(worker_cmd "$2"); sleep 2; echo "started $2" ;;
  stop-central) fuser -k 8101/tcp >/dev/null 2>&1; pkill -f "[.]venv/bin/.*uvicorn apps[.]central" 2>/dev/null; sleep 1; echo "central stopped" ;;
  start-central) . "$ENVF"; start central "$ROOT/.venv/bin/uvicorn" apps.central.main:app --port 8101; for i in $(seq 1 30); do curl -sf -o /dev/null http://127.0.0.1:8101/healthz && break; sleep 1; done; echo "central started" ;;
  restart-ent) stop_ent; start_ent; for i in $(seq 1 40); do curl -sf -o /dev/null http://127.0.0.1:8100/healthz && break; sleep 1; done; echo restarted ;;
  authority) # authority <mode> [ack]: ndryshon env-in e rinis Enterprise (API + punonjësit)
    sed -i "s/SMS_SENDER_AUTHORITY=[a-z]* SMS_SENDER_AUTHORITY_ACK=[0-9a-f]*/SMS_SENDER_AUTHORITY=$2 SMS_SENDER_AUTHORITY_ACK=${3:-}/" "$ENVF"; grep -c "SMS_SENDER_AUTHORITY=$2" "$ENVF" >/dev/null || exit 1
    "$0" restart-ent ;;
  up|reseed)
    stop_all; mkenv; sed -i "s/SMS_SENDER_AUTHORITY=[a-z]* SMS_SENDER_AUTHORITY_ACK=[0-9a-f]*/SMS_SENDER_AUTHORITY=shadow SMS_SENDER_AUTHORITY_ACK=/" "$ENVF"; . "$ENVF"; cd "$ROOT"
    for d in h_ent h_cen; do psql "postgresql://sms:sms@localhost:5432/postgres" -qc "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='$d' AND pid<>pg_backend_pid()" >/dev/null 2>&1; psql "postgresql://sms:sms@localhost:5432/postgres" -qc "DROP DATABASE IF EXISTS $d" -c "CREATE DATABASE $d" >/dev/null || exit 1; done
    t0=$(date +%s)
    "$ROOT/.venv/bin/alembic" upgrade head > "$PILOT_DIR/logs/alembic_ent.log" 2>&1 || { tail -5 "$PILOT_DIR/logs/alembic_ent.log"; exit 1; }
    "$ROOT/.venv/bin/alembic" -c apps/central/alembic.ini upgrade head > "$PILOT_DIR/logs/alembic_cen.log" 2>&1 || { tail -5 "$PILOT_DIR/logs/alembic_cen.log"; exit 1; }
    echo "migrations (clean install, both DBs): $(( $(date +%s) - t0 )) s"
    $PY -m e2e.pilot.seed "$PILOT_DIR/ids.json" > "$PILOT_DIR/logs/seed.log" 2>&1 || { tail -12 "$PILOT_DIR/logs/seed.log"; exit 2; }
    start central "$ROOT/.venv/bin/uvicorn" apps.central.main:app --port 8101
    start_ent
    for i in $(seq 1 40); do curl -sf -o /dev/null http://127.0.0.1:8100/healthz && curl -sf -o /dev/null http://127.0.0.1:8101/healthz && break; sleep 1; done
    ( (cd "$ROOT/frontend" && SMS_API_URL=http://127.0.0.1:8100 setsid nohup npx vite --port 5173 --host 127.0.0.1 > "$PILOT_DIR/logs/vite_ent.log" 2>&1 < /dev/null & echo $! > "$PILOT_DIR/pids/vite_ent") ) > /dev/null 2>&1 < /dev/null
    ( (cd "$ROOT/frontend-central" && CENTRAL_API_URL=http://127.0.0.1:8101 setsid nohup npx vite --port 5174 --host 127.0.0.1 > "$PILOT_DIR/logs/vite_cen.log" 2>&1 < /dev/null & echo $! > "$PILOT_DIR/pids/vite_cen") ) > /dev/null 2>&1 < /dev/null
    sleep 4; echo up ;;
  down) stop_all; echo down ;;
  status) for p in $(ports); do printf "%s: " $p; curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:$p/ ; done; ps -eo args | grep -E "app.worker|uvicorn" | grep -v grep | cut -c1-90 ;;
  *) echo "usage: $0 up|down|status"; exit 2 ;;
esac
