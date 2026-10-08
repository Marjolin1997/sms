# M9-g — faturimi periodik në Central (g1: modeli, lëshimi, void)

Vendimet e miratuara: faturim në **arrears**, **pa proporcion**; pagesa e faturës = `payments` i M9-b me `purpose=invoice` (g3); credit note (g3);
faturat legacy importohen read-only (g4); `cp.billing.usage.v1` për email (g2); faturat Central vetëm admin deri në M11; SMS wallet mbetet SMS-only
(`pay_from_wallet`/auto-pay mbeten të bllokuara nën shadow/central). **g1 s'prek Enterprise, përdorimin e email, pagesat, credit notes, importin apo authority switch.**

## Skema (migrimi `0022`, aditiv)
| Tabela | Roli | E pandryshueshme |
|---|---|---|
| `commercial_plans` | kodi + emri i planit | po (ORM + trigger) |
| `plan_versions` | `draft→active→retired`; monedhë, `monthly_fee`, `included_emails`, `content_hash` | pas aktivizimit: fushat financiare + hash (ORM + trigger); `retired` përfundimtar |
| `billing_profiles` | një per enterprise: bill-to, `vat_rate` (vetëm admin) | i ndryshueshëm (snapshot në lëshim) |
| `billing_subscriptions` | një per enterprise; `anchor_started_at`, `anchor_period_index`, `next_period_index`, `cancel_at_period_end`, `status active\|cancelled` | identiteti; `next_period_index` s'kthehet pas |
| `billing_periods` | prova e çdo periudhe: `invoiced\|no_charge`, `UNIQUE(subscription, period_index)` dhe `(subscription, period_start)` | plotësisht (pa UPDATE/DELETE) |
| `invoices` | numër, enterprise, abonim, periudhë, monedhë, `subtotal/vat_rate/tax/total`, `bill_to` + `issuer` (JSON të ngrira), `plan_version_id`, `status open\|paid\|void` | fushat financiare + snapshot-et; `open→void` (g3: `open→paid`), përfundimtar |
| `invoice_lines` | `line_type`, përshkrim, sasi, çmim, shumë, monedhë, periudhë, `plan_version_id`, ref çmimi (g2) | plotësisht |
| `invoice_number_sequence` | rresht per vit, kyç `FOR UPDATE` | vetëm përpara |

Totalet: `subtotal = Σ lines.amount`, `line.amount = cents(sasi × çmim)`, `tax = cents(subtotal × vat_rate)`, `total = subtotal + tax` (HALF_UP). Të
derivuara në shërbim dhe **të verifikuara nga një constraint trigger i shtyrë në PG** (faturë pa linja ose me total të gabuar nuk commit-ohet); `verify_invoice` e
bën të njëjtën kontroll vetëm-lexim. Çmimi i email overage s'është në plan (vjen nga çmimi Central M9-e, g2).

## Semantika
- Periudha k = `[add_months(anchor, k − base), add_months(anchor, k − base + 1))`, UTC, gjysmë-hapur; ditë e kufizuar në fund të muajit, pa drift.
  Faturohet kur `period_end <= now`. Aktivizimi në mes të muajit nis periudhë të plotë; plani i ri hyn nga periudha e radhës; anulimi (`cancel_at_period_end`)
  vlen në fund të periudhës aktuale, që faturohet ende. Rifillimi pas anulimit merr ankorë të re dhe e vazhdon indeksin (UNIQUE s'përplaset).
- Gjendjet e abonimit: `active`, `scheduled_cancel` (`active` + flamur), `cancelled`.
- **Transaksioni i lëshimit** (`billing.process_period`, një periudhë): kyç abonimin → verifikon periudhën e afatuar dhe të papërpunuar → numër i kyçur → snapshot
  issuer/bill-to/VAT/version → linjat → totalet → fatura → `billing_periods` → `next_period_index++` (+ plani në pritje, + anulimi) → audit `system:billing`.
  Crash ⇒ asgjë (numri s'konsumohet). Tarifë 0 ⇒ `no_charge` (rresht periudhe + audit, pa faturë, pa numër).
- `billing.run_due` (punonjësi; mjeti `apps.central.tools.billing_run` vjen në g2): një transaksion per periudhë, rikuperim ≤ 12, periudhë e shtyrë ndalon unazën
  (pa profil; në prodhim pa `CENTRAL_ISSUER_NAME`). **Pa ekzekutim faturimi përmes HTTP.**
- Void: vetëm `open`, arsye ≥ 3 shenja, terminal, i audituar; replay = no-op. Fatura e paguar s'anulohet (credit note, g3).
- Audit: njeri për plan/version/profil/abonim/void; `system:billing` për `billing.period_processed` dhe `invoice.issued`. Detajet s'mbajnë PII (adresë/email).

## API admin (`/admin/billing`, admin shkrim / operator lexim, vetëm GET/POST)
Plane (`/plans`, `/plans/{id}`, `/plans/{id}/versions`, `/plan-versions/{id}` + `update` (vetëm draft) `activate` `retire`), profile (`/profiles/{enterprise}`),
abonime (`/subscriptions`, `/{enterprise}`, `assign`, `cancel` = planifikim në fund periudhe, `resume`), periudha (`/periods`), fatura (`/invoices`, `/{id}`, `void`).
Shumat/çmimet: vetëm string dhjetor (kurrë float; pa eksponent/shenjë/hapësira), `extra=forbid`.

## Konfigurim (Central)
`CENTRAL_INVOICE_DUE_DAYS` (14), `CENTRAL_ISSUER_NAME/ADDRESS/TAX_ID` — ngrihen në çdo faturë në lëshim; në prodhim emri-shembull bllokon lëshimin (shtyhet, alarm në log).

## Bllokuesit për g2
1. Kontrata `cp.billing.usage.v1` (numërues kumulativ i email-eve "billable" me watermark = id i ngjarjes së parë billable) + outbox Enterprise + ingest i pandryshueshëm Central.
2. Linja `email_overage` (çmimi Central M9-e i produktit email, versioni/rregulla të ngrira) dhe `usage_from/usage_to` te `billing_periods`; periudha pret raportin që mbulon `period_end`.
3. Mjeti/punonjësi `apps.central.tools.billing_run` (`--role billing`), readiness i faturimit, integrimi në `financial_readiness`.
4. Numërimi: Central nis nga `0`; para authority switch (g4) sekuenca duhet të farëtohet mbi maksimumin e numrave legacy që të mos përplaset formati `INV-{year}-{n}`.

---

# M9-g2 — Përdorimi i pandryshueshëm i email-it + ingest Central + overage

Authority NUK ndryshon: faturimi legacy i Enterprise mbetet autoritativ; Central faturon vetëm abonimet që i janë caktuar. Wallet-i SMS s'preket.

## 1. Çfarë është një email "billable" (i auditaruar)
Statuset: `queued → sending → sent | unknown | failed`; `sent → delivered | bounced | complained | failed`; `unknown → sent | failed | delivered | bounced | complained`.
**Billable** = hyrja e PARË në `sent|delivered|bounced|complained`. Jo billable: `queued`, `sending`, `unknown` (s'dihet nëse provider-i e pranoi), `failed`.
`SENT→DELIVERED→COMPLAINED` është NJË njësi. Defekti i legacy (numërim sipas `created_at` + status aktual) zëvendësohet nga prova e tranzicionit.

## 2. Prova (Enterprise, `sms_email_billable_events`)
Një rresht per email (`UNIQUE(email_id)`), append-only (ORM + trigger PG UPDATE/DELETE/TRUNCATE), `id` monoton, tenant-scoped. Shkruhet nga `emails._move`
(dera e vetme e tranzicioneve) me **një** `INSERT … ON CONFLICT DO NOTHING` në të njëjtin transaksion me ndryshimin e statusit (atomike; pa COUNT/MAX në tranzicion).
Kosto e matur në hot path: **+1 INSERT** per email te tranzicioni i parë billable; asgjë për tranzicionet e tjera. Race/callback dublikat ⇒ konflikti UNIQUE e bën no-op.

## 3. Raporti `cp.billing.usage.v1` (kumulativ)
`{schema, report_id, report_seq, enterprise_id, product_id, generated_at, watermark, cumulative_billable_count}`; strikt (fusha të panjohura ⇒ refuzim), goldens
`tests/golden/control_plane_billing_usage/`. `cumulative_billable_count` = numri i provave të dukshme; `watermark` = id-ja maksimale e tyre (bosh ⇒ 0); të dyja nga NJË
deklaratë në snapshot REPEATABLE READ (read-only). `generated_at` merret PARA hapjes së snapshot-it. **Watermark-u është informativ**: sepse id-të mund të commit-ohen jo
sipas rendit, faturimi përdor VETËM numëruesin kumulativ dhe deltën (cumulative_to − cumulative_from) — zinxhiri i deltave e bën saktësisht-një-herë pavarësisht rendit të commit-it.

## 4. Outbox + raportuesi (Enterprise, rol `billing_usage_reporter`)
`sms_billing_usage_reports` (pending/sending/sent/retry/failed/superseded; përmbajtja e ngrirë). At-least-once: lease + backoff, rrjeti jashtë transaksionit DB,
raportet e vjetra të papërcjella zëvendësohen. Raport i ri edhe pa ndryshim kur mbaron heartbeat (`SMS_BILLING_USAGE_HEARTBEAT_SECONDS`), që Central të ketë gjithmonë
raport që mbulon fundin e periudhës. 409/413/422 ⇒ `failed` (alarm CRITICAL `billing_usage_rejected`). **Outage-i i raportimit nuk ndalon dërgimin e email-it** (prova dhe outbox
janë lokale; dërgimi s'importon klientin Central). `SMS_BILLING_USAGE_REPORTING=false` (default) ⇒ procesi është boshe. Çelësi duhet të ketë scope `billing:report`.

## 5. Ingest Central (`POST /internal/billing/usage-reports`)
Scope i dedikuar `billing:report` (NUK pranon `money:report/read`, `sync:read`); enterprise-i i raportit duhet të jetë i autorizuar për klientin; klient/çelës i çaktivizuar ⇒ 401.
201 ruajtur · 200 dublikat identik (`report_id`+payload) · 409 `report_id` me payload tjetër, `report_seq` i zënë, ose **rikthim** (numërues/watermark/`generated_at` më i vogël se fqinji
i poshtëm sipas seq, ose më i madh se fqinji i sipërm) · 422 i pavlefshëm (produkt jo-email, enterprise/produkt i panjohur, `generated_at` > tani+5 min) · 413 > 4 KB.
`billing_usage_reports` është append-only (ORM + trigger PG), nuk fshihet kurrë (periudhat e referojnë me FK RESTRICT). Ingest paralel serializohet me advisory lock per (enterprise, product) në PG.

## 6. Semantika e prerjes së periudhës (e saktë)
- **Raporti i prerjes** = raporti i parë i pranuar (sipas `report_seq`) me `generated_at ≥ period_end`. Nëse s'ka ⇒ periudha **PRET** (`waiting_usage`, `usage_report_missing`): asnjë gjendje s'ndryshon, kurrë estimim.
- **Baseline** = `usage_to` i periudhës së mëparshme (zinxhir i pandërprerë) ose, për periudhën e parë me matje, raporti më i fundit me `generated_at ≤ period_start`. S'ka ⇒ `postponed` (`usage_baseline_missing`; kërkon veprim, jo pritje).
- `delta = cumulative_to − cumulative_from`; `extra = max(0, delta − included_emails)` (included nga **versioni i ngrirë i planit** të periudhës). `delta < 0` ⇒ `usage_regression` (s'ndodh pasi ingest e refuzon).
- **Atribuimi (lag i pranuar):** një email numërohet në periudhën e raportit të parë që e përmban tranzicionin e tij — jo sipas `created_at`. Shembull: krijuar 30 jan, `SENDING` kur faturohet janari (raporti ≥ 1 shk), `DELIVERED` 2 shk ⇒ s'është në deltën e janarit; hyn në deltën e shkurtit, saktësisht një herë (zinxhiri e garanton).
- Një raport që mbulon disa fund-periudha: e para merr deltën, të tjerat marrin 0 (të njëjtin raport si prerje; `usage_from == usage_to`).
- Çmimi: caktimi i çmimit Central (M9-e) i produktit email **në `period_end`**; linja ngrin `unit_price`, `price_book_id/version_id/rule_id`, `pricing_source=central`. Pa caktim çmimi ⇒ enterprise-i NUK matet (s'ka overage dhe s'kërkohet raport); caktim pa version/rregull efektiv ⇒ `email_price_unavailable`; monedhë ≠ monedha e planit ⇒ `currency_mismatch` (pa FX); më shumë se një produkt email ⇒ `email_product_ambiguous`. Çmimi i ri më vonë NUK prek faturën e lëshuar.
- `amount = cents(extra × unit_price)` (HALF_UP, helper ekzistues). Sasi nën-cent që rrumbullakoset në 0.00 ⇒ pa linjë (s'krijohet linjë me vlerë zero; emailet konsumohen në zinxhir). `extra = 0` ⇒ pa linjë; faturë vetëm me tarifë, vetëm me overage, ose `no_charge`.
- `billing_periods.usage_from/usage_to` + `usage_from_report_id/usage_to_report_id` provojnë deltën (edhe për `no_charge`). Audit `billing.period_processed` mban numërues dhe ID raportesh (pa PII).
- **Asnjë thirrje rrjeti në transaksionin e faturimit; Central nuk thërret kurrë Enterprise sinkronisht.**

## 7. Mjetet
- `python -m apps.central.tools.billing_run [--limit N] [--max-periods N] [--subscription-id UUID] [--json]` → `due/invoiced/no_charge/waiting_usage/postponed/failed`. Idempotent, i kufizuar, i sigurt në paralel (abonimi kyçet `FOR UPDATE`; UNIQUE e dyfishton mbrojtjen). Kodi 0 pa dështime · 1 me dështime · 2 gabim argumenti/i brendshëm.
- `python -m apps.central.tools.billing_readiness [--json] [--strict]` (vetëm lexim, pa PII): `billing_usage_reports_fresh`, `billing_periods_not_stuck_waiting`, `billing_no_postponed_config`, `billing_invoice_arithmetic`, `billing_run_not_stalled`. Integruar në `financial_readiness` (burimi `billing`).
- Konfigurim Central: `CENTRAL_BILLING_USAGE_FRESH_SECONDS` (900) / `_STALE_SECONDS` (7200), `CENTRAL_BILLING_WAIT_WARN_SECONDS` (3600) / `_FAIL_SECONDS` (86400). Enterprise: `SMS_BILLING_USAGE_REPORTING`, `_REPORT_INTERVAL_SECONDS` (300), `_HEARTBEAT_SECONDS` (600), `_STALE_SECONDS` (7200).
- Retention: raportet dhe prova NUK fshihen (konservator); politika e ruajtjes vendoset në g4 pas importit.

## 8. Bllokuesit për g3 (pagesa/alokimi/credit notes)
1. Alokimi i pagesave mbi faturat `open` + gjendja `paid` (kolona `paid_at` e rezervuar) dhe credit notes (void i faturës së paguar sot refuzohet).
2. Ndarja e mbulimit të pagesës per linjë (tarifë/overage) dhe rregullat e pagesës së pjesshme/mbipagesës — pa auto-pay nga wallet SMS.
3. Rakordim fature↔pagesë dhe readiness i arkëtimit (aging) — `billing_run_not_stalled`/`waiting` nuk mjaftojnë për arkëtimin.
4. Numërimi/farëtimi legacy dhe baseline-i i përdorimit të email-it para authority switch (g4): periudha e parë pa raport para ankorës ⇒ `usage_baseline_missing` derisa të ketë opening balance të importuar.

---

# M9-g3 — Shlyerja e faturave: pagesa fature, alokim, paid, credit notes

Vetëm Central. Enterprise legacy NUK migrohet (g4). Wallet-i SMS dhe ledger-i tregtar i kredisë NUK preken kurrë nga faturat.

## 1. Pagesat (`payments`, një tabelë) — `purpose = credit | invoice`
Shtuar `purpose` (default `credit`) dhe `invoice_id`; `account_id` bëhet nullable. CHECK në DB: `credit ⇒ account_id NOT NULL, invoice_id NULL`; `invoice ⇒ account_id NULL, invoice_id NOT NULL`
(kombinim i paqartë refuzohet). FK e përbërë `(invoice_id, enterprise_id, currency) → invoices` (e njëjta monedhë/enterprise). `purpose`/`invoice_id` të ngrira (ORM + trigger PG).
Rrjedha `credit` (M9-b: llogari, ledger, maker-checker) është e pandryshuar; `payments.approve/list` janë vetëm për `credit`, pagesat e faturave shihen/miratohen vetëm te `invoice_payments`.

## 2. Alokimi (`invoice_payment_allocations`, i pandryshueshëm)
Një pagesë = një alokim i plotë: `UNIQUE(payment_id)`, `UNIQUE(invoice_id)`; FK e përbërë `(payment_id, invoice_id, currency, amount) → payments` e detyron shumën/monedhën të përputhen me pagesën. Prova historike e shlyerjes (s'nxirret vetëm nga `invoice.status`).

## 3. Transaksioni i shlyerjes (`invoice_payments.approve`, një transaksion, pa rrjet)
kyç pagesën → (idempotent nëse approved) → pending + maker-checker → kyç faturën (rend i fiksuar: pagesë → faturë; `void_invoice` kyç vetëm faturën) → OPEN → shuma = `invoice.total` dhe monedha → pa alokim paraprak →
alokim + pagesë `approved` + faturë `paid` (`paid_at`) → audit `payment.approve` dhe `invoice.settle`. Çdo dështim rikthen gjithçka. **V1: pa pjesëtime, pa mbipagesë/nënpagesë, pa konvertim në kredi** (mospërputhje ⇒ 409, pagesa mbetet pending dhe refuzohet nga stafi).
PG (constraint trigger-a të shtyrë): fatura `paid` kërkon alokim me shumë/monedhë të njëjtë; alokimi kërkon faturë `paid` + pagesë `approved` fature; pagesë fature e miratuar kërkon alokim.

## 4. Gjendjet
Faturë: `open → paid | void` (terminale). `paid` s'anulohet; `void` s'paguhet; pa rihapje. Korrigjimi i `paid` = credit note. Pagesë: `pending → approved | rejected` (si M9-b; krijuesi njeri ≠ miratuesi).

## 5. Credit notes (`credit_notes`, `credit_note_sequence`)
Të pandryshueshme (ORM + trigger PG, pa fshirje, pa gjendje). Vetëm për fatura `paid`; `amount > 0`; monedha = ajo e faturës; arsye e detyrueshme (≤ 500); **Σ credit notes ≤ `invoice.total`** (kyç faturën `FOR UPDATE` + constraint trigger i shtyrë në PG).
Numërim `CN-{year}-{n:06d}` me rresht të kyçur (sekuencë e ndarë nga faturat; rollback e kthen numrin). Idempotent me `(invoice_id, idempotency_key)`. Foto e issuer/bill-to të faturës. Pa rifund automatik, pa pagesë, pa kredi wallet; pa linja (V1: shumë + arsye + referencë fature mjafton — s'ka ende nevojë për atribuim për linjë). Faturë `open` korrigjohet me void.

## 6. Rakordim, aging, readiness
`GET /admin/billing/settlement`: numërues fature/pagesash, aging i faturave OPEN (`current, 1-30, 31-60, 61-90, 90+` sipas `due_at`; pa interes/penalitet) dhe anomali (pa korrigjim automatik): paid pa alokim, pagesë e miratuar pa alokim, alokim i papërputhshëm, alokim i dyfishtë, credit notes mbi total/mbi faturë jo-paid.
`billing_readiness`: FAIL `billing_settlement_integrity`; WARN `billing_invoice_payments_not_stale`, `billing_invoices_not_overdue`, `billing_rejected_payments_reconciled`. Faturë e vonuar NUK bllokon dërgimin SMS/email.

## 7. API admin (`/admin/billing`; admin shkrim, operator lexim; asnjë DELETE/PUT/PATCH; asnjë API klienti)
`/invoice-payments` (GET lista/detaj, POST krijim — `external_reference` i detyrueshëm = çelës idempotence, `/{id}/approve`, `/{id}/reject`), `/allocations` (vetëm lexim), `/credit-notes` (GET, POST — `idempotency_key` i detyrueshëm), `/settlement`;
detaji i faturës (`/invoices/{id}`) shton `settlement` (alokim, pagesa, credit notes, `credited_total`, `net_amount`). Hyrje strikte: `extra=forbid`, shuma si string dhjetor (kurrë float), monedhë/UUID strikte, arsye/referencë të kufizuara.

## 8. Audit
`payment.create|approve|reject` (detail me `purpose=invoice`), `invoice.settle`, `credit_note.issue` (arsye e detyrueshme). Pa PII (vetëm UUID/numra/shuma/monedhë).

## 9. Bllokuesit për g4
1. Import i pagesave/faturave legacy të Enterprise (mapping drejt `purpose=invoice` + alokim; numërim: farëtimi i `invoice_number_sequence` dhe `credit_note_sequence` mbi maksimumin legacy).
2. Pagesa të pjesshme/mbipagesa të legacy (V1 i refuzon; kërkon vendim biznesi ose rrugë manuale).
3. Faturë `open` legacy me credit note (V1: vetëm `paid`).
4. Opening balance i përdorimit të email-it dhe authority switch/shadow (nga g2).

---

# M9-g4 — Import legacy, shadow dhe cutover i autoritetit të faturimit

Nuk ka asnjë fshirje/drop të të dhënave legacy (M13 pronar i pastrimit). Central NUK lexon DB-në e Enterprise dhe anasjelltas: transporti është një **artifact offline** me hash.

## 1. Auditimi i formës legacy (skema aktuale `app/models/billing.py`)
| Objekt | Fakte që drejtojnë importin |
|---|---|
| `sms_plans` | kod unik, **i pandryshueshëm** (çmim i ri = kod i ri), `email_overage_price` BRENDA planit (në Central është çmimi M9-e, jo fushë plani), `status active\|retired` |
| `sms_subscriptions` | `started_at` (ankorë), `periods_billed`, `cancel_at_period_end`, `pending_plan_id`; **rifillimi pas anulimit RIVENDOS `started_at` dhe `periods_billed=0`** (Central e vazhdon indeksin) |
| `sms_invoices` | `INV-{year}-{n:06d}`; `bill_to` JSON text; **pa issuer snapshot, pa version plani, pa indeks periudhe**; `paid_via wallet\|online`, `voided_reason`; **pa `voided_at`/`cancelled_at`** |
| `sms_invoice_lines` | vetëm përshkrim/sasi/çmim/shumë; pa `line_type`; `pricing_source legacy_plan\|central`, `pricing_version_ref` |
| `sms_payments` (purpose=invoice) | seanca online; `succeeded` = e plotë ose jo (shuma/monedha krahasohen me totalin) |
| wallet | pagesa wallet = hyrje ledger `invoice` (`ref_id = numri i faturës`) |
| `sms_invoice_counters` | maksimumi i numrit për vit; **s'ka dokumente credit-note** (numërimi CN fillon nga Central) |
| periudha pa faturë | plan falas ⇒ `periods_billed` avancon pa asnjë rresht ⇒ **s'ka provë** për `no_charge` |
Klasat e rreshtave: `exact · importable · already_imported · conflict · invalid · unsupported · requires_manual_review`. Numrat konkretë gjenden me `billing_import` në dry-run mbi eksportin real (auditi është i parametrizuar nga të dhënat, jo i supozuar).

## 2. Eksport / import
- `python -m scripts.billing_export --out export.json` (Enterprise, vetëm lexim, snapshot REPEATABLE READ, 0600, pa mbishkrim): plane, profile, abonime, fatura+linja, pagesa fature, dëshmi wallet, maksimumet e numërimit, gjendja e hapjes së përdorimit, atestimi i autoritetit. Kontrata `cp.billing.legacy_export.v1` (strikte; `counts` kundër shkurtimit; `content_hash`).
- `python -m apps.central.tools.billing_import --artifact export.json` → **dry-run** (zero shkrime; klasifikim, seed-e sekuencash, bllokues; kodi 1 nëse ka bllokues). `--apply --evidence-hash <hash> --actor-email <admin>` (atomik; `--require-clean` refuzon me bllokues). Rirunim i të njëjtit artifact = no-op; i njëjti `export_id` me hash tjetër = refuzim; hash i gabuar = refuzim.
- Evidenca: `billing_import_batches/items/issues` (source_system/table/id, `source_hash` i pjesës së ngurtë + `state_hash`, batch, koha). Rreshtat e bllokuar → `billing_import_issues` (zgjidhje manuale `--resolve ISSUE --reason`, një herë, e audituar).
- Deltat: eksport i ri (`export_id` i ri) → objektet e importuara kalojnë përpara vetëm kur është e ligjshme (plan active→retired, faturë open→paid|void me shlyerje të vlefshme, abonim me `periods_billed` që rritet); gjithçka tjetër = `conflict`.

## 3. Hartëzimi
- **Plan**: një `CommercialPlan` + një version `active` (ose `retired`) me `monthly_fee/included_emails/currency` të ngrira; i njëjti kod nuk dyfishon version. Çmimi i overage mbetet autoriteti Central (M9-e); çmimi legacy ruhet vetëm si dëshmi.
- **Abonim**: `anchor_started_at = started_at`, `anchor_period_index = 0`, `next_period_index = periods_billed` (pa mbivendosje/anashkalim); rifillim pas importit ⇒ `requires_manual_review`.
- **Periudha historike**: rresht `billing_periods` (`provenance=legacy_import`, `plan_version_id` NULL) vetëm për fatura të provuara; periudha pa faturë mbeten **të panjohura** (s'fabrikohet `no_charge`); fatura nga segmenti i një ankore të mëparshme marrin `period_index = −source_id` (pa periudhë).
- **Faturë/linja**: aritmetika rikontrollohet (kurrë coercion; shkelje ⇒ `invalid`); `provenance=legacy_import`, issuer `{"provenance":"unknown"}`, `plan_version_id` NULL; `line_type`: `monthly_fee` / `email_overage` vetëm me rregullin eksplicit të teksteve të gjeneruesit legacy, përndryshe `legacy` (shuma ruhet). `price_version_id` vetëm nëse ekziston në Central.
- **Shlyerja**: fatura `paid` kërkon provë: një pagesë online e plotë (`provider:external_id` real) ose dëshmi wallet me shumë të barabartë; pjesëtim/mbipagesë/shumë pagesa ⇒ `unsupported` (bllokon); pa provë ⇒ `requires_manual_review`. Importohet **pagesë `purpose=invoice` e miratuar (`source=legacy_import`) + alokim**; për wallet `external_reference=wallet-ledger:<id>` dhe shënim "debiti NUK rilozet" — pa ledger tregtar, pa grant, pa wallet. `void` ruan arsyen (ose default të shënuar); `voided_at` = koha e importit (e shënuar në evidencë). `open` importohet `open`.

## 4. Sekuencat
`invoice_number_sequence` ngrihet në maksimumin e **të gjithë** numrave legacy (edhe të bllokuarve) dhe të numëruesit; kurrë nuk ulet; audit `billing.sequence_seed`; dry-run tregon vlerat. Credit notes legacy = 0 (auditim skemë); `credit_note_like>0` bllokon.

## 5. Gjendja e hapjes së përdorimit të email-it
`billing_usage_baselines` (e pandryshueshme): `cumulative_count`/`watermark` = numri i provave me `billable_at < boundary` (boundary = fillimi i periudhës së parë të faturuar nga Central), + `capture_active_since`. Kushti i detyrueshëm: `capture_active_since ≤ boundary` (përndryshe `requires_manual_review`: prova e përdorimit s'është e plotë — pritet kufiri i periudhës së radhës dhe ri-eksport). Delta e Central e nis nga baseline (renditja: `usage_to` i periudhës së mëparshme › baseline hapjeje › raporti ≤ fillimi).
**Email i vonuar para-cutover:** një email i krijuar para cutover-it por që bëhet i faturueshëm PAS kufirit kap provë normale (vlen momenti i tranzicionit, jo `created_at`) dhe faturohet saktësisht një herë në deltën e Central; kurrë nuk groposet në baseline.

## 6. Autoriteti (`local | shadow | central`)
Enterprise `SMS_BILLING_AUTHORITY` (+ `SMS_BILLING_AUTHORITY_ACK` në prodhim; jo-local kërkon `SMS_BILLING_USAGE_REPORTING`); Central tabela `billing_authority_state` (singleton, parazgjedhje `local`). `central` në Enterprise = **freeze fail-closed** (`BillingAuthorityFrozen`, API 409): run/generate, plane, profil, abonim, anulim, pagesë wallet, void, shlyerje online (pagesa e vonuar legacy shënohet `failed/billing_authority_central`, s'kreditohet wallet-i); worker-i i faturimit nuk bën asgjë; leximi i historisë mbetet. Central: `billing_run` autoritar refuzon (kodi 3) derisa modaliteti të jetë `central`; `process_period` mbetet shërbim i brendshëm.
Kalimi: `local→shadow→central` (jo drejtpërdrejt); `central` kërkon `--ack` + readiness pa FAIL.

## 7. Shadow
`python -m apps.central.tools.billing_shadow`: projekton periudhat e fundit legacy dhe i krahason me faturat e importuara; `billing_shadow_comparisons` (append-only). Kategori: `exact, period_mismatch, currency_mismatch, plan_mismatch, tax_mismatch, insufficient_usage, pricing_mismatch, usage_mismatch, amount_mismatch, legacy_only, central_only`. **Nuk** konsumon numër fature, s'krijon faturë/periudhë autoritare, s'shlyen, s'përparon kursorin. Readiness: FAIL për period/currency/plan/tax/amount/central_only; WARN për usage/pricing/insufficient_usage/legacy_only (të shpjeguara, kërkojnë shqyrtim).

## 8. Protokolli i cutover (rend i detyrueshëm; asnjë çast me dy lëshues)
1. Enterprise: `scripts.billing_authority_readiness` · Central: `shadow` aktiv me import + `billing_shadow` të pranueshëm.
2. Enterprise: ndalo workerin e faturimit; `SMS_BILLING_AUTHORITY=central` (+ACK) dhe rinis ⇒ **freeze** (Enterprise nuk lëshon më).
3. Enterprise: eksport FINAL (`authority.mode=central` atestuar).
4. Central: dry-run → `--apply` (deltat e fundit, baseline-et, seed-et e sekuencave).
5. Central: `billing_authority_readiness` (të gjitha FAIL = 0) · `CENTRAL_BILLING_WORKER_CONFIGURED=true`.
6. Central: `billing_authority set --mode central --ack --reason ...` · 7. `billing_run` (periudhat e afatuara faturohen nga Central).
Boshllëku mes hapit 2 dhe 6 është i sigurt (askush s'lëshon; periudhat e afatuara presin).

## 9. Rollback
- **Para faturës së parë autoritare Central** (`provenance=central` = 0): `billing_authority set --mode shadow|local --ack --reason` + Enterprise `SMS_BILLING_AUTHORITY=local` (hiq ACK) dhe rinis; kursori Enterprise (`periods_billed`) verifikohet me eksportin e fundit para rihapjes.
- **Pas saj**: rollback i bllokuar (`Conflict`). Procedurë: forward-fix në Central (credit note/void/korrigjim sipas g3) ose rakordim manual i dokumentuar; asnjë rikthim i verbër te Enterprise (do të rilëshonte periudha dhe numra).

## 10. Gate-t e readiness (`billing_authority_readiness`)
import i plotë · konflikte të pazgjidhura = 0 · pa pjesëtim/mbipagesë të pazgjidhur · abonimet aktive të hartëzuara · seed-et e sekuencave të sigurta · baseline hapjeje për çdo abonim të matur · çmim Central për çdo plan legacy me overage · raport përdorimi i freskët · shadow i pranueshëm · Enterprise i ngrirë (atestuar) · pa lëshues të dyfishtë · worker Central i konfiguruar · ACK.

## 11. Bllokuesit për g5
1. Dërgimi i faturave/PDF te klienti (email/portal) dhe API klienti — ende vetëm admin.
2. Pagesa online Central (gateway) dhe pjesëtime/mbipagesa me rregull biznesi (V1 i refuzon).
3. Retention/arkivim i artifact-eve të importit dhe i `billing_import_*` pas stabilizimit; pastrimi i tabelave legacy (M13).
4. Re-anchor i abonimit të importuar (rifillim pas importit) pa shqyrtim manual.

## 12. Shënime testimi — M9-g4 (final)
Rerun i plotë mbi `f6e1e3f`: SQLite 2404 passed / 1088 skipped / 0 failed; PG (3 shard, bashkim = 3496 teste = koleksioni i plotë, pa dublikime): 1268+1055+1093 passed, 0 failed. Gate-t: migrimi 0025 up/down/up, `compare_metadata=0`, trigger-at PG, `ruff check` — të gjelbra.
**Përjashtim i pranuar (mjedis, jo regresion):** 4 error në `tests/test_tenant_isolation.py` (`socket.gaierror`) — DNS nuk funksionon në VM-në e testimit (`getent hosts example.com` bosh). Riprodhohen identikisht në rev. g3 të miratuar `35e6a62`; sjellja e prodhimit të g4 nuk preket. Çdo dështim tjetër i ri NUK klasifikohet "mjedis" pa provë.

---

# M9-g5 — Hardening final dhe mbyllja e prodhimit (V1)

Nuk shton funksion faturimi (pa proporcion, pagesa të pjesshme/mbipagesa, gateway, dunning, UI, tatime, FX, pastrim legacy). Shton: readiness final, invariante të verifikueshme, observability/alarme, hardening të workerit, workflow formal të çështjeve manuale, politikë shadow, runbook/rollback, retention, backup/DR, rishikim sigurie.

## 13. Arkitektura V1 (përmbledhje) dhe makina e autoritetit
Enterprise mbetet pronar i dërgimit/trafikut; **Central** është autoriteti i faturimit periodik pas cutover-it: plane/versione (g1) → abonime → periudha (arrears, pa proporcion) → fatura/linja të pandryshueshme me aritmetikë të imponuar nga DB → përdorim email nga raportet kumulative (g2) → shlyerje: pagesë fature (maker-checker) + alokim 1:1 + credit notes mbi fatura të paguara (g3) → import/shadow/cutover (g4) → hardening (g5). Asnjë thirrje rrjeti brenda transaksionit financiar; asnjë lidhje DB mes dy sistemeve.

```
 local ──(set shadow)──▶ shadow ──(set central + ACK + readiness pa FAIL)──▶ central
   ▲                        ▲                                                  │
   └──────(ACK, para faturës së parë Central)───────────────────────────────────┘   (central → shadow|local)
 central → * pas faturës së parë (provenance=central): BLLOKUAR (forward-fix / rakordim manual)
```
Enterprise: `SMS_BILLING_AUTHORITY=local|shadow|central`; `central` = freeze fail-closed (nuk lëshon, nuk shlyen). Central: tabela `billing_authority_state`; `billing_run` autoritar del me kod 3 jashtë `central`.

## 14. Runbook cutover (operatori) — rend i detyrueshëm
`ENT$` = host Enterprise · `CEN$` = host Central. Çdo hap: **komanda → pritja → kushti i dështimit → vendimi (stop/rollback)**. Asnjë hap nuk kalon pa pritjen e treguar.

| # | Komanda | Pritja | Dështim ⇒ vendim |
|---|---|---|---|
| 1 | `ENT$ python -m scripts.billing_export --out /secure/rehearsal.json` (Enterprise ende aktiv: provë e përgatitjes) · `CEN$ python -m apps.central.tools.billing_import --artifact /secure/rehearsal.json --json` | dry-run: `blocking_total`, `blocking_by_category`, `sequence_seeds`, `proposed_baselines`; NUK shkruan asgjë | kod 2 (artifact i pavlefshëm) ⇒ STOP, korrigjo eksportuesin. Bllokues ⇒ shko te hapi 5 para çdo gjëje tjetër |
| 2 | `ENT$ python -m scripts.billing_authority_readiness --target shadow` pastaj `--target central` | `shadow`: PASS; `central`: shfaq çfarë mbetet (pending checkouts, periudha të afatuara) | FAIL ⇒ STOP; mos vazhdo me çekout-e online pending |
| 3 | (artifact FINAL merret në hapin 14–16; këtu vetëm verifiko hash-in e provës) `CEN$ python -m apps.central.tools.billing_import --artifact /secure/rehearsal.json` | `content hash …` i njëjtë me `content_hash` të printuar nga eksporti | `invalid artifact` / hash i ndryshëm ⇒ STOP; transferim i dëmtuar, eksporto sërish |
| 4 | `CEN$ python -m apps.central.tools.billing_authority set --mode shadow --reason "<arsye>" --actor-email <admin>` | `status` ⇒ `shadow`; Enterprise ende lëshon | `Conflict` ⇒ lexo mesazhin; mos kalo `local→central` |
| 5 | `CEN$ python -m apps.central.tools.billing_import --issues` | çdo çështje ka `category`, `allowed`, `forbidden`, `waivable` | çështje `unsupported/invalid/conflict` ⇒ korrigjo në burim + ri-eksport (shih §16); mos përdor waiver |
| 6 | `CEN$ python -m apps.central.tools.billing_import --artifact /secure/final.json --apply --evidence-hash <hash nga ENT> --actor-email <admin> --require-clean` | JSON `batch_id`, `summary`; rirunim = no-op | `conflict:` hash i gabuar/bllokues ⇒ STOP, nuk është shkruar asgjë (atomik) |
| 7 | `CEN$ python -m apps.central.tools.billing_import --artifact /secure/final.json --json` | të gjitha rreshtat `already_imported`/`exact`, `blocking_total=0` | çdo `importable` i mbetur ⇒ import jo i plotë ⇒ STOP |
| 8 | `CEN$ python -m apps.central.tools.billing_final_readiness --json` (kërko `usage_opening_baseline`) | `PASS` për çdo abonim të matur (baseline krijohet nga hapi 6) | FAIL ⇒ pritet kufiri i periudhës së radhës + ri-eksport (kurrë baseline zero i shpikur) |
| 9 | (e njëjta komandë) kërko `sequence_seeds_safe` | `PASS`: `invoice_number_sequence` ≥ maksimumi i çdo numri legacy/Central | FAIL ⇒ STOP; mos e ul kurrë sekuencën |
| 10 | (e njëjta) kërko `inv_credit_note_sequence_not_behind` | `PASS` (legacy s'ka credit notes; `credit_note_sequence` nuk seed-ohet) | FAIL ⇒ STOP, hetim |
| 11 | `CEN$ python -m apps.central.tools.billing_shadow --recent 3` (disa ditë/cikle) | krahasime të reja; `--summary --json` për gjendjen e fundit | shih §17 |
| 12 | `CEN$ python -m apps.central.tools.billing_shadow --summary --json` | `by_category` vetëm `exact` (+ WARN të dokumentuar) | çdo kategori HARD ⇒ STOP (mos normalizo) |
| 13 | `CEN$ python -m apps.central.tools.billing_final_readiness --json` (kërko `latest_usage_report_healthy`) | `PASS`: raport i freskët për çdo enterprise të matur | stale ⇒ prit/rindiz reporterin Enterprise; mos kalo |
| 14 | `ENT$` ndalo workerin e faturimit; `SMS_BILLING_AUTHORITY=central`, `SMS_BILLING_AUTHORITY_ACK=true`, rinis web+worker | API faturimi Enterprise kthen 409 `billing_authority_frozen`; worker nuk bën asgjë | s'ngrin ⇒ STOP, rikthe `SMS_BILLING_AUTHORITY=shadow` |
| 15 | `ENT$ python -m scripts.billing_authority_readiness --target central` | `no_pending_online_checkouts` PASS, asnjë transaksion në ecje | pending ⇒ prit skadimin/refuzimin ose trajtoje manualisht; mos vazhdo |
| 15b | `ENT$ python -m scripts.billing_export --out /secure/final.json` **tani** (i ngrirë) | `authority.mode=central` i atestuar; printon `export_id`, `content_hash` | atestim ≠ central ⇒ readiness `enterprise_billing_frozen` FAIL ⇒ STOP |
| 16 | **ACK prodhimi** (vendim njerëzor i regjistruar në tiketë): `CENTRAL_BILLING_WORKER_CONFIGURED=true` në mjedisin Central dhe `--ack` në hapin 17 | ACK shfaqet te `production_ack` | pa ACK ⇒ `Conflict`, asgjë nuk ndryshon |
| 17 | `CEN$ python -m apps.central.tools.billing_authority set --mode central --ack --reason "<tiketë>" --actor-email <admin>` | `status` ⇒ `central`; readiness pa FAIL kontrollohet brenda komandës | `Conflict` me emrat e FAIL ⇒ rregulloji, mos detyro |
| 18 | `CEN$ python -m apps.central.tools.billing_final_readiness --strict --json --observability` | `PASS` (ose WARN të shpjeguar), `alerts` pa CRITICAL | CRITICAL ⇒ STOP para batch-it të parë; vendos rollback sipas §15 Case A |
| 19 | `CEN$ python -m apps.central.tools.billing_run --limit 50 --json` | `invoiced/no_charge/waiting_usage/postponed/failed`, `failed=0`; kod 0; një ekzekutim i dytë njëkohësisht jep kod 4 | `failed>0` ⇒ STOP, shih log pa PII; kod 3 ⇒ mode ≠ central |
| 20 | `CEN$ GET /admin/billing/invoices?status=open` + `GET /admin/billing/ops` | numri i faturave = `invoiced`; numrat vazhdojnë pas seed-it (p.sh. `INV-2030-000004`), pa boshllëk/dublikat; shuma = projeksioni i shadow | numër/total i papritur ⇒ STOP; VOID i faturës OPEN me arsye (jo ri-lëshim) dhe hap incident |
| 21 | monitoro ciklin e parë: `billing_final_readiness --json` çdo orë (cron i jashtëm) + `GET /admin/billing/ops` | heartbeat i freskët, `periods.due_unprocessed=0`, `settlement.anomalies` bosh | alarm CRITICAL ⇒ Case B nëse ka fatura autoritare |

Pas hapit 17 çdo korrigjim bëhet me mjetet e g3 (void faturë OPEN / credit note për të paguar), kurrë me ndryshim të drejtpërdrejtë në DB.

## 15. Rollback — dy raste të qarta
**Case A — Central `central` por ZERO fatura autoritare (`provenance=central` = 0):**
1. Ndalo `billing_run` (cron/worker Central). Verifiko: `billing_authority status` dhe `GET /admin/billing/ops → authority.central_invoices = 0`.
2. Verifiko kursorin: `GET /admin/billing/periods` pa periudha `provenance=central`; `next_period_index` i çdo abonimi = `periods_billed` i eksportit të fundit (asnjë periudhë e ndryshuar); `invoice_number_sequence` nuk ka kaluar maksimumin e eksportit (asnjë numër Central i konsumuar).
3. ACK eksplicit i operatorit: `billing_authority set --mode shadow|local --ack --reason "<tiketë>" --actor-email <admin>` (pa `--ack` refuzohet).
4. Enterprise: `SMS_BILLING_AUTHORITY=local`, heq ACK, rinis. Para rihapjes krahaso `periods_billed`/maksimumet e numrave me eksportin e fundit.
5. Nëse ka çfarëdo mospërputhje kursori/sekuence ⇒ MOS rihap Enterprise; trajtoje si Case B.

**Case B — Central ka lëshuar të paktën një faturë autoritare:** NUK ka rollback automatik (`Conflict` nga `set_mode`; kurrë override). Procedurë:
1. Ngrij të dy lëshuesit nëse duhet: ndalo `billing_run`; Enterprise mbetet `central` (i ngrirë). Mos e kthe Enterprise në `local` (do të rilëshonte periudha/numra).
2. Rakordim: `billing_final_readiness --json`, `GET /admin/billing/settlement`, `GET /admin/billing/invoices`; liston faturat e gabuara.
3. Forward-fix: faturë OPEN e gabuar ⇒ **void** me arsye; e paguar ⇒ **credit note**; periudha e munguar ⇒ korrigjim i konfigurimit dhe ri-ekzekutim `billing_run` (idempotent). Çdo ndërhyrje manuale dokumentohet me tiketë; asnjë ndryshim i drejtpërdrejtë në DB, asnjë reset i heshtur i kursorit.
4. Kthim në shadow/local nuk është opsion; nëse biznesi kërkon rikthim te Enterprise ⇒ projekt i veçantë migrimi (jashtë V1).

## 16. Workflow i çështjeve manuale të importit
`billing_import --issues` (JSON, pa PII) liston: `category`, `classification`, burimin, arsyen, `allowed`, `forbidden`, `waivable`. Një çështje mbyllet VETËM me një nga dy rrugët reale:
- **`superseded`** — një batch i mëvonshëm (ri-eksport pas korrigjimit në burim) ka importuar objektin ose e mban si çështje më të re. Zgjidhja e vërtetë ndodh në Enterprise.
- **`operator_waiver`** — vetëm `requires_manual_review`, jo `usage_baseline_insufficient`, me `--evidence-ref` (tiketë/dokument, 8–200 shenja, pa `;`). Objekti MBETET jashtë Central; readiness raporton WARN `import_waivers_documented`; audit `billing.import_issue_resolve` me `kind`+`evidence_ref`.
`unsupported | invalid | conflict` nuk fshihen kurrë me fjalë: `resolve` pa dëshmi kthen `Conflict` që shpjegon çfarë duhet bërë.

| Kategoria | Operatori sheh | Veprimi i lejuar | Shkurtorja e ndaluar |
|---|---|---|---|
| `partial_payment` | "online payment/wallet debit is lower than (partial payment of) the invoice total" | korrigjo shlyerjen në burim (një pagesë e plotë) dhe ri-eksporto | importo si e paguar; mbyll me tekst |
| `overpayment` | "...is higher than (overpayment of)..." | rimburso/korrigjo në burim, ri-eksporto | importo tepricën; waiver |
| `missing_external_reference` | paid pa `paid_via`/ledger/`paid_at`/referencë | jep referencën reale në burim dhe ri-eksporto; ose waiver me `evidence_ref` | shpik referencë; shëno paguar pa dëshmi |
| `usage_baseline_insufficient` | "usage evidence is incomplete" / produkt email i paqartë | prit kufirin e periudhës së radhës dhe ri-eksporto (capture duhet të paraprijë periudhën e parë Central) | waiver; baseline zero; ndrysho `capture_active_since` |
| `unsupported_line` | rresht që s'përputhet me rregullat e tekstit | korrigjo tekstin/aritmetikën në burim (rreshtat e panjohur me aritmetikë të saktë importohen si `legacy`) | ndrysho shumat në Central |
| `plan_conflict` | plani ndryshoi në burim pas importit / kod tjetër ekziston | rikthe planin në burim (kod i ri = çmim i ri), ri-eksporto | ndrysho version plani në Central |
| `subscription_conflict` | `periods_billed` prapa / abonim ri-ankoruar / abonim ekziston | rreshto `periods_billed`/ankorën në burim; rishikim manual i hartëzimit | reset `next_period_index` në Central |
| `sequence_conflict` | numër jashtë `INV-YYYY-NNNNNN` ose numër ekzistues | rregullo numrin në burim; fatura Central e përplasur kalon nga void normal | ul `invoice_number_sequence` |
| `invoice_arithmetic_mismatch` | subtotal/tatim/total nuk rrjedhin | korrigjo faturën në burim, ri-eksporto | rillogaritje gjatë importit |
| `currency_mismatch` | monedha e faturës/pagesës/planit ndryshon | rreshto monedhën në burim (pa FX në V1) | konverto monedhën |

## 17. Politika e pranimit të shadow (cutover lejohet vetëm kur…)
Çdo (abonim, periudhë) merr krahasimin e FUNDIT. Pragjet:
- **HARD FAIL** (blloku): `currency_mismatch`, `period_mismatch`, `plan_mismatch`, `tax_mismatch`, `central_only`, `legacy_only` (të pashpjeguara), `amount_mismatch` mbi tolerancën; si dhe kontrollet e veçanta `usage_opening_baseline` (baseline mungon), `currency_pricing_mapping_valid`/`legacy_overage_has_central_price` (çmim mungon), `sequence_seeds_safe` (përplasje sekuence), krahasim që s'është ekzekutuar për një abonim të importuar.
- **Tolerancë shume:** `CENTRAL_BILLING_SHADOW_AMOUNT_TOLERANCE` (parazgjedhje `0` = asnjë). Brenda tolerancës ⇒ WARN; mbi të ⇒ FAIL. Nuk normalizon: vetëm vendos nivelin e raportimit; kategoria e ruajtur mbetet `amount_mismatch`.
- **WARN i lejuar vetëm kur është i dokumentuar:** `usage_mismatch` (përdorimi ndryshon sepse legacy s'kishte baseline të plotë), `pricing_mismatch` (çmimi M9-e ≠ çmimi legacy i overage — pritet kur çmimet u migruan), `insufficient_usage` (periudha pret raportin), amount brenda tolerancës. Çdo WARN regjistrohet në tiketën e cutover-it me shpjegim.
- Asnjë ndryshim i dukshëm nuk "pastrohet": korrigjohet shkaku, `billing_shadow` rilëshohet (krahasimi i ri zëvendëson të vjetrin në pamje; i vjetri mbetet si evidencë).

## 18. Retention (konservator; asgjë financiare nuk fshihet)
| Të dhëna | Politika |
|---|---|
| fatura, linja, alokime, pagesa, credit notes, audit financiar, rreshta të importuar (provenance) | **PA fshirje kurrë** (trigger-a DB + ORM `before_delete`) |
| `billing_import_batches/items/issues`, `billing_usage_baselines`, `billing_shadow_comparisons` | ruhen (evidencë ligjore); nuk ka rrugë fshirjeje; arkivimi është vendim i ardhshëm (M13) pas afatit ligjor |
| `billing_usage_reports` | `CENTRAL_USAGE_REPORT_RETENTION_DAYS` (parazgjedhje 0 = ruaj gjithçka); fshin vetëm pas vendimit ligjor, dry-run i parë (`apps.central.tools.retention`); zinxhiri i baseline/kufijve ruhet (`full_days`, `keep_last`) |
| logje operacionale (stdout i `billing_run`, heartbeat audit `billing.run`) | `billing.run` është audit (vetëm-shtim, ruhet); logjet e proceseve sipas politikës së platformës (rekomandim ≥ 90 ditë) |
| metadata retry | Central s'ka retry financiar jashtë idempotencës së `process_period`; asgjë për pastrim |
Afati ligjor i ruajtjes së faturave është i panjohur ⇒ **ruhet gjithçka si parazgjedhje**; çdo afat është konfigurim eksplicit i operatorit, kurrë parazgjedhje.

## 19. Observability dhe alarme
`GET /admin/billing/ops` (admin|operator, vetëm lexim, pa PII) dhe `billing_final_readiness --observability`: autoriteti aktiv + ACK + `central_invoices` + freeze i atestuar; batch-i i fundit i importit; çështje të pazgjidhura sipas kategorisë + waiver-a; krahasimi i fundit shadow + numërim sipas kategorisë; mosha e raportit më të vjetër/më të ri të përdorimit; periudha që presin përdorim/të shtyra/të afatuara; fatura të lëshuara në ekzekutimin e fundit; periudha `no_charge`; pagesa fature pending/stale; anomali shlyerjeje; ekzekutimi i fundit i workerit (heartbeat) dhe mosha.
**Alarme** (`alerts` në `GET /admin/billing/final-readiness` dhe në mjet; asnjë integrim i rremë me PagerDuty/Prometheus — kodi i daljes ≠ 0 dhe JSON për monitorimin e jashtëm):
| Niveli | Kodi | Kur |
|---|---|---|
| CRITICAL | `dual_issuer_possible` | faturë `provenance=central` ndërsa mode ≠ central |
| CRITICAL | `sequence_collision_risk` | sekuenca (INV/CN) pas numrave ekzistues, ose numër i dyfishtë |
| CRITICAL | `central_authority_with_enterprise_not_frozen` | mode=central pa atestim freeze |
| CRITICAL | `missing_usage_baseline_in_central` / `missing_pricing_in_central` | mode=central dhe kontrolli përkatës FAIL |
| CRITICAL | `invoice_arithmetic_invariant_failure` / `settlement_invariant_failure` | invariant i thyer (çdo mode) |
| CRITICAL | `import_conflict_unresolved_at_cutover` | mode=central me çështje të pazgjidhura |
| WARN | `stale_usage_report`, `shadow_mismatch`, `old_due_period`, `old_pending_invoice_payment`, `unresolved_manual_review_item`, `stale_worker_heartbeat`, `import_waivers_present`, `invoices_overdue` | pragjet e `CENTRAL_BILLING_*` / `CENTRAL_PAYMENT_PENDING_STALE_SECONDS` / `CENTRAL_BILLING_RUN_STALE_SECONDS` |

## 20. Hardening i workerit
- **I kufizuar:** `billing_run --limit N` (default 500 abonime) × `--max-periods` (default 12 per abonim); periudha e shtyrë/në pritje ndalon iterimin e abonimit.
- **Pa mbivendosje të pasigurt:** (1) çdo periudhë në transaksionin e vet me abonimin `FOR UPDATE` + UNIQUE (abonim, indeks) ⇒ dy ekzekutime paralele nuk dyfishojnë fatura/periudha (testuar në PG); (2) `run_exclusive` mban **advisory lock** në nivel sesioni (PostgreSQL) ⇒ ekzekutimi i dytë del me kod 4 `busy` pa bërë punë; SQLite = proces i vetëm (mjedis testimi).
- **Retry idempotent:** rinisja pas dështimit nuk krijon dublikatë. **Crash para commit** ⇒ asnjë fatura, asnjë numër i konsumuar (numri jeton në të njëjtin transaksion) ⇒ pa boshllëk; **crash pas commit** ⇒ fatura ekziston, periudha është `invoiced`, rishikimi e kalon si `not_due`; humbja e heartbeat-it nuk e prish rezultatin.
- **Stale worker:** audit `billing.run` (system, `system:billing_run`, vetëm numërues) ⇒ `billing_worker_heartbeat` WARN pas `CENTRAL_BILLING_RUN_STALE_SECONDS` (26h). Zhdukja e heartbeat-it nuk ndalon faturimin; vetëm alarmon.
- **Rifillim i sigurt:** thjesht rinis `billing_run` (nuk ka gjendje në kujtesë). **Asnjë thirrje rrjeti** brenda transaksionit financiar; as email/SMTP (shih §21).

## 21. Kufiri i dërgimit të faturave
Central **nuk** dërgon fatura (as email, as PDF, as portal): lëshimi i faturës nuk varet nga SMTP/rrjeti. Dërgimi është shqetësim i veçantë (milestone i ardhshëm: M11 klienti/portali); nuk ka outbox faturash në Central për të audituar. Operatori i shpërndan faturat manualisht nga `GET /admin/billing/invoices/{id}` deri atëherë.

## 22. Backup / restore
**Central** (të gjitha në të njëjtin dump/PITR; konsistencë e vetme): `commercial_plans, plan_versions, billing_profiles, billing_subscriptions, billing_periods, invoices, invoice_lines, invoice_number_sequence, payments, invoice_payment_allocations, credit_notes, credit_note_sequence, billing_usage_reports, billing_usage_baselines, billing_import_batches/items/issues, billing_authority_state, billing_shadow_comparisons, audit_log` (+ tabelat e parave M9). **Enterprise:** `sms_plans/sms_subscriptions/sms_invoices/sms_invoice_lines/sms_payments/sms_invoice_counters` (legacy deri në M13), eventet billable të email-it, outbox-i i raporteve të përdorimit, gjendja e autoritetit/freeze (`SMS_BILLING_AUTHORITY*` në mjedis — rruaj `.env`).
`scripts/backup.sh` për secilën bazë + **PITR/WAL për Central** (bllokues prodhimi, shih `M9_MONEY_AUDIT.md` §11). Restore (`scripts/restore.sh`) vetëm në bazë të re; **rendi:** (1) Central, (2) Enterprise, (3) mjedisi/autoriteti (`SMS_BILLING_AUTHORITY`, Central mode lexohet nga DB), (4) verifikim: `alembic current` në të dyja, `billing_final_readiness --json`, `python -m scripts.verify_ledger`, krahaso `invoice_number_sequence` me numrat, (5) vetëm pastaj rinis workerat (`billing_run` i fundit).

## 23. Skenarë DR (asnjë reset i heshtur i kursorit)
| Skenari | Pasoja | Veprimi |
|---|---|---|
| Central i rikthyer MBRAPA gjendjes së eksportit të Enterprise (para importit/cutover-it) | Central s'ka importin/sekuencën | mbaj `shadow`/`local`; ri-importo artifact-in e fundit (idempotent); `billing_final_readiness` duhet PASS para çdo hapi |
| Central i rikthyer pas lëshimit të faturave (humbje e faturave të fundit) | numra/periudha të humbura; klientët mund t'i kenë parë | **freeze** `billing_run`; krahaso me kopje të faturave të dërguara/audit; ri-krijo vetëm me forward-fix të audituar; sekuenca të mos ulet; rakordim manual — asnjë ri-lëshim i verbër |
| Enterprise i rikthyer para baseline-it të cutover-it | Enterprise mund të rilëshonte | MBAJ `SMS_BILLING_AUTHORITY=central` (freeze) para se të hapet trafiku; verifiko `periods_billed` kundrejt Central (Central autoritet) |
| Enterprise i rikthyer pas cutover-it | gjendje legacy e vjetër, pa ndikim në faturim | freeze mbetet; kërkon rishikim vetëm për përdorimin (outbox-i i raporteve ridërgohet idempotent) |
| Sekuenca e humbur | rrezik përplasjeje numrash | `inv_invoice_sequence_not_behind` FAIL ⇒ CRITICAL; ngre sekuencën mbi maksimumin real VETËM përmes seed-it të importit/procedurës së audituar; kurrë ulje |
| Baseline përdorimi i humbur | faturim overage i pasaktë | `usage_opening_baseline` FAIL ⇒ periudha shtyhet (`postponed`); ri-importo artifact-in (baseline është idempotent) |
| Artifact i dyfishuar | ri-import | `export_id` UNIQUE ⇒ no-op; i njëjti id me hash tjetër ⇒ refuzim |
| Kursor periudhe i prapambetur (`next_period_index` < periudhat e faturuara) | rrezik rifaturimi | UNIQUE (abonim, indeks) + `invoices` unike bllokojnë dublikimin; çdo korrigjim kërkon vendim të audituar, jo UPDATE manual |

## 24. Rishikimi i sigurisë (admin APIs faturimi)
Verifikuar (testuar në `test_m9g5_closure.py`): (a) metoda vetëm GET/POST — **asnjë DELETE/PUT/PATCH**; (b) çdo trup POST është model Pydantic `extra="forbid"` (≥10 kontrolluar); (c) shumat janë string dhjetorë (kurrë float), UUID-të në shteg si `uuid.UUID`, `Reason` 1..500, `idempotency_key` i detyrueshëm për pagesa/credit notes, arsye ≥3 për void; (d) admin = shkrim, operator = lexim (operatori merr 403 në POST); (e) çdo mutacion shkruan audit; (f) `final-readiness` dhe `ops` janë GET, pa PII dhe pa mutacion; (g) asnjë qasje ndër-enterprise pa filtër të shprehur (listat marrin `enterprise_id` si filtër admin; s'ka API klienti deri në M11); (h) kredencialet e shërbimit: skopi minimal `billing:report` për ingest-in e përdorimit, asnjë skop faturimi tjetër. Gjetje: abonimi `assign/cancel/resume` nuk ka çelës idempotence por janë të gjendjes (idempotente nga natyra); pranohet.

## 25. Invariantet (verifikim i vazhdueshëm: `inv_*` në readiness)
një periudhë për (abonim, indeks) · total faturë = linja + tatim (aritmetikë DB + `verify_invoice`) · faturë e lëshuar e pandryshueshme (trigger) · paid/void terminale · një alokim për faturë, pagesa e miratuar = totali dhe monedha · credit notes kumulativ ≤ total i faturës së paguar · asnjë mutacion wallet SMS / ledger komercial nga pagesa fature · asnjë dublim lëshuesi (`inv_no_central_invoice_outside_central_mode`) · asnjë numër i dyfishtë dokumenti · sekuencat ≥ numrat · çdo faturë/pagesë e importuar ka rresht evidence (provenance).

## 26. Mjetet e provës së prodhimit (pa prekur të dhëna reale automatikisht)
`billing_import --artifact … --json` (numra, konflikte, `blocking_by_category`, seed-e, `proposed_baselines`) · `billing_import --issues` · `billing_shadow --summary --json` (read-only) · `billing_final_readiness --json [--strict] [--observability]` (cutover readiness JSON). Të gjitha janë vetëm-lexim (përveç `--apply`/`--resolve`/`billing_shadow` pa `--summary` që shkruan krahasime).

## 27. Kufizime të njohura dhe bllokues të prodhimit
- Kufizime V1: pa proporcion, pa pagesa të pjesshme/mbipagesa, pa gateway, pa dunning, pa tatim/FX, pa dërgim faturash, pa UI klienti; faturat Central janë vetëm admin.
- **Bllokues operativ (jashtë kodit):** (1) PITR/WAL ose replikë sinkrone për Central; (2) `billing_run` i planifikuar (cron/systemd) + `CENTRAL_BILLING_WORKER_CONFIGURED=true` dhe monitorim i jashtëm i `billing_final_readiness`; (3) provë e numrave të vërtetë: dry-run i eksportit real, zgjidhje e çështjeve manuale, shadow ≥ 1 cikël i plotë; (4) ACK prodhimi njerëzor; (5) vendim ligjor për afatin e ruajtjes së faturave; (6) procedurë dërgimi manual i faturave deri në M11; (7) mjedisi i testimit pa DNS ka 4 teste izolimi tenant që duhen rikontrolluar në mjedis të shëndetshëm.
- Testim: DNS i prishur në VM ⇒ `tests/test_tenant_isolation.py` (4 error `socket.gaierror`) raportohet veçmas; riprodhohet identikisht në rev. të miratuar para g4.

## 28. Defekt i gjetur dhe i korrigjuar në g5
Çështja manuale për baseline (`usage_baselines`) përdorte `source_id = <uuid>:<boundary ISO>` (69 shenja) në kolonë `varchar(64)`: në PostgreSQL `apply` me baseline të pamjaftueshëm dështonte me `StringDataRightTruncation` (SQLite nuk e imponon gjatësinë). Çelësi tani është `<uuid hex>:<epoch>` (43 shenja); i mbuluar nga `test_baseline_issue_is_never_waivable[postgres]`. Asnjë të dhënë prodhimi s'ishte importuar me çelësin e vjetër.
