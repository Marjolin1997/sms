# M3-a · Audit i varësive dhe kufijve (pa ndryshim kodi)

Metodë: graf importesh nga AST për të gjithë `app/` (87 module; importet **top-level** dhe **lazy** (brenda funksioneve) të ndara),
pastaj analizë e kandidatëve. Nuk u zhvendos asnjë kod. Skripti është reproduktueshëm (AST, pa varësi).

## 1. Grafi i varësive (shtresat reale sot)
```
 api/*  (FastAPI, Pydantic DTO HTTP)                           ← 11 nga 14 modulet kalojnë nga api/tenant.py
   │
 services/*  (domain: messages, emails, webhooks, wallet, billing, contacts, ...)
   │        ▲ lazy: core→services (3 vende)  ◀── ndërlidhja e vetme e kundërt
 core/context · core/scope · core/tenancy · core/security · core/db · core/config · core/crypto · core/timeutil
   │
 models/*  (ORM; Base nga core.db; TenantOwned)                 ← 11 modele marrin `utcnow` nga models/wallet.py
 queue/*   (0 importe nga app; vetëm stdlib + SQLAlchemy)       ← 3 consumers: messages, emails, webhook_queue
 providers/* (base: stdlib; fake/http/twilio/email/payments)
```
- **Cikle top-level: 0.** (graf acyclic në importet e nivelit modul.)
- **Cikle duke numëruar importet lazy: 1 SCC me 5 module:** `core.context → services.enterprises → services.wallet → core.scope → core.context`,
  plus `services.wallet → services.events (lazy) → core.context`. E fshehur nga imports brenda funksioneve.
- Fan-in kryesor (top-level): `core.db` 31, `services.wallet` 31, `core.config` 23(+2 lazy), `core.scope` 24, `models.wallet` 19, `core.security` 16,
  `core.context` 11, `services.audit` 12, `core.timeutil` 8, `queue.*` 3.

## 2. Inventari i kandidatëve
| Kandidat | Imports aktuale | Consumers | Coupling domain | Coupling DB | Coupling framework |
|---|---|---|---|---|---|
| `app/queue/*` | vetëm stdlib + SQLAlchemy (`app.queue` mes vete) | `messages`, `emails`, `webhook_queue` | asnjë (hooks/spec injektohen) | `Session`/`select` (SQLAlchemy Core/ORM përgjithshëm) | — |
| `core/context.py` | lazy: `services.enterprises` | 11 | `TenantContext(enterprise_id, owner_ref)`: `owner_ref` = përputhshmëri Enterprise | lookup nëpërmjet registrit (lazy) | — |
| `core/scope.py` | `core.config`, `core.context`; lazy `models.admin` | 24 | `owned()` mbi `enterprise_id`/`owner_ref`; `cross_tenant` shkruan `AuditLog` | kolona ORM, `AuditLog` | — |
| `core/tenancy.py` + `models/tenant.py` | `core.config`, `models.tenant`; lazy `services.enterprises` | `models` (hook), 12 modele | dual-write M1 (kalimtar, hiqet në M1d) | `before_flush` mbi `Session` | — |
| `services/enterprises.py`, `models/enterprise.py` | `models.enterprise`, `services.wallet` (për gabimet) | lazy nga context/tenancy/readiness | regjistri Enterprise (resolver, backfill, audit) | ORM + SQL | — |
| `core/security.py` | `config`, `db`, `timeutil`, `models.admin`; lazy `crypto`, `totp` | 16 API | `Principal`, `ROLE_PERMS`, auth me çelësa, throttle, TOTP step-up | `ApiKey`/`AuthFailure` ORM | FastAPI (`Depends`, `HTTPException`) |
| `services/audit.py` (+`models.admin.AuditLog`) | `core.security`, `models.admin` | 12 API | forma `actor, role, action, target_type/id, detail` | ORM | — (varet nga `Principal`) |
| Idempotency | **nuk ka primitive**: `sha256(json.dumps([...]))` i dyfishuar te `messages.submit` dhe `emails.submit` + unique `(owner,key)` te modelet | 2 | specifik për kërkesën (fusha të ndryshme) | savepoint + `IntegrityError` | — |
| Ledger/money (`services/wallet.py`, `models/wallet.py`) | `core.context`, `core.scope`, `models.wallet`; lazy `services.events` | 31 (**29 vetëm për gabimet**) | holds, captures, topups, low-balance → emit event | ORM, `FOR UPDATE`, trigger append-only | — |
| Gabimet bazë (`WalletError`, `NotFound`, `Conflict`, `Insufficient…`) | jetojnë te `services/wallet.py` | 31 modula; 30 nënklasa; 10 hartëzime `_STATUS` te API | emri "Wallet" rrjedh në çdo domain | — | — |
| `utcnow`, `MONEY` | jetojnë te `models/wallet.py` | 11 modele marrin vetëm `utcnow` | `MONEY`=Numeric(20,6) money | `Numeric` | — |
| `core/timeutil.py` (`as_utc`) | stdlib | 8 | asnjë | — | — |
| `core/crypto.py` | `core.config` (çelësi Fernet) | 3 (+1 lazy) | asnjë | — | — |
| `core/db.py`, `core/config.py` | `config`; pydantic-settings | 31 / 23 | `Settings` monolit (Twilio, email, payments, queue, tenant...) | `Base`, engine, `SessionLocal` në import | pydantic |
| `providers/base.py` (+`email.EmailRequest`, `payments`) | stdlib | `messages`, `emails`, providers | SPI i provider-ave SMS/email (Gateway) | — | — |
| Webhook envelope + `sign()` + `events.KNOWN_TYPES` + header-at `X-SMS-*` | `envelope(ev: Event)` merr ORM | `webhooks`, `events`, marrësit e klientëve | kontratë **e jashtme** (JSON v1 + HMAC) | merr `Event` ORM | — |
| Pydantic DTO te `api/*` | pydantic | API publike (OpenAPI) | HTTP Enterprise | — | pydantic/FastAPI |
| `services/net_guard.py`, `sms_text.py`, `approvals.py`, `switches.py` | `config` / `models.messaging` / `models.admin` | 1 / 3 / 2 / 4 | Gateway (SSRF webhook, segmente SMS) / sender+template approvals / kill switch | `approvals`, `switches` ORM | — |

## 3. Klasifikimi
| Kandidat | Klasifikimi | Arsyeja |
|---|---|---|
| `app/queue/*` | **KEEP IN INFRASTRUCTURE** (pa lëvizje sot); kandidat kernel i vonë | pastër dhe e ruajtur nga guard; **të tre consumers janë Gateway** (SMS/email/webhook), pra pronësia nuk është e ndarë; Central s'ka ende workload queue |
| `core/context.py`, `core/scope.py` | **KEEP IN ENTERPRISE** | semantika `owner_ref`/`enterprise_id` dhe `owned()` janë të Enterprise; Central është **autoriteti** i regjistrit, s'ka `owner_ref` |
| `core/tenancy.py`, `models/tenant.py`, `services/enterprises.py`, `models/enterprise.py` | **KEEP IN ENTERPRISE** | kalimtare (M1d/M13) dhe regjistri lokal; autoriteti është vendim i M4/M7 → për pjesën "regjistër" **NOT READY** |
| `core/security.py` | **NOT READY** (përzierje) | `Principal`/`ROLE_PERMS` janë të pastra, por auth lidhet me ORM+FastAPI; rolet e Central janë të panjohura |
| `services/audit.py`, `AuditLog` | **KEEP IN ENTERPRISE** (forma e rreshtit: kandidat kontrate më vonë) | varet nga `Principal` dhe ORM; Central do të ketë audit të vetin |
| Idempotency | **NOT READY / vlerë e ulët** | dy kopje me fusha të ndryshme; mos nxirr `request_digest` pa nevojë (byte-identik i detyrueshëm) |
| Ledger/money (`wallet`) | **NOT READY** | pronësia ndahet në M9 (Central autoritet komercial, Enterprise ledger operacional); mos e nxirr |
| **Gabimet bazë** (`WalletError`, `NotFound`, `Conflict`) | **MOVE TO KERNEL** (me emër neutral `DomainError` + alias) | pa varësi, semantikë e qëndrueshme, 29 modula varen nga wallet **vetëm** për to |
| **`utcnow`** (+ `as_utc`) | **MOVE TO KERNEL** (`kernel.time`) | pure; 11 modele varen nga money model **vetëm** për të |
| `MONEY` | **NOT READY** | primitive e parave (M9) |
| `core/crypto.py` | **KEEP IN INFRASTRUCTURE** | kërkon çelës nga `config`; kernel s'duhet të lexojë config (do ishte injektim) |
| `core/db.py`, `core/config.py` | **KEEP IN INFRASTRUCTURE** | `Base`/engine/settings janë të çdo aplikacioni (Central ≠ Enterprise DB); **kernel s'duhet të zotërojë `Base`** |
| `providers/base.py` | **KEEP (Gateway, logjik)** | SPI Gateway↔provider; s'është kontratë Enterprise↔Central; pastër, e lëvizshme më vonë |
| Webhook envelope + `sign` + katalogu i event-eve | **MOVE TO CONTRACTS** (në fazë të vonë) | kontratë e jashtme e versionuar; sot e lidhur me ORM `Event` → duhet DTO i pastër dhe golden bytes |
| Pydantic DTO `api/*` | **KEEP IN ENTERPRISE** | API publike e Enterprise (OpenAPI); s'ndahet me Central |
| `net_guard`, `sms_text` | **KEEP (Gateway)** | domain i webhook/SMS |
| `approvals` | **NOT READY** | M10 (sender/shtet nga Central) mund ta ndajë |
| `switches` | **KEEP IN ENTERPRISE** | kill switch operacional |

## 4. Coupling i fshehur (duket shared, në fakt jo)
1. **`models/wallet.py` si "baza e të gjitha modeleve":** 11 modele e importojnë vetëm për `utcnow`; çdo model varet nga moduli i parave.
2. **`services/wallet.py` si "bazë e gabimeve":** 29 module importojnë prej tij vetëm klasat e gabimeve; `WalletError` është baza e 30 nënklasave të çdo domain-i.
3. **`core → services` (lazy):** `context`, `tenancy`, `readiness` thërrasin `services.enterprises`; shtresa e poshtme varet nga e sipërmja (fshehur nga imports lazy) dhe formon të vetmin cikël.
4. **`scope.cross_tenant` shkruan `AuditLog` direkt** (lazy `models.admin`), paralel me `services.audit.audit`: dy rrugë audit.
5. **`TenantContext.owner_ref`:** fushë përputhshmërie Enterprise brenda "primitives të përbashkëta"; Central s'do ta ketë.
6. **Hartëzimi gabim→HTTP:** 10 fjalorë `_STATUS` te `api/*` mbi kodet e gabimeve; kontrata e kodeve është implicite.
7. **`Settings` monolit** (provider, Twilio, queue/DB, tenant) dhe **`Base` i vetëm**: çdo "kernel" që prek `config`/`db` do të ngarkonte Enterprise.
8. **`webhooks.envelope(ev: Event)`:** kontratë e jashtme e ndërtuar mbi ORM; ndryshimi i modelit ndryshon bytes të nënshkruara.
9. **`wallet → events` (lazy):** parat emetojnë event webhook (`wallet.low_balance`): lidhje money↔outbox.

## 5. Rreziqe ciklesh
- Cikli ekzistues (§1) shqetësues vetëm kur të ndahen paketa: `kernel` nuk guxon të përmbajë `Owner`/`scope`. Zgjidhja natyrore: gabimet dhe `utcnow` jashtë `wallet` e prishin ciklin (`enterprises → wallet` zhduket).
- Kërcënimi i ri: `contracts` që importon `models.events` (ORM) → cikël me `services.events`. Rregull: contracts pa ORM.
- `app.models.__init__` ngarkon `core.tenancy` (listener global); çdo zhvendosje e `Base`/models ndryshon rendin e regjistrimit të metadata (alembic autogenerate).
- Rrezik import-path: 31 consumers të `services.wallet` — alias i detyrueshëm gjatë kalimit.

## 6. Çfarë mund të zhvendoset **pa ndryshim sjelljeje**
| Ndryshim | Veprimi | Mbrojtja |
|---|---|---|
| `utcnow` jashtë `models/wallet.py` (te `core/timeutil`/kernel `time`); `models.wallet.utcnow` mbetet re-export | 11 modele ndryshojnë importin; asnjë tjetër | import-direction test + suite |
| `DomainError`/`NotFound`/`Conflict` (+ alias `WalletError = DomainError`) në modul neutral | 29 module mund të migrohen gradualisht; alias ruan të gjitha | snapshot i kodeve të gabimit (`.code`) për çdo nënklasë + `_STATUS` |
| (më vonë) DTO i envelope-it v1 + golden bytes | `envelope()` deleguar | test golden i `sign()` dhe i bytes të envelope-it |
| `app/queue` | nuk lëviz sot | guard-et ekzistuese |

## 7. Testet/guard-et që mbrojnë kufijtë pas extraction
- **Import-direction (AST)**, të rinj: `kernel` vetëm stdlib (jo SQLAlchemy ORM, jo FastAPI, jo `app.*`); `contracts` pa ORM/FastAPI/`app.models`; `core → services` e ndaluar me allowlist që **tkurret** (sot 3 lazy); asnjë cikël top-level; cikli lazy i njohur i ndjekur deri në 0.
- **Snapshot i kodeve të gabimit** (subclass → `.code`) dhe i `Base.metadata` (tabela/kolona) kundër zhvendosjeve të pavëmendshme.
- **Golden:** `sign()` dhe bytes e `envelope()` (para çdo zhvendosjeje të kontratës).
- **Ekzistuese të ruajtura:** karakterizimi M2, SQL count guards (SMS 100/email 93/webhook 37 statements), concurrency PG, A→B tenant tests M1c, `test_queue_boundaries`, authz matrix.
- **Performance guard** (≤5%): vetëm nëse prekim path-e hot (importet nuk e ndryshojnë; do ta kontrollojmë me smoke).

## 8. Fazat e propozuara (të vogla)
- **M3-b — zbërthim në vend, pa paketa të reja:** (i) `utcnow` te `core/timeutil`; (ii) gabimet bazë te modul neutral i `core` me alias në `services/wallet`; (iii) guard-et e §7. Efekt: prish ciklin, heq coupling-un money→gjithçka.
- **M3-c — formalizim i shtresave:** rregulla importesh të testuara (`core ← models ← services ← api`), zero `core→services` lazy (dependency inversion minimale për `enterprises`), një rrugë audit.
- **M3-d — contracts v1:** katalog event-esh + `EventEnvelopeV1` + specifikim nënshkrimi, golden tests; `webhooks.envelope` delegon.
- **M3-e — paketim:** vendim i shprehur për `app/kernel` dhe `app/contracts` brenda të njëjtës distribuim (guard-et i detyrojnë kufijtë); ndarja në `packages/`/`apps/` **shtyhet deri sa të ketë konsumator të dytë (Central, M4)**. `gateway` mbetet kufi logjik (providers + messages/emails/webhooks/queue), jo shërbim.

## 9. Vlerësimi i rrezikut
| Rreziku | Niveli | Mjetësimi |
|---|---|---|
| Ndryshim i heshtur i kodeve të gabimit/HTTP | Mesatar | snapshot `.code` + testet API ekzistuese |
| Ndryshim i bytes të webhook-ut (nënshkrim) | Lartë nëse prekim envelope | golden tests para kontratës (M3-d) |
| Over-extraction (kernel si "utils") | Mesatar | kriteret e pranimit për çdo anëtar; vetëm 2 anëtarë fillestarë |
| Cikël i ri (contracts↔ORM, kernel↔config) | Mesatar | guard-et AST; `kernel` stdlib-only |
| `Base`/metadata dhe alembic | Mesatar | nuk lëvizim `Base`/models; snapshot metadata |
| Prekja aksidentale e tenant/queue | Ulët | guard-et M1c/M2 + SQL count; M3 nuk prek `scope/context/queue` |
| Money/ledger i hershëm | Lartë | **jashtë M3** (M9) |

## 10. Ndryshimi i parë minimal i rekomanduar (për miratim; nuk është bërë)
**M3-b(i): `utcnow` nga `models/wallet.py` te `core/timeutil.py`** (`models.wallet.utcnow` mbetet re-export), 11 importe modelesh ndryshojnë.
Zero ndryshim sjelljeje, zero migrim, heq varësinë e çdo modeli nga moduli i parave. Pas tij, i njëjti stil për gabimet bazë (M3-b(ii)).

---
## M3-b(i) — ZBATUAR (miratuar nga pronari)
`utcnow` u zhvendos nga `models/wallet.py` te `core/timeutil.py` (burimi i vetëm). `models.wallet.utcnow` mbetet **alias i përkohshëm
përputhshmërie** (i njëjti objekt: `models.wallet.utcnow is core.timeutil.utcnow`); 11 modele (`admin, billing, campaigns, contacts, email,
enterprise, events, inbound, messaging, rates, sending`) e importojnë tani nga burimi neutral. Garda: `tests/test_m3_timeutil.py`
(AST: asnjë modul s'importon `utcnow` nga `models.wallet`; snapshot golden i metadata ORM `tests/golden/orm_metadata.json`: 42 tabela, 417
kolona, default-et callable; PG: alembic autogenerate pa diff). Nuk ka migrim, nuk ka ndryshim skeme/sjelljeje. Hapi tjetër (M3-b(ii): gabimet
bazë) pret miratim.

---
## M3-b(ii) — ZBATUAR (miratuar nga pronari): gabimet bazë te `app/core/errors.py`
- `app/core/errors.py` (pa asnjë import nga `app`): `DomainError` (`code="wallet_error"`, parazgjedhja e trashëguar), `NotFound` (`not_found`), `Conflict` (`conflict`).
  `WalletError` ishte `class WalletError(Exception): code = "wallet_error"` pa konstruktor/atribute/serializim → u migrua saktësisht.
- `services.wallet`: `WalletError = DomainError` (alias, i njëjti objekt), `NotFound`/`Conflict` ri-eksportuar; `InsufficientFunds`/`InvalidAmount` mbeten
  te wallet (wallet-specifike; `InvalidAmount` përdoret edhe nga rates/billing/payments: coupling i raportuar, jo i prekur).
- 30 module migruan importet (nga `services.wallet` te `core.errors`; `WalletError`→`DomainError` në trup); 3 prej tyre (`billing`, `payments`, `rates`) importojnë
  ende `InvalidAmount`/`money` nga wallet. `api/wallets.py` (module wallet) mbetet i pandryshuar.
- Fan-in top-level i `services.wallet`: 31 → 8. Cikli lazy me 5 module u hoq plotësisht (`enterprises → wallet` ishte edge-i i vetëm i varur nga gabimet).
- Guard-et: `tests/test_m3_errors.py` (snapshot i 47 klasave të gabimit, hartëzimi `_STATUS` (10 fjalorë), alias identitet, payload API, AST, zero cikle).
