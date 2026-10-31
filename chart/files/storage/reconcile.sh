#!/bin/sh
# Runs beside the SeaweedFS server in the ach-agent-storage pod. Points the server at the S3
# bucket and mounts it, then uploads the files the sync stream missed: a killed pod loses the
# change events of its last minute or two while the data stays on the volume. Exports two
# gauges on :9328.
# Env: BUCKET, REMOTE_TYPE (s3 | gcs), RECONCILE_INTERVAL; for s3 AWS_REGION and REMOTE_ENDPOINT,
# for gcs GCS_PROJECT. Credentials: the default AWS chain (Pod Identity, or AWS_ACCESS_KEY_ID/
# AWS_SECRET_ACCESS_KEY) for s3; Application Default Credentials (Workload Identity, or
# GOOGLE_APPLICATION_CREDENTIALS) for gcs.
set -u
DIR="/buckets/${BUCKET}"
METRICS=/tmp/metrics

# weed shell exits non-zero when its command fails.
weed_sh() { printf '%s\n' "$2" | timeout "$1" weed shell -master=127.0.0.1:9333; }

export_metrics() {
  cat > "${METRICS}.tmp" <<EOF
# HELP ach_agent_storage_unsynced_files Files created more than 5 minutes ago that are not in S3.
# TYPE ach_agent_storage_unsynced_files gauge
ach_agent_storage_unsynced_files $1
# HELP ach_agent_storage_reconcile_ok_timestamp_seconds Last successful comparison with S3, or the loop start.
# TYPE ach_agent_storage_reconcile_ok_timestamp_seconds gauge
ach_agent_storage_reconcile_ok_timestamp_seconds $2
EOF
  mv "${METRICS}.tmp" "$METRICS"
}

export_metrics 0 "$(date +%s)"
nc -lk -p 9328 -e /scripts/metrics.sh &

i=0
until curl -s -o /dev/null -m 2 http://127.0.0.1:8888/; do
  i=$((i + 1))
  [ "$i" -lt 90 ] || { echo "FAIL: filer not up after 180s" >&2; exit 1; }
  sleep 2
done

# For S3 the wasabi type uses the default AWS credential chain; the s3 type signs anonymously
# when it has no keys.
case "$REMOTE_TYPE" in
  gcs) configure="remote.configure -name=remote -type=gcs -gcs.projectId=${GCS_PROJECT}" ;;
  *) configure="remote.configure -name=remote -type=wasabi -wasabi.endpoint=${REMOTE_ENDPOINT} -wasabi.region=${AWS_REGION}" ;;
esac
weed_sh 60 "$configure" >/dev/null || exit 1
if ! weed_sh 60 "remote.mount" | grep -qF "\"${DIR}\":"; then
  weed_sh 1800 "remote.mount -dir=${DIR} -remote=remote/${BUCKET} -nonempty" || exit 1
fi
touch /tmp/mounted
echo "mounted ${DIR} on the ${REMOTE_TYPE} bucket ${BUCKET}"

weed_sh 3600 "remote.copy.local -dir=${DIR}" >/dev/null ||
  echo "start-up reconcile failed; retrying every ${RECONCILE_INTERVAL}s" >&2

while true; do
  sleep "$RECONCILE_INTERVAL"
  # Younger files are normally still on their way through filer.remote.sync.
  if out=$(weed_sh 1800 "remote.copy.local -dir=${DIR} -minAge=300 -dryRun=true"); then
    n=$(printf '%s\n' "$out" | sed -n 's/^Files to copy: //p')
    export_metrics "${n:-0}" "$(date +%s)"
    if [ "${n:-0}" -gt 0 ]; then
      echo "uploading ${n} files the sync stream missed"
      weed_sh 3600 "remote.copy.local -dir=${DIR} -minAge=300" >/dev/null || echo "reconcile upload failed" >&2
    fi
  else
    echo "reconcile: cannot compare with S3; retrying in ${RECONCILE_INTERVAL}s" >&2
  fi
done
