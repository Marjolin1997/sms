# M10-PROD — përgatitja operacionale e cutover-it (gjendja e provave dhe paketa e komandave)

**Rev i kodit të pranuar:** `8c820ff` (M10 CODE COMPLETE). Ky dokument NUK ndryshon kodin. **Produksioni mbetet BLOKUAR** për `central` derisa të ekzistojnë provat më poshtë.

## 0. Çfarë u verifikua dhe çfarë JO
Auditi u ekzekutua nga një **sandbox zhvillimi** (kontejner pa akses te mjedisi i prodhimit: pa `SMS_*` të vendosur, DB SQLite e parazgjedhur, pa `SMS_CP_BASE_URL`, pa workera, PG vetëm si bazë testesh). Prandaj:
- Të gjitha të dhënat "prodhimi" (numra, versione, shërbime, skope, backup, monitorim) janë **PA PROVË** — nuk u shpikën.
- Provuam komandat mbi **të dhëna sintetike** (provë e komandave, jo provë prodhimi) dhe provuam garën e revokimit në staging-sandbox (DB e veçantë + provider fake).

## 1. Paketa e komandave VETËM-LEXIM për prodhim (ekzekutohet nga operatori)
```bash
# Rev dhe pema
git rev-parse --short HEAD; git branch --show-current; git status --short | wc -l
# Migrimet (Enterprise pret 0032, Central pret 0029)
alembic current ; alembic -c apps/central/alembic.ini current
# Konfigurimi (vetëm emra/vlera jo-sekrete; asnjë çelës)
printenv | grep -E '^SMS_(ENV|SENDER_[A-Z_]+)=' | grep -v ACK ; printenv SMS_CP_BASE_URL | sed -E 's#(https?://[^/]+).*#\1#'
printenv SMS_SENDER_AUTHORITY_ACK | sed -E 's/^(.{8}).*/\1…/'     # vetëm prefiksi i hash-it
# Shërbimet
docker compose -f docker-compose.prod.yml ps
curl -s http://127.0.0.1:8080/readyz
# Gatishmëria (JSON, vetëm-lexim)
python -m apps.central.tools.sender_central_readiness --json > central_readiness.json
python -m scripts.sender_policy_readiness --json --target central --central-readiness central_readiness.json > readiness.json
python -m scripts.sender_authority_readiness --json ; python -m scripts.sender_alerts --json ; python -m scripts.sender_cutover status
```
### Inventari (vetëm numra; pa vlera sender)
```sql
-- Enterprise
select count(*) from sms_enterprises;
select status, count(*) from sms_sender_ids group by 1;  select country, count(*) from sms_sender_ids group by 1;  select kind, count(*) from sms_sender_ids group by 1;
select count(*) from sms_sender_ids where enterprise_id is null;
select count(*) from (select enterprise_id,country,norm_value from sms_sender_ids group by 1,2,3 having count(*)>1) d;   -- konflikte kanonike
select count(*) from sms_synced_sender_authorizations; select count(*) from sms_synced_sender_policies;
select epoch, last_seq, latest_central_seq, last_success_at, failure_count, gap_recoveries from sms_sender_sync_cursor;
select state, count(*), min(created_at) from sms_sender_request_outbox group by 1;
-- Central
select current_status, count(*) from sender_registry group by 1;  select count(*) from country_sender_policies;  select count(*) from sender_decisions;
select id, status, started_at, completed_at, sender_count, imported_count, unresolved_count from sender_bootstrap_runs order by started_at desc limit 5;
select last_seq from sender_sync_sequence;
select c.client_id, c.status, c.scopes, c.auth_generation, (select count(*) from service_client_enterprises e where e.client_pk=c.id) as enterprises,
       (select count(*) from service_keys k where k.client_pk=c.id and k.status='active') as active_keys from service_clients c;
```
Identitete të pavlefshme legacy: pjesë e dry-run-it (`invalid_legacy_identity`), jo e SQL-it.

## 2. Boshllëk i gjetur në deployment (kërkon ndryshim konfigurimi — NUK u aplikua)
`docker-compose.prod.yml` **nuk ka shërbime** për rolet `sender_control_plane` dhe `sender_request_reporter` (ka vetëm cp/money/billing/pricing). Pa to, projeksioni nuk sinkronizohet dhe kërkesat nuk dërgohen. Propozim (profile opsionale, si të tjerët; një aktiv për DB nga kyçi advisory):
```yaml
  sender-sync:
    <<: *app
    profiles: ["sender"]
    command: python -m app.worker --role sender_control_plane
    environment: { SMS_ENV: production, SMS_DATABASE_URL: "postgresql+psycopg://sms:${POSTGRES_PASSWORD}@db:5432/sms", SMS_CP_PRIVATE_KEY_PATH: /run/secrets/cp_private_key }
    volumes: ["./secrets/cp_private_key.pem:/run/secrets/cp_private_key:ro"]
    healthcheck: { test: ["CMD","python","-m","scripts.worker_health","180"], interval: 30s, start_period: 30s }
  sender-report:
    <<: *app
    profiles: ["sender"]
    command: python -m app.worker --role sender_request_reporter
    environment: { SMS_ENV: production, SMS_DATABASE_URL: "postgresql+psycopg://sms:${POSTGRES_PASSWORD}@db:5432/sms", SMS_CP_PRIVATE_KEY_PATH: /run/secrets/cp_private_key }
    volumes: ["./secrets/cp_private_key.pem:/run/secrets/cp_private_key:ro"]
    healthcheck: { test: ["CMD","python","-m","scripts.worker_health","180"], interval: 30s, start_period: 30s }
```
Kredenciali Ed25519 i klientit duhet të ketë **të dy** skopet `sender:read` dhe `sender:report` dhe enterprise-et e autorizuara (`apps.central.tools.create_service_credential --scope sender:read --scope sender:report …`).

## 3. Plani i shadow (NUK ekzekutohet pa miratim)
1. Konfigurim (`.env`): `SMS_SENDER_SYNC_ENABLED=true`, `SMS_SENDER_REQUEST_REPORTING=true`, `SMS_SENDER_AUTHORITY=local` (ende), `SMS_CP_BASE_URL=https://…`. Nis `sender-sync` dhe `sender-report` (`docker compose --profile sender up -d sender-sync sender-report`). Prit snapshot-in; `scripts.sender_sync_readiness` PASS.
2. Bootstrap sipas §6 (dry-run → zgjidhje → apply → reconcile --record).
3. `SMS_SENDER_AUTHORITY=shadow` në **`api` DHE `worker`** (fushatat dhe auto-përgjigjet thërrasin `submit` te worker-i), rinisje e radhitur (`docker compose up -d api worker`).
4. Verifikim: `python -m scripts.sender_policy_readiness --json --target central …`; `select category,count(*) from sms_sender_authority_comparisons group by 1`; mesazhet e reja kanë `sender_authority_source='local'`; `GET /v1/sender-ids` i klientit: `effective_status == status`; norma e pranim/mohimit e `submit` e pandryshuar.
5. SQL: submit +2 SELECT (+1 INSERT për mospërputhje dhe `SMS_SENDER_SHADOW_SAMPLE_PCT=10%` të përputhjeve); asnjë HTTP; `process_one` pa ndryshim.
6. Rikthim: `SMS_SENDER_AUTHORITY=local` + rinisje `api worker` (krahasimet mbeten, append-only).
**Dritarja e provës (pa numër ditësh të shpikur):** mostra ≥ `SMS_SENDER_EVIDENCE_MIN_SAMPLES` (parazgjedhje **100**, e papërcaktuar në `.env.example`) që nga përfundimi i bootstrap-it (opsionale `SMS_SENDER_EVIDENCE_WINDOW_HOURS`). **SHADOW PASS** = `sender_policy_readiness --target central` pa FAIL dhe pa WARN të pashpjeguar: drift kritik 0, çdo sender lokal i miratuar ka projeksion të miratuar (ose çështje e pranuar), sync + reporter të shëndetshëm, bootstrap i plotë, projeksion pa korrupsion. **WARN** = vetëm `expected_drift` (local_deny_central_allow), `scope_sender_read`, `central_readiness` mungon, `projection_divergence` — vazhdo vetëm me shpjegim të dokumentuar. **FAIL** = çdo kontroll FAIL.

## 4. Monitorimi
Repo-ja ka vetëm healthcheck Docker për workerat dhe mjete CLI "pull" (`sender_alerts`, readiness). **Nuk ka Prometheus/Alertmanager/pager në repo** (konfirmuar edhe nga M9). Gjendja për secilën alertë (`sender_sync_lag`, `…_gap_recoveries_total`, `sender_request_oldest_age_seconds`, `sender_request_permanent_failures`, `sender_bootstrap_unresolved`, `sender_shadow_critical_drift`, `sender_central_deny_ratio`, `sender_dispatch_recheck_blocked_1h`, `sender_projection_stale`, `sender_policy_readiness_fail`): **kodi gati; lidhja në prodhim e pa-verifikuar → "partially configured"** (nevojitet cron/Prometheus textfile që thërret `python -m scripts.sender_alerts --json` dhe alarmon në kod ≠ 0). `sender_central_deny_ratio` është numërues në proces (agregim mes proceseve nga log-et).

## 5. Backup / PITR
Në repo: `scripts/backup.sh` (pg_dump custom + checksum + rotacion + `pg_restore --list`) dhe `scripts/restore.sh`; runbook-u i restore te `M9G_BILLING.md`/`M9_MONEY_AUDIT.md`. **PITR/WAL për Central nuk është në repo dhe checklist-a M9 e shënon bllokues prodhimi.** Pa dëshmi të backup-it të fundit dhe të PITR: **EXTERNAL BLOCKER** (nuk pretendohet rikuperueshmëri).

## 6. Plani i bootstrap-it (apply; NUK ekzekutohet)
1. `python -m scripts.sender_bootstrap export --out senders.json --source-revision 8c820ff`
2. `python -m apps.central.tools.sender_import --artifact senders.json --json > dry.json` (shënim hash-et)
3. Shqyrto bllokuesit; korrigjo të dhëna legjitime; për çështje të kuptuara: `python -m scripts.sender_bootstrap resolve …` (`accepted_not_migrated` | `sender_deactivated`) — **`policy_denied` nuk anashkalohet**.
4. `python -m apps.central.tools.sender_import --artifact senders.json --apply --actor-email <admin> --ack-artifact-hash <hash> --batch-size 100 --json > apply.json`
5. Prit S2; 6. `python -m scripts.sender_bootstrap reconcile --central-report apply.json --record`; 7. verifiko `unresolved=0` (ose të pranuara sipas politikës); 8. ruaj `report_hash` (gjendja `sms_sender_bootstrap_state`).

## 7. Prova e garës së revokimit (staging-sandbox, DB e veçantë + provider fake)
Ekzekutuar mbi PG të sandbox-it (`tests/test_m10s5_dispatch.py` 20 teste + 2 prova të përkohshme të hequra pas ekzekutimit): **A** revokim i dukshëm para rikontrollit ⇒ FAILED `sender_revoked`, 0 thirrje provider, hold i liruar; **B** revokim gjatë thirrjes (pas rikontrollit) ⇒ mesazhi dërgohet (provider id `fake-1`) — gara e dokumentuar; **C** revokim pas pranimit nga provider-i ⇒ asnjë anulim; DLR `delivered` e mbyll mesazhin (capture një herë, 9.95/0); **M9**: rezultat i paqartë i provider-it pas rikontrollit të kaluar ⇒ riradhitje me të njëjtën referencë (provider idempotent), hold i pandryshuar (0.05 held), pa release/capture; testet M9-a UNKNOWN janë pjesë e regresionit të pastër. **Kufizim:** provider fake — provë me provider real mbetet EXTERNAL BLOCKER.

## 8. Plani i canary (NUK dërgohet)
Rruga normale (pa përjashtim test): kërkesë sender → Central → `cp.sender.v1` → projeksion → autorizim central → radhë → rikontroll → provider → DLR → faturim. Specifikohen nga operatori: enterprise-i i testit (me wallet të financuar), sender i miratuar në Central, marrës = numër i kontrolluar nga ne, kosto e pritur = `quote` i destinacionit (1 segment), gjendje: `queued → sending → sent → delivered`, lëvizje balance: `available −kosto`, `held +kosto` në submit, capture në DLR. Komanda (vetëm pas miratimit): `python -m scripts.sender_cutover canary … --send --key canary-1`. Ndalo nëse: mohim i papritur, `FAILED sender_*`, DLR `failed`, kosto ≠ quote, alarm critical.

## 9. Lista e bllokimit të central (çdo "jo" = BLOCKED)
shadow PASS · bootstrap i plotë · pa çështje kritike të hapura · sync dhe reporter të shëndetshëm · modeli efektiv i klientit OK · `SMS_SENDER_DISPATCH_RECHECK=true` · monitorim aktiv · ACK i vlefshëm (`sender_cutover evidence`) · canary PASS · rikthimi i provuar.

## 10. Rikthimi (validim vetëm-lexim)
`python -m scripts.sender_cutover rollback --to shadow` / `--to local` raporton `reauthorize_risk` pa ndryshuar asgjë; rikthimi në local bllokohet nëse një sender lokalisht i miratuar është refuzuar/revokuar nga Central. Rruga e sigurt: shadow → `rollback --to local --reconcile-local --actor <vetë>` → local.
