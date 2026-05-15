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
#   docker build -t kros:dev ./image
#   docker build -t kros:dev --build-arg LIGHTPANDA_VERSION=nightly ./image

ARG PYTHON_VERSION=3.11
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
RUN apt-get update && apt-get install -y --no-install-recommends \
        bubblewrap \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Pull lightpanda binary from GitHub release. Only amd64 is published.
ARG LIGHTPANDA_VERSION
RUN curl -fsSL -o /usr/local/bin/lightpanda \
        "https://github.com/lightpanda-io/browser/releases/download/${LIGHTPANDA_VERSION}/lightpanda-x86_64-linux" \
    && chmod +x /usr/local/bin/lightpanda

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
ENV KROS_SYSTEM_PROMPT=/etc/kros/system-prompt.md

# /workspace is a conventional bind-mount target so agents can `cd` into
# caller-supplied files without colliding with kros internals under /app.
WORKDIR /workspace

ENTRYPOINT ["kros"]
CMD ["--help"]
