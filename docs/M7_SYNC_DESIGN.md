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
