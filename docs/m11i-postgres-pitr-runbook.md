# M11-I/F-05A — PostgreSQL PITR (Point-In-Time Recovery): kërkesat dhe procedura

**Statusi:** dokumentim/kërkesa vetëm. **Asnjë PITR nuk është provuar** — repo-ja nuk e aktivizon WAL archiving dhe nuk pretendon PITR. `pg_dump`/`restore.sh` janë **backup logjik** (gjendja e një momenti), NUK janë PITR.
Prova reale (F-05B) është **BLOCKED — kërkohet infrastrukturë prodhimi**. Prodhimi mbetet BLOCKED.

## 1. Pse PITR (jo vetëm pg_dump)

- Pa PITR, humbja e të dhënave arrin deri në backup-un e fundit (deri 24 orë me cron natën).
- **Central është autoriteti i parave** (`commercial_ledger_entries`, grant-et). Nëse Central rikthehet pas kursorit të Enterprise, grant-et e humbura nuk mund të ri-emetohen me të njëjtin ID (`docs/M9_MONEY_AUDIT.md` §11, `M9_PRODUCTION_CHECKLIST.md` §1). Prandaj Central duhet **PITR/WAL ose replikë sinkrone** — bllokues prodhimi i dokumentuar që më parë.
- Enterprise ka ledger-in e vet të wallet-eve; PITR rekomandohet gjithashtu (RPO i vogël për para/mesazhe).

## 2. Dy rrugë (zgjedhja është vendim infrastrukture, ende e hapur)

| | Provider i menaxhuar PostgreSQL (PROPOSED si parazgjedhje) | PostgreSQL i vetë-menaxhuar |
|---|---|---|
| WAL archiving / base backup | nga provider-i (backup automatik + WAL i vazhdueshëm) | duhet ndërtuar (më poshtë) |
| Rikthim në kohë | funksion i provider-it: "restore to point in time" → instancë e RE | procedurë manuale (§4) |
| Retention i WAL | konfigurim provider-i (dritare PITR, p.sh. 7–35 ditë) | lifecycle i vetë-menaxhuar |
| Çfarë provohet | rikthim real në instancë të re + `recovery_verify` + `verify_ledger`/`recovery_ledger` | e njëjta + arkivimi i WAL |
| Rreziku kryesor | varësi nga provider-i; kontrolloni që dritarja PITR mbulon RPO/RTO | gabime konfigurimi, WAL i humbur pa alarm |

Edhe me provider të menaxhuar duhet **provë e vetë ne** (drill në instancë të re), jo vetëm pohimi i provider-it.

## 3. Cilësimet e kërkuara (vetë-menaxhuar) — referencë, JO të aktivizuara në repo

| Parametër | Vlera / shënim |
|---|---|
| `wal_level` | `replica` (ose më i lartë) |
| `archive_mode` | `on` (kërkon rinisje) |
| `archive_command` | komandë që kopjon segmentin WAL në ruajtje off-host të enkriptuar dhe **dështon me kod ≠ 0** nëse s'e arkivon (p.sh. `pgBackRest archive-push`, `wal-g wal-push`, ose `test ! -f … && cp …` vetëm për provë). Asnjëherë `/bin/true`. |
| `archive_timeout` | p.sh. 60–300 s: kufizon RPO-n kur trafiku është i ulët (PROPOSED) |
| `max_wal_senders` | ≥ 2 nëse përdoret `pg_basebackup`/replikë |
| `restore_command` | e kundërta e arkivimit: tërheq segmentin e kërkuar (`pgBackRest archive-get`, `wal-g wal-fetch`, ose `cp /archive/%f %p`) |
| `recovery_target_time` | koha UTC e synuar (me zonë kohore të shprehur), p.sh. `'2026-11-02 14:05:00+00'` |
| `recovery_target_action` | `pause` (parazgjedhje e sigurt: inspekto para `pg_wal_replay_resume()`/promovimit) ose `promote` për drill të automatizuar |
| `recovery_target_inclusive` | `on`/`off` sipas nevojës (dokumento zgjedhjen) |
| `recovery_target_timeline` | `latest` (parazgjedhje në PG ≥ 12) |
| Metoda e base backup | `pg_basebackup` ose pgBackRest/WAL-G (backup fizik) — **ndryshe nga `pg_dump`** |
| Lifecycle i WAL/base backup | mbaj WAL nga base backup-i më i vjetër i ruajtur deri tani; fshij vetëm pas verifikimit të zinxhirit |
| Monitorim | alarm kur `pg_stat_archiver.failed_count` rritet ose `last_archived_time` është e vjetër; alarm kur ruajtja e WAL mbushet |

**Të dyja bazat logjike (Enterprise + Central) ndajnë të njëjtin cluster fizik nëse janë në të njëjtin server** — PITR rikthen gjithë cluster-in në të njëjtën kohë; për rikthim të pavarur duhen cluster-a të veçantë (vendim arkitekture, i hapur). Nëse rikthehen në kohë të ndryshme, vlen semantika `cursor_ahead` e §11 të `M9_MONEY_AUDIT.md`.

## 4. Procedura e rikthimit (vetë-menaxhuar; parim, jo komandë prodhimi)

1. **Mos prek cluster-in aktiv.** Rikthe në një instancë/host të RI e të izoluar (pa akses nga aplikacionet).
2. Merr base backup-un më të ri **para** kohës së synuar; vendose në data directory të instancës së re.
3. Krijo `recovery.signal`; vendos `restore_command`, `recovery_target_time`, `recovery_target_action = 'pause'` te `postgresql.auto.conf`.
4. Nise instancën; ndiq log-un deri sa raporton arritjen e objektivit (`recovery stopping before/after …`).
5. **Validimi i kohës së arritur:** regjistro koha_e_kërkuar dhe koha_e_arritur (`select pg_last_xact_replay_timestamp()`); arritja duhet të jetë ≤ kërkuarës dhe brenda kufirit të WAL-it (më e afërta e mundshme). Regjistro edhe `pg_is_in_recovery()`.
6. Verifiko: `recovery_verify head`, `verify_ledger` (Enterprise), `python -m apps.central.tools.recovery_ledger` (Central) në bazën e rikthyer; krahaso rreshta të njohur (p.sh. një pagesë e bërë para dhe një pas kohës së synuar: e para duhet të ekzistojë, e dyta jo).
7. Vetëm pas kontrollit: `pg_wal_replay_resume()`/promovim dhe vendimi i veçantë i kalimit të aplikacionit (ndalo `api`/workers; ndrysho URL-në; nis; monitoro `cursor_ahead`).
8. Fshi instancën e provës dhe të dhënat (përmbajnë PII/para).

**Abort:** WAL i munguar në zinxhir; koha e arritur nuk përputhet; kokat/ledger-i dështojnë; verifikimi nuk është i pastër → mos kalo aplikacionin; ruaj instancën për hetim, jo për prodhim.

## 5. Çfarë mund të provohet LOKALISHT tani dhe çfarë JO

| Pika | Lokalisht (F-05A) | Prodhim (F-05B) |
|---|---|---|
| Backup logjik + restore + kokë + ledger | **VERIFIED** (drill, CI) | EXTERNAL |
| WAL archiving real off-host | jo (nuk aktivizohet) | EXTERNAL |
| PITR real me kohë të synuar | jo — **pg_dump nuk është PITR** | EXTERNAL |
| Alarm për arkivim të dështuar | jo | EXTERNAL |
| Matje reale RPO/RTO | jo | EXTERNAL |

Një provë PITR e izoluar lokale (cluster i përkohshëm në Docker me `archive_mode=on` në disk lokal) është e mundur dhe e sigurt, por **nuk është dëshmi prodhimi** dhe nuk është e përfshirë në F-05A për të mos krijuar përshtypjen e gabuar të gatishmërisë PITR; mund të shtohet më vonë si ushtrim i dokumentuar nëse kërkohet.

## 6. Dëshmia e kërkuar për ta konsideruar PITR të provuar

Shih `docs/m11i-recovery-evidence-checklist.md` (seksioni F-05B): konfigurim aktiv i arkivimit, base backup, një rikthim real në kohë të caktuar me kohë të kërkuar/arritur të regjistruara dhe firmë, për të dyja bazat.
