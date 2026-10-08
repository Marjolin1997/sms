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
