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
