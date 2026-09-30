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
