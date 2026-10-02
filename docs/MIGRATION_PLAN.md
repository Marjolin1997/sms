# Plani i migrimit: nga monoliti multi-tenant te Central + Enterprise (+ Gateway logjik)

Bazuar në `docs/ARCHITECTURE_AUDIT.md` (vendimet e fiksuara). **Ky dokument nuk përmban kod implementimi.** Asnjë fazë nuk fillon pa miratimin tënd.

## Parime (vlejnë për çdo fazë)
1. **Expand → migrate → contract**: shto strukturën e re pa hequr të vjetrën, dyfisho shkrimin, kalo leximet, vetëm në fund hiq të vjetrën.
2. **Prapavajtje-kompatibilitet**: API-ja publike ekzistuese (përfshirë `owner_ref` në trupa/parametra dhe `/v1/*`) nuk prishet; shtohet e re, e vjetra shënohet e vjetruar.
3. Çdo fazë ka **migrim Alembic** (up/down të provuar në PostgreSQL dhe SQLite), **teste**, **rikthim**, **kritere pranimi**. Suita ekzistuese (379 backend, 47 frontend, e2e 74) duhet të mbetet e gjelbër në çdo hap.
4. **Dërgimi ekzistues i SMS-ve nuk duhet të ndalet**: çdo fazë që prek `messages.submit`, workers ose provider-at kërkon të kaluara `tests/test_postgres.py` (konkurrenca) dhe krahasim me `scripts/bench.py` (pa regres > 10%).
5. **Një fazë prek sa më pak module**; feature flags për sjellje të reja.
6. Izolimi i tenant-ëve provohet automatikisht (`tests/test_authz_matrix.py`, `tests/test_tenant_isolation.py`) dhe zgjerohet në çdo fazë.

## Korrigjime të miratuara nga pronari (kanë përparësi mbi tekstin më poshtë)
1. **M1a është strikt additiv**: vetëm `sms_enterprises` + backfill + `enterprises.for_owner_ref()` + teste + rikthim. Pa scoping, autorizim, dërgim, ledger, fushata, API. Sistemi sillet identikisht para dhe pas.
2. **`owner_ref` nuk është identifikues biznesi i përhershëm.** UUID `id` është identiteti kanonik; `owner_ref` mbetet vetëm për pajtueshmëri. Metadata (`legal_name` etj.) nuk plotësohen nga supozime: mbeten `NULL`.
3. **Asnjë normalizim/bashkim automatik** i `owner_ref`. `CLIENT_A`, `client_a`, `client_a ` nuk bashkohen; migrimi ndalon dhe raporton, përplasjet vetëm nga ndarësit raportohen si paralajmërim.
4. **M1b: dual-write i centralizuar, jo dhjetëra thirrje manuale.** Një mekanizëm i vetëm (mixin/ORM event me një resolver të vetëm) plotëson `owner_ref` + `enterprise_id` që t'i referohen gjithmonë të njëjtit Enterprise; invariant i verifikueshëm: `record.owner_ref == enterprise.owner_ref for record.enterprise_id` (kontroll periodik + në teste).
5. **M1c: tre kontekste të ndara** — *tenant*, *system/admin*, *worker*. Tenant API është secure-by-default; punët administrative dhe rikonsilimi punojnë **eksplicitisht** cross-tenant; **jo** filtër global implicit që fsheh gabime ose prek punët e sistemit. Çdo qasje cross-tenant është e qëllimshme dhe e audituar. **Standard testi negativ:** Enterprise A tenton të lexojë UUID-në e një burimi të Enterprise B → 404/403 pa asnjë informacion që rrjedh, për: contacts, lists, campaigns, messages, sender IDs, API keys, webhooks, reports.
6. **`MessageQueue` sinkrone** (`publish/reserve/acknowledge/retry`); asnjë adapter async pa backend që e kërkon realisht.
7. **Politika e konfigurimit të vjetruar është sipas llojit, jo një TTL universal:** çmimet → *last-known-good* + alarm; konfigurimi i produktit → *last-known-good* + alarm; rrugët → *last-known-good* kur është e sigurt; miratimi i sender-it → **fail closed** kur s'mund të provohet; revokimet e sigurisë → **fail closed** / sinkronizim me prioritet të lartë.
8. **Paratë (M9):** "Central i padisponueshëm" nuk i jep Enterprise të drejtë të krijojë kredi. Enterprise shpenzon vetëm kredinë **e dhënë dhe të rikonsiliuar lokalisht**. Çdo tavan offline është pjesë e grant-it të fundit të autorizuar nga Central.
9. **M12 (Gateway fizik) është opsional**; migrimi nuk konsiderohet i paplotë nëse Gateway mbetet i njëjti deployment, me kusht që kufiri modular të jetë i pastër dhe performanca/siguria të jenë të pranueshme. Nxjerrja bëhet vetëm për: shkallëzim i pavarur, izolim kredencialesh, deploy të pavarur, ulje të blast-radius, compliance.

## Inventari i sotëm (nga kodi)
- **21 tabela** mbajnë drejtpërdrejt `owner_ref` (api_keys, wallets, account_plans, messages, emails, email_domains, contacts, contact_lists, consent_events, consent_state, campaigns, sender_ids, templates, events, webhook_endpoints, inbound_messages, keywords, billing_profiles, subscriptions, invoices, payments); të tjerat e trashëgojnë (ledger/holds/topups nga wallet, list_members, template_versions, campaign_recipients, message_events, webhook_deliveries, invoice_lines).
- ~500 referenca `owner_ref` në kod: më të ngarkuarat `app/api/console.py`, `app/api/contacts.py`, `app/services/contacts.py`, `app/api/portal.py`, `app/services/campaigns.py`, `app/services/billing.py`, `app/api/billing.py`, `app/services/consent.py`, `app/services/inbox.py`, `app/services/webhooks.py`, `app/services/apikeys.py`; 64 në teste; 43 në frontend.
- Kufiri i pronësisë sot: `Principal.owner_ref` + `Principal.check_owner()` + `owner_for()` te `app/api/contacts.py` + filtra `owner_ref == …` të shkruar me dorë në çdo shërbim.
- Queue sot: outbox në tabelat `sms_messages`, `sms_emails`, `sms_webhook_deliveries`, `sms_campaign_recipients` me `SKIP LOCKED` te `app/services/messages.py`, `emails.py`, `webhooks.py`, `campaigns.py`, të ngarkuara nga `app/worker.py`. Shërbimet janë **sinkrone** (SQLAlchemy `Session`).

## Struktura e fazave dhe rendi i varësisë
```
M0 Rrjeta e sigurisë ─┬─► M1 Identiteti Enterprise (owner_ref → enterprise_id) ─┬─► M6 Users/RBAC
                      │        │                                              ├─► M7 Sinkronizimi ─► M8 Regjistrim + Provisioning
                      │        └─► M2 Kontrata MessageQueue (e pavarur)       │        │
                      │                                                       │        ├─► M9 Paratë: kredi + rikonsilim
                      └─► M3 Kernel + kontrata (kufij kodi) ─► M4 Central bazë ─► M5 Katalog + EnterpriseProduct ─┘        └─► M10 Sender/shtet nga Central
                                                                                                                          │
                                       M11 Funksionet tregtare kalojnë te Central; frontend-et ndahen  ◄─────────────────┘
                                       M12 (opsionale) Nxjerrja fizike e Gateway  ·  M13 Pastrimi (heq owner_ref)
```
Varësi të ngurta: **M1 → M6, M7, M9**; **M3 → M4 → M5 → M7 → (M8, M9, M10) → M11**; M2 është e pavarur (mund të bëhet paralel me M3); M12 dhe M13 janë të fundit dhe kushtëzohen.

---

## M0 · Rrjeta e sigurisë
- **Objective:** matë dhe mbrojmë sjelljen ekzistuese para se të prekim strukturën.
- **Tables:** asnjë.
- **Files/modules:** `tests/` (zgjerim), `scripts/bench.py` (baseline i ruajtur në `docs/PERFORMANCE.md`), CI.
- **Migration strategy:** shto **test skanues** që liston çdo tabelë me `owner_ref` dhe dështon nëse shfaqet e re pa u regjistruar; shto testin që çdo query tenant-scoped përmban filtër (inventar).
- **Backwards compat:** e plotë (vetëm teste).
- **Tests:** testi i inventarit; baseline performance.
- **Rollback:** heq testet.
- **Acceptance:** CI e gjelbër; inventari = 21 tabela; baseline i dokumentuar.

## M1 · Identiteti Enterprise: `owner_ref` → `Enterprise` + `enterprise_id`  (ndryshimi strukturor i parë)
- **Objective:** entitet real `enterprises` (UUID) dhe `enterprise_id` në çdo tabelë tenant, me izolim të zbatuar automatikisht.
- **Tables affected:** **e re** `sms_enterprises(id UUID PK, external_id UNIQUE, legal_name, short_name, status, created_at, updated_at)`; **të prekura:** të 21-at + lidhjet trashëguese më sipër marrin `enterprise_id UUID` (FK, indeks). `external_id` = `owner_ref` ekzistues (rruga e kalimit).
- **Files/modules affected:** `app/models/*` (mixin `TenantOwned`), `app/core/security.py` (`Principal.enterprise_id`; `owner_ref` mbetet alias), `app/api/contacts.py::owner_for` → `tenant_for`, shërbimet që filtrojnë `owner_ref`, `app/services/apikeys.py`, `scripts/seed_demo.py`, frontend `api.js` (dërgon ende `owner_ref`).
- **Migration strategy (expand/migrate/contract, 4 nën-hapa, secili release i veçantë):**
  1. **M1a** krijo `sms_enterprises` + backfill nga `SELECT DISTINCT owner_ref` (unioni i të gjitha tabelave), `legal_name=owner_ref`; shërbimi `enterprises.for_owner_ref()`. *Zero ndryshim sjelljeje.*
  2. **M1b** shto `enterprise_id` **nullable** + indeks në çdo tabelë; backfill me batch (`UPDATE … FROM sms_enterprises` me `LIMIT`/id-range, jo një UPDATE gjigant); **dual-write**: mixin plotëson `enterprise_id` automatikisht nga `owner_ref` në insert; testi "shadow" pohon `enterprise_id` përputhet me `owner_ref`.
  3. **M1c** kalo **leximet**: `Principal.enterprise_id`; scoping i detyruar me `do_orm_execute` + `with_loader_criteria` mbi mixin (çdo SELECT/UPDATE/DELETE e tabelave tenant merr `enterprise_id = :ctx` automatikisht; për staf/shërbim kërkohet `unscoped()` eksplicit, i regjistruar në audit); shto `NOT NULL` me `CHECK … NOT VALID` pastaj `VALIDATE` (pa kyçje të gjatë).
  4. **M1d (vonë, M13)** heq `owner_ref` nga tabelat; `external_id` mbetet.
- **Backwards compat:** API pranon `owner_ref` (rezolvohet në `enterprise_id` përmes `external_id`) dhe e kthen si më parë, plus `enterprise_id`; çelësat API ekzistues vazhdojnë; frontend-i nuk ndryshon.
- **Tests:** i gjithë suite ekzistues; testi i inventarit (M0) kalon nga `owner_ref` te `enterprise_id`; **test negativ**: query pa kontekst tenant hedh gabim; test shadow i dual-write; izolim tenantësh (i ekzistuesi) i përsëritur mbi `enterprise_id`; migrim up/down/up në PG me të dhëna; bench pa regres.
- **Rollback:** M1a/M1b: `downgrade` heq kolonat (të dhënat origjinale `owner_ref` janë të paprekura); M1c: `SMS_TENANT_SCOPING=owner_ref|enterprise` kthen leximet te `owner_ref` (zbatuar).
- **Acceptance:** 100% e rreshtave kanë `enterprise_id`; asnjë query tenant pa filtër (test); suite + bench të gjelbër; API e vjetër punon; audit tregon `unscoped()` vetëm te vendet e lejuara.

### M1a — ZBATUAR (strikt additiv)
- **Migrimi:** `alembic/versions/0018_enterprises.py`. Auditon `owner_ref` **para** se të krijojë ndonjë gjë; nëse ka anomali ndalon me raport të qartë (asgjë nuk krijohet/bashkohet). Krijon `sms_enterprises` dhe një rresht për çdo `owner_ref` legacy me UUID të gjeneruar një herë (persistent). Nuk prek asnjë tabelë tjetër (provuar me test që krahason skemën e çdo tabele para/pas), nuk bën UPDATE/backfill te tabelat e mëdha, nuk shton FK.
- **Kod:** `app/models/enterprise.py`, `app/services/enterprises.py` (`for_owner_ref`, `require_for_owner_ref`, `audit_owner_refs`, `backfill_missing`), `scripts/enterprises_audit.py`. Asnjë modul i rrugës së kërkesave nuk i importon (test i detyron).
- **Auditim para migrimit (rekomandohet në çdo mjedis):** `SMS_DATABASE_URL=… python -m scripts.enterprises_audit` (vetëm-lexim; dalja 1 nëse ka anomali).
- **Tenant-ët e krijuar pas M1a** nuk marrin Enterprise automatikisht (nuk ka lidhje në rrugën e kërkesave, me qëllim): `python -m scripts.enterprises_audit --backfill` i kap (idempotent); M1b e zëvendëson me shkrim të centralizuar.
- **Rikthimi:** `alembic downgrade 0017` heq `sms_enterprises` (dhe dy indekset e saj). **UUID-të humbin**; s'ka çfarë t'i referojë në M1a, dhe një `upgrade` i ri krijon UUID të reja. Të dhënat legacy nuk preken kurrë. *Pas M1b rikthimi kërkon kujdes shtesë (UUID-t referohen).*
- **Kyçje:** migrimi bën vetëm `SELECT DISTINCT owner_ref` (kyçje `ACCESS SHARE`, pa shkrime) mbi 21 tabelat dhe një `CREATE TABLE`; për tabela shumë të mëdha kjo është skanim vetëm-lexim.
- **Pranimi:** invariant `COUNT(DISTINCT owner_ref të vlefshëm) == COUNT(sms_enterprises)`; suita ekzistuese e pandryshuar.

### M1b — ZBATUAR (aditiv; dual-write i centralizuar)
- **Migrimi `0019`:** shton `enterprise_id UUID NULL` + indeks `ix_<tabela>_enterprise_id` te të 21 tabelat me `owner_ref`. **Pa FK, pa NOT NULL, pa backfill brenda migrimit** (M1c). PostgreSQL: `SET LOCAL lock_timeout='5s'` (dështon shpejt në vend që të bllokojë trafikun), kolonat janë vetëm metadata, indekset me `CREATE INDEX CONCURRENTLY IF NOT EXISTS` jashtë transaksionit.
- **`sms_consent_events` (i pandryshueshëm me trigger):** trigger-i zëvendësohet me `sms_consent_guard()` që lejon **vetëm** UPDATE që ndryshon vetëm `enterprise_id` nga NULL në vlerë (backfill); çdo ndryshim tjetër, kthimi/ndërrimi i vlerës dhe çdo DELETE mbeten të ndaluara (provuar në PostgreSQL). Rikthimi rikthen trigger-in origjinal.
- **Dual-write i centralizuar (një vend, jo në shërbime):** mixin `TenantOwned` (`app/models/tenant.py`) + listener `before_flush` (`app/core/tenancy.py`) + resolver i vetëm `enterprises.resolve_id()` (`INSERT … ON CONFLICT DO NOTHING` i sigurt në konkurrencë; cache për sesion). Tenant i ri → Enterprise i krijuar automatikisht. `enterprise_id` i dhënë që nuk i përket `owner_ref` → `TenantMismatch` (gabim programimi). Ndryshimi i `owner_ref` rezolvohet sërish. Asnjë shërbim/API nuk e shkruan `enterprise_id` (test i detyron).
- **Anomalitë s'ndryshojnë sjelljen:** `owner_ref` bosh/me hapësira/të gjatë/variant shkronjash → rreshti ruhet si më parë me `enterprise_id` NULL dhe raportohet (`SMS_ENTERPRISE_DUAL_WRITE_STRICT=true` i kthen në gabim). **Çelës rikthimi:** `SMS_ENTERPRISE_DUAL_WRITE=false` çaktivizon plotësisht shkrimin.
- **Backfill në batch (i rifillueshëm, idempotent):** `python -m scripts.backfill_enterprise_id [--batch 5000] [--table …]` (commit për batch, kursor `id`, kurrë UPDATE gjigant); refuzon nëse auditi ka anomali. `--check` vetëm raporton.
- **Invariant:** `enterprises.check_consistency()` / `scripts.enterprises_audit --check [--strict]`: për çdo rresht `record.owner_ref == enterprise.owner_ref` për `record.enterprise_id`; jo-përputhje DUHET të jenë 0; rreshta pa `enterprise_id` raportohen (`--strict` i trajton si dështim).
- **Rikthimi:** `alembic downgrade 0018` heq indekset, kolonat dhe rikthen trigger-in; `owner_ref` i paprekur. Nëse ka nevojë vetëm të ndalet shkrimi: `SMS_ENTERPRISE_DUAL_WRITE=false`.
- **Radha e rekomanduar në një mjedis me të dhëna:** `enterprises_audit` → `alembic upgrade head` (0018+0019) → aplikacioni me dual-write → `backfill_enterprise_id` → `enterprises_audit --check --strict` = 0/0. Vetëm pastaj M1c.

### M1c — ZBATUAR (skopim i shprehur me `enterprise_id`; pa filtër global)
Vendim i pronarit: **jo** `do_orm_execute`/`with_loader_criteria` global (fsheh query-t). Skopimi është një funksion i shprehur në çdo query.
- **Tre kontekste** (`app/core/context.py`, objekte të pandryshueshme që kalohen si argument; asnjë ContextVar/gjendje globale):
  - **TENANT** — `TenantContext(enterprise_id, owner_ref)`. Krijohet vetëm nga `api/tenant.py::tenant()` nga `Principal` (çelësi API mban `enterprise_id`); klienti nuk mund të zgjedhë tenant tjetër (`owner_ref` i huaj → 404 pa metadata). Stafi duhet ta emërtojë tenant-in shprehimisht (`owner_ref`), përndryshe 422.
  - **SYSTEM** — `SystemContext(actor, reason)` (`api/tenant.py::system()`, vetëm staf). Leximi ndër-tenant kalon **vetëm** nga `scope.cross_tenant(db, ctx, resource, action)`, që shkruan `sms_audit_log` (`cross_tenant.<veprim>`, me dedup 5 min për aktor/burim). Përdoret te radhët e shqyrtimit (sender-ids, templates), radha e top-up-eve dhe lista e llogarive.
  - **WORKER** — identiteti vjen nga rreshti/job-i: `context.worker_owner(db, row)` (`row.enterprise_id`; rresht legacy pa të → resolver vetëm-lexim për përputhshmëri; anomali pa identitet → rruga legacy e shprehur, që SMS-i të mos ndalet). Eventet trashëgojnë `enterprise_id` nga konteksti i burimit (`events.emit` e shkruan eksplicit). Inbound: tenant-i vjen nga rreshti `SenderId` i numrit.
- **Skopimi:** `scope.owned(Model, owner)` → për `TenantContext`: `enterprise_id == ctx.enterprise_id AND owner_ref == ctx.owner_ref` (të dyja duhet të përputhen; rresht me `enterprise_id` NULL ose të korruptuar është i padukshëm: **fail-closed**, pa fallback të heshtur). Për `str` (skripte, rrugë të pa migruara): rruga LEGACY e shprehur `owner_ref == …`, e numëruar te `scope.LEGACY_READS`. `scope.belongs(row, owner)` për rreshta të marrë me `db.get`; `api/tenant.py::scoped()` shton skopimin në SELECT sipas ID (klienti) ose e lë të lirë (stafi, i kufizuar nga leja e rrugës).
- **Resurset e migruara (M1c-b, M1c-c):** contacts, contact lists (+ anëtarë, audiencë), consent, templates, sender IDs (kërkesa + listë), keywords/inbox, messages, wallets/ledger/topups/balance, campaigns (+ recipients, estimate), API keys (self-service), webhooks (endpoints, deliveries, events), reports (usage, CSV), portal overview/onboarding, billing (profile, subscription, invoices, payments), email (domains, messages), inbound SMS. Shërbimet pranojnë `Owner = TenantContext | str`; API-t e klientit kalojnë gjithmonë `TenantContext`.
- **Çelës rikthimi:** `SMS_TENANT_SCOPING=owner_ref` (parazgjedhja `enterprise`) kthen skopimin te vetëm `owner_ref` (sjellja para M1c). `enterprise` **refuzon të niset** pa `SMS_ENTERPRISE_DUAL_WRITE=true`. `/readyz` kthen 503 me skopim `enterprise` nëse ekziston ndonjë rresht me `owner_ref` dhe pa `enterprise_id` (backfill i paplotë).
- **Ndryshime sjelljeje (të dokumentuara):** stafi që lexon një tenant pa asnjë shkrim ende (pa Enterprise) merr 404, jo listë bosh; `owner_ref` anomal në shkrim nga stafi → 422.
- **Nuk është bërë (M1d/M13):** `NOT NULL`/FK te `enterprise_id`, heqja e `owner_ref`, rruga `str` për `admin/billing/{owner}`, `admin/plans/{owner_ref}`, `admin/accounts`, `set_vat`, skriptet.
- **Testet:** `tests/test_tenant_context.py` (cikli i jetës, mungesa e rrjedhjes mes kërkesave/job-eve, worker), `tests/test_tenant_scoping.py` (A→B për contacts, lists, consent, templates, sender IDs, messages, wallets/ledger, campaigns, API keys, webhooks, reports/CSV, dashboard; çdo teste pohon që kërkesa e klientit **nuk** kalon nga rruga legacy).

## M2 · Kontrata `MessageQueue` (e pavarur nga M1)
- **Objective:** domain-i nuk di për PostgreSQL; brokeri mund të ndërrohet më vonë.
- **Tables:** asnjë e re (outbox ekzistues mbetet).
- **Files/modules:** `app/queue/` (Protocol + `PostgresOutboxQueue`), `app/services/messages.py`, `emails.py`, `webhooks.py`, `campaigns.py`, `app/worker.py`.
- **Migration strategy:** kontratë **sinkrone** (`publish / reserve / acknowledge / retry`, plus `dead_letter`), sepse shërbimet janë sinkrone; variant async shtohet vetëm kur ka backend që e kërkon. Nxirret logjika `SKIP LOCKED`/backoff/lease nga shërbimet te implementimi PostgreSQL, **një outbox në herë** (SMS → email → webhooks → campaign recipients), pa ndryshuar skemën.
- **Backwards compat:** e plotë (refaktorim i brendshëm).
- **Tests:** ripërdor `tests/test_postgres.py` (workers paralelë, asnjë dyfishim, idempotencë); teste kontrate që çdo implementim (PG + një `InMemoryQueue` për teste) i kalon; bench pa regres.
- **Rollback:** çdo outbox në commit të veçantë → revert individual.
- **Acceptance:** asnjë import i `SKIP LOCKED` jashtë `app/queue/`; testet paralele të gjelbra; bench brenda ±10%.

### M2 — CLOSED / APPROVED (refaktorim sjellje-ruajtës; referenca: `docs/QUEUE_ARCHITECTURE.md`)
- **Gate i mbetur (jo bllokues për M3):** S1/E1 (SENDING i ngecur: raportim, veprim admin, rikonsilim hold, audit, vendim lease) para M9 / wallet-credit real në prodhim / go-live sign-off. Devijimi SKIP LOCKED (3 përjashtime) i pranuar, i fiksuar me allowlist test.
- Zbatuar si **dy** kontrata sinkrone mbi rreshtat ekzistues (jo një `MessageQueue` me tabelë të veçantë): `DispatchQueue` (SMS, email; status
  SENDING, at-most-once për crash) dhe `DeliveryQueue` (webhook; lease, at-least-once), me adapterë `PostgresDispatchQueue`/`PostgresDeliveryQueue`
  te `app/queue/` dhe hooks të domain-it te services. Emrat e planit fillestar (`PostgresOutboxQueue`, `InMemoryQueue`, `dead_letter`) **nuk u zbatuan**
  qëllimisht (s'ka tabelë dead-letter; s'ka backend të dytë).
- Devijime nga plani: campaigns, sweeps (`expire_stale`, `expire_pending`) dhe DLR mbeten jashtë abstraksionit (kriteri "asnjë SKIP LOCKED jashtë
  `app/queue/`" plotësohet me 3 përjashtime të dokumentuara dhe të fiksuara nga test); patch i veçantë i miratuar për transaksionin e email.
- Borxhi i besueshmërisë (SENDING i ngecur, pa lease, SMTP/HTTP real i paprovuar, dublikim webhook) është te regjistri i `QUEUE_ARCHITECTURE.md` §6.

## M3 · Kernel + kontrata (kufij kodi, jo shërbime)
### M3 — CLOSED / APPROVED (referenca: `docs/M3_AUDIT.md` §M3-e, `docs/CONTRACTS_V1.md`)
- **Çfarë u bë (sjellje-ruajtës):** `core.timeutil`, `core.errors` (kernel-like); `app/contracts/` stdlib-only (`EventEnvelopeV1`, `PUBLIC_EVENT_TYPES_V1`, `sign_v1`/`verify_v1`) me 33 golden fixture bytes; `models.enterprise_registry` (identitet persistence); `core` pa varësi te `services`/`api`; audit me një rrugë shkrimi; guard AST (cikle, shtresa, queue, contracts, pronësi).
- **Devijime nga plani origjinal (të miratuara):** nuk krijohet `app/kernel/` as paketa fizike (primitivat qëndrojnë në `core`); kontratat janë `dataclass` + serializer eksplicit, jo Pydantic (bytes të ngrira); `import-linter` zëvendësohet nga testet AST në CI.
- **Paketim fizik:** shtyhet te M4 (konsumatori i dytë real).
- **Gate-et e mbetura (jo bllokuese për M4):** S1/E1 para M9/go-live; borxhet në `docs/M3_AUDIT.md` (teknik) dhe `docs/CONTRACTS_V1.md` §7 (kontratë).

### Kriteret e hyrjes në M4
1. Contracts V1 stabile (golden 33/33; ndryshim vetëm me miratim kontrate).
2. Identiteti kanonik i tenant-it në Enterprise = `enterprise_id` (`owner_ref` vetëm kompat).
3. Pa shkelje të varësive shtresore (guard-et në CI të gjelbra).
4. Central NUK importon ORM-in e Enterprise.
5. Central ka DB dhe migrime të veta (versioning table e vet).
6. Komunikimi Central↔Enterprise vetëm përmes kontratave/API të versionuara.
7. Pa DB të përbashkët.

## M4 · Central bazë (aplikacion i ri, DB e vet)
### M4-a — skelet i pavarur (referenca: `docs/M4_ARCHITECTURE.md`)
Vendime të miratuara: `apps/central/` në të njëjtin repo, Enterprise mbetet te `app/`; `app/contracts` mbetet (pa kopje, nxjerrja në `packages/contracts` vetëm me konsumatorin e parë real); version table `central_alembic_version`. M4-a = vetëm health/readiness + DB + migrime bosh, pa tabela biznesi.
### M4-b — regjistri i Enterprise-ve në Central (referenca: `docs/M4_ARCHITECTURE.md` §M4-b)
Tabela `enterprises` (UUID kanonik, `name`, `status` active/suspended, timestamps), migrimi `0002`, service konkret pa HTTP/auth/sync/fshirje. Central ≠ rekord lokal i Enterprise; nuk ka sync deri te M7.
### M4-c — bootstrap i Enterprise-ve ekzistues (referenca: `docs/M4_ARCHITECTURE.md` §M4-c)
Kopjon `sms_enterprises` → Central `enterprises` me UUID të ruajtura, manual dhe idempotent (`apps/central/tools/bootstrap_enterprises.py`, `--dry-run`); zero ndryshim skeme, `owner_ref` nuk ruhet në Central, pa sync runtime.
### M4-d — autentikimi/autorizimi bazë i Central (referenca: `docs/M4_ARCHITECTURE.md` §M4-d)
Staf i vetin (`users`, migrimi `0003`), Argon2id, JWT HS256 me sekret `CENTRAL_AUTH_SECRET`, RBAC `admin|operator`, CLI `create_admin`, endpoint-e vetëm auth/probë; pa Product/CRUD/sync/regjistrim.

- **Objective:** ekziston Central si aplikacion i veçantë administrativ me identitet, audit dhe API shërbimi.
- **Tables (DB Central):** `enterprises` (burimi i të vërtetës për ID/status), `staff_users`, `staff_roles`, `audit_log`, `service_credentials`.
- **Files/modules:** `apps/central/` (FastAPI, migrime të veta, vetëm `kernel`+`contracts`); regjistrimi i çdo Enterprise ekzistues në Central me **të njëjtin UUID** (skript backfill nga `sms_enterprises` te Enterprise).
- **Migration strategy:** Central nis **pasiv** (vetëm lexim/regjistër); Enterprise vazhdon të punojë si më parë. Sinkron fillestar një-drejtimësh i listës së enterprises.
- **Backwards compat:** Enterprise s'varet nga Central për dërgim (asnjë thirrje në rrugën e SMS).
- **Tests:** unit + API të Central; test kontrate me Enterprise; izolim DB (Central s'lidhet kurrë me DB të Enterprise; test që kontrollon konfigurimin).
- **Rollback:** fik shërbimin Central; Enterprise pa ndikim.
- **Acceptance:** Central nis, migron, ka audit dhe 2FA staf; regjistri i enterprises përputhet 1:1 me Enterprise.

## M5 · Katalogu i produkteve dhe `EnterpriseProduct` (Central)
### M5-b — assignment Enterprise↔Product (zbatuar; `docs/M5_CENTRAL_CATALOG.md` §8)
`enterprise_products` (UUID PK, FK RESTRICT, UNIQUE çift, `active|suspended`), API admin, audit, pa config/çmim/sync.
### M5-a — katalogu i produkteve + audit minimal (referenca: `docs/M5_CENTRAL_CATALOG.md`)
Zbatuar: `products` (`code` i pandryshueshëm, `channel` sms|email, `status` active|retired), API admin (admin shkruan, operator lexon), `audit_log` vetëm-shtim. Fazat e mbetura: M5-b assignment, M5-c config/mapim (vetëm nëse justifikohet), M5-d pricing. Pa sync me Enterprise.
- **Objective:** produktet menaxhohen te Central; caktohen te Enterprise pa kopjuar të dhëna.
- **Tables (Central):** `products(code, name, channel, description, active, visible, self_registration_enabled, auto_approval_enabled, requires_manual_approval, requires_payment, requires_sender_registration, requires_external_account, default_config JSONB)`, `product_country_rules(product_id, country, allowed, requires_approval)`, `enterprise_products(enterprise_id, product_id, status, activation_date, pricing_config/rate_card_id, external_account_mapping JSONB, limits JSONB, config JSONB; UNIQUE(enterprise_id,product_id))`, `rate_cards/versions/rates` (të lëvizura ose të lidhura), `provider_accounts` (kredencialet vetëm këtu/Gateway).
- **Files/modules:** `apps/central/catalog/*`; `GET /api/public/catalog/products` (vetëm `active ∧ visible ∧ self_registration_enabled`, pa fusha të brendshme, cache, rate limit).
- **Migration strategy:** për çdo `account_plans/rate_card` ekzistues krijo produkt "SMS Standard" dhe `enterprise_product` me çmimin aktual (backfill); asgjë nuk ndryshon në rrugën e dërgimit ende.
- **Backwards compat:** dërgimi vazhdon me `sms_account_plans` lokal deri në M7.
- **Tests:** rregullat e katalogut publik (fshehja e produkteve jo-active/jo-visible), unikaliteti `(enterprise,product)`, audit i çdo ndryshimi, validimi i konfigurimit JSON.
- **Rollback:** tabelat janë shtesë; heqja e tyre s'prek dërgimin.
- **Acceptance:** katalogu publik kthen vetëm produktet e duhura; çdo Enterprise ekzistues ka `enterprise_product` me çmim të barabartë me atë aktual.

## M6 · Përdorues dhe role në Enterprise
- **Objective:** login me email + fjalëkalim + 2FA, role/leje, ftesa; çelësat API mbeten për integrime.
- **Tables (Enterprise):** `users(enterprise_id, email, password_hash, status, totp_*)`, `roles`, `role_permissions`, `user_roles`, `invitations(token_hash, expires_at, used_at)`, `user_sessions`/refresh tokens.
- **Files/modules:** `app/api/auth.py` (i ri), `app/core/security.py` (`current_principal` pranon edhe sesion përdoruesi → `Principal` me `enterprise_id`, `user_id`), `app/services/twofactor.py` (ripërdoret), frontend `App.jsx` login.
- **Migration strategy:** shtesë; hyrja me çelës API mbetet (dual auth). Për çdo enterprise ekzistues krijohet ftesë për admin (opsionale).
- **Backwards compat:** çelësat dhe konsola e sotme punojnë pa ndryshim.
- **Tests:** login/2FA/ftesa/çaktivizim, matrica e lejeve për role të reja, izolim tenant-esh për sesionet, throttling i provave (ripërdor `AuthFailure`).
- **Rollback:** flag `USER_LOGIN=off`; tabelat mbeten të pa përdorura.
- **Acceptance:** përdorues me role kufizuar sipas modulit; audit për person; asnjë regres i çelësave API.

## M7 · Sinkronizimi Central → Enterprise
- **Objective:** Enterprise mban read models lokale të konfigurimit të Central; ndryshimet vijnë me evente të nënshkruara dhe të versionuara.
- **Tables:** Central: `sync_outbox(event_id UUID, enterprise_id, type, version BIGINT, payload JSONB, status, attempts, next_attempt_at)`, `enterprise_config_versions`. Enterprise: `sync_inbox(event_id UNIQUE, version, applied_at, result)`, `synced_config(version, snapshot JSONB)`, `synced_products`, `synced_prices`, `synced_sender_approvals`, `synced_limits`.
- **Files/modules:** `apps/central/sync/` (dispatcher me retry/circuit breaker), `app/sync/` në Enterprise (`POST /internal/sync/events`, puller `GET /api/sync/v1/config?since=`), `contracts/sync_v1`.
- **Migration strategy:** së pari **snapshot** për çdo enterprise (nga M5), pastaj delta; Enterprise **kalon** te read models vetëm kur `config_version` përputhet (feature flag `USE_SYNCED_CONFIG`); përpara kësaj, i vjetri (`account_plans` lokal) mbetet burim.
- **Backwards compat:** flag; kur fikur, sjellja e vjetër.
- **Tests:** idempotencë (i njëjti `event_id` dy herë), rend/boshllëqe (version i munguar → tërheq delta), nënshkrim i keq → 401, replay jashtë dritares, Central i padisponueshëm → Enterprise dërgon me config-un e fundit të mirë (politikë TTL), rikonsilim kur ka drift, test i kontratës.
- **Rollback:** fik `USE_SYNCED_CONFIG`; tabelat sync mbeten.
- **Acceptance:** një ndryshim çmimi në Central del te Enterprise brenda SLA-së (p.sh. < 60 s); drifti zbulohet dhe korrigjohet automatikisht; dërgimi nuk varet nga disponueshmëria e Central.

## M8 · Self Registration + Provisioning
- **Objective:** flow i plotë: katalog → regjistrim → verifikim email → auto/manual miratim sipas produktit → provisioning → aktivizim → login i parë.
- **Tables (Central):** `registration_requests(id, payload, product_id, status, email_verified_at, terms_version, terms_accepted_at, reviewed_by/at, reason, idempotency_key)`, `provisioning_runs(enterprise_id, step, status, attempts, error)`, `email_verifications`. Enterprise: `users`, `invitations` (M6).
- **Files/modules:** `apps/central/registration/*`, `apps/central/provisioning/*` (orkestrues me hapa idempotentë: krijo enterprise → `enterprise_products` → snapshot sync → kredi fillestare (nëse trial) → admin + ftesë), `registration-web` (SPA publike).
- **Migration strategy:** shtesë; endpoint-et publike nën rate limit + captcha; regjistrimi i sotëm manual (staf krijon llogari) mbetet paralel.
- **Backwards compat:** e plotë.
- **Tests:** vendimi i miratimit sipas flamujve të produktit (matricë 6 flamuj), idempotencë e regjistrimit, abuzim (rate limit, email i pa verifikuar), rifillim i provisioning pas dështimi në secilin hap, ftesa e njëpërdorshme.
- **Rollback:** fik endpoint-et publike; enterprise-et e krijuara mbeten të vlefshme.
- **Acceptance:** produkt trial → aktivizim automatik deri te login; produkt real SMS → pret miratim; asnjë gjendje gjysmë-provisionuar pa mundësi rikuperimi.

## M9 · Paratë: kredia nga Central, ledger operacional në Enterprise, rikonsilim
- **Objective:** Central zotëron pagesat/top-up/miratimet; Enterprise konsumon kredi lokale; rikonsilim periodik.
- **Tables:** Central: `credit_grants(id, enterprise_id, amount, currency, source_ref, idempotency_key UNIQUE, approved_by/at)`, `usage_snapshots(enterprise_id, period, consumed, opening, closing, received_at)`, `reconciliation_runs(status, drift)` (+ pagesat/top-up/faturat që bartet nga sot). Enterprise: `credit_ledger` (i pandryshueshëm, trigger si sot; hyrje `grant`/`consume`/`release`/`adjust`), `usage_outbox`.
- **Files/modules:** `app/services/wallet.py` (mbetet motori i ledger-it; shndërrohet në ledger operacional me `grant` nga event), `app/services/payments.py`/`billing.py` (lëvizin te Central në M11), `app/services/messages.py::submit` (hold lokal, pa thirrje rrjeti).
- **Migration strategy:** **balanca ekzistuese** kthehet në `credit.granted` "opening balance" të idempotent (një për enterprise); pastaj top-up-et e reja krijohen në Central dhe vijnë si event. Në fillim **modaliteti hije**: Central llogarit paralel dhe krahason, pa u bërë autoritet; kalim i autoritetit me flag pas 0 drift për N ditë.
- **Backwards compat:** wallet-i lokal ekzistues punon; API e portofolit e pandryshuar për klientin.
- **Tests:** idempotencë e `credit.granted` (dy herë = një), pa balancë negative, Central i ra → SMS vazhdon, rikonsilim gjen dhe raporton drift, shuma totale ruhet (SUM ledger), konkurrencë (ripërdor testet PG), audit i çdo miratimi.
- **Rollback:** flag kthen autoritetin te wallet-i lokal; ledger-i i pandryshueshëm nuk humbet të dhëna.
- **Acceptance:** drift = 0 për N ditë; asnjë SMS i dështuar për shkak të Central; top-up nga Central shfaqet te Enterprise brenda SLA-së.

## M10 · Sender ID ↔ shtet nga Central
- **Objective:** kërkesa e sender-it kalon te Central për miratim; Enterprise zbaton nga read model.
- **Tables:** Central: `sender_id_registry(enterprise_id, sender, country, approval_status, requires_approval, approved_by, approval_date, rejection_reason)`. Enterprise: `synced_sender_approvals`; `sms_sender_ids` lokal mbahet si kërkesë/gjendje.
- **Files/modules:** `app/services/sender_ids.py`, `app/services/messages.py` (validim kundër read model), `apps/central/approvals/*`.
- **Migration strategy:** kopjo miratimet ekzistuese te Central si `approved` (backfill); valido në Enterprise nga read model me fallback te tabela lokale gjatë kalimit.
- **Backwards compat:** sender-at e miratuar sot mbeten të vlefshëm.
- **Tests:** dërgim i bllokuar për shtet jo të miratuar, `requires_approval=false` kalon, revokimi nga Central hyn në fuqi pas sync, unikaliteti (`approved_key`) ruhet.
- **Rollback:** flag kthen validimin lokal.
- **Acceptance:** çdo SMS validohet për sender+shtet nga miratimi i sinkronizuar.

## M11 · Funksionet tregtare kalojnë te Central; frontend-et ndahen
- **Objective:** Central = backoffice (çmime, miratime, pagesa/top-up, provider accounts, statistika të agreguara); Enterprise = vetëm operativ.
- **Tables:** lëvizje/heqje: `rate_cards*`, `topups`, `invoices/payments/plans/subscriptions` (te Central), `account_plans`, `routes` (konfigurim → Central → sync te modulit Gateway).
- **Files/modules:** `app/api/admin.py`, `console.py` (pjesa staf), `rates.py`, `billing.py` (admin), `frontend/src/pages/{Accounts,Approvals,Finance,Rates,Admin,Providers,Security}` → `central-console`; `enterprise-console` mban vetëm faqet operative.
- **Migration strategy:** një kapacitet në herë, me proxy të përkohshëm (endpoint-i i vjetër i staf-it thërret Central); pasi Central ka të dhënat autoritare, endpoint-i i vjetër hiqet.
- **Backwards compat:** endpoint-et klient `/v1/*` të pandryshuara.
- **Tests:** çdo kapacitet i lëvizur ka testet e veta të bartura; e2e për të dy konsolat; matrica e autorizimit e ndarë sipas app-it.
- **Rollback:** proxy-ja kthen te implementimi i vjetër deri në heqjen e tij.
- **Acceptance:** stafi punon vetëm nga Central; Enterprise nuk ka UI/endpoint administrative.

## M12 · (Opsionale) Nxjerrja fizike e Gateway
- **Kushte (vetëm një mjafton):** shkallë (workers duhen të shkallëzohen veçmas), izolim kredencialesh provider-i, deploy i pavarur, incident që e justifikon.
- **Objective/plan:** workers + `providers` + rrugëzim në shërbim të veçantë; Enterprise poston në `POST /gateway/v1/messages` (idempotent, batch); callback DLR/inbound kthehen te Enterprise me webhook. Kontrata `MessageQueue` e M2 e bën ndarjen të lirë.
- **Rollback:** kthim në proces të njëjtë (kodi mbetet i njëjtë, ndryshon vetëm deployment-i).
- **Acceptance:** SLA e dërgimit e pandryshuar; kredencialet vetëm te Gateway.

## M13 · Pastrimi
- **Objective:** hiq `owner_ref`, alias-et e përkohshme, flamujt e migrimit, kodin e vjetër të admin-it në Enterprise.
- **Backwards compat:** njoftim i vjetrimit për API `owner_ref`; heqja vetëm me version të ri API (`/v2`) ose pas dritares së dakorduar.
- **Acceptance:** asnjë referencë `owner_ref` përveç `enterprises.external_id`.

---

## Rreziqet kryesore
1. **Rrjedhje mes tenant-ëve gjatë M1** (~500 referenca, 21 tabela): një filtër i harruar = klient sheh të dhënat e tjetrit. *Zbutje:* scoping automatik me loader criteria, dual-write me test shadow, teste negative, matrica e izolimit ekzistuese, flag kthimi.
2. **Migrimi i tabelave të mëdha (`sms_messages`, `sms_ledger_entries`)**: backfill i gjatë/kyçje. *Zbutje:* kolona nullable, backfill me batch, `NOT VALID`→`VALIDATE`, dritare mirëmbajtjeje vetëm për ndryshimet që s'shmangen.
3. **Drift i parave mes Central dhe Enterprise (M9)**: humbje/ripërsëritje eventesh, mospërputhje balance. *Zbutje:* ledger i pandryshueshëm, `idempotency_key`, modaliteti hije, rikonsilim me alarm, politikë e qartë për balancë negative kur Central s'arrihet (kufi kredie).
4. **Konfigurim i vjetruar kur Central është jashtë (M7):** çmime/miratime të vjetra. *Zbutje:* versionim, TTL i konfigurimit të fundit të mirë, çelësi i emergjencës i shtyrë me prioritet dhe i tërhequr periodikisht.
5. **Dyfishim i përkohshëm i funksioneve (M4–M11):** staf punon në dy vende. *Zbutje:* tabelë pronësie për çdo kapacitet, proxy, flamuj, hiqet një kapacitet në herë.
6. **Mospërputhje sinkron/asinkron:** kontrata juaj `MessageQueue` shembull është `async`, por shërbimet janë sinkrone. *Zbutje:* kontratë sinkrone fillimisht; async shtohet kur një backend real e kërkon.
7. **Mbi-inxhinierim:** mikroshërbime fizike parakohe. *Zbutje:* kufij në kod (M3 + import-linter), Gateway logjik deri te M12.
8. **Sipërfaqja publike e regjistrimit:** spam/mashtrim. *Zbutje:* captcha, rate limit, verifikim email, miratim manual për produkte me rrezik, audit.
9. **Provisioning gjysmë i përfunduar:** enterprise pa admin/kredi. *Zbutje:* hapa idempotentë, `provisioning_runs`, rifillim manual, monitorim.
10. **API e vjetër me `owner_ref`:** integrime ekzistuese prishen. *Zbutje:* alias i pranuar deri në M13, njoftim vjetrimi, testet e kontratës OpenAPI.

## Ndryshimi i parë minimal i rekomanduar
**M1a:** migrimi `0018` që krijon `sms_enterprises` dhe e mbush nga `owner_ref`-et ekzistuese, plus një shërbim i vogël `enterprises.for_owner_ref()` dhe testet. **Asnjë tabelë tjetër, asnjë endpoint, asnjë sjellje nuk ndryshon.** Rikthimi = `downgrade` (heq një tabelë). Vlera: fikson identitetin (UUID) që kërkohet nga M4–M10 pa asnjë rrezik për dërgimin. Vetëm pas kësaj vjen M1b (kolonat `enterprise_id` nullable + dual-write).
