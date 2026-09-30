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

## M2-b: `DispatchQueue` + `PostgresDispatchQueue` (vetëm SMS)
Paketa `app/queue/` (nuk importon asgjë nga `app.models/services/providers/api`; provohet nga test AST).
- `DispatchSpec(model, pending, attempts, next_attempt_at, id, backoff_s=30, max_attempts=5)`: e ofron service.
- `DispatchHooks(reserved, requeued, sent, failed)`: e ofron service (`messages._SmsHooks`); brenda tyre
  `_move` mbetet burimi i vetëm i state machine-it, `_fail` lëshon hold-in e wallet-it.
- `DispatchQueue`: `publish`, `reserve`, `acknowledge`, `retry -> Outcome{RETRIED,FAILED,EXHAUSTED}`, `fail`,
  `cancel_if_pending`. **Asnjë commit/rollback** (transaksioni është i thirrësit). Retry: i përhershëm →
  `hooks.failed`; i përkohshëm dhe `attempts<max` → `next_attempt_at=now+backoff_s·2^(attempts−1)` + `hooks.requeued`;
  përndryshe `hooks.failed` (EXHAUSTED). `attempts` rritet vetëm në `reserve`.
- `messages.claim_next` / `cancel_if_queued` mbeten funksione publike (delegojnë); SQL-i i `FOR UPDATE SKIP LOCKED`
  ka dalë nga `messages.py`. Email dhe webhook nuk janë migruar (M2-c, M2-d).
- **Provë ekuivalence:** 100 SQL statement (submit + process_one + retry) në PostgreSQL, teksti i normalizuar,
  identik para/pas (diff bosh); numri: submit 21, process_one 8, retry 8.

## Risku i njohur, i pandryshuar: email thërret SMTP brenda transaksioni leximi të hapur
**Shkaku:** `emails.process_one` bën COMMIT#1 (claim), por para `provider.send` thërret
`email_domains.verified_domain_for(db, …)` dhe `decrypt_private_key(domain)`, që ekzekutojnë SELECT në të
njëjtin `Session`. SQLAlchemy autobegin hap transaksion të ri, që mbetet i hapur gjatë ndërtimit të MIME/DKIM
dhe gjatë SMTP, deri te COMMIT#2. SMS s'ka thirrje DB midis COMMIT#1 dhe provider-it, prandaj është `idle`.
**Ndikimi:**
1. `idle_in_transaction_session_timeout` (`db_idle_tx_timeout_ms`=60 s në `make_engine`): SMTP me
   STARTTLS+AUTH+DATA, me `timeout=15s` **për operacion**, mund ta kalojë 60 s; PostgreSQL e vret sesionin,
   COMMIT#2 dështon (OperationalError) dhe email-i mbetet `SENDING` ndërsa mund të jetë dërguar. Kjo
   është dritare e re për SENDING të ngecur (rikuperim manual; s'ka raportim për email).
2. Një lidhje e pool-it (`db_pool_size`=10) mbahet e zënë gjatë gjithë SMTP-së (SMS e lëshon).
   Me shumë workers/provider të ngadaltë kjo ul kapacitetin e pool-it dhe mban snapshot të hapur (vacuum).
3. Nuk mban lock rreshti (provuar me `FOR UPDATE NOWAIT`).
**Rekomandim (patch i veçantë, jo në M2):** lexo domenin/çelësin DKIM **para** COMMIT#1, ose bëj
`db.commit()` para `provider.send`, me test që `pg_stat_activity.state='idle'` gjatë SMTP dhe benchmark të vetin.
Testi `test_worker_connection_state_during_the_provider_call` e dokumenton sjelljen e sotme (email=1
`idle in transaction`, SMS=0) dhe do të kthehet në `0` kur të rregullohet.
