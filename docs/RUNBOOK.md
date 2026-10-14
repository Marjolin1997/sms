# Runbook i prodhimit

Një host me Docker Compose (`docker-compose.prod.yml`): `db` (PostgreSQL 16), `migrate` (një herë), `api`, `worker` (SMS/email/fushata/faturim), `webhook-worker`, `web` (nginx + konsola). Asgjë nuk publikohet përveç `web` te `127.0.0.1:8080`: vendosni TLS përpara tij (Caddy/Traefik/nginx i hostit/load balancer).

## 1. Nisja e parë
1. `cp .env.example .env` dhe plotësoni **sekrete të vërteta** (kurrë në git):
   - `POSTGRES_PASSWORD` (e gjatë, e rastësishme)
   - `SMS_PII_HMAC_KEY` = `python -c "import secrets;print(secrets.token_hex(32))"` — **mos e ndryshoni kurrë** pasi ka të dhëna (hash-et e pëlqimit nuk do të gjenden më)
   - `SMS_SECRETS_KEY` = `python -c "from cryptography.fernet import Fernet as F;print(F.generate_key().decode())"` — ruajeni në një vend të sigurt të veçantë nga backup-et e DB-së (pa të, çelësat DKIM, sekretet e webhook-ut dhe 2FA nuk dekriptohen)
   - `SMS_ADMIN_API_KEY` (bootstrap, 24+ karaktere), `SMS_PUBLIC_BASE_URL=https://…`, provider-i email (`SMS_EMAIL_PROVIDER=smtp` + SMTP), provider-i SMS
   - `SMS_PAYMENT_PROVIDER=disabled` derisa të lidhet një gateway i vërtetë
2. `docker compose -f docker-compose.prod.yml --env-file .env up -d --build`
   `up -d` i zakonshëm mbetet vetëm-Enterprise; Central është opt-in (`--profile central`, shih §8).
   Aplikacioni **refuzon të nisë** me konfigurim të pasigurt (`SMS_ENV=production`); `docker compose logs api` liston çdo problem.
3. Kontrolli (nga host-i; publikisht `/readyz` është 403, shih §2): `curl -s http://127.0.0.1:8080/readyz` → `{"status":"ready"}` (kthen 503 nëse DB s'përgjigjet ose skema s'është në versionin e kodit).
4. **Stafi i parë** (me çelësin bootstrap; shih README → “Nisje”): krijoni një çelës `superadmin` personal për çdo person, hyni në konsolë me të, aktivizoni 2FA te *Siguria*. Pastaj **hiqni `SMS_ADMIN_API_KEY`** nga `.env` dhe rinisni `api` (bootstrap-i është vetëm për nisjen). Vendosni `SMS_REQUIRE_STAFF_2FA=true`.

## 2. TLS, DNS dhe IP e klientit (M11-I/F-02)
**Modeli i synuar:** një terminator i JASHTËM i TLS (LB/WAF/proxy) përpara nginx-eve të aplikacionit, që mbeten **HTTP-only** në loopback/rrjet privat. Asnjë certifikatë nuk futet në kontejnerët e aplikacionit.
```
Internet → DNS → terminator TLS/LB/WAF → nginx (web 8080 / central-web 8081 + 8082) → api / central-api (të brendshme)
```
| Hop | Përfundon TLS | Vendos `X-Forwarded-For` | Vendos `X-Forwarded-Proto` | Besohet nga nginx | Vlera në aplikacion |
|---|---|---|---|---|---|
| terminator TLS/LB/WAF (CDN/WAF para tij, nëse ka) | po; ridrejton 80→443; **zotëron HSTS** | shton IP-në e lidhjes së klientit (kurrë mos kalo një XFF të klientit të pandryshuar) | `https` | — | — |
| nginx (`web`, `central-web`) | jo (HTTP) | **normalizon**: dërgon vetëm `$remote_addr` pas `real_ip` | `https` vetëm kur peer-i është proxy i besuar që e deklaron; ndryshe `http` | vetëm CIDR-t te `*_TRUSTED_PROXY_CIDRS` | `SMS_TRUSTED_PROXY_HOPS=1`, `CENTRAL_TRUSTED_PROXY_HOPS=1` (kurrë më të mëdha si zgjidhje e përgjithshme) |

- **Proxy-t e besuar:** `WEB_TRUSTED_PROXY_CIDRS` dhe `CENTRAL_WEB_TRUSTED_PROXY_CIDRS` te `.env` (shih `.env.example`). Parazgjedhja: **asnjë**; një `/0` refuzohet nga nginx në nisje. Me terminator në host dhe port të publikuar në loopback, nginx sheh adresën e gateway-t të rrjetit Docker (p.sh. `172.18.0.1`), jo `127.0.0.1`: merre nga `docker compose logs web` dhe vendose si CIDR `/32`.
- **`real_ip`:** `real_ip_header X-Forwarded-For; real_ip_recursive on;` pas së cilës `$remote_addr` është klienti real; `limit_req` dhe kyçja e provave përdorin të njëjtën IP.
- **Kërkesa ndaj terminatorit:** të **shtojë** IP-në e lidhjes së klientit te `X-Forwarded-For` (ose ta mbishkruajë), të mos e kalojë header-in e klientit siç është, dhe të vendosë `X-Forwarded-Proto: https`.
- **Health:** terminatori/LB provon vetëm `GET /nginx-health` (vetëm gjallëria e nginx). `/healthz` dhe `/readyz` janë **të brendshme**: loopback dhe `WEB_INTERNAL_ALLOW_CIDRS` (parazgjedhja `172.16.0.0/12`, rrjetet e parazgjedhura Docker; vendos CIDR-n e rrjetit tënd nëse është tjetër); publikisht 403. `/readyz` zbulon arsye të brendshme.
- **Webhooks/DLR:** `/webhooks/*` kalojnë nga `enterprise.<domen>`; autentikimi është nënshkrimi (HMAC / `X-Twilio-Signature` mbi `SMS_PUBLIC_BASE_URL`), jo IP-ja; nginx ruan path-in dhe query (`proxy_pass` pa URI) dhe i jep callback-eve zonë kufizimi të veçantë (`hooks`, më e gjerë se ajo e klientëve). `SMS_PUBLIC_BASE_URL` duhet të jetë saktësisht origjina publike HTTPS e `enterprise.<domen>`.
- **Kufizimi i shpejtësisë:** kthen **429** (jo 503); vlerat (`perip` 30 r/s, `hooks` 200 r/s, `central_auth` 5 r/s) janë PROPOSED dhe rregullohen sipas trafikut real.

### 2a. DNS dhe certifikata (konceptuale; asgjë s'është krijuar)
| Emri | Qëllimi | Rekord (konceptual) | Mbërrin te |
|---|---|---|---|
| `enterprise.<domen>` | konsola e klientit, API, `/webhooks/*`, `/u/*` | A/AAAA (ose CNAME te LB) | terminatori → `web` |
| `central.<domen>` | konsola dhe API e stafit Central | A/AAAA/CNAME | terminatori → `central-web:8081` |
| `central-internal.<domen>` (ose rrjet privat) | VETËM Enterprise→Central (`/internal/*`), jo publik | A/AAAA brenda ose me allowlist | terminatori → `central-web:8082` |
- Certifikata (të menaxhuara jashtë këtij repo-je): SAN për secilin emër të përdorur; TLS ≥ 1.2; rinovim automatik me alarm skadence (≥ 21 ditë, shih `m11i-observability-alerting-plan.md`); ridrejtimi HTTP→HTTPS i takon terminatorit.
- **Enterprise→Central:** `SMS_CP_BASE_URL=https://central-internal.<domen>` (HTTPS i detyrueshëm në prodhim). `CENTRAL_S2S_ALLOW_CIDRS` = IP-ja/rrjeti i Enterprise **ashtu siç e sheh nginx pas `real_ip`** (adresa publike e dalje e Enterprise kur kalon nga terminatori); bosh = askush (siguri e parazgjedhur). Autentikimi i kërkesave mbetet nënshkrimi Ed25519 i Central; allowlist-a është mbrojtje shtesë. `/internal/*` NUK shërbehet kurrë te `central.<domen>`.

### 2b. HSTS: pronësia dhe rendi i aktivizimit
HSTS **nuk dërgohet** nga nginx as nga aplikacioni (as Enterprise as Central); nginx hedh poshtë çdo HSTS që vjen nga upstream. Pronari është terminatori i TLS. Rendi:
1. Provo HTTPS në çdo emër (zinxhir, SAN, rinovim, ridrejtim 80→443, aplikacioni funksional nën HTTPS).
2. Aktivizo HSTS te terminatori me `max-age=300` (5 min), pa `includeSubDomains`; verifiko 24–48 orë.
3. Ngri në `86400`, pastaj `31536000`; `includeSubDomains` vetëm pasi të konfirmosh që TLS punon në **të gjitha** nënemrat; `preload` jo para konfirmimit.
4. **Rikthim:** `max-age=0` te terminatori (shfletuesit e harrojnë pas vizitës); asnjë ndryshim te nginx/aplikacioni.
HSTS nuk duhet aktivizuar para se HTTPS të jetë provuar: pasi shfletuesit e mbajnë mend, një HTTPS i prishur bëhet i paarritshëm për klientët derisa të skadojë `max-age`.

### 2c. Validimi i skajit
`python3 e2e/edge/check_edge.py` (nginx real në Docker; `EDGE_WEB_IMAGE`, `EDGE_CENTRAL_WEB_IMAGE`) provon `real_ip`, XFF/Proto, header-at, 429, webhooks, health dhe `/internal`. Ekzekutohet edhe në CI. Nuk zëvendëson provat me terminatorin dhe CIDR-t reale.

## 3. Përditësimet
1. Backup (seksioni 4), pastaj `git pull && docker compose -f docker-compose.prod.yml up -d --build`.
2. Shërbimi `migrate` ekzekuton `alembic upgrade head` para se të nisin `api`/workers; `readyz` kthen 503 derisa skema të jetë e rregullt.
3. **Rikthim:** migrimet kanë `downgrade`, por një rikthim që heq kolona me të dhëna humb ato: rikthimi i sigurt është *imazhi i vjetër + backup-i i marrë para përditësimit* te një bazë e re (seksioni 4).

## 4. Backup dhe restore
- Backup (format custom, checksum, rotacion 14 ditë): `docker compose -f docker-compose.prod.yml exec -T db sh -c 'pg_dump --format=custom --no-owner -U sms -d sms -f /backups/sms-$(date -u +%Y%m%dT%H%M%SZ).dump'` ose `scripts/backup.sh` nga një host me `pg_dump` (shton checksum dhe rotacion). Skedarët dalin te `./backups`.
- Cron (çdo natë 02:30): `30 2 * * * cd /opt/sms && DATABASE_URL=postgresql://… BACKUP_DIR=/opt/sms/backups scripts/backup.sh >> /var/log/sms-backup.log 2>&1`
- **Kopjoni backup-et jashtë hostit** (objekt-storage me enkriptim); një backup që jeton vetëm në të njëjtin disk nuk është backup.
- **Provoni restore-in çdo muaj:** `ADMIN_URL=postgresql://…/postgres scripts/restore.sh backups/sms-….dump sms_restored` (kurrë mbi bazën ekzistuese; skripti refuzon), pastaj `SMS_DATABASE_URL=postgresql+psycopg://…/sms_restored python -m scripts.verify_ledger` (balanca = SUM(delta) për çdo wallet, faturat pa boshllëqe). Dalja 0 = në rregull.
- Për të kaluar prodhimin te baza e restauruar: ndalni `api` dhe workers, ndryshoni emrin e bazës te `SMS_DATABASE_URL`/compose, nisni.

## 5. Monitorimi
- `GET /healthz` (procesi gjallë), `GET /readyz` (DB + skema). Docker healthcheck për `api`, `worker`, `webhook-worker` (heartbeat: një worker i ngecur del “unhealthy”).
- Çdo përgjigje ka `X-Request-ID` (nginx e gjeneron; kalon te API dhe log-et) për të ndjekur një kërkesë.
- Njoftimet për klientët: ngjarja `wallet.low_balance` te webhook-et; raportet te konsola.
- Alarmoni te: `unhealthy` te compose, 5xx në nginx, rritje e mesazheve `failed`, radhë `queued` që s'ulet (workers të ndaluar), fatura “Overdue”.

## 6. Incidente
| Situata | Veprimi |
|---|---|
| Provider-i SMS ka ndërprerje | Konsola → Admin → *Kill switches* → **dispatch** në pauzë (mesazhet mblidhen në radhë dhe dalin kur e vazhdoni) |
| Duam të ndalim mesazhet e reja | *Kill switches* → **submit** në pauzë |
| Një klient po abuzon | Llogaritë → *Ndalo dërgimin* |
| Çelës API i rrjedhur | Çelësat API → *Revoko* (menjëherë); pastaj krijoni një të ri; kontrolloni *Audit log* |
| Sulm me çelësa të gabuar | Kufizimi për IP kthen 429 automatikisht (`SMS_AUTH_MAX_FAILURES`); bllokoni IP-në edhe te firewall-i |
| Humbi telefoni i një stafi (2FA) | Superadmin tjetër ose bootstrap: `POST /v1/admin/api-keys/{id}/reset-2fa`; personi e konfiguron sërish |
| Dyshim për balancë të gabuar | `python -m scripts.verify_ledger`; korrigjim vetëm me *Financa → Korrigjim* (i audituar, i pandryshueshëm) |
| Disku i DB-së po mbushet | `event_retention_days` pastron ngjarjet; ledger/audit janë të pandryshueshme me qëllim: zmadhoni diskun |

## 6b. Skopimi i tenant-it (M1c)
- **Para se të nisësh versionin M1c në një mjedis me të dhëna:** `python -m scripts.enterprises_audit --check` → `alembic upgrade head` → `python -m scripts.backfill_enterprise_id` → `python -m scripts.enterprises_audit --check --strict` = **0/0**. Pa backfill, `/readyz` kthen 503 (rreshtat pa `enterprise_id` do të fshiheshin nga klientët).
- **Rikthim emergjent (pa ndryshuar kodin):** `SMS_TENANT_SCOPING=owner_ref` dhe rinis API/workers. Kthen skopimin te `owner_ref`. Mos e çaktivizo `SMS_ENTERPRISE_DUAL_WRITE` ndërsa skopimi është `enterprise` (aplikacioni refuzon të niset).
- Qasja ndër-tenant e stafit shfaqet në `GET /v1/admin/audit` si `cross_tenant.*`.

## 7. Çfarë NUK mbulohet ende
- **Central (`apps/central`) është i paketuar që nga M11-I/F-01** (imazh `--target central`, profilet opt-in `central` dhe `central-jobs`, `.env.central.example`; shih §8). Provuar vetëm lokalisht/dev; **nuk është vendosur në prodhim**. Mbeten jashtë paketimit: TLS/DNS/LB, `CENTRAL_TRUSTED_PROXY_HOPS` real, PostgreSQL prodhimi/PITR, sekretet, monitorimi, planifikimi i `billing_run`/`send_notifications`.
- `alembic downgrade` heq tabelat e shtuara pas objektivit (me të dhënat e tyre): rikthimi i prodhimit = imazhi i vjetër + backup (seksioni 3), jo downgrade.
- Pa gateway të vërtetë pagese (`SMS_PAYMENT_PROVIDER=disabled`; mbushjet bëhen manualisht nga Financa) dhe pa provider SMS të vërtetë të lidhur (HTTP/SMPP sipas dokumentacionit të tij).
- Rrotullimi i `SMS_SECRETS_KEY` dhe `SMS_PII_HMAC_KEY` nuk mbështetet (kërkon rienkriptim/rihash). Ruajini me kujdes.
- Auditim i jashtëm sigurie dhe pentest para se të shiten klientëve.

## 8. Central (staf admin/operator) — M11-I/F-01
**Opt-in:** shërbimet Central ndodhen te profili `central` (serving) dhe `central-jobs` (komanda manuale). `docker compose up -d` i zakonshëm mbetet **vetëm-Enterprise** dhe NUK kërkon `.env.central` as DB `sms_central`. Central niset me `--profile central` (që kërkon `.env.central`; pa të, compose dështon qartë).
Topologjia me `--profile central`: `db` (PG i njëjtë, **DB logjike e veçantë `sms_central`**) → `central-migrate` (një herë) → `central-api` (:8001, vetëm rrjeti i brendshëm) → `central-web` (nginx + konsola; **8081** sipërfaqja publike e stafit, **8082** listener i brendshëm Enterprise→Central për `/internal/*`; të dyja vetëm `127.0.0.1`). Central ka imazh të vetin (`target: central`), mjedis të vetin (`.env.central`, prefiks `CENTRAL_`) dhe NUK lexon `.env` të Enterprise. Profili `central-jobs` NUK niset nga `--profile central`.
1. **Hap infrastrukture (një herë; aplikacioni NUK e krijon DB-në):** `docker compose -f docker-compose.prod.yml --env-file .env exec db psql -U sms -d sms -c "CREATE ROLE sms_central LOGIN" -c "CREATE DATABASE sms_central OWNER sms_central"`, pastaj vendos fjalëkalimin në mënyrë interaktive (`\password sms_central` në `psql`), kurrë në argument komande, git apo chat.
2. `cp .env.central.example .env.central` (leje 600, jashtë git). Plotëso `CENTRAL_DATABASE_URL` (rol `sms_central`, DB `sms_central`) dhe `CENTRAL_AUTH_SECRET` (`python -c "import secrets;print(secrets.token_hex(32))"`). Me sekret bosh/<32 karaktere, SQLite ose mailer/bot-challenge `fake`, nisja në prodhim REFUZOHET.
3. Validim pa nisur asgjë: `docker compose -f docker-compose.prod.yml --env-file .env --profile central config -q`.
4. Migrimi (eksplicit, i dukshëm): `docker compose -f docker-compose.prod.yml --env-file .env --profile central run --rm central-migrate` (`alembic -c apps/central/alembic.ini upgrade head`; kokë 0031). Refuzon një DB Enterprise (gjen `sms_alembic_version`) dhe SQLite në prodhim. **Rikthimi i të dhënave NUK bëhet me `alembic downgrade`** (shih §3 dhe `docs/m11i-production-rollback-runbook.md`).
5. Nisja vetëm e Central (provuar lokalisht: nis `central-migrate` → `central-api` → `central-web` dhe `db`, jo shërbimet Enterprise): `docker compose -f docker-compose.prod.yml --env-file .env up -d central-web` (profili `central` aktivizohet nga emri i shërbimit). `docker compose … --profile central up -d` nis **Enterprise + Central** bashkë. Kontroll nga host-i: `curl -s http://127.0.0.1:8081/readyz` → `{"status":"ready"}` (503 nëse skema nuk është te koka; publikisht 403, shih §2).
6. Administratori i parë (fjalëkalimi kërkohet në terminal, nuk jepet në argument): `docker compose -f docker-compose.prod.yml --env-file .env --profile central-jobs run --rm --no-deps --entrypoint python central-tool -m apps.central.tools.create_admin --email <email> --role admin`.
7. Komanda të planifikuara/operacionale (profili `central-jobs`; asnjë nuk niset me `up -d`; Central s'ka scheduler, planifikimi është i jashtëm: cron/systemd): `docker compose -f docker-compose.prod.yml --env-file .env --profile central-jobs run --rm central-billing-run` (refuzon nëse autoriteti i faturimit s'është `central`), `... central-send-notifications` (një kalim; `--loop` vetëm me komandë të shprehur), `... central-tool [modul] [opsione]` (parazgjedhje `sender_central_readiness`).
8. Rrugëzimi i `central-web`: **8081 (publik, stafi):** `/auth`, `/admin` → `central-api` (me `Cache-Control: no-store`); `/healthz`, `/readyz` vetëm nga rrjeti i brendshëm; `/v1`, `/webhooks`, `/u`, `/internal`, `/registration` → 404; pjesa tjetër = SPA. **8082 (i brendshëm):** vetëm `/internal/*` (sinkron, para, çmime, faturim, sender) me allowlist (`CENTRAL_S2S_ALLOW_CIDRS`, parazgjedhja askush); gjithçka tjetër 404. HTTP i thjeshtë, **pa TLS dhe pa HSTS** (shih §2). `CENTRAL_TRUSTED_PROXY_HOPS=1` ka efekt real te login-i dhe rrugët `/internal` (log-et tregojnë IP-në e klientit pas `real_ip`). Regjistrimi publik (`/registration*`) mbetet i mbyllur: hapja e tij kërkon `/registration` me `client_max_body_size 4k` dhe `limit_req` (docs/M8_REGISTRATION.md), `CENTRAL_PUBLIC_REGISTRATION_PROXY_ACK`, mailer dhe CAPTCHA reale.

### Inventari i komandave që ekzekutohen BRENDA imazhit Central
Vetëm këto janë të dokumentuara si të ekzekutueshme në imazhin Central (`python -m <modul>`; testohen te `tests/test_central_packaging.py`: importohen, `--help` del me kod 0 dhe nuk varen nga `scripts/` apo `app/` të Enterprise).
<!-- central-image-commands:start -->
| Moduli | Qëllimi |
|---|---|
| `apps.central.tools.billing_run` | faturimi periodik (shërbimi `central-billing-run`; refuzon pa autoritet `central`) |
| `apps.central.tools.send_notifications` | outbox i njoftimeve (shërbimi `central-send-notifications`) |
| `apps.central.tools.sender_central_readiness` | gatishmëria e sender-ave (parazgjedhja e `central-tool`) |
| `apps.central.tools.billing_readiness` | gatishmëria e faturimit |
| `apps.central.tools.billing_final_readiness` | gatishmëria përfundimtare e faturimit |
| `apps.central.tools.registration_readiness` | gatishmëria e regjistrimit |
| `apps.central.tools.money_reconciliation` | rakordimi i parave |
| `apps.central.tools.retention` | retention i raporteve të përdorimit (plan/dry-run si parazgjedhje; `--apply` fshin) |
| `apps.central.tools.create_admin` | krijimi i administratorit/operatorit (fjalëkalimi në terminal) |
| `apps.central.tools.create_service_credential` | kredencial shërbimi (çelës publik) për Enterprise |
| `apps.central.tools.service_credential_admin` | çaktivizim/administrim i kredencialeve të shërbimit |
<!-- central-image-commands:end -->

**Jashtë imazhit Central:** `apps.central.tools.financial_readiness` është agregator ndër-sistem: thërret `scripts.financial_ops`, `scripts.queue_readiness`, `scripts.money_authority_readiness` dhe `scripts.pricing_authority_readiness` të Enterprise si nënprocese. Imazhi Central s'ka `scripts/` (kufi i qëllimshëm: Central s'ngarkon kodin Enterprise), prandaj **nuk është në inventar dhe nuk ekzekutohet në kontejner Central**. Ekzekutohet nga një checkout i repo-së me të dy mjediset (venv me `CENTRAL_DATABASE_URL` dhe `SMS_DATABASE_URL`, ose `--enterprise-cwd`), siç e kërkon `docs/M9_PRODUCTION_CHECKLIST.md` §8.
