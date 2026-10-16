# M11-I/F-07A — Provider, DLR dhe callback: kontrata, makina e gjendjeve, operimi

**Statusi:** F-07A = LOCAL/STAGING-GRADE (provider të rremë, transport i simuluar, pa rrjet). **F-07B (provider real, URL publik, dorëzim real) = BLOCKED / PRODUCTION-ONLY** — asgjë këtu nuk është provë prodhimi. Prodhimi mbetet **BLOCKED**.
Çdo pohim më poshtë vjen nga kodi aktual (`app/providers/*`, `app/services/messages.py`, `app/services/emails.py`, `app/api/webhooks.py`, `app/api/twilio.py`, `app/api/public.py`) dhe provohet nga `tests/test_f07_provider_dlr.py`, `tests/test_f07_canary.py` dhe testet ekzistuese (`test_dlr`, `test_twilio`, `test_provider_http`, `test_m9a_unknown_outcome`, …). Nuk është shpikur asnjë sjellje e re.

## 1. Kanalet e zbatuara (dhe ato që NUK ekzistojnë)

| Kanali | Adapterë | Statusi |
|---|---|---|
| SMS | `fake` (test), `http` (JSON gjenerik, emër i konfigurueshëm), `twilio` | të zbatuar |
| Email | `fake`, `smtp` (STARTTLS/SSL) | të zbatuar |
| **Voice / Flashcall** | — | **MISSING: nuk ekziston asnjë kod** (kërkimi `flashcall|voice` te `app/`, `apps/`, `scripts/` nuk gjen kanal; përputhjet janë «invoice»). Nuk shpikim kanal; çdo kërkesë për thirrje kërkon fazë të veçantë. |
| Pagesa | `fake` + webhook `/webhooks/payments/{provider}` | jashtë këtij dokumenti (M9) |

## 2. Matrica e kontratës së provider-it

Legjenda: **VERIFIED** (kod + test lokal) · **PARTIAL** (kod ekziston, mbulim/gjendje e pjesshme) · **MISSING** · **PRODUCTION-ONLY** (kërkon provider/URL/kredenciale reale).

| Fusha | `http` (SMS) | `twilio` (SMS) | `smtp` (email) | `fake` |
|---|---|---|---|---|
| Moduli | `providers/http.py` | `providers/twilio.py` | `providers/email.py` | `providers/fake.py`, `email.py` |
| Submit | `POST {SMS_HTTP_PROVIDER_URL}` JSON | `POST /Accounts/{sid}/Messages.json` form | SMTP `sendmail` (RFC 5322 i nënshkruar) | në memorie |
| Autentikimi | `Authorization: Bearer <SMS_HTTP_PROVIDER_KEY>` | Basic (`SMS_TWILIO_ACCOUNT_SID`/`AUTH_TOKEN`) | SMTP user/pass (`SMS_SMTP_*`) | — |
| Timeout | `SMS_HTTP_PROVIDER_TIMEOUT`=10 s | `SMS_TWILIO_TIMEOUT`=10 s | 15 s | — |
| Retry | përmes radhës: 5 përpjekje, 30·2^(n−1) s (30/60/120/240) | njësoj | njësoj | njësoj |
| Idempotencë | `idempotent_by_reference=False` (dërgon `reference`, s'është provuar dedup) | `False` (Twilio s'ka çelës) | `False` | `True` (provuar nga kodi) |
| Provider message id | `{"id": …}` (i detyrueshëm) | `sid` (i detyrueshëm) | `Message-ID` i ndërtuar nga platforma | `fake-N` |
| Rezultate sinkrone | sukses / i përkohshëm definitiv / i përkohshëm i paqartë / i përhershëm | + `failed|undelivered|canceled` në 2xx ⇒ i përhershëm | sukses / 4xx / 5xx / i paqartë | 0001 temp, 0002 reject |
| Callback/DLR asinkron | `POST /webhooks/dlr/{provider}` | `POST /webhooks/twilio/status` | `POST /webhooks/email/{provider}` | si http |
| Verifikimi i firmës | HMAC-SHA256 i trupit, `X-Signature: sha256=<hex>` (`SMS_DLR_SECRETS[provider]`) | `X-Twilio-Signature` (HMAC-SHA1 mbi URL publike + parametra) | HMAC-SHA256 si SMS | — |
| Gjendje terminale | DELIVERED, FAILED | DELIVERED, FAILED | DELIVERED, BOUNCED, COMPLAINED, FAILED | — |
| Klasat e riprovueshme | 408/425/429 (definitive), 5xx/rrjet pas lidhjes (të paqarta) | 429/502/503/504, 401/403 (konfigurim), connect | SMTP 4xx, connect | `fake_temporary` |
| Klasat jo-riprovueshme | çdo 4xx tjetër | 4xx, `failed` status, kodet Twilio | SMTP 5xx, recipient refused 5xx | `fake_rejected` |
| Rezultat i panjohur | 5xx, read-timeout, reset, 2xx i palexueshëm/pa id ⇒ **UNKNOWN** | 500, read-timeout, 2xx pa `sid` ⇒ **UNKNOWN** | prerje pas lidhjes ⇒ **UNKNOWN** | — |
| Kufizim shpejtësie | vetëm për llogari (600/min default, `AccountPlan.rate_limit_per_min`); **asnjë throttling për provider** | njësoj | njësoj (`email_rate_limit_per_min`) | — |
| Observabilitet | `/v1/admin/providers`, `/v1/admin/stats`, `queue_readiness`, `provider_readiness`, `DlrReceipt` | njësoj | `/v1/admin/stats` (UNKNOWN); **pa receipts për email** | — |
| Klasifikimi | kod: VERIFIED; provider real: PRODUCTION-ONLY | kod: VERIFIED; **nuk është provuar kurrë kundër Twilio real** (docs/TWILIO.md) ⇒ PRODUCTION-ONLY | kod: VERIFIED; SMTP real PRODUCTION-ONLY | VERIFIED |

Gjetje të matricës (nuk ndryshojnë sjelljen; shih §8): provider që kthen **id të dyfishtë** nuk refuzohet te SENT (PARTIAL: zbulohet nga `provider_readiness`); **nuk ka fallback automatik** midis rrugëve; **nuk ka throttling për provider**; **DlrReceipt pa retention**; **email pa receipts**; **nuk ka mbrojtje replay me kohë** (por idempotenca e gjendjes e bën të padëmshëm).

## 3. Pipeline-i i dërgimit (si funksionon)

`submit` (idempotent sipas `(owner, idempotency_key)`) → kontrollet **para parave**: kill switch `submit` → plan aktiv/entitlement → rate-limit llogarie → E.164 → **rruga** (`find_route`: prefiks më i gjatë, pastaj prioriteti më i lartë) → **sender/shtet** (`sender_authority.assert_outbound_authority`) → pëlqimi → çmimi → portofoli. Pastaj, **në të njëjtin transaksion**: hold (`wallets.reserve`) + `Message` QUEUED (outbox). Asnjë provider nuk thirret këtu.

`process_one` (worker): kill switch `dispatch` → claim `FOR UPDATE SKIP LOCKED` (QUEUED→SENDING, `attempts+1`) → **COMMIT#1** → rikontrolli i sender-it (M10-S5) → `dispatch_started_at` → **COMMIT#1b** → **`provider.send` pa transaksion DB** → finalizim me `FOR UPDATE` dhe verifikim që claim-i është ende i yni → **COMMIT#2**. Provider-i thirret vetëm nga `messages.process_one` dhe `emails.process_one` (testuar: `test_provider_send_is_only_invoked_from_the_two_worker_functions`).

Rregulla për rezultatin (`dispatch_outcome.settle_error`): definitiv ⇒ retry/FAILED; **i paqartë ⇒ UNKNOWN** (hold i pandryshuar, pa ridërgim) përveç nëse provider-i është provuar idempotent dhe ka përpjekje (retry me të njëjtën `reference` = `public_id`). Sweeper-i (`recover_stuck`, lease `SMS_SENDING_LEASE_SECONDS`=600 s) klasifikon SENDING të ngecur sipas fazës; `expire_stale` mbyll SENT pa DLR pas `SMS_DLR_TIMEOUT_HOURS`=72 h (FAILED `dlr_timeout` + release).

## 4. Makina e gjendjeve SMS (nga `models/sending.py` + `messages.py`)

```
QUEUED ──claim──▶ SENDING ──pranuar──▶ SENT ──DLR delivered──▶ DELIVERED   (capture, një herë)
   ▲                │  │                 │ └─DLR failed / dlr_timeout──▶ FAILED   (release, një herë)
   └─ retry/backoff ┘  ├─ i përhershëm / mbaruan përpjekjet / sender revokuar ─▶ FAILED (release)
                       └─ rezultat i paqartë ─▶ UNKNOWN ──DLR autoritativ ose stafi──▶ DELIVERED | FAILED
```

Tranzicionet e ligjshme (`TRANSITIONS`): QUEUED→{SENDING,FAILED}; SENDING→{SENT,QUEUED,FAILED,UNKNOWN}; SENT→{DELIVERED,FAILED}; UNKNOWN→{DELIVERED,FAILED}; DELIVERED, FAILED = terminale (asnjë dalje). `_move` hedh `Conflict` për çdo tranzicion tjetër.

Sjellja e callback-ut (`apply_dlr` + endpoint-et), e provuar:

| Rasti | Rezultati | Prova |
|---|---|---|
| DLR delivered/failed për SENT | tranzicion + capture/release një herë | `test_dlr`, kanari |
| Terminal i njëjtë (dublikat/replay) | 200, asnjë efekt i dytë (`m.status == target` ⇒ return) | `test_replayed_signed_callback…`, kanari |
| Terminal i kundërt (i vonuar/stale) | `Conflict` ⇒ **409**, gjendja s'regres (DELIVERED mbetet DELIVERED), receipt `conflict` | kanari, `test_replayed…` |
| DLR para se SENT të jetë commit-uar / id e panjohur | `NotFound` ⇒ **503** (provider-i riprovon), receipt `unknown_message`, asgjë s'krijohet | kanari |
| DLR për UNKNOWN (me id ose me `reference`) | zgjidhet vetë, audit `system:dlr_reconciliation`; id e ndryshme ⇒ Conflict | `test_m9a_*` |
| Firmë e gabuar/mungon/sekret i pakonfiguruar | **401**, asnjë gjendje, asnjë receipt | kanari, `test_dlr` |
| Trup i keq (JSON/skemë) me firmë të vlefshme | **422**, asnjë gjendje | kanari |
| DLR pas `dlr_timeout` (delivered) | 409; klienti nuk tarifohet (politikë e dokumentuar) | `test_late_dlr_after_expiry…` |
| Twilio: status i ndërmjetëm (`queued/sent/sending`) | 200 TwiML bosh, injorohet | `test_twilio` |
| Twilio: `delivered`→delivered; `failed|undelivered|canceled`→failed me `twilio_<ErrorCode>` | si më sipër | `test_twilio` |

**Jashtë rradhe:** deterministik — shih rreshtat 503/409. **Replay:** HMAC mbi trup pa kohë/nonce ⇒ një callback i vjetër i nënshkruar pranohet sërish teknikisht, por është idempotent (asnjë tranzicion/para; vetëm një rresht `DlrReceipt` i ri). Ky është kompromis i njohur (§8, G-05).

### Email (`emails.apply_event`)
Ngjarjet: `delivered`, `bounce_hard`, `bounce_soft` (vetëm shënohet), `complaint`. Tranzicionet (`EMAIL_TRANSITIONS`): SENT→{DELIVERED,BOUNCED,COMPLAINED,FAILED}; DELIVERED→{COMPLAINED,BOUNCED}; UNKNOWN→{SENT,FAILED,DELIVERED,BOUNCED,COMPLAINED}. Bounce i ashpër/complaint regjistron opt-out «hard». Ngjarje e kundërt pas terminalit ⇒ 409; id e panjohur ⇒ 503; firmë e gabuar ⇒ 401 (`test_email_webhook`). **Email nuk ka `DlrReceipt`** (G-06).

## 5. Sekretet dhe siguria e callback-eve

- Sekretet vijnë vetëm nga mjedisi (`SMS_HTTP_PROVIDER_KEY`, `SMS_DLR_SECRETS`, `SMS_TWILIO_*`, `SMS_SMTP_*`); nisja në prodhim refuzon sekret DLR të dobët (<24 kar. ose placeholder) për provider-in HTTP aktiv dhe Twilio gjysmë-konfigurim (F-03).
- Krahasimet e firmave përdorin `hmac.compare_digest` (HMAC dhe Twilio); token/sekret bosh ⇒ refuzim (fail closed). Endpoint-i **nuk beson** `X-Forwarded-*`, `X-Admin-Key` apo `Authorization`: vetëm firma (testuar).
- Gabimet e provider-it kalojnë si **kod i shkurtër** (`http_503`, `twilio_21211`, `network:ReadTimeout`); trupi i përgjigjes, kokat dhe çelësat nuk futen te `error_code`, `MessageEvent`, `DlrReceipt` apo log-et (provuar me trup që pasqyron çelësin).
- `DlrReceipt.raw_body` ruan trupin e callback-ut siç mbërriti (jo kokat/firmën). Për Twilio trupi përmban `To/From` (numra telefoni = PII): pranim i dokumentuar (G-07).
- Tooling real-traffic: `scripts/twilio_smoke.py` dërgon SMS të vërtetë me `--confirm` — **kurrë në F-07A**.

## 6. Operimi

| Situata | Veprim |
|---|---|
| Mesazhe `UNKNOWN` | **Asnjëherë ridërgim.** `python -m scripts.queue_readiness`; rakordo me panelin e provider-it; zgjidh me `POST /v1/admin/queue/{sms|email}/{public_id}/resolve` (`billable_delivered`/`non_billable_failed`) ose lidh id me `…/provider-id`. Një DLR autoritativ e mbyll vetë. |
| `dlr_problems_24h` rritet (`conflict`, `unknown_message`) | `conflict`: DLR kontradiktor/vonë — shiko `DlrReceipt`; `unknown_message` i përsëritur: id e panjohur ose SENT i pa-commit-uar (pritet pak; shumë ⇒ hetim). |
| SENT pa DLR | `provider_readiness check --phase post` (`sent_without_dlr`); pas 72 h mbyllen automatikisht FAILED. |
| Kredenciale të refuzuara (`twilio_auth`, `http_401/403`) | P1: Twilio riprovon 5× pastaj FAILED+release për **çdo** mesazh; HTTP 401/403 = FAILED menjëherë. Pauzo `dispatch`, rregullo kredencialet. |
| Rikthim | `docs/m11i-canary-cutover-runbook.md` §5 dhe `python -m scripts.provider_readiness rollback-plan`. |

Mjetet vetëm-lexim (F-07A): `python -m scripts.provider_canary` (canary sintetik), `python -m scripts.provider_readiness check|rollback-plan`.

## 7. Klasifikimi përfundimtar

| Zona | Klasifikimi |
|---|---|
| Kontrata e adapterëve, taksonomia e dështimeve, UNKNOWN, idempotenca financiare, firma/401/422, jashtë-rradhe/replay | **VERIFIED** (lokalisht, provider të rremë/transport i simuluar) |
| Id e dyfishtë e provider-it, throttling për provider, fallback rruge, retention i `DlrReceipt`, receipts email, replay me kohë | **PARTIAL / MISSING** — shih §8 |
| Voice/Flashcall | **MISSING** (kanal i pazbatuar) |
| Provider real, URL publik/DNS/TLS, sender real, numra real, saldo, dorëzim real, latenca DLR reale, sjellja reale e Twilio | **PRODUCTION-ONLY (F-07B)** |

## 8. Gjetje dhe boshllëqe (pa ndryshuar kodin; propozime për miratim)

| ID | Gjetja | Efekti | Propozim |
|---|---|---|---|
| G-01 | Provider që kthen id të dyfishtë: `sent` hook s'e refuzon; DLR zgjidh një rresht arbitrar | rrezik i ulët (gabim i provider-it); i zbulueshëm | refuzo/UNKNOWN në `sent` nëse `(provider, id)` ekziston (kërkon indeks unik + migrim) — **vendim i veçantë** |
| G-02 | Nuk ka fallback automatik midis rrugëve; mesazhi lidhet me provider-in në submit | `PUT routes enabled=false` **NUK i ridrejton** mesazhet e radhitura (provuar) | procedurë: pauzo `dispatch`; **nuk ka API anulimi për mesazh të vetëm** (vetëm fushatë/pauzë llogarie) — mjet masiv ridrejtimi/anulimi = propozim i veçantë |
| G-03 | Nuk ka throttling për provider (vetëm për llogari) | provider real mund të kufizojë (429 ⇒ retry me backoff) | vlerësim në F-07B |
| G-04 | 401/403 Twilio riprovohen 5× pastaj FAILED për çdo mesazh | dështim masiv me kredenciale të gabuara | alarm P1 (AL-24); nuk ndryshohet semantika |
| G-05 | Callback pa kohë/nonce | replay i padëmshëm për gjendjen, rrit `DlrReceipt` | opsionale: timestamp te kontrata e provider-it (varet nga vendori) |
| G-06 | Email pa `DlrReceipt` | pa gjurmë të ngjarjeve të refuzuara | propozim: receipt për email |
| G-07 | `DlrReceipt` pa retention dhe me PII (Twilio) | rritje + PII në tabelë | propozim: retention (si events) |
| G-08 | `docs/TWILIO.md` §4 pohonte që `twilio_outcome_unknown` kthen paratë | **dokument i vjetruar** pas M9-a | **korrigjuar në F-07A** (UNKNOWN, hold mbahet) |
