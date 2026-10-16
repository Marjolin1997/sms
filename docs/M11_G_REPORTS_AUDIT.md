# M11-G — Reports / Audit UX

Faqe vetëm-lexim mbi të dhëna ekzistuese. Asnjë veprim shkrimi, asnjë ndryshim i semantikës SMS/faturimi/sender-ave.
Frontend-i nuk llogarit asnjë metrikë autoritative: çdo numër vjen nga serveri (agregate të grupuara, jo faqe të papërpunuara).

## 1. Harta: ekran → burim → faqosje/filtra → roli → boshllëqe

| Ekran | Burimi (endpoint ← tabelë) | Faqosje / filtra | Roli | Boshllëqe |
|---|---|---|---|---|
| **Enterprise · Reports · Overview** | `GET /v1/reports/overview` ← `sms_messages`, `sms_campaigns`, `sms_emails` (GROUP BY) | `from`/`to` (ditë UTC, max 366) | klient `reports:read`; stafi me `owner_ref` | — |
| Reports · Campaigns | `GET /v1/reports/campaigns` ← `sms_campaigns` + 3 pyetje të grupuara (`campaign_views.page_summaries`) | keyset `before_id`, `limit`≤201, `status`, `q`, `from`/`to` (data e krijimit) | si më sipër | pa kolonë "last activity" (nuk ruhet; tregohen `created/started/completed`) |
| Reports · Delivery & failures | `GET /v1/reports/failures` ← `sms_messages` GROUP BY (status, error_code) | `from`/`to`, `campaign_id` (i huaj ⇒ 404) | si më sipër | pa ofrues/rrugë/operator për klientin (shih §5); kategoria = paraqitje e `error_code` te UI |
| Reports · Usage | `GET /v1/reports/usage` (ekzistues) ← `sms_messages`, `sms_emails` | `from`/`to` | si më sipër | pa gjendje reconciliation (është e Central, jo e klientit) |
| **Enterprise · Activity log** | `GET /v1/reports/audit`, `/audit/facets` ← `sms_audit_log` (kolona e re `owner_ref`) | keyset `before_id`, `from`/`to`, `action`, `target_type` | si më sipër | rreshtat para 0034 nuk kanë tenant ⇒ nuk shfaqen kurrë; veprimet e stafit pa `detail.owner` nuk shfaqen |
| Eksporte Enterprise | `/v1/reports/{messages,emails,campaigns,usage,audit}.csv`, `/campaigns/{id}/recipients.csv` | filtrat e ekranit; kufi rreshtash | si më sipër | — |
| **Central · Audit · Sender decisions** | `GET /admin/audit/sender-decisions` ← `sender_decisions` ⋈ `sender_registry` ⋈ `enterprises` ⋈ `users` | offset/limit≤200; `decision`, `category`, `source`, `country`, `enterprise_id`, `registry_id`, `from`/`to` | admin + operator | — |
| Central · Commercial audit | `GET /admin/audit?scope=commercial` ← `audit_log` | si më poshtë | admin + operator | pagesat/grant/credit-note e regjistrojnë vendimtarin edhe te rreshti i tyre (lidhje te faqet ekzistuese); `money_events` mbulon vetëm grant issued/reversed dhe s'ekspozohet |
| Central · Staff activity | `GET /admin/audit`, `/audit/facets` ← `audit_log` | offset/limit≤200; `scope`, `action`, `resource_type`, `actor_kind`, `actor_id`, `from`/`to` | admin + operator | veprimet që s'shkruajnë `audit_log` s'shfaqen (p.sh. leximet) |
| Central · Operations state | `GET /admin/operations/sender` (ekzistues, M11-D) | — | admin + operator | **vetëm gjendja e fundit** (M11-D2): s'ka histori/trend; ekrani e thotë shprehimisht |
| Eksporte Central | `/admin/audit.csv`, `/admin/audit/sender-decisions.csv` | filtrat e ekranit; max 50 000 rreshta | admin + operator | pa eksport fature/pagese/reconciliation/alarmesh (borxh) |

## 2. Ndryshime në backend (vetëm shtesa vetëm-lexim)

- **Migrim Enterprise `0034`** (aditiv): `sms_audit_log.owner_ref` (nullable) + indeks `ix_sms_audit_owner (owner_ref, id)`.
  Pa këtë kolonë audit-i nuk skopohet dot për tenant-in. `audit()` e plotëson nga principal-i klient ose nga `detail.owner`; histori e vjetër mbetet NULL
  (pa rindërtim) dhe klienti nuk e sheh kurrë (fail-closed). Up/down/up dhe `compare_metadata`=0 janë në test.
- `app/services/csv_export.py`: shërbimi i vetëm i CSV (BOM UTF-8, apostrof për `= + - @ TAB CR`, numrat/telefonat e pandryshuar, grupe 500 rreshtash, `require_bounded`).
- `app/services/report_views.py`: agregatet e raporteve (statuse PUBLIKE: UNKNOWN → `sending`; `delivery_rate` = delivered ÷ (delivered + failed/bounced)).
- `app/api/reports.py`: endpoint-et e reja; `messages.csv` merr filtra `status`, `campaign_id`, `sender`, shfaq statuset publike (më parë shfaqte `unknown` të papërpunuar), dhe tejkalimi i kufirit është **422 `export_too_large` para trupit** (më parë prerje e heshtur në 100 000).
- Central: `apps/central/services/audit_reports.py` + `apps/central/api/admin_audit.py` (GET-only; `scope` = prefikse veprimesh në një dict të vetëm; `detail` kalon nga pastruesi i sekreteve).

Asnjë modul hot-path (submit, process_one, dispatch, sync, reporter, money) nuk importon `report_views`/`csv_export` (pin në test).

## 3. Kufijet e eksportit (sinkron, pa punë asinkrone)

| Eksport | Kufi |
|---|---|
| Enterprise messages / emails / audit / recipients | 100 000 rreshta (422 mbi kufi) |
| Enterprise campaigns | 20 000 rreshta |
| Intervali i datave | 366 ditë (422) |
| Central audit / sender decisions | 50 000 rreshta (422 mbi kufi) |

Mbi kufi: gabim i qartë, kërkohen filtra/data më të ngushta; nuk nis asnjë shkarkim.

## 4. Semantika e datave

Të gjitha `from`/`to` janë ditë kalendarike **UTC**, të dyja skajet përfshihen (`[from 00:00Z, to+1d 00:00Z)`). Parazgjedhja: 30 ditët e fundit. `from>to` dhe >366 ditë ⇒ 422 (UI e bllokon para kërkesës).

## 5. Privatësia

- Audit klienti: aktori vetëm `you` / `staff` / `system`; `detail` kalon nga lista e lejuar (`at, version, list, fields, from, to, reason, purpose`); veprimet `cross_tenant.*` fshihen; asnjë token, URL, email stafi, DB-id e të tjerëve.
- Audit Central: identiteti i stafit sipas modelit të auth (email i përdoruesit; `system:<emër>` për procese); `detail` pa çelësa `secret|token|password|hash|signature|api_key|private|credential`, vargje >300 karaktere të prera.
- Telefonat në CSV: si më parë (të dukshëm për rolet e autorizuara, nuk zgjerohet).
- Ofrues/rrugë/operator: **nuk** ekspozohen te klienti (rregullat e rrugëzimit janë të brendshme).

## 6. Performanca (PostgreSQL 16, sintetike)

Enterprise: 100 000 kontakte, 1 000 campaign, 120 000 mesazhe/recipient, 100 000 rreshta audit.

| Pamje | Pyetje | Koha |
|---|---|---|
| overview (30d / 366d) | 9 / 9 | ~200 / ~220 ms |
| campaign report faqe / status+q / keyset i thellë | 5 / 5 / 5 | ~160 / ~80 / ~70 ms |
| failures / sipas campaign-it | 3 / 4 | ~55 / ~75 ms |
| usage | 4 | ~80 ms |
| audit list / filtër / keyset i thellë | 3 / 3 / 3 | ~17 / ~22 / ~21 ms |
| audit facets | 4 | ~100 ms |
| messages.csv (58 699 rreshta) | 5 | ~4.6 s |
| audit.csv (40 674) | 5 | ~2.2 s |
| campaign recipients.csv (20 000) | 6 | ~1.1 s |

Central: 100 000 rreshta `audit_log`, 50 000 vendime, 25 000 sender.

| Pamje | Pyetje | Koha |
|---|---|---|
| audit list (e gjitha / scope / data / veprim) | 2 | 40–80 ms |
| audit offset 20 000 | 2 | ~90 ms |
| sender decisions (pa filtër / me filtra / offset 20 000) | 2 | ~115 / ~65 / ~165 ms |
| audit.csv (8 127) / sender-decisions.csv (4 021) | 3 | ~0.8 / ~0.6 s |

Numri i pyetjeve nuk varet nga numri i rreshtave (pin në teste). Plani: renditjet përdorin top-N heapsort mbi seq scan (Central) — **nuk u shtua indeks**
(≤ 165 ms në 100k); kur `audit_log` kalon disa milionë rreshta, një indeks `(created_at DESC, id DESC)` bëhet i justifikuar. Enterprise: `ix_sms_messages_owner_created`
ekzistues mbulon agregatet; `sms_audit_log` përdor indeksin e ri/PK. `facets` është seq scan mbi audit-in e tenant-it (~60 ms në 83k rreshta).

## 7. Çfarë NUK ka (dhe nuk u shpik)

Histori operative e Central (vetëm gjendja e fundit), grafik trendi operativ, ofrues/rrugë/operator për klientin, audit i leximeve, retry/resend, eksport faturash/pagesash/alarmesh në Central,
rreshta audit Enterprise para 0034 (pa tenant), segmente, punë eksporti asinkrone.
