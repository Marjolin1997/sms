# M11-I — Plani i monitorimit dhe alarmeve

**Revision i bazës:** `6020026`. Plan; **asgjë nuk është ngritur**. Statusi: **BLOCKED** (nuk ka sistem monitorimi, kanal alarmesh as on-call; **EXTERNAL EVIDENCE REQUIRED**).

## 0. Gjendja e verifikuar

| Çka ekziston në repo | Klasifikim |
|---|---|
| Healthcheck Docker: API `/readyz`, workers me heartbeat (`scripts.worker_health`, prag 90 s për `worker`/`webhook-worker`, 180 s për workerat CP/sender) | VERIFIED |
| `GET /healthz`, `GET /readyz` për Enterprise dhe Central; `X-Request-ID` në çdo përgjigje | VERIFIED |
| `GET /v1/admin/stats` (Enterprise) me `oldest_queued_age_seconds`, `stuck_sending`, `unknown_outcome`, `dlr_problems_24h` | VERIFIED (kodi `app/api/admin.py`) |
| CLI vetëm-lexim: `scripts.queue_readiness`, `scripts.financial_ops`, `scripts.sender_alerts`, readiness për money/pricing/billing/sender (kode dalje 0/1/2) | VERIFIED |
| Alarme të llogaritura brenda aplikacionit te Central: `GET /admin/financial/alerts`, `GET /admin/billing/ops`, `GET /admin/operations/sender` | VERIFIED (nga docs M9/M10/M11-D2) |
| Prometheus, Alertmanager, pager, dashboard, agregim log-esh | NOT VERIFIED — mungojnë në repo (`docs/M10_PROD_OPERATIONS.md` §4 e konfirmon) |
| Formati i log-ut nginx me `$request_time` (latencë) | NOT VERIFIED — `deploy/nginx.conf` nuk përcakton `log_format` |
| Log-e të strukturuara (JSON) | NOT VERIFIED — workers përdorin `logging.basicConfig(level=INFO)` |

## 1. Ashpërsia dhe përgjigjja (të gjitha PROPOSED)

| Niveli | Kuptimi | Kohë përgjigjeje | Kanal |
|---|---|---|---|
| S1 | Ndërprerje ose rrezik financiar/të dhënash | Menjëherë (24/7) | Pager (EXTERNAL) |
| S2 | Degradim i shërbimit ose i një rrjedhe kyçe | < 30 min në orar pune | Kanal alarmesh (EXTERNAL) |
| S3 | Paralajmërim, tendencë | Ditën pasardhëse | Tiketë |

Pronarët janë **NUK ËSHTË CAKTUAR** (role të propozuara: Operacione, DBA, Siguri, Financa). Lidhjet e reagimit janë seksione të `m11i-production-rollback-runbook.md` (RB), `docs/RUNBOOK.md` (RUN), `docs/M10_PROD_OPERATIONS.md` (M10), `docs/M9G_BILLING.md` (M9G), `docs/M9_PRODUCTION_CHECKLIST.md` (M9C).

## 2. Alarmet

Çdo prag është **PROPOSED**, përveç ku shënohet **VERIFIED** (default i kodit ose i konfigurimit). Burimi 'EXT' = kërkon sistem të jashtëm që nuk ekziston në repo.

### 2.1 API dhe gatishmëria

| # | Sinjali | Burimi | Pragu | Ashpërsia | Pronari | Reagimi / runbook | Klas. e burimit |
|---|---|---|---|---|---|---|---|
| A-01 | Norma 5xx e API-së | Log nginx (`$status`; default `combined` e ka) — agregimi EXT | ≥ 5% për 5 min → S1; ≥ 1% për 15 min → S2 | S1/S2 | Operacione | RB §1 RT-03; RB §3 | PARTIALLY VERIFIED (statusi në log; agregimi EXT) |
| A-02 | Latenca e API-së (p95) | Log nginx me `$request_time` (PROPOSED: shtohet `log_format`) | p95 > 1 s për 10 min → S2 (M11-H: faqet interaktive 11–200 ms në PG sintetike) | S2 | Operacione | RUN §6; hetim me `X-Request-ID` | NOT VERIFIED (konfigurimi mungon) |
| A-03 | `/readyz` ≠ ready | Probe e jashtme çdo 30 s në `/readyz` (Enterprise dhe Central) | 2 dështime radhazi → S1 | S1 | Operacione | RB §3, §4 | VERIFIED (endpoint); probe EXT |
| A-04 | Rritje e 429 (lockout) | Log nginx 429 | > 20 përgjigje 429 në 5 min nga IP të ndryshme → S2 (shenjë e `TRUSTED_PROXY_HOPS` të gabuar) | S2 | Siguri | RB §7; `m11i-secrets-and-access-plan.md` | PARTIALLY VERIFIED |

### 2.2 Workers, radhë, UNKNOWN

| # | Sinjali | Burimi | Pragu | Ashpërsia | Pronari | Reagimi / runbook | Klas. e burimit |
|---|---|---|---|---|---|---|---|
| W-01 | Heartbeat i workerit | Docker health (`scripts.worker_health 90/180`) | unhealthy > 2 min → S1 për `worker`/`webhook-worker`; S2 për workerat CP/sender | S1/S2 | Operacione | RB §8; RUN §5 | VERIFIED (healthcheck); alarmimi EXT |
| W-02 | Mosha e mesazhit më të vjetër në radhë | `GET /v1/admin/stats` → `oldest_queued_age_seconds` | > 300 s → S2; > 900 s → S1 (kur `dispatch` nuk është në pauzë) | S1/S2 | Operacione | RUN §6; RB §6 | VERIFIED (fusha); pragjet PROPOSED |
| W-03 | Mesazhe SENDING të ngecur | `stats.stuck_sending`; `scripts.queue_readiness` | > 0 për > 10 min → S2; `queue_readiness` FAIL → S1 | S1/S2 | Operacione | RB §6; M9 (queue readiness) | VERIFIED (mjetet) |
| W-04 | Rezultate UNKNOWN | `stats.unknown_outcome`; `SMS_FINANCIAL_UNKNOWN_WARN_SECONDS` | kufiri WARN default 3600 s (VERIFIED); `queue_readiness` default WARN 3600 s / FAIL 86400 s (VERIFIED); për S1: UNKNOWN > 1 orë (PROPOSED) | S1 | Financa + Operacione | RB §6; M9C §2 | VERIFIED (default-et) |
| W-05 | Outbox-et e raportimit (sender/money/billing) | `scripts.sender_alerts`: `sender_request_oldest_age_seconds`, `sender_request_permanent_failures`; `financial_ops` | `SMS_SENDER_REQUEST_ALERT_AGE_SECONDS` default 900 s (VERIFIED) | S2 | Operacione | M10 §4 | VERIFIED |

### 2.3 SMS, DLR, provider

| # | Sinjali | Burimi | Pragu | Ashpërsia | Pronari | Reagimi / runbook | Klas. e burimit |
|---|---|---|---|---|---|---|---|
| S-01 | Norma e dështimeve SMS | `stats` (`top_errors`, `delivery_rate` për provider); numërim `failed` | delivery_rate < 90% për 30 min dhe ≥ 20 mesazhe → S2 | S2 | Operacione | RB §6; `docs/TWILIO.md` (kodet) | PARTIALLY VERIFIED (fushat ekzistojnë; pragjet PROPOSED) |
| S-02 | Latenca e DLR (dërguar → delivered/undelivered) | Llogaritje nga timestamps të mesazheve (NUK ka metrikë gati) | p95 > 5 min → S2; çdo mesazh pa DLR pas `SMS_DLR_TIMEOUT_HOURS` (default 72 h, VERIFIED) → S2 | S2 | Operacione | RB §6 | NOT VERIFIED (metrika s'ekziston; kërkon pyetje të re vetëm-lexim) |
| S-03 | Gabime provider-i | `stats.dlr_problems_24h`; kodet e gabimit (p.sh. `twilio_21608`, `twilio_21408`) | çdo `twilio_2xxxx` autentikimi/rajoni → S1; rritje 3× kundrejt bazës → S2 | S1/S2 | Operacione | RB §6; `docs/TWILIO.md` | VERIFIED (fusha); pragjet PROPOSED |

### 2.4 Faturim dhe financë

| # | Sinjali | Burimi | Pragu | Ashpërsia | Pronari | Reagimi / runbook | Klas. e burimit |
|---|---|---|---|---|---|---|---|
| F-01 | Periudha të faturimit të pa-përpunuara / rillogaritje në pritje | Central `GET /admin/billing/ops` → `periods.due_unprocessed`; heartbeat i `billing_run`; `CENTRAL_BILLING_RUN_STALE_SECONDS` (default 93600 s, VERIFIED) | `due_unprocessed` > 0 për > 2 orë → S2; heartbeat > 93600 s → S1 | S1/S2 | Financa | M9G §rikthim; RB §8 | PARTIALLY VERIFIED (terminologjia 'pending recalculation' NUK gjendet në repo; mapuar te `due_unprocessed`) |
| F-02 | Rakordimi financiar | `python -m apps.central.tools.money_reconciliation --strict`; `GET /admin/financial/alerts` | çdo CRITICAL → S1; kursori money: WARN 900 s / CRITICAL 3600 s; reversal i pazgjidhur CRITICAL 3600 s (default-et VERIFIED) | S1/S2 | Financa | M9C §6, §8 | VERIFIED |
| F-03 | Integriteti i librit | `python -m scripts.verify_ledger` (çdo orë ditën 1, pastaj çdo natë — PROPOSED) | FAIL → S1 | S1 | Financa + DBA | RB §5 | VERIFIED (mjeti) |
| F-04 | Raportet e përdorimit të freskëta | `CENTRAL_MONEY_REPORT_FRESH_SECONDS` 600 s / STALE 1800 s (VERIFIED) | stale → S2 | S2 | Financa | M9C §6 | VERIFIED |

### 2.5 Sender dhe Central

| # | Sinjali | Burimi | Pragu | Ashpërsia | Pronari | Reagimi / runbook | Klas. e burimit |
|---|---|---|---|---|---|---|---|
| C-01 | Gjendja operative sender | `python -m scripts.sender_alerts --json`; Central `GET /admin/operations/sender`; `CENTRAL_SENDER_OPS_STALE_SECONDS` 300 s (VERIFIED) | `sender_sync_lag`, `sender_projection_stale`, `sender_shadow_critical_drift` kritik → S2; pas cutover-it `central` → S1 | S1/S2 | Operacione | M10 §4; RB §8 | VERIFIED |
| C-02 | Procesi i njoftimeve Central | `send_notifications` heartbeat/log | jo i ekzekutuar > 5 min (`--loop 15 s`) → S2 | S2 | Operacione | M8 (registration) | PARTIALLY VERIFIED (mjeti ekziston; monitorimi EXT) |

### 2.6 Infrastruktura (të gjitha EXT)

| # | Sinjali | Burimi | Pragu | Ashpërsia | Pronari | Reagimi / runbook | Klas. e burimit |
|---|---|---|---|---|---|---|---|
| I-01 | Shëndeti i DB-së dhe lidhjet | `pg_stat_activity`, `pg_isready` (EXT); Central: `db_pool_size`=5, `db_statement_timeout_ms`=30000 (VERIFIED) | lidhje > 80% të `max_connections` për 10 min → S2; DB e paarritshme → S1 | S1/S2 | DBA | RB §5 | EXTERNAL EVIDENCE REQUIRED |
| I-02 | Disk | `df` në host (EXT) | < 30% e lirë → S3; < 15% → S2; < 5% → S1 | S1–S3 | Infrastruktura | RUN §6 (disku i DB-së) | EXTERNAL EVIDENCE REQUIRED |
| I-03 | Memorie dhe swap | `free`/cgroup (EXT) | swap në përdorim të vazhdueshëm > 10 min → S2; OOM kill → S1 | S1/S2 | Infrastruktura | RB §3 | EXTERNAL EVIDENCE REQUIRED |
| I-04 | Skadenca e certifikatës | Probe TLS (EXT) | < 21 ditë → S3; < 7 ditë → S2; < 48 orë → S1 | S1–S3 | Infrastruktura | RB §7 | EXTERNAL EVIDENCE REQUIRED |
| I-05 | Suksesi i backup-it | Rreshti `OK …` i `scripts/backup.sh` në `/var/log/sms-backup.log` (cron, RUN §4); `.sha256` i krijuar | pa `OK` brenda 26 orësh → S2; 2 dështime radhazi → S1 | S1/S2 | DBA | RUN §4; RB §5 | VERIFIED (skripti dhe log-u i planifikuar); alarmimi EXT |
| I-06 | Restore i provuar | Rezultati i restore mujor (RUN §4) | pa provë > 35 ditë → S3 | S3 | DBA | RUN §4 | PROPOSED |

## 3. Kërkesat që duhen përmbushur para prodhimit

| # | Kërkesa | Provë e pranimit | Klas. |
|---|---|---|---|
| M-1 | Platformë monitorimi e zgjedhur dhe e mbërritshme nga operatorët | Dashboard me të paktën A-03, W-01, W-02, I-05 | EXTERNAL EVIDENCE REQUIRED |
| M-2 | Kanal alarmesh dhe pager me pranues të emëruar për S1 | Test: një S1 i provokuar arrin pranuesin brenda 5 min | EXTERNAL EVIDENCE REQUIRED |
| M-3 | Format log-u nginx me `$request_time`, `$status`, `$request_id` (F-04 i ardhshëm) | Mostër log-u | NOT VERIFIED |
| M-4 | Ekzekutim i planifikuar i `queue_readiness`, `financial_ops`, `sender_alerts`, `verify_ledger` me dalje te sistemi i alarmeve | Alarm i provokuar nga `queue_readiness` FAIL | NOT VERIFIED |
| M-5 | Pyetje vetëm-lexim për latencën e DLR (S-02) | Rezultat i rishikuar nga pronari | NOT VERIFIED |
| M-6 | Mbajtje log-esh dhe qasje (kush i lexon) | Politikë e shkruar | EXTERNAL EVIDENCE REQUIRED |

## 4. Verdikti

**BLOCKED**: burimet e të dhënave për pjesën më të madhe të alarmeve ekzistojnë si mjete vetëm-lexim, por asnjë sistem nuk i mbledh, nuk i vlerëson dhe nuk njofton njeri. Pragjet më sipër janë propozime derisa të verifikohen në staging.
