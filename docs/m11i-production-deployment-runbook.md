# M11-I — Procedura e deployment-it në prodhim (NUK ekzekutohet)

**Revision i bazës:** `6020026`. Ky dokument përshkruan një procedurë; **nuk është ekzekutuar dhe nuk ekzekutohet në M11-I**. Çdo hap që varet nga infrastruktura, llogari ose njerëz të jashtëm është shënuar **EXTERNAL EVIDENCE REQUIRED**.
**Komandat:** ato me burim në repo/docs janë të listuara si ekzistuese; ato të shënuara **PROPOSED** ose **FUTURE** nuk ekzistojnë ende ose s'janë provuar.
**Statusi i procedurës: BLOCKED** (shih `m11i-production-readiness-gap-analysis.md`). **Përditësim M11-I/F-01:** paketimi i Central ekziston dhe u provua LOKALISHT (imazhe, compose, migrim, hyrje në konsolë); komandat Central më poshtë janë tani ekzistuese (jo FUTURE), por **s'janë provuar në CI as në prodhim** (CI PENDING). Një autorizim ndërmjet fazave është i detyrueshëm; asnjë fazë nuk nis automatikisht.

Dokumentet shoqëruese: `m11i-production-cutover-checklist.md`, `m11i-production-rollback-runbook.md`, `m11i-observability-alerting-plan.md`, `m11i-secrets-and-access-plan.md`.

## Rregulla të përgjithshme

- Një operator i emëruar ekzekuton; një i dytë verifikon (PROPOSED).
- Asnjë migrim pa backup të ri të provuar (Faza 3).
- Asnjë `alembic downgrade` si rikthim në prodhim; rikthimi është imazh i vjetër + restore në DB të re.
- Autoritetet (`SMS_SENDER_AUTHORITY`, `SMS_MONEY_AUTHORITY`, `SMS_PRICING_AUTHORITY`, `SMS_BILLING_AUTHORITY`) mbeten `local` në deployment-in e parë; kalimi në `shadow`/`central` ndjek runbook-ët M9/M10 dhe kërkon miratim të veçantë.
- Asnjë sekret nuk shfaqet në terminal, log, tiketë ose chat.
- Në çdo abort: ndalo, regjistro gjendjen, ndiq `m11i-production-rollback-runbook.md`.

## Faza 0 — Parakushtet (të gjitha të jashtme)

| Fusha | Përmbajtja |
|---|---|
| Klasifikimi / evidenca | EXTERNAL EVIDENCE REQUIRED. Aktualisht NUK plotësohet asnjë (shih `m11i-production-readiness-gap-analysis.md`). |
| Rreziku | Nisja pa parakushte = deployment i pamundur ose i pasigurt. |
| Parakusht | GAP-04, 05, 07, 08, 09, 12 të mbyllura; F-01 e zbatuar (IMPLEMENTED / LOCALLY VERIFIED, **CI PENDING**: kërkohet CI jeshile mbi revision-in); F-02..F-04 sipas gap analysis; provë e plotë në staging; dritare ndryshimi; operator dhe zëvendës të emëruar; rollback runbook i lexuar nga operatori. |
| Validim para prodhimit | Lista e dëshmive e firmosur nga pronari i produktit dhe pronari i infrastrukturës. |
| Veprimi (NUK ekzekutohet në M11-I) | Asnjë (vetëm konfirmim). |
| Abort / rikthim | Nëse një parakusht mungon: NUK nisim. |
| Pronari / varësia | NUK ËSHTË CAKTUAR |
| GO/NO-GO | NO-GO derisa çdo parakusht ka dëshmi të bashkëngjitur. |

## Faza 1 — Preflight i revision-it

| Fusha | Përmbajtja |
|---|---|
| Klasifikimi / evidenca | Hapat e kodit: VERIFIED (komandat ekzistojnë). Rezultati në mjedisin e prodhimit: EXTERNAL EVIDENCE REQUIRED. |
| Rreziku | Deployment i një revision-i të pavalidar. |
| Parakusht | Revision i caktuar (tag/commit) me CI jeshile; M11-H e mbyllur (kod i pandryshuar që nga `229347d`). |
| Validim para prodhimit | `git rev-parse HEAD`; `git status --short` bosh; CI jeshile në atë revision; kokat: `alembic heads` = `0034` (Enterprise); kokat Central = `0031` (komanda e saktë për Central: PROPOSED `alembic -c apps/central/alembic.ini heads`). |
| Veprimi (NUK ekzekutohet në M11-I) | Regjistro revision-in dhe kokat në regjistrin e ndryshimit. |
| Abort / rikthim | Nëse CI s'është jeshile ose kokat ndryshojnë: abort. |
| Pronari / varësia | Inxhinieria — NUK ËSHTË CAKTUAR |
| GO/NO-GO | GO vetëm me revision të regjistruar dhe CI jeshile. |

## Faza 2 — Validimi i sekreteve dhe mjedisit (pa shfaqur vlera)

| Fusha | Përmbajtja |
|---|---|
| Klasifikimi / evidenca | Mbrojtjet e nisjes: VERIFIED (`production_problems()`); M11-I/F-03: kontrollon edhe `SMS_TRUSTED_PROXY_HOPS`/`CENTRAL_TRUSTED_PROXY_HOPS` = 1, `SMS_DLR_SECRETS` për provider-in HTTP aktiv, Twilio gjysmë-konfigurimin dhe SMTP (kushtëzuar; kredencialet e provider-it nuk janë globalisht të detyrueshme) — VERIFIED LOKALISHT. Vlerat reale: EXTERNAL EVIDENCE REQUIRED. |
| Rreziku | Sekrete të dobëta/default ose të ruajtura gabim; humbja e `SMS_SECRETS_KEY`/`SMS_PII_HMAC_KEY` është e pariparueshme. |
| Parakusht | Sekretet e provisionuara sipas `m11i-secrets-and-access-plan.md`; `.env` me leje 600, jashtë git; çelësi CP si mount read-only. |
| Validim para prodhimit | Brenda imazhit me `env_file`: `python -c "from app.core.config import settings; print(chr(10).join(settings.production_problems()) or 'OK')"` (PROPOSED; mesazhet janë tekste statike, pa vlera) — pritet `OK`. Kontrolle manuale: asnjë vlerë `change-me`/`dev`/`test`/`demo`; `SMS_ENV=production`; `SMS_PAYMENT_PROVIDER=disabled`; `SMS_PUBLIC_BASE_URL` fillon me `https://`; `SMS_TRUSTED_PROXY_HOPS` sipas topologjisë së dokumentuar. **Central:** `.env.central` (nga `.env.central.example`, leje 600, jashtë git): `CENTRAL_ENV=production`, `CENTRAL_DATABASE_URL` drejt DB `sms_central` (rol i veçantë; jo DB-ja `sms` e Enterprise), `CENTRAL_AUTH_SECRET` ≥ 32 karaktere, `CENTRAL_MAILER` dhe `CENTRAL_BOT_CHALLENGE` ≠ `fake`, `CENTRAL_ALLOW_UNVERIFIED_AUTO_REGISTRATION=false`. Nisja në prodhim refuzon SQLite/jo-PostgreSQL, sekret të dobët dhe `fake` (VERIFIED LOKALISHT). Vlerat reale: EXTERNAL EVIDENCE REQUIRED. |
| Veprimi (NUK ekzekutohet në M11-I) | Asnjë (vetëm validim). |
| Abort / rikthim | Çdo problem i listuar ose vlerë placeholder: abort. |
| Pronari / varësia | Sigurinë + operatori — NUK ËSHTË CAKTUAR |
| GO/NO-GO | GO vetëm me daljen `OK` dhe kontrollet manuale të firmosura. |

## Faza 3 — Konfirmimi i backup-it

| Fusha | Përmbajtja |
|---|---|
| Klasifikimi / evidenca | Skriptet: VERIFIED (CI round-trip). Backup/restore në prodhim: EXTERNAL EVIDENCE REQUIRED. |
| Rreziku | Pa backup të provuar, rikthimi i migrimeve me humbje të dhënash është i pamundur (`alembic downgrade` humb të dhëna, RUNBOOK §3). |
| Parakusht | GAP-05 dhe GAP-06 të mbyllura. |
| Validim para prodhimit | Backup i ri i të dy bazave (`BACKUP_LABEL=enterprise|central scripts/backup.sh`); `sha256sum -c <dump>.sha256` = OK; kopjim off-host; restore në DB të re me `ADMIN_URL=... scripts/restore.sh <dump> <db_e_re>`, pastaj `scripts.recovery_verify head` + `scripts.verify_ledger` (Enterprise) / `python -m apps.central.tools.recovery_ledger` (Central) = PASS. Detaje: `docs/m11i-postgres-backup-restore-runbook.md`. |
| Veprimi (NUK ekzekutohet në M11-I) | Ekzekutimi i backup-it para ndryshimit (jo të restore-it në bazën e prodhimit). |
| Abort / rikthim | Nëse checksum, restore ose `verify_ledger` dështon: abort; asnjë migrim. |
| Pronari / varësia | DBA — NUK ËSHTË CAKTUAR |
| GO/NO-GO | GO vetëm me backup të ri të provuar që ditë (PROPOSED: <24 orë). |

## Faza 4 — Rendi i migrimeve

| Fusha | Përmbajtja |
|---|---|
| Klasifikimi / evidenca | Mekanizmi: VERIFIED (shërbimi `migrate` në compose; `depends_on: service_completed_successfully`; CI up/down/up). Central: `central-migrate` ekziston (VERIFIED LOKALISHT mbi PG të kontejnerit; CI PENDING). **DB `sms_central` krijohet para tij me hap infrastrukture** (aplikacioni nuk e krijon; RUNBOOK §8.1); migrimi refuzon një DB Enterprise dhe SQLite në prodhim. |
| Rreziku | Migrimi i pjesshëm lë skemën në gjendje të papajtueshme; `readyz` kthen 503. |
| Parakusht | Faza 3 e mbyllur. |
| Validim para prodhimit | Në staging me kopje të përmasave reale: migrim Enterprise → kokë 0034; Central → 0031; `readyz` ready në të dyja. |
| Veprimi (NUK ekzekutohet në M11-I) | 1) Enterprise: `docker compose -f docker-compose.prod.yml --env-file .env run --rm migrate` (PROPOSED si hap i veçantë; `up -d` e ekzekuton automatikisht). 2) Central: `docker compose -f docker-compose.prod.yml --env-file .env run --rm central-migrate` (`alembic -c apps/central/alembic.ini upgrade head`, kokë 0031). Bazat janë të ndara; kokat verifikohen veçmas (`central_alembic_version` në `sms_central`, `sms_alembic_version` në `sms`). |
| Abort / rikthim | Nëse migrimi dështon: NDALO; mos nis api/workers; rikthim = imazh i vjetër + restore në DB të re (jo downgrade). |
| Pronari / varësia | DBA + inxhinieria — NUK ËSHTË CAKTUAR |
| GO/NO-GO | GO kur kokat janë 0034/0031 dhe `readyz` ready. |

## Faza 5 — Rendi i nisjes

| Fusha | Përmbajtja |
|---|---|
| Klasifikimi / evidenca | Vartësitë: VERIFIED (compose: db → migrate → api/worker/webhook-worker → web). Nisja reale: NOT VERIFIED. |
| Rreziku | Workers para API-së ose para migrimit. |
| Parakusht | Faza 4. |
| Validim para prodhimit | Rendi i dokumentuar: db healthy → migrate përfundon → api → worker, webhook-worker → web; Central: db → `central-migrate` → `central-api` (healthy) → `central-web`; pastaj profilet opsionale vetëm me miratim shprehimisht (`control-plane`, `money-*`, `billing-report`, `pricing-sync`, `sender`). |
| Veprimi (NUK ekzekutohet në M11-I) | `docker compose -f docker-compose.prod.yml --env-file .env up -d --build` (RUNBOOK §1.2) — pa profile = **vetëm Enterprise** (nuk kërkon `.env.central` as DB `sms_central`). Central nis veçmas, me opt-in të shprehur, në Fazën 9 (`--profile central`). Jobet Central (`central-jobs`) nuk nisen kurrë me `up -d`. |
| Abort / rikthim | Nëse një shërbim del unhealthy brenda 5 min (PROPOSED): ndalo dhe diagnostiko; mos vazhdo. |
| Pronari / varësia | Operatori — NUK ËSHTË CAKTUAR |
| GO/NO-GO | GO kur `docker compose ps` tregon healthy për db, api, worker, webhook-worker, web. |

## Faza 6 — Shëndeti dhe gatishmëria

| Fusha | Përmbajtja |
|---|---|
| Klasifikimi / evidenca | Pikat: VERIFIED (`/healthz`, `/readyz`, Docker healthcheck). Rezultati në prodhim: EXTERNAL EVIDENCE REQUIRED. |
| Rreziku | Pranim i ndërprerë i trafikut mbi një aplikacion jo-gati. |
| Parakusht | Faza 5. |
| Validim para prodhimit | `curl -s http://127.0.0.1:8080/readyz` → `{"status":"ready"}`; `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/healthz` = 200; `docker compose ... ps` pa 'unhealthy'. |
| Veprimi (NUK ekzekutohet në M11-I) | Asnjë. |
| Abort / rikthim | 503 ose unhealthy për >5 min: abort. |
| Pronari / varësia | Operatori — NUK ËSHTË CAKTUAR |
| GO/NO-GO | GO vetëm me `ready`. |

## Faza 7 — Workers dhe radhët

| Fusha | Përmbajtja |
|---|---|
| Klasifikimi / evidenca | Mjetet: VERIFIED (`worker_health`, `scripts.queue_readiness`, kill switches). Sjellja në kontejnerë të compose: PARTIALLY VERIFIED (M11-H përdori procese lokale). |
| Rreziku | Workers të ngecur; mesazhe UNKNOWN/SENDING të pazgjidhur. |
| Parakusht | Faza 6. |
| Validim para prodhimit | `docker compose ... ps worker webhook-worker` = healthy; `docker compose ... exec api python -m scripts.queue_readiness --json` pa FAIL; kill switch `dispatch` te konsola (Admin → Kill switches) në pauzë deri te canary. |
| Veprimi (NUK ekzekutohet në M11-I) | Mbaje `dispatch` në pauzë; çaktivizo çdo rrugë provider-i derisa të nisë canary. |
| Abort / rikthim | FAIL te `queue_readiness` ose heartbeat >90 s: abort. |
| Pronari / varësia | Operatori — NUK ËSHTË CAKTUAR |
| GO/NO-GO | GO kur 0 FAIL dhe heartbeat i freskët. |

## Faza 8 — Frontend (Enterprise)

| Fusha | Përmbajtja |
|---|---|
| Klasifikimi / evidenca | Build/imazh: VERIFIED (CI ndërton `deploy/web.Dockerfile`; M11-H: 281 teste Vitest, build OK). Shfletim në prodhim: EXTERNAL EVIDENCE REQUIRED. |
| Rreziku | Konsolë e prishur ose me CSP të gabuar. |
| Parakusht | Faza 6, TLS (Faza 10). |
| Validim para prodhimit | Hapje në Chromium e `https://<domen>/`; login me çelës provë personal; 0 gabime console; kontroll i header-ave: `curl -sI https://<domen>/` përmban `Content-Security-Policy`, `Strict-Transport-Security`. |
| Veprimi (NUK ekzekutohet në M11-I) | Asnjë (imazhi `web` nis me stack-un). |
| Abort / rikthim | Gabime console ose CSP: abort i hapjes së konsolës. |
| Pronari / varësia | Operatori — NUK ËSHTË CAKTUAR |
| GO/NO-GO | GO me 0 gabime console. |

## Faza 9 — Central (backend dhe frontend)

| Fusha | Përmbajtja |
|---|---|
| Klasifikimi / evidenca | Paketimi: **IMPLEMENTED / LOCALLY VERIFIED; CI PENDING** (GAP-01, 02, 03). Prodhimi: EXTERNAL EVIDENCE REQUIRED / NOT VERIFIED. |
| Rreziku | HIGH. |
| Parakusht | F-01 (paketimi) i zbatuar; CI jeshile mbi revision-in; faza 2 (sekretet Central) dhe faza 4 (DB `sms_central` + migrim) të mbyllura. |
| Validim para prodhimit | Në staging (dhe i provuar lokalisht): `central-migrate` te 0031; `curl -s http://127.0.0.1:8081/readyz` → `ready`; `/internal/sync/changes`, `/v1/*`, `/webhooks/*` përmes `central-web` = 404; `central-api:8001` i paarritshëm nga host-i; hyrje admin dhe operator në Chromium pa gabime console; `CENTRAL_ENV=production` refuzon SQLite, sekret bosh dhe `fake`. Në prodhim: EXTERNAL EVIDENCE REQUIRED. |
| Veprimi (NUK ekzekutohet në M11-I) | `docker compose -f docker-compose.prod.yml --env-file .env --profile central run --rm central-migrate`, pastaj `… up -d central-web` (nis vetëm Central: `central-migrate` → `central-api` → `central-web`; `… --profile central up -d` nis Enterprise + Central bashkë); admini i parë me `… --profile central-jobs run --rm --no-deps --entrypoint python central-tool -m apps.central.tools.create_admin --email <email> --role admin` (fjalëkalimi kërkohet në terminal). Procesat e planifikuara (`central-billing-run`, `central-send-notifications`, `central-tool`; profili `central-jobs`) nisen vetëm manualisht ose nga një planifikues i jashtëm, kurrë me `up -d`; komandat e mbështetura brenda imazhit Central janë vetëm ato të inventarit te RUNBOOK §8 (`financial_readiness` ekzekutohet jashtë imazhit, nga një checkout me të dy mjediset); planifikimi/monitorimi = GAP-16 (EXTERNAL). |
| Abort / rikthim | Central s'bëhet healthy: Enterprise mbetet `local` (fail-static); asnjë cutover. |
| Pronari / varësia | Inxhinieria — NUK ËSHTË CAKTUAR |
| GO/NO-GO | NO-GO derisa CI të jetë jeshile mbi paketimin dhe fazat 2/4 të Central të kenë dëshmi; TLS/proxy real (Faza 10) është kusht i veçantë. |

## Faza 10 — Proxy, TLS dhe skaji (M11-I/F-02)

| Fusha | Përmbajtja |
|---|---|
| Klasifikimi / evidenca | Konfigurimi i skajit: **IMPLEMENTED / LOCALLY VERIFIED** (nginx real: `real_ip`, XFF/Proto, header-at, 429, webhooks, health, `/internal`; 64 kontrolle; CI PENDING). C-3 dhe N-1 RESOLVED. TLS/DNS/LB/CIDR reale: EXTERNAL EVIDENCE REQUIRED. |
| Rreziku | TLS i gabuar/mungues; CIDR e gabuar e proxy-t (IP e gabuar, kyçje masive, ose besim i tepërt); HSTS i aktivizuar para HTTPS-së së provuar. |
| Parakusht | GAP-07, GAP-08; terminator i jashtëm i TLS (80→443, SAN për `enterprise.`, `central.`, `central-internal.`, TLS ≥ 1.2) që shton XFF dhe vendos `X-Forwarded-Proto: https`; `.env`: `WEB_TRUSTED_PROXY_CIDRS`, `CENTRAL_WEB_TRUSTED_PROXY_CIDRS`, `CENTRAL_S2S_ALLOW_CIDRS` (RUNBOOK §2). |
| Validim para prodhimit | `python3 e2e/edge/check_edge.py` mbi imazhet e lëshimit; `nginx -t`; në staging me terminatorin: test me dy IP, Proto, `/readyz` 403 publikisht, `/internal` vetëm nga Enterprise, callback i nënshkruar; checklist EG-01..EG-10. |
| Veprimi (NUK ekzekutohet në M11-I) | 1) Plotëso CIDR-t. 2) Nis/rinis `web` dhe `central-web` (`up -d --force-recreate web` / `central-web`). 3) Vendos DNS pas provës së TLS. 4) HSTS te terminatori vetëm sipas RUNBOOK §2b (`max-age=300` → ngritje graduale). |
| Abort / rikthim | Zinxhir i pavlefshëm, IP e gabuar ose 429 për përdorues legjitim: abort; kthe CIDR-t e mëparshme dhe rinis nginx; kthe DNS/LB; HSTS: `max-age=0` te terminatori. |
| Pronari / varësia | Infrastruktura — NUK ËSHTË CAKTUAR |
| GO/NO-GO | GO vetëm me EG-01..EG-10 të plotësuara dhe provën me dy IP përmes terminatorit real. |

## Faza 11 — Provider dhe DLR

| Fusha | Përmbajtja |
|---|---|
| Klasifikimi / evidenca | Kodi: VERIFIED. Provider real: EXTERNAL EVIDENCE REQUIRED; canary: GAP-11. |
| Rreziku | SMS reale/faturim gabim; DLR i falsifikuar. |
| Parakusht | GAP-10, GAP-11; `SMS_DLR_SECRETS` të vendosur; `SMS_PUBLIC_BASE_URL` i arritshëm nga provider-i. |
| Validim para prodhimit | Provë idempotence e provider-it (M9 checklist §1); callback i nënshkruar pranohet, i pasaktë refuzohet (401/4xx). |
| Veprimi (NUK ekzekutohet në M11-I) | Hap rrugën (`PUT /v1/admin/routes`, kërkon 2FA) vetëm për prefiksin e canary-t, pastaj ndiq `m11i-production-cutover-checklist.md` seksionin CANARY. |
| Abort / rikthim | UNKNOWN ose DLR që s'vjen brenda pragut: pauzo `dispatch`, çaktivizo rrugën. |
| Pronari / varësia | Operatori + pronari i provider-it — NUK ËSHTË CAKTUAR |
| GO/NO-GO | GO vetëm pas canary-t me 0 UNKNOWN. |

## Faza 12 — Validimi pas deployment-it

| Fusha | Përmbajtja |
|---|---|
| Klasifikimi / evidenca | Hapat: VERIFIED (RUNBOOK §1.4, `verify_ledger`, `financial_ops`). Rezultatet: EXTERNAL EVIDENCE REQUIRED. |
| Rreziku | Qasje e papritur: çelësi bootstrap mbetet aktiv. |
| Parakusht | Faza 6–11. |
| Validim para prodhimit | Çelësa `superadmin` personalë + 2FA aktive; `SMS_ADMIN_API_KEY` hequr nga `.env` dhe shërbimet e rinisura (RUNBOOK §1.4); `python -m scripts.verify_ledger` PASS; `python -m scripts.financial_ops --json` pa CRITICAL; alarmet: një test i provokuar arrin pranuesin. |
| Veprimi (NUK ekzekutohet në M11-I) | Hiq çelësin bootstrap. |
| Abort / rikthim | CRITICAL ose test alarmi i dështuar: mos hap trafik; trajto sipas rollback runbook. |
| Pronari / varësia | Operatori + sigurinë — NUK ËSHTË CAKTUAR |
| GO/NO-GO | GO final vetëm me të gjitha rreshtat e checklist-it të cutover-it të shënuar. |

## Pikat e abort-it (përmbledhje)

| Pika | Kushti (PROPOSED nëse s'është i verifikuar) | Veprimi |
|---|---|---|
| Para Fazës 4 | Backup/restore/`verify_ledger` dështon | Mos migro; rregullo backup-in |
| Gjatë Fazës 4 | Migrimi del me gabim | Ndalo; imazh i vjetër + restore në DB të re |
| Pas Fazës 5 | Shërbim unhealthy >5 min | Ndalo; diagnostiko; mos hap trafik |
| Pas Fazës 6 | `readyz` ≠ ready | Rikthe imazhin e mëparshëm |
| Pas Fazës 10 | Zinxhir TLS i pavlefshëm ose 429 masiv | Kthe DNS/LB |
| Gjatë Fazës 11 | UNKNOWN ose DLR që s'vjen | Pauzo `dispatch`; çaktivizo rrugën |
| Pas Fazës 12 | CRITICAL te `financial_ops`/`verify_ledger` FAIL | Mos hap trafik; rikthim sipas runbook-ut |

## Kriteri përfundimtar

Deployment-i konsiderohet i përfunduar vetëm kur çdo rresht i `m11i-production-cutover-checklist.md` është i shënuar me provë. Deri atëherë: **BLOCKED**.
