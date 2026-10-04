# Arkitektura e queue pas M2 (rishikim përfundimtar, M2-e)

> **STATUSI: M2 CLOSED / APPROVED (pronari).** Ky dokument është arkitektura e referencës për M2.
> **Invariante të ruajtura:** SMS → `DispatchQueue` → `PostgresDispatchQueue` → hooks SMS; Email → `DispatchQueue` →
> `PostgresDispatchQueue` → hooks email; Webhook → `DeliveryQueue` → `PostgresDeliveryQueue` → hooks webhook;
> campaigns / sweeps / DLR / retention **jashtë** abstraksionit me qëllim.
> **Vendime të miratuara në mbyllje:** (1) devijimi SKIP LOCKED pranohet: `messages.expire_stale`, `campaigns.run_due`,
> `payments.expire_pending` mbeten jashtë queue; allowlist testi (`tests/test_queue_boundaries.py`) mbetet dhe çdo përdorim i ri
> kërkon review; të tre nuk zhvendosen në abstraksion për të plotësuar një kriter formal. (2) **Gate i fortë i besueshmërisë:**
> **M9-a (zbatuar): S1/E1 u mbyll me UNKNOWN + sweeper sipas fazës — shih `docs/M9_MONEY_AUDIT.md`.** S1/E1 (SMS/email `SENDING` i ngecur) dhe recovery/reporting duhen trajtuar **para** M9 (sjellja e parave/kreditit në prodhim), para
> përdorimit real të wallet/credit në prodhim dhe para sign-off-it të besueshmërisë së go-live: SMS (raportim, veprim admin i kontrolluar,
> rikonsilim i wallet hold, audit trail, vendim i shprehur për lease/auto-recovery); email (raportim, veprim admin, politika për
> provider-sukses + COMMIT#2 dështim, vendim i shprehur për lease/auto-recovery). Nuk bllokon M3. E2 (email stuck reporting) mbetet në
> reliability backlog dhe nuk bëhet para M3.


Dokumenti përmbledh gjendjen **pas M2**. Detajet e karakterizimit dhe të secilit hap janë te `QUEUE_SEMANTICS.md` dhe
`PERFORMANCE.md`. Nuk përdoret "exactly once" askund: nuk provohet end-to-end për asnjë kanal.

## 1. Arkitektura

```
                         DOMAIN (services/*)                    QUEUE MECHANICS (app/queue/*)         INFRASTRUCTURE
 ┌────────────────────────────────────────────┐        ┌────────────────────────────────────┐   ┌──────────────────┐
 │ SMS    messages.py  + _SmsHooks            │──────▶ │ DispatchQueue (Protocol)           │   │                  │
 │ Email  emails.py    + _EmailHooks          │──────▶ │   └ PostgresDispatchQueue ─────────┼──▶│   PostgreSQL     │
 │                                            │        │ DispatchSpec: model, pending,      │   │  (rreshti i      │
 │                                            │        │   attempts, next_attempt_at, id,   │   │   domain-it =    │
 │                                            │        │   backoff_s, max_attempts          │   │   queue; SKIP    │
 ├────────────────────────────────────────────┤        ├────────────────────────────────────┤   │   LOCKED)        │
 │ Webhook webhooks.py + webhook_queue.py     │──────▶ │ DeliveryQueue (Protocol)           │   │                  │
 │   (_WebhookHooks: politika e endpoint-it)  │        │   └ PostgresDeliveryQueue ─────────┼──▶│                  │
 │                                            │        │ DeliverySpec: model, base, eligible│   │                  │
 │                                            │        │   attempts, next_attempt_at, id,   │   └──────────────────┘
 │                                            │        │   lease_s, retry_delays_s          │
 ├────────────────────────────────────────────┤        └────────────────────────────────────┘
 │ Campaigns · sweeps (expire_stale,          │   JASHTË abstraksionit, qëllimisht (lock pune / mirëmbajtje, jo queue artikujsh)
 │ expire_pending) · DLR · retention          │
 └────────────────────────────────────────────┘
```
Drejtimi i varësisë: **services → app/queue**, kurrë anasjelltas (provuar nga `tests/test_queue_boundaries.py`).

| Shtresa | Çfarë është |
|---|---|
| **Queue mechanic** (`app/queue`) | zgjedhja e elementit "due", `FOR UPDATE [OF] SKIP LOCKED`, `attempts`, planifikimi/lease, koha e retry, rendi `(next_attempt_at,id)`, `publish` pa commit |
| **Domain behavior** (services + hooks) | state machine (`_move`), ngjarjet (MessageEvent/EmailEvent/Event), wallet hold, klasifikimi i gabimeve (i përkohshëm/i përhershëm), kodet e gabimit, HTTP/SMTP/provider, nënshkrimi, politika e disable të endpoint-it, idempotency e publish, tenant scoping |
| **Infrastructure** | PostgreSQL (tabelat ekzistuese `sms_messages`, `sms_emails`, `sms_webhook_deliveries`). Asnjë broker, asnjë tabelë `queue_items`/dead-letter |
| **Nuk u fut qëllimisht** | campaigns (`run_due`: lock pune për hap), sweeps (`expire_stale`, `expire_pending`), DLR/provider events, retention, listimi/metrikat admin, `stuck_sending`, kill switch |

## 2. Kontratat finale
```python
# SMS + email (mbajtje me status; at-most-once për crash)
DispatchSpec(model, pending, attempts, next_attempt_at, id, backoff_s=30, max_attempts=5)
DispatchHooks:  reserved(db,item) · requeued(db,item,error) · sent(db,item,provider_ref) · failed(db,item,reason)
DispatchQueue:  publish(db,item,*,not_before=None)           # add + flush, kurrë commit
                reserve(db,now)                              # due, SKIP LOCKED, hooks.reserved, attempts+1
                acknowledge(db,item,provider_ref)            # hooks.sent
                retry(db,item,*,error,temporary,now) -> Outcome{RETRIED,FAILED,EXHAUSTED}
                fail(db,item,reason)                         # hooks.failed
                cancel_if_pending(db,item_id,*,reason)->bool # SKIP LOCKED

# Webhook (lease; at-least-once)
DeliverySpec(model, base, eligible, attempts, next_attempt_at, id, lease_s=120, retry_delays_s=(30,120,600,1800,7200,21600,43200))
DeliveryHooks:  completed(db,item,now) · failed(db,item,outcome) · replayed(db,item)
DeliveryQueue:  publish(db,items)                            # vetëm db.add (pa flush), kurrë commit
                reserve(db,now)                              # eligible+due, SKIP LOCKED OF model, lease, attempts+1
                lock(db,id,*,where=None)                     # FOR UPDATE që pret
                complete(db,item,now) · retry(db,item,*,now,permanent)->DeliveryOutcome · fail(db,item,outcome)
                replay(db,item,*,now)                        # attempts=0, next=now, hooks.replayed, flush
```
Dy kontrata, jo një: `DispatchQueue` mban me **status** (SENDING, pa lease, retry me formulë), `DeliveryQueue` mban me **lease**
(PENDING mbetet, retry me listë eksplicite, replay).

### Rishikimi i kontratave (gjetje; asnjë API nuk u ndryshua)
| Gjetje | Lloji | Vendim |
|---|---|---|
| `DispatchQueue.fail`, `DeliveryQueue.fail` nuk thirren nga kodi i prodhimit (vetëm `retry`/`cancel_if_pending` thërrasin `hooks.failed`) | metodë pa caller prodhimi | mbahet (e emëruar në kontratën e miratuar, e testuar); rishiko në M3 nëse mbetet pa përdorues |
| `DispatchQueue.publish(not_before=…)` përdoret vetëm nga testet | parametër pa caller | mbahet (planifikim i qëllimshëm); rishiko |
| `DeliveryQueue.lock(where=…)` përdoret vetëm nga `redeliver` | një caller | e justifikuar (predikati i tenant-it) |
| `publish` asimetrik: Dispatch bën flush, Delivery jo | mospërputhje | **e qëllimshme**: ruan SQL-in identik (provuar); dokumentuar |
| `_SmsHooks` ≈ `_EmailHooks` (4 metoda pothuajse identike, ndryshojnë `_move`/`_fail`) | logjikë e dyfishtë e vogël | e pranueshme: secili ka state machine të vet; mos u bashkoftë pa nevojë |
| Kontratë **implicite** `service → DeliveryHooks`: service duhet të vendosë `last_status_code`/`last_error` PARA `retry`, dhe të kyçë endpoint-in para `completed/failed` (hooks e lexojnë nga identity map pa SQL) | implicit | dokumentuar këtu dhe te `QUEUE_SEMANTICS.md`; test mbulon rezultatin |
| `PostgresDispatchQueue` emri sugjeron PG, kodi është SQLAlchemy i përgjithshëm (`skip_locked` injorohet në SQLite) | emërtim | pranuar (PG është backend-i i vetëm); SQLite s'është besnik për konkurrencë |
| Hooks marrin `item` ORM të gjallë; asnjë DTO | coupling i pranuar | rrjedh nga "rreshti = queue" (vendim i miratuar) |

## 3. Kufijtë (import / varësi)
Provuar nga testet (nuk dublikohen): `test_queue_package_imports_nothing_from_the_domain`,
`test_queue_package_never_commits_or_rolls_back`, `test_delivery_adapter_imports_no_http_or_domain_modules_and_never_commits`
dhe, të reja në M2-e (`tests/test_queue_boundaries.py`): `app/queue` varet vetëm nga stdlib, SQLAlchemy dhe `app.queue`;
services importojnë queue, jo anasjelltas; SKIP LOCKED jashtë `app/queue` vetëm te 3 përjashtimet e dokumentuara;
konstantet e retry kanë një burim (spec); asnjë aritmetikë backoff jashtë adapterit.

## 4. Matrica e transaksionit
| | SMS | Email | Webhook |
|---|---|---|---|
| Flow | reserve → **COMMIT#1** → provider → finalize → **COMMIT#2** | reserve + lexime (domen, DKIM) → **COMMIT#1** → MIME/DKIM → SMTP → finalize → **COMMIT#2** | reserve (lease) + lexim endpoint/event → **COMMIT** → HTTP → lock delivery+endpoint → complete/retry → **COMMIT** |
| Tx DB gjatë provider/HTTP | **jo** | **jo** (patch-i i email) | **jo** |
| Lidhje pool e checkout-uar gjatë provider/HTTP | **jo** (0) | **jo** (0; para patch-it 16/16) | **jo** (0) |
| Row lock gjatë provider/HTTP | **jo** | **jo** | **jo** (`FOR UPDATE NOWAIT` kalon) |
| Rezervimi | statusi SENDING | statusi SENDING | lease (`next_attempt_at = now+120 s`) |
| Garancia | **at-most-once** për crash (SENDING pa lease) | **at-most-once** për crash | **at-least-once** (lease skadon → ridërgim) |
| Retry | temporar & attempts<5: `30·2^(n−1)` = 30/60/120/240; i përhershëm → FAILED | e njëjta | listë eksplicite 30/120/600/1800/7200/21600/43200; 8 përpjekje; 410/unsafe → FAILED + disable |
| Crash para COMMIT#1 | pa efekt (QUEUED, attempts 0) | pa efekt | pa efekt |
| Crash pas COMMIT#1, para provider | **SENDING**, manual | **SENDING**, manual | lease skadon (120 s) → riprovim |
| Provider OK, crash/COMMIT#2 fail | **SENDING**; mund të jetë dërguar | **SENDING**; mund të jetë dërguar | lease skadon → **dublikim i mundshëm** |
| Raportim i ngecurish | `stuck_sending` (count te admin, >10 min) | **s'ka** | s'ka nevojë (lease) |
| Gjendje terminale | FAILED + `error_code`, hold i lëshuar | FAILED + `error_code` | FAILED + `last_error`; replay rikthen PENDING |

## 5. Garancitë e dërgimit
- **SMS: at-most-once për dritaren aktuale të crash-it** (pas COMMIT#1). Përjashtim: përjashtim i papritur nga provider-i ri-radhitet → për rezultat të panjohur është të paktën një herë, mbrojtur vetëm nga idempotency e provider-it (`reference = public_id`; provuar me FakeProvider, jo me Twilio real).
- **Email: at-most-once për dritaren aktuale të crash-it**; i njëjti përjashtim për përjashtim të papritur (Message-ID si dedup, e paprovuar me SMTP real).
- **Webhook: at-least-once me lease**; pranuesi deduplikon me `X-SMS-Delivery-Id`.
- **Idempotency e enqueue** (unique `(owner, key)`, e njëjta kërkesë → i njëjti rresht): vetëm për publish; **nuk** garanton procesim një herë.
- **Dedup i provider/pranuesit:** SMS/email varen nga provider; webhook nga pranuesi.
- **Retry:** SMS/email me formulë, webhook me listë; asnjë nuk garanton "pa dublikim".

## 6. Regjistri i borxhit të besueshmërisë (NUK implementohet në M2)
| # | Borxhi | Ashpërsia | Gjasa | Ndikimi | Rekomandim | Faza e sugjeruar |
|---|---|---|---|---|---|---|
| S1 | SMS `SENDING` i ngecur pas crash (COMMIT#1→COMMIT#2) | Lartë | Mesatare-ulët (SIGKILL/deploy gjatë provider call; rritet me volum) | mesazh i vonuar/i humbur; **hold-i i parave mbetet i bllokuar** | (a) raportim + alarm, (b) veprim admin "ri-dërgo me të njëjtin reference / dështo+lësho", (c) pastaj lease me idempotency të provider-it | para M9 (para parave reale); (a)+(b) sa më parë |
| S2 | Rikuperim vetëm manual, pa veprim në admin (vetëm numërim) | Mesatare | Mesatare | operim i ngadaltë | (b) më sipër | me S1 |
| S3 | SMS pa lease/auto-recovery | Mesatare | = S1 | = S1 | vetëm pas S1(a,b) dhe idempotency të provider-it të verifikuar | M-reliability |
| S4 | Idempotency e provider-it SMS e paprovuar (Twilio real) | Lartë | Mesatare | dublikim nëse ri-radhitet pas përjashtimi të papritur | smoke me Twilio real; mos aktivizo lease pa të | para go-live |
| E1 | Email `SENDING` pas crash (para provider) | Lartë | Mesatare-ulët | email i vonuar/i humbur | si S1 | si S1 |
| E2 | Email pa `stuck_sending` reporting | Mesatare | Mesatare | pa dukshmëri | shto raportim analog me SMS | **hapi i parë i vogël** |
| E3 | Provider OK + COMMIT#2 dështon → SENDING, mund të jetë dërguar | Lartë | Ulët (dritare e shkurtër pas patch-it) | email "i humbur" ose dublikim në ri-dërgim manual | E2 + ri-dërgim me Message-ID të njëjtë (dedup i provider-it) | me E2 |
| E4 | Email pa lease | Mesatare | = E1 | = E1 | si S3 | M-reliability |
| E5 | SMTP real i paprovuar (vetëm fake) | Mesatare | — | sjellje timeout/TLS e panjohur | smoke me SMTP real (staging) | para go-live |
| W1 | Dublikim webhook pas HTTP OK + crash para finalize | Mesatare | Ulët-mesatare | pranuesi merr dy herë | dokumentim për klientët: dedup me `X-SMS-Delivery-Id`; asnjë ndryshim kodi | docs/API (vogël) |
| W2 | Pranuesi duhet të deduplikojë (kërkesë për klientët) | Ulët | — | — | shto te `docs/API.md` | me W1 |
| W3 | Replay nuk riaktivizon endpoint-in e çaktivizuar | Ulët | Mesatare | klienti mendon se u dërgua | vendim produkti: replay të raportojë "endpoint disabled", jo heshtje | M3+ (API) |
| W4 | HTTP/TLS/latencë reale e paprovuar (vetëm MockTransport) | Mesatare | — | timeout vs lease 120 s | smoke me endpoint real; `webhook_timeout`=10 s ≪ lease | para go-live |
| T1 | Flake `test_0019_downgrade_restores_and_reupgrade_works[postgres]` (2 nga ~7 ekzekutime të plota PG; lë `sms_m1b_*`) | Ulët | Mesatare (në sandbox) | gate i paqëndrueshëm | forco fixture-in (retry CREATE DATABASE, pastrim) | në çdo kohë (e vogël) |
| T2 | SQLite pa SAVEPOINT korrekt (rollback pas `submit()` nuk zhbën); SKIP LOCKED i injoruar | Ulët | e njohur | SQLite s'është besnik | PG si burim i së vërtetës; CI me PG; mos shto teste konkurrence në SQLite | vazhdimisht |
| T3 | Cluster PG ndalet midis turneve (mjedis sandbox) | Ulët | e lartë në sandbox | gabime të rreme setup | service container në CI; skript start | CI |
| Q1 | Metoda pa caller prodhimi (`fail`, `publish(not_before)`) | Ulët | — | sipërfaqe e panevojshme | rishiko në M3 | M3 |
| Q2 | SKIP LOCKED jashtë queue (campaigns, `expire_stale`, `expire_pending`) kundër kriterit origjinal të M2 | Ulët | — | kriteri nuk plotësohet litteralisht | pranuar nga ti (jashtë scope); fiksuar nga test allowlist | M-reliability (ExclusiveTask, opsionale) |

## 7. Baseline përfundimtar i performancës (nga matjet e M2; një makinë, provider/HTTP fake, BASE/NEW të ndërthurura)
| Kanal | Matje | Vlera pas M2 | Para → pas |
|---|---|---|---|
| SMS | accept A1 / A2 (req/s) | 55.5 / 79.3 | −1.8% / −1.1% |
| | drain, 2 workers (msg/s) | 223.1 | −0.8% |
| | CPU accept / drain | 12.8 s / 34.2 s | +1.0% / +0.3% |
| | SQL submit / process_one / retry | 21 / 8 / 8 | identik (100 statements) |
| Email | submit (/s) | 91.0 | −1.2% (pas patch-it; −2.1% në M2-c) |
| | drain (/s; DKIM+MIME reale) | 41.0 | +2.2% (+0.3% në M2-c) |
| | CPU submit / drain | 8.3 s / 21.7 s | +0.5% / −2.1% |
| | SQL submit / process_one / retry / fail / cancel | 10 / 9 / 9 / 9 / 6 | identik (93 statements) |
| | pool gjatë provider (16 workers) | 0 lidhje, 0 `idle in transaction` | para patch-it 16 / 16 |
| Webhook | publish (/s; 1 event→2 deliveries) | 171.5 | −1.1% |
| | drain (/s) | 88.0 | +2.3% |
| | CPU publish / drain | 7.2 s / 26.3 s | +1.2% / −0.7% |
| | SQL emit / deliver ok / retry / fail 410 / replay | 4 / 7 / 7 / 8 / 2 | identik (37 statements) |
| | gjatë HTTP | 0 lidhje, 0 idle in tx, 0 lock | e pandryshuar |
Të gjitha brenda ±5%. Numrat absolutë varen nga makina; vlejnë si baseline krahasues, jo si kapacitet prodhimi.

## 8. Matrica e testeve
Legjenda: ✓ i provuar · — jo i zbatueshëm/jo i provuar · ✗ boshllëk. "Real" = provider/SMTP/HTTP/Twilio real.
| Semantika | SQLite | PostgreSQL | Fake provider/HTTP | Real |
|---|---|---|---|---|
| **SMS** characterization (publish, due-only, rend, attempts) | ✓ | ✓ | ✓ | ✗ |
| SMS concurrency (SKIP LOCKED, stampede, cancel vs reserve) | — (SKIP LOCKED i injoruar) | ✓ (Barrier, 2+ lidhje) | ✓ | ✗ |
| SMS rollback / outbox | — (SAVEPOINT) | ✓ | ✓ | — |
| SMS retry 30/60/120/240, permanent, exhausted | ✓ | ✓ | ✓ | ✗ (klasifikimi i gabimeve real) |
| SMS crash window (pas/ para COMMIT#1; `pg_terminate_backend`) | ✓ (simulim) | ✓ (kill backend) | ✓ | ✗ (kill proçesi real) |
| SMS idempotency (publish; paralel me të njëjtin key) | ✓ | ✓ | ✓ | ✗ (dedup i provider-it) |
| **Email** characterization/concurrency/rollback/retry/crash/idempotency | si SMS | si SMS | ✓ | ✗ |
| Email transaction/provider (pa SQL, pa tx, pa lidhje, pa row lock, `pg_stat_activity`, pool 16 workers, `expire_on_commit=True`) | ✓ (pa pool/pg_stat) | ✓ | ✓ | ✗ (SMTP real) |
| Email failure windows (COMMIT#1 fail → provider s'thirret; COMMIT#2 fail → SENDING) | ✓ | ✓ | ✓ | ✗ |
| DKIM/MIME | ✓ | ✓ | ✓ | ✗ |
| **Webhook** lease (aktiv, skadim, attempts, 120 s) | ✓ | ✓ | ✓ | ✗ |
| Webhook retry exact + exhaustion | ✓ | ✓ | ✓ | ✗ |
| Webhook replay (i njëjti rresht, tenant, PENDING refuzohet, endpoint s'riaktivizohet) | ✓ | ✓ | ✓ | — |
| Webhook disable (5 radhazi, 410, unsafe_url, reset në sukses) | ✓ | ✓ | ✓ | ✗ |
| Webhook at-least-once (ridërgim me të njëjtin Delivery-Id) | ✓ | ✓ | ✓ | ✗ |
| Webhook concurrent reserve (1 nga 6 pas skadimit; skip locked pa pritje) | — | ✓ | ✓ | ✗ |
| Webhook HTTP pa tx/lidhje/lock/SQL | ✓ (pa pool/lock) | ✓ | ✓ | ✗ (latencë/TLS reale) |
| Adapterët: pa commit, pa import domain, SQL count të ngurtë | ✓ | ✓ (count PG) | — | — |

**Boshllëqet kryesore:** asnjë provë me provider real (Twilio, SMTP, endpoint HTTP real); kill i proçesit real (jo vetëm i lidhjes);
konkurrencë vetëm në PG; idempotency e provider-it e paverifikuar; kapaciteti nën ngarkesë të gjatë/me volum prodhimi.

## 9. Pastrimi (gjetje)
Bërë (sigurt, pa ndryshim sjelljeje): hequr aliasi i vdekur `NowFn` dhe importi `Callable` te `app/queue/dispatch.py`;
shtuar `tests/test_queue_boundaries.py`; dokumentacioni u harmonizua (shih më poshtë).
Kontrolluar dhe **pastër**: asnjë import/variabël e papërdorur te `app/` (ruff F401/F841/F811); asnjë konstante e vjetër retry/backoff e
papërdorur (`MAX_ATTEMPTS`, `BACKOFF_SECONDS`, `RETRY_DELAYS`, `LEASE_SECONDS`, `DISABLE_AFTER` përdoren dhe ushqejnë specs); `_after_error`
(SMS/email) është hequr; asnjë SELECT queue i mbetur te services (përveç 3 përjashtimeve).
Vetëm raportim: `QUEUE_SEMANTICS.md` i përzien tabelën historike të karakterizimit (para M2-b) me seksionet M2; u shtua shënim në krye.
Hooks SMS/email të ngjashme (shih §2). `docs/MIGRATION_PLAN.md` M2 përmbante emra të planit fillestar (`PostgresOutboxQueue`, `InMemoryQueue`,
`dead_letter`): u shënua "ZBATUAR" me devijimet. `ARG` te testet (parametra fixture të papërdorur): zhurmë, jo problem.

## 10. Kriteret e pranimit të M2
| Kriteri | Statusi |
|---|---|
| SMS dhe Email përdorin `DispatchQueue` | ✓ |
| Webhook përdor `DeliveryQueue` | ✓ |
| Adapteri nuk njeh domain-in | ✓ (AST + fjalë të ndaluara) |
| SQL semantics të ruajtura | ✓ (SQL i normalizuar identik: SMS 100, email 93, webhook 37 statements) |
| Kufijtë transaksionalë të ruajtur ose të përmirësuar me miratim | ✓ (email: patch i miratuar; SMS/webhook të pandryshuar) |
| Full suites të gjelbra; PG concurrency e gjelbër | ✓ (shih verdiktin) |
| Performance ≤ 5% | ✓ (të gjitha) |
| Docs të përditësuara | ✓ |
| Asnjë broker/infrastrukturë e re | ✓ |
| Asnjë commit i fshehur | ✓ (test) |
| Asnjë pretendim exactly-once | ✓ |
| *(plani origjinal)* "asnjë SKIP LOCKED jashtë `app/queue/`" | **Pjesërisht**: 3 përjashtime të qëllimshme (campaigns, 2 sweeps), të fiksuara nga test allowlist |
