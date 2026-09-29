# SMS Platform Console

React (Vite) mbi API-n e platformës. Hyrje me çelës API (`Bearer`); menyja tregon vetëm çka lejon roli.

```bash
cd frontend && npm install
npm run dev          # http://localhost:5173, proxy drejt API-së në :8000 (SMS_API_URL për tjetër)
npm run build        # dist/ statike; shërbe nga i njëjti origin me API-n (pa CORS)
```

Për të parë të dhëna demo: `python -m scripts.seed_demo` (vetëm zhvillim) printon një çelës `client` dhe një `superadmin`.

- Çelësi ruhet vetëm në `sessionStorage` (fshihet me mbylljen e skedës) — kurrë në localStorage.
- Stafi zgjedh llogarinë (`owner_ref`) lart; klienti sheh vetëm të vetën (serveri e detyron, jo UI).
- Faqe: Overview, Send (SMS/email), Campaigns (krijim, vlerësim, pauzë/rifillim/anulim, statistika), Contacts (import, lista, consent), Email domains (rekordet DNS, verifikim), Webhooks & events, API keys, Admin (kill switches, audit).
