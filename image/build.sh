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
LIGHTPANDA_VERSION="${LIGHTPANDA_VERSION:-nightly}"

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

if ! command -v curl >/dev/null 2>&1; then
    echo "ERROR: curl is not installed on the host (needed to fetch lightpanda)." >&2
    exit 1
fi

# --- lightpanda host-side cache --------------------------------------------
# We download the lightpanda binary on the host (not inside `docker build`)
# because docker build doesn't inherit the host's HTTP(S)_PROXY env, which
# makes GitHub releases painfully slow or unreachable behind GFW. The host
# already has a working network path (browsers / proxies), so reuse it.
#
# Cache layout (kept inside the build context so COPY can see it):
#   image/.cache/lightpanda          binary
#   image/.cache/lightpanda.version  version stamp used for cache validation
# ---------------------------------------------------------------------------
CACHE_DIR="${SCRIPT_DIR}/.cache"
LIGHTPANDA_BIN="${CACHE_DIR}/lightpanda"
LIGHTPANDA_STAMP="${CACHE_DIR}/lightpanda.version"
LIGHTPANDA_URL="https://github.com/lightpanda-io/browser/releases/download/${LIGHTPANDA_VERSION}/lightpanda-x86_64-linux"

need_download=1
if [[ -f "${LIGHTPANDA_BIN}" && -f "${LIGHTPANDA_STAMP}" ]]; then
    cached_ver="$(cat "${LIGHTPANDA_STAMP}" 2>/dev/null || true)"
    if [[ "${cached_ver}" == "${LIGHTPANDA_VERSION}" ]]; then
        need_download=0
    fi
fi

if [[ "${need_download}" -eq 1 ]]; then
    mkdir -p "${CACHE_DIR}"
    echo "==> Fetching lightpanda (${LIGHTPANDA_VERSION}) on the host..."
    echo "    URL: ${LIGHTPANDA_URL}"
    echo "    (respects host HTTPS_PROXY / HTTP_PROXY env)"
    rm -f "${LIGHTPANDA_BIN}.tmp"
    curl -fL --progress-bar -o "${LIGHTPANDA_BIN}.tmp" "${LIGHTPANDA_URL}"
    chmod +x "${LIGHTPANDA_BIN}.tmp"
    mv "${LIGHTPANDA_BIN}.tmp" "${LIGHTPANDA_BIN}"
    echo "${LIGHTPANDA_VERSION}" > "${LIGHTPANDA_STAMP}"
    echo "    cached → ${LIGHTPANDA_BIN}"
else
    echo "==> lightpanda cache hit (${LIGHTPANDA_VERSION}) → ${LIGHTPANDA_BIN}"
fi
echo

# --- build ---
echo "==> Building ${FULL_TAG}  (platform=${PLATFORM})"
echo "    context: ${SCRIPT_DIR}"
echo

docker build \
    --platform "${PLATFORM}" \
    --build-arg "LIGHTPANDA_VERSION=${LIGHTPANDA_VERSION}" \
    -t "${FULL_TAG}" \
    "${SCRIPT_DIR}"

echo
echo "==> Done. Image built:"
docker images --filter="reference=${FULL_TAG}" \
    --format 'table {{.Repository}}:{{.Tag}}\t{{.ID}}\t{{.Size}}\t{{.CreatedSince}}'

echo
echo "Try it:"
echo "    docker run --rm ${FULL_TAG} kros caps | head -20"
