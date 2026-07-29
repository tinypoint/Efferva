# syntax=docker/dockerfile:1.7

ARG VIBE_TRADING_REVISION=0e004d7a98097a01751c5dd6adafa1a3afa3817d

FROM alpine:3.22 AS source
ARG VIBE_TRADING_REVISION
RUN apk add --no-cache ca-certificates git
RUN git clone --filter=blob:none --no-checkout \
        https://github.com/HKUDS/Vibe-Trading.git /src \
    && git -C /src checkout --detach "${VIBE_TRADING_REVISION}"
COPY examples/vibe-trading-gcp/integration.patch /tmp/integration.patch
RUN git -C /src apply --unidiff-zero /tmp/integration.patch
COPY examples/vibe-trading-gcp/overlay/ /src/

FROM node:22-slim AS frontend-build
WORKDIR /app/frontend
COPY --from=source /src/frontend/package.json /src/frontend/package-lock.json ./
RUN npm ci --ignore-scripts
COPY --from=source /src/frontend/ ./
RUN npm run build

FROM python:3.11-slim AS python-build
ARG EFFERVA_WHEEL_URL
RUN apt-get update \
    && apt-get install --yes --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*
ENV VIRTUAL_ENV=/opt/venv
ENV PATH="${VIRTUAL_ENV}/bin:${PATH}"
RUN python -m venv "${VIRTUAL_ENV}"
WORKDIR /app
COPY --from=source /src/agent/requirements.txt agent/requirements.txt
COPY --from=source /src/requirements-lock.txt requirements-lock.txt
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --require-hashes -r requirements-lock.txt
COPY --from=source /src/pyproject.toml /src/LICENSE /src/README.md ./
COPY --from=source /src/agent/ agent/
RUN test -n "${EFFERVA_WHEEL_URL}" \
    && pip install --no-cache-dir \
        "efferva[opensandbox] @ ${EFFERVA_WHEEL_URL}" \
        "google-auth>=2.40,<3" \
    && pip install --no-cache-dir -e .

FROM python:3.11-slim AS app
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:${PATH}" \
    VIBE_TRADING_AGENT_RUNTIME=efferva
RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        fonts-dejavu-core \
        libcairo2 \
        libfontconfig1 \
        libgdk-pixbuf-2.0-0 \
        libharfbuzz0b \
        libpango-1.0-0 \
        libpangoft2-1.0-0 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=python-build /opt/venv /opt/venv
COPY --from=source /src/pyproject.toml /src/LICENSE /src/README.md ./
COPY --from=source /src/agent/ agent/
COPY --from=frontend-build /app/frontend/dist frontend/dist
RUN useradd --create-home --shell /usr/sbin/nologin vibe \
    && useradd --system --no-create-home --shell /usr/sbin/nologin \
        --uid 10001 vibe-sandbox \
    && mkdir -p \
        agent/runs \
        agent/sessions \
        agent/uploads \
        agent/.swarm/runs \
        /home/vibe/.codex \
        /home/vibe/.vibe-trading \
    && chown -R vibe:vibe /app /home/vibe
USER vibe
EXPOSE 8899
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8899/live')"
CMD ["vibe-trading", "serve", "--host", "0.0.0.0", "--port", "8899"]

FROM app AS sandbox
USER root
RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        ca-certificates \
        curl \
        git \
        ripgrep \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /workspace \
    && chown vibe:vibe /workspace
USER vibe
WORKDIR /workspace
CMD ["sleep", "infinity"]
