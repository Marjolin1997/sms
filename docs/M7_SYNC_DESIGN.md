# M7-a — Sinkronizimi Central → Enterprise: arkitekturë dhe dizajn (VETËM DIZAJN)

Statusi: audit dhe rekomandim. **Asnjë kod, migrim, kontratë, outbox, worker, endpoint ose token.**
Parim: Central = gjendja autoritative e control plane; Enterprise = snapshot operacional lokal. `messages.submit` / `emails.submit` **nuk pyesin kurrë Central** dhe punojnë mbi gjendjen e fundit të mirë të autorizuar.
Pa DB të përbashkët. Kontrata e sync-ut është **familje e veçantë** nga webhook-et e klientëve (`EventEnvelopeV1`).

## 1. Inventari i gjendjes autoritative të Central
| Gjendja | Pronari | Konsumatori në Enterprise | Hot path? | Konsistenca e kërkuar | Vjetërsia e pranueshme | Sjellja kur dështon |
|---|---|---|---|---|---|---|
| `Enterprise.id` | Central | çelësi i çdo snapshot-i (`enterprise_id` kanonik, tashmë i përbashkët nga M4-c) | po (identiteti i tenant-it) | e fortë (s'ndryshon) | — | s'ka ndryshim |
| `Enterprise.status` (`active|suspended`) | Central | bllokon dërgimin për gjithë enterprise-in | **po** (entitlement) | **zbatim i shpejtë** (sensitive: siguri/komercial) | p95 ≤ 60 s, alarm 5 min, SLO i fortë 15 min | fail-static (shih §14) |
| `Enterprise.name` | Central | etiketë shfaqjeje | jo | eventuale | orë | s'bllokon asgjë |
| `Product` (`code`, `channel`, `status`, `name`) | Central | **nuk sinkronizohet si entitet**; `code`+`channel` ngjiten si fusha të assignment-it | jo | — | — | — |
| `EnterpriseProduct.status` (`active|suspended`) | Central | `enabled` per kanal në snapshot | **po** | **suspend: i shpejtë**; activate: eventual | suspend ≤ 60 s p95; activate ≤ 15 min | fail-static |
| *(M5-c i shtyrë)* `EnterpriseProduct.rate_limit_per_min` | Central | cak në submit | **po** | ulje: ≤ 5 min; rritje: ≤ 15 min | si më sipër | fail-static |
| *(M10)* politika sender/vend | Central | miratime sender | po | e dizajnuar në M10 | — | — |
| *(M9)* çmime / kredi | Central autorizon; **ledger-i mbetet lokal** | tarifim, wallet | po | s'është sync i config-ut: dizajn i veçantë M9 | — | — |
| Përdoruesit e stafit të Central (`users`) | Central | **kurrë** | — | — | — | — |

## 2. Gjendja e sotme e Enterprise (target) dhe hartimi i saktë
Evidencë: `sms_enterprises(id, owner_ref, external_id, legal_name, short_name, status[nuk lexohet nga asnjë kod], …)`; `AccountPlan` (per-owner, UNIQUE `owner_ref`): `rate_card_id` **NOT NULL**, `enabled`, `rate_limit_per_min`, `email_rate_limit_per_min`; `messages.submit` lexon `AccountPlan` (enabled, limit, `rate_card_id` për çmim); `emails.submit` lexon po `AccountPlan` (enabled, limit email); `campaigns` e lexon për çmim; `Plan`/`Subscription` janë faturim; tenant-i zgjidhet nga `TenantContext(enterprise_id, owner_ref)` (çelësi API mban `enterprise_id`).
| Fusha e Central | Target sot në Enterprise | Konsumatori | Mospërputhja | Target i rekomanduar |
|---|---|---|---|---|
| `Enterprise.id` | `sms_enterprises.id` | gjithandej | asnjë | i pandryshuar |
| `Enterprise.status` | `sms_enterprises.status` (e papërdorur) | **asnjë** | s'zbatohet nga asgjë | përditësohet nga sync **dhe** pasqyrohet te `enabled` efektiv i snapshot-it (§5) |
| `Enterprise.name` | `legal_name`/`short_name` (NULL) | asnjë | emri i Central është emër shfaqjeje | `short_name` (vendim i vogël në M7-d) |
| `EnterpriseProduct(SMS).status` | `AccountPlan.enabled` (**i përbashkët**) | `messages.submit` | `enabled` i vetëm për të dyja kanalet | rresht snapshot per kanal (§5) |
| `EnterpriseProduct(EMAIL).status` | `AccountPlan.enabled` (**i njëjti**) | `emails.submit` | s'mund të shprehet veç SMS | rresht snapshot per kanal |
| `EnterpriseProduct.rate_limit_per_min` | `AccountPlan.rate_limit_per_min` (SMS) / `email_rate_limit_per_min` (email, pa shkrues) | submit | dy kolona specifike; email s'ka shkrues | një kolonë e vetme per assignment te snapshot |
| `rate_card_id` | `AccountPlan.rate_card_id` NOT NULL | tarifim SMS, fushata | lidh enterprise vetëm-email me çmim SMS | **mbetet te `AccountPlan`** (pricing, M9); email s'e lexon më |

## 3. Zgjidhja e mospërputhjes `AccountPlan`
| Kriter | **A**: zgjero `AccountPlan` (`sms_enabled`, `email_enabled`, `*_rate_limit`) | **B**: tabelë snapshot operacional e re, një rresht per (enterprise, produkt) | **C**: harto statusin te `AccountPlan.enabled` ekzistues |
|---|---|---|---|
| Produkti i tretë | **kolonë e re çdo herë** (jo shkallëzon) | **rresht i ri**, pa ndryshim skeme | s'shprehet |
| Kompleksiteti i migrimit | i ulët (4 kolona) | mesatar (tabelë e re + applier) | zero |
| SQL në hot path | 0 shtesë | **0 shtesë** nëse lexohet me një `JOIN` (§3.1); përndryshe +1 në SMS, 0 në email | 0 |
| Prapavajtje | po | po (fallback te `AccountPlan` kur s'ka rresht) | po |
| Semantika M1 (tenant) | `owner_ref` unik i trashëguar | tabelë e re me `enterprise_id` kanonik (pa `owner_ref`) | trashëgon `owner_ref` |
| Varësia nga billing | email mbetet i lidhur me `rate_card_id` NOT NULL | **e zgjidh**: email s'lexon `AccountPlan` | e ruan lidhjen |
| Pastrimi `owner_ref` (M13) | rritet borxhi | pastër (s'ka `owner_ref`) | rritet borxhi |
| Kosto kryesore | borxh strukturor | applier + tabelë | humb per-kanal (**refuzohet**) |
**Rekomandimi: B.**

### 3.1 Modeli snapshot i rekomanduar (Enterprise, i dizajnuar, jo i krijuar)
`sms_entitlements`: `id` UUID · `enterprise_id` UUID FK `sms_enterprises.id` · `product_code` String(32) · `channel` (`sms|email`) · `status` (`active|suspended`, nga assignment) · `enabled` BOOL **efektiv** (`status = active` ∧ `enterprise.status = active`, i rillogaritur nga applier në të njëjtin transaksion) · `rate_limit_per_min` INT NULL (kur të aktivizohet; §15) · `revision` BIGINT (versioni i assignment-it në Central) · `applied_at` · `UNIQUE(enterprise_id, product_code)`. Pa `owner_ref`, jo `TenantOwned`; shkruhet vetëm nga applier-i (SYSTEM context); lexohet me filtër eksplicit `enterprise_id`.
- **Rregulli i kanalit** (disa produkte në të njëjtin kanal): kanali është i lejuar nëse ekziston ≥ 1 rresht `enabled`; caku = minimumi i vlerave jo-NULL midis rreshtave `enabled` (NULL → parazgjedhja e Enterprise 600). Sot ka një produkt per kanal; rregulli është i përcaktuar që produkti i tretë të mos kërkojë ndryshim skeme.
- **Hot path pa SQL shtesë:** `select(AccountPlan, Entitlement).outerjoin(...)` në një pyetje (SMS) ose `Entitlement` vetëm (email). Për kontroll: garda ekzistuese SQL-count (21 pyetje në submit SMS) dhe benchmark; buxheti ≤ 5%.
- **Prapavajtje/fallback:** pa rresht snapshot → sjellja e sotme (`AccountPlan`). Fusha e flamurit `SMS_CP_ENTITLEMENTS = off | shadow | enforce` (§12). `AccountPlan` mbetet për çmim (M9) dhe trashëgimi deri në M13.
- **Mohimi fiton:** `effective = enabled_local(AccountPlan/Switch) ∧ enabled_cp`; çelësat lokalë të emergjencës mbeten (break-glass) deri te M11.

## 4. Burimi i së vërtetës dhe pa varësi runtime
Enterprise zbaton vetëm nga snapshot lokal; Central i padisponueshëm nuk ndalon dërgimin (fail-static, §14). Snapshot-i shkruhet vetëm nga applier (jo nga rruga e kërkesave) dhe audit-ohet në `sms_audit_log` si veprim `system` (`control_plane.apply`).

## 5. Familja e kontratave të sync-ut (`cp.v1`; e ndarë nga `EventEnvelopeV1`)
Gjendje-bazë (jo delta): çdo ngjarje mban **gjendjen e plotë të dëshiruar** të entitetit → idempotente dhe tolerante ndaj rendit/humbjes.
- Zarf: `{"schema":"cp.v1","event_id":uuid,"type":…,"enterprise_id":uuid,"entity":{"type":…,"id":uuid},"revision":int,"occurred_at":iso,"data":{…}}`; `occurred_at` informative (jo autoritet rendi). Serializim kanonik deterministik (si V1: `sort_keys`, separatorë kompakt, ASCII) + golden fixtures **para** çdo ekstraktimi (precedenti M3-d).
- Tipet (emrat përfundimtarë pas M7-b): `EnterpriseUpsertedV1` (`name`, `status`), `EnterpriseProductUpsertedV1` (`assignment_id`, `product:{id,code,channel}`, `status`, `[rate_limit_per_min]`). Pa `deleted` (s'ka fshirje). `Product` s'ka ngjarje të veçantë.
- Pa ORM, pa fusha çmimi; shtimi i fushave = ndryshim i versionuar. Kontratat nxirren në `packages/contracts` **në M7-b** (konsumatori i dytë i vërtetë), me re-export kompatibiliteti.

## 6. Transporti
| Opsion | Latenca | Kompleksiteti | Siguria | Rimëkëmbje nga ndërprerje | Vlerësim |
|---|---|---|---|---|---|
| A. Central shtyn HTTP të nënshkruar | e ulët | dispatcher + retry/lease + gjendje per target në Central; endpoint hyrës te Enterprise | sipërfaqe e re hyrëse te data plane | duhet rikonsilim | mirë por më i rëndë |
| **B. Enterprise tërheq (feed + snapshot)** | interval poll (30–60 s) | Central: vetëm lexim; Enterprise: një worker | s'ka port hyrës te Enterprise | **natyrale** (vazhdon nga kursori/snapshot) | **më i thjeshti i besueshëm** |
| C. Hibrid push + pull | më e ulëta | A + B | si A | shumë e mirë | opsionale më vonë |
| D. Broker (Kafka/Rabbit/SQS) | e ulët | infrastrukturë e re | — | e mirë | **refuzohet**: një deployment, PG ekziston |
**Rekomandimi: B me rikonsilim; `nudge` push opsional (C) vetëm nëse 30–60 s s'mjafton.** Central ofron: `GET /internal/sync/changes?after_seq=&limit=` dhe `GET /internal/sync/snapshot` (gjendja e plotë e scope-it të thirrësit me `revision` per entitet). Enterprise: worker `sync` që thërret feed çdo ~30 s, aplikon, ruan kursorin, dhe bën snapshot të plotë në nisje + çdo orë/ditë.

## 7. Auth shërbim-te-shërbim (Enterprise → Central; drejtim i kundërt me skicën e M4-d për shkak të pull)
- **Rekomandimi:** *client assertion* i nënshkruar asimetrikisht (**EdDSA/Ed25519**): Enterprise mban çelësin privat, Central ruan çelësat publikë të regjistruar (`service_credentials`: `client_id`, `kid`, `public_key`, `status`, `scopes`, `enterprise_scope`). Çdo kërkesë `Authorization: Bearer <JWT>` me `iss=<client_id>`, `sub`, `aud=sms-central-sync`, `kid`, `iat`, `exp ≤ 300 s`, `jti` (cache replay ≈ TTL), `scope` (`sync:read`). Central verifikon dhe kufizon sipas `enterprise_scope`.
- Rotacioni: dy `kid` aktivë njëkohësisht, çaktivizim i vjetrit pas kalimit; nuk ka sekret të përbashkët mes planeve.
- Autenticiteti i përgjigjes: TLS (verifikim certifikate) në fazën 1; **detached JWS** i nënshkruar nga Central mbi trupin e feed-it në fazën e mëvonshme, që Enterprise të verifikojë origjinën edhe përmes proxy-ve.
- Alternativat: HMAC mbi kërkesën (sekret i përbashkët; i refuzuar në M4-d), mTLS (shtresë transporti, opsionale), JWT HS256 i përbashkët (refuzohet).
- Çelësi i përdorur te sync **nuk ripërdoret** me JWT-të e stafit (`CENTRAL_AUTH_SECRET`).

## 8. Idempotencë
Çdo ngjarje: `event_id` (UUID i qëndrueshëm), `enterprise_id`, `entity.id`, `revision` (INT per entitet). Rregulli i aplikimit **për entitet**: `incoming.revision > applied_revision` → apliko; `==` → no-op (dublikat); `<` → injoro (e vjetër, numërohet metrikë). Snapshot dhe feed përdorin të njëjtat gjendje-bazë → rikonsilimi është i sigurt. Pa efekte anësore të dyfishta (applier-i shkruan rreshtin + audit vetëm kur ndryshon).

## 9. Renditja dhe versionimi
**`revision` e qartë per entitet** (jo `updated_at`): rritet me +1 për çdo ndryshim material, brenda transaksionit të ndryshimit, nën kyçje rreshti (`UPDATE … SET revision = revision + 1 … RETURNING`). Timestamps janë vetëm informative (ora e murit s'është autoritet rendi: ndryshim ore, transaksione paralele). Për kursorin e feed-it: `seq` global (identity i outbox-it). Kujdes i njohur: `seq` mund të bëhet i dukshëm jashtë rendi (transaksion më i vonë me `seq` më të vogël) → Enterprise lexon me **mbivendosje** (`after_seq - N`) dhe aplikimi sipas `revision` e bën të padëmshme; rikonsilimi mbyll boshllëqet.
**Epoka e feed-it:** UUID i brendshëm i linjës së DB-së Central; Enterprise e ruan; ndryshim (restore nga backup) → ndalon aplikimin dhe alarmon (parandalon regres revision-esh).

## 10. Outbox transaksional (Central) — skemë kandidate
`sync_outbox`: `seq` BIGINT GENERATED ALWAYS AS IDENTITY PK · `event_id` UUID UNIQUE · `enterprise_id` UUID (indeks; skopimi i feed-it) · `entity_type`, `entity_id`, `revision` BIGINT · `payload` JSON (gjendja e plotë; `schema` brenda) · `created_at`; **UNIQUE(`entity_type`,`entity_id`,`revision`)**. Plus `revision BIGINT NOT NULL DEFAULT 1` në `enterprises` dhe `enterprise_products`.
- Shkruhet **në të njëjtin transaksion** me ndryshimin (service) → "assignment i commit-uar por ngjarje e humbur" është e pamundur; rollback heq edhe ngjarjen. No-op → pa revision, pa ngjarje. `audit_log` mbetet i veçantë (kush/çfarë), outbox = propagim gjendjeje.
- Retention: ruaj ≥ 30 ditë; snapshot i plotë mbulon çdo boshllëk më të vjetër. Trade-off: shkrim shtesë për çdo ndryshim admin (rrallë); nuk ka tabelë gjendjeje per target sepse Central s'dërgon (pull).

## 11. Idempotenca në Enterprise — skemë kandidate
- `revision` BIGINT te `sms_entitlements` (dhe kolonë `cp_revision` NULL te `sms_enterprises`): rreshti vetë mban `applied_revision` → **pa tabelë inbox per ngjarje** (kufizim i rritjes).
- `sms_cp_cursor(feed PK, epoch UUID, last_seq BIGINT, last_success_at, last_error)`: një rresht.
- Varësi që mungon (assignment para Enterprise): applier-i e vë në pritje dhe nuk përparon mbi të në atë cikël; rikonsilimi e zgjidh. Çdo aplikim audit-ohet (`system`).

## 12. Fillimi / snapshot fillestar dhe rikonsilimi (drejtimi Central → Enterprise)
1. **Parakusht:** UUID-të përputhen (M4-c) ✔.
2. **Assignment-et fillestare në Central** për enterprise ekzistues: mjet manual si M4-c (`bootstrap_assignments`, dry-run, idempotent, pa mbishkrim): çdo `AccountPlan` → assignment `sms` (`active` nëse `enabled`, `suspended` përndryshe); `email` vetëm për owner-a me ≥ 1 `EmailDomain` të verifikuar ose `Subscription`; vlera ekzistuese të cakut mbartën kur kolona ekziston. Heuristika e email-it raportohet për rishikim njerëzor (s'e shpik).
3. **Enterprise:** `off` → `shadow` (llogarit të dyja, zbaton `AccountPlan`, regjistron mospërputhje) → `enforce` (përdor snapshot; fallback te `AccountPlan` kur s'ka rresht). `enforce` vetëm kur raporti i krahasimit = 0 mospërputhje per kanal.
4. **Pa mbishkrim shkatërrues:** `AccountPlan` s'preket deri në M13; rollback = `SMS_CP_ENTITLEMENTS=off|shadow`.

## 13. Matrica e dështimeve
| Dështimi | Sjellja e pritur | Rikuperimi | Alarm |
|---|---|---|---|
| Central i padisponueshëm | Enterprise dërgon me gjendjen e fundit të mirë (fail-static); poller përsërit me backoff | vazhdon vetvetiu | lag i feed-it > 5 min |
| Enterprise i rënë | Central s'ka gjendje per target; ngjarjet mbeten në outbox | në ngritje: feed nga kursori + snapshot | — |
| Timeout rrjeti | kërkesa dështon, kursori s'përparon; backoff | përsëritje | numërues gabimesh |
| Ngjarje e dërguar dy herë | `revision` e njëjtë → no-op | — | — |
| Ngjarje jashtë rendi | `revision` më e vogël → injorohet | rikonsilimi korrigjon | metrikë "stale" |
| Enterprise refuzon kontratë të pavlefshme/`schema` e panjohur | ngjarja karantinohet (jo e aplikuar), kursori përparon ose ndalon sipas politikës "ndal-në-të-panjohur" | rregullo kontratën/versionin, rikonsilim | alarm i menjëhershëm (rrezik prishjeje) |
| Çelësi i auth i rrotulluar | kërkesat me `kid` të vjetër → 401 pas dritares; çelësi i ri ekziston paraprakisht | dy `kid` aktivë gjatë kalimit | 401 të vazhdueshme |
| Rollout i pjesshëm (Central i ri, Enterprise i vjetër) | Enterprise injoron `schema` të panjohur pa aplikuar; asgjë s'prishet | përditëso Enterprise | alarm versioni |
| Mospërputhje e versionit të skemës | `cp.v1` e pa-ndryshueshme; version i ri = lloj i ri me opt-in | përputhje dy-drejtimëshe e dokumentuar | — |
| Central ndryshoi gjatë ndërprerjes | snapshot/feed sjell gjendjen më të re; suspendimet aplikohen të parat | rikonsilim në nisje | lag i rikuperuar |
| Restore i Central (revision regres) | epoka e re → Enterprise ndalon aplikimin | rikonsilim manual (bump i revision-eve) | alarm i menjëhershëm |
| Hendek `seq` / mbivendosje | aplikim sipas `revision` (idempotent) | rikonsilimi | — |

## 14. Politika e konsistencës dhe e vjetërsisë
| Gjendja | Klasa | Buxheti | Dështimi |
|---|---|---|---|
| Suspendim Enterprise; suspendim assignment; **ulje** cakut | **zbatim i shpejtë** | p95 ≤ 60 s; alarm 5 min; SLO i fortë 15 min | break-glass lokal (çelësat ekzistues) + alarm; **pa auto-suspend nga vjetërsia** (shmang ndërprerje të përgjithshme kur Central bie) |
| Aktivizim; assignment i ri; **rritje** caku | eventuale | ≤ 15 min | pa alarm para 30 min |
| Emër/etiketë | eventuale | orë | — |
| Mospërputhje e vazhdueshme / revision regres | **rikonsilim manual** | — | alarm + procedurë |
Kuptimi: vjetërsia është e kufizuar nga poll + rikonsilim; politika e përgjithshme është **last-known-good + alarm** (si korrigjimi #7 i planit). Rreziku i pranuar: gjatë një ndërprerjeje të gjatë të Central, një suspendim s'zbatohet; zbutja është break-glass dhe alarmi.

## 15. Caku i shpejtësisë (rivlerësim)
Pasi snapshot-i ekziston: `EnterpriseProduct.rate_limit_per_min` (Central, kolonë e vetme, NULL = parazgjedhja e Enterprise) → `sms_entitlements.rate_limit_per_min` (kolonë e vetme per assignment; kuptimi sipas kanalit të produktit) → konsumohet në submit me rregullin e kanalit (§3.1). **Nuk** `sms_…`/`email_…` në të njëjtin rresht. Email përfshihet **sepse** modeli i ri e ka semantikën e qartë (email s'lexon më `AccountPlan`). Implementimi i kolonës në Central vjen **vetëm** pas M7-b/d (në M7-g), me backfill nga `AccountPlan`.

## 16. Plani i fazuar i M7 (çdo fazë me approval)
| Faza | Përmbajtja | Ndikon Enterprise? |
|---|---|---|
| **M7-b1** | Central: `revision` + `sync_outbox` + shkrim transaksional në service; testet e atomicitetit; pa rrjet, pa endpoint | jo |
| M7-b2 | Kontratat `cp.v1` + golden fixtures; ekstraktim `packages/contracts` me re-export | jo (vetëm paketim) |
| M7-c | Central: `service_credentials`, auth i klientit (EdDSA), `GET /internal/sync/changes|snapshot`, CLI çelësash | jo |
| M7-d | Enterprise: migrim `sms_entitlements`, `cp_revision`, `sms_cp_cursor`; applier i pastër (pa HTTP); fallback; flamuri `off` | po (shtesë, `off`) |
| M7-e | Enterprise: worker `sync` (poll + rikonsilim), metrika/alarme; `shadow` | po (pa ndryshim sjelljeje) |
| M7-f | `bootstrap_assignments` + raporti i krahasimit shadow | jo (lexim) |
| M7-g | `enforce` në submit SMS/email me fallback + matje SQL/latence (≤ 5%); aktivizim `rate_limit_per_min` | po |
| M7-h (opsionale) | `nudge` push; JWS e përgjigjes | — |
| M13 | heq fushat e vjetra të `AccountPlan` | — |

## 17. Hapi i parë minimal i implementimit
**M7-b1 (vetëm Central, shtesë, pa Enterprise, pa rrjet):** migrim `0007` me `revision BIGINT NOT NULL DEFAULT 1` te `enterprises` dhe `enterprise_products` + tabela `sync_outbox` (skema §10); service-t ekzistuese rrisin `revision` dhe shkruajnë një rresht outbox me gjendjen e plotë në **të njëjtin transaksion** (vetëm kur ka ndryshim real); testet: atomicitet/rollback, no-op pa ngjarje, revision monoton, UNIQUE `(entity, revision)`, PG garë (dy ndryshime paralele → revision të ndryshme), `compare_metadata` 0 diff. Asnjë endpoint, worker, kontratë e shpërndarë, token.

## Pyetje të hapura për vendim
1. Transporti: pull-first (rekomanduar) vs push-first.
2. Rregulli i kanalit me shumë produkte (§3.1) pranohet si parazgjedhje?
3. Politika fail-static pa auto-suspend (§14) pranohet, me break-glass lokal?
4. `Enterprise.name` → `short_name` në Enterprise?
5. Heuristika e assignment-it email në bootstrap (§12.2) kërkon rishikim njerëzor.

---
## 18. Vendimet e miratuara dhe amendamenti i `seq` (M7-b1)
**Të miratuara:** pull-first + rikonsilim · agregimi i kanalit V1 (kanali i lejuar nëse ≥ 1 entitlement `enabled`; caku efektiv = minimumi i vlerave jo-NULL të entitlement-eve `enabled`; NULL → parazgjedhja lokale) · fail-static (pa auto-suspend nga vjetërsia; monitorim + alarm; break-glass lokal) · `Enterprise.name` → `sms_enterprises.short_name` (metadata eventuale, jo `legal_name`) · heuristika e email-it në bootstrap kërkon rishikim njerëzor para apply.

**Amendament i detyrueshëm — numërues global transaksional (jo sekuencë/identity):** sekuencat e PostgreSQL nuk janë transaksionale dhe nuk garantojnë rendin e commit-it (Tx A `seq 100` pa commit, Tx B `seq 101` commit, konsumatori avancon te 101, A commit-ohet më vonë → humbet 100). Overlap/rikonsilim mbeten mbrojtje shtesë, jo garanci korrektësie.

### Implementimi M7-b1 (vetëm Central, migrimi `0007`)
- `sync_sequence(id=1 singleton, last_seq BIGINT)`; alokimi: `UPDATE … SET last_seq = last_seq + 1 … RETURNING` mbi rreshtin e kyçur (`SELECT … FOR UPDATE`) në **të njëjtin transaksion** me ndryshimin. Transaksioni tjetër që do `seq` pret deri në commit/rollback të të parit → `seq` N bëhet i dukshëm para N+1, pa `seq` të ulët që mbërrin vonë. Rollback heq njëkohësisht ndryshimin, `revision`, rritjen e numëruesit dhe outbox-in. Mekanizëm më i thjeshtë me të njëjtën garanci nuk u gjet (advisory lock do të kërkonte pikërisht të njëjtën serializim, pa integritet transaksional të numëruesit).
- **Rendi i kyçjeve (pa deadlock):** numëruesi global FILLON, pastaj rreshti i entitetit. Rrjedha e një ndryshimi real: pre-kontroll pa kyç (no-op → dil, **pa kyç, pa `seq`**) → kyç numëruesin → rilexo entitetin `FOR UPDATE` → rishiko → ndrysho + `revision += 1` → rrit numëruesin → shto outbox. Ndryshimet e njëkohshme të të njëjtit entitet serializohen (pa update të humbur); të entiteteve të ndryshme serializohen vetëm te numëruesi (volumi administrativ është i ulët).
- **`revision`:** `BIGINT NOT NULL DEFAULT 1` te `enterprises` dhe `enterprise_products`; rregulli i vetëm: **gjendja ekzistuese para M7 = revision 1** (migrimi nuk krijon ngjarje historike: migrim ≠ replay; snapshot-i M7-c/d e mbulon); ndryshim real `+1`; no-op: asnjë ndryshim. Disiplina e ORM: fushat e ndjekura (`name`/`status`, `status`) ndryshojnë vetëm me `revision += 1` të saktë (`RevisionError` përndryshe).
- **`sync_outbox`:** `seq` (PK, i alokuar, jo identity) · `event_id` UUID unik e i qëndrueshëm · `enterprise_id` FK RESTRICT · `entity_type` · `entity_id` · `revision` · `event_type` (**emra të përkohshëm** `enterprise.upserted`, `enterprise_product.upserted`; finalizohen te M7-b2) · `payload` JSON · `created_at`; UNIQUE `(entity_type, entity_id, revision)`; indeks `(enterprise_id, seq)`; vetëm-shtim (ORM). Pa `delivered_at`, `attempts`, target ose gjendje retry (Central është burim pull).
- **Payload = snapshot i ngrirë** në çastin e ndryshimit (jo vetëm id): enterprise `{enterprise_id, name, status}`; assignment `{assignment_id, enterprise_id, product:{id, code, channel}, status}` (`code`/`channel` të pandryshueshme → të sigurta). Gjendja mund të ndryshojë sërish para leximit të feed-it; ngjarja mban gjendjen e atij çasti.
- **Çfarë emeton:** `Enterprise` create/rename/suspend/activate; `EnterpriseProduct` assign/suspend/activate. **Jo:** ndryshimet e `Product` (nuk është entitet sync), suspendimi i Enterprise nuk gjeneron ngjarje për assignment-et (dy lifecycle të ndarë).
- **Audit ≠ outbox:** audit = kush ndryshoi çfarë; outbox = gjendja që konsumatori duhet të shohë; të dyja në të njëjtin tx të API-së (dështimi i audit-it rollback-on edhe outbox-in, provuar).
- **Retention:** synim ≥ 30 ditë; pa worker pastrimi ende; feed/snapshot (M7-c/d) do zbulojë kursor më të vjetër se retention dhe do kërkojë snapshot të plotë.
- **Pranim i dokumentuar:** mjeti i bootstrap-it M4-c (ORM direkt) krijon enterprise me `revision = 1` pa ngjarje outbox (mbulohet nga snapshot); import pas go-live kërkon rikonsilim.
- **Rregull shkruesish:** çdo shkrues i këtyre entiteteve kalon nga service-t (`services/sync.py`); SQL i drejtpërdrejtë anashkalon disiplinën.
- Pa endpoint, kredenciale shërbimi, kontratë të shpërndarë, tabela/applier në Enterprise, worker.

### Scalability boundary (V1) dhe M7-b2
- **Global sync lock: i pranuar për V1** (volum i ulët i shkrimeve të control plane). Nëse throughput i shkrimeve bëhet realisht pengesë, arkitektura e sekuencimit mund të rishikohet, por **korrektësia (rendi i commit-it të `seq`) nuk sakrifikohet**.
- **M7-b2:** kontrata `cp.v1` në `packages/contracts/control_plane/` (leaf, stdlib-only), mapper `sync_outbox → cp.v1 → bytes`, golden; shih `docs/CONTROL_PLANE_CONTRACT_V1.md`. Emrat e event-eve mbeten `enterprise.upserted` / `enterprise_product.upserted` (pa migrim).

---
## 19. M7-c — sipërfaqja e sync-ut të Central dhe auth shërbim-te-shërbim (zbatuar; vetëm Central)

### Auth (Ed25519 client assertion)
- **Header:** `alg=EdDSA`, `kid`. **Claims të detyrueshme:** `iss` = `sub` = `client_id` · `aud` = `sms-central-sync` · `iat` · `exp` (**`exp − iat ≤ 300 s`**) · `jti` (8..64 karaktere) · `scope` (hapësirë-ndarë; duhet të përmbajë `sync:read`). Leeway ±5 s. Algoritmi fiks (`none`, HS*, RS* refuzohen); token-at e stafit (HS256) nuk pranohen.
- **Rrjedha në Central:** header+`iss` (pa besim) → kërkim `(client_id, kid)` → klienti dhe çelësi `active` → verifikim EdDSA + `aud` + `exp` + claims të detyrueshme → `sub == iss` → lifetime → scope (token **dhe** klienti) → konsumim `jti`.
- **Gabime:** çdo dështim identiteti/nënshkrimi/claims/replay/skadimi → **401 gjenerik identik** (`unauthorized`; pa zbulim të regjistrit); scope mungon → **403**; arsyeja e saktë vetëm në log (`client`, `kid`, `reason`, IP; asnjëherë token/nënshkrim/çelës).
- **Skema:** `service_clients` (`client_id` unik, `status`, `scopes`, `auth_generation`) · `service_keys` (`client_pk`, `kid`, `public_key` PEM Ed25519, `status`; UNIQUE `(client_pk, kid)`) · `service_client_enterprises` (objektivat) · `service_assertion_jti` (PK `(client_pk, jti)`, `expires_at`). **Vetëm çelësa publikë**; privati s'hyn kurrë në Central (CLI refuzon çelës privat/jo-Ed25519).
- **Rotacion:** disa `kid` aktivë për një klient; shtohet çelësi i ri, kalohet thirrësi, `disable-key` për të vjetrin.
- **Replay:** `jti` ruhet pas verifikimit të plotë, në **transaksion të veçantë që mbetet edhe kur kërkesa dështon më vonë**; pastrimi i rreshtave të skaduar bëhet lazy në çdo shkrim (pa worker). Trade-off: një shkrim DB per kërkesë (sync në ~30 s: i papërfillshëm), funksionon mes proceseve/instancave pa Redis; tabela rritet maksimumi ≈ kërkesa në 5 min.
- **Scope-e:** vetëm `sync:read`; feed dhe snapshot e kërkojnë.
- **CLI (manual):** `create_service_credential` (klient+çelës publik+objektiva; idempotent; kid i njëjtë me çelës tjetër → konflikt) dhe `service_credential_admin` (`grant`, `revoke`, `disable-key`, `disable-client`). Çifti i çelësave gjenerohet jashtë: `openssl genpkey -algorithm ed25519 -out k.pem && openssl pkey -in k.pem -pubout -out k.pub.pem`.

### Objektivat dhe `auth_generation`
Një klient lexon vetëm enterprise-et e dhëna (`service_client_enterprises`; pa supozim single-tenant). **Problemi:** kursori `seq` është global; nëse bashkësia zgjerohet më vonë, kapërcimi i `seq` të enterprise-it të ri do të humbiste historinë e tij. **Politika:** `auth_generation` rritet te çdo ndryshim real (create me N enterprise, grant, revoke; no-op jo). Feed **kërkon** `generation`; mospërputhje → **409 `sync_authorization_changed`** → konsumatori bën snapshot të plotë (që e sjell gjendjen e tanishme të enterprise-it të ri) dhe vazhdon nga `snapshot_seq`. Revoke: snapshot-i s'e përfshin më (fshirja lokale është vendim i Enterprise).

### Feed: `GET /internal/sync/changes?after_seq=&epoch=&generation=&limit=` (1..500)
Përgjigje: `{"epoch","authorization_generation","events":[cp.v1…],"next_seq","latest_seq","oldest_available_seq","has_more"}` (wrapper i Central API, jo kontratë e përbashkët). Ngjarjet vijnë nga `sync_outbox` + mapper `cp.v1` (kurrë nga tabelat e biznesit), `seq ASC`, vetëm enterprise-et e autorizuara; pa mutacion, pa ack, pa gjendje konsumatori.
- **Kursori:** kthen `seq` në `(after_seq, latest_seq]`; `latest_seq` lexohet një herë (vlera e commit-uar e numëruesit; çdo `seq` ≤ saj është i commit-uar sepse numëruesi dhe outbox-i ndryshojnë në të njëjtin commit, në rend). Pa nevojë për overlap për korrektësi (konsumatori mund ta përdorë si mbrojtje shtesë).
- **`next_seq`:** nëse `has_more` → `seq` i fundit i kthyer; përndryshe `latest_seq` (kapërcimi i `seq` të tenant-eve të tjerë është i sigurt për shkak të generation/epoch; **devijim i shënuar** nga formulimi "highest seq returned / mbetet after_seq": një konsumator i qetë do të mbetej përgjithmonë "prapa" `latest_seq` kur tenant-et e tjerë ndryshojnë, duke prishur monitorimin e vjetërsisë dhe duke rishkanuar boshllëqet). Nuk lëviz kurrë mbrapsht. **Kontratë e dokumentuar (e miratuar): konsumatori M7-d/e duhet ta respektojë EKZAKT** — kursori lokal vendoset te `next_seq` i mbështjellësit (jo te seq-i i fundit i ngjarjeve), faqja bosh e përparon kursorin, boshllëqet e `seq` (p.sh. 100,107,129) pranohen.
- **Epoka:** UUID i persistuar te `sync_sequence` (gjeneruar një herë në migrim, jo nga procesi); i qëndrueshëm në restart; ndryshon vetëm me `rotate_epoch` (procedurë eksplicite për restore/reseed). Mospërputhje → **409 `sync_epoch_mismatch`**.
- **Retention/kursor i vjetër:** `sync_sequence.floor_seq` = kursori më i vogël i shërbyeshëm me histori të plotë (0 sot; pastrimi i ardhshëm e rrit). `after_seq < floor_seq` → **410 `sync_cursor_expired`** (pa histori të pjesshme, pa `events`); `after_seq > latest_seq` → **409 `sync_cursor_ahead`**; të gjitha me `action: "snapshot"`. `oldest_available_seq = floor_seq + 1`. Pa worker pastrimi.
- **Rregull i konsumatorit:** fillo gjithmonë me snapshot (feed pa `epoch`/`generation` të marrë nga snapshot s'thirret dot; `after_seq=0` pa snapshot do humbiste gjendjen para-M7 pa ngjarje).

### Snapshot: `GET /internal/sync/snapshot[?enterprise_id=]`
Përgjigje: `{"epoch","authorization_generation","snapshot_seq","enterprises":[{entity,enterprise_id,revision,data}],"assignments":[…]}`; `data` = `EnterpriseStateV1`/`EnterpriseProductStateV1` (me `product{id,code,channel}`); pa katalog produktesh, users, audit; vetëm enterprise-et e autorizuara; `enterprise_id` i paautorizuar → **403**. Pa faqosje në V1 (borxh).
**Konsistenca:** transaksion i vetëm **`REPEATABLE READ` vetëm-lexim** (PostgreSQL). Kufiri (`last_seq`) lexohet i pari, pastaj gjendja, në të njëjtin snapshot të DB-së. Numëruesi dhe rreshtat e entiteteve ndryshojnë gjithmonë në të njëjtin commit, ndaj snapshot-i i sheh të dyja në të njëjtin çast: gjendja = të gjitha ndryshimet me `seq ≤ snapshot_seq`, asnjë më shumë; `generation` dhe bashkësia lexohen po aty. Handoff: snapshot → kursor = `snapshot_seq` → feed `after_seq=snapshot_seq` pa humbje; zbatimi sipas `revision`.
**Provat PG:** (1) ndryshim që commit-ohet mes leximit të kufirit dhe gjendjes → snapshot e përjashton plotësisht dhe feed e sjell; (2) kontroll negativ: nën READ COMMITTED gjendja do përmbante ndryshimin me `snapshot_seq` të vjetër (gjysmë-konsistente), pra testi është i ndjeshëm; (3) tx me `seq` të alokuar pa commit gjatë snapshot-it → jashtë snapshot-it, brenda feed-it pas commit-it; (4) 4 shkrues paralelë (48 ndryshime) gjatë snapshot-it → snapshot + feed = gjendja finale, revision +1 pa boshllëk.

### Sjellja në dështim
Central poshtë → konsumatori vazhdon fail-static; auth i dështuar → 401/403 pa efekt; epoch/generation/kursor → 409/410 me `action: snapshot`; kërkesa e dështuar nuk e liron `jti` (replay mbetet i refuzuar).

### Borxhi i sigurisë (para go-live)
Pa limitim kërkesash për auth (kërkon `limit_req` në proxy; s'ka Redis); TLS/proxy nuk ekzistojnë ende; ndryshimet e kredencialeve me CLI nuk shkruhen te `audit_log` (pa aktor); pa JWS të përgjigjes (vetëm TLS); snapshot pa faqosje; çelësat publikë s'kanë datë skadimi/rotacion të detyruar; leeway 5 s mbështetet te ora e Central.


## 20. M7-d — snapshot + aplikues lokal i Control Plane në Enterprise (zbatuar; vetëm gjendje lokale)
**Jashtë shtrirjes (M7-e/g):** HTTP client, polling worker, çelës shërbimi/JWT, scheduler, shadow comparison, enforcement në SMS/Email, `rate_limit_per_min`. Asnjë ndryshim në Central.

**Skema (migrim 0020, aditiv):** `sms_enterprises.cp_revision BIGINT NOT NULL DEFAULT 0` (0 = s'ka gjendje autoritative; rreshtat ekzistues mbeten 0) dhe `short_name` 64→200 (emri Central ≤200; zgjerim metadata në PG) · `sms_entitlements` (`assignment_id` UNIQUE, `UNIQUE(enterprise_id, product_code)`, FK te `sms_enterprises`, `status` active|suspended|withdrawn, `revision`; pa `owner_ref`) · `sms_cp_cursor` singleton (`epoch`/`authorization_generation` NULL + `last_seq` 0 = kërkohet snapshot; pa sekrete). `enabled` **nuk ruhet**: `entitlement_enabled(enterprise_status, entitlement_status)` = enterprise `active` DHE assignment `active` (pa cache të derivuar). AccountPlan i paprekur (break-glass lokal mbetet i pavarur; policy e ardhshme = Central AND jo-deny lokal).

**Kodi:** `app/services/control_plane_sync.py` (importon `packages.contracts.control_plane.v1`; pa HTTP). `parse_snapshot`/`parse_events` (valido gjithçka para aplikimit; skemë/tip i panjohur ⇒ `ContractError`, s'anashkalohet asgjë) · `apply_snapshot` · `apply_feed_batch` · `apply_event` (primitiv, pa kursor). Funksionet punojnë në transaksionin e thirrësit, NUK bëjnë commit, dhe izolohen me savepoint (dështim ⇒ asgjë, as kursori).

**Snapshot:** fushat e Central: `status`, `short_name`←`name`, `cp_revision`; kurrë `legal_name`/`owner_ref`/faturim. Epoka e njëjtë: rregulli i `revision` (përsëritje = no-op; më i ri përditëson; `snapshot_seq` < kursor ⇒ `StaleSnapshot`). **Epokë e re** (restore i Central): zëvendësim pa kontroll revision (revision-et mund të rinisin) dhe kursori rinis. **Entitlement që mungon** (enterprise në snapshot, assignment jo): `withdrawn` (kurrë fshirje; rishfaqja e rikthen; asnjë tombstone i shpikur). Enterprise jashtë snapshot-it ose që s'ekziston lokalisht: i paprekur / i kaluar (`unknown_enterprise`), asnjëherë krijim tenant-i; snapshot-i i ardhshëm e rimerr.
**Ngjarje:** `revision` > lokal ⇒ aplikon; == ⇒ no-op; < ⇒ `stale` (e raportuar). `seq` s'përdoret kurrë për freskinë. Konflikt identiteti (i njëjti `product_code` me assignment tjetër, assignment që ndërron enterprise) ⇒ `ApplyError`.
**Kursori:** epoch NULL ⇒ `SnapshotRequired("no_snapshot")`; epoch/generation ndryshe ⇒ `SnapshotRequired("epoch_mismatch"|"generation_mismatch")` (pa reset automatik, pa aplikim). `apply_feed_batch(next_seq=…)`: `next_seq` nga mbështjellësi, seq-et rriten rreptësisht, > kursor, ≤ `next_seq`, `next_seq` ≥ kursor, përndryshe `ApplyError` (kursori s'lëviz). Retry i një faqeje të commit-uar refuzohet eksplicit (thirrësi rilexon kursorin).
**Konkurrenca:** kursori kyçet i pari (`FOR UPDATE`), pastaj enterprise, pastaj entitlement; leximet me `populate_existing`. Kursori serializon çdo aplikim ⇒ rev5 e vonuar nuk e ul kurrë rev6.
**Audit:** `control_plane.enterprise.apply`, `control_plane.entitlement.apply`, `control_plane.snapshot.apply` me aktor `system:control_plane_sync`, role `system` (`audit.system_event`); vetëm për ndryshime reale; asnjë JWT/çelës.
**Paketim:** `Dockerfile` kopjon `packages/` (API dhe workers janë i njëjti imazh); testi simulon shtresën e imazhit (vetëm COPY-t, pa PYTHONPATH, pa `apps/`).
**Borxh për M7-e/g:** transport + kredenciale + polling/riprovim; `rate_limit_per_min`; enforcement dhe shadow; monitorim vjetërsie (`last_success_at`); politika e enterprise të panjohur lokalisht (sot: kalohet, jo dështim); fshirja/arkivimi i entitlement-eve `withdrawn`.

## 21. M7-e — klient pull + poller + shadow (Enterprise; pa enforcement)
**Jashtë shtrirjes:** enforcement SMS/Email, `rate_limit_per_min`, mutim i AccountPlan (M7-f/g). Asnjë ndryshim në Central.

**Konfigurimi** (`SMS_CP_*`): `SYNC_MODE` = `off`|`shadow` (pa `enforce`), `BASE_URL`, `CLIENT_ID`, `KEY_ID`, `PRIVATE_KEY_PATH` (vetëm skedar/mount sekret), `POLL_INTERVAL_SECONDS` (30), `REQUEST_TIMEOUT_SECONDS` (10), `SNAPSHOT_INTERVAL_SECONDS` (3600, ≥300). Prodhim: `BASE_URL` duhet `https://`. Valido në nisje të poller-it: URL/client/kid të pranishëm; çelësi duhet PEM Ed25519 PRIVAT, i paenkriptuar (publik-only, RSA, i enkriptuar, bosh ⇒ `ConfigError`, mesazh pa përmbajtje çelësi). Asnjë çelës i gjeneruar; asnjë çelës në DB/log/`repr`.
**Assertion:** `alg=EdDSA`, `kid`; `iss`=`sub`=client_id, `aud`=`sms-central-sync`, `iat`, `exp`=iat+120 (≤300), `jti`=uuid4 hex, `scope`=`sync:read`; NJË i ri për çdo kërkesë HTTP.
**Moduli:** `control_plane_client` (vetëm HTTP: `get_snapshot`, `get_changes`; pa SQL/modele/AccountPlan; 401→`CpAuthError`, 403→`CpForbidden`, 409/410 me kod snapshot→`CpSnapshotRequired`, rrjet/timeout/5xx/429→`CpTransportError`, tjetër→`CpProtocolError`) · `control_plane_poller` (orkestrim) · `control_plane_sync` (aplikues) · `control_plane_shadow`.

**Rrjedha (`poll_once`):** epoch NULL ⇒ snapshot i plotë PARA feed-it; feed me kursorin lokal, çdo faqe `apply_feed_batch(next_seq=përgjigje)` + commit (`has_more` ⇒ faqja tjetër, ≤50/iteracion); 409 (`sync_epoch_mismatch`, `sync_authorization_changed`, `sync_cursor_ahead`) ose 410 (`sync_cursor_expired`) ose `SnapshotRequired` lokal ⇒ snapshot i plotë (pa reset automatik) pastaj feed nga `snapshot_seq` (max NJË snapshot i detyruar/iteracion). Pa riprovim brenda iteracionit; kthen `PollOutcome`.
**Rakordim periodik (KORREKTËSI, jo kujdes):** çdo `SNAPSHOT_INTERVAL` (1 orë) bëhet snapshot i plotë edhe kur feed-i punon. Arsye: (1) ngjarjet për enterprise të panjohur lokalisht kalohen dhe kursori përparon — vetëm snapshot-i i plotë i popullon kur enterprise-i krijohet më vonë; (2) mbrojtje nga drift/korrupsion operacional; (3) ndryshime të fushës së autorizimit. Kosto: një SELECT i plotë në Central/orë (snapshot pa faqosje = borxh i njohur); orari lexohet nga `sms_cp_cursor.last_snapshot_at`, pra mbijeton rinisjen.
**Snapshot i plotë vs i pjesshëm:** `apply_snapshot(full_scope=True)` (parazgjedhje; snapshot pa `?enterprise_id`) lëviz kursorin dhe rakordon fushën; `full_scope=False` (`?enterprise_id=`) aplikon vetëm entitetet e tij, NUK prek kursorin, NUK nxjerr përfundime për të tjerët, kërkon epoch/generation të njëjta.
**Dalja nga fusha e autorizimit (V1, pa kolonë të re):** snapshot FULL (pas ndryshimit të generation) pa një enterprise me `cp_revision` > 0 ⇒ entitlement-et e tij aktive/pezull bëhen `withdrawn` (audit `out_of_authorization_scope`): s'konsiderohen më autoritet aktual i Central. NUK fshihet `sms_enterprises` as historiku i entitlement-eve; `status`/`cp_revision` mbeten gjendja e fundit e njohur; shadow e klasifikon `cp_withdrawn`; rikthimi në fushë i rikthen. Enterprise me `cp_revision`=0 (kurrë i menaxhuar) nuk preket. Pa efekt trafiku (shadow). Pse pa kolonë: `withdrawn` + `cp_revision>0` mjafton për të dalluar "i menaxhuar më parë, jo më i autorizuar" nga "kurrë i menaxhuar".
**Retry/backoff (`run_loop`):** dështim ⇒ 1,2,4,8,16,30,30… s × jitter 0.5–1.0; 403 ⇒ 300 s (jo agresiv); suksesi kthen te intervali. Alarm `ALERT` në log pas 5 dështimeve radhazi. Central poshtë ⇒ gjendja lokale mbetet, `last_success_at` s'lëviz, asgjë s'çaktivizohet (fail-static).
**Vjetërsia:** `last_success_at` vendoset vetëm nga aplikuesi pas apply të suksesshëm (jo nga HTTP 200). `sync_age_seconds()`; `check_staleness` logon WARNING >300 s dhe ERROR >900 s (SLO); kurrë auto-disable.
**Singleton:** `PollerLock` = kyç advisory PostgreSQL i nivelit sesion mbi lidhje të dedikuar AUTOCOMMIT (`idle_in_transaction_session_timeout` s'e vret); lirohet kur lidhja mbyllet/proçesi vdes; humbja zbulohet (`held()`) dhe rimerret para poll-it. Nuk është kusht korrektësie (aplikuesi serializon kursorin): vetëm shmang kërkesat e dyfishta. Një kyç për DB. SQLite (dev): pa koordinim. Worker: `python -m app.worker --role control_plane [--once]` (SIGTERM/SIGINT ⇒ mbyllje e hijshme; `off` ⇒ proces boshe; konfigurim i gabuar ⇒ dalje 2). Compose prod: shërbim opsional `cp-sync` (`--profile control-plane`).
**Shadow:** në `messages.submit` dhe `emails.submit`, pas leximit të AccountPlan: `observe(plan, owner, kanal, legacy_allowed)` (vetëm kur `shadow`). CP = `Enterprise.status` + entitlement i kanalit (V1: kanali aktiv nëse ≥1 entitlement aktiv). Klasa: `legacy_allow_cp_allow|legacy_allow_cp_deny|legacy_deny_cp_allow|legacy_deny_cp_deny|cp_missing|cp_withdrawn|cp_stale` (stale = vjetërsi > SLO 900 s). Rendi: missing→withdrawn→stale→4 kombinimet. Numërues në-proçes (`shadow.stats.snapshot()`; s'ka infrastrukturë metrics), përmbledhje në log çdo ≥5 min, log detaji ≤1/10 min për (enterprise, kanal, klasë). Cache për-proces 30 s ⇒ ≈0 SQL shtesë (1 SELECT në savepoint kur humbet); gabimet përlahen; rezultati i submit-it kurrë s'ndryshon. AccountPlan nuk lexohet më shumë se sot (kalohet `enabled` si bool).
**Borxh për M7-f/g:** bootstrap i assignment-eve nga AccountPlan (M7-f); enforcement + `rate_limit_per_min` (M7-g); shadow vetëm në submit SMS/Email (jo kampanjat/console); numëruesit janë për-proçes (API vs worker), pa eksport metrics; snapshot pa faqosje; ndryshimi i kredencialeve të shërbimit ende jashtë audit-it; asnjë dashboard/alarm i jashtëm (vetëm log).
