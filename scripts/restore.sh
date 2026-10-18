#!/usr/bin/env bash
# Rikthen një backup (scripts/backup.sh) në një bazë të RE, kurrë mbi ekzistuesen (M11-I/F-05A).
#   ADMIN_URL=postgresql://postgres:...@host/postgres scripts/restore.sh backups/sms-enterprise-<kohë>.dump sms_restored
#
# Garanci:
#  - checksum-i `.sha256` është i detyrueshëm (pa të: dalje 4; mbivendosje e qartë me ALLOW_NO_CHECKSUM=1);
#  - etiketa (enterprise|central) merret nga PËRMBAJTJA e dump-it dhe, nëse ka, nga `.meta.json`; mospërputhje → dalje 5;
#    RESTORE_LABEL (opsionale) duhet të përputhet. Emri i bazës së re duhet të përputhet me etiketën: një dump Enterprise
#    nuk shkon te baza me "central" në emër, një dump Central kërkon "central" në emër;
#  - baza e re krijohet nga ky skript dhe, nëse restore dështon, FSHIHET prej tij (vetëm ajo që krijoi; KEEP_FAILED_RESTORE=1 e ruan për hetim);
#  - restore bëhet në një transaksion të vetëm; pas tij kontrollohet që revizioni alembic = ai i regjistruar në meta (kodi 6 nëse jo);
#  - ADMIN_URL futet si ndryshore libpq; asnjë URL/fjalëkalim nuk printohet.
# Kontrolli i mëtejshëm (kokë e migrimeve + ledger): scripts/recovery_verify.py dhe scripts/verify_ledger.py (shih docs).
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib_pg.sh
source "$here/lib_pg.sh"

dump="${1:?usage: restore.sh <dump> <new_db_name>}"
target="${2:?usage: restore.sh <dump> <new_db_name>}"
: "${ADMIN_URL:?set ADMIN_URL (lidhje me bazën 'postgres' me të drejtë CREATEDB)}"
[[ "$target" =~ ^[a-z][a-z0-9_]{0,62}$ ]] || { echo "invalid database name" >&2; exit 2; }
case "$target" in postgres|template0|template1) echo "refusing reserved database name" >&2; exit 2 ;; esac
[[ -f "$dump" ]] || { echo "dump not found: $dump" >&2; exit 2; }

pg_parse_url "$ADMIN_URL" || exit 2
unset ADMIN_URL
admin_db="$PGDATABASE"
[[ "$target" != "$admin_db" ]] || { echo "target equals the admin database: refusing" >&2; exit 3; }

# 1) integriteti i skedarit
if [[ -f "$dump.sha256" ]]; then
  ( cd "$(dirname "$dump")" && sha256sum --status -c "$(basename "$dump").sha256" ) \
    || { echo "checksum mismatch: dump is corrupted or modified" >&2; exit 4; }
elif [[ "${ALLOW_NO_CHECKSUM:-0}" == 1 ]]; then
  echo "WARNING: restoring without a checksum file (ALLOW_NO_CHECKSUM=1)" >&2
else
  echo "no checksum file next to the dump (set ALLOW_NO_CHECKSUM=1 only for a conscious exception)" >&2; exit 4
fi
pg_restore --list "$dump" > /dev/null || { echo "dump is unreadable or truncated" >&2; exit 4; }

# 2) etiketa nga përmbajtja (+ meta + RESTORE_LABEL) dhe emri i synuar
label="$(dump_label "$dump")"
[[ -n "$label" ]] || { echo "cannot tell Enterprise from Central in this dump (no/ambiguous version table)" >&2; exit 5; }
expected_rev=""
if [[ -f "$dump.meta.json" ]]; then
  meta_label="$(sed -n 's/^  "label": "\([a-z]*\)",$/\1/p' "$dump.meta.json")"
  [[ "$meta_label" == "$label" ]] || { echo "meta label '$meta_label' contradicts the dump content '$label'" >&2; exit 5; }
  expected_rev="$(sed -n 's/^  "alembic_revision": "\([^"]*\)",$/\1/p' "$dump.meta.json")"
fi
if [[ -n "${RESTORE_LABEL:-}" && "$RESTORE_LABEL" != "$label" ]]; then
  echo "RESTORE_LABEL '$RESTORE_LABEL' does not match the dump ('$label')" >&2; exit 5
fi
if [[ "$label" == enterprise && "$target" == *central* ]]; then echo "an Enterprise dump must not be restored into a Central-named database" >&2; exit 5; fi
if [[ "$label" == central && "$target" != *central* ]]; then echo "a Central dump needs a database name containing 'central'" >&2; exit 5; fi

# 3) baza e re (kurrë mbi ekzistuesen)
exists="$(psql -X -v ON_ERROR_STOP=1 -Atc "select 1 from pg_database where datname='$target'")"
[[ -z "$exists" ]] || { echo "database '$target' already exists: refusing to overwrite" >&2; exit 3; }
psql -X -v ON_ERROR_STOP=1 -qc "create database \"$target\""
created=1
cleanup() {
  local rc=$?
  if (( rc != 0 && created == 1 )) && [[ "${KEEP_FAILED_RESTORE:-0}" != 1 ]]; then
    PGDATABASE="$admin_db" psql -X -qc "drop database if exists \"$target\" with (force)" >/dev/null 2>&1 || true
    echo "restore failed (exit $rc): the partially created database '$target' was dropped" >&2
  fi
}
trap cleanup EXIT

# 4) restore atomik në bazën e re
PGDATABASE="$target" pg_restore --no-owner --no-privileges --exit-on-error --single-transaction --dbname="$target" "$dump"

# 5) kokë migrimesh e rikthyer = ajo e regjistruar në backup
vt="$(pg_version_table "$label")"
got="$(PGDATABASE="$target" pg_revision "$vt")"
if [[ -n "$expected_rev" && "$got" != "$expected_rev" ]]; then
  echo "alembic revision after restore ('$got') differs from the backup meta ('$expected_rev')" >&2; exit 6
fi
echo "OK restored into '$target' label=$label revision=$got. Next: python -m scripts.recovery_verify head --label $label (+ ledger verification, see docs/m11i-postgres-backup-restore-runbook.md)"
