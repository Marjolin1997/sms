# M11-I — Runbook i rikthimit në prodhim (NUK ekzekutohet)

**Revision i bazës:** `6020026`. Procedurë e shkruar paraprakisht; **nuk është provuar në prodhim dhe nuk ekzekutohet në M11-I**. Statusi: **BLOCKED** (infrastruktura dhe backup-et mungojnë; shih `m11i-production-readiness-gap-analysis.md`).

## 0. Parime

- **Kufizim i njohur (VERIFIED, `docs/RUNBOOK.md` §3, M11-H):** `alembic downgrade` heq tabelat e shtuara pas objektivit me të dhënat e tyre. Në prodhim **nuk përdoret downgrade si rikthim**. Rikthimi i të dhënave = **imazhi i vjetër + backup i verifikuar të restauruar në DB të re**.
- Tabelat e provave (append-only) dhe ledger-i janë të pandryshueshme me qëllim; korrigjimet bëhen me regjistrime të reja, jo me fshirje.
- Rikthimi i konfigurimit (authority, kill switch, rrugë) nuk prek të dhënat dhe është i preferuari kur mjafton.
- Asnjë sekret nuk shfaqet gjatë rikthimit; çdo komandë me `.env` printohet pa vlera.
- Çdo rikthim hapet si incident me orë, shkaku dhe dëshmitë.

## 1. Shkaktarët (triggers) dhe pragjet e abort-it

Pragjet janë **PROPOSED** (asnjë nuk është verifikuar në prodhim) përveç ku shënohet.

| ID | Shkaktari | Prag | Veprimi i parë | Klas. |
|---|---|---|---|---|
| RT-01 | `readyz` ≠ ready pas nisjes | > 5 min | §3 Rikthim aplikacioni | PROPOSED; endpoint VERIFIED |
| RT-02 | Migrimi dështon | çdo gabim | §4 (pa downgrade); mos nis api/workers | VERIFIED (mekanizmi) |
| RT-03 | 5xx në API | ≥ 5% e kërkesave për 5 min | §3 | PROPOSED |
| RT-04 | `verify_ledger` FAIL ose `financial_ops` CRITICAL | çdo rast | Pauzo `dispatch` + `submit`; §5 nëse ledger-i është i dëmtuar | VERIFIED (mjetet); pragu PROPOSED |
| RT-05 | Mesazhe UNKNOWN/SENDING të ngecur | `queue_readiness` FAIL, ose UNKNOWN > 1 orë | §6 Rikthim provider-i | VERIFIED (mjeti); pragu PROPOSED |
| RT-06 | Dublim faturimi/ledger | çdo rast | Pauzo `dispatch`; §5; hap incident financiar | PROPOSED |
| RT-07 | Dyshim për sekret të ekspozuar | çdo rast | Rrotullo sekretin (shih `m11i-secrets-and-access-plan.md`); §3 nëse duhet | PROPOSED |
| RT-08 | 429 masiv për përdorues legjitimë (parametra proxy të gabuar) | > 3 përdorues të bllokuar | §7 (rikthe vlerën e hops dhe rinis) | VERIFIED (sjellja, M11-H) |
| RT-09 | TLS/DNS i prishur | zinxhir i pavlefshëm ose NXDOMAIN | §7 | PROPOSED |
| RT-10 | Central i paarritshëm pas cutover-it të autoritetit | sender_sync_lag/`sender_projection_stale` kritik | §8: rikthe authority sipas runbook-ut M10 | VERIFIED (mekanizmi, M10) |

## 2. Vendimi: cilin rikthim të zgjedhim

| Fusha | Përmbajtja |
|---|---|
| Rregulli | Zgjidh rikthimin më të ngushtë që heq shkaktarin: (a) kill switch/rrugë (§6) → (b) konfigurim authority (§8) → (c) imazh i vjetër (§3) → (d) restore DB (§5). |
| Klas. | VERIFIED që mekanizmat (a)–(d) ekzistojnë; EXTERNAL EVIDENCE REQUIRED që janë provuar në prodhim. |
| Pronari | Operatori + pronari i produktit — NUK ËSHTË CAKTUAR |
| GO/NO-GO | Çdo rikthim i nivelit (d) kërkon miratimin e dy personave. |

## 3. Rikthimi i aplikacionit (imazh i vjetër)

| Fusha | Përmbajtja |
|---|---|
| Gjendja | Imazhet ndërtohen nga `Dockerfile`/`deploy/web.Dockerfile`; tag-et e imazhit të mëparshëm nuk janë të regjistruara në repo (NOT VERIFIED që ruhen në prodhim). |
| Klasifikimi | PARTIALLY VERIFIED (compose ndërton nga burimi; nuk ka regjistër imazhesh në repo). |
| Rreziku | Imazhi i vjetër mund të mos përputhet me skemën e migruar; skema e re është përpara-përputhshme vetëm nëse migrimet ishin aditive (të dhënat e M11-G/H: 0034 aditive; nuk është garantuar për të ardhmen). |
| Parakusht | Tag/commit i mëparshëm i regjistruar; imazh i vjetër i ruajtur ose i rindërtueshëm nga tag-u. |
| Validim para prodhimit | Në staging: ngarko imazhin e vjetër mbi skemën e re; `readyz` duhet të jetë ready ose të dështojë qartë (503). |
| Veprimi | 1) `git checkout <tag_i_mëparshëm>` në host. 2) `docker compose -f docker-compose.prod.yml --env-file .env up -d --build api worker webhook-worker web` (PROPOSED: pa `migrate`). 3) Verifiko §9. |
| Abort / rikthim i rikthimit | Nëse `readyz` kthen 503 për shkak të skemës: mos e ndryshosh skemën me downgrade; kalo te §5 (restore në DB të re me backup-in para-migrimit). |
| Pronari | Operatori — NUK ËSHTË CAKTUAR |
| GO/NO-GO | Rikthimi mbyllet vetëm kur §9 kalon. |

## 4. Rikthimi i migrimit / të dhënave

| Fusha | Përmbajtja |
|---|---|
| Gjendja | Enterprise 0034, Central 0031 (VERIFIED). `alembic downgrade` ekziston, por humb të dhëna për tabelat e shtuara më vonë (VERIFIED). |
| Klasifikimi | VERIFIED (kufizimi); rikthimi në prodhim EXTERNAL EVIDENCE REQUIRED. |
| Rreziku | HIGH: humbje e pakthyeshme e të dhënave të tabelave të reja. |
| Parakusht | Backup i para-migrimit i provuar (DB-05/DB-06 të checklist-it). |
| Validim para prodhimit | Në staging: migrim → shto të dhëna → rikthe me restore (jo downgrade) → krahaso numrat. |
| Veprimi | NUK përdorim `alembic downgrade` në prodhim. Nëse migrimi dështoi: ndalo; mos nis api/workers; shko te §5. |
| Abort | Çdo tentativë downgrade në prodhim pa miratim të dy personave = NO-GO. |
| Pronari | DBA — NUK ËSHTË CAKTUAR |
| GO/NO-GO | GO vetëm me backup të provuar. |

## 5. Restore i DB-së

| Fusha | Përmbajtja |
|---|---|
| Gjendja | `scripts/restore.sh` krijon DB të RE (refuzon mbishkrimin) dhe kontrollon checksum; `scripts/verify_ledger.py` verifikon librin (VERIFIED, CI round-trip). |
| Klasifikimi | VERIFIED (skriptet); në prodhim EXTERNAL EVIDENCE REQUIRED. |
| Rreziku | Humbje e të dhënave të krijuara pas backup-it (RPO = koha nga backup-i i fundit, ose nga PITR nëse ekziston; PITR NOT VERIFIED). |
| Parakusht | Dump + `.sha256` të vlefshme; ruajtja off-host e arritshme; `SMS_SECRETS_KEY`/`SMS_PII_HMAC_KEY` të njëjtë me ata të dump-it (të ruajtur veçmas). |
| Validim para prodhimit | Restore muaj-pas-muaji i provuar (RUNBOOK §4). |
| Veprimi | 1) Ndalo `api` dhe workers. 2) `ADMIN_URL=postgresql://…/postgres scripts/restore.sh <dump_enterprise> sms_restored` (dump nga `BACKUP_LABEL=enterprise`). 3) `SMS_DATABASE_URL=postgresql+psycopg://…/sms_restored python -m scripts.verify_ledger` → PASS. 4) Ndrysho emrin e DB-së te `SMS_DATABASE_URL`/compose. 5) Nis shërbimet. Central: `BACKUP_LABEL=central`; restore me `scripts/restore.sh <dump_central> <db_me_emër_që_përmban_central>` (një dump Central refuzohet te emër pa `central`; një dump Enterprise te emër me `central`), pastaj `python -m scripts.recovery_verify head --label central` dhe `python -m apps.central.tools.recovery_ledger` (shih `docs/m11i-postgres-backup-restore-runbook.md`). |
| Abort | `verify_ledger` FAIL ose checksum FAILED: mos kalo shërbimet te baza e restauruar. |
| Pronari | DBA — NUK ËSHTË CAKTUAR |
| GO/NO-GO | GO kur `verify_ledger` PASS dhe `readyz` ready. |

## 6. Rikthimi i provider-it

| Fusha | Përmbajtja |
|---|---|
| Gjendja | Kill switches (`dispatch`, `submit`) te konsola (Admin → Kill switches), `PUT /v1/admin/routes` me `enabled=false` (kërkon 2FA), pauzë e klientit (Llogaritë → Ndalo dërgimin). Të gjitha VERIFIED në RUNBOOK/TWILIO. |
| Klasifikimi | VERIFIED (mekanizmat); me provider real EXTERNAL EVIDENCE REQUIRED. |
| Rreziku | SMS e dërguar nuk kthehet; mesazhet UNKNOWN nuk lirohen automatikisht (vetëm stafi ose një DLR autoritativ). |
| Parakusht | Qasje stafi me 2FA. |
| Validim para prodhimit | Në staging: pauzo `dispatch` → mesazhet mbledhen në radhë → rifillo → dalin. |
| Veprimi | 1) Pauzo `dispatch` (dhe `submit` nëse duhet). 2) Çaktivizo rrugën e provider-it — **kujdes (F-07A, provuar): kjo NUK ridrejton mesazhet që janë tashmë QUEUED** (mbeten të lidhura me provider-in e tyre; s'ka API anulimi për mesazh të vetëm) ⇒ mbaji me `dispatch` të pauzuar derisa të vendosë njeriu. Plani vetëm-lexim: `python -m scripts.provider_readiness rollback-plan --provider <emri>`; procedura e plotë: `m11i-canary-cutover-runbook.md` §5. 3) Nëse kredencialet dyshohen: rrotullo te provider-i dhe hiq nga `.env`. 4) Trajto UNKNOWN sipas `docs/M9_MONEY_AUDIT.md`/queue readiness. |
| Abort | — |
| Pronari | Operatori + pronari i provider-it — NUK ËSHTË CAKTUAR |
| GO/NO-GO | `queue_readiness` pa FAIL; asnjë mesazh i ri në `sending`. |

## 7. Rikthimi i DNS / proxy / TLS / HSTS

| Fusha | Përmbajtja |
|---|---|
| Gjendja | Nginx HTTP-only (`127.0.0.1:8080`, `:8081`, `:8082`); CIDR-t e besuara vijnë nga `.env` (`WEB_TRUSTED_PROXY_CIDRS`, `CENTRAL_WEB_TRUSTED_PROXY_CIDRS`, `CENTRAL_S2S_ALLOW_CIDRS`, `*_INTERNAL_ALLOW_CIDRS`); HSTS vetëm te terminatori i TLS. DNS/LB/TLS nuk janë në repo. |
| Klasifikimi | Mekanizmat: VERIFIED LOKALISHT (M11-I/F-02). Rikthimi real: EXTERNAL EVIDENCE REQUIRED. |
| Rreziku | DNS me TTL të gjatë vonon rikthimin; HSTS i dhënë nga terminatori mbahet mend nga shfletuesit deri në skadimin e `max-age`; CIDR e gabuar e proxy-t jep IP të gabuar (kyçje masive) ose besim të tepërt. |
| Parakusht | TTL i ulur para cutover-it (PROPOSED ≤ 300 s); `.env` dhe konfigurimi i terminatorit të mëparshëm të ruajtur; HSTS i filluar me `max-age` të ulët (RUNBOOK §2b). |
| Validim para prodhimit | Provë e rikthimit të DNS dhe e CIDR-ve në staging; `python3 e2e/edge/check_edge.py`. |
| Veprimi | **DNS/LB:** ktheje te objektivi i mëparshëm. **CIDR/IP e gabuar (429 masiv ose IP e proxy-t te log-et):** korrigjo CIDR-n te `.env` dhe `docker compose … up -d --force-recreate web central-web` (nginx e rigjeneron skedarin në nisje; pa ndryshim kodi). **HSTS:** te terminatori `max-age=0`; asgjë te nginx/aplikacioni. **Enterprise→Central:** korrigjo `CENTRAL_S2S_ALLOW_CIDRS` (bosh = askush, i sigurt) dhe rinis `central-web`; workers CP riprovojnë (fail-static). **Rikthim imazhi nginx:** `up -d --force-recreate` me imazhin e mëparshëm. |
| Abort | Çdo ndryshim CIDR që e bën `/readyz` ose `/internal` të arritshëm nga publiku. |
| Pronari | Infrastruktura — NUK ËSHTË CAKTUAR |
| GO/NO-GO | Provat §9 dhe EG-03/EG-05/EG-08 të checklist-it kalojnë. |

## 8. Rikthimi i workerëve dhe i autoriteteve

| Fusha | Përmbajtja |
|---|---|
| Gjendja | Workers opsionalë me profile (`control-plane`, `money-sync`, `money-report`, `billing-report`, `pricing-sync`, `sender`). Rikthimet e authority janë të dokumentuara: sender (`scripts.sender_cutover rollback --to shadow|local`, `reauthorize_risk`), para (`SMS_MONEY_AUTHORITY=shadow` ose emergjencë `local`), çmime (`SMS_PRICING_AUTHORITY=local`), faturim (M9G §rikthim). |
| Klasifikimi | VERIFIED (mekanizmat, M9/M10); në prodhim EXTERNAL EVIDENCE REQUIRED. |
| Rreziku | Rikthimi i sender-it në `local` bllokohet nëse një sender i miratuar lokalisht është refuzuar/revokuar nga Central; rikthimi money→`local` hap mint-in lokal dhe kërkon baseline të ri. |
| Parakusht | Dëshmia `rollback` vetëm-lexim e kryer para cutover-it. |
| Validim para prodhimit | `python -m scripts.sender_cutover rollback --to shadow` (vetëm-lexim) raporton rrezikun. |
| Veprimi | Workers: `docker compose -f docker-compose.prod.yml --profile sender stop sender-sync sender-report` (M10 §1). Authority: kthe vlerën në `.env` dhe rinis `api` + workers. Mos e ndryshosh `SMS_ENTERPRISE_DUAL_WRITE` kur skopimi është `enterprise` (RUNBOOK §6b). |
| Abort | Rikthimi bllokohet nga një kontroll `reauthorize_risk`: ndiq rrugën e sigurt shadow → `--reconcile-local` sipas M10. |
| Pronari | Operatori — NUK ËSHTË CAKTUAR |
| GO/NO-GO | `sender_cutover status` dhe readiness pa FAIL. |

## 9. Verifikimi pas rikthimit

Çdo rresht me provë; dëshmitë ngjiten në incident.

| # | Prova | Pritet | Klas. |
|---|---|---|---|
| V-1 | `curl -s http://127.0.0.1:8080/readyz` | `{"status":"ready"}` | VERIFIED (endpoint) |
| V-2 | `docker compose -f docker-compose.prod.yml ps` | db, api, worker, webhook-worker, web = healthy | NOT VERIFIED në prodhim |
| V-3 | `python -m scripts.verify_ledger` (kundër bazës aktive) | PASS | VERIFIED (mjeti) |
| V-4 | `python -m scripts.queue_readiness --json` | 0 FAIL | VERIFIED |
| V-5 | `python -m scripts.financial_ops --json` | 0 CRITICAL | VERIFIED |
| V-6 | `python -m scripts.sender_cutover status` | autoriteti i pritur (`local`/`shadow`) | VERIFIED (mjeti) |
| V-7 | `python -m scripts.worker_health 90` te çdo worker | kod daljeje 0 | VERIFIED |
| V-8 | Test me dy IP (nëse u prek proxy-ja) | 429 vetëm për IP-në që gabon | VERIFIED (sjellja); prodhimi EXTERNAL |
| V-9 | Mesazh provë (vetëm nëse `dispatch` është i hapur dhe me miratim) | `delivered` me DLR | EXTERNAL EVIDENCE REQUIRED |

## 10. Përfundimi

Rikthimi mbyllet kur §9 kalon dhe incidenti ka shkakun, orën dhe dëshmitë. Pa infrastrukturë, backup të provuar dhe PITR, ky runbook mbetet **i paprovuar në prodhim: BLOCKED**.
