#!/usr/bin/env bash
# Nightly: copy /data/energy.db to /backup/energy-YYYY-MM-DD.db, check it, keep the newest 7.
# Other files on the card (e.g. the pre-SSD energy.db) are never touched.
set -euo pipefail
dst=/backup/energy-$(date +%F).db
rm -f "$dst.tmp"
sqlite3 -readonly /data/energy.db ".backup '$dst.tmp'"
sqlite3 "$dst.tmp" "pragma journal_mode=delete" >/dev/null   # one self-contained file, no -wal
check=$(sqlite3 "$dst.tmp" "pragma integrity_check")
[ "$check" = ok ] || { echo "integrity check failed: $check"; rm -f "$dst.tmp"; exit 1; }
mv -f "$dst.tmp" "$dst"
echo "$dst: $(sqlite3 "$dst" 'select count(*) from readings') rows"
find /backup -maxdepth 1 -name 'energy-20??-??-??.db' | sort | head -n -7 | xargs -r rm -f --
