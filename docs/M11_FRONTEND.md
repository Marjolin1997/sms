# M11 — Frontend / Console: konventat e bazës (M11-A)

Zbatohet te `frontend/` (React 18 + Vite, JSX, pa TypeScript). Ky dokument është kontrata për ekranet e radhës (M11-B/C).

## 1. Gjendja e gjetur (audit)

| Fusha | Gjendja |
|---|---|
| Aplikacione | **Vetëm një: konsola Enterprise** (`frontend/`). **Nuk ekziston frontend Central.** |
| Framework | React 18.3, Vite 5, `@vitejs/plugin-react`; JS/JSX (pa TS, pa ESLint, pa typecheck) |
| Routing | hash (`#senders`), pa librari; tabela e rrugëve tani te `src/routes.js` |
| Gjendje | `useState`/`useLoad`; pa Redux/Query. Auth = `/v1/me` + çelës API në `sessionStorage` |
| API | `src/api.js` (fetch i vogël): Bearer, `owner_ref` automatik, Idempotency/TOTP hap-i-dytë |
| Role/leje | `permissions[]` nga `/v1/me`; `can(me, "a|b")`; serveri vendos |
| UI | `src/ui.jsx` + `styles.css` (230 rreshta, variabla CSS); pa librari komponentësh |
| Forma/tabela | native + `Field`, `Table` (kartë në mobile); pa librari |
| Toast/dialog | `UiProvider` (`toast`, `confirm`) |
| i18n | `i18n.jsx`, çelës = anglisht, fjalor shqip; `npm run i18n:check` (pjesë e build) |
| Teste | Vitest + Testing Library (jsdom). Komanda: `npm test`, `npm run build` |
| Lint/typecheck | **nuk ka** (borxh, shih §10) |

## 2. Kufiri Enterprise vs Central

**Enterprise UI (`frontend/`, çelës API i klientit/stafit, `/v1/*`)** — përdoruesi/klienti i tenant-it dhe stafi i platformës:
mesazhet, kampanjat, kontaktet/listat, sender ID të klientit (+ statusi efektiv), API keys/webhooks, portofoli, faturat/raportet, ekipi/lejet.
Faqet "Staff" aktuale (miratime lokale, tarifa, finance, provider health) mbeten këtu për kohën që autoriteti është `local`.

**Central UI (aplikacion i ri, `/admin/*` i `apps/central`, auth i adminit Central)** — vetëm operatorët e platformës:
enterprises, katalogu i produkteve, produktet e enterprise, **politikat e sender-ave dhe regjistri/vendimet**, çmimet, pagesat/top-up, faturimi periodik,
provisioning, reconciliation, readiness/operacione, audit.

Rregulla: veprimet admin të Central **nuk hyjnë kurrë** në UI-në e klientit. Dy aplikacione, dy auth-e, dy `api.js` (i njëjti dizajn, baza URL ndryshe).
Primitivët (`ui.jsx`) janë të përbashkët; kur të ndërtohet Central, nxirren në paketë të përbashkët (`packages/ui`) pa ndryshuar API-n e tyre.

## 3. Shell, rrugë, guard-e (`src/routes.js`, `src/App.jsx`)
- **Një tabelë rrugësh** (`NAV`): `id, label, perm, global, load()`; menyja, guard-et dhe breadcrumb-et dalin nga ajo. Faqet ngarkohen lazy (`import()`), bundle kryesor 350→246 kB.
- `resolveRoute(me, hash)` kthen `ok | forbidden | not_found`. **403** dhe **404** janë faqe të vërteta (`pages/ErrorPages.jsx`), kurrë riudhëzim i heshtur.
- Deep-link i sigurt: vetëm `[a-z0-9-]{1,40}` pranohet si id rruge; nën-rrugë `#senders/12?x=1` → `rest: ["12"]` për faqet e detajit.
- Faqet NUK bëjnë kontroll roli vetë: shtoni `perm` në tabelë. Brenda faqes përdoret `can(me, perm)` vetëm për të fshehur/çaktivizuar butona.
- `PageHeader` (titull, përshkrim, breadcrumbs, veprime), `ErrorBoundary` (gabim renderimi → mesazh i rikuperueshëm), `Suspense` skeleton, banner offline, skip-link, `<nav aria-label>`.

## 4. Auth / sesion
- Burim i vetëm: `me` te `App` + çelësi në `sessionStorage` (kurrë `localStorage`, kurrë në DOM/log).
- **401 gjatë sesionit** → `setSessionExpiredHandler` pastron çelësin dhe kthen te hyrja me njoftim. **403 nuk e mbyll sesionin** (vetëm gabim).
- Roli/lejet vijnë nga `/v1/me`; UI nuk i llogarit vetë. Backend-i rikontrollon çdo veprim.

## 5. API client (`src/api.js`)
- `api.get/post/put/patch/del`, `api.page`, `api.download`; **mos përdorni `fetch` direkt në faqe**.
- Timeout 30 s (`timeoutMs`), anulim me `{ signal }` (`useLoad(fn)` e kalon `signal` dhe e anulon automatikisht), përgjigjet e vjetra nuk mbishkruajnë të rejat.
- `ApiError`: `status, code, kind, fields, retryAfter, retryable, friendly`.
  `kind` ∈ `network | unauthenticated | forbidden | not_found | conflict | rate_limited | validation | server | other`.
- `fields` = gabime për fushë nga 422 (`{value: "too short"}`) — shfaqen te `Field error=`; serveri mbetet autoritet.
- Faqosje: `api.page(path, {limit, offset, ...filters})` → `{items, hasMore, limit, offset}` (kontrata Central `has_more` ose varg Enterprise). `Pagination` e shfaq.

## 6. Modeli i gabimeve
| HTTP | kind | Sjellja në UI |
|---|---|---|
| 0 / timeout | network | "Jeni jashtë linje"/serveri vonon + Provo përsëri |
| 400/422 | validation | gabim për fushë + mesazh i përgjithshëm |
| 401 | unauthenticated | logout me njoftim |
| 403 | forbidden | mesazh leje, pa retry |
| 404 | not_found | "S'u gjet", pa retry |
| 409 | conflict | "Ndryshoi ndërkohë, rifreskoni" |
| 429 | rate_limited | koha e pritjes nga `Retry-After`, retry |
| 5xx | server | tekst i përgjithshëm; **teksti i serverit nuk shfaqet** nëse kodi s'është i njohur |

Kodet e sender-it me tekst të qartë (`api.js` FRIENDLY): `sender_authority_frozen`, `sender_policy_denied`, `sender_revoked`, `sender_rejected`, `sender_pending`, `not_synchronized`, `sender_not_allowed`.
Komponentët: `ErrorBox` (inline, formular), `ErrorState` (bllok/faqe, me titull sipas `kind`, retry, `stale`), `displayMessage(err)`.

## 7. Gjendje loading / empty / retry
`Skeleton` (fillim), `Table loading`, `Button busy`, `Empty`/`emptyTitle`, `ErrorState retry`, `useLoad().stale` (të dhëna të vjetra + gabim i fundit → shfaq të dhënat dhe njoftimin). Pa ekran të bardhë: `Suspense` + `ErrorBoundary` në çdo faqe.

## 8. Primitivë dhe statuset
`PageHeader, Breadcrumbs, Card, Stat/MetricCard, StatusBadge(=Badge), Table, Pagination, FilterBar, SearchInput, Field/FormField, Empty, ErrorBox, ErrorState, ErrorBoundary, DetailList/DetailRow, AuditTimeline, Tabs, SecretBanner, confirm()` (dialog me kapje fokusi dhe Esc).
**Statuset**: një hartë `STATUS_TONES` (`good|info|warn|bad|muted`) — faqet kalojnë vetëm statusin e serverit te `<StatusBadge>`; teksti dhe simboli shfaqen gjithmonë (jo vetëm ngjyra); i panjohur → neutral, kurrë jeshil.
Të ardhshme (M11-B): `DateRange`, `Select` i përbashkët, `FilterBar` me URL-state.

## 9. Sender-policy: kontrata e frontend-it (`src/senders/`)
Frontend-i **shfaq** gjendjen e serverit dhe **dërgon** veprime; nuk vendos miratim/politikë/autoritet.
- `adaptSender` (Enterprise `GET/POST /v1/sender-ids`): `localStatus, effectiveStatus, centralStatus, syncStatus, authorityMode, canResubmit, canReviewLocally` (+ admin: `drift, cpRevision, policyRevision, bootstrapIssues`). Fushat M10 që mungojnë → gjendje lokale e sigurt. Klienti shfaq **`effectiveStatus`**, ndërsa `status` mbetet lokal.
- `isAwaitingSync`: `not_synchronized`/`stale` shfaqen si "po verifikohet", kurrë "miratuar".
- Central (për M11-C): `adaptRegistrySender`, `adaptDecision` (→ `AuditTimeline`), `adaptCountryPolicy` (`allowed`, `requires_approval`, `revision`), `adaptPage`.
- `useSenderIds(filters)` (Enterprise). Hook-et Central shtohen me aplikacionin Central.
- `DRIFT_CATEGORIES`/`SENDER_DOMAIN_CODES` janë konstante shfaqjeje, jo rregulla.

## 10. Navigimi i propozuar
**Enterprise** (përdor terminologjinë ekzistuese): Overview · Messaging (Send, Message history, Inbox, Reports, Campaigns) · Audience (Contacts) · Setup (**Sender IDs & templates**, Email domains, Webhooks) · Money (Wallet, Billing) · Developers (Guide, API keys) · Staff (Approvals, Accounts, Rates & routes, Finance, Provider health, Security, Admin).
Ndryshimi i vetëm i planifikuar: te Sender IDs shtohen kolonat *effective status* + *sync*, dhe `Approvals` merr shenjën "menaxhohet nga Central" kur autoriteti është `central`.
**Central** (aplikacion i ri): Dashboard · Enterprises · Products · Sender governance (Sender registry, Country policies) · Commercial (Pricing, Payments, Billing) · Provisioning · Reconciliation · Operations (readiness) · Audit.

## 11. Borxh i mbetur
1. Nuk ka ESLint/Prettier/typecheck (propozim: ESLint + `// @ts-check`/JSDoc, pa migrim të plotë në TS).
2. Faqet ekzistuese ende përdorin `ErrorBox` + `useAction` për forma; `Field error` nga `err.fields` duhet lidhur forma-nga-forma (M11-B kur preken).
3. Lista pa faqosje server-side (Messages, Contacts, Accounts) — backend-i i Enterprise kthen vargje të kufizuara (p.sh. `/v1/sender-ids` max 300).
4. Faqet nuk përdorin ende `AuditTimeline`/`Pagination`/`FilterBar` (shtohen me ekranet e reja).
5. Routing me hash; librari routing nuk u fut (rrezik/rishkrim pa nevojë). Rivlerësohet kur Central të ketë detaje të thella.
6. Pa testet e shfletuesit (e2e Playwright ekziston vetëm për API/console të thjeshtë).
7. Aplikacioni Central nuk ekziston dhe paketa e përbashkët `ui` nuk është nxjerrë.

## 12. M11-B — Sender IDs (Enterprise): sjellja dhe kufizimet e API-së
**Fusha të serverit që UI shfaq (asnjë nuk rillogaritet):** `effective_status` (shenja kryesore), `authority_mode`, `sync_status`, `can_resubmit`, `can_review_locally`; vetëm staf: `central_status`, `drift`, `cp_revision`, `policy_revision`, `bootstrap_issues`.
- **Lista** (`senders/components.jsx` `SenderList`): Sender, Country, Type, Status (efektiv; stafit i shtohet "Local record: …" kur ndryshon), Sync, Authority, Requested, veprime. Në mobile fshihen Type/Sync/Authority/Requested (`hideSm`).
- **Detaji** `#senders/<id>` (`SenderDetail`); stafi e hap pa zgjedhur llogari (route `detail: true`). Klienti nuk sheh diagnostikën.
- **Ridërgim**: vetëm `can_resubmit`; POST `/v1/sender-ids` me `owner_ref` të rreshtit; lista rikërkohet nga serveri para njoftimit të suksesit.
- **Shqyrtim lokal** (`SenderReviewActions`): vetëm staf (pa `owner_ref`, leje `sender:review`) dhe vetëm kur `can_review_locally`; në `central` shfaqet "Sender approvals are managed centrally." Cili veprim (approve/reject kundrejt revoke) zgjidhet nga statusi LOKAL (`pending` / `approved`) sepse vendimet lokale veprojnë mbi rekordin lokal; serveri rikontrollon tranzicionin. Veprimet dërgohen me `noOwner` (skema strikte `extra=forbid`). 409 (`sender_authority_frozen`/konflikt) → njoftim miqësor + rikërkim.
- **Forma e kërkesës**: API pranon vetëm `country` + `value` (lloji nnxirret nga serveri; nuk ka fushë `kind`/`evidence`). Validim vetëm `≥3` karaktere; gabimet 422 `err.fields` dhe `invalid_sender` shfaqen te fusha me fokus; të tjerat si gabim i përgjithshëm.
- **Njoftimi sync**: `not_synchronized` paralajmëron vetëm kur `effective_status` është `not_synchronized`; `stale` vetëm në autoritet `central`. Në local/shadow `sync_status` është diagnostikë e stafit.

**Kufizime të API-së Enterprise (të dokumentuara, jo të zgjidhura me hile në frontend):**
1. `GET /v1/sender-ids` kthen max 300 rreshta, pa faqosje, pa kërkim, pa filtër shteti/lloji; `status` filtron statusin LOKAL. UI filtron (kërkim, status EFEKTIV, shtet, lloj) dhe faqoson 20/faqe vetëm mbi rreshtat e ngarkuar dhe e thotë kur arrihet kufiri.
2. `GET /v1/sender-ids/{id}` nuk ka `created_at`/`reviewed_by` (vetëm lista); detaji i merr nga lista e ngarkuar së fundi, përndryshe "-". Nuk ka `updated_at` askund.
3. Nuk ka endpoint të historikut të vendimeve lokale (`SenderDecision` ekziston në DB, jo në API); historiku Central është në `/admin/senders/{id}/history`. `AuditTimeline` përdoret në M11-C.
4. Filtrim sipas autoritetit/sync në server nuk ekziston.
