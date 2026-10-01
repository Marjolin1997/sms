# M4 — Central (control plane): arkitektura

## Rolet
- **Central = Control Plane** (aplikacion i veçantë: identiteti i enterprise-ve, katalogu, çmimet, miratimet — në fazat e ardhshme).
- **Enterprise = Operational Plane** (aplikacioni ekzistues te `app/`: tenant-ët, dërgimi, ledger-i operacional, API-të e klientit).
- **Gateway = Processing Plane** (logjik, brenda procesit të Enterprise deri te M12: providers, workers, queue).

## Vendime (miratuar)
1. **Vendndodhja:** i njëjti repo; `apps/central/` fizikisht i pavarur. Enterprise MBETET te `app/` (pa lëvizje; shtyhet çdo vendim për `apps/enterprise`).
2. **Contracts:** `app/contracts` mbetet aty; Central nuk e importon në M4-a. Nxjerrja në `packages/contracts/` bëhet vetëm kur Central ka konsumatorin e parë real (EventEnvelopeV1, signature ose kontratë Central↔Enterprise), me re-export kompatibiliteti për Enterprise dhe golden-et ekzistuese. Pa dublikim kontratash.
3. **Version table:** `central_alembic_version` (Enterprise: `sms_alembic_version`).

## M4-a — skelet i pavarur (pa biznes)
```
apps/central/
├── __init__.py
├── main.py                 # create_app(engine=None); GET /healthz, /readyz; pa docs/openapi
├── core/{config,db,readiness}.py
├── api/health.py
├── models/__init__.py      # Base.metadata e Central: bosh (asnjë tabelë biznesi)
├── alembic.ini
└── migrations/{env.py,script.py.mako,versions/0001_baseline.py}
```
- **Konfigurim:** `CENTRAL_*` (`.env.central`): `env`, `database_url`, `db_pool_size`, `db_statement_timeout_ms`. Asgjë nga `SMS_*`.
- **DB:** engine/session/`Base`/`MetaData` të veta; DB logjike e veçantë (`central_db` ≠ `enterprise_db`), jo skemë e njëjtë, jo tabela të përbashkëta.
- **Migrime:** `alembic -c apps/central/alembic.ini upgrade head` (nga rrënja e repo-s, me `CENTRAL_DATABASE_URL`). Baseline bosh `0001`; krijon vetëm `central_alembic_version`. Pa filtër `sms_*`.
- **`/healthz`:** 200 pa DB. **`/readyz`:** 200 vetëm nëse DB përgjigjet dhe `central_alembic_version` është në kokën e vetme të migrimeve të Central. 503 për: DB e padisponueshme; skemë e pa-inicializuar (pa version table, edhe pa rresht); skemë prapa; revision i panjohur/përpara kodit; shumë koka. Nuk kontrollon asgjë nga Enterprise (owner_ref, backfill, queue, wallet, tenant).
- **Izolimi:** `apps/central` nuk importon asnjë modul `app.*` (as `core.db`, `models`, `services`, `api`, `contracts`); `app`, `alembic`, `scripts` nuk importojnë `apps.*`. Provuar: import në proces të pastër pa asnjë `app.*` në `sys.modules`; metadata e Central e ndarë nga Enterprise; DB-të PG të pavarura në migrime (Enterprise në revision tjetër, Central poshtë/lart: asnjëra s'ndikon tjetrën).
- **Nuk përfshihet:** tabela biznesi, Product/EnterpriseProduct, çmime, miratime, pagesa, përdorues/RBAC, regjistrim, provisioning, sync, event bus, service auth, frontend.

## Borxhe/risqe të njohura të M4-a
- Imazhi Docker/compose për Central nuk ekziston ende (Dockerfile i sotëm kopjon vetëm `app`).
- `ruff format --check .` raporton skedarë ekzistues (alembic 0014/0016, docs/API.md) dhe një code-block te docs; jo nga M4.
- Importi `apps.central.*` kërkon rrënjën e repo-s në `PYTHONPATH` (namespace package pa `apps/__init__.py`).

---
## M4-b — Regjistri i Enterprise-ve në Central

### Audit i identitetit ekzistues (Enterprise: `sms_enterprises`)
| Fushë | Klasa | Përdorim real sot |
|---|---|---|
| `id` UUID (v4, default `uuid.uuid4`) | **Identitet kanonik** | PK; `enterprise_id` në 21 tabela; `TenantContext` |
| `owner_ref` (unik, edhe `lower()` unik) | **Kompat legacy** | filtri i dyfishtë i skopimit; heqet në M13 |
| `external_id` (unik kur nuk është NULL) | Rezervë për lidhje të jashtme | asnjë shkrues/lexues; gjithmonë NULL |
| `legal_name`, `short_name` | Metadata komerciale | asnjë shkrues; gjithmonë NULL (`BillingProfile.legal_name` është koncept tjetër: palë faturimi) |
| `status` (`active` default) | Ciklin e jetës | asnjë kod nuk e lexon ose e ndryshon |
| `created_at`, `updated_at` | Teknike | vetëm default |
Përfundim: sjellja operacionale e sotme varet vetëm nga `id` dhe `owner_ref`. Fushat e tjera ekzistojnë nga plani M1 dhe nuk kanë asnjë konsumator; nuk kopjohen verbatim.

### Skema e Central (`enterprises`, migrimi `0002`)
| Fushë | Pse jeton në Central | Konsumator | Pse jo vetëm Enterprise |
|---|---|---|---|
| `id` UUID PK (v4; Central e gjeneron, ose pranon UUID ekzistuese për backfill-in M4-c) | identiteti kanonik ndër-sistem | çdo fazë (M5–M11), sync (M7) | Enterprise e merr nga Central, jo anasjelltas |
| `name` String(200) NOT NULL, jo unik, vetëm `strip`, pa karaktere kontrolli | emër për t'u identifikuar nga stafi dhe për t'u shfaqur kur Enterprise sinkronizohet | konsola e Central (M11), regjistrimi (M8), sync (M7) | është e dhënë e regjistrit; Enterprise s'ka kuptim biznesi për të sot |
| `status` `active|suspended` (CHECK) | ndalimi/rinisja është vendim i control plane | M7 (sync i statusit), M11 | Enterprise vetëm zbaton |
| `created_at`, `updated_at` (UTC aware) | audit i ardhshëm, renditje, sync | M7 | — |
**Qëllimisht jashtë:** `owner_ref`, `external_id`, `legal_name`, `short_name`, kod/slug (propozim për M4-c/M8 nëse nevojitet një referencë legacy opsionale `legacy_owner_ref` për mapimin e migrimit; nuk shtohet pa arsye), çdo gjë operacionale (wallet, sender IDs, kontakte, fushata, çelësa, provider, numërues), produkte, çmime.
**Pa kufizime unike** te `name`: s'ka arsye domain-i (dy kompani mund të kenë të njëjtin emër); vetëm `id` është unik globalisht.

### Cikli i jetës
Dy gjendje, të justifikuara nga roadmap-i (M7/M11) dhe nga `status` ekzistues: `active`, `suspended`. `suspend`/`activate` janë idempotentë dhe të kthyeshëm (pa ndryshim → pa `updated_at`). Pa workflow miratimi (M8), pa gjendje "deleted".
**Pa fshirje fizike:** asnjë funksion `delete`/`remove` (test AST); Enterprise do të ketë produkte, pagesa, audit, histori përdorimi.

### Service (`apps/central/services/enterprises.py`, konkret, pa framework)
`create(db, name, *, enterprise_id=None)`, `get`, `list_enterprises(status, limit, offset)` (renditje `created_at, id`), `rename`, `suspend`, `activate`. Asnjë commit (transaksioni i thirrësit). Gabime të Central (`NotFound`, `Conflict`, `Invalid`), pa HTTP. Pa actor/audit (s'ka auth): operacionet janë të pastra që auditi të shtohet më vonë.

### UUID
Tipi `sa.Uuid`: `UUID` native në PostgreSQL, `CHAR(32)` në SQLite. Gjenerim UUIDv4 (si Enterprise; pa UUIDv7 pa vendim arkitekturor). Serializim kanonik: `str(uuid)` me shkronja të vogla (36 karaktere). `get` pranon `UUID` ose string UUID; id numerike → `Invalid`.

### Kufijtë (të pandryshuar)
Pa sync Central→Enterprise, pa event, pa dual-write, pa DB të përbashkët, pa worker, pa HTTP menaxhimi, pa auth. Central s'importon asnjë `app.*`. Timestamps: `apps/central/core/timeutil.utcnow` (lokal, pa kernel). `/readyz` ndjek koka e re (`0002`): `0001` → 503, `0002` → 200, revision i panjohur → 503.

### Borxh i regjistruar
- **TEST INFRASTRUCTURE DEBT:** `DROP DATABASE … WITH (FORCE)` dështon ndonjëherë me "permission denied to terminate process" (supozim i arsyeshëm, jo i provuar: worker autovacuum i role `postgres` te DB e re). Fixture-t e Central e riprovojnë (`drop_database`); fixture-t legacy (`test_enterprises`, `test_m3_timeutil`, …) jo; fix-i i përbashkët është i veçantë.
- Rrezik: `ruff format --check` mund të ketë drift mbi dokumente/skedarë legacy (alembic 0014/0016, docs/API.md, kodi në markdown); nuk formatohen brenda M4.
