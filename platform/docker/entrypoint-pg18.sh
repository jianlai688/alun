#!/bin/sh
set -eu
if [ ! -f "$PGDATA/PG_VERSION" ]; then
  /usr/libexec/postgresql18/initdb -D "$PGDATA" --username=alun --auth=trust --encoding=UTF8
fi
grep -q "^listen_addresses = '\*'" "$PGDATA/postgresql.conf" 2>/dev/null || echo "listen_addresses = '*'" >> "$PGDATA/postgresql.conf"
grep -q "0.0.0.0/0 trust" "$PGDATA/pg_hba.conf" 2>/dev/null || echo "host all all 0.0.0.0/0 trust" >> "$PGDATA/pg_hba.conf"
/usr/libexec/postgresql18/pg_ctl -D "$PGDATA" -o "-p 5432" -w start
/usr/libexec/postgresql18/createdb -U alun alun 2>/dev/null || true
/usr/libexec/postgresql18/pg_ctl -D "$PGDATA" -m fast -w stop
exec /usr/bin/postgres18 -D "$PGDATA" -p 5432
