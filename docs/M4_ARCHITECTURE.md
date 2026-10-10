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

---
## M4-c — Bootstrap i Enterprise-ve ekzistues në Central

### Audit i formës së të dhënave
- **Si krijohen rreshtat sot** (migrimi `0018` + `enterprises.backfill_missing` + `resolve_id`): vetëm `id` (UUID v4), `owner_ref`, `status='active'`, `created_at`, `updated_at`. `external_id`, `legal_name`, `short_name` janë **gjithmonë NULL** (asnjë kod i shkruan; vendimi M1 #2: metadata nuk plotësohen nga supozime). `status` është gjithmonë `active` (asnjë kod nuk e ndryshon).
- **Qëndrueshmëria e `owner_ref`:** unik (`UNIQUE` + indeks unik `lower(owner_ref)`), i ruajtur saktësisht (pa normalizim), max 64. Anomalitë (bosh, hapësira, variante shkronjash) nuk futen në `sms_enterprises` (auditi i bllokon).
- **Ripërdorimi i `id`:** po. `sms_enterprises.id` është UUID v4 kanonik dhe `enterprises.id` në Central është UUID; tipet përputhen (PG `uuid`, SQLite `CHAR(32)`). Asnjë UUID i ri në bootstrap; pa shtresë përkthimi.
- **Përplasje / rreshta të paplotë:** në një DB reale s'ka përplasje id/owner_ref (kufizime). Mbrojtja e mbetur: DB e korruptuar/e ndryshuar manualisht → raportohet si e pavlefshme, s'shkruhet.
- **Burimi i `name`:** vetëm `owner_ref` ka vlerë reale; politika `legal_name → short_name → owner_ref` (`external_id` përjashtohet: është identifikues, jo emër). Emrat nga `owner_ref` janë **provizorë** (raportohen; rename te Central më vonë).

### Vendimi për `owner_ref` (A/B/C)
| Opsion | Pro | Kundër |
|---|---|---|
| A. kolonë `legacy_owner_ref` te `enterprises` | kërkim i thjeshtë | ndot entitetin kanonik; migrim skeme; `owner_ref` s'ka kuptim në Central |
| B. tabelë mapimi | modeli kanonik i pastër | skemë e re, ruajtje e përhershme për një nevojë të përkohshme |
| **C. asnjë ruajtje (zgjedhur)** | zero ndryshim skeme; ID ruhet 1:1 prandaj mapimi s'nevojitet | `owner_ref` s'gjendet në Central (s'duhet: Enterprise e mban vetë) |
`owner_ref` përdoret vetëm për fallback-un e emrit dhe për raport. **Zero ndryshim skeme.**

### Algoritmi (`python -m apps.central.tools.bootstrap_enterprises [--dry-run]`)
1. Lexon Enterprise DB (`ENTERPRISE_DATABASE_URL`) me SQL minimal në transaksion vetëm-lexim (PG: `SET TRANSACTION READ ONLY`); pa ORM të Enterprise, engine/session i veçantë. Central DB = `CENTRAL_DATABASE_URL` (`settings.database_url`). Të dyja URL-të e njëjta → refuzohet.
2. Planifikon plotësisht para çdo shkrimi: për çdo rresht → i pavlefshëm | konflikt | përputhet | krijo.
3. Nëse ka konflikte ose të pavlefshme: **asgjë nuk shkruhet** (kodi 1). Përndryshe, krijimet shkruhen në **një transaksion të vetëm** në Central (all-or-nothing; trade-off: një rresht i keq bllokon të gjithë, por bootstrap-i është i vogël dhe rerun është idempotent, ndaj ky është versioni më i sigurt).
4. Raport (pa URL/sekrete): `Scanned / Create / Matching / Conflicts / Invalid / Central-only / Name sources / Mode`, plus rreshta `CONFLICT enterprise_id=… reason=… source=… target=…` dhe `INVALID …`.

### Rregullat e hartimit
- `id` → `id` (ruhet). `name` = `normalize_name(legal_name | short_name | owner_ref)` (strip, 1..200, pa karaktere kontrolli; i gjatë → i pavlefshëm, nuk shkurtohet).
- `status`: tabelë eksplicite `active→active`, `suspended→suspended`; çdo tjetër (përfshirë NULL) → i pavlefshëm.
- `created_at`/`updated_at` ruhen nga burimi (aware UTC; naive trajtohet UTC); mungesa → ora e bootstrap-it.
- I pavlefshëm: id mungon/dyfishtë në burim; `owner_ref` mungon ose dyfishtë (pa dallim shkronjash/hapësirash); status i panjohur; asnjë burim emri i përdorshëm.

### Idempotenca dhe konfliktet
Id ekziston në Central me të njëjtin `name`+`status` → no-op (timestamps nuk krahasohen). Id ekziston me të dhëna tjetër → **konflikt**, pa mbishkrim (p.sh. pas një rename të qëllimshëm te Central: pritet; bootstrap-i është për importin fillestar, jo reconciliation). Rreshta vetëm në Central → të paprekur, vetëm të numëruar. Asnjë fshirje. Garë me krijim paralel → transaksioni dështon i tëri (kodi 2).

### Kufijtë
Pa sync runtime (pa event, polling, scheduler, webhook, worker, dual-write, transaksion të përbashkët), pa shkrim prapa te Enterprise, pa import `app.*` (guard AST), Central runtime s'e importon kurrë `tools/`. Sync-u vjen në M7.

### Runbook
1. `ENTERPRISE_DATABASE_URL=… CENTRAL_DATABASE_URL=… alembic -c apps/central/alembic.ini upgrade head` (Central në `0002`).
2. `python -m apps.central.tools.bootstrap_enterprises --dry-run` → kontrollo raportin (Conflicts/Invalid duhet 0; shih "Name sources").
3. Pa `--dry-run` për të aplikuar; përsërit për verifikim (`Create: 0`, `Matching: N`).
4. Pas bootstrap-it, emrat provizorë ndryshohen te Central (`rename`).
Kodet e daljes: 0 ok · 1 konflikte/të pavlefshme (asgjë e shkruar) · 2 konfigurim/lidhje/gabim.

---
## M4-d — Autentikimi dhe autorizimi bazë i Central

### Audit i autentikimit ekzistues (Enterprise) — çfarë ripërdoret vetëm si ide
| Pjesë | Sot në Enterprise (`app/core/security.py`, `totp.py`, `models/admin.py`) | Për Central |
|---|---|---|
| Identitet | **Nuk ka model përdoruesi/fjalëkalimi.** Autentikimi = çelësa API (`sms_<prefix>_<sekret>`, SHA-256 i sekretit, `compare_digest`) + çelës bootstrap `SMS_ADMIN_API_KEY` | Central ka nevojë për **staf me email+fjalëkalim** (njeri në konsolë), jo çelësa makine |
| Fjalëkalime | s'ka (SHA-256 është i përshtatshëm vetëm për sekrete të rastit me entropi të lartë, jo për fjalëkalime) | KDF e ngadaltë e provuar: **Argon2id** |
| Token/seancë | s'ka (çelësi është kredenciali; `last_used_at`) | token me afat të shkurtër |
| Role/leje | `ROLE_PERMS` (superadmin, finance, pricing, approver, support, client) + `Principal.has()` | **vetëm 2 role** (`admin`, `operator`); pa matricë lejesh |
| 2FA | TOTP (RFC 6238) si hap i dytë te çelësat e stafit për veprime të ndjeshme | jashtë scope-it të M4-d (rrugë e hapur, shih borxhin) |
| Mbrojtje brute-force | tabela `AuthFailure` për IP (429) | **jo implementuar** (borxh i shënuar) |
Përfundim: asnjë primitive e Enterprise nuk ripërdoret as importohet; vetëm idetë (krahasim në kohë konstante, përgjigje e njëtrajtshme, rol i ndarë nga identiteti). Pa tabelë, sekret ose rol të përbashkët.

### Skema (`users`, migrimi `0003`)
`id` UUID PK (v4) · `email` String(254) UNIK, i ruajtur i normalizuar (`strip` + `lower`; CHECK `email = lower(trim(email))`) · `password_hash` String(255) (Argon2id, format vetë-përshkrues) · `role` `admin|operator` (CHECK; një kolonë, jo tabelë rolesh) · `status` `active|disabled` (CHECK) · `created_at`, `updated_at`.
Email: formë minimale `local@domain.tld`, pa hapësira/karaktere kontrolli, max 254; pa verifikim email-i; krahasimi vetëm në formën e normalizuar.

### Mekanizmi
- **Login:** `POST /auth/token` `{email, password}` → `{access_token, token_type:"bearer", expires_in}`. Përgjigje **e njëtrajtshme 401** për email të panjohur / fjalëkalim gabim / përdorues i çaktivizuar (pa zbuluar ekzistencën; kosto e njëjtë me hash fiktiv kur përdoruesi s'ekziston). Sekreti i paconfiguruar → 503.
- **Token:** JWT **HS256** (PyJWT), algoritmi konstant (nuk lexohet nga token-i), `iss=sms-central`, `aud=sms-central-admin`, `sub`=user id, `iat`, `exp`, `jti`; **roli dhe statusi s'janë në token**. TTL `CENTRAL_AUTH_TTL_SECONDS` (default 900 s, 60..86400); pa refresh token.
- **Çdo kërkesë e mbrojtur:** dekodim (nënshkrim, `exp`, `iss`, `aud`, claims të kërkuara) → lexim i përdoruesit nga DB → duhet `active`. Roli lexohet nga DB në çdo kërkesë.
- **Kufizimet e revokimit (stateless):** s'ka revokim për token të veçantë/logout; një token i vlefshëm i një përdoruesi aktiv punon deri në `exp`. Çaktivizimi i përdoruesit dhe ndryshimi i rolit vlejnë **menjëherë** (kontroll DB). Rikthimi i përdoruesit aktiv ri-aktivizon token-at ende të pa-skaduar. Ndryshimi i fjalëkalimit nuk i anulon token-at ekzistues (s'ka ende rrjedhë ndryshimi fjalëkalimi).
- **Sekreti:** `CENTRAL_AUTH_SECRET` (min 32 karaktere; i veçantë nga çdo sekret i Enterprise). Bosh/i shkurtër → login dhe endpoint-et e mbrojtura 503; me `CENTRAL_ENV=production` aplikacioni **refuzon të niset**. Rotacioni i sekretit i pavlefëson menjëherë të gjitha token-at (s'ka `kid` ende).
- **Fjalëkalime:** Argon2id (argon2-cffi, parametrat e paracaktuar të bibliotekës; asnjë algoritëm i vetë-shkruar), gjatësi 12..128; hash i keq/i prishur → verifikim `False` (kurrë përjashtim); rehash automatik në login nëse parametrat e bibliotekës rriten.

### RBAC minimal
`admin`, `operator`; `require_role(*roles)` si dependency eksplicite (403 `forbidden` për rol tjetër, 401 pa token). Sot: `/auth/me` për çdo përdorues aktiv; `/admin/ping` vetëm `admin`. Pa policy engine dhe pa matricë lejesh; lejet fine shtohen kur të ketë CRUD (M5+).

### Endpoint-et (vetëm auth/probë; pa CRUD biznesi)
`POST /auth/token`, `GET /auth/me`, `GET /admin/ping`, plus `/healthz`, `/readyz`. Pa regjistrim/self-signup: stafi krijohet vetëm nga CLI.

### Admin-i i parë
`python -m apps.central.tools.create_admin --email x@y.com [--role admin|operator]` me `CENTRAL_DATABASE_URL`; fjalëkalimi nga `CENTRAL_ADMIN_PASSWORD` ose terminal (getpass, dy herë), **kurrë nga argumentet**, pa parazgjedhje. Idempotent: ekziston me të njëjtin fjalëkalim+rol → no-op (0); ekziston me ndryshe → dështim i pastër pa ndryshim (1); gabim input/konfigurim (2). Nuk printon fjalëkalim, hash, token ose URL.

### Logim
`central.auth`: `login ok user=<uuid> ip=…`, `login failed reason=<unknown_user|bad_password|disabled> ip=…` (arsyeja vetëm në log, jo në përgjigje); asnjëherë fjalëkalim/hash/token/email (testuar me `caplog`). Veprimet e ndjeshme (krijim, çaktivizim) kalojnë nga funksione të vetme të service-it, gati për audit të ardhshëm; nuk u ndërtua `AuditLog` i plotë.

### Dizajn i ardhshëm: auth shërbim-te-shërbim Central↔Enterprise (NUK implementohet)
| Opsion | Vlerësim |
|---|---|
| **Client credentials + token i shkurtër i nënshkruar asimetrikisht (EdDSA/RS256, `kid`, `aud`=shërbimi, `scope`)** | **Rekomandimi.** Central lëshon token 5 min për identitet shërbimi (tabela `service_credentials` e planifikuar, sekret i hash-uar); Enterprise verifikon me çelës publik, pa sekret të përbashkët; rotacion me `kid`; kontratat e kërkesës/përgjigjes në `contracts` të versionuara |
| HMAC mbi kërkesën (si webhook V1: `ts.body`) | e thjeshtë, por sekret i përbashkët + menaxhim çelësash për çdo çift; mirë për dërgime push nga Central |
| mTLS | shtresë rrjeti shtesë (identitet transporti), jo zëvendësim i autorizimit; opsionale kur ka infrastrukturë |
| JWT HS256 i përbashkët | **refuzohet**: sekret i përbashkët mes planeve |
Këto kanë kuptim vetëm me M7 (sync); në M4-d s'ka endpoint sync as thirrje drejt Enterprise.

### Borxhe / risqe të M4-d
- **Pa limitim përpjekjesh login** (brute-force/credential stuffing): zbutje e rekomanduar te reverse proxy (nginx `limit_req`) ose tabelë dështimesh/mbyllje llogarie (kujdes DoS mbi llogari admin); i regjistruar para go-live.
- Pa MFA/TOTP për stafin e Central; pa rrjedhë ndryshimi/rivendosjeje fjalëkalimi; pa revokim për token; pa `kid` për rotacion sekreti.
- `argon2-cffi` dhe `PyJWT` u shtuan te `requirements.txt` të përbashkët (imazhi Enterprise i instalon, por nuk i përdor).
- Aplikacioni Central ende nuk ka imazh Docker/proxy; TLS pritet nga proxy.
- Email-i normalizohet me `lower()` (jo `casefold`/IDNA): domene IDN me dallime Unicode trajtohen si të ndryshme.
