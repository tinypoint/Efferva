# syntax=docker/dockerfile:1.7

FROM python:3.13-slim-bookworm AS runtime

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates curl git \
    && rm -rf /var/lib/apt/lists/* \
    && install -d -m 0777 /var/lib/efferva/codex
COPY Efferva/dist/docker/efferva-*.whl /tmp/wheels/
RUN pip install --no-cache-dir /tmp/wheels/*.whl \
    && rm -rf /tmp/wheels \
    && python -c \
        "from efferva import locate_runtime_binary, runtime_build_info; assert locate_runtime_binary().is_file(); assert runtime_build_info() is not None"

ENV CODEX_HOME=/var/lib/efferva/codex \
    PYTHONUNBUFFERED=1
EXPOSE 8080
ENTRYPOINT ["efferva"]
CMD ["--host", "0.0.0.0", "--port", "8080"]

FROM runtime AS app

FROM node:22-bookworm-slim AS sandbox

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        bash \
        ca-certificates \
        curl \
        git \
        jq \
        procps \
        python3 \
        ripgrep \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /workspace

WORKDIR /workspace
ENTRYPOINT ["sleep"]
CMD ["infinity"]
