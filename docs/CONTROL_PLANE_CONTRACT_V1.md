# Kontrata e control-plane `cp.v1` (Central → Enterprise) — M7-b2

Paketa: `packages/contracts/control_plane/v1.py` (import: `packages.contracts.control_plane.v1`). **Leaf, stdlib-only** (pa `app.*`, `apps.*`, SQLAlchemy, FastAPI, Pydantic, httpx). Familje e **ndarë** nga webhook-et e klientëve (`app/contracts`, `EventEnvelopeV1`): pa shkëmbim importesh, golden-et e webhook-it të pandryshuara. Pa nënshkrim/auth/transport (M7-c).

## Zarfi (`ControlPlaneEventV1`)
```json
{"schema":"cp.v1","event_id":"<uuid>","seq":123,"type":"enterprise.upserted","enterprise_id":"<uuid>",
 "entity":{"type":"enterprise","id":"<uuid>"},"revision":4,
 "occurred_at":"2030-01-01T12:00:00.000000+00:00","data":{…}}
```
- `schema`: saktësisht `cp.v1`. `event_id`: vjen **direkt** nga `sync_outbox.event_id` (kurrë i ri gjatë mapimit).
- **`seq`** = pozicioni në feed-in global (kursor). **`revision`** = rendi/idempotenca autoritative **per entitet**. Konsumatori nuk përdor `seq` vetëm për të vendosur nëse gjendja është më e re.
- **`occurred_at`** = `created_at` i rreshtit outbox; UTC, gjerësi fikse (mikrosekonda gjithmonë, `+00:00`); naive trajtohet UTC, offset konvertohet. **Vetëm informativ**, kurrë autoritet rendi.
- **`data`** = gjendja e PLOTË e entitetit në çastin e ndryshimit (state-based; historia e veprimeve është te audit).

## Tipet (vetëm dy; pa delete/tombstone)
| `type` | `entity.type` | Gjendja (`data`) |
|---|---|---|
| `enterprise.upserted` | `enterprise` | `EnterpriseStateV1`: `{id, name, status}` (`status` ∈ active\|suspended) |
| `enterprise_product.upserted` | `enterprise_product` | `EnterpriseProductStateV1`: `{assignment_id, enterprise_id, product:{id, code, channel}, status}` (`channel` ∈ sms\|email; `status` ∈ active\|suspended) |
Emrat janë vlerat ekzistuese të DB (`enterprise.upserted`, `enterprise_product.upserted`): **pa migrim `0008`**. Pa `rate_limit_per_min` (M5-c i shtyrë). Produkti udhëton si `product{id, code, channel}`: Enterprise nuk ka nevojë për tabelë `Product`.

## Serializimi kanonik
`json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")` (si webhook V1; pa Pydantic). `seq`/`revision` ruhen saktësisht (provuar mbi 2^53 dhe INT64 max).

## Validimi strikt
`schema` saktësisht `cp.v1` · UUID kanonike (të vogla, me vija) · `seq`, `revision` ∈ 1..2^63-1 (jo bool/string) · tip i njohur dhe `entity.type` i përputhur · `status` dhe `channel` të njohura · `name` 1..200 pa karaktere kontrolli · `product.code` `[a-z][a-z0-9_]{1,31}` · identitetet e përputhura (`entity.id` = `data.id` / `data.assignment_id`; `enterprise_id` = `data.id` / `data.enterprise_id`). Gabim → `ContractError` (`UnsupportedSchemaError`, `UnknownEventTypeError` për rastet e veçanta).

## Pajtueshmëria përpara (rregullat e konsumatorit)
- `schema` ≠ `cp.v1` → `UnsupportedSchemaError`: mos aplikoni; rikonsilimi dështon qartë.
- `type` i panjohur → `UnknownEventTypeError`: mos e kaloni në heshtje (mos avanconi kursorin pa trajtim).
- Fusha të panjohura në **zarf** → refuzim. Fusha të panjohura te `data` (dhe `data.product`) brenda të njëjtit major → **injorohen**; konsumatori përdor vetëm të njohurat. Shtimi i fushave = ndryshim i rishikuar (golden përditësohet me miratim); ndryshim thyes → `cp.v2`.
- `revision` më e vogël → injoro; e barabartë → no-op; më e madhe → apliko.

## Mapper-i i Central (`apps/central/services/sync_contract.py`)
`SyncOutbox` → `ControlPlaneEventV1` → bytes, **vetëm nga rreshti** (payload i ngrirë): pa Session, pa query. Prova historike: revision 2 (`active`) serializohet ende `active` pasi entiteti është ndryshuar në revision 3 (`suspended`), edhe me rreshtin të shkëputur nga sesioni.

## Golden (`tests/golden/control_plane/`)
13 raste (enterprise active/suspended/non-ASCII; assignment SMS/Email active/suspended; seq e lartë; revision e lartë; mikrosekonda 0/jo-0; created_at naive; offset). Ruhen `*.body` (bytes), `cases.json` (rreshti outbox hyrës + JSON i parsuar). Rregullim vetëm me miratim kontrate; `regenerate.py` është manual-only (si politika e webhook-ut §CONTRACTS_V1).

## Çfarë mbetet jashtë
Feed/snapshot endpoint, auth shërbimi, applier dhe tabela në Enterprise, worker, poller: M7-c/d/e.
