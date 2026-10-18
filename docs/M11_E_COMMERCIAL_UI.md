# M11-E — Commercial / Billing UI (vetëm-lexim)

Konsola Central (frontend-central) merr grupin **Commercial**; Enterprise merr detajin e faturës së klientit.
Frontend-i shfaq të vërtetën e backend-it: asnjë formulë bilanci, çmimi, TVSH-je, shlyerjeje apo gatishmërie nuk llogaritet në klient.

## Harta: ekran → endpoint → faqosje/filtra → rolet → boshllëqe

| Ekran (Central) | Endpoint-e (GET) | Faqosje / filtra | Rolet | Boshllëqe të njohura |
|---|---|---|---|---|
| Overview `#com` | `/admin/financial/overview`, `/admin/billing/ops` | e kufizuar (agregate) | admin, operator | `overview` përfshin rakordim të plotë (kosto e njohur, 45 s polling) |
| Credit & Grants `#com-credit[/id]` | `/admin/money/accounts[/id[/ledger]]`, `/admin/money/grants`, `/admin/money/usage-reports` | offset/limit (`has_more`); ledger me `after_seq` | admin, operator | filtri sipas `enterprise_id` vetëm te llogaritë; grantet filtrohen sipas `account_id`/`status` |
| Payments `#com-payments` | `/admin/money/payments`, `/admin/billing/invoice-payments` | offset/limit, `status`, `account_id` / `enterprise_id`, `invoice_id` | admin, operator | pagesat e kreditit nuk kanë filtër `enterprise_id` |
| Pricing `#com-pricing` | `/admin/pricing/books[/id]`, `/versions/{id}`, `/assignments`, `/state`, `/preview` | offset/limit, `enterprise_id`, `product_id` | admin, operator | parapamja përdor `/preview` të serverit (asnjë përputhje rregullash në JS) |
| Subscriptions `#com-subs[/enterprise]` | `/admin/billing/subscriptions[/id]`, `/profiles/{id}`, `/plan-versions/{id}`, `/periods`, `/plans[/id]` | offset/limit, `status`, `enterprise_id` | admin, operator | nuk ka të dhëna "included usage / overage" në Central (vetëm tarifë dhe email të përfshirë të planit) |
| Invoices `#com-invoices[/id]` | `/admin/billing/invoices[/id]` (+ `settlement`), `/credit-notes` | offset/limit, `status`, `enterprise_id` | admin, operator | **nuk ka filtër për numër faturë apo interval datash**; lista nuk ka tregues credit-note (do kërkonte N+1) |
| Reconciliation `#com-recon` | `/admin/money/reconciliation`, `/usage-reports`, `/admin/billing/settlement`, `/admin/financial/unresolved-reversals` | `min_severity` | admin, operator | — |
| Readiness `#com-ready` | `/admin/financial/readiness`, `/admin/pricing/readiness`, `/admin/billing/final-readiness` | — | admin, operator | tre burime të pavarura; asnjë status i përgjithshëm nuk shpiket |

Enterprise (klient): `GET /v1/wallets` (bilanci), `/v1/billing/subscription` (plani + përdorimi), `/v1/billing/invoices[/id]` (+ `/html` zyrtare), `/v1/billing/payments`. Të gjitha të ngushtuara sipas tenant-it.
Boshllëqe Enterprise: nuk ka fushë "pending recalculation" në API; PDF zyrtare nuk ka (ka vetëm faqe HTML me CSP që printohet).

## Rregulla të frontend-it

- **Para:** string dhjetor nga backend-i, formatim vetëm me string (`commercial/format.js`, `frontend/src/money.js`); kurrë `Number`. Të paktën 2, deri në 6 decimale; shenja dhe monedha ruhen.
- **Data:** datat kontabël (periudha, afati) shfaqen vetëm si datë në UTC; momentet shfaqen të sakta në UTC. Zona e shfletuesit nuk zhvendos datat.
- **Statuse:** `level()` pranon vetëm PASS/WARN/FAIL/CRITICAL; çdo gjë tjetër (ose burim që s'ngarkohet) është `unknown`, kurrë `pass`. Statuset e panjohura të faturave/pagesave shfaqen neutrale.
- **Gabime:** 403/404/429/5xx shfaqin tekst të përgjithshëm (asnjë stack trace, payload ofruesi); 409/422 vetëm mesazhet e kuruara të domenit.
- **Vetëm-lexim:** asnjë buton miratimi/refuzimi/grant/void/credit-note/caktimi/publikimi. Testet dhe E2E verifikojnë që dërgohen vetëm GET.

## Debt / propozim

- Filtra për numër/interval datash te faturat dhe `enterprise_id` te pagesat e kreditit (GET + indeks, pa semantikë të re).
- PDF zyrtare e faturës dhe fushë "pending recalculation" te bilanci Enterprise (kërkojnë backend).
- Veprimet financiare shkruese (miratim pagese, grant, void, credit note, çmime, abonime) — slice i veçantë me miratim të veçantë.
