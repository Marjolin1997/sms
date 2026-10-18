# M11-F — Campaigns / Contacts / Messaging UX

Frontend-i pasqyron të vërtetën e backend-it. Nuk llogarit eligibilitet marrësish, autorizim
sender-i, kredit, çmim, rate limit, queue, retry, DLR apo makinën e gjendjeve të campaign-it.

## Backend (vetëm shtesa read-only, pa ndryshim semantike SMS)

| Endpoint | Shtesa |
|---|---|
| `GET /v1/campaigns` | filtra `status`, `q` (emër), `before_id`, `limit` (≤201); çdo rresht + `recipients_total`, `recipient_counts`, `message_counts` (≤3 query të grupuara për faqen). Kthen ende array. |
| `GET /v1/campaigns/{id}` | + `created_at, text, subject, from_email, list_name, window_*, utc_offset_minutes` |
| `GET /v1/campaigns/{id}/recipients` | + `address, name, message_public_id, message_status, error_code, updated_at` (2 query, pa N+1) |
| `GET /v1/campaigns/{id}/activity` | e re; vetëm ngjarje reale nga `AuditLog`; aktori s'ekspozohet (`you`/`staff`) |
| `GET /v1/messages` | + `campaign_id`, `sender`, `created_from`, `created_to` (ditë UTC gjithëpërfshirëse; from>to → 422) |
| `GET /v1/lists` | + `created_at`, `member_count` (një query e grupuar) |

`UNKNOWN` kalon gjithmonë nga `public_status()` ⇒ klienti sheh `sending`, kurrë `failed`.
Asnjë modul i hot-path nuk importon `campaign_views` (pin në test). Asnjë rrugë mutuese e re.

## Frontend

- Contacts: kërkim/filtër me debounce + anulim kërkesash të vjetra, keyset "Load more", filtër lista, gjendja në URL.
  Telefoni nuk normalizohet në klient (vetëm heq hapësira/()/.-); gabimet vijnë nga serveri.
- Import: grupe 1000 rreshta (max 10 000), gjendje idle/importing/done/failed, ≤50 gabime të shfaqura, pa echo të CSV.
- Lists: numërues dhe audiencë nga serveri; pa segmente (backend nuk i mbështet).
- Campaigns: listë me filtra në URL + keyset; krijim 5 hapa (Details, Recipients, Message, Schedule, Review);
  sender-at vetëm të miratuar nga `/v1/sender-ids`; skedulim me zonë kohore të qartë, instanti UTC i saktë në review,
  data e kaluar refuzohet; mbrojtje nga dërgim i dyfishtë; mungesë balance → mesazhi i serverit + lidhje te Billing.
- Detail: Overview / Recipients / SMS traffic / Activity (`tab` në URL). Pause/resume/cancel vetëm ku lejon serveri.
  Nuk ka retry/resend (backend s'e ka ⇒ pa buton), nuk ka grafik trendi.
- Messages: filtra në URL, kategori dështimi nga `error_code`, timeline vetëm nga ngjarje reale.
- i18n EN+SQ (`campaigns2.js`), responsive deri 390 px.

## Performanca (SQLite, sintetike: 100k kontakte, 1k campaign, 100k recipient/mesazh)

| Pamje | Query | Koha |
|---|---|---|
| contacts faqe / kërkim / në listë | 3 / 3 / 3 | 9 / 20 / 9 ms |
| lists (numërues) | 4 | 15 ms |
| campaign list (26) / status+q | 5 / 5 | ~105 ms (faqja përmban campaign me 100k recipient) |
| campaign detail (100k recipient) | 7 | 87 ms |
| recipients faqe / keyset i thellë | 6 / 6 | 25 / 27 ms |
| messages sipas campaign / status / datë | 3 / 3 / 3 | 47 / 11 / 84 ms |
| activity | 4 | 9 ms |

Numri i query-ve nuk rritet me rreshtat (pin në `tests/test_m11f_messaging_api.py`).

## Pa SMS reale

E2E përdor DB të përkohshme, rrugë `fake` me `FakeProvider` në proces, pa worker dhe pa provider real;
`e2e/test_messaging.py` verifikon që nuk ekziston proces worker dhe asnjë thirrje e jashtme.

## Kufizime

Pa segmente të ruajtura, pa retry/resend, pa API statusi pune për importin (sinkron, në grupe nga klienti),
pa kolonë "created" në listën e campaign-eve, pa filtër vendi/vlefshmërie për kontaktet.
