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

## M2-c: email përmes të njëjtit `PostgresDispatchQueue`
- `emails.queue = PostgresDispatchQueue(DispatchSpec(Email, pending=status==QUEUED, attempts, next_attempt_at,
  id, backoff_s=30, max_attempts=5), _EmailHooks())`: i njëjti adapter dhe e njëjta klasë si SMS (provuar nga test).
- `_EmailHooks`: `reserved` → `_move(SENDING)`; `requeued` → `error_code` + `_move(QUEUED, "retry:<code>")`;
  `sent` → `provider_message_id` + `_move(SENT)`; `failed` → `_fail` (`_move(FAILED)` + `error_code`). `_move` mbetet
  burimi i vetëm. Nuk ka efekt parash (divergjencë e vërtetë nga SMS: hook-u `failed` s'ka wallet).
- Hequr nga `emails.py`: `SELECT … FOR UPDATE SKIP LOCKED` te `claim_next` dhe `cancel_if_queued`, `attempts += 1`,
  llogaritja e backoff, kontrolli i `MAX_ATTEMPTS` (`_after_error`). `claim_next`/`cancel_if_queued` mbeten funksione publike.
- **Transaksioni i `process_one` NUK ndryshoi:** claim → COMMIT#1 → `verified_domain_for`/DKIM/MIME/SMTP → ack|retry|fail → COMMIT#2.
- Provë ekuivalence (PostgreSQL, 93 statement, teksti i normalizuar identik para/pas): submit 10, process_one 9,
  retry 9, fail i përhershëm 9, cancel 6 SQL.

## Email provider transaction risk (i pandryshuar; patch i veçantë i rekomanduar)
**Pse bëhet `idle in transaction`.** `emails.process_one` bën COMMIT#1 pas claim-it. Menjëherë pas tij,
`email_domains.verified_domain_for(db, …)` dhe `decrypt_private_key(domain)` ekzekutojnë SELECT në të njëjtin
`Session`; SQLAlchemy bën autobegin dhe transaksioni i ri qëndron i hapur gjatë ndërtimit të MIME, nënshkrimit DKIM
dhe thirrjes SMTP, deri te COMMIT#2. SMS s'ka thirrje DB midis COMMIT#1 dhe provider-it, prandaj lidhja është `idle`.
**Sa gjatë mbahet lidhja.** Matur me provider `fake` (DB + MIME + DKIM): mesatarisht 15 ms (max 24 ms). Me SMTP real
shtohet koha e rrjetit; kodi e kufizon vetëm për operacion (`timeout=15s` për connect, STARTTLS, AUTH, DATA), pra
në rast të ngadaltë kufiri teorik është shumë më i madh se 60 s.
**`idle_in_transaction_session_timeout`.** `make_engine` e vendos `db_idle_tx_timeout_ms=60000`. Nëse SMTP e kalon
60 s, PostgreSQL e mbyll sesionin; COMMIT#2 dështon me `OperationalError`.
**Dritarja e dështimit.** Email-i është dërguar nga provider-i, por COMMIT#2 dështon (timeout, lidhje e mbyllur,
crash): statusi mbetet `SENDING` (COMMIT#1 e ka bërë të qëndrueshëm), pa `provider_message_id`, pa ngjarje `sent`.
Nuk ka lease/auto-recovery dhe për email nuk ka as raportim të ngecurish (`stuck_sending` ekziston vetëm për SMS).
Rikuperimi është manual dhe provider-i deduplikon vetëm me `Message-ID` (nuk është provuar kundër provider-it real).
**Ndikimi në pool.** Gjatë gjithë SMTP-së një lidhje nga pool-i (`db_pool_size`=10) mbahet e zënë (SMS e lëshon pas
COMMIT#1) dhe mban snapshot të hapur (vacuum). Me N workers email dhe provider të ngadaltë, kapaciteti i pool-it
ulet; nuk mban lock rreshti (provuar me `FOR UPDATE NOWAIT`).
**Patch minimal i rekomanduar (NUK implementohet këtu).** Lexo domenin dhe çelësin DKIM **para** COMMIT#1 (brenda
të njëjtit transaksion të claim-it), ose, minimalisht, `db.commit()` pas leximit dhe para `provider.send`, që
SMTP të ekzekutohet pa transaksion të hapur. Pas rregullimit, testi i karakterizimit
`test_worker_connection_state_during_the_provider_call` duhet të kthehet nga `idle in transaction`=1 në 0 dhe
`test_claim_is_committed_before_the_provider_is_called` nga `in_tx is True` në `False` për email.
Benchmark i veçantë dhe test që një dështim i COMMIT#2 s'e lë email-in të ngecur pa raportim.
