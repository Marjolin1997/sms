# Queue/outbox: sjellja e sotme dhe garancitë e dërgimit (M2-a, karakterizim)

Ky dokument përshkruan **çfarë bën kodi sot** (i provuar nga `tests/test_queue_semantics.py` dhe
`tests/test_queue_concurrency_pg.py`), jo çfarë do të donim. Refactor-i M2 duhet ta ruajë. Termi
"exactly once" **nuk** përdoret: nuk provohet end-to-end për asnjë kanal.

## Modeli
Nuk ka tabelë queue të veçantë: **rreshti i domain-it është queue-ja** (`sms_messages`, `sms_emails`,
`sms_webhook_deliveries`, me `status` + `attempts` + `next_attempt_at` dhe indeks `(status, next_attempt_at)`).
Publish = krijimi i atij rreshti në transaksionin e biznesit. Thirrësi zotëron transaksionin.

| | SMS | Email | Webhook delivery |
|---|---|---|---|
| Publish | `messages.submit` (Message QUEUED + hold + MessageEvent, savepoint) | `emails.submit` | `events.emit` (Event + WebhookDelivery për çdo endpoint aktiv) |
| Reserve | `claim_next`: `FOR UPDATE SKIP LOCKED`, `QUEUED→SENDING`, `attempts+1`, MessageEvent | i njëjti (`EmailEvent`) | `deliver_next`: `FOR UPDATE OF WebhookDelivery SKIP LOCKED`, `attempts+1`, `next_attempt_at=now+120s` (lease); statusi mbetet PENDING; JOIN te endpoint ACTIVE |
| Kush e mban rezervimin | statusi SENDING (jo lock-u) | statusi SENDING | lease (`next_attempt_at` në të ardhmen) |
| Retry | vetëm gabim i përkohshëm dhe `attempts<5`: `SENDING→QUEUED`, vonesa `30·2^(attempts−1)` = 30/60/120/240 s | e njëjta | `RETRY_DELAYS=[30,120,600,1800,7200,21600,43200]`, 8 përpjekje; zëvendëson lease-in |
| Gjendje terminale | `FAILED` + `error_code`, hold i lëshuar | `FAILED` + `error_code` | `FAILED` + `last_error`; `redeliver` e kthen PENDING (attempts=0) |
| Nuk ka | tabelë dead-letter, batch reserve, heartbeat lease | të njëjtat | tabelë dead-letter, batch |
| Ordering | `(next_attempt_at, id)`; FIFO i përafërt, jo garanci strikte nën konkurrencë; retry shkon prapa | i njëjti | i njëjti |
| Kill switch | `switches.DISPATCH` (worker s'rezervon; queue e paprekur) | i njëjti | — |

## Kufijtë e transaksionit
```
publish:   [tx biznesi: ndryshimet + rreshti queue + historiku] COMMIT   (asnjë publish/enqueue nuk bën commit vetë)
SMS/email: claim(UPDATE→SENDING) COMMIT#1 → provider.send → tranzicion (SENT | QUEUED+backoff | FAILED) COMMIT#2
webhook:   reserve(lease, attempts+1) COMMIT → HTTP → tx e re (FOR UPDATE delivery+endpoint) rezultati COMMIT
```
Outbox-i i webhook-ëve është atomik me biznesin: `emit` nuk bën commit; rollback nuk lë delivery jetim
(testuar). Nuk ka `COMMIT biznesi → enqueue i ndarë`.

## Garancitë e dërgimit (aktuale)

### SMS: `claim → SENDING → COMMIT#1 → provider → COMMIT#2`
- **At-most-once nga ana e platformës për crash:** vdekja e worker-it midis COMMIT#1 dhe COMMIT#2 lë
  mesazhin `SENDING` **përgjithmonë**: nuk ka lease/auto-recovery (testuar edhe me `pg_terminate_backend`
  dhe pas 365 ditësh). Provider-i mund ta ketë dërguar ose jo. **Rikuperim manual:** `stuck_sending`
  (>10 min) shfaqet te `admin/queue`; s'ka veprim automatik.
- **Dritarja e dështimit:** (W1) crash para COMMIT#1: asgjë e qëndrueshme, mbetet QUEUED, `attempts` i
  pandryshuar (safe). (W2) crash pas COMMIT#1: SENDING i ngecur. (W4) provider pranoi, crash para
  COMMIT#2: SENDING pa `provider_message_id`; DLR-ja e mëvonshme nuk gjen rreshtin.
- **Përjashtim i papritur nga provider-i** (`Exception` jo `ProviderError`) trajtohet si i përkohshëm dhe
  **ri-radhitet**: nëse provider-i e kishte dërguar, mbrojtja e vetme është idempotency e provider-it me
  `reference = public_id`. Pra për rezultat të panjohur garancia është **të paktën një herë**, e
  mbështetur vetëm nga idempotency e provider-it (FakeProvider e respekton; adapteri Twilio nuk është
  provuar kundër Twilio real).
- **Idempotency-supported:** publish (unique `(owner, idempotency_key)`; e njëjta kërkesë → i njëjti
  rresht; kërkesë tjetër → Conflict). Është domain concern, jo garanci procesimi.

### Email: e njëjta analizë, me këto divergjenca
- Provider thirret brenda një **transaksioni leximi të hapur** (`verified_domain_for` + çelësi DKIM
  ekzekutohen pas COMMIT#1), pra lidhja është `idle in transaction` gjatë SMTP (SMS: `idle`). Rreshti i
  email-it **nuk** është i kyçur (provuar me `FOR UPDATE NOWAIT`). **RISK:** `idle_in_transaction_session_timeout`.
- Nuk ka raportim të ngecurish (`stuck_sending` ekziston vetëm për SMS).
- Nuk ka hold parash; `unsafe_header` dhe `domain_unverified` janë dështime të përhershme në fazën e provider-it.

### Webhook: `reserve(lease) → COMMIT → HTTP → finalize`
- **At-least-once.** Crash pas HTTP (ose para finalize) → lease skadon pas `LEASE_SECONDS=120` → ridërgohet
  me të njëjtin `X-SMS-Delivery-Id` (testuar). Dritarja e dublikimit ≥ lease. Timeout/network error:
  pranuesi mund ta ketë përpunuar, prapë retry. `redeliver` është dublikim me qëllim.
- **Idempotency-supported:** vetëm nga pranuesi, me `X-SMS-Delivery-Id`/`X-SMS-Event-Id`.
- Lease aktiv nuk merret nga worker tjetër; pas skadimit merret nga **saktësisht një** nga shumë workers
  (testuar në PostgreSQL). HTTP jashtë transaksionit (testuar).
- **Endpoint circuit-breaker:** 5 delivery `FAILED` radhazi, ose HTTP 410, ose URL i pasigurt → endpoint DISABLED.

## Çfarë zbuluam gjatë karakterizimit (të papritura)
1. Email: transaksion leximi i hapur gjatë provider call (më sipër).
2. Stampede me `FOR UPDATE` pa `SKIP LOCKED` **prapë jep exactly-once** (READ COMMITTED rivlerëson WHERE
   pas pritjes); prandaj provat vendimtare për SKIP LOCKED janë ato **jo-bllokuese** (`q.claim` i dytë kthen
   `None` shpejt ndërsa i pari mban lock-un). Mutimi i provuar: heqja e `skip_locked` e rrëzon testin.
3. SQLite/pysqlite nuk e nis transaksionin para `SAVEPOINT`: `rollback()` pas `submit()` **nuk** e zhbën
   rreshtin. Testet e rollback-ut janë PostgreSQL-only; prodhimi është PostgreSQL.
4. Retry në SMS/email shkruan `MessageEvent/EmailEvent` (`sending`, `queued`); te SMS eventi webhook del vetëm
   për SENT/DELIVERED/FAILED.
5. `attempts` rritet në reserve, jo në retry: crash pas COMMIT#1 e ka tashmë llogaritur.

## Divergjenca SMS / Email / Webhook (arsye pse dy kontrata, jo një)
SMS+email: mbajtja me status, pa lease, at-most-once për crash. Webhook: mbajtja me lease, at-least-once.
SMS lëshon para në `fail`/`cancel`; email jo. Email hap tx leximi para provider-it; SMS jo.
