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
