# M11-E2.1 — Snapshot i lëshuesit te faturat Enterprise

## Audit: fusha e renderuar → burimi i mëparshëm → fusha e ruajtur
| Fusha e faturës | Burimi para M11-E2.1 | Fusha e re (`sms_invoices.issuer`, JSON) |
|---|---|---|
| emri i lëshuesit | `settings.issuer_name` (`SMS_ISSUER_NAME`) në çastin e renderimit | `name` |
| adresa | `settings.issuer_address` | `address` |
| ID tatimore / TVSH | `settings.issuer_tax_id` | `tax_id` |
| vendi | nuk ekziston në konfigurim dhe nuk renderohej | — (nuk shpikim fusha) |

Vetëm këto tri fusha shfaqeshin te HTML/PDF; të tjera nuk shtohen.

## Skema
`issuer` Text, nullable, JSON — simetrike me `bill_to` (Text/JSON). Pa indeks, pa NOT NULL, pa rishkrim tabele. Migrimi `0033` (up/down/up, `compare_metadata = 0`).
Pandryshueshmëria: `issuer` hyn te `_FROZEN` (ORM, `InvoiceImmutableError`) dhe te funksioni PG `sms_invoice_guard` (çdo ndryshim, edhe NULL→vlerë, refuzohet).

## Rrugët e krijimit
`Invoice(...)` instancohet vetëm te `billing.generate_invoice` (faturimi periodik; i vetmi krijues në `app/`). Test statik e verifikon dhe kërkon `issuer`+`bill_to`.
Eksporti drejt Central (`billing_export`) nuk ndryshon (kontrata e tij s'e përfshin issuer-in).

## Faturat legacy (para migrimit)
`issuer = NULL`. NUK plotësohen prapa: historia e konfigurimit të lëshuesit nuk ruhet askund, ndaj çdo backfill do të paraqiste si "historike" një të dhënë të pavërtetuar.
`invoice_issuer.issuer_of` kthen për to **fallback eksplicit** te `SMS_ISSUER_*` (provenienca `legacy_fallback`, e brendshme, jo te API i klientit). Pra një faturë legacy ndjek ende konfigurimin aktual — kjo është e njohur dhe e dokumentuar, jo e fshehur.

## Një burim i vetëm
API (detaji merr fushën additive `issuer`), HTML dhe PDF e marrin lëshuesin nga `invoice_issuer.issuer_of(invoice)`: snapshot-i i ruajtur kur ekziston, përndryshe fallback legacy. Kur ka snapshot, konfigurimi aktual nuk lexohet fare (testuar).
Lista e faturave dhe UI nuk ndryshojnë.
