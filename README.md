# SMS Platform

Platformë SMS ku **saktësia e parave dhe e statuseve** ka përparësi mbi numrin e veçorive.

## Parimet
- Shumat: `Decimal`/`NUMERIC(20,6)`, kurrë float (float refuzohet nga shtresa e shërbimit).
- Ledger **append-only**: ORM guard + triggers MySQL/MariaDB (`UPDATE`/`DELETE` refuzohen). Bilanci = `balance_after` i rreshtit të fundit; `verify_wallet` e kontrollon me `SUM(delta)`.
- Çdo lëvizje parash është idempotente (`wallet_id + idempotency_key` unik); retry nuk faturon dy herë.
- Lëvizjet serializohen me `SELECT ... FOR UPDATE` mbi rreshtin e wallet-it.
- Tabelat e reja kanë prefiks `sms_`; Alembic ignoron çdo tabelë tjetër dhe përdor `sms_alembic_version`.
- Migrimet e para vetëm në **kopje** të databazës së omnichannel, kurrë direkt në prodhim.

## Fazat
| Faza | Status |
|---|---|
| 0 Analizë e DB ekzistuese | pret `schema.sql` (`mysqldump --no-data`) |
| 1 Skeleti (FastAPI, Docker, Alembic, teste, CI) | ✅ |
| 2 Wallet + ledger + top-up (hold/capture/release/refund) | ✅ |
| 3 Rate cards me versione, prefix/operator, segmente, quote | ✅ |
| 4 Sender IDs + templates me miratim, versione, validim | ✅ |
| 5 Pipeline dërgimi (outbox, retries, DLR, provider fals) | ✅ |
| 6 Adapter HTTP + webhook DLR i nënshkruar + sweeper (SMPP: pret vendorin) | ✅ |
| 7 Admin, RBAC, audit, monitorim | – |

## Nisja
```bash
cp .env.example .env
docker compose up --build        # API në :8000, MariaDB në :3307
```
Lokalisht: `pip install -r requirements-dev.txt && pytest && ruff check .`

Auth i përkohshëm: header `X-Admin-Key` (zëvendësohet në Fazën 7 me RBAC + API keys me scope).
