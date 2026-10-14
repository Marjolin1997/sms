# M11-I — Analiza e boshllëqeve për gatishmërinë e prodhimit

**Revision i bazës:** `6020026`. **Faza:** M11-I Faza 2 (vetëm dokumentim; asnjë ndryshim kodi, konfigurimi apo infrastrukture).
**Verdikti: BLOCKED.** Kodi është pilot-ready (M11-H: PASS WITH ACCEPTED DEBT); sistemi nuk është gati operativisht për prodhim.

## 0. Si lexohet ky dokument

- Klasifikimet: **VERIFIED** (provuar në repo ose në validimet M11-H), **PARTIALLY VERIFIED**, **NOT VERIFIED**, **EXTERNAL EVIDENCE REQUIRED** (kërkon infrastrukturë, llogari ose njerëz jashtë repo-së).
- Asnjë fakt prodhimi nuk është shpikur. Pragjet, kohët dhe pronarët e shënuar **PROPOSED** ose **NUK ËSHTË CAKTUAR** janë propozime/zbrazëti, jo fakte.
- Veprimet e ardhshme teknike (F-xx) janë **të papërfshira** në M11-I; kërkojnë miratim të shprehur në një fazë të mëvonshme.

## 1. Veprimet e ardhshme që heqin bllokuesit (jo të zbatuara)

| ID | Veprimi i ardhshëm | Heq |
|---|---|---|
| F-01 | Imazh/target Central + shërbim dhe migrim Central në compose + `CENTRAL_*` në shabllonin e mjedisit | GAP-01 |
| F-02 | Build/imazh për `frontend-central` dhe servirje statike | GAP-02 |
| F-03 | Rrugëzim proxy/nginx për Central (publike/private), `limit_req` për `/auth/token` | GAP-03 |
| F-04 | Hapa CI: Vitest/build Central, E2E Central, `docker compose config`, skanim sekretesh, imazh Central | GAP-14, GAP-15 |
| F-05 | Mbrojtje nisjeje: `SMS_DLR_SECRETS`, kredenciale provider-i; vendim për 2FA të stafit Central | GAP-17 |
| F-06 | Korrigjim dokumentesh: referenca SMPP, rruga `/backup.sh`, pohimi për TLS në nginx | Kontradiktat |

## 2. Regjistri i boshllëqeve

### GAP-01 — Paketimi i Central (backend)

| Fusha | Përmbajtja |
|---|---|
| Gjendja aktuale | `Dockerfile` kopjon vetëm `alembic`, `app`, `packages`, `scripts`; jo `apps/`. `docker-compose.prod.yml` nuk ka shërbim as migrim Central; `.env.example` nuk ka `CENTRAL_*`. Central nis nga burimi në harness-in e pilotit (M11-H), jo nga imazh. |
| Evidenca | `Dockerfile`; `docker-compose.prod.yml`; `.env.example`; `docs/RUNBOOK.md` §7; M11-H (Central uvicorn `:8101` mbi PG, nga burimi). |
| Klasifikimi | Boshllëku: VERIFIED. Central nga burimi mbi PG: VERIFIED (M11-H). Central nga imazh/compose: NOT VERIFIED. |
| Rreziku | HIGH. Central është autoriteti për sender, para, çmime dhe faturim periodik; me repo-n aktuale nuk mund të vendoset në prodhim. |
| Parakusht | Miratim i ri i shprehur i një faze zbatimi (F-01): imazh/target që përfshin `apps/`, shërbim + migrim Central (DB e veçantë, tabela `central_alembic_version`), shabllon `CENTRAL_*`, user jo-root, FS read-only, healthcheck `/readyz`. |
| Validim i kërkuar para prodhimit | Në staging (sekrete jo-prodhimi): ndërtim nga checkout i pastër; `import apps.central.main` brenda imazhit; migrim bosh→kokë 0031; `/readyz` 200; nisja me `CENTRAL_ENV=production` pa `CENTRAL_AUTH_SECRET` refuzohet (mbrojtja ekziston te `create_app`). |
| Veprimi në prodhim | Ndërto imazhin; ekzekuto migrimin Central; nis shërbimin Central. |
| Rikthim / abort | Abort nëse migrimi dështon ose `/readyz` ≠ 200 për >5 min (PROPOSED). Rikthim: ndalo shërbimin Central; Enterprise mbetet në `local` (fail-static, provuar në M11-H `ops_scenarios`). |
| Pronari / varësia | NUK ËSHTË CAKTUAR (jashtë repo-së); varet nga miratimi i pronarit të produktit. |
| Kriteri GO/NO-GO | GO vetëm kur imazhi ndërtohet nga checkout i pastër, kalon validimin në staging dhe mbulohet nga CI (GAP-14). |

### GAP-02 — Paketimi i frontend-it Central

| Fusha | Përmbajtja |
|---|---|
| Gjendja aktuale | `frontend-central` ka vetëm `npm run dev` (proxy `/auth`, `/admin` te `CENTRAL_API_URL`). Nuk ka Dockerfile/imazh, as shërbim në compose. |
| Evidenca | `frontend-central/README.md`; mungesa e `frontend-central/Dockerfile*` (listim); `deploy/web.Dockerfile` ndërton vetëm `frontend/`. Build dhe Vitest kalojnë lokalisht (M11-H: 232 teste). |
| Klasifikimi | Boshllëku: VERIFIED. Build prodhimi i servirur nga nginx: NOT VERIFIED. |
| Rreziku | HIGH (stafi nuk ka konsolë prodhimi për Central). |
| Parakusht | F-02: target build + servirje statike (p.sh. nginx) me të njëjtat header-a sigurie; vendim për domenin/subdomenin e konsolës Central. |
| Validim i kërkuar para prodhimit | Build nga `npm ci && npm run build` në imazh; `nginx -t`; hapje në Chromium e login-it dhe e një faqeje audit kundrejt Central staging; kontroll CSP (pa `unsafe-inline` për skripte). |
| Veprimi në prodhim | Publiko artefaktin e frontend-it Central pas Central backend. |
| Rikthim / abort | Abort nëse login ose një faqe kyçe jep gabim console/CSP. Rikthim: hiq rrugën/vhost-in e konsolës Central; API mbetet i paarritshëm publikisht. |
| Pronari / varësia | NUK ËSHTË CAKTUAR (jashtë repo-së) |
| Kriteri GO/NO-GO | GO kur konsola Central ngarkohet nga imazhi në staging me 0 gabime console. |

### GAP-03 — Rrugëzimi nginx/proxy për Central

| Fusha | Përmbajtja |
|---|---|
| Gjendja aktuale | `deploy/nginx.conf` ruton vetëm `/(v1|u|webhooks|healthz|readyz)`. Nuk ka upstream, as rrugë për `/auth`, `/admin`, `/internal`, `/registration` të Central. `CENTRAL_TRUSTED_PROXY_HOPS` default 0. |
| Evidenca | `deploy/nginx.conf`; `apps/central/core/config.py`; `docs/M9_PRODUCTION_CHECKLIST.md` §9. |
| Klasifikimi | Boshllëku: VERIFIED. Çfarë ekspozohet publikisht te Central: NOT VERIFIED (vendim i hapur). |
| Rreziku | HIGH. Rrugët `/internal/*` (sync, money, pricing, billing, sender) janë për Enterprise→Central; ekspozimi i gabuar zgjeron sipërfaqen e sulmit. |
| Parakusht | Vendim arkitekture: cilat rrugë Central janë publike/private (p.sh. `/internal/*` vetëm nga rrjeti i Enterprise, `/admin/*` vetëm nga VPN/IP të lejuara). F-03: konfigurim proxy + `limit_req` për `/auth/token`. |
| Validim i kërkuar para prodhimit | Në staging: matricë rrugësh (publike/private) me `curl` nga rrjete të ndryshme; `/internal/*` nga jashtë = 403/404; `nginx -t`; IP e klientit e lexuar saktë (shih GAP-08). |
| Veprimi në prodhim | Aplikimi i konfigurimit proxy për Central. |
| Rikthim / abort | Abort nëse një rrugë `/internal/*` kthen 200 nga rrjet i jashtëm. Rikthim: kthe konfigurimin e mëparshëm nginx dhe ngarko. |
| Pronari / varësia | NUK ËSHTË CAKTUAR (jashtë repo-së) |
| Kriteri GO/NO-GO | GO kur matrica e rrugëve është e saktë dhe e dokumentuar me dalje reale. |

### GAP-04 — Infrastruktura e prodhimit (hosting, rrjet, kapacitet)

| Fusha | Përmbajtja |
|---|---|
| Gjendja aktuale | Nuk ka asnjë informacion për host, rrjet, kapacitet, firewall, OS në repo. Runbook-u supozon një host me Docker Compose. |
| Evidenca | `docs/RUNBOOK.md` (një host me Compose); `docs/M10_PROD_OPERATIONS.md` §0 (të dhënat e prodhimit janë PA PROVË). |
| Klasifikimi | EXTERNAL EVIDENCE REQUIRED. Që `docker-compose.prod.yml` ngrihet realisht: NOT VERIFIED (CI ndërton imazhet, s'e nis compose-in). |
| Rreziku | HIGH. |
| Parakusht | Host/VM i miratuar, Docker + Compose me version të fiksuar, rrjet i brendshëm, firewall që lejon vetëm 80/443 (dhe SSH nga IP të kufizuara). |
| Validim i kërkuar para prodhimit | Inventar i shkruar i hostit (OS, versioni Docker/Compose, CPU/RAM/disk, regjione) i firmosur nga pronari i infrastrukturës; `docker compose -f docker-compose.prod.yml config --services` pa gabime në host; prova e nisjes në staging të njëjtë me prodhimin. |
| Veprimi në prodhim | Pajisja e hostit dhe instalimi i varësive (jashtë repo-së). |
| Rikthim / abort | Abort nëse `compose config` dështon ose healthcheck-et nuk bëhen healthy brenda 5 min (PROPOSED). |
| Pronari / varësia | Pronari i infrastrukturës — NUK ËSHTË CAKTUAR (jashtë repo-së) |
| Kriteri GO/NO-GO | GO vetëm me inventar të firmosur dhe provë nisjeje në staging. |

### GAP-05 — PostgreSQL prodhimi, backup dhe PITR (Enterprise)

| Fusha | Përmbajtja |
|---|---|
| Gjendja aktuale | Në repo: `scripts/backup.sh` (pg_dump custom + checksum + rotacion 14 ditë + `pg_restore --list`) dhe `scripts/restore.sh` (në DB të re). CI bën round-trip backup/restore + `verify_ledger` në PG 16. Compose përdor `postgres:16-alpine` me volum lokal. |
| Evidenca | `scripts/backup.sh`, `scripts/restore.sh`, `.github/workflows/ci.yml`; `docs/RUNBOOK.md` §4; `docs/M10_PROD_OPERATIONS.md` §5 (PITR/WAL = EXTERNAL BLOCKER). |
| Klasifikimi | Skriptet dhe round-trip në CI: VERIFIED. PG prodhimi, backup off-host, PITR/WAL, restore i provuar në infrastrukturën reale: EXTERNAL EVIDENCE REQUIRED. Rreshti `/backup.sh` në koment të `backup.sh` supozon skriptin brenda kontejnerit `db`, por compose monton vetëm `./backups` (PARTIALLY VERIFIED, shih kontradiktat). |
| Rreziku | HIGH. Pa PITR, humbja e të dhënave arrin deri në ndërprerjen e fundit të backup-it; `alembic downgrade` humb të dhëna (RUNBOOK §3). |
| Parakusht | PG në prodhim me rol aplikacioni pa superuser, TLS (`sslmode=require` te URL), WAL arkivim ose replikë, backup i enkriptuar off-host. |
| Validim i kërkuar para prodhimit | Backup real i kryer → kopjuar off-host → restore në DB të re (`restore.sh`) → `python -m scripts.verify_ledger` PASS; vërtetim PITR: rikthim në një kohë të caktuar në një instancë të veçantë me dokumentim të kohës. |
| Veprimi në prodhim | Konfigurimi i backup-it/PITR; cron backup (RUNBOOK §4). |
| Rikthim / abort | Abort nëse restore ose `verify_ledger` dështon. Rikthim: asgjë nuk ndryshohet në prodhim nga këto prova (restore bëhet në DB të re). |
| Pronari / varësia | DBA/infrastrukturë — NUK ËSHTË CAKTUAR (jashtë repo-së) |
| Kriteri GO/NO-GO | GO kur ka dëshmi restore + PITR të firmosura brenda 30 ditëve (PROPOSED). |

### GAP-06 — Backup dhe restore i Central

| Fusha | Përmbajtja |
|---|---|
| Gjendja aktuale | `backup.sh` merr `DATABASE_URL` të çfarëdoshme, por asnjë procedurë nuk e dokumenton për DB-në e Central. `docs/M9G_BILLING.md` e shënon PITR/WAL ose replikë sinkrone për Central si bllokues operativ. |
| Evidenca | `docs/M9G_BILLING.md` (bllokuesit operativë); `docs/M10_PROD_OPERATIONS.md` §5; `docs/M9_PRODUCTION_CHECKLIST.md` §0 (backup i të dy bazave). |
| Klasifikimi | Skripti gjenerik: VERIFIED. Procedurë dhe provë për Central: NOT VERIFIED. PITR Central: EXTERNAL EVIDENCE REQUIRED. |
| Rreziku | HIGH (Central mban librin e parave/faturave si autoritet pas cutover-it). |
| Parakusht | GAP-01 (Central në ekzistencë), GAP-05. |
| Validim i kërkuar para prodhimit | Backup + restore i DB-së Central në DB të re + prova e dy kokave (`central_alembic_version` = 0031) + `python -m apps.central.tools.financial_readiness --json`. |
| Veprimi në prodhim | Cron backup për DB-në Central; kopjim off-host. |
| Rikthim / abort | Abort nëse readiness pas restore ka FAIL. Rikthim: asnjë ndryshim në prodhim. |
| Pronari / varësia | DBA — NUK ËSHTË CAKTUAR (jashtë repo-së) |
| Kriteri GO/NO-GO | GO kur restore Central është provuar dhe dokumentuar. |

### GAP-07 — TLS, DNS dhe load balancer

| Fusha | Përmbajtja |
|---|---|
| Gjendja aktuale | `deploy/nginx.conf` dëgjon `8080` pa certifikata (HSTS dërgohet). Dokumentet thonë 'TLS përfundon në nginx ose në load balancer', por asnjë nuk është konfiguruar në repo. |
| Evidenca | `deploy/nginx.conf`; `docker-compose.prod.yml` (`127.0.0.1:8080`); `docs/RUNBOOK.md` §2. |
| Klasifikimi | Mungesa e TLS në repo: VERIFIED. TLS/DNS/LB real: EXTERNAL EVIDENCE REQUIRED. |
| Rreziku | HIGH. HSTS pa TLS funksional mund të bllokojë shfletuesit; kredencialet kalojnë në tekst të hapur nëse TLS mungon. |
| Parakusht | Domene (Enterprise API/konsolë, Central) të caktuara, certifikata të lëshuara, pika përfundimi TLS e vendosur, HSTS vetëm pasi TLS provohet. |
| Validim i kërkuar para prodhimit | `curl -vI https://<domen>/healthz` = 200 me zinxhir të vlefshëm; skadenca e certifikatës ≥ 30 ditë; test TLS (p.sh. `openssl s_client`) me protokolle/shifra të miratuara; `https://` te `SMS_PUBLIC_BASE_URL` dhe te `SMS_CP_BASE_URL`. |
| Veprimi në prodhim | Vendos TLS + DNS; pastaj hap trafikun. |
| Rikthim / abort | Abort nëse zinxhiri i certifikatës dështon ose DNS nuk zgjidhet. Rikthim: kthe DNS/LB te gjendja e mëparshme (shih rollback). |
| Pronari / varësia | Pronari i infrastrukturës/DNS — NUK ËSHTË CAKTUAR (jashtë repo-së) |
| Kriteri GO/NO-GO | GO kur provat `curl`/`openssl` kalojnë nga një rrjet i jashtëm. |

### GAP-08 — SMS_TRUSTED_PROXY_HOPS dhe CENTRAL_TRUSTED_PROXY_HOPS

| Fusha | Përmbajtja |
|---|---|
| Gjendja aktuale | Enterprise: `SMS_TRUSTED_PROXY_HOPS=1` në compose; nginx shkruan `X-Forwarded-For` me `$remote_addr`, kështu 1 është i saktë për një nginx të vetëm. Me LB para nginx duhet `set_real_ip_from`; nuk është konfiguruar. Central: default 0. Me 0 pas proxy-t kyçja sipas IP (429 pas `SMS_AUTH_MAX_FAILURES`=20) mund të kyçë të gjithë. |
| Evidenca | `deploy/nginx.conf` (komentet); `docker-compose.prod.yml`; `app/core/config.py` (`auth_max_failures`); M11-H (gjetje HIGH, rbac_auth); `apps/central/core/config.py`. |
| Klasifikimi | Sjellja kur vlera është e gabuar: VERIFIED (M11-H). Vlera e saktë në topologjinë reale: EXTERNAL EVIDENCE REQUIRED. |
| Rreziku | HIGH. |
| Parakusht | Topologjia përfundimtare e proxy-ve e dokumentuar (numri i hop-eve për Enterprise dhe për Central). |
| Validim i kërkuar para prodhimit | Nga dy IP të ndryshme jashtë, 21 çelësa të gabuar nga IP-A kthejnë 429 vetëm për IP-A; IP-B hyn normalisht; vlera e raportuar e IP-së te audit/log përputhet me IP-në reale. |
| Veprimi në prodhim | Vendos vlerën e saktë në `.env` të secilit shërbim. |
| Rikthim / abort | Abort nëse përdorues legjitim nga IP-B merr 429. Rikthim: kthe vlerën e mëparshme dhe rinis shërbimin. |
| Pronari / varësia | Infrastrukturë + sigurinë — NUK ËSHTË CAKTUAR (jashtë repo-së) |
| Kriteri GO/NO-GO | GO kur testi me dy IP kalon në të dy shërbimet. |

### GAP-09 — Menaxhimi i sekreteve

| Fusha | Përmbajtja |
|---|---|
| Gjendja aktuale | `.env` jashtë git (`.gitignore`, `.dockerignore`); `.env.example` ka vetëm placeholder-a; çelësi privat Ed25519 për CP montohet si skedar read-only. Nuk ka vault/KMS, skanim sekretesh në CI, as rrotullim të `SMS_SECRETS_KEY`/`SMS_PII_HMAC_KEY`. |
| Evidenca | `.gitignore`; `.env.example`; `docker-compose.prod.yml`; `docs/RUNBOOK.md` §7; `.github/workflows/ci.yml` (pa hap skanimi). |
| Klasifikimi | Higjiena e repo-së: VERIFIED. Vendi real i sekreteve, kush ka qasje: EXTERNAL EVIDENCE REQUIRED. Skanim sekretesh: NOT VERIFIED (mungon). |
| Rreziku | HIGH. Humbja e `SMS_SECRETS_KEY` ose `SMS_PII_HMAC_KEY` është e pariparueshme (s'mbështetet rrotullimi). |
| Parakusht | Plan i miratuar sekretesh (`docs/m11i-secrets-and-access-plan.md`) dhe vend ruajtjeje i zgjedhur. |
| Validim i kërkuar para prodhimit | Inventar i plotë i sekreteve i plotësuar me vendin dhe pronarët; provë që `SMS_SECRETS_KEY` ruhet veçmas nga backup-et; skanim histori git për sekrete (jashtë këtij faze) me 0 gjetje. |
| Veprimi në prodhim | Provision i sekreteve në vendin e miratuar; mountim në shërbime. |
| Rikthim / abort | Abort nëse ndonjë sekret gjendet në git, imazh ose log. Rikthim: rrotullo çdo sekret të ekspozuar (ku mund të rrotullohet). |
| Pronari / varësia | Sigurinë — NUK ËSHTË CAKTUAR (jashtë repo-së) |
| Kriteri GO/NO-GO | GO kur inventari është i plotë dhe skanimi jep 0 gjetje. |

### GAP-10 — Kredencialet e provider-it SMS dhe email

| Fusha | Përmbajtja |
|---|---|
| Gjendja aktuale | Provider-ët në kod: `fake`, `http`, `twilio`; email smtp/fake. `docs/TWILIO.md` pranon që adapteri s'është provuar kundër Twilio të vërtetë. `production_problems()` refuzon `fake` për email dhe pagesa, por nuk verifikon praninë e kredencialeve SMS apo të `SMS_DLR_SECRETS`. |
| Evidenca | `app/providers/__init__.py`; `docs/TWILIO.md`; `app/core/config.py` (`production_problems`); `.env.example`. |
| Klasifikimi | Gjendja e kodit: VERIFIED. Llogari/kredenciale reale: EXTERNAL EVIDENCE REQUIRED. Sjellja kundër provider-it real: NOT VERIFIED. |
| Rreziku | HIGH. |
| Parakusht | Llogari e provider-it, numra/sender të miratuar, geo-permissions, URL publik i arritshëm për callback-et, SMTP me SPF/DKIM. |
| Validim i kërkuar para prodhimit | Në mjedis jo-prodhimi me kredenciale sandbox të provider-it: një SMS pa `--confirm` (`twilio_smoke`) shfaq planin; vetëm pas miratimit shprehimisht një SMS provë te numër i verifikuar. |
| Veprimi në prodhim | Vendos kredencialet në vendin e miratuar; aktivizo rrugën për prefiksin përkatës. |
| Rikthim / abort | Abort nëse provider-i kthen gabim autentikimi ose rajon (`twilio_21608/21408`). Rikthim: çaktivizo rrugën (`enabled=false`) dhe pauzo `dispatch` nga kill switch. |
| Pronari / varësia | Pronari i llogarisë së provider-it — NUK ËSHTË CAKTUAR (jashtë repo-së) |
| Kriteri GO/NO-GO | GO kur kredencialet janë të vlefshme dhe prova e miratuar kalon. |

### GAP-11 — Canary provider/DLR real

| Fusha | Përmbajtja |
|---|---|
| Gjendja aktuale | Asnjë SMS/DLR real nuk është dërguar kurrë; M11-H përdori vetëm FakeProvider me DLR të simuluar të nënshkruar. Plani i canary ekziston te `docs/M10_PROD_OPERATIONS.md` §8 (nuk dërgohet). |
| Evidenca | M11-H (FakeProvider, 0 SMS reale); `docs/M10_PROD_OPERATIONS.md` §8; `docs/M9_PRODUCTION_CHECKLIST.md` §1 (provë idempotence e provider-it). |
| Klasifikimi | EXTERNAL EVIDENCE REQUIRED. |
| Rreziku | HIGH. |
| Parakusht | GAP-07 (URL publik HTTPS për callback), GAP-10, GAP-12 (monitorim aktiv). |
| Validim i kërkuar para prodhimit | Prova e idempotencës së provider-it; një canary me një marrës të miratuar: submit → radhë → provider → DLR 'delivered' i nënshkruar → faturim; kontroll `scripts.queue_readiness`. |
| Veprimi në prodhim | Canary me 1 mesazh, pastaj rritje graduale e miratuar nga operatori. |
| Rikthim / abort | Abort nëse DLR nuk mbërrin brenda pragut të miratuar ose mesazhi mbetet UNKNOWN. Rikthim: pauzo `dispatch`; çaktivizo rrugën. |
| Pronari / varësia | Operatori + pronari i provider-it — NUK ËSHTË CAKTUAR (jashtë repo-së) |
| Kriteri GO/NO-GO | GO kur canary-i kalon me 0 UNKNOWN dhe 0 dublime. |

### GAP-12 — Monitorim dhe alarme

| Fusha | Përmbajtja |
|---|---|
| Gjendja aktuale | Në repo: healthcheck-e Docker, `/healthz`, `/readyz`, `X-Request-ID`, CLI 'pull' (`sender_alerts`, `financial_ops`, `queue_readiness`, readiness) dhe alarme të llogaritura brenda aplikacionit. Nuk ka Prometheus/Alertmanager/pager, as formatim log-esh me latencë. |
| Evidenca | `docs/RUNBOOK.md` §5; `docs/M10_PROD_OPERATIONS.md` §4; `docs/m11i-observability-alerting-plan.md`. |
| Klasifikimi | Burimet në kod: VERIFIED. Sistem monitorimi dhe on-call: EXTERNAL EVIDENCE REQUIRED. Log latence nginx: NOT VERIFIED. |
| Rreziku | HIGH. |
| Parakusht | Platformë monitorimi e zgjedhur; kanal alarmesh; on-call. |
| Validim i kërkuar para prodhimit | Test alarmi me shkelje të provokuar në staging (p.sh. worker i ndaluar → alarm brenda pragut). |
| Veprimi në prodhim | Aktivizo alarmet sipas planit. |
| Rikthim / abort | Abort i cutover-it nëse alarmet kritike s'kanë pranues të konfirmuar. |
| Pronari / varësia | Operacione — NUK ËSHTË CAKTUAR (jashtë repo-së) |
| Kriteri GO/NO-GO | GO kur çdo alarm kritik ka pronar dhe test të suksesshëm. |

### GAP-13 — Evidenca e cutover-it në prodhim

| Fusha | Përmbajtja |
|---|---|
| Gjendja aktuale | Runbook-u sender (bootstrap → shadow → provë → ACK → central) u ushtrua vetëm në stack dev (M11-H). Shadow në trafik real, ACK `evidence_hash` real dhe rikthim i provuar nuk ekzistojnë. |
| Evidenca | `docs/M10_SENDER_RUNBOOK.md`; `docs/M10_PROD_OPERATIONS.md` §3–§10; M11-H (`prepare.sh`). |
| Klasifikimi | Në dev: VERIFIED. Në prodhim: EXTERNAL EVIDENCE REQUIRED. |
| Rreziku | HIGH. |
| Parakusht | GAP-01..GAP-12. |
| Validim i kërkuar para prodhimit | Çdo kriter i `docs/M10_PROD_OPERATIONS.md` §9 = PO; `python -m scripts.sender_cutover evidence` prodhon hash; rikthimi i validuar (`rollback --to shadow`). |
| Veprimi në prodhim | Hapat e §3–§8 sipas M10, vetëm me miratim shprehimisht. |
| Rikthim / abort | Çdo 'jo' te §9 = BLOCKED. |
| Pronari / varësia | Operatori + pronari i produktit — NUK ËSHTË CAKTUAR (jashtë repo-së) |
| Kriteri GO/NO-GO | GO kur §9 është plotësisht PO. |

### GAP-14 — Boshllëqet e CI

| Fusha | Përmbajtja |
|---|---|
| Gjendja aktuale | CI ka: ruff, bandit, pytest PG+SQLite, migrime up/down/up, backup/restore, Vitest + build Enterprise, E2E Enterprise, Docker build + `nginx -t`. Mungojnë: Vitest/build `frontend-central`, E2E Central, `docker compose config`, skanim sekretesh, ndërtim i imazhit Central. `pip-audit`/`npm audit` janë këshillues (`continue-on-error`). |
| Evidenca | `.github/workflows/ci.yml`. |
| Klasifikimi | VERIFIED (lexuar). |
| Rreziku | MEDIUM. |
| Parakusht | Miratim i F-04 (ndryshim CI). |
| Validim i kërkuar para prodhimit | Njëkohësisht me F-01..F-03: CI jeshile mbi një branch me paketimin Central. |
| Veprimi në prodhim | Shto hapat e munguar në CI. |
| Rikthim / abort | Rikthim: hiq hapin që është i paqëndrueshëm, jo testin. |
| Pronari / varësia | Inxhinieria — NUK ËSHTË CAKTUAR (jashtë repo-së) |
| Kriteri GO/NO-GO | GO kur CI mbulon Central dhe compose-in prodhimi. |

### GAP-15 — Validimi i compose-it të prodhimit

| Fusha | Përmbajtja |
|---|---|
| Gjendja aktuale | `docker-compose.prod.yml` nuk validohet në CI dhe nuk është nisur kurrë gjatë validimeve M11. `docs/M10_PROD_OPERATIONS.md` ka komandat `config --services`, por s'janë ekzekutuar në prodhim. |
| Evidenca | `.github/workflows/ci.yml`; `docs/M10_PROD_OPERATIONS.md` §1. |
| Klasifikimi | NOT VERIFIED. |
| Rreziku | MEDIUM. |
| Parakusht | Host staging (GAP-04). |
| Validim i kërkuar para prodhimit | `docker compose -f docker-compose.prod.yml --env-file <env-staging> config --services` dhe `up -d` në staging me kredenciale jo-prodhimi; healthy për të gjitha shërbimet; `readyz` = ready. |
| Veprimi në prodhim | Asnjë (vetëm validim). |
| Rikthim / abort | Abort nëse një shërbim del unhealthy. |
| Pronari / varësia | Inxhinieria + infrastruktura — NUK ËSHTË CAKTUAR (jashtë repo-së) |
| Kriteri GO/NO-GO | GO kur staging bëhet healthy sipas kriterit. |

### GAP-16 — Proceset e planifikuara të Central

| Fusha | Përmbajtja |
|---|---|
| Gjendja aktuale | Central nuk ka worker të përhershëm: `billing_run` (cron/systemd), `send_notifications` (`--loop` ose timer) dhe rakordimi janë mjete CLI. `CENTRAL_BILLING_WORKER_CONFIGURED` duhet vendosur nga operatori. |
| Evidenca | `docs/M9G_BILLING.md` (§21, bllokuesit operativë); `apps/central/tools/send_notifications.py`; `apps/central/core/config.py`. |
| Klasifikimi | Mjetet: VERIFIED. Planifikimi dhe monitorimi në prodhim: EXTERNAL EVIDENCE REQUIRED; ekzekutimi i tyre brenda një paketimi Central: NOT VERIFIED. |
| Rreziku | MEDIUM–HIGH (faturat/njoftimet nuk dalin pa to). |
| Parakusht | GAP-01. |
| Validim i kërkuar para prodhimit | Heartbeat i `billing_run` i freskët; `billing_final_readiness --json` PASS; `periods.due_unprocessed=0`. |
| Veprimi në prodhim | Instalo timer/cron/shërbim për secilin proces. |
| Rikthim / abort | Abort nëse `due_unprocessed` rritet. Rikthim: ndalo `billing_run` (M9G §rikthim). |
| Pronari / varësia | Operacione — NUK ËSHTË CAKTUAR (jashtë repo-së) |
| Kriteri GO/NO-GO | GO kur proceset janë planifikuar dhe të monitoruara. |

### GAP-17 — Mbrojtjet e nisjes që mungojnë (kod, veprim i ardhshëm)

| Fusha | Përmbajtja |
|---|---|
| Gjendja aktuale | `production_problems()` nuk kontrollon `SMS_DLR_SECRETS`, kredencialet e provider-it SMS ose përputhjen e `SMS_TRUSTED_PROXY_HOPS`. Central nuk ka 2FA për stafin (kërkim në kod, 0 përputhje). |
| Evidenca | `app/core/config.py`; `apps/central/` (kërkim për totp/2fa/twofactor). |
| Klasifikimi | PARTIALLY VERIFIED (nga kërkimi, jo nga testi). |
| Rreziku | MEDIUM. |
| Parakusht | Miratim i F-05. |
| Validim i kërkuar para prodhimit | Teste që provojnë refuzimin e nisjes në prodhim pa këto vlera. |
| Veprimi në prodhim | Zbatim i ardhshëm (jo në M11-I). |
| Rikthim / abort | — |
| Pronari / varësia | Inxhinieria + sigurinë — NUK ËSHTË CAKTUAR (jashtë repo-së) |
| Kriteri GO/NO-GO | Jo bllokues vetëm nëse kontrolli kompensohet procedurialisht në cutover (checklist). |

## 3. Kontradiktat e gjetura në dokumentacion

| # | Kontradikta | Evidenca | Klasifikim |
|---|---|---|---|
| C-1 | `docs/RUNBOOK.md` §7 përmend provider SMS 'HTTP/SMPP'; në `app/providers` ka vetëm `fake`, `http`, `twilio` (asnjë SMPP në `app/`) | `docs/RUNBOOK.md`; `app/providers/__init__.py`; kërkim në `app/` | VERIFIED |
| C-2 | Komenti i `scripts/backup.sh` thërret `/backup.sh` brenda kontejnerit `db`, por compose monton vetëm `./backups:/backups` (skripti nuk është i montuar) | `scripts/backup.sh`; `docker-compose.prod.yml` | PARTIALLY VERIFIED (nuk është ekzekutuar) |
| C-3 | Dokumentet thonë 'TLS përfundon në nginx ose LB', por `nginx.conf` dëgjon vetëm `8080` pa certifikata dhe dërgon HSTS | `deploy/nginx.conf`; `docs/RUNBOOK.md` §2 | VERIFIED |
| C-4 | Docstring-u i `app/worker.py` liston vetëm një pjesë të roleve; kodi pranon edhe `pricing_control_plane`, `billing_usage_reporter`, `money_usage_reporter` | `app/worker.py` | VERIFIED (kozmetike) |
| C-5 | `docs/M10_PROD_OPERATIONS.md` kokat 0032/0029 në kohën e shkrimit; aktualet 0034/0031 (dokumenti e pranon) | `docs/M10_PROD_OPERATIONS.md` | VERIFIED (e shënuar) |
| C-6 | `.env.example` këshillon përdorues PostgreSQL me të drejta minimale (jo superuser); compose lidh aplikacionin si `sms`, përdoruesi i krijuar nga `POSTGRES_USER` në imazhin zyrtar, që është superuser | `.env.example`; `docker-compose.prod.yml` | PARTIALLY VERIFIED (nga semantika e imazhit; s'është ekzekutuar) |

## 4. Tabela e bllokuesve sipas varësisë

Rendi është i detyrueshëm: një rresht nuk mund të mbyllet para varësive të tij.

| Rendi | Bllokues | Lloji | Varet nga | Evidenca që mbyll |
|---|---|---|---|---|
| 1 | GAP-04 Infrastruktura | EXTERNAL | — | Inventar i firmosur i hostit |
| 2 | GAP-07 TLS/DNS/LB | EXTERNAL | 1 | `curl`/`openssl` nga jashtë |
| 3 | GAP-08 Trusted proxy hops | EXTERNAL + konfigurim | 2 | Test me dy IP |
| 4 | GAP-09 Sekretet | EXTERNAL | 1 | Inventar + skanim 0 gjetje |
| 5 | GAP-05 PG prodhimi, backup, PITR | EXTERNAL | 1, 4 | Restore + `verify_ledger` + PITR i provuar |
| 6 | GAP-01 Central backend | Kod (F-01, kërkon miratim) | — | Imazh + migrim 0031 + `/readyz` në staging |
| 7 | GAP-02 Central frontend | Kod (F-02) | 6 | Build nga imazhi, 0 gabime console |
| 8 | GAP-03 Proxy për Central | Kod/konfigurim (F-03) | 2, 6 | Matrica e rrugëve |
| 9 | GAP-14 CI, GAP-15 compose | Kod (F-04) | 6, 7 | CI jeshile; staging healthy |
| 10 | GAP-17 Mbrojtje nisjeje/2FA Central | Kod (F-05) | 6 | Teste refuzimi |
| 11 | GAP-06 Backup/restore Central | EXTERNAL | 5, 6 | Restore Central i provuar |
| 12 | GAP-12 Monitorim/alarme | EXTERNAL | 1 | Test alarmi i kaluar |
| 13 | GAP-16 Procese të planifikuara Central | EXTERNAL | 6, 12 | Heartbeat + `due_unprocessed=0` |
| 14 | GAP-10 Kredenciale provider | EXTERNAL | 4 | Prova sandbox e miratuar |
| 15 | GAP-11 Canary provider/DLR | EXTERNAL | 2, 10, 12 | Canary pa UNKNOWN |
| 16 | GAP-13 Evidenca e cutover-it | EXTERNAL | Të gjitha më sipër | `docs/M10_PROD_OPERATIONS.md` §9 plotësisht PO |

## 5. Përfundimi

**BLOCKED.** 17 boshllëqe në 16 rreshta varësie; 11 rreshta kërkojnë dëshmi të jashtme, 5 kërkojnë zbatim të miratuar në kod/konfigurim. Asnjë nuk mbyllet me dokumentim. Statusi mund të kalojë në CONDITIONALLY READY vetëm kur rreshtat 1–11 janë të mbyllur me evidencë, dhe në PRODUCTION READY vetëm pas rreshtit 16.
