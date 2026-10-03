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
   Aplikacioni **refuzon të nisë** me konfigurim të pasigurt (`SMS_ENV=production`); `docker compose logs api` liston çdo problem.
3. Kontrolli: `curl -s http://127.0.0.1:8080/readyz` → `{"status":"ready"}` (kthen 503 nëse DB s'përgjigjet ose skema s'është në versionin e kodit).
4. **Stafi i parë** (me çelësin bootstrap; shih README → “Nisje”): krijoni një çelës `superadmin` personal për çdo person, hyni në konsolë me të, aktivizoni 2FA te *Siguria*. Pastaj **hiqni `SMS_ADMIN_API_KEY`** nga `.env` dhe rinisni `api` (bootstrap-i është vetëm për nisjen). Vendosni `SMS_REQUIRE_STAFF_2FA=true`.

## 2. TLS dhe IP e klientit
- `web` shkruan `X-Forwarded-For` me IP-në e lidhjes; `SMS_TRUSTED_PROXY_HOPS=1` e bën aplikacionin ta besojë. Nëse ka një load balancer para nginx-it, sigurohuni që nginx të marrë IP-në reale (`set_real_ip_from`) — përndryshe kufizimi i provave dhe allowlist-at IP do të shohin IP-në e load balancer-it.
- Vendosni HSTS vetëm pasi TLS punon (nginx e dërgon tashmë; shfletuesit e mbajnë mend).

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
- Pa gateway të vërtetë pagese (`SMS_PAYMENT_PROVIDER=disabled`; mbushjet bëhen manualisht nga Financa) dhe pa provider SMS të vërtetë të lidhur (HTTP/SMPP sipas dokumentacionit të tij).
- Rrotullimi i `SMS_SECRETS_KEY` dhe `SMS_PII_HMAC_KEY` nuk mbështetet (kërkon rienkriptim/rihash). Ruajini me kujdes.
- Auditim i jashtëm sigurie dhe pentest para se të shiten klientëve.
