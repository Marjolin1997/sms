# M5 — Katalogu i produkteve dhe assignment-i te Enterprise (Central)

Statusi: **M5-a (katalogu + audit minimal) dhe M5-b (assignment Enterprise↔Product) zbatuar.** M5-c/d të planifikuara, jo të nisura.
Pa sync me Enterprise; Product/EnterpriseProduct jetojnë vetëm në Central DB.

## 1. Audit i koncepteve produkt-ngjashme në Enterprise
| Koncept ekzistues | Pronari sot | Fusha kryesore | Konsumatorë | Kandidat Central? | Arsye |
|---|---|---|---|---|---|
| **Kanali** (SMS / email) | kodi (pa tabelë) | `Message` vs `Email`; pipeline-e të ndara | `services.messages`, `services.emails`, queue | **Po → `products.channel`** (`sms`,`email`) | i vetmi "lloj produkti" real; s'ka flashcall/WMS/WhatsApp në kod |
| `Plan` (`sms_plans`) | Enterprise/billing | `code` unik, `name`, `currency`, `monthly_fee`, `included_emails`, `email_overage_price`, `status active|retired` | `billing.generate_invoice`, abonime | **Pjesërisht**: identiteti (`code`,`name`,`status active|retired`) → Product; çmimet → pricing (M5-d/M9) | emërtimi/semantika e `active|retired` është precedent; çmimi s'është katalog |
| `Subscription` | Enterprise/billing | `owner_ref` unik, `plan_id`, `pending_plan_id`, `status`, `started_at`, `periods_billed`, `cancel_at_period_end` | faturimi periodik | **Pjesërisht**: lidhja enterprise↔produkt → `EnterpriseProduct` (M5-b); numëruesit e faturimit mbeten operacionalë | assignment ≠ gjendje faturimi |
| `AccountPlan` (`sms_account_plans`) | Enterprise | `owner_ref` unik, `rate_card_id`, `enabled`, `rate_limit_per_min`, `email_rate_limit_per_min` | dërgimi SMS/email (kill switch, limite) | **Po → M5-b/c** (assignment SMS + `limits`) | është assignment-i SMS de facto; `enabled` ≈ status |
| `RateCard`/`RateCardVersion`/`Rate` | Enterprise | `name` unik, `currency`, versione me `effective_from`, norma për prefiks | tarifimi në dërgim | **Po, por jo në M5-a/b** → pricing (M5-d / M9–M10) | kompleksitet: versione, prefikse/vende, efektivitet |
| `SenderId` | Enterprise | `owner_ref`, `country`, `value`, `kind`, `status`, `reviewed_by` | dërgimi SMS, miratimet | **Jo në M5**: vendim miratimi → M10 | proces miratimi, jo katalog |
| `Route` | Enterprise (platformë) | `prefix`, `country`, `provider`, `priority`, `enabled` | dërgimi (zgjedhje provider) | **Jo** (Gateway) | operacional, jo komercial |
| `Switch` | Enterprise | `name`, `enabled`, `reason` | kill switch globale | **Jo** | operacional |
| `EmailDomain`, `Template`, `Keyword`, `ApiKey` | Enterprise/tenant | gjendje e tenant-it | përdorimi | **Jo** | të dhëna operacionale |
| Reference të jashtme (`Payment.external_id`) | Enterprise | id sesioni i gateway-t të pagesës | `payments` | **Jo si mapim produkti** | s'ka mapim provider/WMS për enterprise në kod; nevoja për `external_account_mapping` është hipotetike → vendim i veçantë (M5-c) |
Përfundim: sot ekzistojnë **dy produkte reale** (SMS, email) pa tabelë katalogu; çmimi dhe limitet janë të shpërndara te `Plan`/`AccountPlan`/`RateCard`.

## 2. Fazat e rekomanduara
- **M5-a (zbatuar):** `products` (katalog global) + REST admin + audit minimal (`audit_log`).
- **M5-b:** `enterprise_products` (assignment): FK RESTRICT, `UNIQUE(enterprise_id, product_id)`, status, API admin; produkt `retired` s'caktohet te Enterprise të reja.
- **M5-c:** config/limits dhe mapim i jashtëm — vetëm nëse justifikohet (analiza më poshtë).
- **M5-d / M9–M10:** pricing (rate cards, vende, nivele); katalogu publik/vetë-regjistrimi → M8.
Asnjë fazë s'e prek dërgimin ose sinkronizon Enterprise.

## 3. Skema e M5-a
**`products`** (migrimi `0004`): `id` UUID PK (v4) · `code` String(32) UNIK, **i pandryshueshëm** · `name` String(120) · `description` String(1000) opsionale · `channel` `sms|email` (CHECK, i pandryshueshëm) · `status` `active|retired` (CHECK) · `created_at`, `updated_at`.
- **Pse `code`:** identifikues makinë i qëndrueshëm (`sms`, `email`, `sms_premium`); `name` s'është identifikues. Normalizim: `strip` + lowercase, pastaj `[a-z][a-z0-9_]{1,31}` (pa hapësira, pa vizë, pa Unicode); CHECK në DB `code = lower(trim(code))`, gjatësi ≥ 2.
- **Pse `channel` (kolonë, jo JSON):** përcakton cilin pipeline përdor Enterprise; invariant relacional; sot vetëm `sms`, `email`.
- **Status `active|retired`:** ndjek vokabularin ekzistues (`PlanStatus`) dhe semantikën e tij ("s'mund të caktohet më; ekzistueset vazhdojnë"). I kthyeshëm nga admin-i.
- **Qëllimisht jashtë (M5-a):** flamuj publikë/regjistrimi (`visible`, `self_registration_enabled`, `requires_*` → M8), `default_config` (M5-c), rregulla sipas vendit (M10), çmime.
- **Pandryshueshmëria:** ORM refuzon ndryshimin e `code`/`channel` dhe `DELETE` (`ImmutableError`); API s'pranon ato fusha në PATCH (422). Kufizim: një UPDATE SQL i drejtpërdrejtë anashkalon listener-in ORM (kontrollet CHECK mbrojnë formën, jo pandryshueshmërinë).

**`audit_log`** (migrimi `0005`; vendimi: **po, tani**, në formë minimale): `id` UUID · `actor_id` FK `users.id` RESTRICT NOT NULL · `action` (`product.create`, `product.update`) · `resource_type` · `resource_id` · `detail` JSON (`{"after":…}` te krijimi, `{"before":…,"after":…}` vetëm fushat e ndryshuara) · `created_at`; indeks `(resource_type, resource_id, created_at)`.
Shkruhet nga API në **të njëjtin transaksion** me ndryshimin; vetëm-shtim (ORM refuzon UPDATE/DELETE); pa audit leximesh; pa event sourcing; shkrimet pa ndryshim real dhe kërkesat e refuzuara (401/403/409/422) nuk lënë rresht. Arsyeja: këto janë shkrimet e para të një aktori të identifikuar, ndaj audit-i hyn me to, jo si retrofit. **Gate para M8/M9:** audit për `users` (krijim/çaktivizim), për CLI bootstrap dhe për veprime financiare.

## 4. Lifecycle (semantika përfundimtare)
- **Produkt `active`:** mund të caktohet (M5-b).
- **Produkt `retired`:** s'caktohet te Enterprise të reja (rregull i zbatuar në M5-b); assignment-et ekzistuese **nuk fshihen as ndryshohen automatikisht**; reaktivizimi lejohet.
- Pa fshirje fizike; pa workflow miratimi ose marketplace.

## 5. API (admin, Bearer JWT; shih `docs/M4_ARCHITECTURE.md` §M4-d)
| Metodë | Rruga | Roli | Shënim |
|---|---|---|---|
| GET | `/admin/products?status=&channel=&limit=&offset=` | admin, operator | renditje `created_at, id`; `limit` 1..500 |
| POST | `/admin/products` | admin | 201; `{code, name, channel, description?, status?}`; 409 për code ekzistues; 422 validim |
| GET | `/admin/products/{id}` | admin, operator | `id` UUID (422 nëse jo UUID), 404 |
| PATCH | `/admin/products/{id}` | admin | vetëm `name`, `description`, `status`; `code`/`channel`/fusha të panjohura → 422; `description: null` e pastron; `name: null` → 422 |
Pa DELETE/PUT (405). Transaksioni: API bën `commit` (service dhe audit s'bëjnë). Gabimet: `{"detail": {"code": not_found|conflict|invalid, "message": …}}`.

## 6. Analiza e config-ut (për M5-b/c; jo implementuar)
Parim: **kolonë** për invariantë relacionalë/të interrogueshëm/me constraint; **JSON** vetëm për konfigurim të zgjerueshëm specifik produkti, i validuar me skemë sipas `channel`.
| Fushë (kandidate) | E interrogueshme? | Constraint? | Komerciale? | Vendim i propozuar |
|---|---|---|---|---|
| `status` (active/suspended) | po | CHECK | jo | **kolonë** |
| `activation_date` | po | — | po | **kolonë** (nullable) |
| `rate_card_id`/çmim | po | FK | po | **jo këtu**: pricing i veçantë (M5-d/M9); s'ngjitet ad-hoc |
| `rate_limit_per_min`, `email_rate_limit_per_min` | jo (lexohen në dërgim) | ≥ 0 | jo | **JSON `limits`**, skemë sipas kanalit (SMS vs email kanë çelësa të ndryshëm) |
| ID llogarie provider/WMS/partner | jo | — | jo | **jo në M5-a/b**; s'ka konsumator sot. Nëse duhet (M7/M12): `EnterpriseProduct.config` ose entitet mapimi i veçantë, jo te `Product` (është per-enterprise) |
Rreziku i "dump në JSON": çdo çelës i lejuar kërkon skemë, kufi madhësie dhe test; çelës i panjohur refuzohet.

## 7. Borxh i mbetur komercial/çmimi
Rate cards, versione, vende/operatorë, nivele, monedha, VAT/faturim, plan mujor dhe `included_emails` janë ende vetëm te Enterprise; asnjë çmim s'është në Central. Katalogu publik, `visible`/vetë-regjistrim, rregulla sipas vendit dhe miratime → M8/M10. Audit i plotë (users, CLI) → gate para M8/M9. `retired` si vokabular: i kthyeshëm (nuk është gjendje terminale).

---
## 8. M5-b — `EnterpriseProduct` (assignment)

### Audit i koncepteve të assignment-it (çfarë është identitet / çmim / runtime)
| Koncept | Pronari | Fusha | Identitet assignment? | Çmim? | Runtime operacional? | M5-b | Më vonë |
|---|---|---|---|---|---|---|---|
| `Subscription` (`sms_subscriptions`) | Enterprise/billing | `owner_ref` unik, `plan_id`, `pending_plan_id`, `status active|cancelled`, `started_at`, `periods_billed`, `cancel_at_period_end` | po (enterprise↔plan) | indirekt (`plan`) | po: `periods_billed`, `started_at` si ankorë faturimi | vetëm **identiteti** (çifti + status) | cancel/pending, periudha → M9 |
| `AccountPlan` (`sms_account_plans`) | Enterprise | `owner_ref` unik, `rate_card_id`, `enabled`, `rate_limit_per_min`, `email_rate_limit_per_min` | po (enterprise↔SMS) | `rate_card_id` (referencë çmimi) | po: `enabled` (kill switch), limite në dërgim | vetëm `status` ≈ `enabled` në kuptim administrativ | `rate_card_id` → pricing (M5-d/M9); limite → M5-c |
| `Plan` | Enterprise/billing | `code`, `monthly_fee`, `included_emails`, `email_overage_price`, `status active|retired` | jo (katalog) | po | jo | identiteti u modelua në `Product` (M5-a) | çmimet → pricing |
| `rate_card_id` | Enterprise | FK te `sms_rate_cards` | jo | po | po (tarifim) | **jo** | M5-d/M9 |
| `enabled` | Enterprise (`AccountPlan`), `Switch` globale | boolean | jo | jo | po (kill switch operacional) | **jo**: `status` i Central është vendim administrativ, jo kill switch runtime | sync i statusit → M7 |
| limite (`rate_limit_per_min`…) | Enterprise | int | jo | jo | po | **jo** | M5-c (propozim: JSON `limits` me skemë sipas kanalit) |
| provider/account config | `Route` (global), asnjë per-enterprise | — | jo | jo | po | **jo** | M5-c/M7/M12, vetëm me nevojë reale |
Asnjë nuk kopjohet verbatim: `EnterpriseProduct` mban vetëm çiftin + statusin administrativ.

### Skema (`enterprise_products`, migrimi `0006`)
`id` UUID PK (v4) · `enterprise_id` UUID FK `enterprises.id` **RESTRICT** · `product_id` UUID FK `products.id` **RESTRICT** · `status` `active|suspended` (CHECK) · `created_at`, `updated_at` · **UNIQUE `(enterprise_id, product_id)`** · asnjë kolonë tjetër (pa config, çmim, monedhë, rate_card, limite, llogari të jashtme; testuar).
- **PK UUID, jo kyç i përbërë:** URL-të, audit-i (`resource_id`) dhe lidhjet e ardhshme (M7/M9) përdorin një id të vetëm; çifti mbetet unik me constraint. Pa indeks shtesë: UNIQUE mbulon kërkimet sipas `enterprise_id` (kolona e parë); sipas produktit nuk ka model përdorimi sot.
- **Pandryshueshmëria:** ORM refuzon ndryshimin e `enterprise_id`/`product_id` ("move assignment") dhe `DELETE`; API s'i pranon në PATCH (422).
- Identiteti i produktit (`code`, `name`, `channel`, `status`) **nuk denormalizohet**; lista bën JOIN.

### Lifecycle dhe semantika
- `active`: assignment i lejuar nga control plane. `suspended`: ekziston por i çaktivizuar administrativisht. Pa `pending/approved/rejected/cancelled/expired` (s'ka workflow; vjen në M8).
- **POST ekzistues → 409** (pavarësisht statusit), mesazhi përmban `id` dhe `status`. Zgjedhur për konsistencë me `Product` (409 për code ekzistues) dhe sepse POST **nuk riaktivizon fshehurazi**: aktivizimi është PATCH eksplicit. Unique në DB është burimi i së vërtetës ndaj garës (IntegrityError → 409).
- `PATCH` me statusin e njëjtë = no-op (200, pa audit).

### Matrica e ndërveprimit të statuseve
| Veprim | Enterprise `active` + Product `active` | Enterprise `suspended` | Product `retired` |
|---|---|---|---|
| assign i ri (POST) | lejohet | **409** | **409** |
| `suspend` assignment | lejohet | lejohet | lejohet |
| `activate` assignment (suspended→active) | lejohet | **409** | **409** |
| statusi i njëjtë | no-op | no-op | no-op |
| suspendim Enterprise / `retired` Product | — | **nuk ndryshon asnjë assignment** | **nuk ndryshon asnjë assignment** |
Assignment-et ekzistuese ruhen për histori; asnjë cascade. Enterprise lifecycle ≠ assignment lifecycle.

### API (admin, Bearer JWT)
`GET /admin/enterprises/{enterprise_id}/products?status=&channel=&limit=&offset=` (admin, operator) · `POST` `{"product_id"}` → 201 (admin) · `GET …/{assignment_id}` (admin, operator; assignment i enterprise-it tjetër → 404) · `PATCH …/{assignment_id}` `{"status"}` (admin; fusha të tjera → 422). Pa DELETE (405). Përgjigja: `id, enterprise_id, product_id, product_code, product_name, product_channel, product_status, status, created_at, updated_at`. Gabime: 404 (Enterprise/Product/assignment i panjohur), 409 (konflikt/rregull statusi), 422 (UUID/status i pavlefshëm); UUID parse-ohen pa normalizim tjetër.

### Audit
`enterprise_product.assign` (`{"after": {enterprise_id, product_id, status}}`) dhe `enterprise_product.update` (`{enterprise_id, product_id, before, after}` vetëm `status`), `resource_type=enterprise_product`, `resource_id`=id e assignment-it; në të njëjtin transaksion; kërkesat e refuzuara dhe no-op nuk lënë rresht; rollback heq edhe audit-in.

### Pa sync, pa pricing/config
Asnjë thirrje drejt Enterprise, event, polling, worker, dual-write ose provisioning; Enterprise nuk e konsumon assignment-in. Pa kontratë të përbashkët (konsumatori i dytë vjen në M7).

### Përgjegjësi të ardhshme
- **M5-c:** `limits`/config (vetëm JSON me skemë sipas kanalit) dhe mapim i jashtëm, **vetëm nëse ka nevojë reale**; `activation_date` nëse kërkohet.
- **M5-d / M9–M10:** pricing (rate cards), faturim, miratime sender ID/shtet.
- **M7:** sync i `status` të assignment-it drejt `AccountPlan.enabled`/runtime (Central → Enterprise), me kontratë të versionuar.
