#!/usr/bin/env bash
# build.sh — one-shot KROS image builder.
#
# Usage:
#   ./build.sh                      # build kros:dev (default tag)
#   ./build.sh v0.1.0               # build kros:v0.1.0
#   TAG=foo ./build.sh              # build kros:foo
#   IMAGE=kros/kros ./build.sh      # build kros/kros:dev
#
# Requirements: a working Docker daemon. Nothing else.

set -euo pipefail

# Resolve script dir so the script works no matter where it's invoked from.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- knobs (override via env or first positional argument) ---
IMAGE="${IMAGE:-kros}"
TAG="${1:-${TAG:-dev}}"
PLATFORM="${PLATFORM:-linux/amd64}"

FULL_TAG="${IMAGE}:${TAG}"

# --- preflight ---
if ! command -v docker >/dev/null 2>&1; then
    echo "ERROR: docker is not installed or not in PATH." >&2
    exit 1
fi

if ! docker info >/dev/null 2>&1; then
    echo "ERROR: docker daemon is not running. Start Docker Desktop / dockerd first." >&2
    exit 1
fi

# --- build ---
echo "==> Building ${FULL_TAG}  (platform=${PLATFORM})"
echo "    context: ${SCRIPT_DIR}"
echo

docker build \
    --platform "${PLATFORM}" \
    -t "${FULL_TAG}" \
    "${SCRIPT_DIR}"

echo
echo "==> Done. Image built:"
docker images --filter="reference=${FULL_TAG}" \
    --format 'table {{.Repository}}:{{.Tag}}\t{{.ID}}\t{{.Size}}\t{{.CreatedSince}}'

echo
echo "Try it:"
echo "    docker run --rm ${FULL_TAG} kros caps | head -20"
