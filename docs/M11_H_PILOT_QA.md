# M11-H — QA përfundimtar / gatishmëria për pilot: evidenca

Mjedisi i validimit: **PostgreSQL 16 + uvicorn REAL (Enterprise `:8100`, Central `:8101`) + 3 punonjës Enterprise + 2 Vite (`:5173`, `:5174`)**, DB të përkohshme `h_*`,
**vetëm FakeProvider** (nënklasë me id unike për shkak të rinisjeve; e njëjta sjellje), pa kredenciale të vërteta. `TestClient` NUK përdoret për rezultatet e këtij dokumenti.
Roja `e2e/pilot/guard.py` (importuar nga çdo skript) ndalon ekzekutimin nëse zbulon konfigurim ofruesi të vërtetë; testohet te `tests/test_m11h_pilot_guard.py`.

## Si ngrihet dhe si ekzekutohet
```bash
export PILOT_DIR=/tmp/pilot            # direktori e përkohshme (sekrete të gjeneruara aty; asgjë në repo)
e2e/pilot/prepare.sh                   # up (DB të reja, migrime, seed, uvicorn, punonjës, Vite) → bootstrap → shadow me trafik → provë → ACK → authority=central
. $PILOT_DIR/pilot.env
python -m e2e.pilot.flow_central       # sender→Central→projeksion→dërgim→campaign→revokim→rikontroll + skenarë dështimi
python -m e2e.pilot.iso_matrix         # izolimi ndër-tenant (API + eksporte), dy drejtime
python -m e2e.pilot.rbac_auth          # RBAC (nga rrugët reale) + auth/sesion + 429
python -m e2e.pilot.ui_pilot           # Chromium: rrjedha klient+staf, dështime, responsive, a11y, i18n, console
python -m e2e.pilot.csv_pdf_security   # formula CSV, PDF, snapshot, Unicode, sekrete
python -m e2e.pilot.ops_scenarios      # gatishmëria operative + izolimi kur Central bie
python -m e2e.pilot.migrations_check   # kokat, drift, down/up me të dhëna, instalim i pastër
python -m e2e.pilot.perf_audit central|enterprise ; python -m e2e.pilot.perf_exports
e2e/pilot/stack.sh down
```

## Rezultatet kryesore
| Fusha | Rezultat |
|---|---|
| Rrjedha e sender-it (Enterprise→Central→projeksion→dërgim→revokim) | 35 kontrolle (`flow_central`) + 23 (shadow) |
| Izolimi ndër-tenant | 113 kontrolle, dy drejtime: detaje, koleksione, 5 CSV, PDF, wallet, webhook, çelës, audit |
| RBAC/auth | 22 kontrolle: 139 rrugë anonime→401, 460 refuzime rolesh→403, 42 shkrime Central të operatorit→403; JWT i skaduar/i falsifikuar/përdorues i çaktivizuar→401; 429 pas shumë dështimeve |
| UI Chromium | 150+ kontrolle: rrjedha klient (UI→Central UI→UI), tenant B, dështime (rrjet/5xx/429/401/403), 390/768/1360 px, a11y, shqip, 0 gabime console |
| CSV/PDF/sekrete | 55 kontrolle: `= + - @ TAB CR`, telefonat `+355…` të paprekur, PDF snapshot pa drift, Unicode, skanim sekretesh (API, log, DOM, URL) |
| Operacionet | 18 kontrolle: pa snapshot→unknown, fresh, stale, expired, drift kritik, bootstrap i pazgjidhur, Central i ndalur |
| Migrime | 24 kontrolle: kokat 0034/0031, `compare_metadata=0`, down/up me të dhëna, instalim i pastër base→head |

## Performanca (PostgreSQL, uvicorn real, të dhëna sintetike)
Audit në shkallë, mediana e 5 matjeve:

| Rreshta | Central lista | Central offset 10 000 | Central + indeks kandidat (lista / offset 10 000) | Enterprise audit lista (keyset) |
|---|---|---|---|---|
| 100 000 | 63 ms | 87 ms | – | 15 ms |
| 500 000 | 143 ms | 209 ms | – | 27 ms |
| 1 000 000 | 229 ms | 512 ms | 17 ms / 28 ms | 14 ms |

Enterprise `facets` (DISTINCT mbi audit-in e tenant-it): 83 → 215 ms (100k → 1M). Indeksi `(created_at DESC, id DESC)` te Central: material në 1M, **jo i nevojshëm për pilot** (nuk u shtua).

Eksportet (uvicorn, RSS i serverit 121→139 MB, i qëndrueshëm):

| Eksport | Rreshta | MB | Byte i parë | Total |
|---|---|---|---|---|
| messages.csv | 10 480 / 50 861 / 100 000 | 1.0 / 5.0 / 9.9 | 58–226 ms | 1.0 / 3.6 / 6.6 s |
| audit.csv (tenant) | 10 525 / 50 606 | 0.9 / 4.6 | 37–64 ms | 0.7 / 2.5 s |
| recipients.csv | 100 000 | 4.6 | 36 ms | 3.7 s |
| Central audit.csv / sender-decisions.csv | 10 189 / 10 172 | 1.4 / 1.5 | ~1.1 s (lista mbushet para stream-it) | 1.2 / 1.3 s |
| mbi kufi (100 001 / 50 001) | – | – | **422 në 16–60 ms, pa trup** | – |

Faqet interaktive (100k kontakte, 1k campaign, 100k mesazhe/marrës): kontakte 11–28 ms, lista 24 ms, campaign list 11–127 ms, detail 120 ms, marrës keyset 18–22 ms, mesazhe 14–15 ms, overview 30d/100d 71/196 ms, campaign report 145 ms, failures 52 ms, audit 16 ms; Central audit 46–62 ms, sender decisions 90–117 ms, operations 24 ms. Krahasuar me M11-G (TestClient): të njëjtat rend madhësie, asnjë regres material.

## Vendimi për retention-in e audit-it: A (nuk nevojitet për pilot)
Madhësia: ~205 B/rresht (Enterprise) dhe ~260 B/rresht (Central) me indekse. Rritja e pritshme e pilotit (dhjetëra tenant-ë, qindra veprime/ditë): <1M rreshta/vit (<250 MB). Rishiko kur: >1M rreshta në njërën tabelë, >1 GB, p95 e listës >500 ms, ose `facets` >300 ms. Shënim: çdo klikim eksporti shkruan një rresht `report.export` (zhurmë audit-i; kufizim i mundshëm më vonë).

## Gjetje dhe klasifikim
| Gjetje | Ashpërsia |
|---|---|
| Central s'është i paketuar (Dockerfile s'kopjon `apps/`, compose pa shërbim/konsolë Central, `.env.example` pa `CENTRAL_*`) | HIGH (bllokues i deployment-it të pilotit, jo i kodit) |
| Kyçja e provave të dështuara sipas IP + `SMS_TRUSTED_PROXY_HOPS=0` pas proxy-t ⇒ një sulmues kyç të gjithë | HIGH (konfigurim; i dokumentuar te README) |
| Veprimet e stafit pa `owner` (wallet.adjust, sender.approve lokal) mungojnë nga audit-i i klientit | MEDIUM (ledger-i dhe historiku i sender-it i shfaqin) |
| Login-et Central vetëm në log, jo në tabelë audit | MEDIUM |
| Etiketa "ju" s'dallon çelësat e të njëjtit tenant | LOW |
| Eksport Central e mbush listën në memorie (≤50 000) para stream-it | LOW |
| `alembic downgrade` humb tabelat e shtuara më vonë | ACCEPTED DEBT (rikthimi = backup) |
| Alarmet Enterprise arrijnë te Central me vonesë deri 300 s (cache) | ACCEPTED DEBT |
| Audit Enterprise para `0034` pa tenant | ACCEPTED DEBT (instalim i ri s'e ka) |
| Defekt i gjetur dhe rregulluar gjatë M11-G (E2E): të dhënat e filtrit të mëparshëm shfaqeshin nën filtër të ri që dështonte | u rregullua (`useReport`) |
