# M11-I/F-06 — Matrica e alarmeve të prodhimit

**Statusi:** model alarmesh i përcaktuar në repo; **asnjë alarm nuk është ngritur ose provuar në infrastrukturë reale** (nuk ka sistem monitorimi, kanal alarmesh as on-call: **PRODUCTION-ONLY / EXTERNAL EVIDENCE REQUIRED**). Pragjet shënuar me `kod:` janë default-e të kodit (testohen kundrejt konfigurimit); të tjerat janë **POLICY PROPOSAL**, jo të verifikuara në staging.
Prodhimi mbetet **BLOCKED**. Ky dokument zëvendëson klasifikimin S1/S2/S3 të `m11i-observability-alerting-plan.md` me P1/P2/P3 (S1→P1, S2→P2, S3→P3); ai plan mbetet si histori e burimeve.

## 1. Ashpërsia

| Klasa | Kuptimi | Përgjigje (propozim) | Kanal |
|---|---|---|---|
| **P1** | Ndikim klienti, integritet financiar ose ndërprerje e gjerë | page menjëherë, 24/7 | pager (EXTERNAL) |
| **P2** | Shërbim i degraduar, backlog në rritje, dështim provider-i | page në orar pune / kanal brenda 30 min | kanal alarmesh (EXTERNAL) |
| **P3** | Paralajmërim, ndjekje operacionale | tiketë, ditën pasardhëse | tiketë |

Pronarët janë role të propozuara (**NUK ËSHTË CAKTUAR asnjë person/skuadër**): Operacione, Financa, DBA, Siguri, Infrastrukturë. Eskalimi dhe on-call: **PRODUCTION-ONLY** (shih `docs/m11i-monitoring-alerting-runbook.md` §6).

## 2. Matrica

Kolonat: **Sinjali** = burimi konkret; **Pragu/Dritarja** = kur ngrihet; **Rikuperimi** = kur mbyllet; **Konfigurimi** = ndryshorja me default të kodit (`kod:`) ose `—`; **Burimi** = VERIFIED (ekziston në repo) / EXTERNAL (kërkon infrastrukturë); **Lloji** = page ose tiketë.

| ID | Emri | Klasa | Sinjali | Pragu / dritarja | Rikuperimi | Runbook | Pronari | Lloji | Konfigurimi (kod) | Burimi |
|---|---|---|---|---|---|---|---|---|---|---|
| AL-01 | API Enterprise/Central jo gati | P1 | probe i brendshëm `GET /readyz` (503) + `/nginx-health` i skajit | 2 dështime radhazi çdo 30 s (≈ 1–2 min) | `/readyz` 200 për 2 probe radhazi | RB §3, §4 | Operacione | page | — | probe EXTERNAL; endpoint-et VERIFIED |
| AL-02 | Worker i munguar / i ngecur | P1 (`worker`, `webhook-worker`) · P2 (poller/reporter) | heartbeat i skedarit (`scripts.worker_health`), healthcheck Docker | `unhealthy` > 2 min (prag 90 s / 180 s × 30 s interval) | healthcheck `healthy` | RUN §5; RB §8 | Operacione | page (P1) / kanal (P2) | — | VERIFIED (healthcheck); njoftimi EXTERNAL |
| AL-03 | Mosha e radhës SMS | P2 → P1 | `GET /v1/admin/stats` → `oldest_queued_age_seconds` | > 300 s për 5 min = P2; > 900 s = P1 (dispatch jo në pauzë) | < 120 s | RUN §6; RB §6 | Operacione | page | — | VERIFIED (fusha); pragjet POLICY PROPOSAL |
| AL-04 | Backlog outbox-i (raportim/sender) | P2 | `python -m scripts.sender_alerts` (`sender_request_oldest_age_seconds`), `financial_ops` (outbox raportesh) | mosha > prag | e para < prag | M10 §4 | Operacione | kanal | `SMS_SENDER_REQUEST_ALERT_AGE_SECONDS=900` · `SMS_MONEY_REPORT_STALE_SECONDS=1800` | VERIFIED |
| AL-05 | Faturimi Central i vjetruar | P1 | heartbeat `billing.run` (rresht audit) + `GET /admin/billing/ops`; job `central-billing-run` | pa ekzekutim të suksesshëm > prag (kur autoriteti = `central`) | ekzekutim i suksesshëm | M9G; RB §5 | Financa | page | `CENTRAL_BILLING_RUN_STALE_SECONDS=93600` | VERIFIED |
| AL-06 | Dështime provider-i SMS | P2 → P1 | `stats.dlr_problems_24h`, `top_errors`, `delivery_rate` | delivery_rate < 90% për 30 min (≥ 20 mesazhe) = P2; kod autentikimi/rajoni `twilio_2xxxx` = P1 | normalizim 30 min | RB §6; `docs/TWILIO.md` | Operacione | page (P1) | — | VERIFIED (fusha); pragjet POLICY PROPOSAL |
| AL-07 | UNKNOWN / SENDING i ngecur | P1 | `scripts.queue_readiness` (FAIL), `stats.stuck_sending`, `stats.unknown_outcome` | FAIL (UNKNOWN > 24 h) = P1; WARN (UNKNOWN > 1 h) = P2 | `queue_readiness` PASS | RB §6; M9C §2 | Financa + Operacione | page | `SMS_FINANCIAL_UNKNOWN_WARN_SECONDS=3600` | VERIFIED |
| AL-08 | Mospërputhje financiare Central↔Enterprise | P1 | `apps.central.tools.money_reconciliation` (FAIL/CRITICAL), `GET /admin/financial/alerts`; kursori i parave | CRITICAL = P1; kursori WARN 900 s / FAIL 3600 s | rakordim PASS | M9C §6, §8 | Financa | page | `CENTRAL_MONEY_CURSOR_STALE_WARN_SECONDS=900` · `CENTRAL_MONEY_CURSOR_STALE_FAIL_SECONDS=3600` | VERIFIED |
| AL-09 | Alarm financiar Enterprise | P1 | `python -m scripts.financial_ops --json` (CRITICAL, kod 1) | çdo CRITICAL | `financial_ops` kod 0 | M9C | Financa | page | — | VERIFIED |
| AL-10 | Libri nuk mbyllet | P1 | `scripts.verify_ledger` (Enterprise) · `apps.central.tools.recovery_ledger` (Central) | çdo dështim (1 ekzekutim) | PASS në ekzekutimin e radhës + shqyrtim | RB §5 | Financa + DBA | page | — | VERIFIED |
| AL-11 | Sender sync / qeverisje | P2 (P1 pas cutover-it `central`) | `python -m scripts.sender_alerts` (critical), `GET /admin/operations/sender` | `sender_sync_lag`, `sender_projection_stale`, `sender_shadow_critical_drift` kritik | alertat bosh | M10 §4 | Operacione | kanal | `CENTRAL_SENDER_OPS_STALE_SECONDS=300` | VERIFIED |
| AL-12 | Outbox njoftimesh Central | P2 | job `central-send-notifications` (freskia) | pa sukses > 5 min | sukses i ri | M8 (regjistrim) | Operacione | kanal | — | VERIFIED (job + freskia); njoftimi EXTERNAL |
| AL-13 | Dështime DLR / webhook | P2 | `stats.dlr_problems_24h`; mesazhe pa DLR pas afatit; webhook `failed` | çdo mesazh pa DLR pas afatit; rritje 3× kundrejt bazës | normalizim | RB §6 | Operacione | kanal | `SMS_DLR_TIMEOUT_HOURS=72` | PARTIAL: latenca e DLR s'ka metrikë (NOT VERIFIED) |
| AL-14 | Backup i vjetër/i dështuar | P2 → P1 | `scripts.ops_freshness --backup-dir` (job `backup-enterprise`/`backup-central`) | pa backup të plotë > 26 h = P2; 2 dështime radhazi = P1 | backup i ri i plotë | `m11i-postgres-backup-restore-runbook.md` | DBA | page (P1) | — | VERIFIED (kontrolli); njoftimi EXTERNAL |
| AL-15 | Drill restore i vjetruar | P3 | job `recovery-drill` (host restore) | pa sukses > 35 ditë | drill i suksesshëm | `m11i-postgres-backup-restore-runbook.md` §5 | DBA | tiketë | — | VERIFIED (kontrolli); ekzekutimi real EXTERNAL |
| AL-16 | Norma 5xx | P1 / P2 | log nginx `$status` (agregim) | ≥ 5% për 5 min = P1; ≥ 1% për 15 min = P2 | < 0.5% për 15 min | RB §1, §3 | Operacione | page (P1) | — | **PRODUCTION-ONLY** (agregimi); `log_format` = POLICY PROPOSAL |
| AL-17 | Latenca p95/p99 | P2 | log nginx `$request_time` | p95 > 1 s për 10 min | p95 < 500 ms | RUN §6 | Operacione | kanal | — | **PRODUCTION-ONLY**; `log_format` mungon (NOT VERIFIED) |
| AL-18 | Disku i DB-së | P3 / P2 / P1 | `df`/metrikë storage | < 30% e lirë = P3; < 20% = P2; < 10% = P1 | > 35% | RUN §6 | DBA/Infrastrukturë | page (P1) | — | **PRODUCTION-ONLY** |
| AL-19 | Lidhjet e DB-së | P2 / P1 | `pg_stat_activity`, `pg_isready`; Central `db_pool_size=5` | > 80% `max_connections` për 10 min = P2; DB e paarritshme = P1 | < 60% | RB §5 | DBA | page (P1) | — | **PRODUCTION-ONLY** |
| AL-20 | WAL/PITR i prishur | P1 | `pg_stat_archiver.failed_count` / `last_archived_time`; alarmi i provider-it | arkivimi dështon ose vonon > 15 min | arkivim normal + zinxhir i plotë | `m11i-postgres-pitr-runbook.md` | DBA | page | — | **PRODUCTION-ONLY** (WAL s'është aktivizuar) |
| AL-21 | Dështime autentikimi | P2 | 401/403/429 në log nginx; auditi Central i login-eve | > 20 përgjigje 429 në 5 min nga IP të ndryshme (shenjë `TRUSTED_PROXY_HOPS` i gabuar) | normalizim | RB §7; `m11i-secrets-and-access-plan.md` | Siguri | kanal | — | PARTIAL (statusi në log; agregimi EXTERNAL) |
| AL-22 | Dështim nisjeje / konfigurimi / sekretesh | P1 | dalje ≠ 0 e procesit në nisje (gate-t F-03: sekret i dobët/i munguar); healthcheck `unhealthy` menjëherë pas deploy | çdo kontejner që del në nisje ose restart-loop | proces i qëndrueshëm | RB §3; `m11i-secrets-and-access-plan.md` | Operacione | page | — | VERIFIED (refuzimi i nisjes, F-03); njoftimi EXTERNAL |
| AL-23 | Job i planifikuar i munguar/dështuar | sipas inventarit | `python -m scripts.ops_freshness` (FAILED / STALE / MISSING / STUCK) | sipas `max_age_seconds` të inventarit `deploy/scheduled-jobs.json` | statusi i ri `success` | `docs/m11i-scheduled-operations-runbook.md` | pronari i job-it | sipas klasës së job-it | — | VERIFIED |
| AL-24 | Kredenciale/leje provider-i e refuzuar | P1 | `provider_safety.twilio_auth_failures_24h` te `GET /v1/admin/stats`; `provider_readiness check --phase post` (`provider_auth_failures`); `top_errors` (`twilio_auth`, `twilio_forbidden`, `twilio_200xx`, `http_401/403`) | ≥ 1 dështim auth/leje (propozim; që nga M11-J FAILED menjëherë për çdo mesazh) | kodet e zhdukura + `provider_readiness check` = GO | `m11i-provider-failure-matrix.md` §1–2 | Operacione | page | — | VERIFIED (fushat, lokalisht); pragu POLICY PROPOSAL; **pauzo `dispatch`** pasi çdo mesazh dështon menjëherë |
| AL-25 | ID provider-i e dyfishtë / mospërputhje rakordimi | P2 | `provider_safety.duplicate_provider_id` te `/v1/admin/stats`; `provider_readiness` (`duplicate_provider_id_conflicts`, `sent_without_dlr`, `dlr_problems`) | ≥ 1 mesazh UNKNOWN `duplicate_provider_id` | rakordim i shpjeguar | `m11i-provider-dlr-runbook.md` §6, G-01 | Operacione | kanal | — | VERIFIED (lokalisht); planifikimi real PRODUCTION-ONLY |
| AL-26 | Provider i kufizuar me backlog | P3 | `provider_safety.throttle.queued_due` te `/v1/admin/stats`; `provider_readiness` (`queue_backlog`, `throttle_config`); log `provider throttled` | `queued_due` në rritje të vazhdueshme (propozim) | backlog që bie | `m11i-provider-dlr-runbook.md` §9 | Operacione | tiketë | — | VERIFIED (fushat, lokalisht); kalibrimi i kufijve PRODUCTION-ONLY |

Shkurtesat e runbook-eve: RB = `m11i-production-rollback-runbook.md`, RUN = `RUNBOOK.md`, M9C = `M9_PRODUCTION_CHECKLIST.md`, M9G = `M9G_BILLING.md`, M10 = `M10_PROD_OPERATIONS.md`.

## 3. Rregulla të përbashkëta

- **Page vs tiketë:** vetëm P1 (dhe P2 gjatë orarit) kërkon pranues njeri; P3 është tiketë. Asnjë alarm i ri nuk shtohet pa pronar, prag, kusht rikuperimi dhe runbook.
- **Histerezë:** një alarm mbyllet vetëm pas kushtit të rikuperimit (kolona «Rikuperimi»), jo me një kampion të mirë të vetëm.
- **Alarmet financiare (AL-05, AL-07..AL-10) nuk mbyllen automatikisht pa shqyrtim njeriu**; UNKNOWN nuk lirohet kurrë nga monitorimi (vetëm stafi ose DLR autoritativ).
- **Pragjet e propozuara** duhet të verifikohen në staging me trafik real para se të konsiderohen faktike.

## 4. Çfarë kërkon infrastrukturë reale (PRODUCTION-ONLY)

Platforma e monitorimit që thërret komandat/probe-t më sipër, agregimi i log-eve nginx (AL-16/17), metrikat e hostit dhe të PostgreSQL (AL-18..20), kanali i alarmeve, pager-i, on-call me pronarë të emëruar dhe provë e një alarmi P1 të provokuar që arrin pranuesin brenda 5 min. Shih `docs/m11i-monitoring-alerting-runbook.md` §7.
