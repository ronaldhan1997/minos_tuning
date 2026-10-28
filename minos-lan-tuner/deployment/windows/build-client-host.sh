#!/bin/sh
# Stage the worker and build deployment/windows/dist/MinosClient.exe.
set -eu
TUNER="$(CDPATH= cd -- "$(dirname "$0")/../.." && pwd)"
ROOT="$TUNER"
while [ ! -f "$ROOT/start-miner.sh" ] && [ "$ROOT" != "/" ]; do
  ROOT="$(dirname "$ROOT")"
done
STAGE="$(mktemp -d /tmp/minos-client-src.XXXXXX)"
OUT="$TUNER/deployment/windows/dist"
mkdir -p "$OUT"
COMMIT="$(git -C "$ROOT" rev-parse HEAD)"
trap 'rm -rf "$STAGE"' EXIT

mkdir -p \
  "$STAGE/minos-lan-tuner/apps/worker" \
  "$STAGE/minos-lan-tuner/shared" \
  "$STAGE/minos-lan-tuner/integrations/minos" \
  "$STAGE/templates" \
  "$STAGE/utils" \
  "$STAGE/deployment/windows"

cp "$TUNER/apps/__init__.py" "$STAGE/minos-lan-tuner/apps/"
cp "$TUNER/apps/worker/"*.py "$STAGE/minos-lan-tuner/apps/worker/"
cp "$TUNER/shared/"*.py "$STAGE/minos-lan-tuner/shared/"
cp "$TUNER/integrations/__init__.py" "$STAGE/minos-lan-tuner/integrations/"
cp "$TUNER/integrations/minos/"*.py "$STAGE/minos-lan-tuner/integrations/minos/"
cp "$ROOT/templates/__init__.py" "$ROOT/templates/gatk.py" \
  "$ROOT/templates/tool_params.py" "$ROOT/templates/_common.py" \
  "$STAGE/templates/"
cp "$ROOT/utils/__init__.py" "$ROOT/utils/scoring.py" \
  "$ROOT/utils/scoring_version.py" "$STAGE/utils/"
cp "$TUNER/deployment/windows/pyinstaller.spec" \
  "$TUNER/deployment/windows/build-client.sh" \
  "$STAGE/deployment/windows/"
printf 'COMMIT = "%s"\n' "$COMMIT" > "$STAGE/minos-lan-tuner/apps/worker/frozen_commit.py"

docker run --rm \
  -v "$STAGE":/src \
  -v "$OUT":/out \
  -v /tmp:/tmp \
  --entrypoint bash \
  tobix/pywine:3.11 \
  -lc "sh /src/deployment/windows/build-client.sh"

echo "Built $OUT/MinosClient.exe from commit $COMMIT"
