# M11-I — Plani i sekreteve dhe i qasjes

**Revision i bazës:** `6020026`. Plan; **asnjë sekret nuk është krijuar, lexuar apo shfaqur**. Asnjë vlerë nuk shfaqet në këtë dokument. Statusi: **BLOCKED** (vendi i ruajtjes, pronarët dhe qasja janë **EXTERNAL EVIDENCE REQUIRED**).

Legjenda e rrotullimit: **RROTULLOHET** = mekanizmi ekziston; **PROCEDURË** = e mundur me hapa manualë të padokumentuar plotësisht; **JO** = e pambështetur sot.

## 1. Inventari sipas komponentit

| Komponenti | Sekreti / variabla | Ku duhet të jetë | Kush ka nevojë për qasje | Rrotullimi | Klasifikim |
|---|---|---|---|---|---|
| Enterprise | `POSTGRES_PASSWORD`, `SMS_DATABASE_URL` (përmban fjalëkalim) | Menaxheri i sekreteve → `.env` i hostit me leje 600, jashtë git | Operatori që nis compose; DBA | PROCEDURË (`ALTER ROLE` + `.env` + rinisje; nuk është dokumentuar) | PARTIALLY VERIFIED |
| Enterprise | `SMS_PII_HMAC_KEY` | Menaxheri i sekreteve; kopje e veçantë e sigurt jashtë backup-eve të DB | Operatori; ruajtësi i çelësave | **JO** — mos e ndryshoni pasi ka të dhëna (hash-et e pëlqimit s'gjenden më) | VERIFIED (RUNBOOK §1, §7) |
| Enterprise | `SMS_SECRETS_KEY` (Fernet; çelësat DKIM, sekrete të enkriptuara) | Menaxheri i sekreteve; **veçmas** nga backup-et e DB-së | Operatori; ruajtësi i çelësave | **JO** — rrotullimi kërkon rienkriptim (nuk mbështetet) | VERIFIED (RUNBOOK §1, §7) |
| Enterprise | `SMS_ADMIN_API_KEY` (bootstrap, ≥ 24 karaktere) | Përdoret vetëm për hapin e parë; pastaj **hiqet** nga `.env` | Operatori i parë | RROTULLOHET/HIQET (RUNBOOK §1.4) | VERIFIED |
| Enterprise | Çelësat API të stafit/klientit (prefiks + hash në DB) | Vetëm te zotëruesi (shfaqen një herë) | Çdo staf (personal), klientët | RROTULLOHET — revokim i menjëhershëm te konsola; çelës i ri | VERIFIED (RUNBOOK §6) |
| Enterprise | `SMS_DLR_SECRETS` (JSON provider→sekret HMAC) | Menaxheri i sekreteve; i ndarë me provider-in | Operatori; provider-i | PROCEDURË (koordinim me provider-in; s'është dokumentuar) | PARTIALLY VERIFIED |
| Enterprise | `SMS_TWILIO_ACCOUNT_SID`/`SMS_TWILIO_AUTH_TOKEN`/`SMS_TWILIO_MESSAGING_SERVICE_SID`; `SMS_HTTP_PROVIDER_KEY` | Menaxheri i sekreteve; vetëm në `.env` të serverit (jo në chat/repo — `docs/TWILIO.md`) | Operatori; pronari i llogarisë provider | RROTULLOHET te provider-i + `.env` + rinisje | VERIFIED (docs); llogaria EXTERNAL |
| Enterprise | `SMS_SMTP_PASSWORD` | Menaxheri i sekreteve | Operatori | RROTULLOHET te SMTP + `.env` + rinisje | PARTIALLY VERIFIED |
| Enterprise→Central | Çelësi privat Ed25519 (`./secrets/cp_private_key.pem` → `/run/secrets/cp_private_key`, read-only) | Gjenerohet jashtë Central dhe jashtë repo-së; mountohet si skedar read-only; **kurrë** në `.env`/git | Operatori i hostit Enterprise; asnjë tjetër | RROTULLOHET — çelës i ri publik te Central (`service_credential_admin`), çelësi i vjetër `disable-key` | VERIFIED (M9C §3; compose) |
| Enterprise→Central | `SMS_CP_CLIENT_ID`, `SMS_CP_KEY_ID` (identifikues, jo sekrete) | `.env` | Operatori | — | VERIFIED |
| Enterprise | Vlerat ACK (`SMS_SENDER_AUTHORITY_ACK`, `SMS_MONEY_AUTHORITY_ACK`, …) | `.env`; **dëshmi, jo sekret** (hash prove i cutover-it) | Operatori | Rigjenerohet me çdo provë të re (`scripts.sender_cutover evidence`) | VERIFIED |
| Central | `CENTRAL_DATABASE_URL`/fjalëkalimi i DB-së Central | Menaxheri i sekreteve; DB e veçantë nga Enterprise | Operatori Central; DBA | PROCEDURË | PARTIALLY VERIFIED (emri nga prefiksi `CENTRAL_`) |
| Central | `CENTRAL_AUTH_SECRET` (JWT HS256, ≥ 32 karaktere; nisja refuzon pa të në prodhim) | Menaxheri i sekreteve | Operatori Central | PROCEDURË — ndryshimi i vlerës + rinisja i bën të pavlefshëm çdo token (TTL parazgjedhje 900 s); s'ka `kid` për rrotullim pa ndërprerje | PARTIALLY VERIFIED (nga `tokens.py`: HS256 konstant, një sekret) |
| Central | `CENTRAL_REGISTRATION_VERIFY_KEY` | Menaxheri i sekreteve | Operatori Central | PROCEDURË (lidhjet e verifikimit ekzistuese bëhen të pavlefshme) | PARTIALLY VERIFIED |
| Central | `CENTRAL_SMTP_PASSWORD` | Menaxheri i sekreteve | Operatori Central | RROTULLOHET te SMTP + env + rinisje | PARTIALLY VERIFIED |
| Central | Sekreti i CAPTCHA/bot-challenge | Menaxheri i sekreteve (adapteri real nuk ekziston: vlerat janë `disabled`/`fake`) | — | N/A | VERIFIED (kod); **NOT VERIFIED** adapter real |
| Central | Fjalëkalimet e administratorëve (Argon2id) | Vetëm te personi; kurrë të ndara | Çdo admin/operator Central | RROTULLOHET nga përdoruesi/admin (`create_admin`) | VERIFIED (hashimi); politika e fjalëkalimit NOT VERIFIED |
| Central | Çelësat publikë të klientëve të shërbimit (`service_keys`) | DB Central (publikë, jo sekrete) | — | RROTULLOHET (`disable-key`/`disable-client`) | VERIFIED |
| CI / ndërtim | Sekretet e CI (aktualisht vetëm vlera sintetike `ci-only-…`, DB provë `sms/sms`) | Magazina e sekreteve të CI; **asnjë sekret prodhimi në CI** | Mirëmbajtësit e CI | RROTULLOHET | VERIFIED (ci.yml: vetëm vlera provë) |
| Host | Çelësat SSH/IAM të operatorëve | Menaxheri i identitetit i organizatës | Operatorët e emëruar | RROTULLOHET sipas politikës së organizatës | EXTERNAL EVIDENCE REQUIRED |

## 1b. Matrica e validimit të nisjes në prodhim (M11-I/F-03)

Siç zbatohet në `production_problems()` (Enterprise) dhe `create_app` (Central); mesazhet kurrë s'përmbajnë vlera.

| Sekreti / cilësimi | Klasifikim | Kontrolli i nisjes |
|---|---|---|
| `SMS_PII_HMAC_KEY` (≥ 32), `SMS_SECRETS_KEY` | Gjithmonë i detyrueshëm | refuzon bosh/të shkurtër |
| `SMS_ADMIN_API_KEY` | Opsional (bootstrap) | nëse vendoset: ≥ 24, jo placeholder |
| Çelësat API të stafit/klientit | Operacional (DB), jo env | — |
| `SMS_DLR_SECRETS[<provider HTTP>]` | **Kushtëzuar**: kur `SMS_HTTP_PROVIDER_URL` është vendosur | ≥ 24, jo `change/dev/test/demo…` |
| Hyrje të tjera në `SMS_DLR_SECRETS` | Opsionale | nëse jo-boshe: jo e dobët |
| `SMS_TWILIO_ACCOUNT_SID` + `SMS_TWILIO_AUTH_TOKEN` | **Kushtëzuar**: të dyja ose asnjëra | refuzon gjysmë-konfigurimin; nuk kërkohet HMAC DLR (X-Twilio-Signature) |
| `SMS_HTTP_PROVIDER_KEY`, `SMS_TWILIO_MESSAGING_SERVICE_SID` | Opsional | nuk validohet |
| `SMS_SMTP_HOST` | **Kushtëzuar**: kur `SMS_EMAIL_PROVIDER=smtp` | refuzon bosh; `SMS_SMTP_USER/PASSWORD` opsionale (relay pa auth) |
| `SMS_TRUSTED_PROXY_HOPS` | Gjithmonë (topologjia F-02) | = 1 |
| Çelësi privat CP / `SMS_CP_*` | Kushtëzuar nga sync/authority (gate-t ekzistuese); sekret operacional i jashtëm | `https` + ACK sipas modit |
| Sekretet e pagesave | `SMS_PAYMENT_PROVIDER` ≠ `fake`; asnjë gateway real ende | refuzon `fake` |
| `CENTRAL_AUTH_SECRET` (≥ 32) | Gjithmonë i detyrueshëm | refuzon |
| `CENTRAL_REGISTRATION_VERIFY_KEY` | Opsional: pa të, regjistrimi mbetet manual | nuk kërkohet |
| `CENTRAL_SMTP_HOST` + `CENTRAL_SMTP_FROM` (+ kredenciale) | **Kushtëzuar**: kur `CENTRAL_MAILER=smtp` | host + from të detyrueshëm; user/password opsionalë |
| `CENTRAL_TRUSTED_PROXY_HOPS` | Gjithmonë | = 1 |
| Vlerat reale, rrotullimi, menaxheri i sekreteve | EXTERNAL EVIDENCE REQUIRED | — |

## 2. Kufizimet që nuk rrotullohen sot

- `SMS_PII_HMAC_KEY`: një ndryshim shkatërron përputhjen e hash-eve të pëlqimit për të dhënat ekzistuese. Humbja = humbje e pëlqimeve të verifikueshme. (VERIFIED/PARTIALLY VERIFIED sipas burimit më sipër)
- `SMS_SECRETS_KEY`: një ndryshim lë të palexueshme sekretet e enkriptuara (p.sh. çelësat DKIM). Humbja = rikrijim i tyre. (VERIFIED/PARTIALLY VERIFIED sipas burimit më sipër)
- Të dyja janë të domosdoshme për çdo restore: **backup-i i DB-së pa këto dy çelësa nuk është rikuperim i plotë**. (VERIFIED/PARTIALLY VERIFIED sipas burimit më sipër)
- `CENTRAL_AUTH_SECRET` nuk ka rrotullim pa ndërprerje (nuk ka `kid`); ndryshimi mbyll të gjitha sesionet. (VERIFIED/PARTIALLY VERIFIED sipas burimit më sipër)

## 3. Ndarja e backup-eve dhe e sekreteve

| Kërkesa | Prova e pranimit | Klas. |
|---|---|---|
| Dump-et e DB-së (`scripts/backup.sh`) **nuk** përmbajnë `SMS_SECRETS_KEY`/`SMS_PII_HMAC_KEY` dhe ruhen në vend tjetër | Dy vende ruajtjeje të dokumentuara me qasje të ndara | EXTERNAL EVIDENCE REQUIRED |
| Kopje e çelësave në vend të sigurt të dytë (p.sh. kasafortë e ndarë) me të paktën 2 persona të autorizuar | Regjistër i qasjes me emra | EXTERNAL EVIDENCE REQUIRED |
| Dump-et e enkriptuara dhe off-host | Provë e enkriptimit dhe e ruajtjes jashtë hostit | EXTERNAL EVIDENCE REQUIRED |
| Restore-i testohet me çelësat nga vendi i veçantë | Restore + `verify_ledger` PASS + një lexim i një sekreti të enkriptuar | NOT VERIFIED |

## 4. Qasja SSH/IAM dhe hosti

- Qasje SSH vetëm me çelësa; pa fjalëkalime; pa root direkt (PROPOSED) — **EXTERNAL EVIDENCE REQUIRED**
- Lista e emërtuar e operatorëve me qasje te hosti; heqje brenda 24 orësh pas largimit (PROPOSED) — **EXTERNAL EVIDENCE REQUIRED**
- `.env` dhe `./secrets/` të lexueshëm vetëm nga përdoruesi i shërbimit; `stat -c '%a %U' .env` = `600` — **EXTERNAL EVIDENCE REQUIRED (kontroll te checklist SC-02)**
- Docker socket jo i ekspozuar; kontejnerët pa privilegje (compose: `cap_drop ALL`, `no-new-privileges`, FS read-only) — **VERIFIED (compose); hosti EXTERNAL**

## 5. Rolet e DB-së

| Kërkesa | Gjendja | Klas. |
|---|---|---|
| Aplikacioni lidhet me rol pa superuser (`.env.example` e këshillon) | Compose lidh `sms`, përdoruesin e krijuar nga `POSTGRES_USER` (superuser në imazhin zyrtar) — kontradiktë C-6 e analizës së boshllëqeve | PARTIALLY VERIFIED |
| Roli i migrimeve i ndarë nga roli i ekzekutimit (PROPOSED) | Sot një rol i vetëm | NOT VERIFIED |
| TLS në lidhjen API↔DB (`sslmode=require`) | Shembulli `.env.example` e ka; compose përdor URL pa `sslmode` | PARTIALLY VERIFIED |
| Rol i veçantë vetëm-lexim për raportim/backup (PROPOSED) | Nuk ekziston | NOT VERIFIED |
| DB-të Enterprise dhe Central të ndara (kredenciale të ndara) | Të ndara në harness-in M11-H | VERIFIED (harness); prodhimi EXTERNAL |

## 6. Sekretet e CI

- CI përdor vetëm vlera sintetike (`sms/sms`, `ci-only-hmac-…`, çelës Fernet testi); asnjë sekret prodhimi (VERIFIED, `ci.yml`).
- Nuk ka hap skanimi sekretesh në CI (VERIFIED, mungon) — shih §10.
- `pip-audit` dhe `npm audit` janë këshillues (`continue-on-error`).
- Kërkesa e ardhshme: asnjë sekret prodhimi te runner-i i CI; deployment me kredenciale të shkurtra të ndara nga CI (PROPOSED).

## 7. Kredencialet e provider-it

- Vendosen vetëm në menaxherin e sekreteve dhe `.env` të serverit; **kurrë** në chat, git, tiketë (`docs/TWILIO.md` — VERIFIED).
- Kredencialet e provider-it të provës dhe të prodhimit janë të ndara (PROPOSED); canary-i përdor vetëm kredencialet e prodhimit pas miratimit.
- Rrotullimi i kredencialeve të provider-it: te provider-i, pastaj `.env`, pastaj rinisje; çaktivizo rrugën gjatë ndërrimit (RB §6).
- Statusi: **EXTERNAL EVIDENCE REQUIRED**; asnjë llogari nuk është verifikuar.

## 8. Trajtimi i çelësit privat CP

- Gjenerohet jashtë Central dhe jashtë repo-së; te Central dërgohet vetëm çelësi publik (`create_service_credential --public-key-file`, M9C §3) — VERIFIED.
- Mountohet te Enterprise si `./secrets/cp_private_key.pem` → `/run/secrets/cp_private_key` read-only; `SMS_CP_PRIVATE_KEY_PATH` vendoset aty — VERIFIED (compose).
- Skedari ka leje 600 dhe pronar përdoruesi i operatorit; **nuk kopjohet** në imazh, log apo backup — kontroll te checklist (PROPOSED).
- Një klient shërbimi për rol me scope minimal (`sync:read`, `money:read`, `money:report`, `pricing:read`, `billing:report`, `sender:read`, `sender:report`) — VERIFIED (M9C §3, M10).
- Rrotullimi/revokimi: `service_credential_admin disable-key|disable-client` (+ audit `service_client.create`/`service_key.add`) — VERIFIED.

## 9. 2FA dhe bootstrap i administratorit

| Kërkesa | Gjendja | Klas. |
|---|---|---|
| Hapi i parë me çelës bootstrap; çelës `superadmin` personal për çdo person; 2FA aktive te *Siguria*; pastaj heqje e `SMS_ADMIN_API_KEY` dhe rinisje | Procedura është te RUNBOOK §1.4 | VERIFIED (procedura); zbatimi EXTERNAL |
| `SMS_REQUIRE_STAFF_2FA=true` | Shembulli `.env.example` e ka; mekanizmi ekziston | VERIFIED (mekanizmi) |
| Reset 2FA vetëm nga superadmin tjetër ose bootstrap (`POST /v1/admin/api-keys/{id}/reset-2fa`) | RUNBOOK §6 | VERIFIED |
| 2FA për stafin Central | Kërkimi në `apps/central` nuk gjeti totp/2fa; hyrja është fjalëkalim + JWT | EXTERNAL / POLICY DECISION REQUIRED (F-03 nuk e zbaton; më parë PARTIALLY VERIFIED nga kërkimi) — kontroll kompensues i kërkuar (VPN/IP allow-list), vendim i pronarit |
| Operatori Central vetëm-lexim; admin Central vetëm për të miratuar | 42 shkrime të operatorit refuzohen 403 (M11-H) | VERIFIED (M11-H) |

## 10. Pritshmëritë e audit-it dhe boshllëku i skanimit

| Pritshmëria | Gjendja | Klas. |
|---|---|---|
| Çdo krijim/revokim çelësi dhe veprim i ndjeshëm i stafit audit-ohet | Enterprise `sms_audit_log` dhe Central `audit_log` ekzistojnë (M11-G); veprimet e stafit pa `owner` nuk shfaqen te audit-i i klientit (MEDIUM, M11-H) | VERIFIED |
| Hyrjet (login) e stafit Central në audit | Vetëm në log, jo në tabelë (MEDIUM, M11-H) | VERIFIED (boshllëk) |
| Ruajtja e audit-it | Vendimi A: nuk nevojitet retention për pilot; rishikim kur >1M rreshta ose >1 GB | VERIFIED (M11-H) |
| Askush nuk lexon sekrete nga audit-i | `detail` në audit Central kalon nga pastruesi i sekreteve; audit-i klientit lejon vetëm fusha të listuara | VERIFIED (M11-G/H) |
| Skanim sekretesh në repo, histori dhe imazhe | Nuk ka hap në CI; nuk është ekzekutuar skanim i historisë në këtë fazë | **NOT VERIFIED** — boshllëk (F-04) |
| Qasja te menaxheri i sekreteve e audit-uar (kush lexoi çfarë) | Varet nga menaxheri i zgjedhur | EXTERNAL EVIDENCE REQUIRED |

## 11. Verdikti

**BLOCKED**: inventari dhe rregullat janë të plota nga ana e kodit, por vendi i ruajtjes, pronarët, qasja SSH/IAM, skanimi i sekreteve, kredencialet e provider-it dhe provat e restore-it me çelësa të ndarë janë të jashtme dhe të paverifikuara. Dy çelësa (`SMS_PII_HMAC_KEY`, `SMS_SECRETS_KEY`) nuk rrotullohen; humbja e tyre është e pariparueshme.
