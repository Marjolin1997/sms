# SMS Central Console

Konsola e **stafit Central** (admin / operator), e ndarë nga `frontend/` (konsola e klientit Enterprise): hyrje e veçantë (`POST /auth/token`, JWT), API-ja Central (`/auth`, `/admin`), build i veçantë.
Zbatuar: **Sender Governance** (Sender Registry, Country Policies, përmbledhje). Shih `docs/M11_FRONTEND.md` §13.

```bash
cd frontend-central && npm install
CENTRAL_API_URL=http://127.0.0.1:8001 npm run dev   # http://localhost:5174, proxy /auth /admin
npm test && npm run build                            # build përfshin i18n:check
```
- Token-i JWT ruhet vetëm në `sessionStorage`; skadon sipas `CENTRAL_AUTH_TTL_SECONDS` (parazgjedhje 900 s); 401 ⇒ kthim te hyrja.
- E2E në shfletues (DB e përkohshme): `e2e/central_seed.py` + `e2e/test_central_console.py`.
