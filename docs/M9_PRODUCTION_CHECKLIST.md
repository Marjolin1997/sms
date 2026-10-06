# Lista e prodhimit — parat dhe çmimet (M9)

Çdo hap ka **komandën**, **kriterin e kalimit** dhe **rikthimin**. Rendi është i detyrueshëm. Asgjë këtu nuk ndryshon konfigurim vetë: ndryshimet e `.env` i bën operatori.
Konventa: `ENT$` = ekzekutim në hostin Enterprise (me `SMS_*`), `CEN$` = në hostin Central (me `CENTRAL_*`).

## 0. Parakushte
- [ ] Backup i freskët i të dy bazave (`scripts/backup.sh`) + restore i provuar muajin e fundit (`scripts/restore.sh` + `python -m scripts.verify_ledger`).
- [ ] **Central ka PITR/WAL archiving ose replikë sinkrone** (RPO≈0 për tabelat e parave) — bllokues (shih `M9_MONEY_AUDIT.md` §11).
- [ ] Migrime në kokë: Central `0021`, Enterprise `0026` (`alembic upgrade head` / `python -m alembic -c apps/central/alembic.ini upgrade head`).
- [ ] `CENTRAL_ENV=production`, `SMS_ENV=production`, sekretet e vendosura (`CENTRAL_AUTH_SECRET`, `SMS_SECRETS_KEY`, `SMS_PII_HMAC_KEY`).

## 1. Provë idempotence e provider-it
`ENT$ python -m scripts.queue_readiness --json` → rreshti `provider_capabilities`. **Kriter:** çdo provider real është `idempotent_by_reference=False` nëse s'ka provë kontrate të vendorit; atëherë UNKNOWN trajtohet PA ridërgim automatik (procesi njerëzor `/v1/admin/queue/*/resolve`).
Evidencë e kërkuar për ta kthyer një adapter në `True`: dokument vendori që garanton çelës idempotence + test kontrate. **Rikthim:** asnjë (është vetëm lexim).

## 2. Backlog UNKNOWN + queue readiness
`ENT$ python -m scripts.queue_readiness --strict` → **PASS** (0 `stuck_sending`, UNKNOWN i zgjidhur ose < 1h). Hold-et e UNKNOWN janë para e ngrirë: `python -m scripts.financial_ops`.

## 3. Kredencialet e shërbimit (Central)
Një klient per rol (worker), scope minimal: `money_control_plane`→`money:read`, `money_usage_reporter`→`money:report`, `pricing_control_plane`→`pricing:read`, sinkroni cp.v1→`sync:read`.
`CEN$ python -m apps.central.tools.create_service_credential --client-id <id> --kid <kid> --public-key-file <pub.pem> --scope <scope> --enterprise <uuid>` (gjenero çiftin jashtë Central; dërgo vetëm çelësin publik).
**Kriter:** `python -m apps.central.tools.financial_readiness --json` → `central:financial_service_credentials` PASS; rreshtat `service_client.create`/`service_key.add` te audit. **Rikthim:** `service_credential_admin disable-client|disable-key`.

## 4. Money bootstrap
Sipas `M9_MONEY_AUDIT.md` M9-c runbook: (1) Central: fondo llogarinë (pagesë + miratim nga një admin tjetër). (2) `ENT$ SMS_MONEY_AUTHORITY=shadow`, nis `--role money_control_plane`. (3) `python -m scripts.money_authority baseline-create --wallet-id N --by <operator>`. (4) Central: grant `purpose=bootstrap` me `baseline_ref`. **Kriter:** grant-i bëhet `matched_to_existing_balance` (delta 0).
**Rikthim:** mbaj `shadow` (mint lokal i bllokuar); `local` hap sërish mint-in lokal dhe kërkon baseline të ri.

## 5. Shadow mode i parave dhe money readiness
`ENT$ python -m scripts.money_authority_readiness` → **PASS**. Pastaj `SMS_MONEY_AUTHORITY_ACK=true` dhe `SMS_MONEY_AUTHORITY=central`, rinis web+worker.
**Kriter:** `central`-i nis (në prodhim pa ACK refuzon); `python -m scripts.money_authority cursor-show` → `last_error` null, `last_success_at` i freskët.

## 6. Raportimi i përdorimit dhe rakordimi
`ENT$ SMS_MONEY_REPORTING=true`, nis `--role money_usage_reporter`. `CEN$ python -m apps.central.tools.money_reconciliation --strict` → **PASS** (asnjë CRITICAL/FAIL; asnjë `unexplained_positive_credit`; asnjë `unresolved_reversal` përtej pragut).
Retention i raporteve: parazgjedhje pa fshirje; vendos `CENTRAL_USAGE_REPORT_RETENTION_DAYS` vetëm pas vendimit ligjor, pastaj `python -m apps.central.tools.retention` (dry-run) → `--apply`.

## 7. Çmimet: bootstrap, shadow, readiness
1. `ENT$ python -m scripts.pricing_bootstrap export --out pricing.json` (vetëm lexim). 2. `CEN$ python -m apps.central.tools.pricing_import --proposal pricing.json` (dry-run; vetëm `exact` zbatohet) → `--apply --actor-email <admin> --ack-proposal-hash <hash>`.
3. Central: aktivizo versionin + `POST /admin/pricing/assignments` (ose import). **Kriter:** `GET /admin/pricing/readiness` → `ok: true` (assignment, monedhë = monedha e llogarisë, version efektiv).
4. `ENT$ SMS_PRICING_AUTHORITY=shadow`, nis `--role pricing_control_plane`; mblidh ≥ 20 krahasime pa mospërputhje (`python -m scripts.pricing_authority_readiness`).
5. **PASS** ⇒ `SMS_PRICING_AUTHORITY_ACK=true`, `SMS_PRICING_AUTHORITY=central`. **Rikthim:** `SMS_PRICING_AUTHORITY=local` (fotot e mesazheve mbeten të vlefshme; s'ka rillogaritje).

## 8. Gate-i i agreguar (para hapjes së trafikut real)
`CEN$ python -m apps.central.tools.financial_readiness --strict` (me mjedisin Enterprise të ekspozuar; `--enterprise-cwd` nëse ekzekutohet nga repo-ja Enterprise) → **PASS**.
Çdo `FAIL`/`WARN` ka emrin e kontrollit dhe arsyen; **mos hap prodhimin me WARN/FAIL**. Kontrolli i vazhdueshëm: `GET /admin/financial/alerts` (Central), `GET /v1/admin/financial` (Enterprise), `python -m scripts.financial_ops`.

## 9. Gate-t e regjistrimit të M8 (Central)
- [ ] `CENTRAL_MAILER=smtp` (+ `CENTRAL_SMTP_*`; `fake` refuzohet nga aplikacioni në prodhim).
- [ ] CAPTCHA/sfida reale: `CENTRAL_BOT_CHALLENGE` ≠ `fake` (në prodhim `fake` refuzohet); `CENTRAL_PUBLIC_REGISTRATION_REQUIRE_CHALLENGE=true` nëse regjistrimi publik është i hapur.
- [ ] Proxy i besuar: `CENTRAL_TRUSTED_PROXY_HOPS` i saktë dhe `CENTRAL_PUBLIC_REGISTRATION_PROXY_ACK=true` (kufijtë IP jetojnë te proxy).
- [ ] `CENTRAL_ALLOW_UNVERIFIED_AUTO_REGISTRATION=false` (prodhimi refuzon `true`). `python -m apps.central.tools.registration_readiness` → PASS.

## 10. Procedurat e rikthimit
| Çka | Rikthimi | Kujdes |
|---|---|---|
| Çmimet `central`→`local` | `SMS_PRICING_AUTHORITY=local`, rinis | pa efekt mbi mesazhet ekzistuese; tarifat lokale duhet të jenë të azhurnuara |
| Money `central`→`shadow` | `SMS_MONEY_AUTHORITY=shadow` | mint lokal mbetet i bllokuar; grant-et e reja regjistrohen pa kredituar |
| Money `→local` | vetëm emergjencë | mint lokal hapet; kërkon baseline të ri para çdo cutover-i të ri |
| Kursor i prishur | `python -m scripts.money_authority reset-cursor --epoch … --generation … --ack-replay --by <operator>` | riprodhim idempotent nga 0; audit `money.cursor_reset` |
| Version çmimi i gabuar | Central: `POST /admin/pricing/versions/{id}/retire` + version i ri | s'ka fallback te i vjetri kur versioni efektiv është i tërhequr (fail-closed) |
| Grant i gabuar | `POST /admin/money/grants/{id}/reverse` | reversal konservativ: s'çon wallet-in negativ ⇒ mund të kërkojë `unresolved_reversal` |
| Retention | s'ka rikthim (fshirje) — dry-run gjithmonë më parë | vetëm të dhëna operacionale |
| Migrim | restore nga backup + imazhi i vjetër (jo downgrade me humbje) | `0021`/`0026` nuk ndryshojnë skemë: downgrade i sigurt |

## 11. ACK-et e prodhimit (të gjitha duhen)
`SMS_MONEY_AUTHORITY_ACK=true` (pas PASS në shadow) · `SMS_PRICING_AUTHORITY_ACK=true` (pas PASS në shadow) · `SMS_MONEY_REPORTING=true` me worker aktiv · `CENTRAL_PUBLIC_REGISTRATION_PROXY_ACK=true` (nëse regjistrimi publik hapet).
Asnjë mjet nuk i vendos vetë.

## 12. Pas hapjes (ditën 1)
Çdo 5 min `financial_readiness` në monitorim të jashtëm (kodi ≠ 0 ⇒ njoftim); rishikim ditor i `GET /admin/financial/overview`; `financial_ops` pas çdo incidenti UNKNOWN.
