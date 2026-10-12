# M11-E2 — Boshllëqet vetëm-lexim të faturimit / PDF zyrtar

Vetëm lexim: asnjë endpoint ose veprim financiar shkrues. Asnjë logjikë biznesi financiare nuk u ndryshua.

## Auditimi: boshllëk → burimi i së vërtetës → ndryshimi minimal

| Boshllëk | Burimi ekzistues | Ndryshimi |
|---|---|---|
| Filtër sipas numrit të faturës | `invoices.number` (unik, `INV-<vit>-<n>`) | `GET /admin/billing/invoices?number=` — parafytyrë (hyrja bëhet me shkronja të mëdha, `[A-Za-z0-9._-]`, ≤24; `_` nuk është wildcard) |
| Filtër sipas datave | `invoices.issued_at` (timestamptz) | `issued_from`, `issued_to` — DATA kalendarike UTC, të dyja përfshirëse: `issued_at >= from 00:00Z` dhe `< (to+1) 00:00Z`; `from > to` ⇒ 422 `invalid` (kurrë këmbim i heshtur); datë e pavlefshme ⇒ 422 sipas fushës |
| `enterprise_id` te pagesat e kreditit | `payments.enterprise_id` | `GET /admin/money/payments?enterprise_id=`; UUID i pavlefshëm ⇒ 422, i panjohur ⇒ listë bosh |
| Treguesi credit-note te lista | `credit_notes.invoice_id` (+ `ix_credit_notes_invoice`) | fusha additive `credit_note_count`, `credited_total` nga NJË pyetje e grupuar për faqen (3 SELECT në total për 1 apo 4 fatura) |
| "Pending recalculation" te bilanci Enterprise | `sms_money_grants.status ∈ UNRESOLVED` (`reconciliation_required`, `baseline_mismatch`, `unmapped`) | `GET /v1/wallets` merr `has_pending_recalculation` (bool) nga një pyetje për të gjitha wallet-et e tenant-it |
| PDF zyrtar | HTML ekzistues (`invoice_render`) — jo zyrtar | `GET /v1/billing/invoices/{id}/pdf` (klient) dhe `GET /admin/billing/invoices/{id}/pdf` (Central), të dyja mbi një renderues të përbashkët `packages/invoice_pdf` |

## Semantika e dates
`issued_at` është timestamptz (UTC). Filtri punon me ditë kalendarike UTC, jo me ora lokale të shfletuesit. Datat e kontabilitetit në UI (periudha, afati)
shfaqen si datë në UTC.

## Pending recalculation (vetëm shfaqje)
- **Krijohet** kur aplikuesi i `cp.money.v1` regjistron një grant që s'mund të aplikohet sigurt: reversal pa fonde të mjaftueshme (`reconciliation_required`),
  bootstrap që s'përputhet me baseline (`baseline_mismatch`) ose produkt/wallet pa mapim (`unmapped`) — kodi te `app/services/money_sync.py`.
- **Pastrohet** kur statusi del nga ky grup (zgjidhje nga operatori/rakordimi); sot s'ka rrugë automatike në kod.
- **Bilanci mbetet i disponueshëm dhe i saktë sipas ledger-it**: këto statuse s'mutojnë ledger-in dhe s'e bllokojnë shpenzimin (përdoren vetëm nga readiness/operatori).
  Prandaj UI e shfaq shumën, por e etiketon "Pending recalculation" dhe nuk e paraqet si përfundimtare. Frontend-i nuk e nxjerr kurrë këtë gjendje vetë.
- Kufizim: grantet `unmapped` pa `wallet_id` nuk lidhen me asnjë wallet, ndaj nuk shfaqen te klienti.

## PDF
- **Biblioteka:** `reportlab` (Python i pastër, e vlefshme për Docker image-in ekzistues, pa Chrome/headless). Shtuar te `requirements.txt` dhe `pyproject.toml`; `pypdf` vetëm si varësi testimi.
- **Fontet:** DejaVu Sans (regular + bold) të bashkangjitura në `packages/invoice_pdf/fonts/` (licenca përfshirë). Shqipja, gjermanishtja, ë/ç/ß/ü/ö etj. printohen njëlloj kudo; asnjë font i makinës.
- **Pa rillogaritje:** renderuesi merr tekste të gatshme (shumat janë stringjet dhjetore të ruajtura); formatimi i vetëm është grupimi i mijëshave. Test me total të pazakontë (`≠ nëntotal + tatim`) e provon.
- **Snapshot historik:** Central përdor `issuer`/`bill_to` të ruajtur në faturë (ndryshimi i profilit s'e ndryshon PDF-në; e njëjta faturë = të njëjtat bajte, `invariant=1`).
  **Kufizim Enterprise:** tabela `sms_invoices` ruan snapshot vetëm të `bill_to`; lëshuesi (emër/adresë/ID tatimore) vjen nga konfigurimi aktual `SMS_ISSUER_*`, njëlloj si faqja HTML ekzistuese.
  Një snapshot i lëshuesit kërkon ndryshim skeme në Enterprise — mbetet debt.
- **Siguria:** Enterprise — vetëm fatura e tenant-it (e huaj ⇒ 404, `owner_ref` s'e ngre klientin); Central — admin|operator. Emri i skedarit sanitizohet (`invoice-[A-Za-z0-9._-]{1,48}.pdf`), asnjë shteg/hyrje skedari nga klienti,
  `Cache-Control: private, no-store`, `X-Content-Type-Options: nosniff`. Dështim renderimi ⇒ log `invoice pdf render failed` + 500 JSON `pdf_render_failed` pa tekst të brendshëm; buffer-i ndërtohet i plotë para përgjigjes (asnjë PDF i pjesshëm).
- `?inline=true` kthen `Content-Disposition: inline`; parazgjedhja është `attachment`.

## Indekse (migrimi Central `0031`, additiv, up/down/up, `compare_metadata = 0`)
EXPLAIN ANALYZE në PostgreSQL 16 me 300 000 fatura sintetike (LIMIT 51, renditje `issued_at DESC, id DESC`):

| Pyetje | Pa indeks | Me indeks |
|---|---|---|
| lista pa filtër | 87 ms (Seq Scan + sort) | 0.09 ms (`ix_invoices_issued_at`, skanim prapa) |
| interval datash | 13.5 ms | 0.08 ms |
| parafytyrë numri | 26 ms (Seq Scan) | 0.11 ms (`ix_invoices_number_prefix`, `varchar_pattern_ops`) |
| status + datë | 18.9 ms | 0.08 ms |

Pagesat e kreditit sipas `enterprise_id` nuk morën indeks: tabela është e vogël (pagesa manuale/bankare); mbetet e mbuluar nga filtri ekzistues + faqosja.

## Performancë
PDF: 2 rreshta ≈ 7.5 ms / 45 KB; 50 rreshta ≈ 31 ms / 49 KB; 300 rreshta ≈ 160 ms / 67 KB. Pyetje: 4 (Central, përfshirë auth) / 5 (Enterprise), asnjë shkrim (përveç `last_used_at` i çelësit API që ekziston).
`GET /v1/wallets`: +1 pyetje për të gjitha wallet-et. Asnjë modul i rrugës së dërgimit (submit/process_one) nuk preket.
