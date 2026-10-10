# Auditimi i arkitekturës: Central (Control Plane) + Enterprise (Operational Plane)

Gjendja e analizuar: dega `claude/sms-platform-architecture-lugdyz` (faza 0–22). **Asnjë kod nuk u ndryshua për këtë auditim.**

## Përmbledhje
Sot kemi **një monolit multi-tenant me një DB**: gjithçka operative (kontakte, fushata, SMS, inbox, shabllone, raporte) dhe gjithçka administrative (çmime, miratime, pagesa, provider-a, statistika) jeton në të njëjtin aplikacion dhe të njëjtën bazë; klienti dallohet vetëm nga një varg `owner_ref`. Pjesa më e madhe e **planit operativ (Enterprise)** dhe e **planit të përpunimit/integrimit (Queue/Workers/Providers)** ekziston dhe është e fortë. Mungon **i gjithë Control Plane si sistem i veçantë**: Enterprise si entitet, Product/Catalog/EnterpriseProduct, Self Registration, provisioning, sinkronizimi Central↔Enterprise, dhe **përdoruesit/rolet brenda një Enterprise** (sot ka vetëm çelësa API).

Rekomandimi: **mos rishkruaj**. Kodi ekzistues bëhet themeli i **Enterprise + Gateway**, dhe **Central ndërtohet i ri** përreth tij, me kontrata të qarta (REST + eventet e nënshkruara + idempotencë) dhe **pa DB të përbashkët**.

---

## Vendimet e fiksuara (të miratuara nga pronari i projektit)
| # | Vendimi | Pasoja |
|---|---|---|
| 1 | **Central = autoriteti tregtar** (pagesa, top-up, miratim pagese, kredi komerciale, konfigurim çmimesh/produktesh). **Enterprise = ledger/balancë operacionale lokale** për dërgimin. **Pa hold HTTP te Central për çdo SMS.** Një SMS nuk dështon sepse Central është përkohësisht i padisponueshëm. | Rrjedha: `Payment/Top-up → Central → approval → credit.granted → Enterprise Ledger → konsum SMS`; periodikisht `Enterprise → usage/balance snapshot → Central → reconciliation`. Ledger i pandryshueshëm, çelësa idempotence, kufij transaksioni, audit. |
| 2 | **Enterprise hostohet te ne**, multi-tenant; Central është platformë administrative e veçantë. Klienti përdor frontend/API të Enterprise. | Kredencialet e provider-ave nuk ekspozohen te klientët; s'ka deployment për klient tani. |
| 3 | **Një DB multi-tenant për Enterprise**, me **`enterprise_id` UUID real** (FK te `enterprises`) në vend të `owner_ref` të lirë. Izolimi zbatohet në backend/domain, jo në frontend; harrimi i `enterprise_id` në një query duhet të jetë i vështirë/i pamundur. | Arkitektura nuk lidhet aq fort sa të pamundësojë më vonë një DB/deployment të dedikuar për një tenant. |
| 4 | **Queue: mbetet outbox PostgreSQL (`SKIP LOCKED`)**, por pas një kontrate `MessageQueue` (`publish/reserve/acknowledge/retry`). Domain/service layer nuk di nëse pas saj është PostgreSQL, Redis, RabbitMQ, SQS ose Kafka. | Pa migrim brokeri tani. |
| 5 | **Python + FastAPI + React + PostgreSQL.** Kopjojmë kufijtë e domain-it, ndarjen Central/Enterprise, caktimin e produkteve, rrjedhat e miratimit, konceptet e sinkronizimit; jo framework-un. | Ripërdoret ~80% e kodit. |
| 6 | **Regjistrimi drejtohet nga produkti**: flamuj `self_registration_enabled`, `auto_approval_enabled`, `requires_manual_approval`, `requires_payment`, `requires_sender_registration`, `requires_external_account`. Auto-miratim për demo/sandbox/trial pa rrezik tregtar; miratim manual për produkte SMS reale me pagesë, top-up, kontratë, SID, llogari të jashtme ose kur produkti e deklaron. | Rrjedha e regjistrimit shih F. |
| + | **Gateway nuk bëhet mikroshërbim fizik tani**: mbetet **modul/proces i kufizuar brenda deployment-it ekzistues**; ndarja është logjike/kod-nivel, dhe nxjerrja fizike bëhet vetëm me arsye reale (shkallë, izolim sigurie/kredencialesh, deploy të pavarur). | Central është aplikacion i ri me DB të vet; Enterprise dhe Gateway mbeten në kodin ekzistues. |
| + | **Migrim pa big-bang**, çdo hap prapavajtës-kompatibil, me migrim, teste, pa prishur dërgimin ekzistues, dhe pa prekur më shumë module sesa duhet. Ndryshimi strukturor i parë: **`owner_ref` → entitet `Enterprise` + `enterprise_id`.** | Shih `docs/MIGRATION_PLAN.md`. |

Ndryshimet ndaj tekstit më poshtë: kudo ku shkruan "Gateway" lexo "modul logjik i kufizuar (proces workers) me kredencialet e provider-ave"; kudo ku shkruan "DB e ndarë për Enterprise" lexo "DB multi-tenant me `enterprise_id`"; struktura G është **synim përfundimtar**, jo hapi i parë.

---

## A. Çfarë ekziston tashmë (dhe përputhet me arkitekturën)
| Plani | Ekziston |
|---|---|
| **Enterprise / operativ** | Kontakte, lista, pëlqim me provë (GDPR), fushata SMS+email (audiencë e ngrirë, buxhet, kufi shpejtësie, dritare orare, pauzë/rifillim), SMS me çmim live, të planifikuara (fushata), Kutia hyrëse + fjalë kyçe, historik, shabllone me versione, sender ID, raporte + CSV, dashboard, webhooks/events, çelësa API (IP allowlist, rrotullim), portofol me ledger të pandryshueshëm, faturim/plane/TVSH, konsolë React në shqip/anglisht |
| **Processing** | Outbox në PostgreSQL (`SKIP LOCKED`), workers të ndarë (SMS/email/fushata/faturim; webhooks veçmas), retry me backoff, idempotencë (`Idempotency-Key`), kill switches, heartbeat, healthchecks |
| **Integration** | Interface `SmsProvider` + adaptera (fake, HTTP gjenerik, **Twilio**), rrugëzim sipas prefiksit (`sms_routes`), callback DLR/inbound të nënshkruar dhe idempotentë (`sms_dlr_receipts`), pa dërgim direkt nga kërkesa HTTP |
| **Control (i pjesshëm, brenda të njëjtit app)** | Konsola e stafit: llogari, miratim sender ID/shabllone, listat e çmimeve me versione + rrugë, top-up (dy-sy) dhe korrigjime të audituara, ndalim dërgimi, shëndeti i provider-ave, audit log, 2FA, RBAC (superadmin/finance/pricing/approver/support) |
| **Sender ID ↔ shtet** | `sms_sender_ids(owner_ref, country, value)` me `status`, `reviewed_by/at`, `reason`, unikalitet i miratuar; validimi para dërgimit (`sender_not_allowed`) |
| **Sigurinë/operim** | Matrica e autorizimit e testuar, izolim tenant-ësh i testuar, OpenAPI, CI, backup/restore, runbook |

## B. Çfarë mungon
1. **`Enterprise` si entitet** (sot vetëm `owner_ref` varg pa tabelë; s'ka statuse, kontakt kompanie, vendndodhje, konfigurim).
2. **`Product`, `Catalog`, `EnterpriseProduct`** (nuk ekziston asnjë koncept produkti; çmimi lidhet drejtpërdrejt me llogarinë).
3. **Self Registration**: kërkesë regjistrimi, informacion kompanie/admin, zgjedhje produkti, kushte, rishikim nga Central, aktivizim.
4. **Provisioning** i një Enterprise (krijim instance, DB, admin i parë, çelësa shërbimi, sync fillestar).
5. **Sinkronizimi Central↔Enterprise**: eventet e versionuara, outbox/inbox, rikonsilim, raportim i agreguar lart.
6. **Përdorues dhe role brenda Enterprise** (login me email+fjalëkalim/2FA, ftesa, roli/leje sipas modulit). Sot: vetëm çelësa API; konsola hyn me çelës.
7. **Kufizime shtetesh** në nivel produkti/enterprise-i (jo vetëm sender ID) dhe **`requires_approval`** si politikë (sot çdo sender kërkon miratim).
8. **Mapping llogarish të jashtme** (provider account/WMS) për Enterprise/Product.
9. **Statistika të agreguara** që rrjedhin nga Enterprise drejt Central (sot Central i llogarit direkt nga tabela të përbashkëta).
10. **Autentikim shërbim-me-shërbim** mes planeve (kredenciale të ndara, nënshkrim, rrotullim, rate limit).

## C. Çfarë është e gabuar ose shumë e çiftuar (për arkitekturën e re)
1. **Një DB dhe një kod për dy plane.** Staff dhe klient jetojnë në të njëjtin FastAPI/React/DB; ndarja është vetëm me `role`/`owner_ref`.
2. **`messages.submit()` bën gjithçka në një transaksion**: çmim (rate card), wallet, sender-shtet, route/provider. Në modelin e ri Enterprise duhet të validojë kundër **kopjeve lokale të sinkronizuara** (read models), jo kundër tabelave të Central.
3. **Çmimet, plani, wallet-i, faturimi dhe miratimet janë në të njëjtën DB me trafikun.** Në modelin e ri vetëm konfigurimi tregtar është Central; Enterprise mban pamjen e tij të sinkronizuar.
4. **Kredencialet e provider-ave** (Twilio token) jetojnë në mjedisin e të njëjtit proces që shërben klientin. Në një satellite të hostuar te klienti **kjo është e papranueshme**: kredencialet duhet të qëndrojnë vetëm te plani Processing/Integration.
5. **Identiteti = çelës API.** Nuk mund të kesh disa përdorues me role, ftesa, çaktivizim personi, audit për person.
6. **Pagesa/top-up dhe balanca janë të lidhura me DB lokale**: kur balanca duhet të kontrollohet nga Central por të vlerësohet shpejt në Enterprise, kjo kërkon model të ri (shih vendimin 1).
7. **`owner_ref`** si string i lirë kalon nëpër çdo tabelë; në dy sisteme duhet ID e qëndrueshme (`enterprise_id`, UUID) e pronësuar nga Central.

## D. Çfarë duhet refaktoruar (në këtë rend)
1. **Shkëputja e "shared kernel"** (paketë e brendshme): siguria/çelësat, ledger-i i parave, outbox/worker runtime, kripto, audit, envelope e eventeve. Përdoret nga të dy.
2. **Ndarja e `submit()`** në: *Enterprise-side* (validim lokal, kredi lokale, hold lokal, futje në outbox) dhe *Gateway-side* (rrugëzim, provider, DLR).
3. **Read models të sinkronizuara** në Enterprise: `synced_products`, `synced_prices`, `synced_sender_country_approvals`, `synced_limits` (me `config_version`), në vend që kodi të pyesë tabelat e Central.
4. **Ledger-i i kredisë** (`credit_grants` + shpenzim lokal) në vend të wallet-it që kyçet nga Central.
5. **Ndarja e frontend-it**: `central-console` (backoffice) dhe `enterprise-console` (operativ); sot është një SPA me menu të kushtëzuara.
6. **Identiteti**: shtresë `users/roles` në Enterprise (API keys mbeten për integrime).

---

## E. Modelet, tabelat, endpoint-et, job-et, eventet

### Central (Control Plane), DB e vet
**Tabela:** `enterprises` (id UUID, legal_name, tax_id, country, status: `requested|approved|provisioning|active|suspended|closed`, created_at) · `enterprise_contacts` · `products` (code, name, channel, description, active, visible, self_registration_enabled, default_config JSON) · `product_country_rules` (product_id, country, allowed, requires_approval) · `enterprise_products` (enterprise_id, product_id, status, activation_date, pricing_config JSON *ose* rate_card_id, external_account_mapping JSON, limits JSON, config JSON; unik `(enterprise_id, product_id)`) · `rate_cards/versions/rates` (ekzistuese, bartohen) · `registration_requests` (payload, status, reviewed_by/at, reason) · `sender_id_registry` (enterprise_id, sender, country, approval_status, requires_approval, approved_by, approval_date, rejection_reason) · `provider_accounts` (kredencialet vetëm këtu/Gateway) · `credit_grants` (enterprise_id, amount, currency, ref, idempotency_key) + top-up/ledger financiar (ekzistues) · `enterprise_stats_daily` (agregat i pranuar nga Enterprise) · `sync_outbox` (event_id, enterprise_id, type, version, payload, status, attempts) · `service_credentials` (për çdo Enterprise) · `audit_log`, `staff_users/roles` (ekzistuese, plus përdorues me fjalëkalim).
**Endpoint-e:** `GET /api/public/catalog/products` (vetëm `active ∧ visible ∧ self_registration_enabled`, pa çmime të brendshme, cache) · `POST /api/public/registrations` (rate-limited, captcha, idempotent) · `GET/POST /api/admin/enterprises|products|enterprise-products|registrations/{id}/approve|reject` · `POST /api/admin/enterprises/{id}/provision` · `PUT /api/admin/enterprises/{id}/products/{p}` · `POST /api/admin/enterprises/{id}/topups` (dy-sy) · `POST /api/admin/sender-ids/{id}/approve|reject` · **API shërbimi që thërret Enterprise:** `GET /api/sync/v1/config?since=<version>` (snapshot/delta), `POST /api/sync/v1/sender-requests`, `POST /api/sync/v1/usage-reports` (idempotent), `POST /api/sync/v1/credit-requests`.
**Job-e:** sync dispatcher (outbox→Enterprise, retry, circuit breaker), provisioning orchestrator (steps idempotente, të rifillueshme), reconciler (krahason `config_version` dhe kreditë), aggregator i statistikave, expiry i regjistrimeve të pamarra.
**Evente (Central→Enterprise, të nënshkruara, të versionuara):** `enterprise.provisioned`, `product.assigned|updated|deactivated`, `pricing.changed`, `sender_id.approved|rejected|revoked`, `credit.granted`, `limits.changed`, `enterprise.suspended|resumed`.

### Enterprise (Operational Plane), DB e vet (për çdo Enterprise ose multi-tenant i brendshëm)
**Tabela:** përdorimi i gjithçkaje ekzistuese operative (`contacts`, `contact_lists`, `campaigns`, `campaign_recipients`, `messages`, `message_events`, `templates`, `inbound_messages`, `keywords`, `consent_*`, `webhook_*`, `events`) **plus**: `users` (email, password_hash, status, 2FA) · `roles`/`user_roles`/`permissions` · `invitations` · `api_credentials` (çelësat ekzistues) · **read models nga Central:** `synced_config` (version, snapshot), `synced_products`, `synced_prices`, `synced_sender_approvals(sender, country, status)`, `synced_limits` · `credit_ledger` (grants + consumption, i pandryshueshëm; **balanca lokale = SUM**) · `sync_inbox` (event_id unik, version, result) · `sync_outbox` (raportet lart: përdorim, kërkesa sender/kredie) · `gateway_outbox` (mesazhet drejt Gateway).
**Endpoint-e:** ekzistuese operative + `POST /auth/login|refresh|2fa`, `/users`, `/roles`, `/invitations` · `POST /internal/sync/events` (pranon eventet e Central: verifikon nënshkrimin, `event_id` unik, `version` monoton, hap boshllëqet duke thirrur `GET /config?since`).
**Job-e:** campaign processor (ekzistues) → gjeneron SMS jobs; gateway dispatcher (outbox→Gateway me batch + idempotencë); sync puller (rikonsilim periodik); usage reporter (agregim orar/ditor drejt Central); credit low-balance alert.
**Evente (Enterprise→Central):** `usage.reported`, `sender_id.requested`, `credit.requested`, `campaign.completed` (opsionale, vetëm agregat).

### Gateway (Processing + Integration Plane), i vetmi që ka kredencialet e provider-ave
Kod ekzistues (workers, `SmsProvider`, `sms_routes`, DLR/inbound). **Endpoint:** `POST /gateway/v1/messages` (idempotent, batch, i kufizuar me kredenciale shërbimi të Enterprise) · callback-et e provider-ave (Twilio etj.) · webhook DLR/inbound drejt Enterprise (me retry). **Rrugëzimi** dhe zgjedhja e provider-it merren nga konfigurimi i Central (`provider_accounts`, `routes`).

---

## F. Rrjedha e plotë: Self Registration → Product → Provisioning → Login i parë
1. **Public:** `GET /api/public/catalog/products` → SPA e regjistrimit liston produktet (asgjë e hardcode-uar).
2. **Formulari (hapa):** Kompania → Admin → Produkt/shërbim → Shteti/informacion operativ → Kushte (ruhet versioni i kushteve dhe koha) → dërgim.
3. `POST /api/public/registrations` (idempotent, captcha, rate limit, verifikim email i adminit **para** rishikimit) → `registration_requests(status=pending_review)` + audit. Produkti kërkon `product_country_rules`.
4. **Vendimi i miratimit sipas produktit:** `auto_approval_enabled` dhe asnjë nga `requires_manual_approval`, `requires_payment`, `requires_sender_registration`, `requires_external_account` (demo/sandbox/trial) → auto-miratim; përndryshe **rishikim në Central**: stafi miraton/refuzon me arsye (audit, 2FA për veprime të ndjeshme) → `enterprises(status=approved)`.
5. **Provisioning (orkestrues me hapa idempotentë, i rifillueshëm):** krijon `enterprise_id`; krijon/instancon Enterprise (DB, migrime, konfigurim); krijon kredenciale shërbimi; **krijon `enterprise_products`** (çmim/limite/mapping); dërgon **snapshot** të plotë konfigurimi (`config_version=1`); dërgon `credit.granted` fillestar nëse ka trial; krijon **përdoruesin admin** (jo aktiv) me lidhje ftese të njëpërdorshme.
6. **Aktivizim:** admini hap lidhjen → vendos fjalëkalim → aktivizon 2FA → `user.status=active`; Enterprise raporton `enterprise.ready` te Central → `status=active`.
7. **Login i parë në Enterprise:** sheh checklistën (sender ID i kërkuar, kontakte, mesazhi i parë); kërkesa e sender ID kalon te Central për miratim; miratimi kthehet si event `sender_id.approved` dhe hap dërgimin drejt atij shteti.
Çdo hap shkruan audit; çdo tranzicion është idempotent, dhe dështimi lë gjendje të rikuperueshme (`provisioning_failed` me retry manual).

## G. Struktura synim (monorepo). Central ka DB të vet; Enterprise dhe Gateway ndajnë DB multi-tenant; Gateway fillimisht modul logjik
```
sms-platform/
├── packages/
│   ├── kernel/            # siguria, kripto, ledger parash, outbox/worker runtime, audit, envelope evente, HMAC
│   └── contracts/         # skemat e eventeve/API Central↔Enterprise↔Gateway (versionuara, me testet e kontratës)
├── apps/
│   ├── central/           # backend (catalog, enterprises, registrations, provisioning, sync, finance, approvals, stats)
│   │   └── frontend/      # central-console (backoffice)
│   ├── enterprise/        # backend (contacts, campaigns, sms, inbox, users/roles, credit ledger, sync client)
│   │   └── frontend/      # enterprise-console (operativ)
│   ├── gateway/           # workers, routing, provider adapters, DLR/inbound
│   └── registration-web/  # self-registration publik (ose pjesë e central-console)
├── deploy/                # compose/helm për secilin, nginx, CI
└── docs/
```
Rregulla të kufirit: asnjë app nuk importon modele të tjetrit; vetëm `kernel` + `contracts`. Asnjë akses direkt në DB të tjetrit. Çdo ndërveprim kalon me API/event të nënshkruar.

---

## Vendimet e mbetura
Të gjashtë pyetjet e hapura u mbyllën (tabela "Vendimet e fiksuara" më sipër). Plani konkret, i ndarë në faza me kritere pranimi: **`docs/MIGRATION_PLAN.md`** (zëvendëson listën orientuese më poshtë).

## Plani orientues fillestar (i zëvendësuar nga MIGRATION_PLAN.md)
1. **Kontratat + kernel:** nxjerr `kernel`, përcakto `contracts` (envelope, nënshkrim, idempotencë, versionim) me testet e kontratës.
2. **Central bazë:** `enterprises`, `products`, `product_country_rules`, `enterprise_products`, katalogu publik, audit; migrime + DTO + teste.
3. **Registration:** kërkesa, verifikim email, rishikim/miratim, auto-rregulla.
4. **Sync:** `sync_outbox` në Central, `sync_inbox`/read models në Enterprise, snapshot + delta + rikonsilim, testet e idempotencës/rendit.
5. **Provisioning orkestrues** dhe admin i parë me ftesë.
6. **Enterprise users/roles/login/2FA** (ndërfaqja e re e hyrjes).
7. **Kredia:** `credit_grants` → ledger lokal, validim balance, raportim përdorimi, rikonsilim.
8. **Sender-country nëpërmjet Central** dhe zbatimi te dërgimi; `requires_approval`.
9. **Gateway split:** kontrata `POST /gateway/v1/messages`, ndarja e kredencialeve, DLR/inbound drejt Enterprise.
10. **Frontend:** central-console, enterprise-console, self-registration flow.
11. **Migrimi** i llogarive ekzistuese të kësaj DB në modelin e ri.
