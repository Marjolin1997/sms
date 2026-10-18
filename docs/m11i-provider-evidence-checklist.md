# M11-I/F-07 — Checklist i evidencës së provider-it, DLR dhe canary-t

Dy pjesë të ndara. **A = evidencë lokale/staging-grade (e mbyllur me F-07A, rishtrohet në çdo ndryshim).** **B = evidencë reale (BLOCKED; asnjë rresht nuk plotësohet me simulim).** Pronarët janë role të propozuara — **NUK ËSHTË CAKTUAR asnjë person**.

## A. F-07A — evidenca lokale (provider të rremë / transport i simuluar; pa rrjet)

| # | Pohimi | Komanda / prova | Pritet | Statusi |
|---|---|---|---|---|
| A-01 | Canary sintetik kalon plotësisht | `python -m scripts.provider_canary --json` | `ok:true`, `network_attempts:0`, `cleaned_up:true` | VERIFIED LOKALISHT |
| A-02 | Harness-i refuzon DB jo të hedhshme | `pytest tests/test_f07_canary.py -k disposable` | 7 raste | VERIFIED |
| A-03 | Taksonomia e dështimeve HTTP/Twilio/SMTP | `pytest tests/test_f07_provider_dlr.py -k taxonomy` | të gjitha rastet kalojnë | VERIFIED |
| A-04 | Rezultati i provider-it → gjendje + para, 1 thirrje | `pytest … -k provider_outcome_maps` | 8 raste | VERIFIED |
| A-05 | Retry/backoff/shterim | `pytest … -k retry_schedule` | 30/60/120/240 s; FAILED + release pas 5 | VERIFIED |
| A-06 | UNKNOWN s'ridërgohet kurrë (ndryshim rruge, sweeper, worker) | `pytest … -k unknown_outcome_is_never_resent` + `tests/test_m9a_unknown_outcome.py` | 1 thirrje | VERIFIED |
| A-07 | DLR i dyfishtë/replay/jashtë rradhe/stale | `pytest … -k replayed` + canary | pa regres, pa para të dyfishta | VERIFIED |
| A-08 | Firma: e gabuar/mungon ⇒ 401; e keqe ⇒ 422; headers s'e zëvendësojnë | `pytest … -k "signature or forwarded"` | fail closed | VERIFIED |
| A-09 | Sekretet s'rrjedhin (error_code, events, log, repr, receipts) | `pytest … -k secrets` | asnjë çelës | VERIFIED |
| A-10 | Sender/shtet/rrugë/rate-limit para parave dhe provider-it | `pytest … -k "unapproved_sender or rate_limit"` | 0 mesazhe, 0 hold, 0 thirrje | VERIFIED |
| A-11 | Nuk ka fallback automatik; çaktivizimi i rrugës s'ridrejton QUEUED | `pytest … -k route_fallback` | provuar (G-02) | VERIFIED (sjellje e dokumentuar) |
| A-12 | Kill switch `dispatch` pauzon pa prekur gjendjen/paratë | `pytest … -k dispatch_switch` | QUEUED, attempts 0 | VERIFIED |
| A-13 | Gate-i GO/NO-GO (pre/post) + vetëm-lexim + pa rrjedhje | `pytest tests/test_f07_canary.py` | NO-GO me arsyen e emërtuar | VERIFIED |
| A-14 | Plani i rikthimit (`must_not_resend`) pa mutacion | `pytest … -k rollback_plan` | UNKNOWN i numëruar, i paprekur | VERIFIED |
| A-15 | Idempotenca financiare në çdo rrugë zgjidhjeje | `pytest … -k money_moves_once` | një capture, ledger i verifikuar | VERIFIED |
| A-16 | Regresioni i provider/DLR/radhës ekzistues | `pytest tests/test_dlr.py tests/test_twilio.py tests/test_provider_http.py tests/test_m9a_unknown_outcome.py tests/test_dispatch_queue.py tests/test_queue_semantics.py` | të gjelbra | VERIFIED |
| A-17 | Shard-et e CI mbulojnë testet e reja | `python -m scripts.ci_shard_proof` | unioni = suita, prerje bosh | VERIFIED |

## B. F-07B — evidenca reale (BLOCKED)

| # | Evidenca e kërkuar | Pronari (propozim) | Statusi |
|---|---|---|---|
| B-01 | Llogari provider-i + kredenciale në vendin e miratuar të sekreteve | pronari i sekreteve/provider-it | **EXTERNAL — NOT ASSIGNED** |
| B-02 | URL publik HTTPS i arritshëm nga provider-i (DNS + TLS reale), callback i nënshkruar pranuar nga jashtë | infrastruktura | PRODUCTION-ONLY |
| B-03 | Sender i miratuar realisht + lejet gjeografike për shtetin e canary-t | operacione/provider | PRODUCTION-ONLY |
| B-04 | Numra destinacioni realë të kontrolluar nga ne (miratuar me shkrim) | operacione | PRODUCTION-ONLY |
| B-05 | Saldo reale e provider-it dhe e portofolit të canary-t | financa | PRODUCTION-ONLY |
| B-06 | Provë idempotence e provider-it real (nëse pretendohet `idempotent_by_reference=True`) | inxhinieri + provider | PRODUCTION-ONLY |
| B-07 | Canary real sipas `m11i-canary-cutover-runbook.md` §3 me GO te §4 (JSON `provider_readiness` para/pas, paneli i provider-it, `verify_ledger`) | operacione + financa | PRODUCTION-ONLY |
| B-08 | Latenca dhe formati real i DLR; pragu i `dlr_timeout`/alarmit i kalibruar | operacione | PRODUCTION-ONLY |
| B-09 | Alarm i provokuar (AL-06/07/13/24) arrin pranuesin brenda pragut | on-call (jo i emëruar) | PRODUCTION-ONLY |
| B-10 | Prova e rikthimit (rrugë e mëparshme + pauzë `dispatch` + rakordim UNKNOWN) e ushtruar në staging real | operacione | PRODUCTION-ONLY |
| B-11 | F-05B (PITR real) para çdo trafiku real | DBA | **BLOCKED** |

Rregull: këtu s'hyn asgjë «provuar» me provider të rremë ose me një tunel/provider sandbox pa miratim; një provë sandbox (nëse miratohet) etiketohet veçmas dhe nuk mbyll B-07.
