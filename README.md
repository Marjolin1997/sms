# SMS Platform

Platformë SMS ku **saktësia e parave dhe e statuseve** ka përparësi mbi numrin e veçorive.

## Parimet
- Shumat: `Decimal`/`NUMERIC(20,6)`, kurrë float (float refuzohet nga shtresa e shërbimit).
- Ledger **append-only**: ORM guard + triggers MySQL/MariaDB (`UPDATE`/`DELETE` refuzohen). Bilanci = `balance_after` i rreshtit të fundit; `verify_wallet` e kontrollon me `SUM(delta)`.
- Çdo lëvizje parash është idempotente (`wallet_id + idempotency_key` unik); retry nuk faturon dy herë.
- Lëvizjet serializohen me `SELECT ... FOR UPDATE` mbi rreshtin e wallet-it.
- **PostgreSQL 16** (izolim `READ COMMITTED`, `SELECT ... FOR UPDATE` mbi wallet, `SKIP LOCKED` për radhën, afate për lock/statement/idle-in-transaction).
- Append-only edhe në nivel databaze: triggers PL/pgSQL bllokojnë `UPDATE/DELETE/TRUNCATE` mbi ledger, audit log, historikun e statuseve dhe DLR receipts.
- Tabelat kanë prefiks `sms_`; Alembic ignoron çdo tabelë tjetër dhe përdor `sms_alembic_version`. Migrimet e para vetëm në një databazë kopje, kurrë direkt në prodhim.

## Fazat
| Faza | Status |
|---|---|
| 0 Integrimi me DB/përdoruesit e omnichannel | pezull (platforma tani ka DB PostgreSQL të vetën) |
| 1 Skeleti (FastAPI, Docker, Alembic, teste, CI) | ✅ |
| 2 Wallet + ledger + top-up (hold/capture/release/refund) | ✅ |
| 3 Rate cards me versione, prefix/operator, segmente, quote | ✅ |
| 4 Sender IDs + templates me miratim, versione, validim | ✅ |
| 5 Pipeline dërgimi (outbox, retries, DLR, provider fals) | ✅ |
| 6 Adapter HTTP + webhook DLR i nënshkruar + sweeper (SMPP: pret vendorin) | ✅ |
| 7 RBAC + API keys, audit log, kill switch, rate limit, monitorim | ✅ |

## Nisja
```bash
cp .env.example .env
docker compose up --build        # API në :8000, PostgreSQL në :5433
```
Lokalisht: `pip install -r requirements-dev.txt && ruff check . && pytest`.
Teste të plota mbi PostgreSQL (konkurrencë, triggers, migrime):
`SMS_TEST_DATABASE_URL=postgresql+psycopg://sms:sms@localhost:5432/sms_test pytest`

## Auth dhe RBAC
- `Authorization: Bearer sms_<prefix>_<secret>`. Ruhet vetëm SHA-256 i sekretit; çelësi i plotë shfaqet një herë.
- Role: `superadmin`, `finance`, `pricing`, `approver`, `support`, `client` (i lidhur me një `owner_ref`; sheh vetëm të dhënat e veta).
- Bootstrap: `X-Admin-Key` = `SMS_ADMIN_API_KEY` (superadmin). Përdore vetëm për të krijuar çelësat e parë (`POST /v1/admin/api-keys`), pastaj **hiqe variablën në prodhim**.
- Çdo ndryshim administrativ shkruhet te `sms_audit_log` (vetëm-shtim) në të njëjtin transaksion.
- Kill switch: `PUT /v1/admin/switches/{submit|dispatch}` (arsye e detyrueshme kur çaktivizohet).
