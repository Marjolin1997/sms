# M9 — Para / kredi / autoritet tregtar / rakordim: AUDIT + DESIGN GATE
Gjendja: dega `claude/sms-platform-architecture-lugdyz` pas M8. **Asnjë kod nuk u ndryshua.** Çdo pohim më poshtë është gjurmuar te shkruesit realë (jo nga emrat). Asgjë nuk zbatohet pa miratimin e dizajnit.

## 1. Audit — tabela e koncepteve
| Koncept | Pronari sot | Tabela / modele | Shkruesit | Lexuesit | Kufiri i transaksionit | Probleme të njohura | Vendim M9 |
|---|---|---|---|---|---|---|---|
| Wallet | Enterprise (`app/`) | `sms_wallets` (`owner_ref`,`currency`, UNIQUE; `enterprise_id` nullable nga `TenantOwned`) — pa balancë | `wallet.create_wallet`, API `POST /wallets` (audit) | portal, `balances()` | rresht i kyçur `FOR UPDATE` për çdo lëvizje | çelësi i biznesit është `owner_ref`, jo `enterprise_id` | **KEEP**; harto Central account→wallet me `(enterprise_id, currency)` |
| Ledger | Enterprise | `sms_ledger_entries` (available/held delta + after, `idempotency_key` UNIQUE(wallet,key), CHECK `*_after >= 0`, ORM guard + PG triggers `UPDATE/DELETE/TRUNCATE`) | vetëm `wallet._post` (nën kyç wallet-i) | `balances()` = rreshti i fundit, `verify_wallet` = SUM(delta) | brenda transaksionit të thirrësit | triggers vetëm PG (SQLite dev jo); `verify_wallet` s'kontrollon që Σ(holds aktive) = `held_after` | **KEEP** (bazë e ledger-it operacional); shto lloje `grant`/`grant_reversal` |
| Balanca | Enterprise | s'ka kolonë; = `available_after`/`held_after` i rreshtit të fundit | — | API, portal, campaigns | — | O(1) por vetëm nga rreshti i fundit | **KEEP** (ledger first) |
| Hold (rezervim) | Enterprise | `sms_holds` (amount>0, `captured_amount`, status ACTIVE→CAPTURED\|RELEASED, UNIQUE(wallet,reference)) | `wallet.reserve/capture/release` | `messages`, `campaigns` | rezervimi bëhet në savepoint me INSERT të mesazhit | tabela s'është e pandryshueshme; s'ka invariant DB `held_after = Σ aktive` | **KEEP**; shto invariant verifikimi në reconcile |
| Topup | Enterprise | `sms_topups` (pending/confirmed/failed, `external_ref` UNIQUE) | `wallet.create_topup/confirm_topup`; API `topup:write` + `topup:confirm` (krijuesi ≠ konfirmuesi, përveç superadmin; audit); `payments._apply` | admin | `_post(TOPUP)` + status në të njëjtin tx | **minton para lokalisht** (kjo ndalet nën autoritet Central) | **REPLACE** në modalitet Central (autoriteti: Central payment→grant); mbetet si `local` në tranzicion |
| Payment online | Enterprise | `sms_payments` (shuma/monedha nga serveri; webhook verifikon; `amount_mismatch`→FAILED+rakordim) | `payments.start_payment/complete`; gateway adapter (`fake` vetëm) | admin/portal | webhook → `_apply` → top-up në të njëjtin tx | pa gateway real; `complete` s'ka `audit()` (ka event+ledger ref); fatura e paguar → paraja bëhet kredit wallet | **MIGRATE** te Central (autoritet pagese); Enterprise s'pranon pagesa në modalitet central |
| AccountPlan | Enterprise | `sms_account_plans` (`rate_card_id`, `enabled`, limite) — **0 fusha monetare** | admin API | `messages.submit` | — | — | **KEEP** (lidhja me rate card = snapshot lokal) |
| Çmimet (RateCard) | Enterprise | `sms_rate_cards` (monedhë), `sms_rate_card_versions` (draft→published, `effective_from`, e pandryshueshme), `sms_rates` (`price_per_segment` NUMERIC(20,6), prefix+operator; ORM guard pas publikimit) | API `rates.py` (pricing role) | `rates.quote` | — | autoriteti është Enterprise (M5-d e shtyu); s'ka lidhje me EnterpriseProduct | **MIGRATE** autoritetin te Central; Enterprise mban snapshot të pandryshueshëm sinkronizuar |
| Kosto SMS (klient) | Enterprise | `sms_messages`: `currency, unit_price, total_price, rate_version_id, rate_id, segments, encoding` ngrihen në submit | `messages.submit` | campaigns, reports, DLR | në savepoint me hold | `Message` s'ka kosto vendori | **KEEP** (snapshot i çmimit tashmë ekziston) |
| Kosto email | Enterprise | **s'ka çmim/hold për email**; `sms_plans` (`monthly_fee`, `included_emails`, `email_overage_price`) | `billing.create_plan` | `generate_invoice` | — | emaili s'kalon nga wallet; faturohet pas faktit sipas numrit të email-eve `SENT/DELIVERED/BOUNCED/COMPLAINED` në periudhë | **DEFER** (mbetet abonim+overage; jo wallet në M9) |
| Monedha | Enterprise | `String(3)` në wallet/ratecard/plan/invoice/payment/message; pa CHECK ISO | — | — | — | s'ka FX; wallet UNIQUE(owner,currency) lejon shumë wallet; `submit` zgjedh wallet sipas monedhës së rate card (NotFound nëse s'ka) | **KEEP** (pa FX); shto CHECK format në Central |
| Precision | Enterprise | `MONEY = Numeric(20,6)`; `wallet.money()` refuzon float/ >6 decimale; faturat `cents()` ROUND_HALF_UP 0.01; `Numeric(6,4)` TVSH | — | — | — | s'ka të dhëna reale çmimesh në repo (vetëm teste) | **KEEP NUMERIC(20,6)** në të dy planet (zgjedhja ekzistuese; çmim/segment 6 dec. × int mbetet saktësisht 6 dec.) |
| Debit SMS | Enterprise | hold në submit → capture në DLR `delivered` → release në fail/cancel/`dlr_timeout` | `messages.submit/apply_dlr/expire_stale/_fail` | — | submit: savepoint; DLR: `FOR UPDATE` mesazh → `_post` (kyç wallet) | **faturim në DORËZIM** (DLR); SENT pa DLR 72h ⇒ FAILED+release (klienti s'paguan) | **KEEP** semantikën; shih §4 për SENDING |
| Debit email | — | s'ka | — | — | — | — | **DEFER** |
| Fushata | Enterprise | `campaigns` → `msg.submit` për çdo marrës (`camp:{id}:{contact}`), `max_cost` kundrejt Σ `total_price` të jo-FAILED | `campaigns._submit_sms` | stats | savepoint për marrës | `InsufficientFunds` pauzon fushatën; s'ka hold në nivel fushate (nuk duhet) | **KEEP** |
| Retry/failed SMS | Enterprise | `queue.retry`: i përkohshëm → QUEUED+backoff `30·2^(n-1)`, max 5; i përhershëm/EXHAUSTED → FAILED + `wallets.release` | `PostgresDispatchQueue.retry`, `_SmsHooks` | — | COMMIT#2 | një hold për të gjitha riprovat (reference=`public_id`); retry nuk prek paratë | **KEEP** |
| DLR / rregullime | Enterprise | `apply_dlr`: delivered→capture; failed→release; idempotent; kontradiktor→`conflict` (në `sms_dlr_receipts`); mesazh i panjohur→503 (provider riprovon) | `api/webhooks.py`, `api/twilio.py` | admin queue | tx me `FOR UPDATE` mesazh | DLR i vonuar `delivered` pas `dlr_timeout` refuzohet ⇒ mesazh i dorëzuar, klienti s'paguan (rrjedhje të ardhurash, jo e klientit); kosto provider-i s'lexohet | **KEEP**; përmirëso në M9-a |
| Refund/reversal | Enterprise | `wallet.refund(wallet, amount, key)` (CREDIT `available`) — **0 thirrës** | — | — | — | **shumë arbitrare**, jo e lidhur me hold ⇒ mund të mintojë para nëse lidhet gabim | **REPLACE**: refund vetëm i lidhur me capture ≤ shumës; reversal Central si hyrje ledger |
| Rregullim manual | Enterprise | `wallet.adjustment` (delta ±, çelës, shënim ≥3, `wallet:adjust`, audit në API); s'bie nën 0 (`_post`+CHECK) | `api/wallets.py /adjustments` | — | tx API | delta pozitive mint lokal | **KEEP** (negativ); **pozitiv ⇒ vetëm via Central grant** në modalitet central |
| Admin API balancash | Enterprise | `/wallets`, `/ledger`, `/topups`, `/adjustments`, `/alert`, `/verify` | RBAC `finance`/`superadmin` | konsola | `_run` commit/rollback | `topup:confirm`≠krijues ✔ | **KEEP**; mbyll mint lokal në modalitet central |
| Faturim | Enterprise | `sms_plans/subscriptions/invoices/invoice_lines/billing_profiles`; faturat/linjat të pandryshueshme (ORM guard); `pay_from_wallet` → `charge` (INVOICE debit) | `billing.generate_invoice/run_billing` | portal | një abonim për tx | TVSH vendoset nga staf; një monedhë/plan | **KEEP** (jashtë M9 core) |
| Kod tregtar në Central | — | **asnjë** (`apps/central` s'ka pagesë/wallet/çmim/ledger) | — | — | — | — | **NEW** |
| Triggers parash | PG | `sms_forbid_mutation()` mbi `sms_ledger_entries`/audit | migrim 0001/0006 | — | — | vetëm PG; `sms_holds`, `sms_topups`, `sms_payments` pa trigger | **KEEP**; trigger të njëjtë për ledger-in Central |
| Audit | Enterprise | `sms_audit_log` (API wallet/topup/adjust/alert aktor+rol); ledger = prova për lëvizjet nga sistemi | `services.audit.audit` | admin | i njëjti tx | `payments.complete` pa audit rresht (event+ledger ref) | **KEEP**; Central ledger/pagesa me aktor njeri\|sistem (`record_system`) |

## 2. Burimet e së vërtetës (sot)
Një: **Enterprise** (`sms_ledger_entries` + `sms_holds`). Central s'ka asgjë tregtare. Çmimi: Enterprise (`sms_rate_cards*`). Pagesa: Enterprise (`sms_payments/topups`).

## 3. Sekuenca e faturimit SMS (gjurmuar në kod)
1. `messages.submit` (idempotent `(owner_ref,key)` + `request_hash`): kontrolle (switch, plan, M7 gate, rate limit, numër, route, sender, consent, tekst/segmente) → `rates.quote` (version efektiv, prefiks më i gjatë, `total = price × segments`, refuzon ≤0) → wallet i monedhës.
2. Savepoint: `reserve` (`hold:{public_id}`: `available -= total`, `held += total`; `InsufficientFunds` nëse s'ka) + INSERT `Message` QUEUED (me `unit_price/total_price/rate_version_id/rate_id/hold_id`) + `MessageEvent`. Gara me të njëjtin key ⇒ IntegrityError ⇒ rollback i savepoint (edhe hold) ⇒ kthen fituesin.
3. Worker `process_one`: `queue.reserve` (SKIP LOCKED, `_move(SENDING)`, `attempts+=1`) → **COMMIT#1** → `provider.send` (pa tx) → `acknowledge` (SENT + `provider_message_id`) | `retry` | `_fail` (+release) → **COMMIT#2**.
4. DLR: `delivered` ⇒ `_move(DELIVERED)` + `capture(hold)` (debit: `held -= total`, pa lëvizje `available`); `failed` ⇒ `_fail` + release. Idempotent; kontradiktor ⇒ Conflict+receipt.
5. `sweep` (çdo 60s): `expire_stale` SENT>72h ⇒ FAILED `dlr_timeout` + release. **Asgjë s'trajton SENDING.**
Pra: **faturim në dorëzim të konfirmuar**; para e rezervuar nga pranimi deri te DLR/afati.

## 4. Sekuenca email dhe dritaret e crash-it (S1/E1)
**Email:** pa para në rrugë (s'ka hold/çmim); `submit` publikon QUEUED; `process_one`: claim → leximet DKIM/domen → COMMIT#1 → MIME/DKIM/SMTP pa tx → SENT/retry/FAILED → COMMIT#2. Faturohet pas faktit nga numri i email-eve billable në periudhë (overage). `Message-ID` është deterministik (`<public_id@domain>`).
**SMS/email — dritaret** (kodi: `messages.process_one`, `emails.process_one`, `PostgresDispatchQueue`):
| # | Dritarja | Gjendja pas crash | Para | Rezultat |
|---|---|---|---|---|
| W1 | para COMMIT#1 | QUEUED (rollback) | hold ACTIVE | i sigurt; rinis |
| W2 | pas COMMIT#1, para thirrjes provider | SENDING pa dërgim | hold ACTIVE **përgjithmonë** | mesazh i humbur; para e bllokuar; s'ka lease/sweeper |
| W3 | gjatë thirrjes provider (kërkesa në fluturim) | SENDING; provider mund ta ketë pranuar | hold ACTIVE | rezultat i panjohur |
| W4 | provider pranoi, crash para COMMIT#2 (ose COMMIT#2 dështon) | SENDING **pa `provider_message_id`** | hold ACTIVE | DLR s'përputhet (503→riprovë nga provider-i, por rreshti s'ka id kurrë) ⇒ hold bllokuar; mesazhi mund të jetë dorëzuar |
| W5 | timeout/gabim rrjeti **me kërkesë të mundshme të mbërritur** | `HttpProvider`: rrjeti→**i përkohshëm→QUEUED→ridërgim** (mbështetet vetëm te dedup i provider-it me `reference`, e paverifikuar); `TwilioProvider`: `twilio_outcome_unknown`→**FAILED+release** (s'ridërgon; klienti s'paguan; mesazhi mund të ketë shkuar) | — | rrezik dublim dërgimi (HTTP) ose dërgim falas (Twilio) |
| W6 | `except Exception` jo-ProviderError (të dy rrugët) | trajtohet si i përkohshëm ⇒ **ri-radhitet** | hold i njëjtë | rrezik dublim te provider-ë pa idempotence (Twilio s'ka) |
| W7 | DLR para COMMIT#2 | 503 (provider riprovon) ose pas 72h SENT→FAILED | — | nëse provider-i s'riprovon: dorëzuar, klienti s'paguan |
Para-pasoja: asnjëherë **debit i dyfishtë** (hold unik, `capture:{hold_id}` unik, kyç wallet-i), por **para të bllokuara** (W2–W4) dhe **dërgime të dyfishta/falas** (W5–W7) pa rakordim. Emaili: W2–W4 ⇒ SENDING i ngecur pa raportim (`stuck_sending` ekziston vetëm për SMS) dhe pa pasojë parash (jo billable).
Provat ekzistuese: `tests/test_queue_semantics.py` (SIGKILL/`pg_terminate_backend` ⇒ SENDING përgjithmonë, i dokumentuar si i pranuar deri te M9).

## 5. Rekomandimi S1/E1 (HARD GATE para parash reale)
Vetëm **raportim** s'mjafton. Parimi: **rezultat i panjohur ≠ retry ≠ fail**. Kurrë ridërgim automatik pa provë idempotence të provider-it.
1. **Gjendje e re `UNKNOWN`** (SMS dhe email): SENDING pa progres > `SENDING_LEASE` (default 10 min, >> timeout provider-i 10s) kalon (sweeper, SKIP LOCKED, event+MessageEvent) në `UNKNOWN`. Hold mbetet ACTIVE (para të mbajtura, jo të kapura, jo të liruara). Alarm + numërues + dukshmëri në `admin/queue`.
2. **Aftësia e provider-it** `idempotent_by_reference: bool` (Fake=true; Http=konfigurim, default false; Twilio=false). Rrugët që sot ri-radhisin ose dështojnë me rezultat të panjohur (W5/W6) bëhen: provider idempotent ⇒ retry (si sot); përndryshe ⇒ **`UNKNOWN`** (jo ridërgim, jo release automatik). `twilio_outcome_unknown` ⇒ `UNKNOWN`.
3. **Zgjidhja e UNKNOWN** (vetëm veprim eksplicit, i audituar, idempotent, RBAC i ri `queue:resolve`): (a) *provider-i e konfirmon* me kërkim sipas `reference` (kur provider-i e ofron; ndryshe manual) ⇒ `SENT` me `provider_message_id` ⇒ rrjedha normale DLR; (b) *provuar jo i dërguar* ⇒ `QUEUED` (ridërgim i kontrolluar, `attempts` i ruajtur) ose `FAILED`+release; (c) *dërguar, pa id* ⇒ `SENT` pa id + afati DLR ⇒ release/capture manual me shënim. Çdo veprim përdor çelës idempotence `resolve:{id}:{action}`; paratë lëvizin vetëm me `capture/release` ekzistues.
4. **Pa lease automatik ridërgimi** (S3/E4 mbeten të mbyllura) derisa një provider real të provojë idempotencë me `reference`.
5. Fallback DLR sipas `reference` kur payload-i e mban (HTTP gjenerik); Twilio s'e ofron ⇒ vetëm veprim manual/ID.
6. Testet: crash injection W2/W3/W4/W5/W6 për SMS+email (përfshirë PG `pg_terminate_backend`), invariant parash (hold ACTIVE pa capture/release automatik; zgjidhje idempotente; asnjë ridërgim për provider jo-idempotent).

## 6. Përgjigjet A–T
A. **Njësia e faturueshme:** SMS = *segmenti* (`segments × price_per_segment`), `segments` ngrihet në submit nga teksti përfundimtar (GSM-7 160/153 · UCS-2 70/67; GSM ext=2 karaktere) dhe ruhet i pandryshueshëm; `Message.text` s'ka rrugë ndryshimi pas rezervimit. Segmentimi i provider-it mund të ndryshojë — s'prek faturimin (kuantiteti është i yni, i regjistruar; ndryshimi provider-i = vetëm raport kosto, additiv).
B. **Rezervimi:** në submit, shuma e plotë e kuotës, atomikisht me INSERT-in e mesazhit (sot). Mbahet.
C. **Capture:** sot në `delivered` (DLR). **Vendim biznesi #1:** mbahet (klienti s'paguan pa konfirmim) ose kalon në SENT (pranim)? Rekomandim: mbahet; DLR i pabesueshëm ⇒ i dokumentuar si rrjedhje të ardhurash.
D. **Release:** DLR failed, FAILED terminal, cancel, `dlr_timeout`, UNKNOWN i zgjidhur "jo i dërguar". Kurrë i dyfishtë (`release:{id}` unik).
E. **Timeout provider:** provider idempotent ⇒ retry; përndryshe `UNKNOWN` (hold mbahet), jo ridërgim/jo release automatik (§5).
F. **Sukses + crash lokal:** `UNKNOWN` ⇒ zgjidhje sipas §5.3; hold i mbajtur.
G. **Retry:** një hold për gjithë riprovat; retry s'prek paratë; capture/release një herë.
H. **Submit i dyfishtë:** UNIQUE `(owner_ref,key)` + hash; hold key `hold:{public_id}` brenda savepoint ⇒ s'ka hold të dytë.
I. **Callback i dyfishtë:** `apply_dlr` idempotent; kontradiktor ⇒ Conflict + receipt; capture/release me çelësa unikë.
J. **SMS:** faturim në dorëzim (sot) — vendimi #1.
K. **Email:** jo në wallet; abonim/overage pas faktit; i shtyrë.
L. **Fushata:** një hold për mesazh (`msg.submit`), `max_cost` si buxhet, `InsufficientFunds` pauzon; pa hold fushate.
M. **Rregullime manuale:** hyrje ledger `adjustment` (delta ±, çelës, shënim i detyrueshëm, audit, RBAC); në modalitet central: **pozitive vetëm nga Central** (si grant/ledger adjustment_credit), negative lokale lejohet (zvogëlon, raportohet).
N. **Refund/reversal:** Central: hyrje `reversal`/`refund` që referon pagesën/grantin origjinal (kurrë fshirje). Enterprise: reversal grant-i aplikohet si debit ≤ available (mungesa ⇒ zbatim i pjesshëm + gjetje rakordimi, kurrë negativ). Refund mesazhi (kthim pas capture) lidhet me hold-in dhe ≤ shumës së kapur.
O. **Monedha:** ISO-4217 3 shkronja; një monedhë për wallet; pa FX (kurrë konvertim implicit).
P. **Shumë monedha për Enterprise:** teknikisht po (wallet për monedhë, sot); praktikisht një plan ⇒ një rate card ⇒ një monedhë. Rekomandim: një monedhë operacionale për EnterpriseProduct në V1.
Q. **Kosto provider ≠ çmim klienti:** sot s'ka kosto provider fare; shtohet më vonë si fusha opsionale/additive në usage report (jo në rrugën kritike).
R. **Rakordimi:** raporte kumulative nga Enterprise (§11) kundrejt ledger-it Central.
S. **Central i padisponueshëm:** grant-et e sinkronizuara shpenzohen; s'krijohen para; poller-i dështon pa efekt në ledger; alarm për "sync i vjetër".
T. **Parandalimi i debit të dyfishtë:** UNIQUE `(wallet,idempotency_key)`; UNIQUE `(wallet,reference)` te holds; state machine me kyç rreshti; `capture:{hold_id}`; CHECK `*_after >= 0`; rollback i savepoint; (M9) UNIQUE grant_id.

## 7. Modeli kanonik Central (propozim)
Tabela (migrim Central 0017+, NUMERIC(20,6), pa float):
- `credit_accounts(id, enterprise_id FK, currency CHAR(3), status active|frozen, created_at)` UNIQUE(enterprise_id,currency).
- `payments(id, enterprise_id, account_id, amount>0, currency, method cash|bank|gateway, status pending|approved|rejected|reversed, external_ref UNIQUE(provider,ref), created_by, approved_by (≠created_by, përveç rolit të lartë), approved_at, rejected_reason)`.
- `commercial_ledger(id UUID, seq BIGINT i vetëm (si `sync_sequence`), account_id, entry_type grant|grant_reversal|adjustment_credit|adjustment_debit, amount (>0), currency, source_type, source_id, idempotency_key UNIQUE(account_id,key), actor_user_id | actor_label, reason NOT NULL për rregullime, created_at)` — **e pandryshueshme** (ORM guard + trigger PG); UNIQUE(source_type,source_id) ⇒ **një pagesë ⇒ maksimumi një grant**. Total i autorizuar = Σ(grant + adjustment_credit − grant_reversal − adjustment_debit).
- `reconciliation_reports` (raporte të pranuara nga Enterprise, të pandryshueshme) dhe `reconciliation_findings` (të llogaritura).
Pagesa e miratuar ⇒ në të njëjtin tx një hyrje `grant` (id = `grant_id` i qëndrueshëm). Refuzim pas grant ⇒ `grant_reversal` eksplicit (kurrë fshirje). Pa API publik për para (regjistrimi s'prek paranë).

## 8. Modeli operacional Enterprise (ndryshime minimale mbi atë që ekziston)
Mbahen wallet/ledger/hold. Shtohen: `EntryType.GRANT`, `GRANT_REVERSAL` (ref `central_grant`/`grant_id`, çelës `grant:{grant_id}` ⇒ replay pa para të dyfishta); kursor `sms_money_cursor` (singleton, epoch/seq si `sms_cp_cursor`); `SMS_MONEY_AUTHORITY=local|central` (default `local`): në `central` mbyllen rrugët që minton lokalisht (`confirm_topup`, `payments._apply`→top-up, `adjustment` pozitive, `refund` pa hold); wallet krijohet/zgjidhet me `(enterprise_id,currency)`; raportues përdorimi (§11). Floor: balanca nuk bie nën 0 (sot e detyruar; mbetet; **pa overdraft/postpaid** — model eksplicit më vonë).

## 9. Modeli i grant-it
`Central pagesë e miratuar → commercial_ledger (grant, grant_id, seq) → feed i pandryshueshëm → Enterprise e aplikon idempotent (GRANT, available += amount) → shpenzim lokal`. Enterprise **nuk merr kurrë "balancë përfundimtare"**; vetëm grant/reversal. Replay: i njëjti `grant_id` ⇒ no-op (UNIQUE). Reversal > available ⇒ aplikim i pjesshëm + finding. **Pa mint gjatë outage:** pa poll s'ka grant; para ekzistuese shpenzohen; pagesë pending s'është e përdorshme; do të provohet me teste (kursor i bllokuar, Central i fikur, poller-i i dështuar).

## 10. Autoriteti i çmimeve + snapshot
Central: `pricebooks` (version i pandryshueshëm, `effective_from`, rates sipas prefiksi/operatori, monedhë) të lidhur me EnterpriseProduct (jo `Plan`/`AccountPlan`). Enterprise merr **kopje të pandryshueshme versioni** (kontratë `cp.pricing` e ndarë) në `sms_rate_cards/versions/rates` ekzistuese (lexim lokal, pa thirrje sinkrone në çdo dërgim); `Message` ruan tashmë `unit_price, total_price, currency, rate_version_id, rate_id` ⇒ historia s'rillogaritet me çmim të sotëm. Pa version efektiv ⇒ `NoRate` (fail-safe). Migrim: bootstrap i rate card-eve ekzistuese në Central (si M7-f), pa ndryshuar çmimet.

## 11. Rakordimi (report-first, pa korrigjim automatik)
Enterprise → Central (push, kredenciale shërbimi, scope `money:report`, idempotent sipas `(account, watermark)`): për çdo wallet kumulative `{grants_applied(Σ,count,max_seq), reserved_open, captured, released, refunded, adjustments±, available, held, last_ledger_id, report_at}`. Central krahason me `commercial_ledger` dhe raporton: **grant që mungon** (seq>kursor+SLO), **grant i dyfishtë/ i tepërt** (aplikuar > autorizuar), **kredi e pashpjeguar** (TOPUP/adjustment pozitiv/refund jo-grant në modalitet central), **invariant negativ** (`available/held<0`, Σholds≠held), **mospërputhje totalesh**, **raport i vjetër**. Ekuacioni kontrollues: `grants_applied + adj_credit + refunds − captured − adj_debit = available + held`. CLI/admin vetëm-lexim; korrigjimi vetëm me hyrje eksplicite (grant_reversal/adjustment).

## 12. Transporti / kontrata
**Jo cp.v1 state.** cp.v1 është gjendje "revision më e fundit fiton"; paratë janë ngjarje additive ⇒ kontratë e re **`cp.money.v1`** (paketë `packages/contracts/`, stdlib-only, golden) me: pull `GET /internal/money/v1/ledger?after_seq&limit` (ngjarje të pandryshueshme, `next_seq`, epoch, filtrim sipas `service_client_enterprises` + `auth_generation` si M7) dhe push `POST /internal/money/v1/usage-reports`. Autentikim = stack-u ekzistues (Ed25519, jti, scopes të reja `money:read`/`money:report`), idempotencë, audit, validim strikt. Pa `balance=` te `EnterpriseProductState`. Pricing: kontratë e ndarë `cp.pricing.v1` (version snapshots).

## 13. Invariantet (DB / shërbim)
1. një operacion i faturueshëm s'debiton dy herë — UNIQUE `(wallet,idempotency_key)`, UNIQUE holds `(wallet,reference)` ✔ ekziston; 2. i njëjti grant s'aplikohet dy herë — UNIQUE `grant:{id}` (M9); 3. `captured ≤ held` — `capture` refuzon `final > hold.amount` ✔; 4. release ≤ rezervim i hapur — `_post` + status hold ✔; 5. balanca nën floor — CHECK `*_after >= 0` + `InsufficientFunds` ✔; 6. monedha s'ndryshon — hold/ledger në wallet të një monedhe; Central CHECK `currency = account.currency` (M9); 7. ledger i pandryshueshëm — ORM + trigger PG ✔ (+Central); 8. `amount > 0` — CHECK holds/topups/payments ✔ (+Central ledger); 9. rollback ruan paratë — savepoint/tx ✔ (testuar); 10. retry provider s'dyfishon — një hold, capture një herë ✔; (M9) **verifikim `held_after = Σ holds ACTIVE`** në reconcile dhe `verify_wallet`.

## 14. Faza (rendi i rishikuar nga auditi)
Ledger-i operacional Enterprise **ekziston dhe është i fortë**, kështu M9-c e kërkuar zvogëlohet; rendi kritik është **S1/E1 para çdo paraje reale**.
- **M9-a — S1/E1** (gate): `UNKNOWN`, sweeper, `idempotent_by_reference`, veprime `queue:resolve`, alarm, teste crash. *(Enterprise)*
- **M9-b — Central commercial ledger + pagesa + grant-e** (pa Enterprise). *(Central)*
- **M9-c — `cp.money.v1` + grant applier + `SMS_MONEY_AUTHORITY` + mbyllja e minting lokal** (provat outage/no-mint/replay). *(të dy)*
- **M9-d — Usage reports + rakordim (Central, report-first).**
- **M9-e — Pricing authority + snapshots (`cp.pricing.v1`) + bootstrap.**
- **M9-f — Admin APIs/readiness/hardening** (readiness parash si M7-f; gate prodhimi).
(Ndryshim nga propozimi: sync+Enterprise ledger bashkohen në M9-c; S1/E1 bëhet M9-a.)

## 15. Slice-i i parë: M9-a (S1/E1)
Skedarë: `app/models/sending.py` (`UNKNOWN`, tranzicionet), `app/models/email.py` (po ashtu), `app/providers/{base,fake,http,twilio}.py` (`idempotent_by_reference`), `app/services/messages.py`+`emails.py` (`recover_stuck`→UNKNOWN, rrugët W5/W6, `resolve`), `app/worker.py` (sweep), `app/api/admin.py` (lista + `POST /admin/queue/{kind}/{id}/resolve`, `queue:resolve`, audit), konfig `SMS_SENDING_LEASE_SECONDS` (default 600), migrim vetëm nëse nevojitet (statuset janë string pa CHECK), docs, teste: crash W2–W6 SMS+email, PG `pg_terminate_backend`, invariant parash, idempotencë e veprimeve, RBAC. **Pa ndryshim semantike capture/release ekzistuese.**

## 16. Vendime që kërkoj nga ti
1. Capture në **dorëzim (DLR)** (rekomandim, sot) vs në **pranim provider (SENT)**. 2. `UNKNOWN`: para të mbajtura deri në veprim njeriu (rekomandim) vs auto-release pas N ditësh. 3. Një monedhë për Enterprise/produkt në V1. 4. Pozitive lokale e ndaluar nën autoritet Central (rekomandim). 5. Email jashtë wallet në M9. 6. Dëshmi idempotence reale e provider-it (Twilio s'ka) — pa të, pa ridërgim automatik.

---

## M9-a (zbatuar) — S1/E1: besueshmëria e rezultatit të provider-it
Vendimet finale të miratuara: capture mbetet në DLR `delivered` · `UNKNOWN` mban hold-in, pa auto-release · V1 një monedhë/enterprise-produkt, pa FX · nën `SMS_MONEY_AUTHORITY=central` mint pozitiv lokal ndalohet (M9-c) · email jashtë wallet (rishikim te M9-e) · pa retry automatik pas rezultati të paqartë përveç provider-it me idempotencë të provuar · `idempotent_by_reference` default FALSE.

**Skema (migrim Enterprise 0022, aditiv):** `sms_messages.dispatch_started_at`, `sms_emails.dispatch_started_at` (nullable). `unknown` është vlerë e re stringu në kolonën ekzistuese të statusit (Enum jo-native pa CHECK): s'ka ndryshim skeme; rreshtat historikë s'migrohen (kolona e re = NULL; vetëm SENDING aktiv është kandidat).
**Kuptimi i `UNKNOWN`:** rezultati i dërgimit është i panjohur dhe i pasigurt për retry — NUK është sukses i provider-it dhe NUK është dështim. Terminal për automatizim: pa ridërgim, pa release, pa capture; hold-i mbetet ACTIVE (available i ulur, held i pandryshuar). Daljet e vetme: DLR/ngjarje autoritative ose zgjidhje e stafit. Tranzicionet: `SENDING→UNKNOWN`; `UNKNOWN→DELIVERED|FAILED` (SMS); `UNKNOWN→SENT|FAILED|DELIVERED|BOUNCED|COMPLAINED` (email: zgjidhje e stafit ose ngjarje e provider-it). **Klienti nuk e sheh kurrë "unknown"**: statusi publik/console/historiku/fushatat tregojnë `sending` (pa kod të brendshëm).
**Faza e thirrjes (metadata minimale):** `COMMIT#1` claim (SENDING, `attempts+1`) → ndërtimi i kërkesës/MIME → `COMMIT#1b` vendos `dispatch_started_at` → provider (pa tx) → finalizim me `FOR UPDATE` që kontrollon se claim-i (status SENDING + i njëjti `attempts`) është ende yni → `COMMIT#2`. Kosto: një UPDATE+commit shtesë për dërgim; asnjë kërkim sinkron te provider/Central në rrugën e nxehtë. `dispatch_started_at` zbrazet në çdo claim/riradhitje të re (të përpjekjes).
**Provider capability:** `idempotent_by_reference` (default FALSE; kontroll në memorie `is_idempotent`). Fake SMS/email = TRUE (provuar nga kodi: `accepted[reference]` + testet); **HttpProvider = FALSE** (reference-ja `public_id` është e qëndrueshme ndër riprova, por s'ka kontratë/dokument/test që provon dedup te vendori); **TwilioProvider = FALSE** (s'ka çelës idempotence për Messages); **SmtpEmailProvider = FALSE**. `ProviderError.ambiguous`: True kur kërkesa mund të ketë mbërritur — HTTP: read timeout/reset pas lidhjes, 5xx, 2xx i palexueshëm; Twilio: `twilio_outcome_unknown` (timeout pas lidhjes, 500, 2xx pa sid); SMTP: gabim pas lidhjes. Definitive (jo të paqarta): 429/408/425, 4xx, lidhja e pavendosur, Twilio 429/502/503/504/401/403.
**Rregullat e retry:** gabim definitiv ⇒ sjellja e vjetër (retry i përkohshëm / FAILED+release). Rezultat i paqartë (ose përjashtim i papritur PAS `dispatch_started_at`) ⇒ provider idempotent DHE `attempts<max` ⇒ retry me të NJËJTËN `reference` (`public_id`); përndryshe (përfshirë mbarimin e riprovave) ⇒ `UNKNOWN`. Përjashtim PARA marker-it (regjistri, `SendRequest`, MIME) ⇒ retry i sigurt (provider-i s'u thirr). Zëvendëson sjelljen e vjetër W5/W6.
**Sweeper (`recover_stuck`, SMS+email; worker çdo 60s; `SMS_SENDING_LEASE_SECONDS`=600, min 60):** SENDING me `updated_at` mbi lease, `FOR UPDATE SKIP LOCKED` (dy përjashtime të reja në allowlist), i kufizuar. Klasifikim: **A** `dispatch_started_at IS NULL` ⇒ provider-i definitivisht s'u thirr ⇒ rirradhitje (pas `MAX_ATTEMPTS`: FAILED+release `recovery_exhausted` për provider jo-idempotent; idempotent ⇒ UNKNOWN); **B** thirrja mund të ketë nisur + provider idempotent + `attempts<max` ⇒ rirradhitje (e njëjta reference); **C** çdo rast tjetër ⇒ `UNKNOWN` (`stuck_sending`). Idempotent (rreshti i kaluar s'zgjidhet më); i freskët i paprekur. `expire_stale` (72h) prek VETËM SENT: UNKNOWN përjashtohet nga auto-release (invariant i vështirë, i provuar).
**Crash SMS (W1–W7):** W1 para COMMIT#1 ⇒ QUEUED (rollback). W2 pas #1/para marker ⇒ SENDING pa marker ⇒ sweeper A ⇒ rirradhitje (provuar jo-UNKNOWN). W3 marker i commit-uar + thirrje + vdekje ⇒ SENDING+marker ⇒ UNKNOWN. W4 provider pranoi, COMMIT#2 dështon ⇒ SENDING pa id ⇒ UNKNOWN; asnjë ridërgim (provider jo-idempotent: 1 thirrje; idempotent: rirradhitje me të njëjtën reference, `accepted` 1). W5 timeout i paqartë ⇒ UNKNOWN (ose retry me reference nëse idempotent). W6 përjashtim pas thirrjes ⇒ UNKNOWN. W7 DLR para COMMIT#2 ⇒ 503 (provider riprovon) ⇒ pas UNKNOWN zgjidhet me id/reference. **Worker i ngadaltë** që humbi claim-in te sweeper-i: finalizimi s'mbishkruan UNKNOWN; nëse provider-i ktheu id, ruhet (`late_ack`) që DLR ta mbyllë vetë.
**Crash email:** e njëjta logjikë (pa para): A ⇒ rirradhitje; ambiguous/stuck ⇒ UNKNOWN; provider idempotent ⇒ retry me reference. Zgjidhje: `confirmed_sent` ⇒ SENT (jo "delivered": modeli ka SENT/DELIVERED nga ngjarje) ose `not_sent` ⇒ FAILED; ngjarje e provider-it (delivered/bounce/complaint) për UNKNOWN me id e zgjidh vetë (audit `system:email_event_reconciliation`). Asnjë hyrje wallet.
**Hold/capture/release:** UNKNOWN s'prek paratë. Zgjidhja përdor VETËM `wallets.capture/release` ekzistues (çelësa unikë `capture:{hold}`/`release:{hold}`) ⇒ asnjë debit arbitrar, asnjë dyfishim.
**DLR pas UNKNOWN:** delivered ⇒ DELIVERED + capture një herë; failed final ⇒ FAILED + release një herë; audit i sistemit `system:dlr_reconciliation` (`message.unknown_auto_resolve`, burimi `dlr`). Lidhja: me `provider_message_id` të ruajtur, ose me `reference`=`public_id` (opsionale në `/webhooks/dlr/{provider}`; vetëm për të njëjtin provider dhe vetëm për UNKNOWN); id e ndryshme nga e ruajtura ⇒ Conflict (kurrë mbishkrim). Dublikat ⇒ idempotent; kontradiktor ⇒ Conflict+receipt.
**Zgjidhja manuale:** `resolve_unknown(db, public_id, outcome, actor, role, reason, provider_message_id?)`: SMS `billable_delivered` (capture) | `non_billable_failed` (release); email `confirmed_sent` | `not_sent`. Vetëm UNKNOWN; replay me të njëjtin rezultat = no-op (pa audit/lëvizje të dytë); rezultat kontradiktor pas finalizimit = Conflict; arsye e detyrueshme (1..500, pa karaktere kontrolli); `attach_provider_message_id` (pa lëvizje parash). API (vetëm admin, jo publik): `GET /v1/admin/queue/unknown`, `POST /v1/admin/queue/{sms|email}/{id}/resolve|provider-id`; leja e re `queue:resolve` = vetëm `superadmin` (+ hap i dytë TOTP si veprimet e tjera të ndjeshme); finance/support/pricing/approver/client ⇒ 403. **Audit** (`message.unknown_resolve`/`email.unknown_resolve`, aktor+rol, `previous_state`, rezultati, `hold_id`/shuma/monedha, provider/id, arsyeja, kohë) në të njëjtin transaksion me lëvizjen e parave (provuar: dështimi i audit-it rikthen gjithçka).
**Dukshmëria:** `/v1/admin/queue/unknown` (listë + përmbledhje: numri, mosha, shuma e mbajtur sipas monedhës, provider, përpjekja e fundit), `/v1/admin/stats.unknown_outcome`, `/v1/admin/providers.unknown_outcome`; worker logon `recovered stuck …`. **Readiness:** `python -m scripts.queue_readiness [--warn-seconds 3600] [--fail-seconds 86400] [--strict] [--json]` (vetëm-lexim): FAIL nëse SENDING>2×lease (sweeper s'punon), WARN/FAIL për UNKNOWN të vjetër, raporton capability-t e provider-ave. Para parave reale në prodhim ky kontroll hyn në gate.
**Fushatat:** përdorin `msg.submit` + të njëjtin `process_one` (nuk ka rrugë tjetër drejt provider-it: guard AST që `get_provider/get_email_provider/.send` jetojnë vetëm te `process_one`/`recover_stuck`); statistikat e fushatës tregojnë `sending` për UNKNOWN dhe e llogarisin në `in_flight` (hold-i mbetet i rezervuar).
**Borxh/prova që mungojnë:** (1) asnjë provider real nuk është provuar idempotent ⇒ pa ridërgim automatik në prodhim; (2) Twilio nuk është testuar kundër Twilio real; (3) HttpProvider: vendori real duhet të konfirmojë dedup me `reference` para se të vendoset TRUE; (4) DLR-ja e Twilio s'kthen `reference` ⇒ UNKNOWN i Twilio zgjidhet vetëm me id nga paneli (attach) ose zgjidhje manuale; (5) `GET /v1/admin/messages/unresolved` (legacy, FAILED me `outcome_unknown`) mbetet për rreshtat historikë.

---

## M9-b (zbatuar) — autoriteti tregtar në Central (ledger + pagesa + grant-e)
**Vetëm Central.** Asnjë ndryshim te Enterprise (migrimi 0022 mbetet kokë; wallet/top-up/pagesa lokale operacionale), asnjë `cp.money.v1`, asnjë `SMS_MONEY_AUTHORITY`, asnjë API HTTP, asnjë gateway pagese, asnjë çmim. Grant-et krijohen në Central por NUK sinkronizohen (M9-c).
**Skema (Central 0017):** `credit_accounts`, `money_sequence`, `commercial_ledger_entries`, `payments`, `credit_grants`, `money_events` (NUMERIC(20,6), kurrë float; një prerje e vetme me Enterprise `MONEY`).
**Formula kanonike (e vetmja; `commercial_ledger.totals`):**
`funds = Σ payment_credit + Σ manual_credit_adjustment − Σ manual_debit_adjustment` · `outstanding_grants = Σ grant_issued − Σ grant_reversal` · **`available_to_grant = funds − outstanding_grants`** (invariant ≥ 0). Pagesë 100 + grant 40 ⇒ funds=100, outstanding=40, available=60 (**jo 140**): pagesa krijon fonde tregtare një herë; granti vetëm i alokon. Shumat janë pozitive (`amount > 0`), drejtimi vjen nga `entry_type` (stil i vetëm, i dokumentuar). Asnjë kolonë balance e ndryshueshme (provuar).
**Llogaria & monedha (V1, pa FX):** një llogari për (enterprise, produkt); `UNIQUE(enterprise_id, product_id)` në DB ⇒ një monedhë (monedhë e dytë = Conflict në shërbim + IntegrityError me SQL). Monedha ISO-4217 3 shkronja e mëdha (CHECK portativ; s'ka enum ekzistues në kod për t'u ripërdorur: Enterprise përdor `String(3)`). Monedha është e pandryshueshme: ORM guard + FK të përbëra `(account_id, currency)` te ledger/pagesa/grant + trigger PG (provuar me SQL të drejtpërdrejtë). Pezullim: bllokon pagesa të reja/miratim, grant-e dhe rregullime; reversal dhe refuzim lejohen.
**Pagesa:** `pending → approved | rejected`. Miratimi (atomik): kyç `money_sequence` → llogari → pagesë; valido pending + maker-checker + llogari aktive; approved; **një** `payment_credit` (UNIQUE `(entry_type, source_type, source_id)`); audit njeriu — çdo dështim rikthen gjithçka (provuar edhe me audit që dështon). Replay = no-op (pa kredit/audit të dytë); rejected⇒Conflict; refuzimi kërkon arsye; approved nuk refuzohet/fshihet (reversal i ardhshëm eksplicit). **Maker-checker:** miratuesi ≠ krijuesi njeri (shërbim + CHECK DB); krijuesi mund të jetë njeri admin OSE proces sistemi me etiketë `system:<emër>` (kurrë përdorues i rremë); miratuesi është gjithmonë admin njeri. `(source, external_reference)` unik i skopuar (indeks i pjesshëm); e njëjta përmbajtje ⇒ pagesa ekzistuese, ndryshe Conflict. Fushat e parave të pandryshueshme (ORM + trigger PG).
**Grant-et:** `grants.issue` (atomik): sekuencë → llogari → idempotencë → valido (aktive; fonde ≥ shuma, përndryshe `InsufficientFunds`; shuma e saktë kalon) → grant → ledger `grant_issued` → ngjarje `credit_grant.issued` → audit. Idempotencë `(account, idempotency_key)` me `request_hash`: e njëjta përmbajtje ⇒ grant-i ekzistues (edhe pas shterimit të fondeve), ndryshe Conflict. Pa kufizim një-grant-për-pagesë (40+60 nga 100 lejohet; `source_payment_id` informues, duhet approved e njëjta llogari). Grant i pandryshueshëm (ORM + trigger); korrigjim = reversal + grant i ri. Emetuesi: admin njeri ose `system:<emër>`.
**Reversal (njohje tregtare, e brendshme):** `grants.reverse`: active→reversed, arsye e detyrueshme, `grant_reversal` + ngjarje `credit_grant.reversed` + audit; no-op i dytë; rikthen fondet e alokueshme në Central dhe NUK fshin/ndryshon grantin. **Nuk është i plotë operacionalisht:** Enterprise mund t'i ketë shpenzuar tashmë; zbatimi i sigurt (debit ≤ available + gjetje rakordimi) është M9-c/d — prandaj mbetet shërbim pa API.
**Rregullime manuale:** `credit_accounts.adjust` (credit|debit): hyrje ledger (kurrë UPDATE balance), admin njeri, arsye e detyrueshme, `idempotency_key`, audit; debit s'lejohet të rrëzojë `available_to_grant` nën 0.
**Sekuenca/ditari:** `money_sequence` singleton TRANSAKSIONAL (si `sync_sequence`; kyç `FOR UPDATE` deri në commit ⇒ seq N i dukshëm para N+1; rollback heq edhe rritjen ⇒ pa seq fantazmë, pa boshllëk; `epoch` i qëndrueshëm). Çdo hyrje ledger dhe çdo ngjarje merr seq. `money_events` (vetëm grant issued/reversed): ditar i pandryshueshëm me **payload të ngrirë** (pa shënime/arsye të brendshme); UNIQUE `(event_type, entity_id)`. Rendi i kyçjeve: sekuencë → llogari → pagesë/grant (serializon mutacionet e parave; volum administrativ i ulët; kjo i shuan garat e overspend-it).
**Transaksioni:** shërbimet s'bëjnë commit; mutacioni + ledger + ngjarje + audit dalin/zhduken bashkë. **Mbrojtjet e pandryshueshmërisë:** ledger/events ORM + trigger PG `UPDATE/DELETE/TRUNCATE` (SQL i drejtpërdrejtë refuzohet); llogari/pagesë/grant: fusha të ngrira + statusi përfundimtar + pa DELETE (ORM + trigger PG).
**Audit:** `credit_account.create|status_change`, `payment.create|approve|reject`, `credit_grant.create|reverse`, `credit_adjustment.create`, `debit_adjustment.create`; aktor njeri ose `system:<emër>`.
**Mbetet te M9-c:** `cp.money.v1` (feed i `money_events` + scope-e shërbimi), aplikuesi i grant-it te Enterprise (`GRANT`/`GRANT_REVERSAL` në ledger lokal, kursor, idempotencë `grant:{id}`), `SMS_MONEY_AUTHORITY`, mbyllja e minting lokal pozitiv, reversal operacional, provat e outage/no-mint/replay. Pa API admin (M9-f), pa rakordim (M9-d), pa çmime (M9-e).

## M9-c (zbatuar) — `cp.money.v1` + aplikuesi i grant-eve + `SMS_MONEY_AUTHORITY`

**Rrjedha e vetme e autoritetit:** Central `money_events` → feed i autentikuar `GET /internal/money/changes`
(`cp.money.v1`, scope `money:read`, autorizim per enterprise) → consumer Enterprise (`--role money_control_plane`)
→ `sms_money_grants` + rresht ledger `GRANT` (`grant:<uuid>`, UNIQUE sipas wallet+key) → wallet-i lokal i
shpenzueshëm. **Nuk ka thirrje të sinkronizuar drejt Central në rrugën e dërgimit** (provuar me test AST + test me rrjetin
të bllokuar). Central jashtë funksionit ⇒ kredia e sinkronizuar mbetet e shpenzueshme, s'ka kredi të re, asgjë s'çaktivizohet.

### Formulat dhe problemi i migrimit (vendimi i miratuar)
`available = available_after` i rreshtit të fundit; `held = Σ held_delta` (= Σ ACTIVE holds); `gross = available + held`.
Një Enterprise ekzistues ka p.sh. 1000 + 200 të krijuara lokalisht. Një grant "fillestar" i postuar si kredi normale do ta
dyfishonte (2400). Zgjidhja = **baseline-match i pandryshueshëm** (Opsioni A): baseline regjistron autorizimin e parasë që
ekziston, jo një wallet të dytë.

1. **Baseline** (`scripts.money_authority baseline-create`, kërkon `SMS_MONEY_AUTHORITY=shadow`): `sms_money_baselines`
   ruan `available/held/gross_at_cutover`, `ledger_max_id`, wallet, enterprise, monedhë, produkt SMS, `created_at/by` dhe
   `baseline_ref` = SHA-256 i JSON-it kanonik të këtyre fushave (rillogaritet nga readiness). Fushat financiare janë të
   pandryshueshme (guard ORM + trigger PG; `gross = available + held` si CHECK); vetëm `status` (active→superseded).
   Gross përfshin holds aktive; baseline-i **nuk** krahasohet me bilancin aktual (trafiku mund ta ndryshojë).
2. **Grant bootstrap** në Central: `purpose=bootstrap` + `baseline_ref` (fusha të ngrira, jo shënim i lirë; UNIQUE: një
   baseline = një bootstrap). Aplikohet vetëm nëse `amount == baseline.gross_at_cutover`, monedha/produkti/enterprise/wallet
   përputhen, baseline-i është aktiv me hash të vlefshëm, s'ka bootstrap tjetër të përputhur dhe **s'ka mint pozitiv lokal pas
   baseline-it** (provuar nga ledger-i: `id > ledger_max_id`, rritje neto > 0, tip ≠ GRANT). Rezultati: `matched_to_existing_balance`,
   rresht `GRANT` me **delta 0** — asnjë rritje bilanci. Çdo mospërputhje ⇒ `baseline_mismatch`, pa mutacion, pa fallback në
   GRANT normal; kursori përparon vetëm pas regjistrimit durabël; readiness dështon.
3. **Modalitetet:** `local` (default, sjellja e sotme; consumer boshe) · `shadow` (mint lokal i ngrirë; grant-et normale
   REGJISTROHEN `deferred_shadow`, s'kreditojnë; fondet ekzistuese shpenzohen normalisht) · `central` (vetëm GRANT i Central krijon
   kredi; `deferred_shadow` kreditohen sipas `issued_seq` në çdo cikël; wallet bosh krijohet për grantin e parë).
   reserve/capture/release/dërgim/rregullim negativ vazhdojnë në të gjitha.
4. **Porta e mint-it:** `wallet.check_posting` (thirret nga `_post` dhe nga guard-i ORM `before_insert` i `LedgerEntry`):
   çdo rresht me rritje neto (available+held) > 0 bllokohet kur authority ≠ local (përveç GRANT autoritativ nën `central`);
   GRANT/GRANT_REVERSAL postohen vetëm nga `money_sync` (`authoritative=True`). **`wallet.refund` u kufizua:** merr `hold_id`,
   vetëm mbi hold të kapur, total ≤ shumës së kapur, idempotent (nuk është më burim kredie arbitrare) dhe plotësisht i bllokuar
   nën shadow/central. `payments._apply` dështon me `money_authority_frozen` (pagesa → FAILED, rakordim manual).
   **Wallet-i është SMS-only nën shadow/central:** `billing.pay_from_wallet`/auto-pay bllokohen (faturat përmbajnë tarifë plani
   + email overage ⇒ pa provenance produkti); faturat paguhen online.
5. **Mapimi (pa emra/FX):** `(enterprise_id, product_id, currency)` → wallet `(owner_ref, currency)` vetëm nëse `product_id` është
   produkti i VETËM me kanal `sms` i enterprise-it (entitlements cp.v1, jo `withdrawn`). Zero/shumë produkte SMS, produkt
   email, enterprise i panjohur ⇒ `unmapped` (regjistrohet, rivlerësohet kur mapimi bëhet i vlefshëm, bllokon readiness).
6. **Reversal konservativ:** i regjistruar para kredisë (`deferred/mismatch/unmapped`) ⇒ `voided_before_apply`; i aplikuar ⇒ debit
   `GRANT_REVERSAL` vetëm nëse `available ≥ shuma` (holds aktive s'preken); përndryshe `reconciliation_required` (pa mutacion,
   kurrë negativ, kursori përparon pas regjistrimit). Pa provenance lot/FIFO. Zgjidhja finale e rakordimit është M9-d.
7. **Kursori/protokolli:** `sms_money_cursor` (epoch, generation, last_seq, last_success_at, last_error). Faqja = një transaksion
   (kursor→ngjarje idempotente sipas `grant_id`+`event_id`+hash→kursor). Ngjarje e palexueshme/në konflikt ⇒ kursori ndalet para saj
   (prefiksi i mirë aplikohet), `last_error`, alarm. `money_authorization_changed` ⇒ rebase dhe riprodhim nga 0 (idempotent);
   `money_epoch_mismatch`/`cursor_ahead` ⇒ veprim operatori (`reset-cursor --ack-replay`). Nuk ka snapshot parash.
8. **Readiness** (`python -m scripts.money_authority_readiness [--json]`, vetëm lexim, kodi 1 me FAIL): mode, consumer i konfiguruar,
   kursor i shëndetshëm (≤ 15 min, pa gabim), baseline per wallet ekzistues, hash valid, bootstrap i përputhur, **asnjë mint pozitiv
   pas baseline-it**, asnjë rresht GRANT jetim, held = Σ holds aktive, ledger = Σ delta, bilanc jo-negativ, mapim i qartë, asnjë
   mismatch/unmapped/reversal i pazgjidhur, `queue_readiness` pa FAIL, ACK në prodhim (`SMS_MONEY_AUTHORITY_ACK`; vendoset
   kur readiness del PASS në shadow, para kalimit në `central`). Prodhimi `central` pa ACK ⇒ aplikacioni refuzon të nisë.

### Runbook cutover (një enterprise me wallet ekzistues)
1. Central: kredito llogarinë (pagesë ose rregullim manual i miratuar me maker-checker) për të paktën `gross`. Central nuk mutoi asgjë te Enterprise.
2. Central: kredencial me scope `money:read` (`create_service_credential --scope money:read`) dhe autorizim per enterprise. Enterprise: `SMS_MONEY_AUTHORITY=shadow`, rinis web+worker; nis `money-sync` (`--role money_control_plane`).
3. Parakusht: sinkronizimi cp.v1 (M7) ka aplikuar entitlement-et SMS të enterprise-it (mapimi i produktit lexon `sms_entitlements`).
   `python -m scripts.money_authority baseline-create --wallet-id N --by <operator>` → jep `baseline_ref` te stafi Central.
4. Central: `grants.issue(account, gross, purpose='bootstrap', baseline_ref=...)`; pritet `matched_to_existing_balance`.
5. `python -m scripts.money_authority_readiness` ⇒ PASS (në prodhim: vendos `SMS_MONEY_AUTHORITY_ACK=true`).
6. `SMS_MONEY_AUTHORITY=central`; rinis. Grant-et e regjistruara kreditohen sipas rendit.
**Rollback emergjent central→local:** vetëm konfigurim; ledger-i i pandryshueshëm dhe historia e grant-eve mbeten (s'fshihen).
Pas rikthimit mint-i lokal rihapet — çdo mint lokal i mëvonshëm do ta dështojë readiness-in te cutover i radhës (kërkon baseline të ri).

### Çfarë NUK bën M9-c
Pa rakordim të plotë (M9-d), pa çmime/FX (M9-e), pa API/UI klienti për para, pa ndryshim të semantikës së ngarkimit SMS (M9-a).
Borxhe: `sms_money_grants` s'ka retention; grant-et në `reconciliation_required` zgjidhen manualisht deri te M9-d.

## M9-d (zbatuar) — raportimi i përdorimit + rakordimi financiar (vetëm detektim/raportim)

**Parim:** Central provon *çfarë ka autorizuar* kundrejt *çfarë ka marrë Enterprise* kundrejt *çfarë mban/shpenzon/rezervon
Enterprise*, pa krijuar të vërtetë të dytë. **Asnjë korrigjim automatik**: asnjë `wallet ±=` diferencë, asnjë replay grant-i,
asnjë reset kursori; operatori vepron eksplicit. Rakordimi Central është vetëm-lexim (provuar me test AST + numërim rreshtash).

### 1. Skema e raportit (`cp.money.usage.v1`, `packages/contracts/control_plane/money/usage_v1.py`)
Kumulativ (rikuperon nga raportet e humbura), stdlib-only, kanonik, golden-e te `tests/golden/control_plane_money_usage/`.
`report_id` (UUID stabil), `report_seq` (monoton lokal per enterprise/product/currency — rendi kryesor, jo koha), `enterprise_id`,
`product_id`, `currency`, `generated_at`, `authority_mode` (local|shadow|central), `ledger_max_id` (watermark), `wallet`
{available, held, gross, active_hold_total, active_hold_count}, `baseline` {baseline_ref, gross_at_cutover, ledger_max_id, status}|null,
`flows` (PAS baseline-it), `integrity`, `cursor` {epoch, last_seq, generation, last_success_at, has_error}, `grants[]` (grant_id, status,
amount, currency, product_id, purpose, baseline_ref, issued_seq, reversed_seq, updated_at, detail; ≤ 5000). Shuma = string me 6 shifra
(kurrë float); gjendjet e wallet-it pranojnë shenjë (bilanc negativ duhet të dukët CRITICAL, jo të refuzohet), totalet kumulative ≥ 0.
Vetëm vlera të rillogaritshme nga ledger-i i pandryshueshëm dhe tabelat e parave: nuk raportohet asgjë që s'derivohet.

### 2. Snapshot
`money_usage.build_drafts` lexon NJË transaksion REPEATABLE READ vetëm-lexim (PG) — bilanci, holds, shumat e ledger-it, grant-et, baseline
dhe kursori nga e njëjta pamje. Testuar në PG me trafik që commit-ohet ndërmjet leximeve (+ kontroll negativ: READ COMMITTED përzihet).

### 3. Ekuacioni (nga llojet reale të ledger-it; HOLD/RELEASE janë neto 0 mbi gross)
`gross = baseline_gross + grants_applied − grant_reversals − captured − negative_adjustments − invoice_debits − other_debits +
positive_local_credit` (flukset = rreshtat me `id > baseline.ledger_max_id`; pa baseline: gjithçka). `positive_local_credit` = rritje neto e
çdo lloji ≠ GRANT (TOPUP, REFUND, ADJUSTMENT+); `captured` = Σ CAPTURE; `negative_adjustments` = ADJUSTMENT neto<0; `invoice_debits` = INVOICE;
`other_debits` = çdo debit tjetër (duhet 0). Plus: `held = Σ ACTIVE holds`, `stored = Σ delta ledger` (available dhe held), `orphan_grant_*`
(rreshta GRANT/GRANT_REVERSAL pa rresht `sms_money_grants`).

### 4. Transporti + auth
Outbox i ngushtë `sms_usage_reports` (pending/sending/sent/retry/failed/superseded; lease 120 s; backoff 30 s→15 min; përmbajtja e ngrirë,
guard ORM + trigger PG). Roli i veçantë `--role money_usage_reporter` (cikël `SMS_MONEY_REPORT_INTERVAL_SECONDS`=300; dedup me heartbeat 600 s;
raportet kumulative të vjetra në pritje → `superseded`). **Jo në rrugën e dërgimit.** `POST /internal/money/usage-reports` me Ed25519 +
scope **`money:report`** (nuk pranon `money:read`/`sync:read`); klienti duhet të jetë i autorizuar për enterprise-in e raportit (403 ndryshe).
Përgjigje: 201 stored · 200 duplicate · 409 conflict/watermark · 422 i pavlefshëm · 403. 409/413/422 = dështim PERMANENT (alarm); rrjeti/5xx = retry.

### 5. Ruajtja në Central
`usage_reports` (migrimi 0019): append-only (ORM + trigger PG), UNIQUE(enterprise, product, currency, report_seq), indekse për
(enterprise, generated_at) dhe (enterprise, product, currency, ledger_max_id). **Aktual = report_seq më i madh** (pa gjendje të ndryshueshme):
raport i vjetër që vonohet ruhet por s'bëhet kurrë aktual. Idempotencë: i njëjti `report_id`+payload ⇒ no-op, payload tjetër ⇒ Conflict;
(key, seq) i zënë nga tjetër ⇒ Conflict; `ledger_max_id` s'bie me `report_seq` ⇒ Conflict.

### 6. Kategoritë dhe ashpërsia (`money_reconciliation`)
INFO < WARN < FAIL < CRITICAL. **CRITICAL:** `negative_invariant`, `hold_total_mismatch`, `wallet_formula_mismatch`, `unexplained_positive_credit`
(pas baseline-it në shadow/central ose GRANT jetim), `unexplained_debit` (other_debits/reversal jetim), `baseline_mismatch`, `unexpected_grant`,
`grant_amount|currency|product_mismatch`, `grant_state_mismatch`, `epoch_mismatch`, `cursor_ahead`. **FAIL:** `missing_grant` (kursori e ka kaluar ose
grace i skaduar), `missing_reversal`, `unresolved_reversal` (pas `unresolved_reversal_fail`), `stale_report` (> stale), `cursor_stale` (> fail ose gabim),
`grant_unmapped`, `report_missing` (pas grace). **WARN:** `cursor_behind` brenda grace, `stale_report` (fresh..stale), `unresolved_reversal` i ri,
`baseline_pending`, `grant_deferred_in_central`, `authority_mode_mismatch`, fatura nga wallet nën shadow/central. Pragjet (Central, konfigurueshme `CENTRAL_MONEY_*`):
fresh 600 s, stale 1800 s, lag-grace 900 s, kursor warn/fail 900/3600 s, reversal-fail 3600 s, report-missing-grace 1800 s. Enterprise: `SMS_MONEY_REPORT_FRESH/STALE_SECONDS` 600/1800.
Grant-et lidhen VETËM me `grant_id` (kurrë me shumë); `cursor_behind` vs `missing_grant` vendoset nga seq-i i ngjarjes kundrejt kursorit të raportuar.

### 7. Modalitetet
`local`: informative (mospërputhjet e autoritetit → INFO/WARN; invariantet e wallet-it mbeten të plota). `shadow`: raporti tregon grant-et e marra/deferred,
projeksionin `projected_gross_after_cutover = gross + deferred_total`, mospërputhjet; asnjë mutacion. `central`: dëshmon që s'ka mint lokal, çdo pozitiv vjen nga
bootstrap/grant, ekuacioni mbyllet, kursori i freskët, ngjarjet e pazgjidhura të dukshme (`--strict` ⇒ edhe WARN jep kod 1).

### 8. Reversal i pazbatuar, baseline, grant-e
Reversal `reconciliation_required` del me grant_id, shumën, `reversed_seq`, available/held aktual, arsyen dhe moshën; Central NUK e shënon kurrë të rakorduar. Baseline:
bootstrap Central = `baseline.gross_at_cutover` (+ ref + `matched_to_existing_balance`), **kurrë** kundrejt bilancit aktual. Pa baseline në Enterprise por me bootstrap në Central ⇒
CRITICAL; baseline pa bootstrap ende ⇒ `baseline_pending` WARN.

### 9. Mjetet dhe readiness
`python -m apps.central.tools.money_reconciliation [--enterprise-id U] [--strict] [--json]` (vetëm-lexim; 0/1/2). `GET /internal/money/reconciliation?enterprise_id=`
(scope `money:report`, enterprise i autorizuar) kthen verdiktin. `scripts.money_authority_readiness` shton: `usage_reporting_enabled`, `usage_report_fresh`
(raporti i dorëzuar), `usage_report_delivery` (asnjë i fundit i refuzuar), `usage_report_equation` (ekuacioni + hold për çdo wallet) dhe `central_reconciliation`
(verdikt PASS; FAIL/CRITICAL/pa lidhje ⇒ FAIL; i detyrueshëm në prodhim, `--with-central` kudo; thirret vetëm nga ky CLI, kurrë nga dërgimi). `central` në prodhim kërkon `SMS_MONEY_REPORTING=true`.

### Çfarë mbetet për M9-e
Çmimi/FX dhe snapshot-i i çmimit për pagesën SMS; përdorimi i email-it në para (jashtë wallet-it sot); API/UI klienti për para; retention e `usage_reports`/`sms_usage_reports`
(rreshta çdo ≥10 min; heartbeat+dedup e kufizon); zgjidhja operatore e `reconciliation_required` me provë (M9-d vetëm e raporton); alarm-e/dashboard mbi verdiktin.

## M9-e (zbatuar) — autoriteti i çmimeve në Central + foto e pandryshueshme e çmimit

### Auditi i çmimeve ekzistuese (para kodit)

| Koncept | Pronari (para M9-e) | Model | Shkruesi | Lexuesi | Monedha | Versionimi | Foto historike | Vendimi M9-e |
|---|---|---|---|---|---|---|---|---|
| Tarifa SMS (rate card → version → rate) | Enterprise | `sms_rate_cards/_versions/_rates` (prefiks, operator, çmim, `effective_from`) | admin Enterprise (`services/rates.py`) | `rates.quote` | `RateCard.currency` | draft/active/retired, i pandryshueshëm pas publikimit | `Message.rate_version_id/rate_id`, `unit_price`, `total_price` | Mbetet LEGACY (`local`); Central bëhet autoriteti nën `central` |
| Caktimi i tarifës te llogaria | Enterprise | `sms_account_plans.rate_card_id` | admin | `messages`, `campaigns`, `console` | — | pa histori | — | Zëvendësohet nga `price_assignments` (Central, histori e pandryshueshme) |
| Çmimi mujor + overage email | Enterprise | `sms_plans` (`monthly_fee`, `email_overage_price`) | admin / `billing.create_plan` | `billing` | `Plan.currency` | pa versione | `InvoiceLine.unit_price/amount` | Overage → Central (`email_overage`); tarifa mujore mbetet dyshim M10 |
| Vlerësimi i fushatës | Enterprise | `campaigns.estimate` | — | UI | e llogarisë | — | informativ | Përdor të njëjtin motor (`pricing.quote`) |
| Kosto provider-i | askush | — | — | — | — | — | — | NUK shpikur; `customer_price` ≠ `provider_cost` |

### Modeli kanonik (Central)
`price_books` → `price_versions` (draft→active→retired; një draft për libër) → `price_rules` (UNIQUE(version, channel, prefix, operator); email: prefiks/operator bosh) · `price_assignments` (histori e pandryshueshme, UNIQUE(enterprise, product, effective_from)) · `pricing_sequence` (epoch, revision — rritet në activate/retire/assign). Triggera PG + guard ORM e bëjnë versionin aktiv/retired dhe rregullat e tij të pandryshueshme.

### Precedenca e kërkimit (nga dimensionet REALE: prefiks, operator)
Prefiksi më i gjatë fiton; në barazim, rregulla me operator të saktë mmbi atë pa operator. Pa rregull ⇒ `NoRate` (fail-closed). Version i retired ⇒ fail-closed, pa fallback te i vjetri. Funksionet e pastra (`pick_rule`, `select_version`, `select_assignment`, `candidate_prefixes`, `line_total`) jetojnë në `packages/contracts/control_plane/pricing/v1.py` dhe i ndajnë Central + Enterprise.

### Kontrata dhe sinkronizimi
`cp.pricing.v1` (jo `cp.money.v1`): snapshot i plotë me `snapshot_hash` + `content_hash` për version; `GET /internal/pricing/state|snapshot` (scope `pricing:read`, autorizim për enterprise). Aplikimi është atomik (`pricing_sync.apply_snapshot`), pa version të përzier; epokë/revision të vjetra injorohen; përmbajtje e ndryshuar e versionit ekzistues ⇒ `PricingApplyError`, asgjë s'aktivizohet. Kursor/shëndet i veçantë (`sms_pricing_state`), worker role `pricing_control_plane` (profil `pricing-sync`). Ndërprerje ⇒ përdoret snapshot-i i fundit i plotë; pa snapshot ⇒ fail-closed; vjetërsia vetëm alarmon.

### Modalitetet `SMS_PRICING_AUTHORITY`
`local` (parazgjedhje) · `shadow` (llogarit të dyja, krahason, CHARGE me legacy; klasat: missing_rule, currency_mismatch, unit_price_mismatch, total_mismatch, precedence_mismatch, version_missing → `sms_pricing_comparisons`) · `central` (kërkon `SMS_PRICING_AUTHORITY_ACK=true` në prodhim; mutacionet lokale të tarifave bllokohen me `pricing_authority_frozen`, përveç aplikuesit të sinkronizimit). Pavarur nga `SMS_MONEY_AUTHORITY`.

### Foto në mesazh, rezervim, rrumbullakim, monedha
`Message`: `price_source`, `pricing_book_ref`, `pricing_version_ref`, `pricing_rule_ref`, `currency`, `unit_price`, `segments`, `total_price` — e pandryshueshme (guard ORM `MessagePriceFrozenError`). Reserve/capture/DLR/UNKNOWN përdorin vlerën e ngrirë; DLR nuk bën kërkim çmimi. Segmentimi pandryshuar (`count_segments`). Rrumbullakimi: `total = unit × segments` e saktë në 6 shifra dhjetore (kontekst dhjetor eksplicit, ROUND_HALF_UP); rreshtat e faturës në cent HALF_UP (siç ishte). Pa FX: monedha e çmimit duhet të përputhet me atë të wallet-it, përndryshe `currency_mismatch` (fail-closed).

### Email
Central zotëron çmimin/versionin e email; overage postpaid mbetet jashtë wallet-it SMS; linja e faturës ngrin `pricing_source`, `pricing_version_ref`, sasinë, çmimin, monedhën; faturat e vjetra nuk rillogariten; `pay_from_wallet` mbetet i bllokuar (M9-c). Nëse çmimi Central s'ka ⇒ fatura shtyhet (`CentralPriceError`), nuk përdoret çmim i supozuar.

### Runbook i kalimit (rendi)
1. Money authority në `central` fillimisht (M9-c/d).  2. Pricing mbetet `local`.  3. `python -m scripts.pricing_bootstrap export --out pricing.json` (read-only).  4. Central: `python -m apps.central.tools.pricing_import --proposal pricing.json` (dry-run: exact/conflict/invalid/unmapped), pastaj `--apply --actor-email <admin> --ack-proposal-hash <hash>` (vetëm `exact`).  5. Aktivizo versionin + cakto `price_assignment` në Central.  6. Enterprise: `SMS_PRICING_AUTHORITY=shadow`, nise worker-in `pricing-sync`, mblidh krahasime.  7. `python -m scripts.pricing_authority_readiness` (read-only; PASS duhet, në prodhim kërkon ACK).  8. `SMS_PRICING_AUTHORITY=central` + `SMS_PRICING_AUTHORITY_ACK=true`.  Rikthim: kthe `local` (fotot e mesazheve mbeten të vlefshme).

### Borxhe që mbeten (jo në M9-e)
M9-f: UI admin i çmimeve + hardening/retention i `sms_pricing_comparisons` dhe snapshot-eve të vjetra. M10: tarifa mujore e planit (`monthly_fee`) dhe fatura nga Central. M13: pastrimi i tabelave legacy të tarifave (`sms_rate_*`), kolonat legacy `rate_version_id/rate_id`.

## M9-f (zbatuar) — API admin financiare, operacione, hardening përfundimtar

M9-f mbyll M9: nuk ndryshon semantikën e parave/çmimeve (asnjë bug konkret në M9-a…e që ta kërkonte); e bën nënsistemin
**të administrueshëm, të vëzhgueshëm, të audituar, të gatshëm për prodhim dhe të mbështetshëm operacionalisht**.
Asnjë frontend, asnjë API klienti për para, asnjë M10/M11/M13.

### 1. API admin (Central) — `GET/POST` vetëm; admin = shkrim, operator = lexim; pa DELETE/PUT/PATCH
Përgjigje UI-ready (`items/limit/offset/has_more`, shuma/çmime string me 6 decimale, kohë ISO, asnjë ORM/hash idempotence).
Shumat/çmimet pranohen **vetëm si string dhjetor** (`^(0|[1-9]\d{0,13})(\.\d{1,6})?$`; JSON number/float, `1e3`, `+5`, `" 5"` ⇒ 422);
`extra=forbid`; UUID strikt; monedha `^[A-Z]{3}$`; arsye ≤ 500 shenja pa karaktere kontrolli; çelës idempotence `^[A-Za-z0-9._:-]{8,128}$`.

| Zona | Rruga | Roli | Shënim |
|---|---|---|---|
| Llogari | `GET /admin/money/accounts[/{id}]` | op | + totals (funds, outstanding_grants, available_to_grant) |
| | `POST /admin/money/accounts/{id}/status` | admin | `active`/`suspended` + arsye; no-op ⇒ pa audit |
| | `POST …/adjustments` | admin | `kind credit|debit`, `idempotency_key` i detyrueshëm; debit s'e çon `available_to_grant` < 0 |
| | `GET …/ledger?after_seq&limit` | op | vetëm lexim, `next_after_seq` |
| Pagesa | `GET/POST /admin/money/payments`, `GET /{id}`, `POST /{id}/approve|reject` | op/admin | maker-checker (shërbim + CHECK DB); `(source, external_reference)` unik; i miratuar s'refuzohet/s'ndryshohet |
| Grant-e | `GET/POST /admin/money/grants`, `GET /{id}`, `POST /{id}/reverse` | op/admin | vetëm `standard` (bootstrap vetëm nga mjeti i cutover-it); `idempotency_key` eksplicit |
| Rakordim | `GET /admin/money/reconciliation?min_severity` | op | verdikti aktual (vetëm lexim) |
| Raporte | `GET /admin/money/usage-reports`, `/history`, `/{report_id}` | op | aktuali / historia / payload i plotë |
| Çmime | `/admin/pricing/books`, `/books/{id}`, `POST /books/{id}/versions`, `GET /versions/{id}`, `POST /versions/{id}/rules`, `…/rules/remove`, `…/activate`, `…/retire`, `GET/POST /assignments`, `GET /state|/preview|/readiness` | op/admin | versioni aktiv/retired i pandryshueshëm (shërbim + trigger PG); heqja e rregullës nga DRAFT = `POST …/rules/remove` (pa DELETE) |
| Operacione | `GET /admin/financial/overview|alerts|unresolved-reversals|readiness` | op | vetëm lexim |
| Enterprise | `GET /v1/admin/financial` (`monitor:read`) | staf | UNKNOWN, wallet/hold, kursor, reversal-e, outbox, çmime, shadow |

Pa API klienti: asnjë endpoint publik/klienti për top-up, pagesë ose mutacion wallet-i (provuar me test mbi `openapi()`).

### 2. Gate financiar i agreguar
`python -m apps.central.tools.financial_readiness [--json] [--strict] [--no-enterprise-checks] [--enterprise-cwd DIR]` — vetëm lexim (provuar:
zero INSERT/UPDATE/DELETE). Pjesa **Central** (rakordim pa CRITICAL/FAIL, pa mint pozitiv të pashpjeguar, pa reversal të pasigurt, raporte të freskëta,
baseline/cutover, çmim efektiv + monedhë libri = monedhë llogarie, kredenciale shërbimi financiare aktive) dhe pjesa **Enterprise** si PROCESE të veçanta
(Central s'importon `app`): `scripts.queue_readiness`, `scripts.money_authority_readiness`, `scripts.pricing_authority_readiness`, `scripts.financial_ops`
(UNKNOWN backlog, kursor/sinkron, shadow mismatch, ACK-et e prodhimit mbulohen nga dy të parët). Çdo kontroll që nuk ekzekutohet ⇒ **FAIL**
(`--no-enterprise-checks` ⇒ më së shumti WARN "NOT VERIFIED", kurrë PASS). Dalja `PASS|WARN|FAIL`; kodi 0 PASS (WARN pa `--strict`), 1 FAIL (ose WARN me `--strict`), 2 gabim.
**Prodhimi s'është i gatshëm pa `PASS` (rekomandim: `--strict`).** Mjeti nuk ndryshon asnjë konfigurim.

### 3. Alarmet (të vetmet; pa integrim të rremë — lexohen nga mjeti/API/log)
| Nivel | Kodi | Kushti | Pragu (konfig) |
|---|---|---|---|
| CRITICAL | `unexplained_positive_credit` | kredi pozitive lokale pa grant (rakordim) | çdo |
| CRITICAL | `negative_invariant` / `negative_balance` | bilanc negativ | çdo |
| CRITICAL | `wallet_hold_mismatch` | `held ≠ Σ holds ACTIVE` ose formula e ruajtjes e thyer | çdo |
| CRITICAL | `unresolved_reversal` | reversal i pazbatuar | `CENTRAL_MONEY_UNRESOLVED_REVERSAL_FAIL_SECONDS` (3600) / `SMS_FINANCIAL_UNRESOLVED_REVERSAL_CRITICAL_SECONDS` |
| CRITICAL | `money_feed_broken` | mode `central` + kursor pa epokë / me gabim / mosha > prag / grant-reversal mungon | `SMS_FINANCIAL_MONEY_CURSOR_CRITICAL_SECONDS` (3600) |
| CRITICAL | `pricing_missing` | mode `central` + pa snapshot / sinkron me gabim / mosha > fail | `SMS_PRICING_STALE_FAIL_SECONDS` (3600) |
| WARN | `stale_usage_report` | raport mes fresh e stale | `CENTRAL_MONEY_REPORT_FRESH/STALE_SECONDS` (600/1800) |
| WARN | `cursor_lag`, `money_cursor_lag` | kursor mbrapa / i vjetër | `…CURSOR_WARN_SECONDS` (900) |
| WARN | `shadow_pricing_mismatch` | ≥ 1 mospërputhje shadow | — |
| WARN | `unknown_backlog` | UNKNOWN më i vjetër se pragu (me shumën e mbajtur) | `SMS_FINANCIAL_UNKNOWN_WARN_SECONDS` (3600); `queue_readiness` e bën FAIL në 24h |
| WARN | `reconciliation_drift` | diskrepancë WARN/FAIL jo-kritike | — |
| WARN | `stale_pending_payments`, `price_assignment_missing`, `usage_reports_stale`, `pricing_snapshot_stale` | operacion i ngecur / pa çmim / outbox i pa-dërguar / sinkron i vjetër | `CENTRAL_PAYMENT_PENDING_STALE_SECONDS` (172800) … |

### 4. Reversal-et e pazgjidhura — rrjedha e operatorit
`GET /admin/financial/unresolved-reversals` (+ `scripts.financial_ops`): `grant_id`, shuma, monedha, `available`, `held`, `age_seconds`, arsyeja (Central), gjendja Central
(`reversed`+koha) dhe Enterprise (`reconciliation_required`+detail), ashpërsia (WARN→FAIL pas pragut). **Nuk ka "shëno si zgjidhur"** dhe asnjë debit i detyruar negativ:
diskrepanca mbetet e dukshme derisa Enterprise ta raportojë `reversed`. Veprimet e lejuara (secili veprim financiar real, i audituar): (a) shtimi i fondeve në wallet-in
Enterprise nga grant i ri Central (kështu `available ≥ shuma` dhe reversal-i aplikohet në ciklin e radhës); (b) rregullim Central i miratuar (`debit_adjustment`) që e kompenson;
(c) korrigjim operacional i miratuar (p.sh. kalimi i UNKNOWN në outcome përfundimtar që lëshon/kap hold-in). Origjinali mbetet në historinë e raporteve.

### 5. UNKNOWN në operacionet financiare
`UNKNOWN` mban hold ACTIVE ⇒ para e ngrirë dhe e dukshme: `scripts.financial_ops` / `GET /v1/admin/financial` tregojnë numrin, moshën më të vjetër dhe shumën e mbajtur per monedhë;
alarmi `unknown_backlog`; `queue_readiness` hyn në gate. Semantika e M9-a pa ndryshim (kurrë ridërgim/release/capture automatik; zgjidhje vetëm DLR autoritativ ose stafi me audit).

### 6. Mbulimi i audit-it
Test dinamik (`tests/test_m9f_audit_and_credentials.py`): çdo transaksion që prek tabela financiare/autorizimi (Payment, CreditGrant, Ledger, CreditAccount, MoneyEvent, PriceBook/Version/Rule/Assignment,
ServiceClient/Key/Enterprise, CentralUser) përmban rresht audit në të njëjtin commit (≥ 25 transaksione provohen). Veprimet: `credit_account.create|status_change`, `payment.create|approve|reject`,
`credit_grant.create|reverse`, `credit_adjustment.create`, `debit_adjustment.create`, `price_book.create`, `price_version.create|activate|import|retire`, `price_rule.set|remove`, `price_assignment.create`,
`service_client.create|grant|revoke|disable_key|disable_client|enable_auto_grant|disable_auto_grant`, `service_key.add`, `user.create`, `usage_report.retention` (Central);
`money.baseline_create`, `money.cursor_reset`, `money.grant_recorded|grant_credited|grant_reversal_recorded`, `pricing.snapshot_apply`, `message.unknown_resolve`, `email.unknown_resolve`,
`wallet.adjust`, `topup.create|confirm`, `financial.retention` (Enterprise). **Boshllëqe të mbyllura në M9-f:** krijimi i klientit/çelësit të shërbimit, krijimi i admin-it, baseline-i, reset-i i kursorit,
aplikimi i grant-eve/çmimeve nga sinkronizimi. Nuk auditohet qëllimisht: ingestimi i raporteve (evidencë e pandryshueshme me frekuencë të lartë), `pricing_sequence` (pjesë e veprimit të audituar).
Konfigurimi i autoritetit (`SMS_*_AUTHORITY`, ACK) vjen vetëm nga mjedisi — asnjë rrugë runtime nuk e ndryshon (provuar me AST), pra s'ka mutacion për t'u audituar.
Asnjë sekret/JWT/çelës/fjalëkalim në `detail` (provuar me skanim).

### 7. Kredencialet e shërbimit
Scope-t e vetme: `sync:read`, `money:read`, `money:report`, `pricing:read`; çdo endpoint kërkon SAKTËSISHT scope-in e vet (matrica 4×8 e provuar: scope tjetër ⇒ 403), token që pretendon scope që klienti s'e ka ⇒ 403,
lipsë/e prishur/skaduar/replay (`jti`)/nënshkrim i gabuar/`kid`/audience i gabuar ⇒ 401, klient i çaktivizuar dhe çelës i çaktivizuar ⇒ 401, enterprise jashtë autorizimit ⇒ 403 / jashtë snapshot-it,
`auth_generation` rritet vetëm kur ndryshon bashkësia (grant/revoke), konsumatori e sheh. **Pa ngritje scope-i:** asnjë kod nuk cakton `.scopes` pas krijimit; `create_service_credential` refuzon (kod 2) një klient ekzistues me scope të ndryshëm.
Rekomandim operimi: një klient per scope/roli (worker `money_control_plane`, `money_usage_reporter`, `pricing_control_plane`), jo një klient me të gjitha.

### 8. Retention (vetëm operacional, i kufizuar, i audituar, dry-run parazgjedhje)
| Të dhëna | Politika | Mjeti |
|---|---|---|
| Central `usage_reports` | `CENTRAL_USAGE_REPORT_RETENTION_DAYS=0` (parazgjedhje: **pa fshirje**). Me >0: raporti aktual dhe `KEEP_LAST` (20) të fundit per çelës ruhen gjithmonë; ≤ `FULL_DAYS` (30) të gjitha; mes `FULL_DAYS` dhe `RETENTION_DAYS` një per ditë UTC; më i vjetër fshihet | `python -m apps.central.tools.retention [--apply]` |
| Enterprise `sms_pricing_comparisons` | OK > `SMS_PRICING_COMPARISON_OK_DAYS` (90; 0 = jo), mospërputhje > `…MISMATCH_DAYS` (365). Nuk fshihet gjatë `shadow` (dritarja përdoret nga readiness) pa `--include-shadow` | `python -m scripts.financial_retention [--apply]` |
| Enterprise outbox `sms_usage_reports` | `SMS_USAGE_OUTBOX_RETENTION_DAYS=0` (pa fshirje); me >0 vetëm `sent`/`superseded`, jashtë `KEEP_LAST` (50) të fundit | i njëjti |
Fshirja kalon vetëm nga ky kod: trigger-at PG lejojnë DELETE vetëm me `central.retention_delete=on` / `sms.retention_delete=on` brenda transaksionit; UPDATE/TRUNCATE ndalohen gjithmonë.
**NUK fshihen kurrë:** ledger tregtar, grant-e, pagesa, `money_events`, audit financiar, ledger/holds/grants/baseline të Enterprise, mesazhet (foto e çmimit), faturat, versionet/rregullat/snapshot-et e çmimeve (shpjegojnë ngarkesat).
Politika e pa-vendosur ligjore ⇒ dokumentuar, jo fshirë: `audit_log` (Central+Enterprise), `sms_money_grants`, `sms_pricing_snapshots` rriten ngadalë; vendim retention-i ligjor para M13.

### 9. Hot path (SQL për `submit+commit`, PostgreSQL; `process_one` = 10 në të tre)
`local` **21** (identik me para-M9-e) · `central` **25** (= 21 − 3 legacy [card, versione, rate] + 7 lokale [`sms_pricing_state`, enterprise, entitlement, caktim, libër, version, rregulla]) · `shadow` **30** (= 21 + 7 + 1 INSERT krahasimi + 1 flush; përkohësisht).
Zero thirrje rrjeti/Central në asnjë modalitet (httpx + `socket.connect` të bllokuara në test). Në `local` u hoq një SELECT i tepërt (`Rate`) që M9-e kishte shtuar.

### 10. Backup / restore financiar
**Central (burim i së vërtetës për paranë dhe çmimin e klientit):** `money_sequence`, `commercial_ledger_entries`, `credit_accounts`, `payments`, `credit_grants`, `money_events`, `usage_reports`, `price_books|versions|rules|assignments`, `pricing_sequence`, `users`, `audit_log`, `service_*`.
**Enterprise:** `sms_ledger_entries`, `sms_holds`, `sms_wallets`, `sms_money_cursor|baselines|grants`, `sms_usage_reports`, `sms_pricing_state|snapshots|books|versions|rules|assignments|comparisons`, `sms_messages` (foto), `sms_invoices/lines`, `sms_audit_log`.
Çdo bazë: `scripts/backup.sh` (pg_dump custom + checksum) **plus WAL archiving/PITR për Central** (shih 11). Restore vetëm në bazë të re (`scripts/restore.sh`), pastaj verifikim:
Enterprise `python -m scripts.verify_ledger`; Central `python -m apps.central.tools.money_reconciliation --strict` dhe `python -m apps.central.tools.financial_readiness`.
**Rendi i rikuperimit:** (1) Central DB → migrime në kokë (`0021`) → verifiko `usage_reports`/ledger; (2) Enterprise DB → migrime (`0026`) → `verify_ledger`; (3) shërbimet Central; (4) workers Enterprise në rend:
`pricing_control_plane`, `money_control_plane` (kursori), `money_usage_reporter`; (5) `financial_readiness --strict`; (6) hap trafikun. Authority qëndron `shadow` (mint i bllokuar) deri në PASS.

### 11. Semantika e DR (pa reset të heshtur)
| Skenari | Sjellja | Veprimi |
|---|---|---|
| Central i rikthyer MBRAPA kursorit të Enterprise (seq Enterprise > Central) | `cursor_ahead`/`CURSOR_AHEAD` ⇒ kursori ndalet, alarm `money_feed_broken` (central), rakordim | **Grant-et e humbura s'mund të ri-emetohen me të njëjtin ID.** Prandaj Central duhet **PITR/WAL ose replikë sinkrone (RPO≈0) për tabelat e parave**; restore nga dump i natës mbetet bllokues prodhimi (shih blloqet). Nëse ndodh: freeze (shadow), rakordim, vendim financiar i audituar. |
| Epokë e re e Central (bazë e krijuar nga e para) | `money_epoch_mismatch`, kursori ndalet | `scripts.money_authority reset-cursor --epoch <e re> --generation N --ack-replay --by <operator>` (audit `money.cursor_reset`); riprodhim nga 0, idempotent; grant-et që Central s'i ka ⇒ `unexpected_grant` CRITICAL |
| Enterprise i rikthyer MBRAPA Central | kursori më i ulët ⇒ riprodhim idempotent nga `last_seq`; grant-et e njohur = no-op; raportet vazhdojnë me `report_seq` lokal | `financial_readiness`; rakordimi tregon `cursor_behind` deri sa të arrijë |
| Replay i ngjarjeve dublikatë | no-op (UNIQUE `grant_id`, `event_id`+hash); ngjarje me ID të njëjtë por përmbajtje tjetër ⇒ konflikt, kursori ndalet | — |
| Çmime: Central i rikthyer mbrapa (i njëjti epoch, revision më i ulët) | snapshot më i vjetër ⇒ `STALE`, Enterprise vazhdon me snapshot-in e fundit të plotë; përmbajtje e ndryshuar për version ekzistues ⇒ `PricingApplyError`, asgjë s'aktivizohet | rikrijo versionet e humbura me ID të reja në Central (fotot e mesazheve mbeten të vlefshme); s'ka reset epoke për çmime në M9 |
| Snapshot i çmimeve i humbur në Enterprise | pa snapshot ⇒ fail-closed nën `central`; `pricing_missing` CRITICAL | sinkron i ri (`known_*` bosh ⇒ snapshot i plotë) |
Asnjë rikuperim nuk ndryshon authority-n automatikisht; rikthimi i money authority `central→local` hap sërish mint-in lokal (shih M9-c) — përdor `shadow` si gjendje të sigurt.

### 12. Bllokuesit e mbetur për prodhim (shih edhe `docs/M9_PRODUCTION_CHECKLIST.md`)
1. **PITR/RPO≈0 për Central** nuk është pjesë e repo-s (infra): pa të, DR i grant-eve nuk është i sigurt. 2. **Provider idempotency:** asnjë adapter real nuk ka provë `idempotent_by_reference` (Twilio/HTTP = false) ⇒ UNKNOWN mbetet procesi njerëzor. 3. **Pagesa/gateway:** vetëm manuale me maker-checker (pa gateway, qëllimisht). 4. `pay_from_wallet`/auto-pay të bllokuara nën shadow/central (M9-c) — faturimi nga Central është M10. 5. Tarifa mujore e planit + overage email ende nga plani Enterprise (M10). 6. Retention ligjor i `audit_log`/grant-eve i pavendosur. 7. Gate-i `financial_readiness` kërkon mjedisin Enterprise (`SMS_*`) në hostin që e ekzekuton. 8. Pa alarm-integrim (pa pager/Prometheus): alarmet lexohen nga mjeti/API. 9. M8: SMTP/CAPTCHA/proxy të vërtetë (shih checklist). 10. M11 UI dhe M13 pastrimi legacy.
