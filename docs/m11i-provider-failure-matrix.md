# M11-I/F-07A — Matrica e dështimeve të provider-it

**Burimi:** vetëm sjellja REALE e kodit, e provuar nga tabelat parametrike te `tests/test_f07_provider_dlr.py` (`HTTP_TABLE`, `TWILIO_TABLE`, `SMTP_TABLE`, `PIPELINE`). Asnjë provider real nuk u thirr; kodet/pragjet e alarmeve janë **POLICY PROPOSAL** (shih `m11i-alert-matrix.md`). Sjellja kundër provider-it real: **PRODUCTION-ONLY (F-07B)**.

**Konventa.** *Definitiv* = provider-i definitivisht s'e pranoi (`ambiguous=False`). *I paqartë* = mund të jetë pranuar (`ambiguous=True`) ⇒ për provider jo-idempotent (HTTP, Twilio, SMTP): **UNKNOWN** — hold i pandryshuar, **kurrë ridërgim automatik**. Retry: `MAX_ATTEMPTS=5`, vonesa `30·2^(n−1)` s = 30/60/120/240 s; pas përpjekjes së 5-të ⇒ FAILED + release (`EXHAUSTED`). Rezultati hartohet në gjendje nga `dispatch_outcome.settle_error`.

## 1. Adapteri HTTP (`providers/http.py`)

| Dështimi | Kodi i gabimit | Retry? | Terminal? | Rezultat i panjohur? | Veprimi i operatorit | Alarm (propozim) |
|---|---|---|---|---|---|---|
| DNS / connect timeout / pool timeout | `network:ConnectError` / `ConnectTimeout` / `PoolTimeout` | po (definitiv, backoff) | pas 5 përpjekjeve FAILED+release | jo | kontrollo DNS/rrjetin e provider-it | P2 në rritje të `delivery_rate`↓ |
| TLS (handshake, certifikatë) | httpx e jep si `ConnectError` ⇒ si më sipër | po | si më sipër | jo | kontrollo certifikatën e provider-it | P2. *PARTIAL: s'u provua veçmas (vetëm `ConnectError`)* |
| HTTP 400, 404, 409, 422 (4xx tjetër) | `http_<kod>` | **jo** | **FAILED + release** | jo | kontrollo payload/URL; 404 = URL gabim, 409 = konflikt | P2 nëse ≥ 3 radhazi |
| HTTP 401 / 403 | `http_401` / `http_403` | **jo** | **FAILED + release për çdo mesazh** (si Twilio nga M11-J) | jo | kredenciale të revokuara/të gabuara: **pauzo `dispatch`**, rrotullo çelësin | **P1** (AL-24) |
| HTTP 408 / 425 / 429 | `http_<kod>` | po (definitiv) | pas 5 përpjekjeve | jo | vonesë natyrale; nëse vazhdon, ul volumin / kontakto provider-in | P2 |
| HTTP 5xx (500/502/503/504) | `http_5xx` | **jo auto** | **UNKNOWN** | **po** | rakordo me panelin, zgjidh UNKNOWN; **mos ridërgo** | P2→P1 sipas moshës së UNKNOWN (AL-07) |
| 2xx me JSON të pavlefshëm / trup i palexueshëm | `bad_response` | jo auto | UNKNOWN | po | si 5xx | AL-07 |
| 2xx pa `id` / `id` bosh / `id` jo-string / trup jo-dict | `bad_response` | jo auto | UNKNOWN | po | si 5xx | AL-07 |
| 3xx | `bad_response` | jo auto | UNKNOWN | po | URL e provider-it ridrejton: korrigjo konfigurimin | P2 |
| Përgjigje e ngadaltë (read timeout) | `network:ReadTimeout` | jo auto | UNKNOWN | po | si 5xx; rrit timeout vetëm me miratim | AL-07 |
| Connection reset / protokoll i prishur | `network:RemoteProtocolError` / `ReadError` | jo auto | UNKNOWN | po | si 5xx | AL-07 |
| Pranuar por përgjigjja humbi | (si read timeout/reset) | jo auto | UNKNOWN; DLR i mëvonshëm me `reference` e zgjidh | po | pret DLR ose zgjidh nga stafi | AL-07 |
| Provider kthen ID të dyfishtë | `duplicate_provider_id` (M11-J) | jo (pa ridërgim) | mesazhi i dytë **UNKNOWN**, hold i mbajtur; indeksi unik mbron edhe garën | **po** | rakordo me panelin; zgjidh UNKNOWN (resolve) — mos ridërgo | P2 (AL-25) |
| Llogari pezulluar / pa saldo | zakonisht 402/403 ⇒ `http_402`/`http_403` | jo | FAILED + release | jo | kontakto provider-in; pauzo `dispatch` | **P1** (si 401/403) |
| Destinacion / sender / vend i refuzuar | 4xx ⇒ `http_4xx` (adapteri gjenerik s'ka kod të veçantë) | jo | FAILED + release | jo | kontrollo rrugën, sender-in, lejet gjeografike | P3 për pak; P2 në rritje |

## 2. Twilio (`providers/twilio.py`)

| Dështimi | Kodi | Retry? | Terminal? | Panjohur? | Veprimi | Alarm |
|---|---|---|---|---|---|---|
| Connect error / timeout para dërgimit | `network:ConnectError` etj. | po (definitiv) | pas 5 përpjekjeve | jo | rrjeti | P2 |
| 401 | `twilio_auth` | **jo (M11-J)** | **FAILED + release menjëherë** (1 thirrje) | jo | konfigurim: rregullo `.env`, pauzo `dispatch` | **P1** (AL-24) |
| 403 | `twilio_<kod>` (kodi numerik nga trupi) ose `twilio_forbidden` | **jo (M11-J)** | FAILED + release menjëherë | jo | leje/politikë: kontrollo llogarinë/lejet; pauzo `dispatch` | **P1** (AL-24) |
| 429, 502, 503, 504 | `http_<kod>` | po (definitiv) | pas 5 | jo | vonesë; ul volumin | P2 |
| 500 | `twilio_outcome_unknown` | **jo** | **UNKNOWN** | **po** | rakordo me Twilio Monitor → Logs; zgjidh | AL-07 |
| Read timeout / reset | `twilio_outcome_unknown` | jo | UNKNOWN | po | si 500 | AL-07 |
| 2xx pa `sid` / JSON i keq | `twilio_outcome_unknown` | jo | UNKNOWN | po | si 500 | AL-07 |
| 400/4xx me kod Twilio (21211 numër i pavlefshëm, 21408 shtet pa leje, 21610 i bllokuar, …) | `twilio_<code>` | **jo** | FAILED + release | jo | numër/shtet/sender; 21408 ⇒ aktivizo Geo permissions | P3; kodet e autentikimit/rajonit `twilio_2xxxx` ⇒ P1 (AL-06) |
| 4xx pa kod (404, 409, trup jo-JSON) | `http_<kod>` | jo | FAILED + release | jo | konfigurim/URL | P2 |
| 2xx me `status=failed/undelivered/canceled` | `twilio_<error_code>` | jo | FAILED + release | jo | si më sipër | P3 |
| Callback `delivered` / `failed` / `undelivered` / `canceled` | — | — | DELIVERED / FAILED me `twilio_<ErrorCode>` | — | — | — |
| Callback i ndërmjetëm (`queued/sent/sending`) | — | — | injorohet (200) | — | — | — |

## 3. SMTP (`providers/email.py`)

| Dështimi | Kodi | Retry? | Terminal? | Panjohur? | Veprimi |
|---|---|---|---|---|---|
| Lidhja s'u vendos (`OSError`) | `smtp_connect:<Type>` | po | pas 5 | jo | rrjeti/host |
| `SMTPRecipientsRefused` me 4xx të gjitha | `smtp_recipient_refused` | po | pas 5 | jo | — |
| `SMTPRecipientsRefused` me 5xx | `smtp_recipient_refused` | jo | FAILED | jo | adresa e keqe |
| `SMTPResponseException` 4xx | `smtp_<kod>` | po | pas 5 | jo | — |
| `SMTPResponseException` 5xx | `smtp_<kod>` | jo | FAILED | jo | refuzim i përhershëm |
| Prerje pas lidhjes (`SMTPServerDisconnected`, `OSError` gjatë DATA) | `smtp_error:<Type>` | jo | **UNKNOWN** | **po** | rakordo; **mos ridërgo** |

Email nuk ka hold parash: UNKNOWN për email nuk lëviz para.

## 4. Çfarë NUK mbulohet nga F-07A
Sjellja reale e Twilio/vendorit për 401 vs 403, kodet reale të pezullimit/saldos, kufijtë e shpejtësisë së tyre, formati real i DLR-ve, vonesat reale — **PRODUCTION-ONLY (F-07B)**. TLS-i real nuk u testua (as i simuluar veç ConnectError).

**M11-J — shënim:** Twilio 408/425 (rare) trajtohen si 4xx të përhershëm (të pandryshuar; adapteri HTTP i trajton si të përkohshëm). Kur provider-i është i kufizuar (`SMS_PROVIDER_RATE_PER_SEC` / `SMS_PROVIDER_MAX_CONCURRENCY`) mesazhet **nuk marrin gabim**: mbeten QUEUED derisa të lirohet kapaciteti (shih runbook §9).
