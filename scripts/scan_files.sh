#!/usr/bin/env bash
set -euo pipefail

# Filter given args to YAML files and scan them using the Dockerized action.
# Usage: scripts/scan_files.sh [files...]

if [ "$#" -eq 0 ]; then
  echo "Usage: $0 <file1.yaml> [file2.yml ...]" >&2
  exit 2
fi

IMAGE_TAG=${IMAGE_TAG:-kubepolicy:local}
SEVERITY=${SEVERITY:-error}
POST_COMMENT=${POST_COMMENT:-false}
JSON_OUT=${JSON_OUT:-kubepolicy-summary.json}

files=()
for f in "$@"; do
  case "$f" in
    *.yaml|*.yml)
      if [ -f "$f" ]; then files+=("$f"); fi ;;
  esac
done

if [ ${#files[@]} -eq 0 ]; then
  echo "No YAML files to scan." >&2
  exit 0
fi

if ! docker image inspect "${IMAGE_TAG}" >/dev/null 2>&1; then
  echo "[scan] Building image ${IMAGE_TAG}"
  docker build -t "${IMAGE_TAG}" .
fi

echo "[scan] Scanning ${#files[@]} file(s)"
docker run --rm \
  -e INPUT_SEVERITY_THRESHOLD="${SEVERITY}" \
  -e INPUT_POST_PR_COMMENT="${POST_COMMENT}" \
  -e INPUT_INCLUDE_GLOB="**/*.yml,**/*.yaml" \
  -e INPUT_EXCLUDE_GLOB="" \
  -e KPB_FILE_GLOBS="${files[*]}" \
  -e KPB_JSON_OUTPUT="${JSON_OUT}" \
  -w /workspace \
  -v "$(pwd)":/workspace \
  "${IMAGE_TAG}"

echo "[scan] JSON summary: ${JSON_OUT}"

