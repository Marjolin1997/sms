#!/usr/bin/env bash
# Backup logjik i PostgreSQL (pg_dump format custom, i ngjeshur) për NJË bazë: Enterprise ose Central (M11-I/F-05A).
#   BACKUP_LABEL=enterprise|central DATABASE_URL=postgresql://sms:...@host/db BACKUP_DIR=/backups KEEP_DAYS=14 scripts/backup.sh
#
# Çfarë garanton:
#  - etiketa DUHET të përputhet me bazën burim (tabela e versioneve: sms_alembic_version = enterprise, central_alembic_version = central);
#    një bazë Enterprise nuk bëhet kurrë "backup Central" dhe anasjelltas;
#  - skedari validohet plotësisht (TOC + lexim i të gjithë të dhënave) para se të marrë emrin përfundimtar; skedar i prerë/korruptuar → dalje ≠ 0;
#  - krijohen: <dump>, <dump>.sha256 dhe <dump>.meta.json (etiketë, kohë, madhësi, sha256, revizioni alembic, versione PG; ASNJË kredencial/URL/host);
#  - rotacioni prek vetëm skedarët e së njëjtës etiketë; pa lidhje me Docker ose rrugë lokale të fiksuara;
#  - kredencialet futen vetëm si ndryshore libpq (jo te argumentet e proceseve) dhe asnjëherë nuk printohen.
# Vetëm-lexim ndaj bazës burim. Ky NUK është PITR (shih docs/m11i-postgres-pitr-runbook.md).
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib_pg.sh
source "$here/lib_pg.sh"

label="${BACKUP_LABEL:?set BACKUP_LABEL=enterprise|central}"
: "${DATABASE_URL:?set DATABASE_URL}"
BACKUP_DIR="${BACKUP_DIR:-./backups}"
KEEP_DAYS="${KEEP_DAYS:-14}"
[[ "$KEEP_DAYS" =~ ^[0-9]+$ ]] || { echo "KEEP_DAYS must be a non-negative integer" >&2; exit 2; }
vt="$(pg_version_table "$label")" || exit 2
other_vt="$(pg_version_table "$(pg_other_label "$label")")"

pg_parse_url "$DATABASE_URL" || exit 2
unset DATABASE_URL
src_db="$PGDATABASE"

# mbrojtja nga përzierja e bazave: burimi duhet të jetë i etiketës së kërkuar dhe jo i tjetrës
pg_has_table "$vt" || { echo "source database is not a '$label' database ('$vt' missing): refusing to back it up as '$label'" >&2; exit 5; }
if pg_has_table "$other_vt"; then
  echo "source database also contains '$other_vt': ambiguous Enterprise/Central database, refusing" >&2; exit 5
fi
revision="$(pg_revision "$vt")"

mkdir -p "$BACKUP_DIR"
umask 077
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
base="sms-$label-$stamp.dump"
out="$BACKUP_DIR/$base"
tmp="$out.partial"
trap 'rm -f "$tmp" "$out.sha256.partial" "$out.meta.json.partial"' EXIT

pg_dump --format=custom --no-owner --no-privileges --file="$tmp"
# skedarë bosh/të prerë/të korruptuar nuk pranohen: TOC + lexim i plotë i të dhënave (jo vetëm --list)
pg_restore --list "$tmp" > /dev/null
pg_restore --file=/dev/null "$tmp"
[[ "$(dump_label "$tmp")" == "$label" ]] || { echo "dump content does not match label '$label'" >&2; exit 5; }

sha="$(sha256sum "$tmp" | cut -d' ' -f1)"
bytes="$(wc -c < "$tmp" | tr -d ' ')"
server_version="$(psql -X -Atc 'show server_version')"
dump_version="$(pg_dump --version | sed 's/^pg_dump (PostgreSQL) //')"
mv "$tmp" "$out"
printf '%s  %s\n' "$sha" "$base" > "$out.sha256.partial"
mv "$out.sha256.partial" "$out.sha256"
cat > "$out.meta.json.partial" <<JSON
{
  "format": "sms-backup-meta/1",
  "label": "$label",
  "created_utc": "$stamp",
  "file": "$base",
  "bytes": $bytes,
  "sha256": "$sha",
  "source_database": "$src_db",
  "version_table": "$vt",
  "alembic_revision": "$revision",
  "server_version": "$server_version",
  "pg_dump_version": "$dump_version",
  "kind": "logical-snapshot (pg_dump); NOT point-in-time"
}
JSON
mv "$out.meta.json.partial" "$out.meta.json"

# rotacion: vetëm e njëjta etiketë; dump + .sha256 + .meta.json së bashku
find "$BACKUP_DIR" -maxdepth 1 -name "sms-$label-*.dump*" -mtime +"$KEEP_DAYS" -delete
echo "OK $out ($(du -h "$out" | cut -f1)) label=$label revision=$revision sha256=${sha:0:16}…"
