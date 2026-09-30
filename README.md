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
| 11 Event log + webhooks për klientët (SSRF, retry, nënshkrim), çelësa API vetë-shërbyes, pasqyrë përdorimi (API; pa UI) | ✅ |
| 12 Billing: plane, abonime, fatura të pandryshueshme me TVSH, pagesë nga wallet, pagesa online (adapter + webhook) | ✅ |

## Siguria e prodhimit (Faza 13)
- **Kufizim provash të dështuara:** pas `SMS_AUTH_MAX_FAILURES` (20) autentikimeve të dështuara brenda `SMS_AUTH_FAIL_WINDOW_S` (600 s) nga e njëjta IP, kërkesat me kredenciale kthejnë `429 too_many_attempts` (`Retry-After`). Regjistri është në PostgreSQL (`sms_auth_failures`, pa sekrete), pra funksionon për shumë procese.
- **IP e klientit pas proxy-t:** vendosni `SMS_TRUSTED_PROXY_HOPS=N` (p.sh. 1 pas Nginx) që të besohet elementi N-nga-fundi i `X-Forwarded-For`. Me 0 (parazgjedhje) ky header injorohet, kështu nuk mund të falsifikohet.
- **Allowlist IP për çelësa:** `allowed_cidrs` në krijim (`POST /v1/admin/api-keys`, `/v1/portal/api-keys`); nga IP të tjera çelësi kthen `403 ip_not_allowed`. Vlerat validohen (IPv4/IPv6/CIDR, deri në 20).
- **Rrotullim çelësash:** `POST /v1/admin/api-keys/{id}/rotate` dhe `/v1/portal/api-keys/{id}/rotate` (`grace_minutes` 0–1440, parazgjedhje 60): çelës i ri me të njëjtin rol/llogari/IP; i vjetri skadon pas periudhës kalimtare (0 = revokim i menjëhershëm). Klienti mund të rrotullojë vetëm çelësat e vet.
- **GDPR, e drejta e qasjes:** `GET /v1/contacts/{id}/export` kthen profilin, listat, historikun e pëlqimit (me evidencë), SMS-et dhe emailet e atij personi; regjistrohet në audit. Konsola ka butonin “Eksporto të dhënat”.
- Kujdes: kufizimi është sipas IP; pas një sulmi nga një IP, edhe `X-Admin-Key` dhe çelësat e vlefshëm nga ajo IP bllokohen gjatë dritares.

## 2FA për stafin (Faza 14)
- **TOTP (RFC 6238)** për çelësat e stafit: `POST /v1/me/2fa/enroll` kthen sekretin dhe `otpauth://` URI; `POST /v1/me/2fa/confirm {"code"}` e aktivizon. Sekreti ruhet i enkriptuar (Fernet, `SMS_SECRETS_KEY`); një kod pranohet vetëm një herë (mbrojtje kundër riluajtjes, dritare ±30 s).
- **Çfarë mbrohet:** ndryshimet (jo leximet) me lejet `wallet:adjust`, `topup:confirm`, `keys:manage`, `switch:write`, `plans:write`, `rates:write`, `routes:write`, `billing:admin`. Kodi dërgohet në header-in `X-TOTP`; pa të: `403 totp_required`, i gabuar: `403 totp_invalid` (numërohet te kufizimi i provave, Faza 13).
- **`SMS_REQUIRE_STAFF_2FA=true`:** çelësat e stafit pa 2FA refuzohen për ato veprime (`totp_enrollment_required`) derisa ta aktivizojnë. Çelësi bootstrap (`X-Admin-Key`) dhe klientët përjashtohen; në prodhim hiqni `SMS_ADMIN_API_KEY` pasi krijoni çelësat personalë.
- **Humbi telefoni:** një superadmin (me kodin e vet) ose bootstrap: `POST /v1/admin/api-keys/{id}/reset-2fa`; regjistrohet në audit.
- **Konsola:** faqja “Siguria” (konfigurim me sekret/link + kod konfirmimi) dhe dialog që kërkon kodin automatikisht kur një veprim e kërkon.

## Paneli (frontend)
React + Vite në `frontend/` (shih `frontend/README.md`); pamje në `docs/screenshots/`. Të dhëna demo: `python -m scripts.seed_demo`.

**Klienti:** Overview me listë hapash nisjeje; Send (SMS me çmim live, numërues karakteresh/pjesësh, zgjedhje sender-i të miratuar, template me variabla, paralajmërim për balancë të pamjaftueshme; email); Historik mesazhesh (kërkim, filtra, kronologji); Campaigns (draft → konfirmim → pauzë/anulim, vlerësim kostoje); Kontakte (kërkim, import CSV me parapamje, lista, consent me provë, fshirje GDPR); Sender ID dhe template (kërkesë, statuse, versione); Wallet (balancë, rezervime, ledger, top-up online); Billing; Webhooks dhe event log; Domene email (udhëzime DNS me kopjim); Çelësa API.

**Stafi:** Miratime (radha e sender ID-ve/template-ve, refuzim me arsye), Llogari (lista, aktivizim/ndalim dërgimi, tarifa, wallet, plan, TVSH, çelës për klientin, "hape si këtë llogari"), Tarifa dhe routes (versione, publikim me datë, "provo një çmim"), Finance (top-up në pritje, regjistrim, korrigjim me paralajmërim), Admin (kill switches me arsye, audit me filtër).

**Gjuha:** ndërfaqja është **shqip** si parazgjedhje, me ndërrues Shqip/English (te hyrja dhe te menyja anash; zgjedhja ruhet në shfletues). Teksti anglisht në kod është çelësi; fjalori shqip është te `frontend/src/locales/sq/`. `npm run i18n:check` (ekzekutohet edhe para `npm run build`) dështon nëse ka tekst pa përkthim, çelës të papërdorur, `{variabla}` që s'përputhen ose tekst të shkruar direkt në JSX. Për tekstet që prodhon serveri (faturë e printueshme, faqja publike e çregjistrimit, fundi i emailit) përdoret `SMS_DEFAULT_LANGUAGE` (`sq` parazgjedhje, ose `en`).

**Përdorshmëria:** gabimet e API-së përkthehen në gjuhë të thjeshtë me hapin tjetër; njoftime (toast) dhe dialogë konfirmimi të aksesueshëm (Esc, fokus) në vend të `alert/confirm`; gjendje bosh me udhëzim, skeleton gjatë ngarkimit, "Load more"; navigim me grupe dhe menu celulari, tabela që kthehen në karta në ekran të vogël; kontrolle me tastierë dhe `aria-*`. Test end-to-end me shfletues të vërtetë: `e2e/test_console.py`.

## Nisje e shpejtë me panel (Docker + Node)
```bash
git clone https://github.com/Marjolin1997/sms.git && cd sms
git checkout claude/sms-platform-architecture-lugdyz
docker compose up --build -d                            # PostgreSQL + API + workers
docker compose exec api python -m scripts.seed_demo      # një herë; printon CLIENT_KEY dhe ADMIN_KEY
cd frontend && npm install && npm run dev                # hap http://localhost:5173 dhe ngjit një çelës
```
Rivendosje e plotë: `docker compose down -v`. Seed-i është vetëm për zhvillim.

## Nisja
```bash
cp .env.example .env
docker compose up --build        # API në :8000, PostgreSQL në :5433
```
Lokalisht: `pip install -r requirements-dev.txt && ruff check . && pytest`.
Teste të plota mbi PostgreSQL (konkurrencë, triggers, migrime):
`SMS_TEST_DATABASE_URL=postgresql+psycopg://sms:sms@localhost:5432/sms_test pytest`

## Webhooks dhe event log
- Eventet (`message.sent|delivered|failed`, `email.sent|delivered|bounced|complained|failed`, `campaign.running|paused|completed|cancelled`, `consent.opted_in|opted_out`) shkruhen në të njëjtin transaksion me ndryshimin. Të dhënat përmbajnë vetëm id dhe statuse (pa numër/tekst/email); përjashtim `consent.*` që mban adresën për sinkronizim CRM. Ruhen `SMS_EVENT_RETENTION_DAYS` (30) dhe pastrohen.
- Alternativë pull: `GET /v1/events?after_id=` (kursor). Push: `POST /v1/webhooks/endpoints` (vetëm `https`, IP publike; SSRF kontrollohet në krijim dhe para çdo dërgimi; pa redirect). Sekreti `whsec_…` shfaqet një herë, ruhet i enkriptuar.
- Dërgimi: të paktën një herë, pa garanci renditjeje (përdor `id`/`created_at`). Retry me backoff (30s, 2m, 10m, 30m, 2h, 6h, 12h; 8 përpjekje), 410 ose 5 delivery të shterura radhazi çaktivizojnë endpoint-in. `redeliver`, `test` (ping) dhe `rotate-secret` në API.
- Verifikimi te marrësi: header `X-SMS-Signature: t=<unix>,v1=<hex>` ku `v1 = HMAC_SHA256(secret, f"{t}.{trupi_i_papërpunuar}")`; refuzo nëse `|now - t| > 300s`.
```python
import hmac, hashlib, time


def verify(secret, header, body: bytes, tol=300):
    p = dict(x.split("=", 1) for x in header.split(","))
    ok = hmac.compare_digest(
        p["v1"], hmac.new(secret.encode(), f"{p['t']}.".encode() + body, hashlib.sha256).hexdigest()
    )
    return ok and abs(time.time() - int(p["t"])) <= tol
```
- Worker i ndarë: `python -m app.worker --role webhooks` (një endpoint i ngadaltë nuk bllokon SMS/email). Për mbrojtje të plotë nga DNS-rebinding, kufizo egress-in e këtij worker-i.
- Portal (API): `/v1/portal/api-keys` (çelësa vetë-shërbyes, role `client` gjithmonë, max 20), `/v1/portal/overview` (balanca, SMS/email 30 ditë, campaigns, webhooks).

## Faturimi
- **Plane** (të pandryshueshme; ndryshimi = plan i ri): tarifë mujore + email të përfshira + çmim për email mbi kuotë. SMS mbetet parapagim nga wallet (top-up); faturat janë për abonimin dhe tepricat e email-it.
- **Abonim**: periudha kalendarike nga data e nisjes (pa zhvendosje: 31 jan → 28 shk → 31 mar). Fatura lëshohet në **fund** të periudhës nga worker-i (çdo 10 min); plani i ri hyn në fuqi nga periudha tjetër, anulimi në fund të periudhës, pa proporcion.
- **Fatura**: numër pa boshllëqe (`INV-2026-000001`, kyçje e rreshtit të numëruesit), TVSH nga profili (vendoset vetëm nga stafi), rrumbullakim HALF_UP në cent, fotografi e të dhënave të klientit. E pandryshueshme: ORM + trigger PostgreSQL (fushat financiare, statusi final, pa fshirje). Unike për (abonim, periudhë).
- **Pagesa nga wallet**: hyrje ledger `invoice` idempotente; nëse s'ka para, fatura mbetet e hapur.
- **Pagesa online**: `POST /v1/billing/payments` (shuma e faturës vendoset nga serveri); rezultati vjen me `POST /webhooks/payments/{provider}` (HMAC). Shuma/monedha verifikohen kundrejt regjistrimit; mospërputhje → `failed/amount_mismatch` pa kredit, për rakordim. Nëse fatura u pagua ose u anulua ndërkohë, paraja kreditohet në wallet (nuk humbet).
- **Gateway**: adapter `PaymentGateway` (`SMS_PAYMENT_PROVIDER`); këtu vetëm një gateway fals. Vendori real (Stripe, Paddle, bankë lokale) zbatohet në `app/providers/payments.py`.
- Faturë e printueshme: `GET /v1/billing/invoices/{id}/html` (me `Authorization`; paneli e hap si blob).

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
