# M11-I — Checklist i cutover-it në prodhim

**Revision i bazës:** `6020026`. Checklist për operatorin; **nuk është plotësuar dhe nuk ekzekutohet në M11-I**. Statusi aktual: **BLOCKED** (çdo kuti mbetet bosh). Seksioni CENTRAL është përditësuar pas M11-I/F-01 (paketimi: IMPLEMENTED / LOCALLY VERIFIED; CI PENDING).

**Si përdoret:** secila kuti ka një kriter objektiv: *Prova* (komanda/vëzhgimi) dhe *Pritet* (rezultati i saktë). Një kuti shënohet vetëm kur dalja reale është ngjitur në regjistrin e ndryshimit. Klasifikimi tregon nëse hapi mbështetet në kod të provuar (**VERIFIED**/**PARTIALLY VERIFIED**), është i paprovuar (**NOT VERIFIED**) ose kërkon dëshmi të jashtme (**EXTERNAL EVIDENCE REQUIRED**). Komandat me **PROPOSED**/**FUTURE** nuk ekzistojnë ende ose s'janë provuar. Çdo kuti e pashënuar = NO-GO.

Formati: `[ ] ID — kërkesa | Prova | Pritet | Klas. | NO-GO/abort`

## 1. PRE-CUTOVER

- [ ] **PC-01** — Revision i caktuar dhe CI jeshile.  
  Prova: `git rev-parse HEAD`; statusi CI i atij commit-i  
  Pritet: SHA i regjistruar; CI = success  
  Klas.: VERIFIED (mekanizmi); rezultati EXTERNAL  
  NO-GO/abort: SHA ndryshon pas regjistrimit → NO-GO
- [ ] **PC-02** — Pema pa ndryshime.  
  Prova: `git status --short`  
  Pritet: dalje bosh  
  Klas.: VERIFIED  
  NO-GO/abort: çdo rresht → NO-GO
- [ ] **PC-03** — Kokat e migrimeve.  
  Prova: `alembic heads` (Enterprise); Central: PROPOSED `alembic -c apps/central/alembic.ini heads`  
  Pritet: `0034` dhe `0031`  
  Klas.: VERIFIED (M11-H)  
  NO-GO/abort: kokë tjetër → NO-GO
- [ ] **PC-04** — Të gjitha bllokuesit GAP-01..GAP-17 të mbyllur me dëshmi.  
  Prova: Referenca te `m11i-production-readiness-gap-analysis.md` (rreshtat 1–16 të tabelës së varësive)  
  Pritet: çdo rresht ka dëshmi të bashkëngjitur  
  Klas.: EXTERNAL EVIDENCE REQUIRED  
  NO-GO/abort: një rresht pa dëshmi → NO-GO
- [ ] **PC-05** — Prova e plotë në staging e të njëjtit revision.  
  Prova: Raport i ekzekutimit të stage-it (nisja, migrimet, readyz, canary me sandbox)  
  Pritet: raport i firmosur, 0 gabime  
  Klas.: EXTERNAL EVIDENCE REQUIRED  
  NO-GO/abort: mungon → NO-GO
- [ ] **PC-06** — Operator dhe zëvendës të emëruar; dritare ndryshimi e miratuar.  
  Prova: Emrat dhe ora në regjistër  
  Pritet: dy emra + dritare  
  Klas.: EXTERNAL EVIDENCE REQUIRED  
  NO-GO/abort: mungon → NO-GO
- [ ] **PC-07** — Rollback runbook i lexuar dhe i konfirmuar.  
  Prova: Konfirmim me shkrim nga operatori  
  Pritet: konfirmim i regjistruar  
  Klas.: EXTERNAL EVIDENCE REQUIRED  
  NO-GO/abort: mungon → NO-GO

## 2. DB / BACKUP

- [ ] **DB-01** — Backup i ri i Enterprise DB.  
  Prova: `scripts/backup.sh` (me `DATABASE_URL`, `BACKUP_DIR`)  
  Pritet: rreshti `OK <rruga> (<madhësia>)` dhe kod daljeje 0  
  Klas.: VERIFIED (skripti); prodhimi EXTERNAL  
  NO-GO/abort: dështim → NO-GO
- [ ] **DB-02** — Checksum i backup-it.  
  Prova: `sha256sum -c <dump>.sha256`  
  Pritet: `OK`  
  Klas.: VERIFIED  
  NO-GO/abort: FAILED → NO-GO
- [ ] **DB-03** — Backup i ri i Central DB.  
  Prova: `scripts/backup.sh` me `DATABASE_URL` të Central  
  Pritet: rreshti `OK …`  
  Klas.: PARTIALLY VERIFIED (skripti gjenerik; s'është dokumentuar për Central)  
  NO-GO/abort: dështim → NO-GO
- [ ] **DB-04** — Kopje off-host e të dy backup-eve.  
  Prova: Listim i objektit në ruajtjen e jashtme me madhësi dhe checksum  
  Pritet: madhësia dhe checksum përputhen me origjinalin  
  Klas.: EXTERNAL EVIDENCE REQUIRED  
  NO-GO/abort: mungon → NO-GO
- [ ] **DB-05** — Restore i provuar i Enterprise.  
  Prova: `ADMIN_URL=… scripts/restore.sh <dump> <db_e_re>` pastaj `SMS_DATABASE_URL=… python -m scripts.verify_ledger`  
  Pritet: `OK restored` dhe `verify_ledger` PASS  
  Klas.: VERIFIED (CI round-trip); prodhimi EXTERNAL  
  NO-GO/abort: FAIL → NO-GO
- [ ] **DB-06** — Restore i provuar i Central.  
  Prova: Restore në DB të re + `docker compose … --profile central-jobs run --rm central-tool apps.central.tools.billing_readiness --strict` (komandë e inventarit Central; `financial_readiness` s'punon në imazhin Central — shih CE-07)  
  Pritet: pa FAIL; `central_alembic_version` = 0031  
  Klas.: NOT VERIFIED  
  NO-GO/abort: FAIL ose mungon → NO-GO
- [ ] **DB-07** — PITR/WAL aktiv dhe i provuar.  
  Prova: Rikthim në një kohë të caktuar në një instancë të veçantë; koha e kërkuar dhe koha e arritur të regjistruara  
  Pritet: koha e arritur = koha e kërkuar (në kufirin e WAL-it)  
  Klas.: EXTERNAL EVIDENCE REQUIRED  
  NO-GO/abort: mungon → NO-GO
- [ ] **DB-08** — Roli i aplikacionit pa superuser; TLS në lidhje.  
  Prova: `SELECT rolsuper FROM pg_roles WHERE rolname='<roli>'`; `sslmode=require` te URL  
  Pritet: `f`; URL përmban `sslmode=require`  
  Klas.: EXTERNAL EVIDENCE REQUIRED  
  NO-GO/abort: `t` ose pa TLS → NO-GO

## 3. INFRA

- [ ] **IN-01** — Inventar i hostit i firmosur.  
  Prova: Dokument: OS, Docker/Compose version, CPU/RAM/disk  
  Pritet: dokument i firmosur  
  Klas.: EXTERNAL EVIDENCE REQUIRED  
  NO-GO/abort: mungon → NO-GO
- [ ] **IN-02** — Compose i vlefshëm në host.  
  Prova: `docker compose -f docker-compose.prod.yml --env-file .env config --services` (pa profile)  
  Pritet: vetëm Enterprise: `api db migrate web webhook-worker worker`, pa gabim dhe **pa kërkuar `.env.central`**  
  Klas.: VERIFIED LOKALISHT (rendering me `docker compose config`); në hostin real EXTERNAL EVIDENCE REQUIRED  
  NO-GO/abort: gabim → NO-GO
- [ ] **IN-03** — Vetëm portat e pritura të hapura.  
  Prova: Skanim nga rrjet i jashtëm (p.sh. `nmap` ose rregullat e firewall-it të eksportuara)  
  Pritet: vetëm 80/443 (dhe SSH nga IP të lejuara)  
  Klas.: EXTERNAL EVIDENCE REQUIRED  
  NO-GO/abort: çdo port tjetër → NO-GO
- [ ] **IN-04** — DNS zgjidhet te IP-ja e pritur.  
  Prova: `dig +short <domen>`  
  Pritet: IP e planifikuar  
  Klas.: EXTERNAL EVIDENCE REQUIRED  
  NO-GO/abort: ndryshe → NO-GO
- [ ] **IN-05** — Certifikata e vlefshme dhe ≥ 30 ditë (PROPOSED).  
  Prova: `openssl s_client -connect <domen>:443 -servername <domen> </dev/null 2>/dev/null | openssl x509 -noout -dates`  
  Pritet: `notAfter` ≥ sot + 30 ditë; zinxhir i vlefshëm  
  Klas.: EXTERNAL EVIDENCE REQUIRED  
  NO-GO/abort: më pak se 30 ditë ose zinxhir i prishur → NO-GO
- [ ] **IN-06** — IP e klientit e lexuar saktë.  
  Prova: Dy kërkesa nga dy IP të ndryshme të jashtme; kontroll te audit/log  
  Pritet: IP-të e regjistruara = IP-të reale  
  Klas.: EXTERNAL EVIDENCE REQUIRED  
  NO-GO/abort: IP e proxy-t → NO-GO
- [ ] **IN-07** — Disk dhe memorie të mjaftueshme.  
  Prova: `df -h`, `free -m` në host  
  Pritet: ≥ 30% disk i lirë; swap pa përdorim të vazhdueshëm (PROPOSED)  
  Klas.: EXTERNAL EVIDENCE REQUIRED  
  NO-GO/abort: nën prag → NO-GO

## 3b. EDGE (TLS / DNS / reverse proxy / IP e klientit — M11-I/F-02)

Modeli: terminator i jashtëm i TLS; nginx HTTP-only; `hops=1` pas `real_ip`; HSTS vetëm te terminatori. Prova lokale: `python3 e2e/edge/check_edge.py` (64 kontrolle, nginx real) dhe compose real; **asnjë provë me terminator, DNS, certifikatë ose CIDR real nuk ekziston**.

- [ ] **EG-01** — Imazhet e lëshimit kalojnë provën e skajit.  
  Prova: `EDGE_WEB_IMAGE=<imazhi web> EDGE_CENTRAL_WEB_IMAGE=<imazhi web central> python3 e2e/edge/check_edge.py`  
  Pritet: `… checks passed, 0 failed`  
  Klas.: VERIFIED LOKALISHT; CI PENDING  
  NO-GO/abort: ndonjë FAIL → NO-GO
- [ ] **EG-02** — CIDR-t e proxy-ve të besuara janë të dhëna nga operatori, jo `/0`, jo bosh kur ka proxy.  
  Prova: `docker compose … logs web central-web | grep edge-cidrs` dhe `.env` (vetëm emrat/vlerat jo-sekrete)  
  Pritet: `trusted_proxies=N` me N ≥ 1 sipas topologjisë; `WEB_TRUSTED_PROXY_CIDRS`/`CENTRAL_WEB_TRUSTED_PROXY_CIDRS` = rrjetet e terminatorit; asnjë `/0`  
  Klas.: VERIFIED LOKALISHT (mekanizmi); vlerat reale EXTERNAL EVIDENCE REQUIRED  
  NO-GO/abort: bosh me terminator para nginx (IP e gabuar, kyçje masive) ose CIDR e gjerë → NO-GO
- [ ] **EG-03** — IP e klientit e saktë përmes terminatorit real (dy IP të jashtme).  
  Prova: nga IP-A dërgo 21 çelësa të gabuar te `https://enterprise.<domen>/v1/me`; pastaj nga IP-B një kërkesë me çelës të vlefshëm; kontrollo log-un/audit për IP-të  
  Pritet: 429 vetëm për IP-A; IP-B pranohet; IP-të e regjistruara = IP-të reale; `SMS_TRUSTED_PROXY_HOPS=1` dhe `CENTRAL_TRUSTED_PROXY_HOPS=1`  
  Klas.: EXTERNAL EVIDENCE REQUIRED  
  NO-GO/abort: IP-ja e terminatorit te log-u ose IP-B e bllokuar → NO-GO
- [ ] **EG-04** — `X-Forwarded-Proto` i besuar.  
  Prova: një kërkesë përmes terminatorit dhe një direkt te nginx me `X-Forwarded-Proto: https` të falsifikuar (nga IP jo e besuar)  
  Pritet: aplikacioni sheh `https` vetëm te e para; `http` te e dyta  
  Klas.: VERIFIED LOKALISHT (nginx real); terminatori real EXTERNAL  
  NO-GO/abort: falsifikimi pranohet → NO-GO
- [ ] **EG-05** — HSTS absent nga origjina.  
  Prova: `curl -sI http://127.0.0.1:8080/ http://127.0.0.1:8080/assets/x.js http://127.0.0.1:8081/ | grep -i strict-transport`  
  Pritet: asnjë rresht; HSTS te terminatori vetëm sipas RUNBOOK §2b (fillon `max-age=300`)  
  Klas.: VERIFIED LOKALISHT; aktivizimi te terminatori EXTERNAL EVIDENCE REQUIRED  
  NO-GO/abort: HSTS nga origjina ose `includeSubDomains`/`preload` para konfirmimit → NO-GO
- [ ] **EG-06** — Health publik minimal.  
  Prova: nga jashtë `curl -s -o /dev/null -w '%{http_code}' https://<domen>/readyz` dhe `/healthz`; LB provon `/nginx-health`  
  Pritet: 403 për `/readyz` dhe `/healthz`; 200 për `/nginx-health`  
  Klas.: VERIFIED LOKALISHT; përmes domenit real EXTERNAL EVIDENCE REQUIRED  
  NO-GO/abort: `/readyz` 200 publikisht → NO-GO
- [ ] **EG-07** — Callback-et e provider-it arrijnë aplikacionin pa ndryshuar path/query/trup/nënshkrim.  
  Prova: callback i nënshkruar nga provider-i (ose sandbox) te `https://enterprise.<domen>/webhooks/…`; `SMS_PUBLIC_BASE_URL` = origjina publike  
  Pritet: 200 `applied`; nënshkrim i pasaktë 401; asnjë 429/503 gjatë batch-it të pritur  
  Klas.: VERIFIED LOKALISHT (proxy + firma); provider real EXTERNAL EVIDENCE REQUIRED  
  NO-GO/abort: 401 për nënshkrim të vlefshëm ose 429 gjatë batch-it legjitim → NO-GO
- [ ] **EG-08** — Rruga e brendshme Enterprise→Central.  
  Prova: `SMS_CP_BASE_URL=https://central-internal.<domen>`; nga Enterprise një kërkesë me kredencial shërbimi te `/internal/sync/changes`; nga një IP tjetër e jashtme e njëjta  
  Pritet: nga Enterprise i lejuar: 200 (nënshkrimi i vlefshëm); nga IP tjetër 403; `https://central.<domen>/internal/…` 404; `CENTRAL_S2S_ALLOW_CIDRS` = IP-ja e Enterprise ashtu siç e sheh nginx  
  Klas.: VERIFIED LOKALISHT (allowlist + 401 nga `central-api` pa token); rruga reale EXTERNAL EVIDENCE REQUIRED  
  NO-GO/abort: `/internal` i arritshëm nga publiku → NO-GO
- [ ] **EG-09** — Kufizimi kthen 429 dhe nuk bllokon callback-et.  
  Prova: 400 kërkesa paralele te `/v1/…`; 300 te `/webhooks/dlr/…`  
  Pritet: 429 te e para, asnjë 429/503 te e dyta (vlerat `perip`/`hooks` janë PROPOSED; rregullohen sipas trafikut real)  
  Klas.: VERIFIED LOKALISHT  
  NO-GO/abort: 503 si përgjigje kufizimi ose callback i refuzuar → NO-GO
- [ ] **EG-10** — Certifikata, DNS dhe TLS ≥ 1.2 për `enterprise.`, `central.`, `central-internal.`.  
  Prova: shih IN-04/IN-05; `openssl s_client -tls1_1` dështon, `-tls1_2` kalon  
  Pritet: SAN për secilin emër; TLS ≥ 1.2; ridrejtim 80→443 nga terminatori  
  Klas.: EXTERNAL EVIDENCE REQUIRED  
  NO-GO/abort: mungon ndonjë → NO-GO

## 4. APP

- [ ] **AP-01** — `production_problems()` bosh.  
  Prova: Brenda imazhit me `env_file`: `python -c "from app.core.config import settings; print(chr(10).join(settings.production_problems()) or 'OK')"` (PROPOSED)  
  Pritet: `OK`  
  Klas.: VERIFIED (metoda); përdorimi PROPOSED  
  NO-GO/abort: çdo rresht → NO-GO
- [ ] **AP-02** — Migrimi Enterprise përfundon.  
  Prova: `docker compose -f docker-compose.prod.yml --env-file .env run --rm migrate` (PROPOSED si hap i veçantë)  
  Pritet: kod daljeje 0  
  Klas.: VERIFIED (mekanizmi në compose)  
  NO-GO/abort: gabim → ABORT, rikthim sipas runbook-ut
- [ ] **AP-03** — Skema është te koka.  
  Prova: `curl -s http://127.0.0.1:8080/readyz`  
  Pritet: `{"status":"ready"}`  
  Klas.: VERIFIED (endpoint)  
  NO-GO/abort: 503 → ABORT
- [ ] **AP-04** — Shërbimet healthy.  
  Prova: `docker compose -f docker-compose.prod.yml ps`  
  Pritet: db, api, worker, webhook-worker, web = healthy; me `--profile central`: edhe `central-api` = healthy dhe `central-web` = running (`central-migrate` përfundon me kod 0 dhe nuk shfaqet në `ps`)  
  Klas.: NOT VERIFIED në prodhim  
  NO-GO/abort: unhealthy >5 min → ABORT
- [ ] **AP-05** — Header-at e sigurisë aktive.  
  Prova: `curl -sI https://<domen>/`  
  Pritet: përmban `Strict-Transport-Security`, `Content-Security-Policy`, `X-Frame-Options: DENY`  
  Klas.: VERIFIED (konfigurimi); përgjigja reale EXTERNAL  
  NO-GO/abort: mungesë → NO-GO
- [ ] **AP-06** — Konsola ngarkohet pa gabime.  
  Prova: Chromium: hapje e `https://<domen>/`, login me çelës personal  
  Pritet: 0 gabime console  
  Klas.: EXTERNAL EVIDENCE REQUIRED  
  NO-GO/abort: gabim → NO-GO
- [ ] **AP-07** — Çelësat personalë + 2FA; çelësi bootstrap hequr.  
  Prova: Konsolë: çelësat `superadmin` personalë aktivë; `grep -c '^SMS_ADMIN_API_KEY=' .env` dhe rinisje e shërbimeve  
  Pritet: 2FA aktive për çdo staf; `0` (ose e zbrazur) dhe shërbimet e rinisura  
  Klas.: VERIFIED (RUNBOOK §1.4); zbatimi EXTERNAL  
  NO-GO/abort: mbetet bootstrap → NO-GO
- [ ] **AP-08** — Autoritetet e pacaktuara mbeten `local`.  
  Prova: `grep -E '^SMS_(SENDER|MONEY|PRICING|BILLING)_AUTHORITY=' .env`  
  Pritet: `local` për secilin (deployment i parë)  
  Klas.: VERIFIED (default); vlera në prodhim EXTERNAL  
  NO-GO/abort: ndryshe pa miratim të veçantë → NO-GO

## 5. WORKERS

- [ ] **WK-01** — Workers kryesorë gjallë.  
  Prova: `docker compose … ps worker webhook-worker`; `docker compose … exec worker python -m scripts.worker_health 90`  
  Pritet: healthy; kod daljeje 0  
  Klas.: VERIFIED (mekanizmi); sjellja në compose PARTIALLY  
  NO-GO/abort: heartbeat >90 s → NO-GO
- [ ] **WK-02** — Radha pa UNKNOWN/SENDING të ngecur.  
  Prova: `docker compose … exec api python -m scripts.queue_readiness --json`  
  Pritet: 0 FAIL (rekomandim: edhe 0 WARN)  
  Klas.: VERIFIED  
  NO-GO/abort: FAIL → NO-GO
- [ ] **WK-03** — `dispatch` në pauzë deri te canary.  
  Prova: Konsolë: Admin → Kill switches  
  Pritet: `dispatch` = pauzë  
  Klas.: VERIFIED (RUNBOOK §6)  
  NO-GO/abort: aktiv para canary-t → NO-GO
- [ ] **WK-04** — Profilet CP/sender të pa-nisura pa miratim.  
  Prova: `docker compose … ps` (pa `cp-sync`, `sender-*`, `money-*`, `billing-report`, `pricing-sync`)  
  Pritet: asnjë prej tyre në ekzekutim  
  Klas.: VERIFIED (profile opsionale)  
  NO-GO/abort: ndonjë në ekzekutim pa miratim → NO-GO

## 6. CENTRAL

Paketimi i Central (M11-I/F-01) është **IMPLEMENTED / LOCALLY VERIFIED; CI VERIFICATION PENDING**: provuar me compose lokal mbi PG dev, jo në CI dhe jo në prodhim. Kutitë e mëposhtme kërkojnë dëshmi nga mjedisi i prodhimit.

- [ ] **CE-00** — Revision-i ka CI jeshile për hapat Central (imazh `--target central`, build web Central, `nginx -t`, `compose config`, Vitest/build `frontend-central`, migrim Central në PG).  
  Prova: statusi CI i atij commit-i  
  Pritet: të gjitha punët `backend`, `frontend-central`, `docker` = success  
  Klas.: NOT VERIFIED (CI nuk ka ekzekutuar mbi paketimin; F-04 shton skanim sekretesh dhe `central-e2e` (tani bllokues, 3/3 GREEN); backend ndahet në shard-e paralele — CI EXECUTION PENDING për gjelbërimin e plotë)  
  NO-GO/abort: ndonjë punë e kuqe ose e munguar → NO-GO
- [ ] **CE-01** — Central është opt-in: nisja e zakonshme mbetet vetëm-Enterprise.  
  Prova: `docker compose -f docker-compose.prod.yml --env-file .env config --services` (pa profile) dhe `… --profile central config --services`  
  Pritet: pa profile: `api db migrate web webhook-worker worker` (pa asnjë `central-*`, dhe pa kërkuar `.env.central`); me `--profile central`: shtohen `central-migrate`, `central-api`, `central-web` dhe **jo** `central-billing-run`, `central-send-notifications`, `central-tool` (profili `central-jobs`)  
  Klas.: VERIFIED LOKALISHT  
  NO-GO/abort: një `central-*` në nisjen e zakonshme, ose një job te profili `central` → NO-GO
- [ ] **CE-01a** — Aktivizimi i Central kërkon mjedisin e vet (kërkesa s'është dobësuar).  
  Prova: `docker compose -f docker-compose.prod.yml --env-file .env --profile central config -q` pa `.env.central`  
  Pritet: dështon me `env file … .env.central not found`; me `.env.central` të plotësuar del pa gabim  
  Klas.: VERIFIED LOKALISHT  
  NO-GO/abort: kalon pa `.env.central` → NO-GO
- [ ] **CE-01b** — DB logjike `sms_central` ekziston dhe është e ndarë nga `sms` (hap infrastrukture; RUNBOOK §8.1).  
  Prova: `docker compose … exec db psql -U sms -d postgres -Atc "select datname from pg_database where datname in ('sms','sms_central') order by 1"`  
  Pritet: dy rreshta, `sms` dhe `sms_central`; `CENTRAL_DATABASE_URL` përfundon me `/sms_central` dhe përdor rolin `sms_central`  
  Klas.: EXTERNAL EVIDENCE REQUIRED (prodhimi); procedura VERIFIED LOKALISHT  
  NO-GO/abort: `CENTRAL_DATABASE_URL` drejt DB `sms` → NO-GO
- [ ] **CE-02** — Migrimi Central te 0031.  
  Prova: `docker compose -f docker-compose.prod.yml --env-file .env run --rm central-migrate` (kod daljeje 0) pastaj `docker compose … exec db psql -U sms -d sms_central -Atc "select version_num from central_alembic_version"`  
  Pritet: `0031`; `central-migrate` refuzon DB Enterprise dhe SQLite në prodhim  
  Klas.: VERIFIED LOKALISHT (PG dev); prodhimi EXTERNAL  
  NO-GO/abort: gabim migrimi → ABORT sipas rollback runbook (jo downgrade)
- [ ] **CE-02b** — `central-api` healthy dhe i brendshëm.  
  Prova: `docker compose … ps central-api`; `docker compose … port central-api 8001`; `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8001/healthz` nga host-i  
  Pritet: `healthy`; asnjë port i publikuar; kërkesa nga host-i dështon (kod 000)  
  Klas.: VERIFIED LOKALISHT  
  NO-GO/abort: `central-api` i publikuar ose unhealthy >5 min → NO-GO
- [ ] **CE-03** — Konfigurim prodhimi Central i vlefshëm; gate-t e nisjes aktive.  
  Prova: `.env.central` me `CENTRAL_ENV=production`; një nisje provë pa `CENTRAL_AUTH_SECRET` (ose me SQLite/`fake`) refuzohet; me sekret ≥32 karaktere dhe PG nis  
  Pritet: `RuntimeError` për sekret bosh, SQLite dhe `fake`; nis me konfigurim të vlefshëm  
  Klas.: VERIFIED LOKALISHT (kodi dhe provat); vlerat e prodhimit EXTERNAL  
  NO-GO/abort: nis me sekret bosh ose SQLite → NO-GO
- [ ] **CE-04** — Familjet e API-ve që s'janë të konsolës Central nuk ekspozohen përmes `central-web`.  
  Prova: `for p in /v1/contacts /webhooks/dlr/x /u/x /internal/sync/changes /registration; do curl -s -o /dev/null -w "$p %{http_code}\n" http://127.0.0.1:8081$p; done` (lokalisht) dhe e njëjta nga jashtë me domenin real  
  Pritet: `404` për secilën; `/healthz` dhe `/readyz` **403 publikisht** (vetëm të brendshme); `/admin/ping` pa token 401  
  Klas.: VERIFIED LOKALISHT; përmes domenit real EXTERNAL EVIDENCE REQUIRED  
  NO-GO/abort: çdo 200 për një rrugë të listuar → NO-GO
- [ ] **CE-04b** — Vetëm `central-web` publikon porte, vetëm në loopback: `127.0.0.1:8081` (stafi) dhe `127.0.0.1:8082` (Enterprise→Central).  
  Prova: `docker ps --format '{{.Names}} {{.Ports}}'`  
  Pritet: `central-web` → `127.0.0.1:8081-8082->8081-8082/tcp`; `central-api` vetëm `8001/tcp` (jo i publikuar)  
  Klas.: VERIFIED LOKALISHT  
  NO-GO/abort: port tjetër i publikuar → NO-GO
- [ ] **CE-05** — Konsola Central ngarkon dhe hyn (admin dhe operator).  
  Prova: Chromium kundër `central-web`: hyrje me admin, pastaj operator, një faqe audit; fjalëkalim i gabuar mbetet te hyrja  
  Pritet: 0 gabime console (CSP aktive); asnjë kërkesë `/v1`; token vetëm në `sessionStorage`  
  Klas.: VERIFIED LOKALISHT (nginx real, DB dev); prodhimi EXTERNAL EVIDENCE REQUIRED  
  NO-GO/abort: gabim console ose CSP → NO-GO
- [ ] **CE-05b** — Ndarja admin/operator.  
  Prova: me token operatori `GET /admin/ping`, `POST /admin/products` (trup `{}`); me token admini `GET /admin/ping`  
  Pritet: operator 403 dhe 403; admin 200  
  Klas.: VERIFIED LOKALISHT  
  NO-GO/abort: operator 200 në shkrim → NO-GO
- [ ] **CE-05c** — Header-at e sigurisë në `central-web`.  
  Prova: `curl -sI http://127.0.0.1:8081/` dhe `/assets/x.js`  
  Pritet: `Content-Security-Policy`, `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy`; **pa** `Strict-Transport-Security` (TLS jashtë; HSTS vendoset në pikën e TLS)  
  Klas.: VERIFIED LOKALISHT  
  NO-GO/abort: mungon CSP → NO-GO
- [ ] **CE-06** — Proceset e planifikuara Central të instaluara dhe me heartbeat.  
  Prova: planifikuesi i jashtëm ekzekuton `docker compose -f docker-compose.prod.yml --env-file .env --profile central-jobs run --rm central-billing-run` (vetëm kur autoriteti i faturimit është `central`; me `local` refuzon me kod 3); `central-send-notifications` sipas nevojës; `docker compose -f docker-compose.prod.yml --env-file .env --profile central-jobs run --rm central-tool apps.central.tools.billing_readiness --json`  
  Pritet: ekzekutime të planifikuara me kod 0; `periods.due_unprocessed = 0` kur faturimi është aktiv; `CENTRAL_BILLING_WORKER_CONFIGURED=true` vetëm pasi planifikimi ekziston  
  Klas.: komandat VERIFIED LOKALISHT; planifikimi dhe heartbeat EXTERNAL EVIDENCE REQUIRED  
  NO-GO/abort: `due_unprocessed` >0 → NO-GO
- [ ] **CE-07** — Gate-i financiar agregat (kredencialet e shërbimit + alarmet Enterprise).  
  Prova: **jashtë kontejnerit Central**, nga një checkout i repo-së me të dy mjediset (`CENTRAL_DATABASE_URL` dhe `SMS_DATABASE_URL`, ose `--enterprise-cwd`): `python -m apps.central.tools.financial_readiness --json` (`financial_readiness` thërret `scripts.*` të Enterprise; nuk është në inventarin e imazhit Central, RUNBOOK §8)  
  Pritet: `central:financial_service_credentials` PASS; pa FAIL  
  Klas.: VERIFIED (mjeti, kufiri i dokumentuar); prodhimi EXTERNAL EVIDENCE REQUIRED  
  NO-GO/abort: FAIL → NO-GO

## 7. PROVIDER / DLR

> **M11-I/F-03:** nisja në prodhim refuzon tani `SMS_TRUSTED_PROXY_HOPS`/`CENTRAL_TRUSTED_PROXY_HOPS` ≠ 1, sekret DLR të dobët për provider-in HTTP aktiv, Twilio gjysmë-konfigurimi dhe SMTP pa host (§0d në gap analysis). Provuar lokalisht; vlerat reale mbeten EXTERNAL.

- [ ] **PV-01** — Kredencialet e provider-it në vendin e miratuar.  
  Prova: Konfirmim nga pronari i sekreteve (pa shfaqur vlerat)  
  Pritet: konfirmim i regjistruar  
  Klas.: EXTERNAL EVIDENCE REQUIRED  
  NO-GO/abort: mungon → NO-GO
- [ ] **PV-02** — `SMS_DLR_SECRETS` i vendosur për provider-in aktiv.  
  Prova: Kontroll i pranisë së çelësit pa e shfaqur (p.sh. `python -c` që shfaq vetëm emrat e çelësave)  
  Pritet: emri i provider-it i pranishëm  
  Klas.: VERIFIED LOKALISHT (F-03: nisja në prodhim refuzon `SMS_DLR_SECRETS` që mungon/është i dobët kur provider-i HTTP është aktiv); vlera reale EXTERNAL  
  NO-GO/abort: mungon → NO-GO
- [ ] **PV-03** — Callback i nënshkruar pranohet, i pasaktë refuzohet.  
  Prova: Dy POST te `/webhooks/dlr/<provider>`: një me HMAC të saktë, një me të gabuar  
  Pritet: 2xx dhe 4xx përkatësisht  
  Klas.: VERIFIED (M11-H, FakeProvider); me provider real EXTERNAL  
  NO-GO/abort: pranim i të pasaktit → NO-GO
- [ ] **PV-04** — URL publik arritshëm nga provider-i.  
  Prova: `curl -sI https://<domen>/healthz` nga rrjet jashtë datacenter-it  
  Pritet: 200  
  Klas.: EXTERNAL EVIDENCE REQUIRED  
  NO-GO/abort: jo 200 → NO-GO
- [ ] **PV-05** — Rruga e provider-it e çaktivizuar deri te canary.  
  Prova: Konsolë/API: rrugët (`enabled=false`)  
  Pritet: të gjitha false para canary-t  
  Klas.: VERIFIED (mekanizmi)  
  NO-GO/abort: e hapur → NO-GO

## 8. OBSERVABILITY

- [ ] **OB-01** — Alarmet kritike ekzistojnë dhe kanë pronar.  
  Prova: Lista e alarmeve nga `m11i-observability-alerting-plan.md` të lidhura me kanal  
  Pritet: çdo alarm S1/S2 ka pranues  
  Klas.: EXTERNAL EVIDENCE REQUIRED  
  NO-GO/abort: pa pronar → NO-GO
- [ ] **OB-02** — Test alarmi i provokuar.  
  Prova: Ndaloje `worker` për >90 s në staging/jo-prodhim  
  Pritet: alarmi arrin pranuesin brenda pragut  
  Klas.: EXTERNAL EVIDENCE REQUIRED  
  NO-GO/abort: s'arrin → NO-GO
- [ ] **OB-03** — Log latence/5xx të nginx.  
  Prova: Formati i log-ut përmban `$request_time` dhe `$status` (PROPOSED; konfigurimi aktual nuk e ka)  
  Pritet: fushat janë të pranishme  
  Klas.: NOT VERIFIED  
  NO-GO/abort: mungon → NO-GO
- [ ] **OB-04** — Alarm backup-i.  
  Prova: Një backup i dështuar i provokuar gjeneron alarm  
  Pritet: alarm i marrë  
  Klas.: EXTERNAL EVIDENCE REQUIRED  
  NO-GO/abort: s'ka alarm → NO-GO
- [ ] **OB-05** — Alarm skadence certifikate.  
  Prova: Alarm i konfiguruar me prag ≥ 21 ditë (PROPOSED)  
  Pritet: alarm aktiv  
  Klas.: EXTERNAL EVIDENCE REQUIRED  
  NO-GO/abort: mungon → NO-GO

## 9. SECURITY

- [ ] **SC-01** — Asnjë sekret në git/imazh/log.  
  Prova: Skanim sekretesh i historisë dhe i imazhit (mjet i miratuar). CI skanon vetëm pemën aktuale (F-04, gitleaks); historia e plotë = detyrë manuale e shtyrë  
  Pritet: 0 gjetje  
  Klas.: NOT VERIFIED  
  NO-GO/abort: gjetje → NO-GO
- [ ] **SC-02** — `.env` me leje 600, jashtë git.  
  Prova: `stat -c '%a %U' .env`; `git check-ignore .env`  
  Pritet: `600`; `.env` i injoruar  
  Klas.: VERIFIED (`.gitignore`); leja në host EXTERNAL  
  NO-GO/abort: ndryshe → NO-GO
- [ ] **SC-03** — `SMS_SECRETS_KEY` ruhet veçmas nga backup-et.  
  Prova: Dëshmi e vendit të ruajtjes dhe qasjes  
  Pritet: vend i ndarë i dokumentuar  
  Klas.: EXTERNAL EVIDENCE REQUIRED  
  NO-GO/abort: mungon → NO-GO
- [ ] **SC-04** — Parametra proxy të saktë.  
  Prova: Kontrolli i dy IP-ve (IN-06) për Enterprise dhe Central  
  Pritet: `SMS_TRUSTED_PROXY_HOPS` dhe `CENTRAL_TRUSTED_PROXY_HOPS` të dokumentuar dhe të provuar  
  Klas.: EXTERNAL EVIDENCE REQUIRED  
  NO-GO/abort: kyçje masive ose spoofing → NO-GO
- [ ] **SC-05** — 2FA e stafit Enterprise aktive.  
  Prova: `SMS_REQUIRE_STAFF_2FA=true` dhe çdo çelës superadmin ka 2FA  
  Pritet: true; 100% me 2FA  
  Klas.: VERIFIED (mekanizmi); prodhimi EXTERNAL  
  NO-GO/abort: jo → NO-GO
- [ ] **SC-06** — Kontroll kompensues për Central pa 2FA.  
  Prova: Vendim i shkruar (VPN/IP allow-list për `/admin/*`, fjalëkalime të gjata)  
  Pritet: vendim i firmosur  
  Klas.: EXTERNAL / POLICY DECISION REQUIRED (nuk u gjet 2FA në kod; F-03 nuk e zbaton)  
  NO-GO/abort: pa vendim → NO-GO

## 10. CANARY

- [ ] **CN-01** — Provë idempotence e provider-it.  
  Prova: Procedura te `docs/M9_PRODUCTION_CHECKLIST.md` §1  
  Pritet: raport i kaluar  
  Klas.: EXTERNAL EVIDENCE REQUIRED  
  NO-GO/abort: dështim → NO-GO
- [ ] **CN-02** — Canary: 1 mesazh te marrës i miratuar.  
  Prova: Hap `dispatch` dhe rrugën për një prefiks; dërgo 1 mesazh  
  Pritet: statusi arrin `delivered` nga DLR i nënshkruar brenda pragut të miratuar  
  Klas.: EXTERNAL EVIDENCE REQUIRED  
  NO-GO/abort: FAILED/UNKNOWN ose pa DLR → ABORT: pauzo `dispatch`, çaktivizo rrugën
- [ ] **CN-03** — Pas canary-t: radha e pastër.  
  Prova: `docker compose … exec api python -m scripts.queue_readiness --json`  
  Pritet: 0 FAIL, 0 UNKNOWN  
  Klas.: VERIFIED (mjeti)  
  NO-GO/abort: UNKNOWN → ABORT
- [ ] **CN-04** — Pas canary-t: libri i rregullt.  
  Prova: `python -m scripts.verify_ledger`; `python -m scripts.financial_ops --json`  
  Pritet: PASS; pa CRITICAL  
  Klas.: VERIFIED (mjetet)  
  NO-GO/abort: FAIL/CRITICAL → ABORT
- [ ] **CN-05** — Faturimi i mesazhit canary.  
  Prova: Ledger: një debitim për mesazhin dhe asnjë dublim  
  Pritet: 1 rresht ledger për mesazhin  
  Klas.: EXTERNAL EVIDENCE REQUIRED  
  NO-GO/abort: dublim → ABORT

## 11. GO / NO-GO

- [ ] **GN-01** — Të gjitha kutitë 1–10 të shënuara me provë.  
  Prova: Regjistri i ndryshimit  
  Pritet: 100%  
  Klas.: —  
  NO-GO/abort: ndonjë bosh → NO-GO
- [ ] **GN-02** — Asnjë ABORT i hapur.  
  Prova: Regjistri  
  Pritet: 0 aborte të hapura  
  Klas.: —  
  NO-GO/abort: ndonjë → NO-GO
- [ ] **GN-03** — Firma e dy personave (operator + pronar produkti).  
  Prova: Regjistri  
  Pritet: 2 firma  
  Klas.: EXTERNAL EVIDENCE REQUIRED  
  NO-GO/abort: mungon → NO-GO
- [ ] **GN-04** — Verdikti i M11-I është më i lartë se BLOCKED.  
  Prova: `m11i-production-readiness-gap-analysis.md` §5 i përditësuar  
  Pritet: CONDITIONALLY READY ose më lart me dëshmi  
  Klas.: —  
  NO-GO/abort: BLOCKED → NO-GO

## 12. POST-CUTOVER

- [ ] **PO-01** — Orët e para: `readyz`/`healthz` çdo minutë.  
  Prova: Monitorimi (OB-01)  
  Pritet: 100% 200 për 60 min  
  Klas.: EXTERNAL EVIDENCE REQUIRED  
  NO-GO/abort: dështim i vazhdueshëm 3 min (PROPOSED) → rollback
- [ ] **PO-02** — Norma 5xx.  
  Prova: Log nginx  
  Pritet: < 1% e kërkesave për 60 min (PROPOSED)  
  Klas.: EXTERNAL EVIDENCE REQUIRED  
  NO-GO/abort: ≥ 5% për 5 min → rollback
- [ ] **PO-03** — Radha dhe DLR.  
  Prova: `queue_readiness --json`; `GET /v1/admin/stats` (`oldest_queued_age_seconds`, `stuck_sending`, `unknown_outcome`, `dlr_problems_24h`)  
  Pritet: oldest queued < 300 s (PROPOSED); stuck_sending 0; unknown 0  
  Klas.: VERIFIED (fushat ekzistojnë); pragjet PROPOSED  
  NO-GO/abort: unknown >0 për >1 orë → rollback
- [ ] **PO-04** — Libri çdo orë në ditën 1.  
  Prova: `python -m scripts.verify_ledger`; `python -m scripts.financial_ops --json`  
  Pritet: PASS; pa CRITICAL  
  Klas.: VERIFIED (mjetet)  
  NO-GO/abort: FAIL/CRITICAL → rollback
- [ ] **PO-05** — Backup i parë automatik i suksesshëm.  
  Prova: Log i cron-it të backup-it (`/var/log/sms-backup.log`) me `OK`  
  Pritet: rreshti `OK …` brenda 24 orësh  
  Klas.: EXTERNAL EVIDENCE REQUIRED  
  NO-GO/abort: dështim → alarm, rregullo para trafikut të rritur
- [ ] **PO-06** — Raport pas cutover-it.  
  Prova: Dokument me dëshmitë, shmangiet dhe borxhin  
  Pritet: dokument i arkivuar  
  Klas.: —  
  NO-GO/abort: mungon → hap incident

## Shënim

Pragjet e shënuara **PROPOSED** janë propozime pa verifikim në prodhim. Kutitë me klasifikim **NOT VERIFIED** ose **EXTERNAL EVIDENCE REQUIRED** nuk mund të plotësohen nga ky repo. Verdikti aktual i M11-I mbetet **BLOCKED**.
