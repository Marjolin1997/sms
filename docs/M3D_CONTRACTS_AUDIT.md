# M3-d — Contracts V1: audit + specifikim (pa ndryshim kodi)

Status: AUDIT. Nuk u krijua `app/contracts`; asnjë byte nuk ndryshon. Burimi: `services/events.py`, `services/webhooks.py`, `services/webhook_queue.py`, `models/events.py`, producerët, `api/portal.py`.

## 1. Katalogu i event-eve (`events.KNOWN_TYPES`, 21)
| Tipi | Producer | `data` (përveç `resource_type`,`resource_id`) | Nullable | `resource_id` |
|---|---|---|---|---|
| `message.sent/delivered/failed` | `messages.transition` | `message_id,status,segments` (+`error_code` vetëm në failed) | `error_code` | `public_id` (uuid str) |
| `message.received` | `inbox.process_inbound` | `from,to,text,action,keyword` (PII) | `action,keyword` | inbound `public_id` |
| `email.sent/delivered/failed/bounced/complained` | `emails.transition` | `email_id,status` (+`reason` për failed/bounced/complained) | `reason` | `public_id` |
| `campaign.running/paused/completed/cancelled` | `campaigns` | `campaign_id,name,status,pause_reason` | `pause_reason` | `str(campaign.id)` |
| `consent.opted_in/opted_out` | `consent` | `channel,address,reason,hard` (adresa = PII, qëllimisht) | `reason` | `str(state.id)` |
| `invoice.issued` | `billing` | `invoice_id,total(str),currency,due_at(isoformat)` | — | `inv.number` |
| `invoice.paid` | `billing` | `invoice_id,total(str),via` | — | `inv.number` |
| `payment.succeeded` | `payments` | `payment_id,purpose,amount(str),currency` | — | `str(p.id)` |
| `payment.failed` | `payments` | `payment_id,purpose` | — | `str(p.id)` |
| `wallet.low_balance` | `wallet` | `currency,available(str),threshold(str)` | — | `str(wallet.id)` |
| `webhook.ping` | `webhooks.send_test` | `{"ok": true}` | — | `str(endpoint.id)` |

Tipet dinamike (`message.{status}`, `email.{status}`, `campaign.{status}`) janë mbuluar plotësisht nga `KNOWN_TYPES` (verifikuar kundër enum-eve); status i ri në enum pa tip të ri → `ValueError` në emit (guard i mirë, por sot i pa testuar kundër enum-eve → test i propozuar).
Filtrat: `*`, `<prefix>.*`, tip i saktë (`matches`/`valid_filter`) — semantika e filtrit është pjesë e kontratës së abonimit.

**Publik vs i brendshëm.** Çdo rresht në `sms_events` është PUBLIK: dërgohet në webhook dhe lexohet nga `GET /v1/events`. Nuk ka event të brendshëm në atë tabelë. "Eventet" e brendshme janë tjetër gjë dhe NUK hyjnë në contracts: `MessageEvent`/`EmailEvent` (histori statusesh, append-only), `AuditLog`, `DlrReceipt`. Kandidatë kontrate: të 21 tipet (të gjitha kalojnë kufirin), por stabiliteti ndryshon: `message.*`, `email.*`, `campaign.*`, `wallet.low_balance`, `webhook.ping` = të dokumentuara në `docs/API.md` (të qëndrueshme); `invoice.*`, `payment.*`, `consent.*`, `message.received` = të dokumentuara pjesërisht (fusha `data` jo të listuara) → duhen shpallur në V1.

## 2. Si krijohet Event ORM
`events.emit(db, owner, type, resource_type, resource_id, data, only_endpoint_id, now)`: valido tipin → `Event(owner_ref, enterprise_id, type, resource_type, resource_id=str(...), data=<dict|None>, created_at=now or datetime.now(UTC))` → `flush` (merr `id`) → për çdo endpoint aktiv që përputhet (`owned` + filtër) → `queue.publish(WebhookDelivery(...))`. E gjitha në transaksionin e ndryshimit të burimit (outbox). `data` ruhet si kolonë `JSON` (jo JSONB).

## 3. Envelope aktual (`webhooks.envelope(ev: Event) -> bytes`)
```python
json.dumps({"id": f"evt_{ev.id}", "type": ev.type, "created_at": as_utc(ev.created_at).isoformat(),
            "data": {"resource_type": ev.resource_type, "resource_id": ev.resource_id, **(ev.data or {})}},
           separators=(",", ":"), sort_keys=True).encode()
```
- Fusha: `id, type, created_at, data` (renditja në bytes = ALFABETIKE nga `sort_keys=True`, rekursivisht; jo renditja e shkrimit). Brenda `data`: `resource_type`, `resource_id`, pastaj çelësat e `ev.data` — pas sortimit një radhë e vetme alfabetike.
- Serializim: `separators=(",",":")` (pa hapësira), `sort_keys=True`, `ensure_ascii=True` (default → çdo jo-ASCII del `\uXXXX`, p.sh. `text` i inbound, adresa e consent), `allow_nan` default, `default=` nuk ka (një objekt jo-JSON hedh `TypeError`).
- Encoding: `str.encode()` = UTF-8 (praktikisht ASCII).
- Timestamp: `created_at` në UTC, `isoformat()` → `2026-09-30T09:12:01+00:00`; me mikrosekonda NËSE janë ≠ 0 (`...01.123456+00:00`) → gjatësia VARION. Naive (SQLite) trajtohet si UTC (`as_utc`).
- UUID/ID: `evt_<int>` (int autoincrement i `sms_events.id`, global për DB-në); `resource_id` gjithmonë string; Decimal → `str(Decimal)` në producer (shkalla varet nga Decimal-i i thirrësit: `"5"` vs `"5.0000"`); `due_at` = `isoformat()` i datetime-it të faturës (naive në SQLite, `+00:00` në PG).
- `None` → `null` (p.sh. `pause_reason`, `error_code`); `data=None` → vetëm `resource_*`. Nested: sot vetëm vlera skalare; asnjë listë/objekt i ndërthurur.
- Rreziku i përplasjes: `**ev.data` mund të mbishkruajë `resource_type/resource_id` (asnjë producer sot nuk e bën; pa guard).

## 4. Rruga e plotë e serializimit
`emit` → `Event` (JSON në DB) → `deliver_next`: `reserve` → `db.get(Event)` → `envelope(ev)` (bytes lexohen NGA DB, pra pas round-trip JSON) → `commit` → `sign(secret, ts, body)` → `httpx.post(content=body)`. Bytes ndërtohen sërish në çdo përpjekje/replay; kanë qenë të njëjtë vetëm sepse `data` dhe `created_at` janë të pandryshueshëm.

## 5. Nënshkrimi (`webhooks.sign`)
`mac = HMAC-SHA256(key=secret.encode() [UTF-8, vlera e plotë "whsec_..."], msg=f"{timestamp}.".encode() + body)`; header `t=<int unix>,v1=<hexdigest lowercase>`. `timestamp = int(now.timestamp())` në momentin e dërgimit (JO kohën e event-it; ndryshon në çdo retry/replay, body jo). Timestamp-i është në input; delivery-id/event-id NUK janë; headerat e tjerë nuk nënshkruhen. Verifikimi i marrësit: tolerancë 300s, `compare_digest`, `v1` i vetëm.
- Sekreti: `new_secret()` = `"whsec_" + token_urlsafe(32)`; ruhet Fernet (`secret_enc`), shfaqet një herë (create/rotate). `rotate_secret` e zëvendëson menjëherë (pa dritare grace, pa nënshkrim të dyfishtë).
- Vëzhgime sigurie (vetëm raportim, pa ndryshim): (a) rotate pa grace → ndërprerje te marrësi; (b) `X-SMS-Event-Id`/`Delivery-Id` të panënshkruar (bodyja ka `id`, ndaj event-id është i mbuluar tërthorazi; delivery-id jo); (c) replay brenda 300s i pa mbrojtur pa dedup nga marrësi; (d) `v1=` lejon shtim të `v2=` pa prishur shembujt e dokumentuar (PHP/Node/Python marrin `t` dhe `v1` sipas emrit).

## 6. Headers (`deliver_next`)
`Content-Type: application/json`, `User-Agent: sms-platform-webhooks/1`, `X-SMS-Signature`, `X-SMS-Event-Id: evt_<id>`, `X-SMS-Delivery-Id: <int WebhookDelivery.id>`. (httpx shton vetë `Host`, `Content-Length`, `Accept*`, `Connection` — jo kontratë.)
Delivery-id: i qëndrueshëm për (endpoint, event); i njëjtë në retry dhe `replay`; dedup-i është përgjegjësi e marrësit (at-least-once). Event-id: i përbashkët për të gjitha endpoint-et. Të dy janë integer-ë DB-lokalë, jo globalë.

## 7. Versionimi sot
Nuk ka version në payload, as në URL. Shenjat e vetme: prefiksi `v1=` i nënshkrimit dhe `User-Agent .../1`.

## 8. Konsumatorë implicitë
1. Marrësit e klientëve (docs/API.md, README, shembujt PHP/Node/Python).
2. `GET /v1/events` (portal) — i njëjti `data` por `created_at` serializohet nga FastAPI/Pydantic (`...Z`/forma tjetër, jo bytes të envelope-it) dhe `id` e `cursor` ndarazi: DY pamje të të njëjtit event pa kontratë të përbashkët.
3. `GET /v1/webhooks/deliveries` (`evt_<id>` e rindërtuar me dorë: `portal.py`).
4. `frontend/Webhooks.jsx` (lexon `r.data.resource_id`, `created_at`).
5. `tests/test_webhooks.py` (verifikim semantik + `verify_signature`; **asnjë test golden bytes** sot).
6. Kontrata TJETËR (e jashtme, hyrëse): `api/webhooks.py::verify_signature` (`sha256=<hex HMAC(body)>` për DLR të provider-it) — skemë ndryshe; kandidat për `contracts` më vonë, jo i përzier me V1 dalës.

## 9. Çiftëzimi me ORM
`envelope(ev: Event)` lexon 5 atribute (`id,type,created_at,resource_type,resource_id,data`); `emit` ndërton ORM; `KNOWN_TYPES` jeton në `services.events` (varet nga SQLAlchemy në modul). `matches/valid_filter` janë të pastra por në modul me ORM. `sign/verify_signature` janë të pastra, por në `services.webhooks` (importon httpx, ORM, crypto).

## 10. Propozim `EventEnvelopeV1` (nuk implementohet)
```python
@dataclass(frozen=True, slots=True)
class EventEnvelopeV1:
    id: str            # "evt_<n>"
    type: str
    created_at: datetime   # aware UTC
    resource_type: str
    resource_id: str
    data: Mapping[str, Any]  # JSON-skalare/None, pa çelësat resource_*
    def to_bytes(self) -> bytes: ...   # serializer eksplicit, identik me sot
```
Mapper `Event → EventEnvelopeV1` jeton te `services` (kufiri ORM), jo në contracts. `to_bytes` përdor të njëjtat `json.dumps(..., separators, sort_keys)` dhe formatin `as_utc(...).isoformat()`; mban sjelljen e mbishkrimit `**data` (ose e refuzon me test, vendim i veçantë).

**Dataclass vs Pydantic (krahasim).** Pydantic v2 `model_dump_json` nuk garanton këto: separatorë kompakt (po), renditje alfabetike të çelësave (jo), datetime `...Z` (jo `+00:00`), `ensure_ascii=False` (ndryshon bytes për jo-ASCII), trajtim ndryshe i Decimal/UUID. Mund të detyrohen me `model_dump()`+`json.dumps`, por atëherë Pydantic s'jep asgjë dhe shton varësi + rrezik versioni (v2.x ndryshon serializimin). → **Rekomandim: frozen dataclass + serializer eksplicit, contracts = stdlib vetëm** (`dataclasses, json, hmac, hashlib, datetime, enum`).

## 11. Kontrata e nënshkrimit `WebhookSignatureV1` (kandidat)
`sign(secret: str, timestamp: int, body: bytes) -> str` dhe `verify(...)` të kopjuara 1:1 (stdlib-only), `SCHEME="v1"`, `TOLERANCE_S=300`, emrat e headerave si konstante (`HEADER_SIGNATURE, HEADER_EVENT_ID, HEADER_DELIVERY_ID`). `services.webhooks.sign` bëhet delegim; algoritmi, formati, input-i identik.

## 12. Strategjia e versionimit (pa implementim)
- **Pa `/v1` në payload** (do ndryshonte bytes). V1 = struktura aktuale e ngrirë; versioni jeton në kod (`EventEnvelopeV1`) dhe në doc.
- **Shtim backward-compatible:** fusha të reja VETËM brenda `data` të një tipi (ose si çelës i ri top-level, që marrësit tolerues e injorojnë), me rregull "marrësit injorojnë çelësa të panjohur" të dokumentuar; çdo shtim ndryshon bytes dhe signature, prandaj golden përditësohet me ndryshim të qëllimshëm dhe changelog.
- **Breaking** (heqje/rename/tip i ndryshuar/format kohe): `EventEnvelopeV2` + opt-in për endpoint (kolonë `schema_version` në endpoint, default 1; ndryshim skeme, vendim i veçantë). Bashkëjetesë: i njëjti `Event`, mapper-a V1 dhe V2, endpoint-i zgjedh; `User-Agent`/header `X-SMS-Schema: 2` vetëm për V2.
- **Nënshkrim:** versioni i ri i algoritmit = `v2=` shtuar PAS `v1=` në të njëjtin header (dual-sign gjatë migrimit). Asnjë ndryshim pa vendim të veçantë.

## 13. Plani golden
Fixture-t nga kodi AKTUAL, gjeneruar PARA çdo zhvendosjeje, në `tests/golden/webhooks/`: për secilin tip përfaqësues (`message.sent`, `message.failed` me `error_code=null` dhe me vlerë, `message.received` me jo-ASCII, `email.bounced`, `campaign.paused` (`pause_reason` null/vlerë), `consent.opted_out`, `invoice.issued`, `payment.succeeded`, `wallet.low_balance`, `webhook.ping`) + rastet kufi (mikrosekonda ≠ 0 / = 0, `data=None`, naive vs aware):
1. `*.body` — bytes të saktë (krahasim `==` me bytes, jo dict).
2. `*.json` — objekti i dekoduar.
3. Me `secret`, `timestamp` fiks: `X-SMS-Signature` i saktë (string).
4. Headers e plotë `X-SMS-*` + Content-Type/User-Agent (nga kërkesa e kapur e `httpx` mock).
5. Test strukturor: `EventEnvelopeV1.to_bytes() == envelope(ev)` për secilin fixture (kur të ekzistojë), dhe `sign` i contracts == `sign` aktual.
6. Test mbulimi: çdo `KNOWN_TYPES` ka fixture; çdo status enum (message/email/campaign) ka tip në katalog.
Fixture-t ndërtohen me `now` fiks dhe objekte `Event` në memorie (pa DB) + një variant pas round-trip DB për të provuar që JSON column nuk ndryshon bytes.

## 14. Rreziqet e byte-compatibility
1. Mikrosekonda në `isoformat()` (gjatësi e ndryshueshme). 2. `ensure_ascii` (një ndryshim i vetëm prish çdo event me tekst jo-ASCII). 3. Shkalla e Decimal në `str()` e vendosur nga thirrësi. 4. Naive vs aware (`due_at`, SQLite vs PG). 5. `sort_keys` + përzierje `resource_*` me `data`. 6. Pydantic/FastAPI serialization nëse envelope kalon andej. 7. Round-trip JSON në DB (int/float/unicode; JSON jo JSONB ruan; PG kthen të njëjtën gjë por provohet). 8. `ts` në nënshkrim është koha e dërgimit — golden duhet ta injektojë. 9. Mbishkrimi `**data`. 10. Ndërtim i dyfishtë i bytes në replay (duhet të mbetet deterministik).

## 15. Çfarë NUK hyn në contracts
ORM/Session, `emit`, `deliver_next`, `queue`/lease/retry (politika e dërgimit: `RETRY_DELAYS`, `DISABLE_AFTER` janë sjellje operacionale, jo format), httpx/net_guard (SSRF), crypto/Fernet e sekretit (ruajtje), `_STATUS` HTTP dictionaries, klasat e gabimeve (`DomainError.code` = API-lokal sot; kandidat contract vetëm kur të ketë error contract të vendosur veçmas), eventet e brendshme (`MessageEvent`, `AuditLog`), DLR hyrës (kontratë tjetër), `TenantContext`/`owner_ref`.

## 16. Identifikuesit
- `event_id` / `delivery_id`: `evt_<int>` dhe int — DB-lokalë; në Enterprise të shumëfishtë përplasen. Value object sot JO; vetëm funksion `format_event_id(int)`/`parse` nëse dy vende e formatojnë (sot 4: `webhooks`, `portal` ×3 → vlerë e përbashkët reale). Nuk ndryshohet formati; qëndrueshmëria globale = çështje e M-vonë (prefiks enterprise/plane).
- `enterprise_id`: nuk del në envelope (jo kontratë e jashtme); mbetet intern.
- `message public_id`: uuid4 str; e jashtme, por pa validim të përbashkët → string, jo klasë.
- `DomainError.code`: API-lokal; mos e fut në V1.

## 17. Struktura e propozuar
```
app/contracts/            # stdlib vetëm
  __init__.py
  events.py     # EVENT_TYPES (frozenset), matches(), valid_filter(), EventEnvelopeV1, to_bytes()
  signature.py  # sign(), verify(), HEADER_*, SCHEME, TOLERANCE_S
```
`services.events.KNOWN_TYPES` = ri-eksport; `services.webhooks.envelope(ev)` = mapper + `to_bytes()`; `sign` = delegim. Guard AST: `contracts` nuk importon `app.*` as sqlalchemy/fastapi/pydantic/httpx.

## 18. Fazat
- **d1** golden fixtures nga kodi aktual (vetëm teste, 0 ndryshim prodhimi) + test mbulimi katalogu↔enum.
- **d2** `contracts/signature.py` + delegim `sign/verify`; golden i nënshkrimit i njëjtë.
- **d3** `contracts/events.py`: katalog + `matches/valid_filter` + `EventEnvelopeV1.to_bytes`; `envelope()` delegon; golden bytes i njëjtë.
- **d4** AST guard contracts-pastër; dokumentim i V1 (changelog/politika) në `docs/API.md` pa ndryshim bytes.
- Jashtë M3-d: V2/schema_version, dual-sign, formatimi i ID-ve global, pull `/v1/events` unifikim (ndryshon `created_at`), error contract.

## 19. Hapi i parë minimal
**d1:** vetëm `tests/test_webhook_golden.py` + `tests/golden/webhooks/*` (bytes, JSON, signature, headers për ~10 tipe + rastet kufi), të gjeneruara nga `envelope()`/`sign()`/`deliver_next` aktuale. Zero kod prodhimi i ndryshuar; kjo ngrin kontratën para çdo extraction.

---
## M3-d1 — Golden të kontratës së webhook-ut (zero kod prodhimi)
Skedarët: `tests/test_webhook_golden.py`, `tests/golden/webhooks/{cases.json,*.body,regenerate.py}`.
- 33 fixture (21 kanonike, një për çdo `KNOWN_TYPES`, plus `message.failed` me `error_code` null/vlerë dhe 11 raste kufi). Secret `whsec_test_contract_v1`, `ts=1700000000`; nënshkrimi i parë u verifikua i pavarur me `openssl dgst -sha256 -hmac`.
- Testet thërrasin `webhooks.envelope/sign/deliver_next` të prodhimit dhe krahasojnë BYTES; `regenerate.py` përdoret vetëm për ndryshim të miratuar të kontratës.
- **Divergjencë e qëllimshme (e miratuar):** `GET /v1/events` serializon `created_at` përmes FastAPI; webhook envelope përdor `as_utc(...).isoformat()`. Dy paraqitje të të njëjtit `Event`; nuk unifikohen në M3-d1 (ndryshim API).
- **Rrezik i karakterizuar (jo i miratuar):** `**ev.data` vjen pas `resource_*`, ndaj `data["resource_type"|"resource_id"]` mbishkruan vlerën e envelope-it (`test_characterization_event_data_overwrites_resource_fields`). Asnjë producer sot nuk e bën. Trajtohet vetëm me versionim ose miratim të veçantë.
- **Vëzhgime sigurie (vetëm të dokumentuara dhe të testuara si sjellje):** `rotate_secret` pa grace; `X-SMS-Delivery-Id`/`Event-Id` të panënshkruar (input = `ts.body`); dedup te marrësi; vetëm skema `v1`.
- Datetime: SQLite kthen `created_at` naive, PG aware; bytes janë identike (`as_utc`). Naive trajtohet si UTC; offset konvertohet në UTC.

---
## M3-d2 — Ekstraktimi i kontratës së nënshkrimit V1
- `app/contracts/signature.py` (stdlib-only: `hashlib`, `hmac`, `time`): `sign_v1(secret, timestamp, body)`, `verify_v1(secret, header, body, tolerance=300, now=None)`, `TOLERANCE_S`. Kopje 1:1 e logjikës së mëparshme; asnjë abstraksion shtesë.
- Kompatibilitet: `services.webhooks.sign = sign_v1`, `services.webhooks.verify_signature = verify_v1` (e njëjta bashkësi parametrash/emrash). `deliver_next` pandryshuar.
- Sjellja e `verify` e ruajtur (e karakterizuar): çift pa `=` / `t` mungon ose jo-int → `False`; çelës i përsëritur → fiton i fundit; hapësirat rreth çelësave → çelës tjetër; pjesë shtesë (p.sh. `v2=`) injorohen; `now or time.time()` (now=0 ≡ None); `abs()` në të dyja drejtimet, `> tolerance` refuzon; `hmac.compare_digest`; `v1` jo-ASCII hedh `TypeError` (vëzhgim sigurie, pa ndryshim).
- DLR hyrës (`api/webhooks.py::verify_signature`, `sha256=<hex>`) është kontratë tjetër dhe nuk preket; testet e garantojnë ndarjen.
- Golden-et e d1 pa ndryshim (`tests/golden/` diff bosh).

---
## M3-d3 — EventEnvelopeV1 + serializer eksplicit
- `app/contracts/events.py` (stdlib-only: `json`, `dataclasses`, `datetime`, `typing`; pa `app.*`): `@dataclass(frozen=True, slots=True) EventEnvelopeV1(id, type, created_at, data)`, `to_dict()` (kopje e cekët e `data`), `to_bytes()` = `json.dumps(..., separators=(",",":"), sort_keys=True, ensure_ascii=True).encode("utf-8")`.
- `created_at`: primitive `_as_utc` e kopjuar qëllimisht nga `core.timeutil.as_utc` (që `contracts` të mos varet nga `core`); një test e mban të barabartë me origjinalin. Naive = UTC, offset → UTC, `+00:00` (jo `Z`), mikrosekonda vetëm kur ≠ 0.
- Mapper ORM → kontratë: `services.events.to_envelope_v1(ev)`; `data = {resource_type, resource_id, **(ev.data or {})}` (ev.data fiton në përplasje: sjellje e ngrirë). `webhooks.envelope(ev)` = `to_envelope_v1(ev).to_bytes()` (kthen `bytes`).
- Golden: 33 fixture të pandryshuara; test dual-path (kopje verbatim e serializer-it të vjetër në test) == i ri == golden.
- Kosto: ~+1.1 µs/envelope (≈ +14%) nga shtresimi (mapper + dataclass + `to_bytes`); absolutisht i papërfillshëm krahas HTTP + DB në `deliver_next`.
