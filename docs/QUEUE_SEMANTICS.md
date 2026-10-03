# Queue/outbox: sjellja e sotme dhe garancitë e dërgimit (M2-a, karakterizim)

> **Shënim (M2-e):** pamja përfundimtare dhe matricat janë te `QUEUE_ARCHITECTURE.md`. Seksionet para "M2-b" janë
> karakterizimi historik i kodit para refactor-it (emra si `claim_next`/`_after_error` i përkasin asaj kohe); seksionet
> M2-b/c/d dhe "Email provider transaction window" përshkruajnë gjendjen aktuale.

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
- (Para patch-it të transaksionit) provider thirrej brenda një transaksioni leximi të hapur; **tani** leximet e
  domenit/DKIM bëhen para COMMIT#1 dhe provider-i thirret pa transaksion, si SMS (shih seksionin më poshtë).
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
1. Email: transaksion leximi i hapur gjatë provider call (u rregullua me patch-in e transaksionit të email).
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
- **Transaksioni i `process_one` në M2-c nuk ndryshoi** (claim → COMMIT#1 → leximet → SMTP → COMMIT#2); ndryshoi më pas me patch-in e veçantë më poshtë.
- Provë ekuivalence (PostgreSQL, 93 statement, teksti i normalizuar identik para/pas): submit 10, process_one 9,
  retry 9, fail i përhershëm 9, cancel 6 SQL.

## Email provider transaction window: para / pas patch-it
**Shkaku (final).** `process_one` bënte COMMIT#1 pas claim-it dhe **pastaj** thërriste `verified_domain_for` dhe
`decrypt_private_key`. SQLAlchemy bën autobegin në SELECT-in e parë; transaksioni i ri qëndronte i hapur gjatë
MIME/DKIM/SMTP deri te COMMIT#2 (`idle in transaction`, lidhje e zënë, snapshot i hapur, rrezik nga
`idle_in_transaction_session_timeout`=60 s).

**Para:**
```
claim → COMMIT#1 → SELECT domen + decrypt DKIM (autobegin) → MIME/DKIM → SMTP [tx i hapur] → ack/retry/fail → COMMIT#2
```
**Pas:**
```
claim + SELECT domen + decrypt DKIM → materializim te EmailSendPayload (primitive) → COMMIT#1 (lidhja lirohet)
→ MIME/DKIM → SMTP [asnjë tx, asnjë lidhje, asnjë SQL] → ack/retry/fail → COMMIT#2
```
- `EmailSendPayload` (dataclass i ngrirë me `str`/`bytes`): provider-i, MIME dhe DKIM s'prekin ORM/Session.
  Pra `expire_on_commit=True` nuk mund të shkaktojë lazy-load/SELECT gjatë provider-it (testuar me sesion të tillë).
- Përjashtimet e domain-it në lexim (domen i hequr, çelës i palexueshëm) mbahen dhe riklasifikohen **pas** COMMIT#1
  me të njëjtat rregulla si para (`domain_unverified` përhershëm; dështim decrypt → `provider_exception` i përkohshëm).
  Gabimet e DB-së (`SQLAlchemyError`) në lexim propagohen dhe claim-i zhbëhet (provider-i s'thirret): më e sigurt se para.
- Nëse COMMIT#1 dështon, provider-i **nuk** thirret dhe claim-i s'është i qëndrueshëm (QUEUED, attempts 0) (testuar).
- Delivery semantics të pandryshuara: QUEUED→SENDING, `attempts` (në reserve), retry 30/60/120/240, max 5, i përhershëm
  → FAILED, Message-ID, referenca e provider-it, `DispatchQueue`. SQL: i njëjti numër dhe tekst (9 për process_one).

**Provë (PostgreSQL, 16 workers njëkohësisht brenda provider-it që fle 0.3 s):** pool checked-out 16 → **0**;
sesione `idle in transaction` 16 → **0**. Pa SQL midis COMMIT#1 dhe kthimit të provider-it (testuar me ngjarje engine).

**Dritaret e mbetura të dështimit (patch-i NUK i zgjidh dhe nuk pretendon të ndryshme; s'ka exactly-once):**
- **A)** crash pas COMMIT#1 dhe para provider call → `SENDING`, pa auto-recovery, rikuperim manual.
- **B)** provider dërgon me sukses, por procesi vdes ose COMMIT#2 dështon → `SENDING` pa `provider_message_id` dhe pa
  ngjarje `sent`; **dërgimi mund të ketë ndodhur**. Testuar (`..._REMAINING_RISK`): rreshti mbetet SENDING, attempts=1 dhe
  `process_one` nuk e merr më kurrë vetvetiu. Provider-i deduplikon vetëm me `Message-ID` (s'është provuar me provider real).
- Email nuk ka raportim të ngecurish (`stuck_sending` vetëm për SMS). Lease/auto-recovery janë çështje e veçantë.

## M2-d: `DeliveryQueue` + `PostgresDeliveryQueue` (webhook deliveries; kontratë e veçantë nga `DispatchQueue`)
Webhook mbetet **at-least-once me lease**; `DispatchQueue` (status SENDING, at-most-once për crash) nuk ripërdoret.

**Kontrata** (`app/queue/delivery.py`; implementimi `PostgresDeliveryQueue` te `app/queue/postgres.py`):
```python
DeliverySpec(model, base: () -> Select, eligible: (now) -> predicate, attempts, next_attempt_at, id,
             lease_s=120, retry_delays_s=(30,120,600,1800,7200,21600,43200))   # max_attempts = len+1 = 8
DeliveryHooks:  completed(db, item, now) · failed(db, item, outcome) · replayed(db, item)
DeliveryQueue:  publish(db, items)                      # vetëm db.add: pa flush, pa commit
                reserve(db, now) -> item | None         # eligible+due, ORDER BY next_attempt_at,id, LIMIT 1,
                                                        # FOR UPDATE OF <model> SKIP LOCKED; attempts+1; next=now+lease_s
                lock(db, id, *, where=None) -> item     # FOR UPDATE (pret): db.get, ose SELECT bazë + predikat domain
                complete(db, item, now)                 # hooks.completed
                retry(db, item, *, now, permanent)      # permanent→FAILED; attempts>=max→EXHAUSTED; përndryshe next=now+delays[attempts-1]
                fail(db, item, outcome)                 # hooks.failed
                replay(db, item, *, now)                # attempts=0, next=now, hooks.replayed, flush
```
`DeliveryOutcome`: `RETRIED`, `FAILED` (i përhershëm), `EXHAUSTED`. Adapteri **nuk** bën commit/rollback, nuk importon
HTTP/domain (provuar me AST + kërkim fjalësh) dhe nuk njeh payload, nënshkrim, endpoint ose ngjarje.

**Domain (te `webhooks.py` / `webhook_queue.py`):** ndërtimi i kërkesës HTTP, nënshkrimi, `X-SMS-*`, interpretimi i
rezultatit (2xx / non-2xx / 410 / `UnsafeUrl` / `HTTPError`), `last_status_code`/`last_error`. Hooks:
`completed` → `SUCCEEDED`, `delivered_at`, `consecutive_failures=0`; `failed` → `FAILED`, `consecutive_failures+1`,
dhe disable (`gone` për 410, `unsafe_url`, `too_many_failures` pas `DISABLE_AFTER=5`); `replayed` → `PENDING`,
`last_error=None`. `webhook_queue.py` ekziston që `events` ↔ `webhooks` të mos formojnë cikël importesh.

**Çfarë ka dalë nga service:** SELECT-i `JOIN endpoint … FOR UPDATE OF delivery SKIP LOCKED` (te `deliver_next`), `attempts+=1`,
vendosja e lease-it, zgjedhja e vonesës së retry (`RETRY_DELAYS[attempts-1]`), kontrolli i `MAX_ATTEMPTS`, SELECT-i i
`redeliver` (me JOIN dhe `owned(...)`), rivendosja e `attempts/next_attempt_at`, dhe `db.add(WebhookDelivery)` te `emit`.

**Outbox transaksional (i pandryshuar):** `events.emit` bën `Event` + flush + `queue.publish(deliveries)` brenda transaksionit
të thirrësit; `publish` vetëm `db.add`. Rollback → asnjë Event, asnjë delivery (testuar). Endpoint jo-aktiv nuk merr delivery.

**Flow i `deliver_next` (i pandryshuar):**
```
reserve (lease+attempts, SKIP LOCKED) + lexim endpoint/event → COMMIT → [HTTP: pa tx, pa lock, pa SQL, pa lidhje pool]
→ lock(delivery) + lock(endpoint) → complete | retry(permanent) → COMMIT
```
Testuar: pa SQL midis COMMIT dhe kthimit të HTTP, `db.in_transaction()` False, `pool.checkedout()` 0, `pg_stat_activity` pa
`idle in transaction`, dhe `FOR UPDATE NOWAIT` mbi rreshtat e delivery/endpoint kalon gjatë HTTP (asnjë lock i mbajtur).

**Semantika që ruhet:** lease 120 s; `attempts` rritet vetëm në reserve; endpoint ACTIVE i detyrueshëm; retry 30/120/600/1800/7200/21600/43200
(listë eksplicite, 8 përpjekje); i njëjti `X-SMS-Delivery-Id` në retry dhe replay (replay rivendos të njëjtin rresht,
nuk krijon të ri); replay refuzon PENDING dhe tenant tjetër; replay **nuk** riaktivizon endpoint-in e çaktivizuar.

**Dritaret e dështimit (at-least-once; pa exactly-once):**
- **A)** lease i commit-uar → procesi vdes para HTTP → lease skadon pas 120 s → delivery riprovohet (pa dërgim të humbur).
- **B)** pranuesi e merr webhook-un → procesi vdes para finalize (ose COMMIT final dështon) → lease skadon → **dublikim i mundshëm**
  me të njëjtin `X-SMS-Delivery-Id`; pranuesi është përgjegjës për dedup. `attempts` shtohet në çdo reserve, pra një crash
  shpenzon një përpjekje.
- Timeout/network error: pranuesi mund ta ketë përpunuar; trajtohet si i përkohshëm dhe riprovohet.
