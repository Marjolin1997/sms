# M10 Sender Policy V1 — arkitektura, runbook-ët dhe operimi

Ky dokument është i vetmi vend operacional për cutover-in e sender policy. Kodi është i plotë; **prodhimi kërkon prova të jashtme** (shih fundin).

## 1. Arkitektura V1 (një shikim)
```
Enterprise SenderId (objekt lokal / klienti)         Central (autoriteti i politikës)
  request/resubmit ──outbox atomik (S3)──► POST /internal/sender/requests (sender:report)
                                              registry + vendime + politika (S1)
  projeksion i ndarë  ◄── cp.sender.v1 (S2: feed + snapshot, sender:read) ──┘
  fasada e autoritetit (S4) ─ local | shadow | central ─► messages.submit / campaigns
  process_one ─ rikontrolli para provider-it (S5, vetëm mesazhe të autorizuara nga Central)
```
`SenderId.status` NUK pasqyron kurrë Central. Autoriteti në `central` lexon vetëm projeksionin lokal (pa HTTP).

## 2. Modet e autoritetit (`SMS_SENDER_AUTHORITY`)
| Mod | Vendos | Rishikimi lokal | Lexime shtesë në submit | Efekt te klienti |
|---|---|---|---|---|
| `local` (parazgjedhje) | S0 lokal | i lejuar | 0 | asnjë |
| `shadow` | lokali; projeksioni krahasohet | i lejuar | +2 SELECT (+INSERT krahasimi për mospërputhje/mostër) | asnjë |
| `central` | projeksioni i sinkronizuar | **i ngrirë** (409 `sender_authority_frozen`) | −1 +2 SELECT | statusi efektiv nga Central |

Fail-static: projeksioni i vjetër NUK mohon. Mungesa e provës (nuk ka rresht të miratuar) mohon. Staleness raportohet (`projection_stale`) dhe e bën **gatishmërinë operacionale** FAIL, jo autorizimin.

## 3. Rikontrolli para dispatch-it (vendimi B) dhe gara e revokimit
`process_one`: claim → COMMIT#1 → **rikontroll** → `dispatch_started_at` → COMMIT#1b → provider.send → finalizim.
- Rikontrolli aplikohet vetëm te mesazhe me `sender_authority_source='central'` (provenanca e ngrirë), vetëm lexime lokale.
- **Bllokon** (mesazhi → `FAILED`, `error_code` ∈ `sender_revoked|sender_rejected|sender_pending|sender_policy_denied`, hold-i lirohet, provider-i NUK thirret, pa provider id): revoked/rejected eksplicit; pending kur politika kërkon miratim; politikë `allowed=false` — me kusht që projeksioni të jetë jo më i vjetër se rishikimi i ngrirë në mesazh.
- **NUK bllokon**: projeksion i vjetër sipas moshës; sync i ndërprerë; rresht që mungon/tërhequr; projeksion më i vjetër se provenanca; mesazh historik (pa provenancë) ose me provenancë lokale.
- Parat: dështimi ndodh para `dispatch_started_at`, kështu hold-i është ende ACTIVE → `release` i M9 (idempotent), asnjë capture, asnjë mint; `verify_wallet` kalon.
- Terminal: `FAILED` nuk rimerret nga `claim_next` dhe nuk preket nga `recover_stuck` (pa cikël riprovimesh).
- **Gara e mbetur (e dokumentuar, e pazgjidhur qëllimisht):** revokimi që hyn në projeksion PAS leximit të rikontrollit dhe para pranimit nga provider-i nuk pengohet. Dritarja = nga leximi i rikontrollit deri te pranimi i provider-it (COMMIT#1b + latenca e rrjetit). Pasi provider.send nis nuk ka anulim të garantuar; rezultati ndjek rregullat M9 (UNKNOWN/idempotencë). Nuk përdoret kyç i shpërndarë mbi thirrjen e provider-it. Çdo mesazh i RI refuzohet që nga submit.

## 4. Runbook — bootstrap
1. `python -m scripts.sender_bootstrap export --out senders.json --source-revision <rev>` (vetëm-lexim).
2. `python -m apps.central.tools.sender_import --artifact senders.json --json > dry.json` (dry-run); shqyrto kategoritë.
3. Zgjidh gjetjet: korrigjo (politikë/identitet/çelës), ose vendim operatori: `python -m scripts.sender_bootstrap resolve --sender-id N --category <c> --resolution accepted_not_migrated|sender_deactivated --actor <vetë> --reason "<arsye>"`. Zgjidhja = "e kuptuar dhe e pranuar"; **nuk e miraton sender-in dhe nuk e anashkalon politikën Central**. `missing_in_central` s'zgjidhet me deklaratë (importo).
4. `python -m apps.central.tools.sender_import --artifact senders.json --apply --actor-email <admin> --ack-artifact-hash <hash i dry-run> [--batch-size N] [--enterprise-id U]`.
5. Prit sinkronizimin S2, pastaj `python -m scripts.sender_bootstrap reconcile --central-report apply.json --record`. Duhet `unresolved=0`.

## 5. Runbook — shadow
`SMS_SENDER_AUTHORITY=shadow` (me `SENDER_SYNC_ENABLED=true`, `SENDER_REQUEST_REPORTING=true`). Provat: `python -m scripts.sender_policy_readiness --target central --central-readiness central.json --json`. Kritere (pa numër ditësh të shpikur): mostra ≥ `SMS_SENDER_EVIDENCE_MIN_SAMPLES` që nga përfundimi i bootstrap-it (ose dritarja `SMS_SENDER_EVIDENCE_WINDOW_HOURS`), drift kritik = 0, çdo sender lokal i miratuar ka projeksion të miratuar (ose çështje e pranuar), sync i shëndetshëm, backlog i kërkesave i shëndetshëm, bootstrap i plotë.
Ashpërsia: **critical** = `local_allow_central_deny` (+ `central_missing|pending|rejected|revoked`, `policy_mismatch`, `sender_identity_mismatch`); **warning** = `local_deny_central_allow`; **info** = përputhje, `projection_stale`.

## 6. Runbook — cutover (25 hapa)
PRECHECK: (1) deploy S5 me `authority=local`; (2) migrime (`alembic upgrade head`; Central `0029`); (3) S2 sync i shëndetshëm; (4) S3 reporter i shëndetshëm; (5) bootstrap dry-run; (6) zgjidh gjetjet; (7) bootstrap apply; (8) `reconcile --record`.
SHADOW: (9) `authority=shadow`; (10) mblidh provat; (11) drift kritik = 0; (12) backlog sync/kërkesa; (13) kontrollo `GET /v1/sender-ids` (fushat efektive).
CUTOVER: (14) `sender_policy_readiness --target central` pa FAIL; (15) `python -m scripts.sender_cutover evidence --actor <vetë> --code-revision <rev> --central-readiness central.json` → regjistron provën `pre_cutover` të pandryshueshme dhe printon `evidence_hash`; (16) **ACK i operatorit** = `SMS_SENDER_AUTHORITY_ACK=<evidence_hash>`; (17) `SMS_SENDER_AUTHORITY=central` (+ `SMS_SENDER_DISPATCH_RECHECK=true`), restart; (18) verifiko ngrirjen (approve/reject/revoke → 409); (19) canary: `python -m scripts.sender_cutover canary --owner-ref <tenant-test> --country AL --sender <i miratuar> --to <numër-test> --send --key canary-1` (rregullat normale, pa përjashtim); (20) verifiko provider/DLR/provenancën/faturimin e mesazhit; (21) ndiq alertat.
POST: (22) pa mohime të papritura (`sender_central_deny_ratio`); (23) rikontrolli: provo me sender të revokuar në staging; (24) sythi kërkesë→Central→projeksion (ridërgim i testit); (25) `python -m scripts.sender_cutover complete --actor <vetë> --ref-hash <evidence_hash> --canary-ref <public_id>` → prova `post_cutover`.
ACK është i lidhur me versionin e autoritetit, mjedisin dhe hash-in e bootstrap-it: ndryshimi i provës së bootstrap-it (`reconcile --record` i ri) e bën ACK-un e vjetër të pavlefshëm. Mos e ri-regjistro bootstrap-in pas cutover-it pa prova të reja.

## 7. Runbook — rikthim
- **central → shadow**: gjithmonë i mundur. Vendos `SMS_SENDER_AUTHORITY=shadow`; lokali kthehet autoritet, Central dhe projeksioni nuk preken, rishikimi lokal rihapet.
- **central → local**: `python -m scripts.sender_cutover rollback --to local`. FAIL kur një sender i miratuar lokalisht është refuzuar/revokuar nga Central (do të ri-autorizohej). Rruga e sigurt: kalo në shadow, `rollback --to local --reconcile-local --actor <vetë>` (revokon lokalisht ato sender-a), pastaj local. Divergjencë e pranuar: `--accept-divergence --actor <vetë> --reason "<arsye>"` (regjistrohet prova `rollback_ack`).
- Asnjë fshirje/rishkrim i historisë Central; veprimet e mbetura të paarritshme (mesazhe `central`) mbajnë provenancën.

## 8. Alertat (`python -m scripts.sender_alerts --json`, kod 1 për critical)
`sender_sync_lag`, `sender_sync_age_seconds`, `sender_sync_gap_recoveries_total`, `sender_request_oldest_age_seconds`, `sender_request_permanent_failures`, `sender_bootstrap_unresolved`, `sender_shadow_critical_drift`, `sender_central_deny_ratio` (numërues në proces), `sender_dispatch_recheck_blocked_1h`, `sender_projection_stale`, `sender_policy_readiness_fail`. Pa label me vlera sender.

## 9. Interpretimi i gatishmërisë (`sender_policy_readiness`)
FAIL = bllokon cutover/operim central. WARN = shqyrto (p.sh. `central_readiness` nuk u dha, `expected_drift`). PASS = të gjitha pragjet. Staleness e sync-ut jep FAIL operacional por NUK revokon asnjë sender.

## 10. Troubleshooting
- Mohime të reja të papritura në central → `sender_central_deny_ratio`; kontrollo `scope_sender_read`, kursorin, `GET /v1/sender-ids` (admin: `drift`, `central_status`).
- Kërkesë e mbetur → `sender_request_oldest_age_seconds`; `scripts.sender_request_readiness`; 403 `enterprise_not_authorized` = klienti pa enterprise.
- Mesazh `FAILED sender_*` → rikontrolli bllokoi; hold-i u lirua; klienti ridërgon senderin.
- ACK i pavlefshëm → `python -m scripts.sender_cutover status`.

## 11. Bllokuesit e jashtëm të prodhimit (JO të kodit)
Kodi M10 është i plotë. Pa këto prova nuk mund të thuhet "cutover i kryer": (1) dritare reale shadow me trafik prodhimi; (2) bootstrap në të dhëna prodhimi; (3) canary me provider/DLR real; (4) monitorim/alertim i konfiguruar në prodhim; (5) backup/PITR dhe kredencialet/çelësat operacionalë (fazat e mëparshme); (6) provë staging e gares së revokimit.
