#!/usr/bin/env bash
# Backup i PostgreSQL (format custom, i ngjeshur) me checksum dhe rotacion.
#   DATABASE_URL=postgresql://sms:...@host/sms BACKUP_DIR=/backups KEEP_DAYS=14 scripts/backup.sh
# Në compose:  docker compose -f docker-compose.prod.yml exec db sh -c \
#   'DATABASE_URL=postgresql://sms:$POSTGRES_PASSWORD@localhost/sms BACKUP_DIR=/backups /backup.sh'
# Vetëm-lexim: nuk ndryshon DB-në.
set -euo pipefail
: "${DATABASE_URL:?set DATABASE_URL}"
BACKUP_DIR="${BACKUP_DIR:-./backups}"
KEEP_DAYS="${KEEP_DAYS:-14}"
mkdir -p "$BACKUP_DIR"
umask 077
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
out="$BACKUP_DIR/sms-$stamp.dump"
tmp="$out.partial"
trap 'rm -f "$tmp"' EXIT
pg_dump --format=custom --no-owner --no-privileges --dbname="$DATABASE_URL" --file="$tmp"
# skedari bosh/të prerë nuk pranohen
pg_restore --list "$tmp" > /dev/null
mv "$tmp" "$out"
( cd "$BACKUP_DIR" && sha256sum "$(basename "$out")" > "$(basename "$out").sha256" )
find "$BACKUP_DIR" -name 'sms-*.dump*' -mtime +"$KEEP_DAYS" -delete
echo "OK $out ($(du -h "$out" | cut -f1))"
