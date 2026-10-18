# M11-I/F-06 — Operacionet e planifikuara: inventari, kyçet dhe monitorimi

**Statusi:** inventar i verifikuar në repo dhe mjete lokale të provuara (`scripts/job_runner.py`, `scripts/ops_freshness.py`, `deploy/scheduled-jobs.json`). **Asnjë cron/systemd timer nuk është ngritur në prodhim** (nuk ka host prodhimi): planifikuesi, alarmimi dhe on-call janë **PRODUCTION-ONLY**. Prodhimi mbetet **BLOCKED**.

Burimi i vetëm i së vërtetës është `deploy/scheduled-jobs.json` (testohet te `tests/test_f06_operations.py`: çdo komandë ekziston, çdo shërbim ekziston në compose, çdo job është dokumentuar këtu). Asnjë komandë nuk është shpikur: çfarë mungon në kod është shënuar **MISSING** në §6.

## 1. Si ekzekutohet një job (vendor-neutral)

Repo-ja nuk ka planifikues (Central e thotë shprehimisht: «s'ka scheduler»). Pritet cron/systemd i hostit (ose planifikuesi i platformës) që thërret **gjithmonë** përmes mbështjellësit:

```bash
python -m scripts.job_runner --name queue-readiness --timeout 120 --state-dir /var/lib/sms-jobs -- python -m scripts.queue_readiness
```

`job_runner` jep (lokalisht, pa varësi të jashtme): kyç `flock` jo-bllokues (dy ekzekutime të të njëjtit job nuk mbivendosen në hostin, dalje 75), timeout që vret gjithë grupin e proceseve (dalje 124), **pa retry** të brendshëm, dhe skedar statusi `<state-dir>/<job>.json` (pa komandë/argumente/mjedis). Kyçi i hostit **nuk zëvendëson** mbrojtjen e vetë job-it në DB (kyç advisory / `FOR UPDATE`), që mbron edhe nga dy host-e.

Kontrolli i freskisë (vetëm-lexim; gjithashtu planifikohet, p.sh. çdo 5 min, dhe dalja e tij shkon te sistemi i alarmeve):

```bash
python -m scripts.ops_freshness --state-dir /var/lib/sms-jobs --backup-dir /var/backups/sms --json
# kod 0 = në rregull · 1 = ka FAIL (ose WARN me --strict) · 2 = gabim konfigurimi/inventari
```

Crontab shembull (JO i ngritur këtu; `<…>` plotësohen në prodhim):

```cron
*/5 * * * *  cd /opt/sms && python -m scripts.job_runner --name queue-readiness --timeout 120 -- python -m scripts.queue_readiness
*/5 * * * *  cd /opt/sms && python -m scripts.job_runner --name financial-ops   --timeout 120 -- python -m scripts.financial_ops --json
15 2 * * *   cd /opt/sms && python -m scripts.job_runner --name verify-ledger   --timeout 1800 -- python -m scripts.verify_ledger
30 2 * * *   cd /opt/sms && BACKUP_LABEL=enterprise DATABASE_URL=<…> BACKUP_DIR=<…> python -m scripts.job_runner --name backup-enterprise --timeout 3600 -- scripts/backup.sh
10 3 * * *   cd /opt/sms && python -m scripts.job_runner --name central-billing-run --timeout 1800 -- docker compose -f docker-compose.prod.yml --env-file .env --profile central-jobs run --rm central-billing-run
```

Shënime: (1) `docker compose run --rm` përcjell SIGTERM te kontejneri; një timeout që vret klientin mund të lërë kontejnerin deri sa ta ndalë Docker-i — preferohet `timeout` të jetë më i gjatë se kohëzgjatja normale. (2) Skedari i statusit shkruhet te `SMS_JOB_STATE_DIR` (jo implicit: pa të, dalje 2).

## 2. Inventari i job-eve të planifikuara

Për secilin: nënsistemi, komanda, kadenca, kyçi, idempotenca, koha e pritur, timeout, sjellja në dështim, retry, ndryshoret, alarmi për të munguar/dështuar, pronari. Klasat P1/P2/P3 sipas `m11i-alert-matrix.md`. «Munguar» = `max_age_seconds` i tejkaluar; «dështuar» = ekzekutimi i fundit ≠ sukses.

| Job | Nënsistemi | Komanda | Kadenca (cron) | Timeout / max-age | Kyçi / mbivendosja | Idempotenca | Koha e pritur | Dështim (sjellje) | Ndryshoret | Munguar / dështuar | Pronari |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `queue-readiness` | Enterprise radha | `python -m scripts.queue_readiness` | `*/5 * * * *` | 120 s / 900 s | vetëm-lexim | vetëm-lexim | < 5 s | kod 1 = FAIL (UNKNOWN/SENDING i ngecur) | `SMS_DATABASE_URL` | P2 / P1 | Operacione |
| `financial-ops` | Enterprise financa | `python -m scripts.financial_ops --json` | `*/5 * * * *` | 120 s / 900 s | vetëm-lexim | vetëm-lexim | < 5 s | kod 1 = CRITICAL | `SMS_DATABASE_URL` | P2 / P1 | Financa |
| `sender-alerts` | Enterprise sender | `python -m scripts.sender_alerts --json` | `*/5 * * * *` | 120 s / 900 s | vetëm-lexim | vetëm-lexim | < 5 s | kod 1 = alertë critical | `SMS_DATABASE_URL` | P2 / P2 | Operacione |
| `verify-ledger` | Enterprise financa | `python -m scripts.verify_ledger` | `15 2 * * *` | 1800 s / 93600 s | vetëm-lexim | vetëm-lexim | sekonda–minuta | kod 1 = ledger nuk mbyllet | `SMS_DATABASE_URL` | P2 / P1 | Financa + DBA |
| `backup-enterprise` | Backup | `scripts/backup.sh` (`BACKUP_LABEL=enterprise`) | `30 2 * * *` | 3600 s / 93600 s | `flock` i runner-it; pg_dump transaksional | skedar i ri me UTC; rotacion për etiketë | minuta | dalje ≠ 0, asnjë artefakt i gjysmë | `BACKUP_LABEL`, `DATABASE_URL`, `BACKUP_DIR`, `KEEP_DAYS` | P2 / P2 (P1 pas 2 dështimesh) | DBA |
| `backup-central` | Backup | `scripts/backup.sh` (`BACKUP_LABEL=central`) | `45 2 * * *` | 3600 s / 93600 s | njësoj | njësoj | minuta | njësoj | njësoj | P2 / P2 (P1 pas 2) | DBA |
| `recovery-drill` | Backup (host restore) | `python -m scripts.recovery_drill` | `0 4 1 * *` | 3600 s / 3024000 s (35 d) | `flock`; vetëm baza `f05drill_*` të veta | krijon/fshin bazat e veta | ~25 s lokalisht | kod 1 = drill dështoi (baza e përkohshme fshihet) | `ADMIN_URL` (JO baza e prodhimit) | P3 / P3 | DBA |
| `central-billing-run` | Central faturimi | `… run --rm central-billing-run` (= `apps.central.tools.billing_run --json`) | `10 3 * * *` | 1800 s / 93600 s | kyç advisory `RUN_LOCK_KEY` (kod 4 nëse mbivendoset) + `FOR UPDATE` i abonimit | **idempotent**: një periudhë/faturë për (abonim, indeks) | sekonda–minuta | kod 1 = periudha `failed`; kod 3 = autoriteti ≠ `central` (pritet para cutover-it) | `CENTRAL_DATABASE_URL`, `CENTRAL_BILLING_RUN_STALE_SECONDS` | P2 / P1 (opsional para cutover-it) | Financa |
| `central-send-notifications` | Central regjistrimi | `… run --rm central-send-notifications` (= `send_notifications`, një kalim) | `* * * * *` | 120 s / 300 s | `FOR UPDATE SKIP LOCKED` | retry i kufizuar (5, backoff 1m·4^n), pa dërgim të dyfishtë | < 30 s | kod 2 = konfigurim i munguar | `CENTRAL_DATABASE_URL`, `CENTRAL_MAILER`, `CENTRAL_REGISTRATION_VERIFY_KEY` | P2 / P2 | Operacione |
| `central-money-reconciliation` | Central financa | `… run --rm central-tool apps.central.tools.money_reconciliation` | `*/15 * * * *` | 300 s / 2700 s | vetëm-lexim | vetëm-lexim; asnjë korrigjim automatik | sekonda | kod 1 = FAIL/CRITICAL | `CENTRAL_DATABASE_URL`, `CENTRAL_MONEY_*` | P2 / P1 | Financa |
| `central-ledger-integrity` | Central financa | `… run --rm central-tool apps.central.tools.recovery_ledger` | `30 2 * * *` | 1800 s / 93600 s | vetëm-lexim | vetëm-lexim | sekonda | kod 1 = ledger/sekuencë e paqëndrueshme | `CENTRAL_DATABASE_URL` | P2 / P1 | Financa + DBA |

Shërbimet që punojnë vazhdimisht (jo cron; `kind: service` në inventar): mbulohen nga healthcheck-u Docker `scripts.worker_health`, jo nga `ops_freshness`.

| Shërbimi (compose) | Procesi | Healthcheck (heartbeat max) | Kyçi / paralelizmi | Pronari | Alarmi |
|---|---|---|---|---|---|
| `worker` | `python -m app.worker` (SMS/email, sweep UNKNOWN/SENDING) | `worker_health 90` | `FOR UPDATE SKIP LOCKED` ⇒ replika të shumta të sigurta | Operacione | P1 (AL-02) |
| `webhook-worker` | `--role webhooks` (+ purge i ngjarjeve çdo orë, `SMS_EVENT_RETENTION_DAYS`=30) | `worker_health 90` | `SKIP LOCKED` | Operacione | P1 |
| `cp-sync` | `--role control_plane` | `worker_health 180` | kyç advisory (një aktiv për DB) | Operacione | P2 |
| `money-sync` | `--role money_control_plane` | `worker_health 180` | kyç advisory i parave | Operacione | P2 |
| `money-report` | `--role money_usage_reporter` | `worker_health 180` | kyç advisory i raportuesit | Operacione | P2 |
| `billing-report` | `--role billing_usage_reporter` | `worker_health 180` | kyç advisory i raportuesit | Operacione | P2 |
| `pricing-sync` | `--role pricing_control_plane` | `worker_health 180` | kyç advisory i çmimeve | Operacione | P2 |
| `sender-sync` | `--role sender_control_plane` | `worker_health 180` | kyç advisory i sender-ave | Operacione | P2 |
| `sender-report` | `--role sender_request_reporter` | `worker_health 180` | kyç advisory i outbox-it | Operacione | P2 |

## 3. Siguria e planifikimit (verifikim, nuk është ndryshuar logjikë)

Gjetjet nga leximi i kodit dhe testet. **Asnjë job financiar ose sender nuk ka mbivendosje të pambrojtur** (nuk u ndal asnjë ndryshim logjike).

| Job / shërbim | Mbrojtja nga ekzekutimi i dyfishtë / mbivendosja | Efekt financiar i dyfishtë? |
|---|---|---|
| `central-billing-run` | kyç advisory i sesionit (`pg_try_advisory_lock`, kod 4) + `FOR UPDATE` i abonimit + `process_period` idempotent | **jo**: një periudhë/faturë për (abonim, indeks); rirunimi s'krijon të dytën |
| pollers Enterprise (`cp`, `money`, `pricing`, `sender`) | `PollerLock`: kyç advisory i sesionit me çelës të veçantë; aplikuesi serializon vetë kursorin (kyçi shmang vetëm kërkesa të dyfishta) | **jo**: aplikim idempotent sipas kursorit/seq |
| reporters (`money-report`, `billing-report`, `sender-report`) | kyç advisory me çelës të veçantë; në Central ingest idempotent (append-only, çelës unik) | **jo** |
| `worker` / `webhook-worker` | `FOR UPDATE SKIP LOCKED`; UNKNOWN/SENDING s'lirohet automatikisht; provider pa idempotency ⇒ UNKNOWN (proces njerëzor) | **jo** për parat (hold/ledger me çelës idempotence); rrezik i njohur provider-i i dokumentuar te M9 |
| `central-send-notifications` | `SKIP LOCKED` + retry i kufizuar | jo financiar |
| `backup.sh` / `restore.sh` | emra unikë, `.partial` i pastruar, rotacion për etiketë; restore vetëm në bazë të re | jo |
| `*_readiness`, `financial_ops`, `queue_readiness`, `verify_ledger`, `recovery_ledger`, `money_reconciliation` | vetëm-lexim | jo |
| `scripts.financial_retention --apply`, `apps.central.tools.retention --apply` | **pa kyç**; fshirje idempotente të rreshtave operacionalë (jo ledger/fatura); dry-run parazgjedhje; **nuk planifikohen** (vendim retention i hapur) | jo; dy ekzekutime paralele mund të auditojnë dy herë — ekzekutim manual, jo cron |

`job_runner` shton kyç hosti, timeout dhe **pa retry**: një job financiar i dështuar nuk riprovohet nga mbështjellësi (retry i ekzekutimit të parave do të rrezikonte efekte të dyfishta); ekzekutimi i radhës i planifikuar e kryen, dhe idempotenca e job-it e bën të sigurt.

## 4. Rastet e dështimit dhe veprimi

| Gjendja (`ops_freshness`) | Kuptimi | Veprimi i parë |
|---|---|---|
| `FAILED` | ekzekutimi i fundit dështoi/timeout dhe s'ka sukses më të ri | lexo log-un e cron-it të job-it; shih kodin e daljes te statusi; mos e ri-nis financiar pa kuptuar shkakun |
| `STALE` | suksesi i fundit më i vjetër se `max_age_seconds` | kontrollo planifikuesin (cron/timer), kyçin e ngecur (`skipped_locked` përsëritur), hostin |
| `MISSING` | s'ka status (kurrë i ekzekutuar përmes runner-it) | verifiko që job-i është planifikuar dhe `SMS_JOB_STATE_DIR` është i saktë |
| `STUCK` | «running» > 2× timeout (runner-i vdiq) | verifiko procesin; hiq statusin vetëm pas kontrollit; kyçi `flock` lirohet vetë kur procesi vdes |
| `*-artifact` | backup i vjetër/i pjesshëm/pa `.sha256` | `docs/m11i-postgres-backup-restore-runbook.md`; mos fshi backup-et e vjetra deri sa të ketë një të ri të plotë |

## 5. Siguria e vëzhgueshmërisë

- Statusi i job-eve ruan vetëm emrin e job-it, kodin e daljes, kohët dhe etiketën `python -m <modul>`/emrin e ekzekutuesit — **jo** argumente, mjedis, URL-ra. `ops_freshness` printon vetëm emra, kode dhe sekonda. Të dyja testohen (sekret sintetik nuk del).
- Asnjë thirrje `log.*` në `app/`, `apps/`, `scripts/` nuk i kalon vlera të emërtuara si sekret (kontroll AST te testi). `/readyz` kthen vetëm arsye të përgjithshme («database unavailable»), kurrë URL/gabim drajveri.
- `X-Request-ID`: pranohet vetëm nëse është i sigurt për log-e (≤ 64 karaktere, pa hapësira/kontroll) përndryshe zëvendësohet; **nuk shkruhet ende në log-et e aplikacionit** (shih §6).

## 6. Çfarë mungon (MISSING) dhe nuk u ndryshua pa raportim

| Boshllëk | Klasa | Shënim |
|---|---|---|
| Planifikuesi (cron/systemd/Kubernetes), alarmimi, on-call | **PRODUCTION-ONLY** | s'ka host prodhimi; repo-ja s'ka supozime Kubernetes (vetëm compose + cron i jashtëm) |
| Endpoint `/metrics` / Prometheus / OpenTelemetry / Sentry | MISSING (me qëllim) | nuk u shtua vendor; primitivat vendor-neutral janë kodet e daljes, JSON dhe statuset |
| Log-e të strukturuara (JSON), `request_id`/`enterprise_id` te log-et e aplikacionit | MISSING | ndryshim aplikacioni (`logging.basicConfig` te workers/API); **POLICY PROPOSAL**, nuk u bë |
| Format log-u nginx me `$request_time`, `$request_id` (AL-16/AL-17) | MISSING | ndryshim i sipërfaqes së skajit (F-02); **PROPOSED**, kërkon miratim: `log_format sms '$time_iso8601 $status $request_method $uri $request_time $upstream_response_time $request_id';` (pa query string, pa `Authorization`) |
| Latenca DLR si metrikë (AL-13) | MISSING | kërkon pyetje të re vetëm-lexim në kod aplikacioni; nuk u shtua |
| Agregimi i statistikave (`stats aggregation` si job) | NOT APPLICABLE | `GET /v1/admin/stats` llogaritet sipas kërkesës; s'ka job agregimi |
| Heartbeat për `send_notifications --loop` | MBULUAR | përdor një kalim çdo minutë përmes `job_runner` (`central-send-notifications`) |
| Metrika e pishinës së DB-së / lidhjeve (AL-19) | PRODUCTION-ONLY | `pg_stat_activity` nga host-i/provider-i |

## Operacione manuale të pa-planifikuara (M11-J)
`python -m scripts.dlr_receipt_retention` (retention i `sms_dlr_receipts`) **nuk është në inventarin e job-eve** dhe nuk planifikohet: dry-run për raport; `--apply` vetëm me vendim njeriu (shih `m11i-provider-dlr-runbook.md` §9). Nëse më vonë planifikohet, duhet të kalojë nga `scripts.job_runner` dhe të shtohet te `deploy/scheduled-jobs.json` me alarm.
