#!/usr/bin/env bash
# Regenerate the synthetic dataset. Needs Docker and uv; no Java on the host.
#
# Synthea runs in a container with no network, from a jar pinned by version and checksum.
# The same inputs give the same data, so the committed files only change when an input
# below changes. Run `data/synthea/generate.sh --check` to regenerate into a scratch
# directory and compare it with the committed manifest instead of overwriting anything.
set -euo pipefail

SYNTHEA_VERSION="v4.0.0"
SYNTHEA_JAR_SHA256="ed43c20ad40ba5c3bc724503a5af032715fe3c491620b766148e7c2361e6ecc1"
SYNTHEA_JAR_URL="https://github.com/synthetichealth/synthea/releases/download/${SYNTHEA_VERSION}/synthea-with-dependencies.jar"
JRE_IMAGE="eclipse-temurin@sha256:cff19e6215689161eb6162c11b86b0c60ddf802164f2eaf48d570f8fb79a36c5"

# Inputs that decide the data. Change one and the dataset changes.
SEED="20260901"
CLINICIAN_SEED="20260901"
REFERENCE_DATE="20260901"   # yyyymmdd: "today" for the simulation
POPULATION="20"             # living patients; deaths during the simulation come on top

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
cache="${XDG_CACHE_HOME:-$HOME/.cache}/clinical-copilot"
jar="$cache/synthea-${SYNTHEA_VERSION}.jar"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

mode="write"
if [[ "${1:-}" == "--check" ]]; then
  mode="check"
fi

mkdir -p "$cache"
if [[ ! -f "$jar" ]] || ! echo "${SYNTHEA_JAR_SHA256}  $jar" | sha256sum --check --status; then
  echo "Downloading Synthea ${SYNTHEA_VERSION}"
  curl --fail --silent --show-error --location --output "$jar" "$SYNTHEA_JAR_URL"
  echo "${SYNTHEA_JAR_SHA256}  $jar" | sha256sum --check --status \
    || { echo "Synthea jar checksum mismatch; refusing to use it" >&2; rm -f "$jar"; exit 1; }
fi

mkdir -p "$work/raw"
chmod 777 "$work/raw"
docker run --rm --network none --memory 1536m --cpus 2 \
  --user "$(id -u):$(id -g)" --tmpfs /tmp \
  -v "$jar:/synthea.jar:ro" \
  -v "$here/synthea.properties:/synthea.properties:ro" \
  -v "$work/raw:/out" -w /out \
  "$JRE_IMAGE" \
  java -Xmx1g -Duser.timezone=UTC -Duser.language=en -Duser.country=US \
  -jar /synthea.jar \
  -s "$SEED" -cs "$CLINICIAN_SEED" -r "$REFERENCE_DATE" -p "$POPULATION" \
  -c /synthea.properties \
  --exporter.baseDirectory=/out \
  > "$work/synthea.log" 2>&1 \
  || { tail -20 "$work/synthea.log" >&2; exit 1; }

target="$here"
if [[ "$mode" == "check" ]]; then
  target="$work/prepared"
fi
mkdir -p "$target"
(cd "$repo/api" && uv run --frozen python -m app.fhir_seed prepare "$work/raw/fhir" "$target")

if [[ "$mode" == "check" ]]; then
  if diff --brief "$target/MANIFEST.sha256" "$here/MANIFEST.sha256" > /dev/null; then
    echo "Committed dataset matches a fresh generation."
  else
    echo "Committed dataset differs from a fresh generation." >&2
    exit 1
  fi
fi
