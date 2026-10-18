# Kontrata publike V1 — webhook dalës (M3-d, e mbyllur)

Burimi i së vërtetës: `app/contracts/{events,signature}.py` (stdlib-only) + golden `tests/golden/webhooks/`.
Kjo faqe përshkruan atë që kodi bën sot; nuk premton më shumë.

## 1. Envelope (`EventEnvelopeV1`)
```json
{"created_at":"2030-01-01T12:00:00+00:00","data":{"resource_id":"…","resource_type":"message","status":"sent"},"id":"evt_812","type":"message.sent"}
```
Katër fusha: `id`, `type`, `created_at`, `data`. Nuk ka fushë `version` (ruan byte-compatibility).
- **Serializim:** `json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")`.
- **Renditja:** çelësat alfabetikë (kodpoint) në çdo nivel; listat ruajnë renditjen. Pa hapësira jashtë stringjeve.
- **ASCII:** çdo karakter jo-ASCII del `\uXXXX` (emoji si surrogate pair); bytes janë vetëm ASCII.
- **`created_at`:** UTC, `isoformat()` me `+00:00` (jo `Z`); mikrosekonda vetëm kur ≠ 0 (gjatësi e ndryshueshme: marrësit s'duhet ta parsojnë me gjatësi fikse). Naive trajtohet si UTC; offset konvertohet në UTC.
- **`id`:** `evt_<n>`, n = id i rreshtit të event-it (lokal për DB-në; jo global).
- **`data`:** objekt i hapur (str/int/bool/null/list/objekt); nuk validohet nga kontrata. Producer-at sot japin skalarë; para-shumat janë stringje të kaluara verbatim (shkalla = e producer-it); `null` ruhet si `null`. `data=None` ≡ `{}`.
- **Fushat e burimit:** mapper-i shton `resource_type` dhe `resource_id` në `data`, pastaj `**ev.data`.
- **Borxh i hapur:** `ev.data` MBISHKRUAN `resource_type`/`resource_id` në përplasje (sjellje kompatibiliteti, e karakterizuar nga test; asnjë producer sot nuk e bën). Ndryshohet vetëm me ndryshim të versionuar (rezervim çelësash ose refuzim).
- **Katalogu:** `PUBLIC_EVENT_TYPES_V1` (21 tipe, frozenset). Filtrat e abonimit: `*`, `<prefix>.*`, tip i saktë. Eventet e brendshme (`MessageEvent`, `EmailEvent`, `AuditLog`, `DlrReceipt`, queue) nuk janë pjesë e kontratës.

## 2. Nënshkrimi V1
`X-SMS-Signature: t=<unix>,v1=<hex lowercase>`, `v1 = HMAC-SHA256(secret, f"{t}." + body)` ku `secret` është stringu i plotë `whsec_…` (UTF-8) dhe `t` = koha e dërgimit (jo e event-it). Verifikimi i rekomanduar: tolerancë 300s, krahasim timing-safe. Parser-at duhet të injorojnë pjesët e panjohura (p.sh. një `v2=` i ardhshëm).
Kontratë e ndarë: DLR hyrës (`sha256=<hex>`) nuk lidhet me këtë.

## 3. Headers dhe dërgimi
`Content-Type: application/json`, `User-Agent: sms-platform-webhooks/1`, `X-SMS-Signature`, `X-SMS-Event-Id: evt_<n>`, `X-SMS-Delivery-Id: <int>`.
- **At-least-once:** një event mund të vijë më shumë se një herë (retry me vonesa në rritje, crash pas HTTP OK, replay manual). Marrësi bën dedup me `X-SMS-Delivery-Id` / `X-SMS-Event-Id`; ne nuk garantojmë exactly-once.
- **Retry/replay:** body, `X-SMS-Event-Id` dhe `X-SMS-Delivery-Id` mbeten të njëjtë; ndryshon vetëm `t` dhe prandaj signature.
- `X-SMS-*-Id` NUK nënshkruhen (input i nënshkrimit = `t` + body). `rotate_secret` e zëvendëson sekretin menjëherë (pa grace).

## 4. Versionimi
- **V1** ekziston si version kodi; asnjë `version` në payload.
- **Shtime të pajtueshme** (fusha të reja në `data` ose çelës i ri): vetëm të qëllimshme dhe të rishikuara; ndryshojnë bytes, ndaj golden përditësohet vetëm me miratim kontrate. Marrësit duhet të injorojnë çelësat e panjohur.
- **Ndryshime breaking:** `EventEnvelopeV2` + mekanizëm opt-in/schema-version për endpoint në një fazë të ardhshme.
- **Nënshkrimi:** `v1=` mbetet; `v2=`/dual-sign janë dizajn i ardhshëm, jo implementim.

## 5. Politika e golden
`tests/golden/webhooks/*` NUK rigjenerohen automatikisht. Çdo ndryshim kërkon: (1) rishikim eksplicit ndryshimi kontrate, (2) arsye, (3) analizë pajtueshmërie prapa, (4) vendim versionimi, (5) pastaj rigjenerim/përditësim. `regenerate.py` është manual-only; testet nuk e thërrasin.

## 6. Rregulla e paketës `app/contracts`
Vetëm stdlib. Nuk importon `app.*` (core/models/services/api), sqlalchemy, fastapi, pydantic, httpx, starlette; importet relative ndalohen (asnjë nevojë sot). Guard AST: `tests/test_contracts_events.py`, `tests/test_contracts_signature.py`.

## 7. Borxhe të njohura (të pandryshuara me vendim)
- Mbishkrimi i `resource_*` nga `ev.data` (§1).
- `contracts.events._as_utc` dhe `core.timeutil.as_utc` janë të dyfishuara qëllimisht për të ruajtur leaf-in; testi i barazisë mbetet; pa paketë utils të përbashkët.
- `EventEnvelopeV1` është frozen por `data` është dict i ndryshueshëm: fushat nuk ri-caktohen; thirrësi e trajton `data` si immutable pas ndërtimit; mapper-i krijon dict të ri. Pa `MappingProxyType`/deep-freeze në V1.
- `Decimal` në `data` hedh `TypeError` (pa encoder të fshehur); producer-at konvertojnë në string.
- `GET /v1/events` paraqet `created_at` përmes FastAPI (format tjetër nga envelope): divergjencë e qëllimshme.
- `verify_v1`: `v1` jo-ASCII → `TypeError`; `now=0` bie te ora reale. Vëzhgime sigurie, jo ndryshuar.
- `rotate_secret` pa grace; delivery-id i panënshkruar; `event_id`/`delivery_id` janë lokalë për DB-në.
- **M3-d3 performance exception:** relative serializer regression >5% accepted because absolute overhead is ~1–2 µs per delivery and negligible against actual DB/HTTP delivery latency. Revisit only if end-to-end throughput shows measurable regression.
