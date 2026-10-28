#!/bin/sh
# Build deployment/ubuntu/dist/MinosClient, the same window as the Windows exe.
set -eu
TUNER="$(CDPATH= cd -- "$(dirname "$0")/../.." && pwd)"
ROOT="$TUNER"
while [ ! -f "$ROOT/start-miner.sh" ] && [ "$ROOT" != "/" ]; do
  ROOT="$(dirname "$ROOT")"
done
STAGE="$(mktemp -d /tmp/minos-client-src.XXXXXX)"
OUT="$TUNER/deployment/ubuntu/dist"
WORK="$(mktemp -d /tmp/minos-client-work.XXXXXX)"
mkdir -p "$OUT"
COMMIT="$(git -C "$ROOT" rev-parse HEAD)"
trap 'rm -rf "$STAGE" "$WORK"' EXIT

mkdir -p \
  "$STAGE/minos-lan-tuner/apps/worker" \
  "$STAGE/minos-lan-tuner/shared" \
  "$STAGE/minos-lan-tuner/integrations/minos" \
  "$STAGE/templates" \
  "$STAGE/utils" \
  "$STAGE/deployment/ubuntu"

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
cp "$TUNER/deployment/ubuntu/pyinstaller.spec" "$STAGE/deployment/ubuntu/"
printf 'COMMIT = "%s"\n' "$COMMIT" > "$STAGE/minos-lan-tuner/apps/worker/frozen_commit.py"

PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"
"$PYTHON" -m PyInstaller --noconfirm --clean \
  --distpath "$OUT" \
  --workpath "$WORK" \
  "$STAGE/deployment/ubuntu/pyinstaller.spec"
chmod +x "$OUT/MinosClient"
echo "Built $OUT/MinosClient from commit $COMMIT"
