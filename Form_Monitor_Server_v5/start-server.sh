#!/bin/sh
set -eu
if [ "${FM_STORAGE_ROOT:-/var/data}" != /var/data ]; then
  echo 'This container expects FM_STORAGE_ROOT=/var/data.' >&2
  exit 1
fi
# A newly attached volume may be root-owned. The web process and browser run as pwuser.
install -d -m 700 -o pwuser -g pwuser /var/data
chown -R pwuser:pwuser /var/data
exec runuser -u pwuser -- gunicorn --config /app/gunicorn.conf.py 'server:create_app()'
