#!/usr/bin/env bash
# Backup diário do banco local. Cron: 30 23 * * * /opt/nexosix/deploy/linux/backup.sh
set -euo pipefail
source /opt/nexosix/.env
dest=/var/backups/nexosix
mkdir -p "$dest"
pg_dump --dbname="$DATABASE_URL" --format=custom --file="$dest/nexosix-$(date +%Y%m%d-%H%M).dump"
find "$dest" -name 'nexosix-*.dump' -mtime +14 -delete
