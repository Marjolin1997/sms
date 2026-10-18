# M11-I/F-05A — PostgreSQL: backup, restore dhe verifikim (Enterprise + Central)

**Statusi:** F-05A (repo, drill lokal/staging-grade) = i verifikuar lokalisht dhe në CI. F-05B (dëshmi reale prodhimi, PITR) = **BLOCKED — kërkohet infrastrukturë e vërtetë** (shih `docs/m11i-recovery-evidence-checklist.md`).
Gatishmëria operacionale e prodhimit mbetet **BLOCKED**. Asgjë këtu nuk është provuar kundër një serveri prodhimi.

Fjalor klasifikimi (si te dokumentet e tjera M11-I): VERIFIED = provuar nga kodi/CI/drill-i; PROPOSED = politikë e propozuar, jo fakt; EXTERNAL EVIDENCE REQUIRED = vetëm infrastruktura e vërtetë e provon.

## 1. Arkitektura e backup-it

- **Dy baza logjike të veçanta, pa lidhje DB mes tyre:** Enterprise (`sms`, tabela e versioneve `sms_alembic_version`) dhe Central (`sms_central`, tabela `central_alembic_version`). Mund të jenë në të njëjtin server PostgreSQL, por backup-et, restore-t dhe verifikimet janë **të ndara**.
- **Lloji:** `pg_dump --format=custom` (i ngjeshur, snapshot koherent i një transaksioni). Është backup **logjik**, jo PITR: rikthen gjendjen e momentit të backup-it, asgjë më të re (shih `docs/m11i-postgres-pitr-runbook.md`).
- **Mjetet (në repo):**

| Mjeti | Roli |
|---|---|
| `scripts/backup.sh` | backup i një baze (`BACKUP_LABEL=enterprise\|central`), validim, checksum, meta, rotacion |
| `scripts/restore.sh` | restore vetëm në bazë të RE, me kontrolle etiketë/checksum/kokë |
| `scripts/recovery_verify.py` | `head` (kokë migrimesh), `fingerprint`/`compare` (gishti i DB-së); nuk importon kurrë `apps.*` |
| `apps/central/tools/recovery_ledger.py` | ledger tregtar i Central pas restore (mjet i Central, vetëm-lexim; ekzekutohet edhe brenda imazhit Central) |
| `scripts/verify_ledger.py` | integriteti i parave të Enterprise (balanca = SUM(delta), faturat pa boshllëqe) |
| `scripts/recovery_drill.py` | drill i plotë i përkohshëm për të dyja bazat (seed → backup → burimi i fshirë → restore → verifikim → pastrim) |
| `scripts/lib_pg.sh` | funksione të përbashkëta (URL → ndryshore libpq; etiketa nga përmbajtja) |

## 2. Backup

```bash
BACKUP_LABEL=enterprise DATABASE_URL='postgresql://app_backup:…@host:5432/sms'         BACKUP_DIR=/var/backups/sms KEEP_DAYS=14 scripts/backup.sh
BACKUP_LABEL=central    DATABASE_URL='postgresql://app_backup:…@host:5432/sms_central' BACKUP_DIR=/var/backups/sms KEEP_DAYS=14 scripts/backup.sh
```

Garanci (të testuara te `tests/test_f05_recovery.py`):

1. **Etiketa duhet të përputhet me bazën burim.** Një bazë Enterprise nuk mund të bëhet backup "central" dhe anasjelltas (kod 5, asnjë artefakt).
2. **Validim i plotë para emrit përfundimtar:** TOC (`pg_restore --list`) + lexim i plotë i të dhënave (`pg_restore --file=/dev/null`). Skedar i prerë/korruptuar → dalje ≠ 0, asnjë skedar i mbetur (`.partial` fshihet).
3. **Tre artefakte për backup:** `sms-<etiketa>-<UTC>.dump`, `….dump.sha256`, `….dump.meta.json` (etiketa, kohë, madhësi, sha256, bazë burim, revizioni alembic, versionet PG; **asnjë kredencial/URL/host**). Të tria me leje `0600`.
4. **Kredencialet:** URL-ja shpërbëhet në ndryshore libpq (`PGPASSWORD` etj.) — fjalëkalimi nuk shfaqet te argumentet e proceseve dhe asnjë mesazh nuk printon URL. Përdor rol backup **jo-superuser, vetëm-lexim** (`pg_read_all_data`) kur të ketë infrastrukturë (PROPOSED).
5. **Rotacioni** (`KEEP_DAYS`, parazgjedhje 14) prek vetëm skedarët e së njëjtës etiketë dhe i heq të tre skedarët së bashku.
6. **Dështim = kod ≠ 0** (cron/alarm duhet ta shohë): `pg_dump` i dështuar, bazë e paarritshme, etiketë e gabuar.

Planifikimi (PROPOSED): cron i përditshëm/orar për çdo etiketë, `>> /var/log/sms-backup.log`; pa rresht `OK … label=<etiketa>` brenda dritares → alarm S2 (shih `docs/m11i-observability-alerting-plan.md` I-05). **Alarmi real është EXTERNAL EVIDENCE REQUIRED.**

## 3. Restore (gjithmonë në bazë të re)

```bash
ADMIN_URL='postgresql://admin:…@host:5432/postgres' scripts/restore.sh /var/backups/sms/sms-enterprise-<UTC>.dump sms_restored
ADMIN_URL='postgresql://admin:…@host:5432/postgres' scripts/restore.sh /var/backups/sms/sms-central-<UTC>.dump    sms_central_restored
```

Rregullat (të testuara):

- `.sha256` është i detyrueshëm (kod 4 nëse mungon ose nuk përputhet; `ALLOW_NO_CHECKSUM=1` vetëm si përjashtim i vetëdijshëm, me paralajmërim).
- Dump i prerë/i palexueshëm → kod 4, **nuk krijohet asnjë bazë**.
- **Etiketa nga përmbajtja** e dump-it (jo nga emri i skedarit): Enterprise ≠ Central. Mospërputhje me `.meta.json` ose `RESTORE_LABEL` → kod 5. Një dump Enterprise nuk shkon në emër me `central`; një dump Central kërkon `central` në emër.
- Baza e synuar **nuk duhet të ekzistojë** (kod 3); `postgres`, `template0/1` dhe baza e adminit refuzohen. Nuk ka mënyrë "mbishkruaj".
- Restore bëhet në një **transaksion të vetëm** (`--single-transaction --exit-on-error`). Nëse dështon, baza e re që krijoi skripti **fshihet** (vetëm ajo; `KEEP_FAILED_RESTORE=1` e ruan për hetim).
- Pas restore: revizioni alembic i bazës së re duhet të jetë ai i regjistruar në `.meta.json` (kod 6 nëse jo).
- Restore **nuk e prek kurrë bazën aktive**. Kalimi i aplikacionit te baza e re është hap i veçantë, i vetëdijshëm (ndalo `api`/workers, ndrysho emrin te URL, nis), vetëm pasi kalon kontrolli §4.

## 4. Lista e verifikimit pas restore (të dyja bazat)

Ekzekuto me URL-në e bazës SË RIKTHYER:

```bash
# Enterprise
export SMS_DATABASE_URL='postgresql+psycopg://…/sms_restored'
python -m scripts.recovery_verify head --label enterprise   # revizioni = koka(t) e kodit
python -m scripts.verify_ledger                              # wallets/ledger/faturat
# Central
export CENTRAL_DATABASE_URL='postgresql+psycopg://…/sms_central_restored'
python -m scripts.recovery_verify head --label central
python -m apps.central.tools.recovery_ledger               # ledger tregtar + invariantet e faturimit; money_sequence nuk është pas ledger-it
```

- [ ] sha256 u verifikua (restore.sh e bën), etiketa dhe kokat përputhen.
- [ ] Ledger Enterprise: `checked N wallets, M invoices: OK`.
- [ ] Ledger Central: `checked N credit accounts, M ledger entries: OK` (balanca nga ledger-i; asnjë llogari me fonde/`available_to_grant` negative; `money_sequence` ≥ seq-i më i lartë i lëshuar; invariantet `billing_closure` pa FAIL).
- [ ] (opsionale, për provë: burimi ende ekziston) `recovery_verify fingerprint` në të dyja bazat + `compare` = IDENTICAL (rreshta për tabelë, trigger-at e pandryshueshmërisë, indekset, kufizimet, sekuencat, revizioni).
- [ ] Numrat e rreshtave/të dhëna përfaqësuese përputhen me pritshmërinë e biznesit.
- [ ] Kontrolli i Central pas rikthimit të vetëm Central **pa** Enterprise: kursori i Enterprise mund të jetë përpara Central (`cursor_ahead`, `money_feed_broken`) — shih `docs/M9_MONEY_AUDIT.md` §11; kjo është arsyeja pse Central kërkon PITR/RPO≈0 (shih PITR runbook).

## 5. Drill (lokal ose staging, pa prodhim)

```bash
ADMIN_URL='postgresql://sms:…@127.0.0.1:5432/postgres' python -m scripts.recovery_drill
```

Për Enterprise dhe Central: baza burim e përkohshme (`f05drill_*`) → migrim → seed përfaqësues (Enterprise: `seed_demo` me wallet/ledger/fatura; Central: enterprise, produkt, llogari, pagesë e miratuar, grant) → gisht burimi → `backup.sh` → **fshirje e burimit** → `restore.sh` në bazë të re → `head` + ledger + gisht **identik** → pastrim. Prek vetëm bazat që krijon vetë. E njëjta rrugë ekzekutohet në CI (`tests/test_f05_recovery.py` në shard-et PG; round-trip i vogël te `migrations-backup`).

Rezultati lokal i regjistruar në F-05A: `DRILL OK` për të dyja bazat, ~21 s (Enterprise 67 tabela, Central i migruar në 0031).

## 6. Politika e retention (PROPOSED — vendim biznesi/ligjor i hapur)

| Nivel | Propozim | Shënim |
|---|---|---|
| Ditore (lokale) | 14 ditë (`KEEP_DAYS`, ekziston) | disk lokal NUK është backup i vetëm |
| Ditore off-host | 30 ditë | objekt-storage i enkriptuar, lifecycle rule |
| Javore | 8 javë | |
| Mujore | 12 muaj | |
| Fatura/ledger | **vendim ligjor i hapur** (M9: "ruaj gjithçka") | retention i backup-eve ≠ retention i të dhënave |
| Dump-e restore-i provë | fshihen pas drill-it | përmbajnë të dhëna reale PII/para |

Kjo është politikë e propozuar; rotacioni në repo zbaton vetëm nivelin "ditore lokale". Nivelet e tjera janë konfigurim i storage-it të prodhimit (EXTERNAL).

## 7. Siguria dhe aksesi (kërkesa)

- Backup-et përmbajnë PII dhe para: ruhen **të enkriptuara në prehje** (server-side encryption me çelës të menaxhuar/KMS ose enkriptim në klient) dhe në transit (TLS). **Enkriptimi nuk bëhet nga skriptet** — është kërkesë e storage-it të prodhimit (EXTERNAL EVIDENCE REQUIRED). Dosja lokale: `0700`/skedarë `0600`.
- Akses minimal: roli i backup-it vetëm-lexim; roli i restore-it me `CREATEDB` vetëm në mjedisin e restore-it; asnjë njeri/shërbim aplikacioni nuk ka të dyja. Kopje off-host me kredencial të veçantë shkrimi-vetëm (append-only/object lock — PROPOSED).
- `SMS_SECRETS_KEY`, `SMS_PII_HMAC_KEY`, çelësi Ed25519 dhe `CENTRAL_AUTH_SECRET` ruhen **veçmas** nga backup-et e DB-së (`docs/m11i-secrets-and-access-plan.md`); pa to, kolonat e enkriptuara të rikthyera nuk lexohen. Provo drill-in edhe me sekretet e rikthyera nga vendi i tyre i veçantë.
- Skriptet nuk shkruajnë kredenciale në log, meta ose dalje; dështimet printojnë kodin dhe arsyen, jo URL-në.

## 8. Kushte abort / rikthim

Abort (asnjë ndryshim në prodhim) nëse: checksum nuk përputhet; `restore.sh` del ≠ 0; kokat e migrimeve nuk përputhen; `verify_ledger` ose `apps.central.tools.recovery_ledger` raporton FAIL; gishti ndryshon; etiketa është e paqartë. Restore nuk ndryshon kurrë bazën aktive, kështu rikthimi nga një drill i dështuar është thjesht fshirja e bazës së përkohshme. `alembic downgrade` NUK është mjet rikthimi në prodhim (RUNBOOK §3).

## 9. Çfarë mbetet e bllokuar derisa të ketë infrastrukturë prodhimi

Shih `docs/m11i-recovery-evidence-checklist.md`: PostgreSQL real, WAL archiving/PITR, off-host + enkriptim, alarm backup-i, pronari/on-call, drill mujor real me firmë, RPO/RTO të provuar.
