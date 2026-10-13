# Gatishmëria për pilot (M11-H)

**Kodi i gatshëm për pilot ≠ sistem i gatshëm për prodhim.** Ky dokument thotë çfarë u provua në kod dhe çfarë mbetet të provohet jashtë kodit.
Evidenca e plotë e validimit (komanda, numra, kohë): `docs/M11_H_PILOT_QA.md`. Harness-i i ripërdorshëm: `e2e/pilot/`.

## 1. Parakushtet
- Host(e) Linux me Docker/Compose **ose** systemd; Python 3.12; Node 22 (vetëm për build-in e konsolave); PostgreSQL 16.
- Reverse proxy me TLS (Caddy/nginx/Traefik) para API-ve dhe konsolave; DNS; një vend sekretesh (jo `.env` në git); backup me WAL/PITR (jashtë kodit).
- Dy baza logjike të **veçanta**: Enterprise (`sms`) dhe Central (`central`). Asnjë tabelë e përbashkët.

## 2. Shërbimet e nevojshme
| Shërbim | Komanda | Shënim |
|---|---|---|
| Enterprise API | `uvicorn app.main:app --no-proxy-headers` | `/healthz`, `/readyz` (503 nëse skema ≠ kodi) |
| Punonjësi SMS | `python -m app.worker --role sms` | dërgim, DLR sweeper, fushata, faturim periodik |
| Punonjësi webhooks | `python -m app.worker --role webhooks` | |
| Sender sync | `python -m app.worker --role sender_control_plane` | **një aktiv për DB** (kyç advisory) |
| Sender report + ops snapshot | `python -m app.worker --role sender_request_reporter` | **një aktiv për DB**; dërgon edhe `sender.ops.v1` |
| Konsola Enterprise | build `frontend/` + nginx (`deploy/web.Dockerfile`) | |
| Central API | `uvicorn apps.central.main:app` | **s'është i paketuar në Dockerfile/compose** (shih §19) |
| Konsola Central | build `frontend-central/` + proxy `/auth`, `/admin` | **s'është i paketuar** (shih §19) |
Opsionale e FIKUR për pilot: `control_plane`, `money_*`, `pricing_control_plane`, `billing_usage_reporter` (mbeten në `local`).

## 3. Kërkesat PostgreSQL
Versioni 16; përdorues me të drejta DDL vetëm për shërbimin `migrate`; `statement_timeout` / `lock_timeout` / `idle_in_transaction_session_timeout` jepen nga `SMS_DB_*`; `pg_dump`/WAL jashtë aplikacionit. Matje: instalim i pastër i të dy bazave ≈ 6 s.

## 4. Migrimet
```bash
alembic heads                                    # pritet: 0034 (Enterprise)
alembic -c apps/central/alembic.ini heads        # pritet: 0031 (Central)
alembic upgrade head && alembic -c apps/central/alembic.ini upgrade head
alembic current ; alembic -c apps/central/alembic.ini current   # të dyja "(head)"
```
`downgrade` **nuk është mjet rikthimi në prodhim**: heq tabelat e shtuara më vonë bashkë me të dhënat (p.sh. historiku i vendimeve të sender-ave). Rikthim = imazhi i vjetër + backup (RUNBOOK §3).

## 5. Variablat e mjedisit (pa vlera sekrete këtu)
**Enterprise:** `SMS_ENV=production`, `SMS_DATABASE_URL`, `SMS_PII_HMAC_KEY` (≥32, **mos e ndrysho**), `SMS_SECRETS_KEY` (Fernet), `SMS_ADMIN_API_KEY` (bootstrap — hiqe pas çelësave personalë), `SMS_PUBLIC_BASE_URL=https://…`, `SMS_TRUSTED_PROXY_HOPS=1` (**kritike pas proxy-t**, shih §18), `SMS_REQUIRE_STAFF_2FA=true`, `SMS_DLR_SECRETS={"<provider>":"<sekret>"}`, `SMS_EMAIL_PROVIDER=smtp`+SMTP, `SMS_PAYMENT_PROVIDER=disabled`.
**Sinkronizimi me Central:** `SMS_CP_BASE_URL=https://…`, `SMS_CP_CLIENT_ID`, `SMS_CP_KEY_ID`, `SMS_CP_PRIVATE_KEY_PATH` (mount sekret, Ed25519), `SMS_SENDER_SYNC_ENABLED`, `SMS_SENDER_REQUEST_REPORTING`, `SMS_SENDER_OPS_REPORTING`, `SMS_SENDER_AUTHORITY` (+`SMS_SENDER_AUTHORITY_ACK`, `SMS_SENDER_DISPATCH_RECHECK=true` për `central`).
**Central:** `CENTRAL_ENV=production`, `CENTRAL_DATABASE_URL`, `CENTRAL_AUTH_SECRET` (≥32), `CENTRAL_AUTH_TTL_SECONDS` (900). Regjistrimi publik mbetet i mbyllur.

## 6. Parazgjedhjet e sigurta (nuk ndryshohen për pilot pa arsye)
`SMS_SENDER_AUTHORITY=local` derisa të kryhet cutover-i; `SMS_MONEY_AUTHORITY=local`; `SMS_PRICING_AUTHORITY=local`; `SMS_PAYMENT_PROVIDER=disabled`; `CENTRAL_PUBLIC_REGISTRATION_ENABLED=false`; `SMS_ENV=production` refuzon nisjen me konfigurim të pasigurt.

## 7. Rolet e punonjësve
Shih §2. Dy punonjësit e sender-ave përdorin kyç advisory: kopjet shtesë janë të sigurta por inaktive. Kur Central bie, punonjësit riprovojnë dhe projeksioni lokal mbetet (fail-static): **submit/dispatch nuk varen nga Central** (provuar).

## 8. Build-et e frontend-it
```bash
(cd frontend && npm ci && npm test && npm run build)            # build ekzekuton edhe i18n:check
(cd frontend-central && npm ci && npm test && npm run build)
```

## 9. Smoke test (sekuenca)
1. `GET /readyz` Enterprise + `GET /healthz` Central → 200.
2. Central: `python -m apps.central.tools.create_admin` (admin + operator) → hyrje në konsolë; operatori është vetëm-lexim.
3. Enterprise: çelësa personalë stafi → `SMS_ADMIN_API_KEY` hiqet → 2FA.
4. Tenant A dhe B (çelësa të veçantë) → §10.
5. Sender: §11. Campaign: §12. Faturë: §13. Raporte/audit: §14. Gatishmëria: §15.

## 10. Kontrolli i izolimit ndër-tenant
Me dy tenant-ë: tenant A merr 404/403 për çdo burim të B (kontakt, listë, sender, campaign, marrës, mesazh, faturë+PDF, wallet, webhook, çelës, audit, CSV) dhe koleksionet e tij s'përmbajnë gjurmë të B. Harness: `python -m e2e.pilot.iso_matrix` (113 kontrolle, të dy drejtimet). Në pilotin real: të njëjtat 5–6 kërkesa manuale me dy çelësa.

## 11. Kontrolli i rrjedhës së sender-it
Kërkesë (Enterprise) → Central e sheh `pending` → admin miraton → projeksion → `effective_status=approved` (vetëm me `authority=central`) → dërgimi kalon → revokim → `revoked`, submit i ri refuzohet, mesazhi në radhë dështon `sender_revoked` pa thirrur provider-in. Harness: `e2e/pilot/prepare.sh` + `flow_central`.

## 12. Smoke i campaign-it
Në pilotin real **pa FakeProvider**: një canary me numër të kontrolluar sipas `docs/M10_SENDER_RUNBOOK.md` §6(19–21). Në staging/harness: `python -m e2e.pilot.flow_central` (FakeProvider + DLR i nënshkruar).

## 13. Smoke i faturimit
Faturë e tenant-it → `GET /v1/billing/invoices/{id}/pdf` (PDF zyrtar, snapshot i lëshuesit); tenant tjetër → 404; Central: `/admin/billing/invoices/{id}/pdf`. Harness: `e2e.pilot.csv_pdf_security`.

## 14. Smoke i raporteve/audit-it
`#reports` (overview/campaigns/failures/usage), `#activity`, eksportet CSV (422 mbi kufi), Central `#audit-*`. Harness: `e2e.pilot.ui_pilot`.

## 15. Kontrollet e gatishmërisë
```bash
python -m apps.central.tools.sender_central_readiness --json
python -m scripts.sender_policy_readiness --target central --central-readiness central.json --json
python -m scripts.sender_alerts --json ; python -m scripts.sender_cutover status
```
Central `#ops`: pa snapshot ⇒ UNKNOWN, i vjetër ⇒ stale/expired, drift kritik / bootstrap i pazgjidhur ⇒ alarm (provuar). Alarmet e Enterprise ruhen në cache deri 300 s para se të shfaqen te Central.

## 16. Backup
`scripts/backup.sh` + WAL/PITR i dy bazave; `SMS_SECRETS_KEY`, `SMS_PII_HMAC_KEY`, çelësi Ed25519 dhe `CENTRAL_AUTH_SECRET` ruhen **veçmas** nga backup-i i DB-së. Provë restore (`scripts/restore.sh` + `scripts.verify_ledger`) para pilotit.

## 17. Parakushtet e rikthimit
Imazh i vjetër + backup i marrë para përditësimit; `SMS_SENDER_AUTHORITY` central→shadow/local sipas `M10_SENDER_RUNBOOK` §7; asnjë `alembic downgrade` në prodhim.

## 18. Kufizime të njohura (borxh i pranuar)
- **Kufizimi i provave të dështuara është sipas IP** (README Faza 13): me `SMS_TRUSTED_PROXY_HOPS=0` pas proxy-t, një sulmues mund të kyçë **të gjithë** përdoruesit (429 edhe për çelës të vlefshëm). Vendos `SMS_TRUSTED_PROXY_HOPS=1` dhe provo.
- Audit Enterprise: veprimet e stafit pa `owner` (p.sh. rregullim wallet, miratim lokal sender) nuk shfaqen te klienti (e shfaq ledger-i); etiketa "ju" nuk dallon çelësat e të njëjtit tenant; histori para `0034` e padukshme (nuk ka në instalim të ri).
- Audit Central vetëm për veprimet që shkruajnë `audit_log`; login-et vetëm në log (`login ok/failed`), jo në tabelë; leximet s'auditohen.
- Eksporte sinkrone (≤100 000 / Central ≤50 000 rreshta); Central e mbush listën në memorie para stream-it (≤50 000).
- Pa retention për `sms_audit_log` / `audit_log` (vendim A: nuk nevojitet për pilot; rishiko >1M rreshta ose >1 GB, ose p95 e listës >500 ms ⇒ indeks `(created_at DESC, id DESC)` te Central).
- Paginimi Central është offset (offset 20 000 ≈ 0.1–0.5 s deri 1M rreshta).
- Pa segmente, import asinkron, retry/resend, grafik historik operativ (Central ruan vetëm gjendjen e fundit).

## 19. Bllokuesit e jashtëm (jo të kodit)
Hosting/serverë; PostgreSQL prodhimi; PITR/WAL; TLS/DNS; menaxhim sekretesh; kredenciale provider-i SMS/email; canary provider/DLR real; monitorim/alarme prodhimi; dritare shadow me trafik real dhe ACK-të njerëzore të cutover-it; **paketimi i Central** (Dockerfile s'kopjon `apps/`, compose s'ka shërbim Central/konsolë Central, `.env.example` s'ka `CENTRAL_*`) — punë deployment, jo e kodit të aplikacionit.

## 20. Lista go / no-go
- [ ] Të dy bazat në `0034` / `0031`, `compare_metadata=0`, backup + provë restore.
- [ ] `readyz` / `healthz` OK; punonjësit aktivë (sms, webhooks, sender sync, sender report).
- [ ] Çelësa personalë stafi + 2FA; `SMS_ADMIN_API_KEY` hequr; operatori Central vetëm-lexim.
- [ ] `SMS_TRUSTED_PROXY_HOPS` i saktë; TLS; HSTS pas TLS.
- [ ] Matrica e izolimit dhe RBAC jeshile në mjedisin e pilotit (dy tenant-ë).
- [ ] Sender: kërkesë→Central→projeksion→dërgim→revokim provuar; readiness pa FAIL; ACK i cutover-it kur `central`.
- [ ] Canary real me provider/DLR; faturimi/PDF i provuar; alarmet e monitorimit të lidhura.
- [ ] Rishikim i borxhit të §18 nga pronari i produktit.
Asnjë kuti e paplotësuar nuk lejohet për "go".
