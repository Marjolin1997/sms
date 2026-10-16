# M11-I — Analiza e boshllëqeve për gatishmërinë e prodhimit

**Revision i bazës:** `6020026`. **Faza:** M11-I Faza 2 (vetëm dokumentim; asnjë ndryshim kodi, konfigurimi apo infrastrukture).
**Verdikti: BLOCKED.** Kodi është pilot-ready (M11-H: PASS WITH ACCEPTED DEBT); sistemi nuk është gati operativisht për prodhim.

**Përditësim M11-I/F-01 (paketimi i Central):** GAP-01, GAP-02, GAP-03, GAP-14, GAP-15 dhe pjesa e paketimit e GAP-16/17 janë **IMPLEMENTED / LOCALLY VERIFIED; CI VERIFICATION PENDING** (ndryshimet nuk janë commit-uar/push-uar; CI s'ka ekzekutuar). Asnjë bllokues i jashtëm nuk është mbyllur. Shih §0b.

**Përditësim M11-I/F-02 (skaji: TLS/DNS/reverse proxy/IP e klientit):** pjesa e KODIT dhe e konfigurimit të skajit është **IMPLEMENTED / LOCALLY VERIFIED; CI PENDING** (nginx real në Docker lokal, pa TLS/DNS/LB real). **Mbeten të jashtme:** DNS real, certifikata, LB/WAF real, CIDR-t reale të proxy-ve, rrjeti/firewall-i, emrat reale të hostit, validimi real i TLS, aktivizimi real i HSTS dhe dëshmia e provider-it. Shih §0c.

**Përditësim M11-I/F-04 (forcimi i CI):** shtuar në `.github/workflows/ci.yml` dhe repo: skanim sekretesh (gitleaks, **VETËM peme aktuale**), job `central-e2e` (**BLLOKUES** pas 3 ekzekutimesh radhazi të gjelbra në GitHub CI mbi commit-e të ndryshme), pohim i kokës Enterprise të migrimit, artefakte dështimi (vetëm logje + pamje ekrani) për E2E Enterprise dhe Central, `timeout-minutes`, Dependabot për `frontend-central`, `.gitignore` për `*.pem` dhe `.env.*`. Statusi: **IMPLEMENTED / LOCALLY VERIFIED; CI EXECUTION PENDING** (GitHub s'ka ekzekutuar kurrë këtë workflow). Skanimi i historisë së plotë: **DEFERRED / MANUAL SECURITY TASK**. Sharding-u i PG: **DEFERRED** (pas kohës reale nga GitHub). Shih §0e.

**Përditësim M11-I/F-03 (nisja/sekretet në prodhim):** pjesa e nisjes dhe e sekreteve e GAP-17 është **RESOLVED / LOCALLY VERIFIED** (gate-t kushtëzuese në `production_problems()` dhe te Central; CI PENDING). **2FA e detyrueshme e stafit Central mbetet EXTERNAL / POLICY DECISION REQUIRED** — GAP-17 si i tërë NUK është mbyllur. Shih §0d.

> **Shënim numërimi:** etiketat F-01/F-02/F-03 të milestone-s së ekzekutimit (F-01 paketimi i Central, F-02 TLS/reverse proxy/trusted proxy, F-03 siguria e nisjes/sekreteve) janë të ndryshme nga etiketat e vjetra F-xx të «veprimeve të ardhshme» të shkruara fillimisht në §1. Etiketat e §1 janë **legacy** dhe nuk e udhëheqin më numërimin; historiku nuk është rinumëruar.

## 0. Si lexohet ky dokument

- Klasifikimet: **VERIFIED** (provuar në repo ose në validimet M11-H), **PARTIALLY VERIFIED**, **NOT VERIFIED**, **EXTERNAL EVIDENCE REQUIRED** (kërkon infrastrukturë, llogari ose njerëz jashtë repo-së).
- Asnjë fakt prodhimi nuk është shpikur. Pragjet, kohët dhe pronarët e shënuar **PROPOSED** ose **NUK ËSHTË CAKTUAR** janë propozime/zbrazëti, jo fakte.
- Veprimet e ardhshme teknike (F-xx) janë **të papërfshira** në M11-I; kërkojnë miratim të shprehur në një fazë të mëvonshme.

## 0b. Përditësim M11-I/F-01 — çfarë u zbatua dhe si u verifikua

Zbatuar (vetëm paketim, pa ndryshim të semantikës së biznesit): stage `central` te `Dockerfile` (Enterprise mbetet stage-i i fundit), `deploy/web-central.Dockerfile` + `deploy/nginx.central.conf`, `.env.central.example`, shërbimet `central-migrate` / `central-api` / `central-web` (profili opt-in `central`) dhe profili `central-jobs` te `docker-compose.prod.yml` (nisja e zakonshme mbetet vetëm-Enterprise), hapa CI, mbrojtja e nisjes (prodhimi refuzon SQLite/jo-PostgreSQL) dhe mbrojtja e migrimit (Central refuzon një DB Enterprise), teste statike dhe docs.

| Provë | Rezultati | Klasifikim |
|---|---|---|
| Ndërtim i pastër i 4 imazheve (`central`, Enterprise, web Enterprise, web Central) | 4/4 `exit=0` | VERIFIED LOKALISHT (daemon Docker lokal; shih shënimin e CA më poshtë) |
| Imazhi Central: user 10001, pa `app/`, `scripts/`, `.env*`, `*.db`, `*.pem`, pa frontend; Enterprise pa `apps/` | Kontrolluar brenda imazheve | VERIFIED LOKALISHT |
| `nginx -t` për të dy nginx-et në imazhet e tyre | të dyja `successful` | VERIFIED LOKALISHT |
| `docker compose config` / `create` (amendment F-01) | **Pa profile = vetëm Enterprise** (`api db migrate web webhook-worker worker`), pa kërkuar `.env.central` (provuar me `config` dhe `create`, me dhe pa `.env.central` pranë); `--profile central` shton `central-migrate/api/web` dhe kërkon `.env.central` (dështim i qartë pa të); `--profile central-jobs` shton vetëm jobet; `central` nuk nis jobet; `up -d central-web` nis vetëm Central | VERIFIED LOKALISHT |
| Stack real me compose (PG 16 në kontejner, `central-migrate` → `central-api` → `central-web`) | migrim te `0031`; `central-api` healthy (`/readyz`); vetëm `127.0.0.1:8081` publikohet; `central-api:8001` i paarritshëm nga host-i | VERIFIED LOKALISHT |
| Rrugëzimi përmes nginx real | `/auth`,`/admin`,`/healthz`,`/readyz` → Central; `/v1`,`/webhooks`,`/u`,`/internal`,`/registration` → 404; fallback SPA | VERIFIED LOKALISHT |
| Hyrja në konsolën Central përmes nginx real (Chromium): admin dhe operator; fjalëkalim i gabuar mbetet te hyrja; 0 kërkesa `/v1`; 0 gabime console (CSP aktive); token vetëm në `sessionStorage` | OK | VERIFIED LOKALISHT |
| Ndarja admin/operator (API) | operator: `/admin/ping` 403, shkrim 403; admin 200/422; pa token 401 | VERIFIED LOKALISHT |
| `central-billing-run` kur autoriteti i faturimit është `local` | refuzon (kod 3), nuk lëshon fatura | VERIFIED LOKALISHT |
| Nisja në prodhim refuzohet: SQLite; sekret bosh; mailer `fake` | `RuntimeError` për secilën (provuar edhe me `uvicorn`/import) | VERIFIED LOKALISHT |
| Migrim Central bosh → `0031` në PG; kontrolli `readyz` kur skema është pas (`503 not_ready`), pastaj rikthim në `ready` | OK | VERIFIED LOKALISHT |
| Migrimi Central kundër një DB Enterprise | refuzohet (`sms_alembic_version`); asgjë nuk krijohet | VERIFIED LOKALISHT (PG + SQLite) |
| DB-të ndahen: tabela e versioneve Central vs Enterprise (`0031` / `0034`), 0 tabela `sms_*` në DB Central | OK | VERIFIED LOKALISHT |
| CI (`.github/workflows/ci.yml`) | hapat shtuar, **s'kanë ekzekutuar kurrë** | NOT VERIFIED (CI PENDING) |
| Asnjë provider real, asnjë SMS/email, asnjë sekret në log/imazh | kontrolluar (0 përputhje) | VERIFIED LOKALISHT |

**Shënim mjedisi (vetëm sandbox):** `pip`/`npm` brenda `docker build` dështonin me `CERTIFICATE_VERIFY_FAILED` (proxy TLS i sandbox-it). Ndërtimet u bënë me imazhe bazë LOKALE me CA-në e sandbox-it, të dhëna përmes `--build-context`; `Dockerfile`-t e repo-së NUK u ndryshuan për këtë. Në CI/prodhim nuk nevojitet.

**Çfarë NUK u mbyll (mbetet bllokues):** infrastruktura, TLS/DNS/LB (përfshirë HSTS-në dhe `TRUSTED_PROXY_HOPS` real), PostgreSQL prodhimi/PITR, backup/restore i Central në prodhim, menaxhimi i sekreteve, monitorimi, kredencialet dhe canary i provider-it, planifikimi real i `billing_run`/`send_notifications`, evidenca e cutover-it. Imazhet nuk janë vendosur askund veç sandbox-it lokal.

### Gjetje të reja nga validimi F-01 (nuk u rregulluan këtu)
| ID | Gjetja | Ashpërsia | Klasifikim |
|---|---|---|---|
| N-1 | Nginx i Enterprise humbiste CSP/nosniff/X-Frame/HSTS te `/` dhe `/assets/`. **RESOLVED (F-02):** bashkësia e plotë përsëritet në çdo location me `add_header`, gabimet e nginx kanë header-a të plotë, API-ja i mban ato të aplikacionit pa dyfishim; provuar në nginx real. | ~~MEDIUM~~ RESOLVED | VERIFIED LOKALISHT |
| N-2 | Log-u i hyrjes së Central tregonte IP-në e proxy-t. **RESOLVED (F-02):** login-i dhe `/internal/*` përdorin `CENTRAL_TRUSTED_PROXY_HOPS` me helper të përbashkët; provuar me testet dhe compose real. Vlera e saktë në topologjinë reale mbetet EXTERNAL EVIDENCE REQUIRED (GAP-08). | ~~LOW~~ RESOLVED (kod) | VERIFIED LOKALISHT |
| N-3 | `financial_readiness` thërret `scripts.*` të Enterprise (subproces) dhe del FAIL brenda imazhit Central. **RESOLVED (amendment F-01):** është agregator ndër-sistem, jo komandë e imazhit Central; hequr nga paketimi/inventari (compose, RUNBOOK §8) dhe dokumentuar si "jashtë imazhit" (ekzekutohet nga checkout me të dy mjediset). Inventar i dokumentuar (11 komanda) me test `--help`/pa varësi Enterprise për secilën, dhe i provuar brenda imazhit real (11/11). | ~~MEDIUM~~ RESOLVED | VERIFIED LOKALISHT |
| N-4 | Një `.env` lokal në rrënjë ndryshon sjelljen e testeve Enterprise (lexohet nga `Settings`): kërkon pastrim para `pytest`. **E hapur.** | LOW | VERIFIED |
| N-5 | `docker compose up -d` pa emra nisi edhe Central dhe kërkonte `.env.central`. **RESOLVED (amendment F-01):** shërbimet Central janë te profili opt-in `central` (serving) dhe `central-jobs`; nisja e zakonshme mbetet vetëm-Enterprise dhe nuk kërkon `.env.central`; aktivizimi i Central kërkon `.env.central` (kërkesa s'u dobësua). | ~~LOW~~ RESOLVED | VERIFIED LOKALISHT |

## 0c. Përditësim M11-I/F-02 — çfarë u zbatua dhe si u verifikua

Modeli i vendosur: terminator i JASHTËM i TLS/LB/WAF; nginx-et mbeten HTTP-only; hostname të ndarë (`enterprise.<domen>`, `central.<domen>`, emër/listener i brendshëm për Enterprise→Central); `hops=1` pas `real_ip` me CIDR të besuara të dhëna nga operatori (parazgjedhja: asnjë); HSTS i zotëruar nga terminatori.

| Zbatuar | Provë | Klasifikim |
|---|---|---|
| `real_ip` (`X-Forwarded-For`, `real_ip_recursive on`) në të dy nginx-et, vetëm nga CIDR të gjeneruara nga `EDGE_*_CIDRS` (`deploy/edge-cidrs.sh`); `/0` dhe vlera të pavlefshme refuzohen në nisje; asnjë CIDR e ngulitur | nginx real: klient i falsifikuar majtas injorohet; disa proxy upstream normalizohen; pa proxy të besuar, XFF i klientit injorohet; `0.0.0.0/0`, `::/0`, vlera të pavlefshme → nginx s'niset (64 kontrolle `e2e/edge/check_edge.py`) | VERIFIED LOKALISHT |
| `X-Forwarded-For` i normalizuar (`$remote_addr`), `X-Real-IP` i rishkruar, `X-Forwarded-Proto` = `https` vetëm nga proxy i besuar që e deklaron | nginx real, dy emërtime | VERIFIED LOKALISHT |
| HSTS hequr nga nginx Enterprise dhe nga aplikacioni; nginx hedh poshtë çdo HSTS nga upstream (**C-3 RESOLVED**) | nginx real (stub me HSTS): asnjë `Strict-Transport-Security` në `/`, `/assets`, API, gabime; test app | VERIFIED LOKALISHT |
| Header-at e sigurisë: Enterprise `/`, `/assets/*` (edhe 404), SPA fallback, gabime nginx (403/413/429/5xx), API pa dyfishim (**N-1 RESOLVED**); Central kudo | nginx real: secili header një herë | VERIFIED LOKALISHT |
| Kufizimi: Enterprise 429 (jo 503); zonë e veçantë `hooks` për callback-et; Central 429 | nginx real: 400 kërkesa paralele; 300 callback pa 429/503 | VERIFIED LOKALISHT (vlerat PROPOSED) |
| Webhook/DLR: path, query, trup, `X-Signature`/`X-Twilio-Signature`, `Host` të paprekur; `proxy_pass` pa URI; testet ekzistuese të firmës kalojnë (261) | nginx real + pytest | VERIFIED LOKALISHT |
| Central: IP e login-it dhe e `/internal/*` pas `CENTRAL_TRUSTED_PROXY_HOPS` me helper të përbashkët `packages/netutil` (**N-2 RESOLVED**) | pytest (hops 0/1/pamjaftueshëm/spoof) + compose real: `login failed … ip=<klienti real>` | VERIFIED LOKALISHT |
| `/healthz` dhe `/readyz` vetëm të brendshme (loopback + `*_INTERNAL_ALLOW_CIDRS`), publikisht 403; `/nginx-health` për LB | nginx real + compose real | VERIFIED LOKALISHT |
| Rruga e brendshme Enterprise→Central: listener `central-web:8082` vetëm për `/internal/*`, allowlist (`CENTRAL_S2S_ALLOW_CIDRS`, parazgjedhja askush) para proxy-t; `/internal` = 404 në 8081; nënshkrimi i shërbimit mbetet autoritativ | compose real: i lejuar → 401 nga `central-api`; jashtë allowlist-ës → 403; rrjet Docker | VERIFIED LOKALISHT |
| Regjistrimi publik Central mbetet i mbyllur (`/registration` → 404) | nginx real + test | VERIFIED LOKALISHT |
| Porte: vetëm `127.0.0.1:8080`, `:8081`, `:8082`; asnjë port API | compose real + testet | VERIFIED LOKALISHT |
| CI (hapi `e2e/edge/check_edge.py` dhe të tjerët) | s'ka ekzekutuar | NOT VERIFIED (CI PENDING) |

**Mbeten të jashtme (asnjë nuk është mbyllur):** emrat/DNS real dhe rekordet; certifikatat (SAN, rinovim, TLS ≥ 1.2); LB/WAF/CDN real dhe sjellja e tij për XFF/Proto; CIDR-t reale (`*_TRUSTED_PROXY_CIDRS`, `CENTRAL_S2S_ALLOW_CIDRS`, `*_INTERNAL_ALLOW_CIDRS`); firewall/rrjeti; ridrejtimi 80→443 dhe HSTS te terminatori; vlerat reale të `limit_req` për provider-in; provat me provider real; testi me dy IP të jashtme përmes terminatorit real.

**Pika të dokumentuara për operatorin:** (1) me proxy në host dhe port të publikuar në loopback, nginx sheh adresën e gateway-t Docker, jo `127.0.0.1`; (2) `*_INTERNAL_ALLOW_CIDRS` ka parazgjedhje `172.16.0.0/12` (rrjetet e parazgjedhura Docker): rregulloje nëse rrjeti yt është tjetër; (3) `CENTRAL_S2S_ALLOW_CIDRS` duhet të jetë IP-ja e Enterprise ashtu siç e sheh nginx pas `real_ip`; (4) `hops>1` nuk është zgjidhje e përgjithshme: normalizimi bëhet nga nginx.

## 0d. Përditësim M11-I/F-03 — siguria e nisjes dhe e sekreteve

Zbatuar (kod + teste, pa ndryshuar algoritmet e nënshkrimit, provider-in apo rrugëzimin): `Settings._startup_safety_problems()` te `app/core/config.py` (thirret nga `production_problems()`) dhe `Settings.production_problems()` te `apps/central/core/config.py` (thirret nga `create_app` në prodhim). Të gjitha janë **të kushtëzuara nga funksioni i aktivizuar**; asnjë kredencial provider-i nuk bëhet global i detyrueshëm.

| Gate | Kushti | Dev/test |
|---|---|---|
| `SMS_TRUSTED_PROXY_HOPS` = 1 | gjithmonë në prodhim (topologjia F-02: hops=0 e përmbledh çdo klient te IP e proxy-t; >1 nuk është topologji e miratuar) | e pandryshuar (parazgjedhja 0) |
| `SMS_DLR_SECRETS[<SMS_HTTP_PROVIDER_NAME>]` i fortë (≥ 24 kar., jo `change/dev/test/demo…`) | vetëm kur provider-i HTTP është aktiv (`SMS_HTTP_PROVIDER_URL` i vendosur) | jo |
| Çdo hyrje tjetër e `SMS_DLR_SECRETS` e vendosur nuk është e dobët/placeholder | kur ka hyrje jo-bosh | jo |
| `SMS_TWILIO_ACCOUNT_SID` dhe `SMS_TWILIO_AUTH_TOKEN` së bashku ose asnjëri | gjysmë-konfigurimi e çaktivizon provider-in heshturazi | jo |
| `SMS_SMTP_HOST` | vetëm kur `SMS_EMAIL_PROVIDER=smtp` | jo |
| `CENTRAL_TRUSTED_PROXY_HOPS` = 1 | gjithmonë në prodhim (vetëm log i IP-së) | jo |
| `CENTRAL_SMTP_HOST` + `CENTRAL_SMTP_FROM` | vetëm kur `CENTRAL_MAILER=smtp` | jo |

Mbrojtjet ekzistuese mbeten: SQLite/jo-PG, fshehtësitë PII/Fernet, çelësi admin i dobët, `https` publik, `fake` email/pagesë, gate-t CP/authority; Central: sekret auth ≥ 32, auto-regjistrim i pa-verifikuar, mailer/bot `fake`, jo-PG. Mesazhet e gabimit emërtojnë vetëm variablat (provuar: asnjë vlerë sekrete në përjashtim). Twilio verifikohet me `X-Twilio-Signature` (jo `dlr_secrets`) — nuk kërkohet sekret HMAC për të.

**Teste:** `tests/test_f03_startup_safety.py` (26). Tri teste ekzistuese që izolonin gate-t e tjera të Central (`test_central_packaging.py` ×2, `test_central_auth.py` ×1) tani vendosin `trusted_proxy_hops=1`.

**Ndarja e GAP-17:** (a) mbrojtjet e nisjes/sekreteve — **RESOLVED / LOCALLY VERIFIED; CI PENDING**; (b) 2FA e detyrueshme e stafit Central — **EXTERNAL / POLICY DECISION REQUIRED** (nuk zbatohet në F-03; kontrolli kompensues SC-06 mbetet).

**Mbeten të jashtme:** vlerat reale të sekreteve, kredencialet e provider-it, vendimi i politikës 2FA, CI jeshile.

## 0e. Përditësim M11-I/F-04 — forcimi i CI

| Ndryshim | Detaje | Status |
|---|---|---|
| Skanim sekretesh | Job `secret-scan`: gitleaks 8.21.2 i fiksuar me SHA-256, `gitleaks dir .` mbi checkout-in (pa histori, pa `fetch-depth: 0`), `--redact`, `.gitleaks.toml` (rregullat bazë + allowlist e ngushtë: tre vlera sintetike të emërtuara + një skedar test për PEM-in e prishur; asnjë përjashtim i përgjithshëm i `tests/`, `docs/`, `.github/`, `.env*`). BLLOKUES. | IMPLEMENTED / LOCALLY VERIFIED (0 gjetje në pemën aktuale; rrjedhje sintetike e përkohshme zbulohet në `tests/` dhe `docs/`); CI EXECUTION PENDING |
| Historia e plotë | Jo e përfshirë (173 commit-e; pritet zhurmë nga fixtures të vjetra). | DEFERRED / MANUAL SECURITY TASK |
| Central E2E | Job `central-e2e` ekzekuton `e2e/run_central_e2e.sh` (SQLite i përkohshëm, kredenciale sintetike të gjeneruara, uvicorn + Vite lokalë, Chromium; roja «pa ofrues të vërtetë» e M11-H; port të lirë ose refuzon). Ishte `continue-on-error`; **tani BLLOKUES**. | IMPLEMENTED; **3/3 GREEN në GitHub CI** (82b1895, 256f282, 4162d99; 140+56 kontrolle, ~95 s) dhe u bë bllokues në fazën e performancës |
| Chromium portabël | `e2e/browser_util.py`: `E2E_CHROME` nëse është e ekzekutueshme, përndryshe Chromium i Playwright; gabim i qartë nëse s'nis. U hoq rruga e ngulitur `/opt/pw-browsers/...` nga tre skriptat. | IMPLEMENTED / LOCALLY VERIFIED (rruga fallback me Chromium të instaluar nga Playwright provohet vetëm në CI) |
| Artefakte dështimi | `if: failure()`, `upload-artifact@v4`, 7 ditë: vetëm `logs/` dhe pamje ekrani (`E2E_ARTIFACTS`); kurrë `seed.out`, DB, çelësa, ids. | IMPLEMENTED / LOCALLY VERIFIED (screenshot në dështim provuar lokalisht) |
| Koka Enterprise | `alembic heads` = një kokë; pas `upgrade head` (dhe pas down/up) `current` = `heads`; pa numër të ngulitur. | IMPLEMENTED / LOCALLY VERIFIED |
| Dependabot | Shtuar npm `/frontend-central`. | IMPLEMENTED |
| `timeout-minutes` | backend 90, e2e 30, central-e2e 30, docker 45, secret-scan 10 (të gjera, pa kohë reale CI). | IMPLEMENTED |
| Teste | `tests/test_f04_ci_hardening.py` (11 kontrolle statike të invariantëve). | VERIFIED LOKALISHT |

**Gjetje (kontradiktë pre-existing, ZGJIDHUR me korrigjim vetëm-teste):** `e2e/test_console.py` (E2E Enterprise, job `e2e` BLLOKUES) dështonte edhe në revision-in para F-04, sepse CI s'kishte ekzekutuar kurrë dhe asertimet ishin të vjetruara nga ndryshimet e miratuara të UI: (1) M11-F: importi i pjesshëm CSV tregon tani paralajmërim (`.alert.warn`, «Partly imported», rreshtat e refuzuar), jo `.alert.good`; (2) M11-G: totalet e përdorimit «SMS sent» dhe eksportet CSV janë te skeda **Usage**, ndërsa faqja hapet te Overview; (3) pritje e munguar pas toast-it «Keyword saved» (race me rifreskimin e listës, i dukshëm në PG). Asertimet tani provojnë rezultatin semantik (2 të shtuar, 1 i refuzuar me arsye, jo sukses i plotë; totalet e Usage > 0). Sjellja e aplikacionit është e saktë dhe e mbuluar nga `tests/test_reports.py`, `frontend/src/__tests__/{reports,messaging}.test.jsx` (kontroll: `usage.sms.count` = `overview.messages.total` = 21 në të dhënat e seed-it). **Asnjë kod aplikacioni nuk u ndryshua; `e2e` mbetet BLLOKUES.** Provë lokale: 3 ekzekutime radhazi PASS mbi PostgreSQL (si CI) dhe 3 mbi SQLite.

**Evidencë nga ekzekutimi real i parë në GitHub (F-04):** ruff i pa-fiksuar formatonte blloqet e kodit në dy dokumente (u fiksua `ruff==0.17.0`, dokumentet u formatuan); bandit (pa version, hera e parë që ekzekutohej) dha 11 gjetje B608, nga të cilat 2 (`backfill_enterprise_ids`, argumenti operator `--table`) nuk ishin vetëm-konstante: u mbyll një boshllëk validimi të identifikuesit të tabelës me allowlist `LEGACY_OWNER_TABLES` (refuzim para çdo SQL-je; **SECURITY HARDENING / FAIL-CLOSED INPUT VALIDATION**, jo veçori) dhe 11 `# nosec B608` janë vetëm në rreshtat e gjetur; bandit u fiksua në `bandit==1.9.4`. CI duhet ri-ekzekutuar; Central E2E 2/3 GREEN; F-04 nuk është i mbyllur.

**Performancë e CI (F-04, faza e shardimit):** ekzekutimi i parë real tregoi PG pytest 65m55s dhe >89m (anuluar nga timeout-i 90 min, runner ~1.75× më i ngadaltë; 0 dështime deri në 75%). Zgjidhja është strukturore, jo rritje timeout-i: `backend` u nda në `backend-static` (ruff/format/bandit + provë ndarjeje), `pytest-pg` (4 shard-e deterministe, secili me PG të vet), `pytest-sqlite` (2 shard-e), `migrations-backup` (PG i vet) dhe porta `backend-ci-gate`. Shard-i = `sha1(nodeid) mod N` (`tests/ci_shard.py`, provë `scripts/ci_shard_proof.py`: bashkim = suita e plotë, prerje = bosh). `docker` dhe `e2e` (Enterprise) nuk varen më nga backend. IMPLEMENTED LOCALLY; CI RERUN REQUIRED.

**Mbeten (CI/provë e jashtme):** ekzekutimi real në GitHub i të gjitha jobeve, 3 ekzekutime të gjelbra radhazi për `central-e2e`, koha reale e PR (sharding PG i shtyrë), branch protection/required checks dhe GitHub secret scanning (EXTERNAL EVIDENCE REQUIRED). CI i gjelbër NUK mbyll: DNS, TLS, LB/WAF, CIDR reale, menaxherin e sekreteve, backup/PITR, monitorimin, canary-n e provider-it, cutover/rollback.


## 0f. Përditësim M11-I/F-05A — backup, restore dhe gatishmëria e rikuperimit (lokal/staging-grade)

**Numërimi i miratuar i milestone-it:** F-05 = backup PostgreSQL, PITR dhe gatishmëria e restore (tabela e vjetër e "F-04/F-05" më poshtë është histori e propozimit, jo numërimi i miratuar). Ndarja: **F-05A** = kod/konfig/runbook + drill lokal; **F-05B** = dëshmi reale prodhimi (BLOCKED, kërkon infrastrukturë).

**Çfarë u zbatua (vetëm skripte/doc/teste; ASNJË kod aplikacioni ose biznesi):** `scripts/backup.sh` dhe `scripts/restore.sh` të forcuar (etiketë enterprise|central e detyrueshme dhe e verifikuar nga përmbajtja, checksum i detyrueshëm, validim i plotë i dump-it, `.meta.json` pa kredenciale, rotacion për etiketë, restore atomik në bazë të re me pastrim të bazës së dështuar, kontroll kokë migrimesh); `scripts/recovery_verify.py` (`head`, `fingerprint`, `compare`; pa importe `apps.*`) dhe mjeti i Central `apps/central/tools/recovery_ledger.py`; `scripts/recovery_drill.py` (drill i plotë për të dyja bazat); `tests/test_f05_recovery.py` (12 teste); round-trip i `migrations-backup` në CI shtrirë te Central + kontroll koke. Dokumente: `m11i-postgres-backup-restore-runbook.md`, `m11i-postgres-pitr-runbook.md`, `m11i-recovery-evidence-checklist.md`.

**Gjetje të rregulluara:** `restore.sh` printonte URL-në me fjalëkalim në dalje; kredencialet shkonin si argumente procesi; emri `sms-*.dump` përzinte bazat; mungonte kontrolli i kokës dhe ledger-i i Central; restore i dështuar linte bazë të gjysmë; checksum i munguar ishte vetëm paralajmërim. **Klasifikimi:** F-05A = LOCAL/STAGING RECOVERY READINESS VERIFIED — PRODUCTION PITR EVIDENCE STILL REQUIRED. F-05B (WAL/PITR real, off-host + enkriptim, alarme, pronar, RPO/RTO të matur) = **BLOCKED — REAL PRODUCTION INFRASTRUCTURE REQUIRED**. RPO/RTO janë **POLICY PROPOSAL**, jo të arritur. Gatishmëria e prodhimit mbetet **BLOCKED**.

## 1. Veprimet e ardhshme që heqin bllokuesit (jo të zbatuara)

| ID | Veprimi i ardhshëm | Heq |
|---|---|---|
| F-01 | Imazh/target Central + shërbim dhe migrim Central në compose + `CENTRAL_*` në shabllonin e mjedisit | GAP-01 — **IMPLEMENTED / LOCALLY VERIFIED; CI PENDING** |
| F-02 | Build/imazh për `frontend-central` dhe servirje statike | GAP-02 — **IMPLEMENTED / LOCALLY VERIFIED; CI PENDING** |
| F-03 | Rrugëzim proxy/nginx për Central (publike/private), `limit_req` për `/auth/token` | GAP-03 — **IMPLEMENTED / LOCALLY VERIFIED** (rrugët publike vs private në topologjinë reale mbeten vendim/provë të jashtme) |
| F-04 | Hapa CI: Vitest/build Central, E2E Central, `docker compose config`, skanim sekretesh, imazh Central | GAP-14, GAP-15 — **PARTIALLY IMPLEMENTED** (Vitest/build Central, imazh Central, `nginx -t`, `compose config`, kokat Central; **mbeten**: E2E Central dhe skanim sekretesh); CI PENDING |
| F-05 | Mbrojtje nisjeje: `SMS_DLR_SECRETS`, kredenciale provider-i; vendim për 2FA të stafit Central | GAP-17 — **PARTIALLY IMPLEMENTED** (vetëm refuzimi i SQLite/jo-PG te Central; `SMS_DLR_SECRETS`/provider/2FA mbeten) |
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
| Status pas F-01 | **IMPLEMENTED / LOCALLY VERIFIED; CI VERIFICATION PENDING.** Stage `central` te `Dockerfile` (Enterprise mbetet i fundit), `central-migrate` → `central-api` në compose; imazhi u ndërtua nga checkout i pastër, u nis me compose lokal mbi PG, migroi te 0031, `/readyz` healthy, user 10001, FS read-only, pa sekrete në imazh. Nuk është vendosur në prodhim. |

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
| Status pas F-01 | **IMPLEMENTED / LOCALLY VERIFIED; CI VERIFICATION PENDING.** `deploy/web-central.Dockerfile` (npm ci + build + nginx); hyrja admin/operator provuar me Chromium përmes nginx real, 0 gabime console. 232 teste Vitest + `i18n:check` + build kaluan. |

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
| Status pas F-01 | **IMPLEMENTED / LOCALLY VERIFIED.** `deploy/nginx.central.conf`: vetëm `/auth`, `/admin`, `/healthz`, `/readyz` te `central-api`; `/v1`, `/webhooks`, `/u`, `/internal`, `/registration` = 404; kufizim `limit_req` (429) për `/auth`; pa HSTS dhe pa TLS (HTTP lokal). Vlera e saktë e `CENTRAL_TRUSTED_PROXY_HOPS` në topologjinë reale = GAP-08 (e hapur). |
| Status pas F-02 | **IMPLEMENTED / LOCALLY VERIFIED.** Sipërfaqja publike e stafit (8081) kurrë s'shërben `/internal`; listener i brendshëm 8082 për Enterprise→Central me allowlist; `/readyz` i brendshëm; `Cache-Control: no-store` te `/auth` dhe `/admin`. Emri/rruga e jashtme HTTPS për 8082 (central-internal) = EXTERNAL EVIDENCE REQUIRED. |

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
| Gjendja aktuale | Në repo: `scripts/backup.sh` (pg_dump custom + checksum + rotacion 14 ditë + `pg_restore --list`) dhe `scripts/restore.sh` (në DB të re). CI bën round-trip backup/restore + kokë + ledger për Enterprise dhe Central në PG 16 (F-05A). Compose përdor `postgres:16-alpine` me volum lokal. |
| Evidenca | `scripts/backup.sh`, `scripts/restore.sh`, `.github/workflows/ci.yml`; `docs/RUNBOOK.md` §4; `docs/M10_PROD_OPERATIONS.md` §5 (PITR/WAL = EXTERNAL BLOCKER). |
| Klasifikimi | Skriptet, drill-i lokal dhe round-trip në CI (Enterprise + Central, F-05A): VERIFIED; PITR i dokumentuar, pa provë (`m11i-postgres-pitr-runbook.md`). PG prodhimi, backup off-host, PITR/WAL, restore i provuar në infrastrukturën reale: EXTERNAL EVIDENCE REQUIRED. Rreshti `/backup.sh` në koment të `backup.sh` supozon skriptin brenda kontejnerit `db`, por compose monton vetëm `./backups` (PARTIALLY VERIFIED, shih kontradiktat). |
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
| Gjendja aktuale | *(Gjendja historike e auditit para F-05A: `backup.sh` ishte gjenerik dhe pa procedurë për Central.)* **Pas F-05A:** `backup.sh`/`restore.sh` mbulojnë Central me `BACKUP_LABEL=central`, me kontroll koke (`0031`) dhe ledger (`apps.central.tools.recovery_ledger`), të provuara lokalisht dhe në CI; procedura është te `docs/m11i-postgres-backup-restore-runbook.md`. PITR/WAL ose replikë sinkrone për Central mbetet bllokues operativ (`docs/M9G_BILLING.md`, `docs/m11i-postgres-pitr-runbook.md`). |
| Evidenca | `docs/M9G_BILLING.md` (bllokuesit operativë); `docs/M10_PROD_OPERATIONS.md` §5; `docs/M9_PRODUCTION_CHECKLIST.md` §0 (backup i të dy bazave). |
| Klasifikimi | Skriptet dhe procedura/drill-i lokal për Central: VERIFIED (F-05A). Restore real në prodhim dhe PITR Central: EXTERNAL EVIDENCE REQUIRED. |
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
| Status pas F-02 | **PARTIALLY RESOLVED.** Origjinat janë HTTP-only dhe pa HSTS; modeli i terminatorit të jashtëm, DNS/SAN, rinovimi, TLS ≥ 1.2, ridrejtimi 80→443 dhe renditja e HSTS janë dokumentuar (RUNBOOK §2/§2a/§2b). **Mbeten të jashtme:** terminatori real, DNS, certifikatat, aktivizimi real i HSTS, provat `curl`/`openssl` nga jashtë. |

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
| Status pas F-02 | **PARTIALLY RESOLVED.** Modeli `hops=1` pas `real_ip` me CIDR të operatorit (asnjë e besuar si parazgjedhje; `/0` refuzohet); helper i përbashkët Enterprise/Central; Central login/internal përdorin tani hops; provuar me nginx real dhe compose real. **Mbetet e jashtme:** CIDR-t reale dhe testi me dy IP përmes terminatorit real. |

### GAP-09 — Menaxhimi i sekreteve

| Fusha | Përmbajtja |
|---|---|
| Gjendja aktuale | `.env` jashtë git (`.gitignore`, `.dockerignore`); `.env.example` ka vetëm placeholder-a; çelësi privat Ed25519 për CP montohet si skedar read-only. Nuk ka vault/KMS, skanim sekretesh në CI, as rrotullim të `SMS_SECRETS_KEY`/`SMS_PII_HMAC_KEY`. |
| Evidenca | `.gitignore`; `.env.example`; `docker-compose.prod.yml`; `docs/RUNBOOK.md` §7; `.github/workflows/ci.yml` (hap `secret-scan`, F-04), `.gitleaks.toml`. |
| Klasifikimi | Higjiena e repo-së: VERIFIED. Vendi real i sekreteve, kush ka qasje: EXTERNAL EVIDENCE REQUIRED. Skanim sekretesh: gitleaks në CI mbi pemën aktuale (F-04: IMPLEMENTED / LOCALLY VERIFIED; CI EXECUTION PENDING); historia e plotë DEFERRED / MANUAL SECURITY TASK. |
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
| Status pas F-01 | **PARTIALLY IMPLEMENTED; CI PENDING.** Shtuar: Central migrations në PG (kokë 0031), Vitest/build `frontend-central`, imazh Central + smoke, build web Central, `nginx -t` i të dy nginx-eve, `compose config`. **Mbeten:** E2E Central, skanim sekretesh. Asnjë nga hapat s'ka ekzekutuar në CI. |
| Status pas F-04 | **IMPLEMENTED / LOCALLY VERIFIED; CI EXECUTION PENDING.** Shtuar: skanim sekretesh (**CURRENT TREE ONLY**; historia e plotë **DEFERRED / MANUAL SECURITY TASK**), E2E Central (**tani BLLOKUES (3/3 GREEN)**), koka Enterprise, artefakte dështimi, timeouts, Dependabot `frontend-central`. **Mbeten:** ekzekutim real në GitHub; sharding PG (DEFERRED); E2E Enterprise u ri-aliniua (vetëm teste, §0e) dhe mbetet bllokues. |

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
| Status pas F-01 | **LOCALLY VERIFIED (compose renders + stack Central u nis); CI PENDING.** `docker compose config` kaloi; Central u nis realisht me compose lokal. Stack-u i plotë i prodhimit (Enterprise + workers + provider) NUK u nis. |

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
| Status pas F-01 | **PACKAGED (explicit/manual), NOT SCHEDULED.** Profili `central-jobs`: `central-billing-run` (refuzon kur autoriteti ≠ central), `central-send-notifications` (një kalim), `central-tool` (parazgjedhje `sender_central_readiness`). Planifikimi, heartbeat-i dhe monitorimi mbeten EXTERNAL EVIDENCE REQUIRED. |

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
| Status pas F-01 | **PARTIALLY IMPLEMENTED.** Në prodhim Central refuzon SQLite/jo-PostgreSQL (nisja) dhe migrimi refuzon SQLite dhe një DB Enterprise; gate-t ekzistuese nuk u dobësuan. **Mbeten:** `SMS_DLR_SECRETS`/provider; 2FA e stafit Central. |
| Status pas F-03 | **Nisja/sekretet: RESOLVED / LOCALLY VERIFIED; CI PENDING** (§0d: proxy hops, `SMS_DLR_SECRETS` kushtëzuar nga provider-i HTTP aktiv, Twilio gjysmë-konfigurim, SMTP, Central hops/SMTP). **2FA e stafit Central: EXTERNAL / POLICY DECISION REQUIRED** — GAP-17 nuk mbyllet i tërë. |

## 3. Kontradiktat e gjetura në dokumentacion

| # | Kontradikta | Evidenca | Klasifikim |
|---|---|---|---|
| C-1 | `docs/RUNBOOK.md` §7 përmend provider SMS 'HTTP/SMPP'; në `app/providers` ka vetëm `fake`, `http`, `twilio` (asnjë SMPP në `app/`) | `docs/RUNBOOK.md`; `app/providers/__init__.py`; kërkim në `app/` | VERIFIED |
| C-2 | Komenti i `scripts/backup.sh` thërret `/backup.sh` brenda kontejnerit `db`, por compose monton vetëm `./backups:/backups` (skripti nuk është i montuar) | `scripts/backup.sh`; `docker-compose.prod.yml` | PARTIALLY VERIFIED (nuk është ekzekutuar) |
| C-3 | HSTS dërgohej mbi HTTP të thjeshtë (nginx Enterprise dhe aplikacioni). **RESOLVED (F-02):** hequr nga të dyja; pronari është terminatori i TLS (RUNBOOK §2b). TLS real dhe aktivizimi i HSTS = EXTERNAL EVIDENCE REQUIRED. | `deploy/nginx.conf`; `app/main.py` | VERIFIED LOKALISHT |
| C-4 | Docstring-u i `app/worker.py` liston vetëm një pjesë të roleve; kodi pranon edhe `pricing_control_plane`, `billing_usage_reporter`, `money_usage_reporter` | `app/worker.py` | VERIFIED (kozmetike) |
| C-5 | `docs/M10_PROD_OPERATIONS.md` kokat 0032/0029 në kohën e shkrimit; aktualet 0034/0031 (dokumenti e pranon) | `docs/M10_PROD_OPERATIONS.md` | VERIFIED (e shënuar) |
| C-6 | `.env.example` këshillon përdorues PostgreSQL me të drejta minimale (jo superuser); compose lidh aplikacionin si `sms`, përdoruesi i krijuar nga `POSTGRES_USER` në imazhin zyrtar, që është superuser | `.env.example`; `docker-compose.prod.yml` | PARTIALLY VERIFIED (nga semantika e imazhit; s'është ekzekutuar) |
| C-3b | Humbja e header-ave te `/` dhe `/assets/` të nginx Enterprise (N-1). **RESOLVED (F-02).** | `deploy/nginx.conf` | VERIFIED LOKALISHT |
| C-7 | `docs/RUNBOOK.md` §7 pohonte se Central s'është i paketuar; **zgjidhur nga F-01** (§7 përditësuar, §8 shtuar) | `docs/RUNBOOK.md` | VERIFIED (rregulluar) |

## 4. Tabela e bllokuesve sipas varësisë

Rendi është i detyrueshëm: një rresht nuk mund të mbyllet para varësive të tij.

| Rendi | Bllokues | Lloji | Varet nga | Evidenca që mbyll |
|---|---|---|---|---|
| 1 | GAP-04 Infrastruktura | EXTERNAL | — | Inventar i firmosur i hostit |
| 2 | GAP-07 TLS/DNS/LB | EXTERNAL (konfigurimi i skajit: **IMPLEMENTED / LOCALLY VERIFIED**, F-02) | 1 | `curl`/`openssl` nga jashtë |
| 3 | GAP-08 Trusted proxy hops | EXTERNAL + konfigurim (modeli, real_ip dhe testet: **IMPLEMENTED / LOCALLY VERIFIED**, F-02; CIDR-t reale të jashtme) | 2 | Test me dy IP përmes terminatorit real |
| 4 | GAP-09 Sekretet | EXTERNAL | 1 | Inventar + skanim 0 gjetje |
| 5 | GAP-05 PG prodhimi, backup, PITR | EXTERNAL | 1, 4 | Restore + `verify_ledger` + PITR i provuar |
| 6 | GAP-01 Central backend | Kod — **IMPLEMENTED / LOCALLY VERIFIED; CI PENDING** | — | Imazh + migrim 0031 + `/readyz` në staging |
| 7 | GAP-02 Central frontend | Kod — **IMPLEMENTED / LOCALLY VERIFIED; CI PENDING** | 6 | Build nga imazhi, 0 gabime console |
| 8 | GAP-03 Proxy për Central | Kod/konfigurim — **IMPLEMENTED / LOCALLY VERIFIED** | 2, 6 | Matrica e rrugëve |
| 9 | GAP-14 CI, GAP-15 compose | Kod — **IMPLEMENTED / LOCALLY VERIFIED; CI EXECUTION PENDING** (F-04) | 6, 7 | CI jeshile; staging healthy |
| 10 | GAP-17 Mbrojtje nisjeje/2FA Central | Kod — nisja/sekretet **RESOLVED / LOCALLY VERIFIED** (F-03); 2FA Central: **POLICY DECISION REQUIRED** | 6 | Teste refuzimi |
| 11 | GAP-06 Backup/restore Central | EXTERNAL | 5, 6 | Restore Central i provuar |
| 12 | GAP-12 Monitorim/alarme | EXTERNAL | 1 | Test alarmi i kaluar |
| 13 | GAP-16 Procese të planifikuara Central | EXTERNAL | 6, 12 | Heartbeat + `due_unprocessed=0` |
| 14 | GAP-10 Kredenciale provider | EXTERNAL | 4 | Prova sandbox e miratuar |
| 15 | GAP-11 Canary provider/DLR | EXTERNAL | 2, 10, 12 | Canary pa UNKNOWN |
| 16 | GAP-13 Evidenca e cutover-it | EXTERNAL | Të gjitha më sipër | `docs/M10_PROD_OPERATIONS.md` §9 plotësisht PO |

## 5. Përfundimi

**BLOCKED.** 17 boshllëqe në 16 rreshta varësie; 11 rreshta kërkojnë dëshmi të jashtme (të pandryshuar nga F-01). Nga 5 rreshtat e kodit/konfigurimit (6–10), pas F-01: 3 (rreshtat 6–8) janë IMPLEMENTED / LOCALLY VERIFIED, 2 (rreshtat 9–10) PARTIALLY IMPLEMENTED; asnjë nuk është verifikuar ende në CI. Asnjë nuk mbyllet me dokumentim. Statusi mund të kalojë në CONDITIONALLY READY vetëm kur rreshtat 1–11 janë të mbyllur me evidencë, dhe në PRODUCTION READY vetëm pas rreshtit 16.
