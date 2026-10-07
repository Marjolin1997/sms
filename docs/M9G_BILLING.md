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
