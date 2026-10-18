#!/bin/sh
# Restore a database backup into a running stack:  ./restore.sh backups/atlas-20260101-030000.dump
# Stops Atlas, replaces the database contents, starts Atlas again. Test this before you need it.
set -eu
dump="${1:?usage: restore.sh <backup.dump>}"
[ -f "$dump" ] || { echo "no such file: $dump" >&2; exit 1; }
docker compose stop atlas caddy
docker compose exec -T db psql -U atlas -d postgres -c "DROP DATABASE IF EXISTS atlas WITH (FORCE)" -c "CREATE DATABASE atlas OWNER atlas"
docker compose exec -T db pg_restore -U atlas -d atlas --no-owner < "$dump"
docker compose up -d
echo "restored from $dump"
