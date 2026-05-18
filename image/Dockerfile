# syntax=docker/dockerfile:1.7
#
# Kros in-image CLI — Linux/amd64 only.
#
# Runtime layout:
#   /app/.venv              Python virtualenv with kros + PyPI deps.
#   /usr/local/bin/kros     Entrypoint (symlinked from the venv).
#   /usr/local/bin/lightpanda
#                           Headless browser binary used by `kros browser`.
#   /usr/bin/bwrap          Unprivileged sandbox runtime used by `kros sandbox`.
#   /workspace              Default CWD; mount a host dir here for I/O.
#
# Build:
#   ./image/build.sh                                # recommended; handles host-side lightpanda cache
#   ./image/build.sh v0.1.0                         # build kros:v0.1.0
#   LIGHTPANDA_VERSION=nightly ./image/build.sh     # pin a specific lightpanda release
#
# NOTE: this Dockerfile expects `.cache/lightpanda` to already exist in the
# build context. build.sh fetches it on the host (where HTTP(S)_PROXY works);
# `docker build` itself no longer reaches GitHub. If you must invoke
# `docker build` directly, fetch the binary yourself first:
#   curl -fL -o image/.cache/lightpanda \
#     https://github.com/lightpanda-io/browser/releases/download/nightly/lightpanda-x86_64-linux

ARG PYTHON_VERSION=3.12
ARG LIGHTPANDA_VERSION=nightly

# ---------------------------------------------------------------------------
# Stage 1 — builder: resolve deps and install kros into a clean venv.
# ---------------------------------------------------------------------------
FROM --platform=linux/amd64 python:${PYTHON_VERSION}-slim-bookworm AS builder

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=0 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# build-essential is kept in the builder in case any transitive wheel is
# missing a manylinux build and needs to compile from sdist.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Pull uv from its official image — pinned by digest-less :latest is fine
# inside a pinned builder stage; bump when we need a newer uv.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Copy the project metadata first. README.md is referenced by pyproject.toml
# (readme = "README.md") so the build fails without it.
COPY pyproject.toml README.md ./

# pyproject.toml's [tool.uv.sources] section points seeka / dokapy at
# sibling working copies (../../seeka-ai, ../../doka-ai) that only exist
# on the maintainer's laptop. Strip that section so uv resolves those
# two packages from PyPI instead. [project].dependencies stays intact.
RUN python3 -c "import re, pathlib; p=pathlib.Path('pyproject.toml'); t=p.read_text(); t=re.sub(r'(?ms)^\[tool\.uv\.sources\].*?(?=^\[|\Z)', '', t); p.write_text(t)"

COPY src ./src

# Single install: venv + kros + all PyPI deps.
# Aliyun mirror is the default per pyproject's [[tool.uv.index]]; we
# pass it explicitly here to stay robust even if uv stops honoring it.
RUN uv venv /app/.venv \
    && uv pip install --python /app/.venv/bin/python \
        --index-url https://mirrors.aliyun.com/pypi/simple/ \
        --extra-index-url https://pypi.org/simple/ \
        .

# ---------------------------------------------------------------------------
# Stage 2 — runtime: minimal image with bubblewrap, lightpanda, and the venv.
# ---------------------------------------------------------------------------
FROM --platform=linux/amd64 python:${PYTHON_VERSION}-slim-bookworm AS runtime

# bubblewrap: backs `kros sandbox`.
# ca-certificates: outbound HTTPS for memory/browser.
# curl: only used by this stage to fetch the lightpanda binary; kept
# because `kros browser` tooling and user troubleshooting benefits from it.
# tini: PID 1 zombie reaper. `kros shell spawn` detaches tasks from the
# kros CLI's session, so when those tasks exit they would otherwise
# linger as <defunct> under PID 1 (see design/04 §8). tini reaps them.
RUN apt-get update && apt-get install -y --no-install-recommends \
        bubblewrap \
        ca-certificates \
        curl \
        tini \
    && rm -rf /var/lib/apt/lists/*

# Pull lightpanda binary, pre-fetched on the host by build.sh into
# `.cache/lightpanda` (see top-of-file note). We COPY instead of curl-ing
# from inside the build because `docker build` doesn't inherit the host's
# HTTP(S)_PROXY env, and GitHub releases over the bare network can be
# unreasonably slow / unreachable.
ARG LIGHTPANDA_VERSION
COPY .cache/lightpanda /usr/local/bin/lightpanda
RUN chmod +x /usr/local/bin/lightpanda

# Bring the fully-populated venv over in one layer.
COPY --from=builder /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    KROS_SANDBOX_RUNTIME=bubblewrap \
    KROS_BROWSER_LIGHTPANDA_BIN=/usr/local/bin/lightpanda

# ---------------------------------------------------------------------------
# Agent bootstrap — dual-channel capability discovery.
#
# Channel 1 (主动推): /etc/profile.d/kros.sh fires on first shell open, so
#   any agent naturally sees kros exists without prior knowledge.
# Channel 2 (被动拉): /etc/kros/system-prompt.md + `kros caps` command, for
#   frameworks that explicitly inject capabilities into system prompts.
# ---------------------------------------------------------------------------
COPY kros-motd.sh /etc/profile.d/kros.sh
RUN mkdir -p /etc/kros \
    && kros caps > /etc/kros/system-prompt.md \
    && echo '. /etc/profile.d/kros.sh' >> /etc/bash.bashrc
ENV KROS_SYSTEM_PROMPT=/etc/kros/system-prompt.md \
    BASH_ENV=/etc/profile.d/kros.sh

# /workspace is a conventional bind-mount target so agents can `cd` into
# caller-supplied files without colliding with kros internals under /app.
WORKDIR /workspace

ENTRYPOINT ["/usr/bin/tini", "--", "kros"]
CMD ["--help"]
