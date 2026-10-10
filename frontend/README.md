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

## Gjuha (shqip / English)
- Parazgjedhja është shqip; ndërrimi bëhet nga `LangSwitch` (ruhet në `localStorage`, çelësi `sms_lang`).
- Në kod shkruani tekstin anglisht me `t("Text")`, `t("Hi {name}", { name })`, `tn(n, "1 item", "{n} items")`; për konstante në nivel moduli përdorni `T("Text")` dhe përktheni kur shfaqet me `t(...)`.
- Shtoni përkthimin te një skedar në `src/locales/sq/` (regjistrohet te `index.js`). Statuset e API-së janë çelësa me shkronja të vogla (`delivered`, `opted_out`…), të përkthyer nga `Badge`.
- `npm run i18n:check` gjen çdo mungesë (përdoret edhe nga `npm run build`). `I18N_FULL=1` printon çelësat e plotë që mungojnë.
- Data, numra dhe monedha formatohen sipas gjuhës (`sq-AL` / `en-GB`) nga `money`, `when`, `dateOnly`, `ago`.
