# SMS Platform

Platformë SMS ku **saktësia e parave dhe e statuseve** ka përparësi mbi numrin e veçorive.

## Parimet
- Shumat: `Decimal`/`NUMERIC(20,6)`, kurrë float (float refuzohet nga shtresa e shërbimit).
- Ledger **append-only**: ORM guard + triggers MySQL/MariaDB (`UPDATE`/`DELETE` refuzohen). Bilanci = `balance_after` i rreshtit të fundit; `verify_wallet` e kontrollon me `SUM(delta)`.
- Çdo lëvizje parash është idempotente (`wallet_id + idempotency_key` unik); retry nuk faturon dy herë.
- Lëvizjet serializohen me `SELECT ... FOR UPDATE` mbi rreshtin e wallet-it.
- **PostgreSQL 16** (izolim `READ COMMITTED`, `SELECT ... FOR UPDATE` mbi wallet, `SKIP LOCKED` për radhën, afate për lock/statement/idle-in-transaction).
- Append-only edhe në nivel databaze: triggers PL/pgSQL bllokojnë `UPDATE/DELETE/TRUNCATE` mbi ledger, audit log, historikun e statuseve dhe DLR receipts.
- Tabelat kanë prefiks `sms_`; Alembic ignoron çdo tabelë tjetër dhe përdor `sms_alembic_version`. Migrimet e para vetëm në një databazë kopje, kurrë direkt në prodhim.

## Fazat
| Faza | Status |
|---|---|
| 0 Integrimi me DB/përdoruesit e omnichannel | pezull (platforma tani ka DB PostgreSQL të vetën) |
| 1 Skeleti (FastAPI, Docker, Alembic, teste, CI) | ✅ |
| 2 Wallet + ledger + top-up (hold/capture/release/refund) | ✅ |
| 3 Rate cards me versione, prefix/operator, segmente, quote | ✅ |
| 4 Sender IDs + templates me miratim, versione, validim | ✅ |
| 5 Pipeline dërgimi (outbox, retries, DLR, provider fals) | ✅ |
| 6 Adapter HTTP + webhook DLR i nënshkruar + sweeper (SMPP: pret vendorin) | ✅ |
| 7 RBAC + API keys, audit log, kill switch, rate limit, monitorim | ✅ |
| 8 Contacts, lista, consent me prova, opt-out (STOP/START), fshirje GDPR, audienca | ✅ |
| 9 Campaigns SMS: planifikim, personalizim, ritëm, buxhet, dritare orare, pauzë/anulim, statistika | ✅ |
| 10 Email: domene SPF/DKIM, dërgim i nënshkruar, bounces/complaints, unsubscribe me një klik, campaigns email | ✅ |
| 11 Webhooks për klientët, portal vetë-shërbyes | – |
| 12 Billing: plane, fatura, pagesa | – |

## Nisja
```bash
cp .env.example .env
docker compose up --build        # API në :8000, PostgreSQL në :5433
```
Lokalisht: `pip install -r requirements-dev.txt && ruff check . && pytest`.
Teste të plota mbi PostgreSQL (konkurrencë, triggers, migrime):
`SMS_TEST_DATABASE_URL=postgresql+psycopg://sms:sms@localhost:5432/sms_test pytest`

## Email
- Klienti shton një domen (`POST /v1/email/domains`), publikon rekordet DNS të kthyera (DKIM, SPF `include:`, DMARC opsional) dhe thërret `verify`. Dërgimi lejohet **vetëm nga domene të verifikuara të vetë klientit**; një domen s'mund të verifikohet nga dy klientë; nëse DNS hiqet, verifikimi anulohet.
- Çelësi privat DKIM ruhet i enkriptuar (Fernet, `SMS_SECRETS_KEY`); çdo email nënshkruhet DKIM (RSA-2048). Marketing merr `List-Unsubscribe` + `List-Unsubscribe-Post` (RFC 8058) dhe footer; lidhja `/u/<token>` nuk çregjistron me GET (skanerët), vetëm me POST, dhe nuk përmban adresë.
- Bounce i ashpër dhe complaint bllokojnë adresën (consent hard); soft bounce regjistrohet vetëm. Webhook: `POST /webhooks/email/{provider}` (HMAC si DLR).
- Provider: adapter SMTP (STARTTLS) që mbulon SES/Mailgun/SendGrid-SMTP/MTA jotja (`SMS_SMTP_*`, `SMS_EMAIL_PROVIDER=smtp`); provider fals për teste.
- Email nuk faturohet për mesazh (plane/fatura vijnë në Fazën 12); ka rate limit për llogari.
- Campaigns mbështesin `channel: "sms" | "email"`; personalizim `{{first_name}}` në subject/text/html (html me escape).

## Campaigns
- Rrjedha: `draft → scheduled → preparing → running → completed` (+ `paused`, `cancelled`). Worker-i e përgatit audiencën (snapshot) në pjesë të 500 dhe pastaj dërgon deri në 100 për cikël.
- Çdo marrës dërgohet me idempotency key `camp:<id>:<contact>`; consent-i rikontrollohet në çastin e dërgimit; kufij: `rate_per_minute`, `max_cost` (pauzë `budget_exhausted`), dritare orare me offset, kill switch global.
- Mbarimi i parave e pauzon campaign-in (`insufficient_funds`), s'dështon marrës pas marrësi; pas top-up `resume` vazhdon nga aty ku mbeti.
- `GET /v1/campaigns/{id}/estimate` jep koston e saktë para nisjes; `GET /v1/campaigns/{id}` jep statistika (statuse, arsye përjashtimi, delivery rate, kosto e dorëzuar / në rrugë / e rimbursuar).

## Consent dhe privatësi
- Marketing kërkon **opt-in me provë** (`evidence`); transactional (OTP) nuk kërkon, por bllokohet nga opt-out i ashpër.
- Opt-out i ashpër (`STOP`, bounce, complaint, erasure) bllokon çdo kategori dhe nuk zhbëhet me opt-in, përveç `STOP` të vetë personit (`START`). `unsubscribe` bllokon vetëm marketing.
- Adresat në tabelat e consent-it ruhen vetëm si HMAC-SHA256 (`SMS_PII_HMAC_KEY`, mos e ndrysho pasi ka të dhëna). Fshirja GDPR heq PII nga contact-i dhe e lë adresën të bllokuar si hash.
- `sms_consent_events` është vetëm-shtim (ORM + trigger).

## Auth dhe RBAC
- `Authorization: Bearer sms_<prefix>_<secret>`. Ruhet vetëm SHA-256 i sekretit; çelësi i plotë shfaqet një herë.
- Role: `superadmin`, `finance`, `pricing`, `approver`, `support`, `client` (i lidhur me një `owner_ref`; sheh vetëm të dhënat e veta).
- Bootstrap: `X-Admin-Key` = `SMS_ADMIN_API_KEY` (superadmin). Përdore vetëm për të krijuar çelësat e parë (`POST /v1/admin/api-keys`), pastaj **hiqe variablën në prodhim**.
- Çdo ndryshim administrativ shkruhet te `sms_audit_log` (vetëm-shtim) në të njëjtin transaksion.
- Kill switch: `PUT /v1/admin/switches/{submit|dispatch}` (arsye e detyrueshme kur çaktivizohet).
