# Kapaciteti dhe performanca

Matje reale me `scripts/bench.py` dhe `scripts/bench_campaign.py` (vetëm mbi një bazë PostgreSQL `*_bench`; refuzojnë çdo bazë tjetër).

**Mjedisi i matjes:** një VM me 4 vCPU / 16 GB, **gjithçka në të njëjtin host**: PostgreSQL 16, 2 procese uvicorn, gjeneruesi i ngarkesës dhe workers. Provider-i është `fake` (pa rrjet). Pra numrat janë të konservativë për API/DB, por **nuk përfshijnë latencën e provider-it real** (Twilio etj.), që e ngadalëson dërgimin. Përsërit matjen në serverin tënd para se të bësh premtime kapaciteti.

## Rezultatet (20 llogari, 3 000 kërkesa për skenar, konkurrencë 32)
| Skenari | Rezultati |
|---|---|
| Pranim SMS, **një llogari** (një wallet i kyçur nga të gjitha kërkesat) | **55 req/s**, p50 573 ms, p95 694 ms, p99 809 ms, 0 gabime |
| Pranim SMS, 20 llogari | **79 req/s**, p50 406 ms, p95 611 ms, p99 689 ms, 0 gabime |
| Dërgimi nga radha (2 workers, provider fake) | **248 mesazhe/s** (6 000 mesazhe në 24 s) |
| Fushatë e vetme (1 500–3 000 marrës, kufi 10 000/min) | **≈ 40 marrës/s** (kufizohet nga hapat e mëposhtëm) |
| Integriteti pas 6 000 mesazhesh | 0 wallet inkonsistente, 0 mesazhe të ngecura, 0 dublikate idempotence |

Përkthim praktik: një host i vetëm pranon rreth **5 000 SMS/min** nga API-ja (≈ 7 milionë/ditë) dhe workers i dërgojnë shumë më shpejt sesa i pranon API-ja. Një llogari e vetme është vetëm ~30% më e ngadaltë se e shpërndara: kyçja e wallet-it (e domosdoshme për saktësinë e parave) nuk është pengesa.

## Ku shkon koha
Një pranim SMS bën 21 pyetje SQL (idempotencë, çelësat, plani, rrugë, sender, pëlqim, çmimi, wallet i kyçur, ledger, hold, mesazh, ngjarje). Sekuencialisht: ~24 ms me profilizim (~12–15 ms pa), nga të cilat vetëm ~8 ms janë në DB; pjesa tjetër është ORM/Python. Pra **kufizimi është CPU-ja e procesit API/worker**, jo bllokimet e PostgreSQL.

## Kufiri i njohur: shpejtësia e një fushate të vetme
Një fushatë përpunohet nga **një worker njëherësh** (kyçje `SKIP LOCKED`, me qëllim: buxheti dhe kufiri/min llogariten në një vend). Rreth 40 marrës/s ⇒ **~150 000 mesazhe/orë për fushatë**; një fushatë me 1 milion marrës zgjat ~7 orë (kufiri `rate_per_minute` max 10 000/min = 167/s mbetet tavani teorik). Fushata të ndryshme përpunohen paralelisht nga workers të ndryshëm. Nëse ky tavan bëhet i ngushtë, hapi tjetër është përpunimi paralel i marrësve të së njëjtës fushatë (copëza me `SKIP LOCKED` mbi rreshtat e marrësve) me buxhetin e rezervuar në mënyrë atomike; nuk është ndërtuar ende.

## Si të shkallëzosh (nga më e lira)
1. **Më shumë procese API**: `--workers N` te uvicorn (rreth 1 për vCPU); `docker-compose.prod.yml` nis 2.
2. **Më shumë workers SMS**: shto shërbime `worker` (të pavarur, të sigurt paralelisht me `SKIP LOCKED`; testuar në `tests/test_postgres.py`).
3. **PostgreSQL në host të veçantë** (ose i menaxhuar) dhe me `db_pool_size` sipas numrit të proceseve.
4. **Më shumë instanca API** pas nginx (gjendja është vetëm në DB; nuk ka sesione në memorie).
5. Kufijtë e provider-it (Twilio: shpejtësia për numër/messaging service) shpesh e vendosin tavanin e vërtetë: konfirmoji me ofruesin.

## Si ta ekzekutosh vetë
```bash
createdb sms_bench
export SMS_DATABASE_URL=postgresql+psycopg://…/sms_bench SMS_PII_HMAC_KEY=… SMS_SECRETS_KEY=…
alembic upgrade head
uvicorn app.main:app --port 8000 --workers 2 &          # API-ja që do të ngarkohet
python -m scripts.bench --accounts 20 --messages 3000 --concurrency 32 --workers 2
python -m scripts.bench_campaign 3000 10000              # shpejtësia e një fushate
```


## M1b: kostoja e dual-write (`SMS_ENTERPRISE_DUAL_WRITE`), matje krahasuese

Metodë: `scripts/bench_ab.py` (3 ON + 3 OFF, renditje e ndërthurur, të njëjtat të dhëna nga TEMPLATE PG),
`scripts/bench_probe.py` (SQL/op, mikro-matje), `scripts/bench_profile.py` (cProfile i ciklit të workerit).

**Gjetja e parë (para optimizimit):** drain i radhës (2 workers) ON 204.2 vs OFF 235.1 msg/s = **−13.1%**
(konsistent, CPU workeri +15.6%). `before_flush` hynte në rrugën e workerit 2×/mesazh në të dyja mënyrat,
por me ON `events.emit` shton një `Event` tenant-owned → `resolve_id` një herë/mesazh; sesion i ri për cikël
⇒ cache bosh ⇒ +1 SELECT ORM/mesazh. cProfile: `resolve_id` 1.47 s/1000 mesazhe (≈1.5 ms/thirrje, kryesisht
Python i ORM-it, jo pritja e DB).

**Optimizimi (i vetëm):** `enterprises._from_loaded_rows`: kur sesioni ka tashmë një rresht tenant-owned të
ruajtur me të njëjtin `owner_ref` dhe `enterprise_id` (Message që workeri sapo lexoi), merret prej tij pa
SELECT (invarianti `record.owner_ref == enterprise.owner_ref`); rreshtat me `owner_ref`/`enterprise_id` në
ndryshim shpërfillen. Pas tij: `resolve_id` 0.024 s/1000 mesazhe.

**Pas optimizimit (3+3):** drain ON 226.5 vs OFF 235.0 msg/s = **−3.6%** (stdev 10.6/5.9, diapazonet mbivendosen;
brenda pragut 5%), CPU workeri +3.4%, transaksione DB të barabarta; accept brenda zhurmës (A1 −2.3% më mirë me ON);
SQL/request tipik 23 → 23; SQL/mesazh workeri (sesion i ri/cikël) 8 → ~8.6 (mbetet rasti kur rreshti i ngarkuar
nuk ka `enterprise_id`); mikro: ON cold ≈ +0.5 ms/op (një SELECT), ON warm ≈ OFF.
`enterprises_audit --check --strict` = 0/0 pas `backfill_enterprise_id` mbi rreshtat e shkruar me OFF
(OFF nuk plotëson `enterprise_id`, sipas konceptit).


## M1c: kostoja e skopimit me `enterprise_id` (`SMS_TENANT_SCOPING=enterprise`, dual-write ON) kundrejt rikthimit (`owner_ref`, dual-write OFF)

Metodë: `scripts/bench_ab.py` (dataset identik nga TEMPLATE PG **me backfill** të `enterprise_id`, sepse skopimi është fail-closed), renditje e ndërthurur, plus matje të kontrolluara në të njëjtin proces.

| Matje | ON (M1c) | OFF (rikthim) | ON vs OFF |
|---|---|---|---|
| Drain radhe, 2 workers (msg/s, mesatare 3) | 211.4 | 217.6 | −2.9% |
| A1 accept, një wallet (req/s, 5+5 runs) | 51.9 | 53.6 | −3.1% |
| A2 accept, 20 llogari (req/s, 5+5 runs) | 76.5 | 78.1 | −2.1% (mediana −6.3%, OFF varion 67.7–84.1) |
| Request tipik në proces, 6 raunde të ndërthurura (ms) | 28.09 | 28.19 | −0.3% |
| SQL për `POST /v1/messages` | 23 | 24 | rikthimi bën 1 SELECT shtesë te `sms_enterprises` |

Një A/B i parë 3+3 dha A2 −7.7% (brenda 5–10%): u analizua me 10 runs të balancuara + matje në proces; nuk u riprodhua (shih tabelën), prandaj konsiderohet zhurmë e makinës (OFF ndryshon ±10% mes runs). Para/pas M1c me të njëjtin harnes: ON 226.5 → 211.4 msg/s por edhe OFF 235.0 → 217.6 (zhvendosje e përbashkët e makinës); raporti ON/OFF 0.964 → 0.972, pra M1c s'ka regres të vetin. Kufij: një makinë, provider `fake`.

## M2-b: `DispatchQueue` për SMS: para/pas (interleaved BASE/NEW, 5+5, dual-write ON, enterprise scoping)

| Matje | BASE (para) | NEW (pas) | NEW vs BASE |
|---|---|---|---|
| A1 accept, një wallet (req/s, mesatare) | 56.5 | 55.5 | −1.8% (mediana +1.8%) |
| A2 accept, 20 llogari (req/s) | 80.2 | 79.3 | −1.1% (mediana −0.4%) |
| CPU aplikacioni gjatë accept (s) | 12.7 | 12.8 | +1.0% |
| Drain, 2 workers (msg/s) | 224.8 | 223.1 | −0.8% (mediana +1.9%) |
| CPU workers gjatë drain (s) | 34.1 | 34.2 | +0.3% |
| SQL: submit+commit / process_one / retry | 21 / 8 / 8 | 21 / 8 / 8 | identik (100 statements, teksti i normalizuar identik) |

Runs të ndërthurura BASE/NEW (worktree i commit-it paraardhës), integriteti 0 probleme. Brenda pragut 5%.


## M2-c: email përmes `DispatchQueue`: para/pas (BASE/NEW të ndërthurura, 5+5, 1200 email/run, provider fake)

| Matje | BASE | NEW | NEW vs BASE |
|---|---|---|---|
| Submit email (/s, një commit për email) | 96.5 | 94.5 | −2.1% (mediana −1.4%) |
| Submit CPU (s) | 7.87 | 7.97 | +1.2% |
| Worker drain (email/s, sesion i ri/cikël, DKIM+MIME reale) | 40.7 | 40.8 | +0.3% (mediana −0.4%) |
| Worker CPU (s) | 21.8 | 21.7 | −0.6% |
| SQL submit / process_one / retry / fail / cancel | 10 / 9 / 9 / 9 / 6 | 10 / 9 / 9 / 9 / 6 | identik (93 statements, teksti identik) |
| Lidhja gjatë provider call | idle in transaction | idle in transaction | e pandryshuar (dokumentuar; mat 15 ms me fake) |

Brenda pragut 5%. Kufizime: një makinë, provider `fake`, pa SMTP real.

## Patch i transaksionit të email: para/pas (BASE/NEW të ndërthurura, 5+5, 1200 email/run, provider fake)

| Matje | BASE | NEW | NEW vs BASE |
|---|---|---|---|
| Submit email (/s) | 92.2 | 91.0 | −1.2% (mediana +2.5%) |
| Submit CPU (s) | 8.22 | 8.26 | +0.5% |
| Worker drain (email/s) | 40.2 | 41.0 | +2.2% (mediana +2.6%) |
| Worker CPU (s) | 22.2 | 21.7 | −2.1% |
| SQL submit / process_one / retry / fail / cancel | 10/9/9/9/6 | 10/9/9/9/6 | identik, e njëjta renditje (93 statements) |

Lidhja gjatë provider call (PostgreSQL, 16 workers njëkohësisht, provider që fle 0.3 s):

| | para | pas |
|---|---|---|
| Lidhje të pool-it të zëna (pool_size=10, overflow 10) | 16 | **0** |
| Sesione `idle in transaction` | 16 | **0** |

(Koha e mbajtjes së lidhjes për email në atë test sintetik përfshin konkurrimin e 16 thread-eve për GIL/CPU: 658 → 179 ms mesatarisht; nuk është matje e izoluar.) Brenda pragut 5%.
