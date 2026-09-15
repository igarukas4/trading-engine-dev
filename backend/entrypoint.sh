#!/bin/sh
set -eu

if [ -n "${DATABASE_PASSWORD_FILE:-}" ] && [ -r "$DATABASE_PASSWORD_FILE" ]; then
  install -o app -g app -m 0400 "$DATABASE_PASSWORD_FILE" /tmp/postgres_password
  export DATABASE_PASSWORD_FILE=/tmp/postgres_password
fi

exec su -s /bin/sh app -c 'exec "$@"' sh "$@"
