# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
#
# Family office portal image. Pattern follows homelab-infra's root Dockerfile:
# DHI -dev builder (has a shell and apt) and the matching DHI runtime, so the
# virtualenv's interpreter path is identical in both stages.

# =============================================================================
# Stage 1: builder
# =============================================================================
# Source: dhi.io/python:3.12-debian13-dev -> ghcr.io/byronwilliamscpa/dhi-python:3.12-debian13-dev
FROM ghcr.io/byronwilliamscpa/dhi-python@sha256:fe90645fdf287796e54df8b16360094e0001c607f7105ce3c15f026a6bcf2a3b AS builder

WORKDIR /app

# curl fetches the Tailwind standalone CLI below. The builder stage is
# discarded, so apt version pinning is intentionally omitted (DL3008).
# hadolint ignore=DL3008
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.8.17@sha256:e4644cb5bd56fdc2c5ea3ee0525d9d21eed1603bccd6a21f887a938be7e85be1 /uv /usr/local/bin/uv

# Tailwind CSS standalone CLI (no Node.js). Checksum from the release's
# sha256sums.txt; bump both together.
ARG TAILWIND_VERSION=v4.1.13
ARG TAILWIND_SHA256=b9ed9f8f640d3323711f9f68608aa266dff3adbc42e867c38ea2d009b973be11
RUN curl -fsSL -o /usr/local/bin/tailwindcss \
      "https://github.com/tailwindlabs/tailwindcss/releases/download/${TAILWIND_VERSION}/tailwindcss-linux-x64" \
    && echo "${TAILWIND_SHA256}  /usr/local/bin/tailwindcss" | sha256sum -c - \
    && chmod +x /usr/local/bin/tailwindcss

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app
COPY templates ./templates
COPY static ./static
RUN tailwindcss -i static/css/input.css -o static/css/output.css --minify

# =============================================================================
# Stage 2: runtime (distroless, no shell)
# =============================================================================
# Source: dhi.io/python:3.12-debian13 -> ghcr.io/byronwilliamscpa/dhi-python:3.12-debian13
FROM ghcr.io/byronwilliamscpa/dhi-python@sha256:a5425367554af74a6894e94ed3e98fee761973235307fc92438850163bf5f200

LABEL org.opencontainers.image.title="Family Office Portal"
LABEL org.opencontainers.image.description="Private read-only family estate portal"
LABEL org.opencontainers.image.source="https://github.com/ByronWilliamsCPA/family-office-portal"
LABEL org.opencontainers.image.licenses="MIT"

WORKDIR /app

COPY --from=builder --chown=65532:65532 /app/.venv /app/.venv
COPY --from=builder --chown=65532:65532 /app/app ./app
COPY --from=builder --chown=65532:65532 /app/templates ./templates
COPY --from=builder --chown=65532:65532 /app/static ./static

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app

# The SQLite volume (/data) must be writable by this UID on the host.
USER 65532:65532

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health', timeout=2).status == 200 else 1)"]

# One worker: the APScheduler jobs must run in exactly one process so there is
# a single SQLite writer (see app/db.py).
# #CRITICAL: security: --forwarded-allow-ips trusts X-Forwarded-* only from
# Docker's private range, where Traefik runs. #VERIFY: narrow this once the
# homelab traefik_proxy network has a pinned subnet; never set it to "*".
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips=172.16.0.0/12"]
