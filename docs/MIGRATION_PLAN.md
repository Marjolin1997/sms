# Plani i migrimit: nga monoliti multi-tenant te Central + Enterprise (+ Gateway logjik)

Bazuar në `docs/ARCHITECTURE_AUDIT.md` (vendimet e fiksuara). **Ky dokument nuk përmban kod implementimi.** Asnjë fazë nuk fillon pa miratimin tënd.

## Parime (vlejnë për çdo fazë)
1. **Expand → migrate → contract**: shto strukturën e re pa hequr të vjetrën, dyfisho shkrimin, kalo leximet, vetëm në fund hiq të vjetrën.
2. **Prapavajtje-kompatibilitet**: API-ja publike ekzistuese (përfshirë `owner_ref` në trupa/parametra dhe `/v1/*`) nuk prishet; shtohet e re, e vjetra shënohet e vjetruar.
3. Çdo fazë ka **migrim Alembic** (up/down të provuar në PostgreSQL dhe SQLite), **teste**, **rikthim**, **kritere pranimi**. Suita ekzistuese (379 backend, 47 frontend, e2e 74) duhet të mbetet e gjelbër në çdo hap.
4. **Dërgimi ekzistues i SMS-ve nuk duhet të ndalet**: çdo fazë që prek `messages.submit`, workers ose provider-at kërkon të kaluara `tests/test_postgres.py` (konkurrenca) dhe krahasim me `scripts/bench.py` (pa regres > 10%).
5. **Një fazë prek sa më pak module**; feature flags për sjellje të reja.
6. Izolimi i tenant-ëve provohet automatikisht (`tests/test_authz_matrix.py`, `tests/test_tenant_isolation.py`) dhe zgjerohet në çdo fazë.

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
- **Rollback:** M1a/M1b: `downgrade` heq kolonat (të dhënat origjinale `owner_ref` janë të paprekura); M1c: flag `TENANT_SCOPING=owner_ref|enterprise` kthen leximet te `owner_ref`.
- **Acceptance:** 100% e rreshtave kanë `enterprise_id`; asnjë query tenant pa filtër (test); suite + bench të gjelbër; API e vjetër punon; audit tregon `unscoped()` vetëm te vendet e lejuara.

## M2 · Kontrata `MessageQueue` (e pavarur nga M1)
- **Objective:** domain-i nuk di për PostgreSQL; brokeri mund të ndërrohet më vonë.
- **Tables:** asnjë e re (outbox ekzistues mbetet).
- **Files/modules:** `app/queue/` (Protocol + `PostgresOutboxQueue`), `app/services/messages.py`, `emails.py`, `webhooks.py`, `campaigns.py`, `app/worker.py`.
- **Migration strategy:** kontratë **sinkrone** (`publish / reserve / acknowledge / retry`, plus `dead_letter`), sepse shërbimet janë sinkrone; variant async shtohet vetëm kur ka backend që e kërkon. Nxirret logjika `SKIP LOCKED`/backoff/lease nga shërbimet te implementimi PostgreSQL, **një outbox në herë** (SMS → email → webhooks → campaign recipients), pa ndryshuar skemën.
- **Backwards compat:** e plotë (refaktorim i brendshëm).
- **Tests:** ripërdor `tests/test_postgres.py` (workers paralelë, asnjë dyfishim, idempotencë); teste kontrate që çdo implementim (PG + një `InMemoryQueue` për teste) i kalon; bench pa regres.
- **Rollback:** çdo outbox në commit të veçantë → revert individual.
- **Acceptance:** asnjë import i `SKIP LOCKED` jashtë `app/queue/`; testet paralele të gjelbra; bench brenda ±10%.

## M3 · Kernel + kontrata (kufij kodi, jo shërbime)
- **Objective:** kufij të qartë në kod para se të shtohet Central.
- **Tables:** asnjë.
- **Files/modules:** `app/kernel/` (security/HMAC, crypto, ledger money, audit, envelope evente, idempotencë, tenant scoping), `app/contracts/` (skemat Pydantic të versionuara të evenimenteve dhe API-ve Central↔Enterprise↔Gateway), rregulla `import-linter` në CI (Enterprise nuk importon Central; Gateway nuk importon API Enterprise; vetëm `kernel`/`contracts` janë të përbashkëta).
- **Migration strategy:** lëviz kod pa ndryshuar sjellje (`git mv` + re-export i përkohshëm për prapavajtje).
- **Backwards compat:** e plotë.
- **Tests:** teste kontrate (serializim/versionim/nënshkrim); CI import-linter; e gjithë suite.
- **Rollback:** revert i lëvizjeve.
- **Acceptance:** rregullat e kufijve kalojnë në CI; asnjë cikël importi.

## M4 · Central bazë (aplikacion i ri, DB e vet)
- **Objective:** ekziston Central si aplikacion i veçantë administrativ me identitet, audit dhe API shërbimi.
- **Tables (DB Central):** `enterprises` (burimi i të vërtetës për ID/status), `staff_users`, `staff_roles`, `audit_log`, `service_credentials`.
- **Files/modules:** `apps/central/` (FastAPI, migrime të veta, vetëm `kernel`+`contracts`); regjistrimi i çdo Enterprise ekzistues në Central me **të njëjtin UUID** (skript backfill nga `sms_enterprises` te Enterprise).
- **Migration strategy:** Central nis **pasiv** (vetëm lexim/regjistër); Enterprise vazhdon të punojë si më parë. Sinkron fillestar një-drejtimësh i listës së enterprises.
- **Backwards compat:** Enterprise s'varet nga Central për dërgim (asnjë thirrje në rrugën e SMS).
- **Tests:** unit + API të Central; test kontrate me Enterprise; izolim DB (Central s'lidhet kurrë me DB të Enterprise; test që kontrollon konfigurimin).
- **Rollback:** fik shërbimin Central; Enterprise pa ndikim.
- **Acceptance:** Central nis, migron, ka audit dhe 2FA staf; regjistri i enterprises përputhet 1:1 me Enterprise.

## M5 · Katalogu i produkteve dhe `EnterpriseProduct` (Central)
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
