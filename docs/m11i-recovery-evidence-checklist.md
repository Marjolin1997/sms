# M11-I/F-05 — Evidenca e rikuperimit: çfarë është provuar dhe çfarë mbetet

Dy pjesë të ndara me qëllim: **F-05A** (repo + drill lokal/staging-grade) dhe **F-05B** (dëshmi reale prodhimi). Gatishmëria operacionale e prodhimit mbetet **BLOCKED**.

## 1. Analiza e boshllëqeve (para → pas F-05A)

Klasat: VERIFIED · PARTIAL · MISSING · PRODUCTION-ONLY.

| # | Kërkesa | Para F-05A | Pas F-05A | Shënim |
|---|---|---|---|---|
| A | Krijimi i backup-it | PARTIAL (një skript gjenerik, vetëm Enterprise në CI) | **VERIFIED** | Enterprise + Central, `BACKUP_LABEL` i detyrueshëm |
| B | Konsistenca e backup-it | PARTIAL (`--list` nuk e kap të dhënën e prerë në mes) | **VERIFIED** | pg_dump transaksional + TOC + lexim i plotë; testuar |
| C | Kompresimi | VERIFIED (custom format) | VERIFIED | |
| D | Enkriptim në prehje | MISSING | **PRODUCTION-ONLY** | kërkesë e storage-it (dokumentuar §7 runbook); nuk bëhet nga skriptet |
| E | Retention | PARTIAL (14 ditë lokale) | PARTIAL → propozim | tiering = POLICY PROPOSAL; zbatimi = storage prodhimi |
| F | Rotacioni | PARTIAL (pa etiketë; `sms-*` përzien bazat) | **VERIFIED** | rotacion për etiketë, 3 skedarë së bashku |
| G | Procedura e restore | VERIFIED (një DB) | **VERIFIED** | runbook i ri |
| H | Restore në DB të pastër | VERIFIED | **VERIFIED** | + pastrim i bazës së dështuar, transaksion i vetëm |
| I | Kokë migrimesh pas restore | MISSING | **VERIFIED** | `recovery_verify head` + kontroll te `restore.sh` (kod 6) |
| J | Integritet financiar/ledger | PARTIAL (vetëm Enterprise) | **VERIFIED** | `verify_ledger` + `apps.central.tools.recovery_ledger` |
| K | Restore i Central DB | MISSING | **VERIFIED** (drill + CI) | |
| L | Restore i Enterprise DB | PARTIAL (CI round-trip) | **VERIFIED** | |
| M | Përkufizim RPO | MISSING | **PROPOSED** (§3) | politikë, jo e arritur |
| N | Përkufizim RTO | MISSING | **PROPOSED** (§3) | politikë, jo e arritur |
| O | WAL archiving | MISSING | **PRODUCTION-ONLY** | cilësimet e kërkuara të dokumentuara |
| P | Procedura PITR | MISSING (vetëm "bllokues") | PARTIAL → dokumentuar | `m11i-postgres-pitr-runbook.md`; e pa-provuar |
| Q | Validim kohe objektiv PITR | MISSING | **PRODUCTION-ONLY** | kohë e kërkuar/e arritur e regjistruar |
| R | Zbulim korrupsioni | PARTIAL (sha256 + `--list`) | **VERIFIED** | checksum i detyrueshëm + TOC + lexim i plotë + restore atomik |
| S | Off-host/off-site | PRODUCTION-ONLY | PRODUCTION-ONLY | |
| T | Kontroll aksesi te backup-et | PARTIAL (`umask 077`) | PARTIAL → kërkesa të dokumentuara | IAM/storage = prodhim |
| U | Trajtimi i sekreteve | PARTIAL (URL në argv; `restore.sh` printonte URL-në me fjalëkalim) | **VERIFIED** | ndryshore libpq; asnjë URL te log/meta; testuar |
| V | Kadenca e drill-it | PARTIAL (doc "mujore", pa pronar) | PROPOSED | drill-i tani është një komandë; kadenca = politikë |
| W | Vëzhgim/alarm për backup të dështuar | PARTIAL (plan I-05, i pazbatuar) | PRODUCTION-ONLY | kodi del ≠ 0 dhe printon `OK …`; alarmi real = infrastrukturë |
| X | Pronësia / on-call | MISSING | **PRODUCTION-ONLY** | DBA NUK ËSHTË CAKTUAR |

Gjetjet konkrete të rregulluara në F-05A: (1) `restore.sh` printonte `SMS_DATABASE_URL=…` me fjalëkalim në dalje; (2) `pg_dump`/`pg_restore` merrnin URL-në me fjalëkalim si argument; (3) emri `sms-*.dump` i njëjtë për të dyja bazat (rrezik restore/rotacion i përzier); (4) asnjë kontroll i kokës së migrimeve ose i ledger-it të Central pas restore; (5) restore i dështuar linte bazë të gjysmë; (6) checksum-i mungues ishte vetëm paralajmërim; (7) `--list` i vetëm nuk kap të dhëna të prera pas TOC-së; (8) koment i vjetër `/backup.sh` në kontejner (rruga nuk ekziston në compose) hiqet.

## 2. Çfarë është VERIFIED lokalisht/në CI (F-05A)

Provat: `tests/test_f05_recovery.py` (12 teste), drill `python -m scripts.recovery_drill`, round-trip i `migrations-backup` në CI.

- [x] Backup Enterprise dhe Central me checksum + meta (pa kredenciale)
- [x] Restore në bazë të re për të dyja; burimi i fshirë para restore (pa varësi)
- [x] Kokë migrimesh: Enterprise 0034, Central 0031 = koka e kodit
- [x] Ledger: Enterprise (`verify_ledger`), Central (`apps.central.tools.recovery_ledger`); gishti i bazës = identik me burimin
- [x] Rrugë dështimi: backup ≠ 0 kur `pg_dump` dështon/DB e paarritshme; etiketë e gabuar (të dyja drejtimet); checksum i ndryshuar; dump i prerë; pa checksum; target ekzistues; Enterprise↔Central i refuzuar; restore i dështuar fshin vetëm bazën e vet; verifikim kokë/ledger/gisht dështon kur baza është e prishur
- [x] Rotacion vetëm brenda etiketës; asnjë bazë e përkohshme e mbetur pas drill-it

## 3. POLICY PROPOSAL — RPO/RTO (jo fakte të arritura)

| | Propozim | Vlerësim realist për platformën | Çfarë e provon |
|---|---|---|---|
| RPO Enterprise | ≤ 15 min | realist **vetëm me PITR**; me pg_dump ditor RPO = deri 24 orë | WAL archiving real + rikthim në kohë |
| RPO Central (para/ledger) | ≤ 5 min (synim: ≈ 0 me replikë sinkrone) | M9 e kërkon RPO≈0 për grant-et; realist me PITR + `archive_timeout` të ulët ose replikë sinkrone | e njëjta + test `cursor_ahead` |
| RTO (secila bazë) | ≤ 60 min (synim 30) | dump i vogël restore-ohet në sekonda (drill lokal ~10 s/bazë); koha e prodhimit varet nga madhësia, rrjeti, kalimi i aplikacionit | matje në drill real me volum real |
| Kalimi i aplikacionit | brenda RTO | hap manual (ndalo, ndrysho URL, nis, monitoro) | drill me kronometër |

**Asnjë nga këto nuk deklarohet e arritur.** Drill-i lokal provon korrektësinë e procedurës, jo RPO/RTO të prodhimit.

## 4. F-05B — dëshmia që mungon (BLOCKED, kërkon infrastrukturë reale)

Çdo rresht mbetet EXTERNAL EVIDENCE REQUIRED derisa të ketë server prodhimi/staging real:

- [ ] PostgreSQL real i vendosur (rol aplikacioni jo-superuser, TLS) për Enterprise dhe Central
- [ ] WAL archiving aktiv off-host (ose PITR i provider-it) — dëshmi konfigurimi + `pg_stat_archiver`
- [ ] Base backup + një **rikthim real në kohë të caktuar** në instancë të veçantë: koha e kërkuar vs. e arritur e regjistruar; të dyja bazat
- [ ] Backup real i kryer → kopjuar off-host (madhësi + sha256 të krahasuara) → enkriptim në prehje i provuar
- [ ] Restore i vërtetë nga storage-i off-host në bazë të re + `head` + ledger PASS (firmosur nga një person)
- [ ] Alarm i provokuar për backup të dështuar (OB-04) dhe për arkivim WAL të dështuar
- [ ] Pronari DBA/on-call i caktuar; kadencë drill-i mujor me firmë
- [ ] RPO/RTO të matur në drill real; vendim mbi tiering-un e retention-it dhe afatin ligjor të faturave
- [ ] Drill me sekretet (`SMS_SECRETS_KEY`, …) të rikthyer nga vendi i tyre i veçantë
- [ ] Vendim arkitekture: një cluster për të dyja bazat apo dy cluster-a (ndikon rikthimin e pavarur)

## 5. Statusi

- **M11-I/F-05A:** LOCAL/STAGING RECOVERY READINESS VERIFIED — PRODUCTION PITR EVIDENCE STILL REQUIRED
- **M11-I/F-05B:** BLOCKED — REAL PRODUCTION INFRASTRUCTURE REQUIRED
- **Production operational readiness:** BLOCKED
