# M11-I/F-07 — Runbook i canary-t dhe i cutover-it të provider-it/rrugës

**NUK ekzekutohet kurrë nga ky repo.** F-07A = canary sintetik lokal (provuar). **F-07B = canary real + cutover = BLOCKED** deri sa të ekzistojnë infrastrukturë, kredenciale dhe miratim i miratuar nga njeriu. Asgjë këtu nuk bën një cutover real, nuk ndryshon rrugëzimin e kompanisë, nuk dërgon trafik real.

## 0. Mekanizmat ekzistues që përdoren (nuk shpiket asnjë)
- Rrugët: `PUT /v1/admin/routes` (kërkon 2FA): `prefix`, `provider`, `priority`, `enabled`. Rruga fituese = prefiksi më i gjatë, pastaj prioriteti më i lartë (`find_route`).
- Kill switch-et: `submit` (pranimi) dhe `dispatch` (dërgimi nga worker-i), lexohen nga DB pa cache.
- Mënyrat e autoritetit (para/sender/çmim): `local → shadow → central` me readiness/ACK (M9-g4, M10-S5; `scripts/sender_cutover.py`). **Nuk janë mënyra të provider-it**; provider-i ndërrohet vetëm me rrugë.
- Mjetet e reja vetëm-lexim (F-07A): `python -m scripts.provider_readiness check|rollback-plan`, `python -m scripts.provider_canary`.

## 1. F-07A — çfarë provon canary-i sintetik (lokal)
`python -m scripts.provider_canary [--json]` (DB SQLite e përkohshme që fshihet; soketat bllokohen dhe numërohen; `assert_disposable` refuzon çdo DB tjetër). Provon 50+ kontrolle: submit → payload i provider-it → SENT me id → DLR sukses → DELIVERED + capture një herë; DLR dështim → FAILED + release; refuzim i përhershëm/retry i përkohshëm; timeout i paqartë dhe «pranuar por përgjigjja humbi» ⇒ UNKNOWN pa ridërgim (worker + sweeper) dhe zgjidhje nga DLR me `reference`; DLR i dyfishtë (një capture, çdo callback i audituar); DLR para SENT (503 pastaj 200), DLR i vonuar kontradiktor (409, pa regres, pa para); firmë e gabuar/mungon (401), trup i keq (422), id e panjohur (503 + receipt); dukshmëria (`/v1/admin/stats`, `/v1/admin/providers`, `queue_readiness`); idempotenca financiare (ledger verifikohet); adapterët HTTP/Twilio mbi `httpx.MockTransport`. Kodi i daljes 0 = gjithçka kaloi.
**Kufiri:** ky NUK është canary real. Nuk provon arritshmërinë e callback-ut, provider-in, DNS/TLS, sender-in real, saldon, apo dorëzimin real.

## 2. PRE-CUTOVER (F-07B — NUK ekzekutohet këtu)
Çdo hap duhet evidencë të ruajtur (shih `m11i-provider-evidence-checklist.md`). Vendimi bëhet nga një njeri i emëruar (**NUK ËSHTË CAKTUAR**).

| # | Kontrolli | Si matet | Klasifikimi |
|---|---|---|---|
| 1 | Kredencialet e provider-it valide | `scripts.twilio_smoke` **pa** `--confirm` (plan) + konfirmim i pronarit të sekreteve | PRODUCTION-ONLY |
| 2 | Sender/shteti i miratuar | sender i miratuar (local/central) për shtetin e canary-t; `sender_*_readiness` PASS | PRODUCTION-ONLY (miratimi real) |
| 3 | Saldo/kredia e mjaftueshme (platformë + provider) | `wallets.balances` i enterprise-it të canary-t ≥ koha e pritur; saldo e provider-it e konfirmuar | PRODUCTION-ONLY |
| 4 | Radha e shëndetshme | `python -m scripts.queue_readiness` pa FAIL; `provider_readiness check --phase pre` = GO | VERIFIED (mjeti) |
| 5 | Worker i shëndetshëm | healthcheck `scripts.worker_health`, `ops_freshness` (AL-23) | VERIFIED (mjeti) |
| 6 | Endpoint-i DLR i arritshëm nga provider-i | POST i nënshkruar nga jashtë rrjetit te `https://<domen>/webhooks/dlr/<provider>` kthen 2xx; i pa-nënshkruar 401 | PRODUCTION-ONLY |
| 7 | Sekreti i firmës i konfigurur | `provider_readiness`: `callback_auth` PASS (vetëm emrat, jo vlerat) | VERIFIED (kontrolli); vlera EXTERNAL |
| 8 | Alarmimi funksionon | një alarm i provokuar arrin pranuesin (AL-06/07/13/24) | PRODUCTION-ONLY |
| 9 | Backup/recovery të shëndetshëm | F-05B (provë PITR reale) — **BLOCKED** | PRODUCTION-ONLY |
| 10 | Rruga e rikthimit e njohur | rruga e mëparshme (prefiksi, provider-i, prioriteti) e shkruar; `provider_readiness check` për të = GO | VERIFIED (mjeti) |

## 3. CANARY (F-07B — NUK ekzekutohet këtu)
1. Enterprise i veçantë i canary-t, sender i miratuar, destinacione **të njohura dhe të miratuara** (numra të kontrolluar nga ne), volum shumë i vogël (p.sh. 5–20 mesazhe; **jo përqindje e trafikut të klientëve**).
2. Rruga e provider-it e hapur **vetëm për prefiksin e canary-t** me prioritet më të lartë se rruga e mëparshme; `provider_readiness check --phase pre` ⇒ GO (përfshin `route_wins_precedence`).
3. Dërgo me çelësa idempotence të rinj. Mos përdor retry manual të UNKNOWN.
4. Vëzhgo: çdo mesazh arrin DELIVERED; DLR vjen; `python -m scripts.provider_readiness check --phase post --hours 1 --min-sample <n> --strict`; faturimi/rakordimi i saktë (balancë = çmim × dorëzuar; `scripts.verify_ledger`); panel i provider-it: një kërkesë për mesazh (**pa submit të dyfishtë**); asnjë rritje backlog-u.

## 4. GO / NO-GO (kritere të matshme, POLICY PROPOSAL)

| Kriteri | GO | NO-GO |
|---|---|---|
| Mesazhe canary të dorëzuara | 100% e mesazheve të canary-t DELIVERED brenda pragut të miratuar të DLR (propozim ≤ 15 min) | çdo FAILED i pashpjegueshëm ose DLR që s'vjen |
| UNKNOWN | 0 | ≥ 1 (hetohet; asnjë rritje volumi) |
| Dublikime | 0 submit të dyfishtë te paneli i provider-it; `duplicate_provider_ids` PASS | ≥ 1 |
| Para | `verify_ledger` OK; capture = dorëzuar; asnjë hold i varur | çdo mospërputhje |
| DLR | `dlr_problems` të shpjeguara; 401/422 vetëm në testet e qëllimshme | `conflict`/`unknown_message` të pashpjeguara |
| Radha | `queue_backlog` PASS; `queue_readiness` PASS | backlog në rritje |
| Alarme | asnjë P1/P2 provider/DLR aktiv | alarm aktiv |
Rritja e volumit pas GO është një vendim i veçantë njerëzor, me të njëjtat kritere në çdo hap.

## 5. ROLLBACK (procedurë; provuar lokalisht si helper vetëm-lexim)
`python -m scripts.provider_readiness rollback-plan --provider <emri> [--json]` printon rrugët e aktivizuara, kill switch-et, mesazhet në ajër sipas gjendjes dhe `must_not_resend` (= UNKNOWN), pa ndryshuar asgjë. Hapat:
1. **Ndal trafikun e ri të canary-t:** çaktivizo rrugën e provider-it (`enabled=false`) — dhe/ose pauzo `submit`.
2. **ATENCION (gjetje G-02, provuar):** çaktivizimi i rrugës **nuk i ridrejton mesazhet që janë tashmë QUEUED**: ato mbeten të lidhura me provider-in e tyre dhe do të dërgohen nëse `dispatch` është aktiv dhe provider-i i regjistruar. Për t'i mbajtur: **pauzo `dispatch`** (mesazhet rrinë QUEUED, paratë mbahen; rifillimi i dërgon). Nëse provider-i hiqet nga konfigurimi, mesazhet e radhitura dështojnë përfundimisht me `unknown_provider` + release (jo UNKNOWN).
3. **Rikthe rrugën/provider-in e mëparshëm:** rihap rrugën e mëparshme me prioritet më të lartë; `provider_readiness check` për të ⇒ GO; rifillo `dispatch`.
4. **Drenazho/inspekto in-flight:** QUEUED të vjetrat NUK ridrejtohen dhe **nuk ka API anulimi për mesazh të vetëm** (vetëm anulim fushate dhe pauzë llogarie): ose lihen të dalin përmes provider-it ende të regjistruar, ose mbahen me `dispatch` të pauzuar derisa vendos njeriu (një mjet masiv anulimi/ridrejtimi është propozim i veçantë, jo i zbatuar); SENT presin DLR (ose `dlr_timeout` 72 h); SENDING i ngecur trajtohet nga sweeper-i.
5. **Ruaj rezultatet e panjohura; mos i ridërgo:** UNKNOWN zgjidhet vetëm pas rakordimit me panelin (resolve/attach) ose me DLR autoritativ.
6. **Verifiko rakordimin:** `queue_readiness` PASS, `verify_ledger`, lista UNKNOWN e shqyrtuar, `provider_readiness check --phase post`.
7. **Dokumento incidentin:** JSON-et e readiness para/pas, zgjidhjet e UNKNOWN, përmbledhja e `DlrReceipt`, koha dhe vendimmarrësi.

## 6. Çfarë mbetet F-07B (nuk simulohet)
Llogari/kredenciale reale të provider-it, URL publik + DNS + TLS real, sender i miratuar realisht, numra destinacioni realë, saldo reale e provider-it, dorëzim real i jashtëm, latenca/formati real i DLR, sjellja reale e Twilio/vendorit (kufij shpejtësie, kode gabimi), alarmim real dhe on-call i emëruar. **Asnjë test lokal, as ky canary, nuk është provë prodhimi.**
