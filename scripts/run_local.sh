#!/usr/bin/env bash
set -euo pipefail

# Simple local runner for KubePolicy PR Bot
# Usage: scripts/run_local.sh [glob ...]
# Example: scripts/run_local.sh "samples/**/*.yaml"

IMAGE_TAG=${IMAGE_TAG:-kubepolicy:local}
SEVERITY=${SEVERITY:-error}
POST_COMMENT=${POST_COMMENT:-false}
GLOBS=${*:-"samples/**/*.yaml"}

echo "[local] Building image ${IMAGE_TAG}"
docker build -t "${IMAGE_TAG}" .

echo "[local] Running scan for globs: ${GLOBS}"
docker run --rm \
  -e INPUT_SEVERITY_THRESHOLD="${SEVERITY}" \
  -e INPUT_POST_PR_COMMENT="${POST_COMMENT}" \
  -e INPUT_INCLUDE_GLOB="**/*.yml,**/*.yaml" \
  -e INPUT_EXCLUDE_GLOB="" \
  -e KPB_FILE_GLOBS="${GLOBS}" \
  -w /workspace \
  -v "$(pwd)":/workspace \
  "${IMAGE_TAG}"

