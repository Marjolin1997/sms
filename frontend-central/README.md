# SMS Central Console

Konsola e **stafit Central** (admin / operator), e ndarë nga `frontend/` (konsola e klientit Enterprise): hyrje e veçantë (`POST /auth/token`, JWT), API-ja Central (`/auth`, `/admin`), build i veçantë.
Zbatuar: **Sender Governance** (Sender Registry, Country Policies), **Operations** (gatishmëria e sender-ave, sync, bootstrap, alarme), **Commercial** (kredi, pagesa, çmime, abonime, fatura + PDF, rakordim), **Audit** (vendimet e sender-ave, audit komercial, gjendja operative, aktiviteti i stafit; vetëm lexim, eksport CSV i kufizuar). Shih `docs/M11_FRONTEND.md`, `docs/M11_G_REPORTS_AUDIT.md`, `docs/PILOT_READINESS.md`.

```bash
cd frontend-central && npm install
CENTRAL_API_URL=http://127.0.0.1:8001 npm run dev   # http://localhost:5174, proxy /auth /admin
npm test && npm run build                            # build përfshin i18n:check
```
- Token-i JWT ruhet vetëm në `sessionStorage`; skadon sipas `CENTRAL_AUTH_TTL_SECONDS` (parazgjedhje 900 s); 401 ⇒ kthim te hyrja.
- E2E në shfletues (DB e përkohshme): `e2e/central_seed.py` + `e2e/test_central_console.py` + `e2e/test_central_audit.py`; stack-u i plotë Enterprise+Central mbi PostgreSQL: `e2e/pilot/` (shih `docs/PILOT_READINESS.md`).
