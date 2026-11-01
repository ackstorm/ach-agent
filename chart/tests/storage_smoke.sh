#!/usr/bin/env bash
# Runs the storage server's three containers as the chart wires them (shared /tmp, non-root,
# read-only root) in front of a stand-in bucket, then checks: create-only writes, replication to
# the bucket, eviction of a replicated object (SeaweedFS 4.48 never marks objects written through
# the server as replicated) and read-through, that only the namespace folder (MOUNT_PATH) is
# mounted, and the reconcile gauges.
# Usage: storage_smoke.sh s3|gcs. s3 stands in with a second SeaweedFS; gcs with fake-gcs-server,
# which the GCS Go SDK reaches through STORAGE_EMULATOR_HOST. The docker volume reports the host
# disk, so the built-in evictor gets a 0.99 threshold to keep it out of the explicit uncache check.
# Needs docker and runtime/.venv (boto3). Run before changing storage.image.
set -euo pipefail
cd "$(dirname "$0")/../.."
MODE=${1:?usage: storage_smoke.sh s3|gcs}
IMG=$(sed -n 's/^  image: *//p' chart/values.yaml | grep seaweedfs)
FAKE_GCS=fsouza/fake-gcs-server:1.52.2@sha256:d47b4cf8b87006cab8fbbecfa5f06a2a3c5722e464abddc0d107729663d40ec4
N=ach-storage-smoke
fail() { echo "FAIL ($MODE): $*" >&2; exit 1; }
cleanup() {
  docker rm -f smoke-remote smoke-ss smoke-sync smoke-loop >/dev/null 2>&1 || true
  docker volume rm -f smoke-tmp smoke-data >/dev/null 2>&1 || true
  docker network rm "$N" >/dev/null 2>&1 || true
}
trap cleanup EXIT
cleanup
retry() { # retry <tries> <command...>: every 2 s
  local n=$1; shift
  for _ in $(seq 1 "$n"); do "$@" >/dev/null 2>&1 && return 0; sleep 2; done
  return 1
}
netcurl() { docker run --rm --network "$N" --entrypoint curl "$IMG" -s -m 5 "$@"; }

docker network create "$N" >/dev/null
# grep without -q reads all input: an early exit would fail the pipeline under pipefail.
if [ "$MODE" = gcs ]; then
  docker run -d --name smoke-remote --network "$N" "$FAKE_GCS" -scheme http -port 4443 \
    -public-host smoke-remote:4443 >/dev/null
  retry 30 netcurl -f http://smoke-remote:4443/storage/v1/b || fail "fake GCS not up"
  netcurl -f -X POST 'http://smoke-remote:4443/storage/v1/b?project=smoke' \
    -H 'Content-Type: application/json' -d '{"name":"smoke"}' >/dev/null
  remote_env=(-e REMOTE_TYPE=gcs -e GCS_PROJECT=smoke -e GOOGLE_CLOUD_PROJECT=smoke
    -e STORAGE_EMULATOR_HOST=smoke-remote:4443)
  netcurl -f -X POST 'http://smoke-remote:4443/upload/storage/v1/b/smoke/o?uploadType=media&name=ach/other/x' \
    --data-binary x >/dev/null
  in_remote() { netcurl 'http://smoke-remote:4443/storage/v1/b/smoke/o?prefix=ach/ns/uid/ws/' |
    grep '"name":"ach/ns/uid/ws/registry.json"' >/dev/null; }
else
  docker run -d --name smoke-remote --network "$N" "$IMG" server -dir=/data -s3 -ip=smoke-remote -ip.bind=0.0.0.0 >/dev/null
  retry 60 docker exec smoke-remote curl -s -o /dev/null -m 2 http://127.0.0.1:8333/ || fail "stand-in S3 not up"
  printf '%s\n' 's3.configure -user=remote -access_key=remote -secret_key=remote-secret -actions=Admin -apply' \
    's3.bucket.create -name smoke' | docker exec -i smoke-remote weed shell -master=127.0.0.1:9333 >/dev/null
  remote_ip=$(docker inspect smoke-remote -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}')
  runtime/.venv/bin/python -c 'import sys, boto3; from botocore.config import Config; boto3.client("s3", endpoint_url=f"http://{sys.argv[1]}:8333", aws_access_key_id="remote", aws_secret_access_key="remote-secret", region_name="us-east-1", config=Config(s3={"addressing_style": "path"})).put_object(Bucket="smoke", Key="ach/other/x", Body=b"x")' "$remote_ip"
  remote_env=(-e REMOTE_TYPE=s3 -e AWS_REGION=us-east-1 -e REMOTE_ENDPOINT=http://smoke-remote:8333
    -e AWS_ACCESS_KEY_ID=remote -e AWS_SECRET_ACCESS_KEY=remote-secret)
  in_remote() { printf 'fs.ls /buckets/smoke/ach/ns/uid/ws\n' |
    docker exec -i smoke-remote weed shell -master=127.0.0.1:9333 | grep -x registry.json >/dev/null; }
fi

docker volume create smoke-tmp >/dev/null
docker volume create smoke-data >/dev/null
docker run --rm -v smoke-tmp:/tmp -v smoke-data:/data --entrypoint chown "$IMG" 1000:1000 /tmp /data
ident='{"identities":[{"name":"control","credentials":[{"accessKey":"ctl","secretKey":"ctl-secret"}],"actions":["Read","Write","List","Tagging"]}]}'
common=(--user 1000:1000 --read-only -v smoke-tmp:/tmp -e HOME=/tmp -e BUCKET=smoke -e MOUNT_PATH=ach/ns -e RECONCILE_INTERVAL=15
  "${remote_env[@]}")
docker run -d --name smoke-ss --network "$N" "${common[@]}" -v smoke-data:/data \
  --entrypoint sh "$IMG" -c "mkdir -p /tmp/s3 && printf '%s' '$ident' > /tmp/s3/identities.json && exec /usr/bin/weed -logtostderr=true server \
  -dir=/data -filer -s3 -s3.config=/tmp/s3/identities.json -ip=127.0.0.1 -ip.bind=0.0.0.0 \
  -metricsPort=9327 -volume.max=0 -master.volumeSizeLimitMB=256 -filer.remoteCacheEvictThreshold=0.99 \
  -volume.allowUntrustedRemoteEndpoints -filer.allowUntrustedRemoteEndpoints -s3.allowUntrustedRemoteEndpoints" >/dev/null
docker run -d --name smoke-loop --network container:smoke-ss "${common[@]}" -v "$PWD/chart/files/storage:/scripts:ro" \
  --entrypoint sh "$IMG" /scripts/reconcile.sh >/dev/null
docker run -d --name smoke-sync --network container:smoke-ss "${common[@]}" -v "$PWD/chart/files/storage:/scripts:ro" \
  --entrypoint sh "$IMG" /scripts/sync.sh >/dev/null
retry 90 docker exec smoke-loop test -f /tmp/mounted || { docker logs smoke-loop; fail "bucket never mounted"; }
printf 'fs.ls /buckets/smoke/ach\n' | docker exec -i smoke-ss weed shell -master=127.0.0.1:9333 | grep -x other >/dev/null &&
  fail "another namespace's keys were indexed"

ip=$(docker inspect smoke-ss -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}')
runtime/.venv/bin/python - "$ip" <<'PY'
import sys, boto3
from botocore.config import Config
c = boto3.client("s3", endpoint_url=f"http://{sys.argv[1]}:8333", aws_access_key_id="ctl",
                 aws_secret_access_key="ctl-secret", region_name="us-east-1",
                 config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 1}))
key = "ach/ns/uid/ws/registry.json"
c.put_object(Bucket="smoke", Key=key, Body=b'{"v": 1}', IfNoneMatch="*")
try:
    c.put_object(Bucket="smoke", Key=key, Body=b'{"v": 2}', IfNoneMatch="*")
    raise SystemExit("FAIL: a second create-only put succeeded")
except c.exceptions.ClientError as exc:
    assert exc.response["Error"]["Code"] in ("PreconditionFailed", "412"), exc.response
bad = boto3.client("s3", endpoint_url=f"http://{sys.argv[1]}:8333", aws_access_key_id="nobody",
                   aws_secret_access_key="nope", region_name="us-east-1",
                   config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 1}))
try:
    bad.get_object(Bucket="smoke", Key=key)
    raise SystemExit("FAIL: an unknown key read an object")
except bad.exceptions.ClientError:
    pass
print("create-only put and auth ok")
PY

retry 60 in_remote || { docker logs smoke-sync; fail "object never reached the stand-in bucket"; }
uncached() { printf 'remote.uncache -dir=/buckets/smoke/ach/ns\n' | docker exec -i smoke-ss weed shell -master=127.0.0.1:9333 | grep 'registry.json ... Done' >/dev/null; }
retry 30 uncached || fail "the replicated object cannot be evicted"
runtime/.venv/bin/python - "$ip" <<'PY'
import sys, boto3
from botocore.config import Config
c = boto3.client("s3", endpoint_url=f"http://{sys.argv[1]}:8333", aws_access_key_id="ctl",
                 aws_secret_access_key="ctl-secret", region_name="us-east-1",
                 config=Config(s3={"addressing_style": "path"}))
assert c.get_object(Bucket="smoke", Key="ach/ns/uid/ws/registry.json")["Body"].read() == b'{"v": 1}'
print("read-through after eviction ok")
PY

gauges() { docker exec smoke-ss curl -s -m 5 http://127.0.0.1:9328/metrics; }
start=$(gauges | sed -n 's/^ach_agent_storage_reconcile_ok_timestamp_seconds //p')
[ -n "$start" ] || fail "no reconcile gauges on :9328"
fresh() { [ "$(gauges | sed -n 's/^ach_agent_storage_reconcile_ok_timestamp_seconds //p')" -gt "$start" ]; }
retry 30 fresh || { docker logs smoke-loop; fail "the reconcile loop never compared with the bucket"; }
gauges | grep -x 'ach_agent_storage_unsynced_files 0' >/dev/null || fail "unexpected unsynced files: $(gauges)"
docker exec smoke-ss curl -s -m 5 http://127.0.0.1:9327/metrics | grep '^SeaweedFS_volumeServer_resource' >/dev/null ||
  fail "no SeaweedFS metrics on :9327"

# A restart serves again once the start-up upload is done.
docker exec smoke-loop rm -f /tmp/mounted
docker stop smoke-sync smoke-loop smoke-ss >/dev/null
docker start smoke-ss >/dev/null
docker start smoke-loop smoke-sync >/dev/null
retry 90 docker exec smoke-loop test -f /tmp/mounted || { docker logs smoke-loop; fail "not serving after a restart"; }
ip=$(docker inspect smoke-ss -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}')
runtime/.venv/bin/python - "$ip" <<'PY'
import sys, boto3
from botocore.config import Config
c = boto3.client("s3", endpoint_url=f"http://{sys.argv[1]}:8333", aws_access_key_id="ctl",
                 aws_secret_access_key="ctl-secret", region_name="us-east-1",
                 config=Config(s3={"addressing_style": "path"}))
assert c.get_object(Bucket="smoke", Key="ach/ns/uid/ws/registry.json")["Body"].read() == b'{"v": 1}'
print("restart ok")
PY
echo "PASS ($MODE)"
