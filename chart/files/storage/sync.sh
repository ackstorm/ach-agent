#!/bin/sh
# Writes local changes back to S3 once reconcile.sh has mounted the bucket. filer.remote.sync
# keeps its offset in the filer and resumes from it after a restart.
i=0
until [ -f /tmp/mounted ]; do
  i=$((i + 1))
  [ "$i" -lt 900 ] || { echo "FAIL: bucket not mounted after 1800s" >&2; exit 1; }
  sleep 2
done
exec weed filer.remote.sync -filer=127.0.0.1:8888 -dir="/buckets/${BUCKET}"
