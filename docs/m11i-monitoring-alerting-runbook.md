# M11-I/F-06 — Monitorimi dhe alarmimi: gjendja, boshllëqet dhe çfarë kërkon infrastrukturë reale

**Statusi:** gatishmëria operacionale **lokale** (komanda, kodet e daljes, freskia e job-eve, backup, pragjet) është e verifikuar në repo. **Asnjë platformë monitorimi, kanal alarmesh, pager ose on-call nuk ekziston** (**PRODUCTION-ONLY / EXTERNAL EVIDENCE REQUIRED**). Prodhimi mbetet **BLOCKED**. Dokumente shoqëruese: `docs/m11i-alert-matrix.md` (alarmet), `docs/m11i-scheduled-operations-runbook.md` (job-et), `docs/m11i-observability-alerting-plan.md` (plani i F-04/M11-I, histori).

Klasat: **VERIFIED** (provuar në repo/testet), **PARTIAL**, **MISSING**, **PRODUCTION-ONLY** (vetëm infrastruktura reale e provon).

## 1. Primitivat ekzistuese të vëzhgueshmërisë (gjetur në discovery)

| Primitiva | Ku | Klasa |
|---|---|---|
| `GET /healthz` (proces i gjallë, pa DB) dhe `GET /readyz` (DB + skema në kokë, backfill-i i tenant-it; 503 me arsye të përgjithshme) | `app/main.py`, `app/core/readiness.py`, `apps/central/api/health.py`, `apps/central/core/readiness.py` | VERIFIED |
| `/nginx-health` në skaj; `/readyz` publikisht 403, vetëm nga rrjeti i brendshëm | `deploy/nginx*.conf` (F-02) | VERIFIED |
| `X-Request-ID` (nginx `$request_id` → API; pranim vetëm i sigurt) | `deploy/nginx*.conf`, `app/main.py` | PARTIAL (te përgjigjja/skaji; jo te log-et e aplikacionit) |
| Heartbeat i workerave (`SMS_WORKER_HEARTBEAT`) + `scripts.worker_health <max>` si healthcheck Docker | `app/worker.py`, `docker-compose.prod.yml` | VERIFIED |
| Radha: `GET /v1/admin/stats` (`oldest_queued_age_seconds`, `stuck_sending`, `unknown_outcome`, `dlr_problems_24h`, `delivery_rate`, `top_errors`) | `app/api/admin.py` | VERIFIED |
| CLI vetëm-lexim me kode daljeje: `queue_readiness`, `financial_ops`, `sender_alerts`, `verify_ledger`, readiness për money/pricing/billing/sender; Central `money_reconciliation`, `billing_final_readiness`, `recovery_ledger` | `scripts/`, `apps/central/tools/` | VERIFIED |
| Alarme brenda Central: `GET /admin/financial/alerts`, `/admin/billing/ops`, `/admin/operations/sender`; heartbeat `billing.run` (rresht audit) | `apps/central/api/` | VERIFIED |
| Kyçe advisory/`SKIP LOCKED` për njëkohshmëri | `app/services/*poller*.py`, `billing.py`, `messages.py`, `notifications.py` | VERIFIED |
| Backup: rreshti `OK … label=…`, `.sha256`, `.meta.json` | `scripts/backup.sh` (F-05A) | VERIFIED |
| **Të reja në F-06:** `job_runner` (kyç + timeout + status), `ops_freshness` (job FAILED/STALE/MISSING/STUCK + backup), inventari `deploy/scheduled-jobs.json` | `scripts/`, `deploy/` | VERIFIED (lokalisht) |
| Prometheus, OpenTelemetry, Sentry, Datadog, `/metrics`, log JSON, `log_format` nginx me latencë | — | MISSING (të gjetura vetëm si fjalë te `sender_alerts`/`registration_metrics`, jo si integrim) |

Variablat e mjedisit për monitorim në prodhim: **asnjë** (s'ka vendor). Për job-et: `SMS_JOB_STATE_DIR` (mjeti i ri), pragjet `SMS_*`/`CENTRAL_*` të listuara te `m11i-alert-matrix.md`.

## 2. Analiza e boshllëqeve (A–AF)

| # | Kërkesa | Gjendja | Evidenca / shënim |
|---|---|---|---|
| A | Gjallëria e API | **VERIFIED** | `/healthz` (test); probe EXTERNAL |
| B | Gatishmëria e API | **VERIFIED** | `/readyz` 200/503, arsye e përgjithshme (testuar, pa rrjedhje URL) |
| C | Gatishmëria e Central | **VERIFIED** | `/readyz` 503 pa DB/skemë (testuar) |
| D | Gjallëria e worker-ave | **VERIFIED** | heartbeat + `worker_health` (testuar: mungon/stale/i freskët/prag i gabuar) |
| E | Thellësia e radhës | PARTIAL | `stats` jep moshën, jo thellësinë si metrikë kohore; pa agregim |
| F | Mosha më e vjetër në radhë | **VERIFIED** (burimi) | `oldest_queued_age_seconds`; pragjet POLICY PROPOSAL |
| G | Backlog outbox-i | **VERIFIED** (burimi) | `sender_alerts`, `financial_ops` |
| H | Retry/dead-letter | PARTIAL | UNKNOWN/permanent failures të dukshëm; s'ka «dead-letter» si koncept i unifikuar |
| I | Norma e dështimeve të provider-it | PARTIAL | `stats.top_errors`, `delivery_rate`; pragjet PROPOSED |
| J | Dështime DLR | PARTIAL | `dlr_problems_24h`; **latenca DLR MISSING** |
| K | Dështime webhook | PARTIAL | status `failed` te outbox-i; pa agregim |
| L | Lag-u i sender sync | **VERIFIED** (burimi) | `sender_alerts` / `/admin/operations/sender`, `CENTRAL_SENDER_OPS_STALE_SECONDS=300` |
| M | Backlog miratimi/kërkesash sender | **VERIFIED** (burimi) | `sender_request_oldest_age_seconds`, `SMS_SENDER_REQUEST_ALERT_AGE_SECONDS=900` |
| N | Freskia e faturimit | **VERIFIED** | heartbeat `billing.run` + job `central-billing-run` te `ops_freshness`; `CENTRAL_BILLING_RUN_STALE_SECONDS=93600` |
| O | Freskia e rakordimit | **VERIFIED** | job `central-money-reconciliation` (15 min); kursori WARN 900 / FAIL 3600 s |
| P | Dështime gjenerimi fakture | **VERIFIED** | `billing_run` kod 1 + `/admin/billing/ops` |
| Q | Suksesi i backup-it | **VERIFIED** | `backup.sh` ≠ 0 në dështim; statusi i job-it |
| R | Mosha e backup-it | **VERIFIED** | `ops_freshness --backup-dir` (26 h; plotësia e dump-it) |
| S | Freskia e drill-it të restore | **VERIFIED** (kontrolli) | job `recovery-drill` (35 d); ekzekutimi real PRODUCTION-ONLY |
| T | Disku i DB-së | **PRODUCTION-ONLY** | AL-18 |
| U | Lidhjet e DB-së | **PRODUCTION-ONLY** | AL-19 (pool Central = 5 është konfigurim, jo metrikë) |
| V | Replikim/PITR | **PRODUCTION-ONLY** | AL-20; WAL s'është aktivizuar (F-05B) |
| W | Norma HTTP 5xx | **PRODUCTION-ONLY** | log nginx `$status`; agregimi EXTERNAL |
| X | Latenca p95/p99 | **MISSING** + PRODUCTION-ONLY | `log_format` mungon (PROPOSED) |
| Y | Dështime login/auth | PARTIAL | 401/403/429 te log-u nginx; auditi Central; agregimi EXTERNAL |
| Z | Zbulim job-i të munguar | **VERIFIED** | `ops_freshness` (MISSING/STALE/FAILED/STUCK) |
| AA | Mbrojtje nga dyfishimi/mbivendosja | **VERIFIED** | §3 te scheduled-operations runbook; `job_runner` kyç + testet |
| AB | Dështim konfigurimi/sekretesh në nisje | **VERIFIED** | F-03 (refuzim nisjeje); njoftimi PRODUCTION-ONLY (AL-22) |
| AC | Routing i alarmeve | **PRODUCTION-ONLY** | s'ka kanal |
| AD | Pronësia on-call | **PRODUCTION-ONLY** | NUK ËSHTË CAKTUAR |
| AE | Politika e eskalimit | **PRODUCTION-ONLY** | propozim te §6 |
| AF | Runbook për çdo alarm | **VERIFIED** | çdo alarm ka kolonën «Runbook»; çdo job lidhet me ID të matricës (testuar) |

## 3. Çfarë provohet lokalisht (me teste)

`tests/test_f06_operations.py`: health/readiness (+ pa rrjedhje), `X-Request-ID`, `worker_health`, inventari (çdo komandë ekziston; Central te inventari i imazhit; role workerash = compose), `--help`/import i komandave të planifikuara, `job_runner` (sukses/dështim/seri dështimesh, `--ok-codes`, **pa retry**, **mbivendosje bllokuar**, **timeout vret grupin**, pa argumente/sekrete te statusi), `ops_freshness` (OK/STALE/FAILED/MISSING/STUCK, shkallëzim backup-i, mjedise, backup i pjesshëm, gabime konfigurimi → 2), pragjet e matricës = default-et e kodit, kufijtë e `Settings` refuzojnë vlera jashtë diapazonit, AST kundër sekreteve te log-et.

## 4. Siguria e vëzhgueshmërisë

Log-et, statuset dhe daljet e mjeteve nuk duhet të përmbajnë fjalëkalime, tokena, çelësa API, sekrete provider-i, `Authorization`, URL të plota DB-je, trupa mesazhesh (përveç kur dokumentohet e sigurt) ose PII përtej nevojës operacionale. Verifikuar: asnjë `log.*` nuk merr vlerë sekret (AST), `/readyz` e përgjithshme, mjetet operacionale printojnë vetëm lloj gabimi, statusi i job-eve pa argumente. **Korrelacioni:** `X-Request-ID` (hex/të sigurt) dhe `enterprise_id` (UUID) nuk janë sekrete; kur të shtohen te log-et (POLICY PROPOSAL, §5), duhet të mbahet e njëjta rregull (asnjë fushë e lirë nga kërkesa).

## 5. Propozime që NUK u zbatuan (kërkojnë miratim; prekin aplikacionin ose skajin)

1. **Log JSON me fusha të strukturuara** (`ts`, `level`, `logger`, `request_id`, `enterprise_id`, `role`) për API dhe workers — ndryshim te konfigurimi i logging-ut të aplikacionit.
2. **`log_format` nginx** me `$request_time`, `$upstream_response_time`, `$request_id`, `$status`, `$uri` (pa query string): hap AL-16/AL-17; prek `deploy/nginx*.conf` (sipërfaqe e verifikuar në F-02, riprovohet me harness-in e skajit).
3. **Metrikë latence DLR** (dërguar → delivered) si pyetje vetëm-lexim.
4. **Endpoint metrikash** vetëm nëse zgjidhet Prometheus/OpenTelemetry (vendim infrastrukture).

## 6. Pronësia dhe eskalimi (POLICY PROPOSAL; asnjë person i caktuar)

| Roli | Përgjegjësia | Alarme |
|---|---|---|
| Operacione | API, workers, radhë, provider, sender | AL-01..04, 06, 11..13, 16, 17, 22 |
| Financa | faturim, rakordim, ledger | AL-05, 07..10 |
| DBA | backup, PITR, disk, lidhje | AL-14, 15, 18..20 |
| Siguri | auth, sekrete | AL-21 |
| Infrastrukturë | host, rrjet, TLS, disk | AL-18 |

Eskalimi (propozim): P1 pa pranim brenda 15 min → zëvendësues → lideri i inxhinierisë; P2 pa pranim brenda 60 min → pronari i radhës; P3 → tiketë. Çdo P1 hap incident me orë, shkak dhe veprim; çdo alarm financiar mbyllet vetëm pas shqyrtimit njerëzor.

## 7. Çfarë mbetet BLOCKED derisa të ketë infrastrukturë reale

- Platforma e monitorimit që thërret `ops_freshness`, readiness-et dhe probe-t; dashboard me të paktën AL-01, AL-02, AL-03, AL-14.
- Kanali i alarmeve + pager me pranues të emëruar; **provë: një P1 i provokuar arrin pranuesin brenda 5 min**.
- Planifikuesi i vërtetë (cron/systemd) me `job_runner`; `SMS_JOB_STATE_DIR` i qëndrueshëm.
- Agregimi i log-eve nginx (5xx, latencë), metrikat e hostit/PostgreSQL/WAL, mbajtja e log-eve dhe politika e aksesit.
- Verifikimi i pragjeve të propozuara në staging me trafik real; caktimi i pronarëve dhe politika e eskalimit.

## 8. Statusi

**M11-I/F-06:** LOCAL OPERATIONAL MONITORING READINESS VERIFIED — REAL MONITORING/ON-CALL INTEGRATION STILL REQUIRED. **Gatishmëria operacionale e prodhimit: BLOCKED.**
