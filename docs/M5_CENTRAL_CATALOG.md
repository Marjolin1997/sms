# M5 — Katalogu i produkteve dhe assignment-i te Enterprise (Central)

Statusi: **M5-a (katalogu + audit minimal) zbatuar.** M5-b/c/d të planifikuara, jo të nisura.
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
