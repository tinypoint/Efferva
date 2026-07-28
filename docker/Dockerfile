# syntax=docker/dockerfile:1.7

FROM rust:1.95-bookworm AS rust-source

ARG TARGETARCH

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        build-essential \
        ca-certificates \
        clang \
        cmake \
        git \
        libclang-dev \
        pkg-config \
        protobuf-compiler \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /source
COPY codex-fork /source/codex-fork
COPY agent-framework/Cargo.toml agent-framework/Cargo.lock agent-framework/rust-toolchain.toml \
    /source/agent-framework/
COPY agent-framework/crates /source/agent-framework/crates
WORKDIR /source/agent-framework

FROM rust-source AS runtime-builder

RUN --mount=type=cache,id=agentframe-cargo-registry,target=/usr/local/cargo/registry,sharing=locked \
    --mount=type=cache,id=agentframe-cargo-git,target=/usr/local/cargo/git,sharing=locked \
    --mount=type=cache,id=agentframe-rust-target-${TARGETARCH},target=/source/agent-framework/target,sharing=locked \
    cargo build --locked --profile container --jobs 4 \
        --package agentframe-codex-runtime \
    && mkdir -p /artifacts \
    && cp target/container/agentframe-codex-runtime /artifacts/

FROM rust-source AS sandbox-builder

RUN --mount=type=cache,id=agentframe-cargo-registry,target=/usr/local/cargo/registry,sharing=locked \
    --mount=type=cache,id=agentframe-cargo-git,target=/usr/local/cargo/git,sharing=locked \
    --mount=type=cache,id=agentframe-rust-target-${TARGETARCH},target=/source/agent-framework/target,sharing=locked \
    cargo build --locked --profile container --jobs 4 \
        --package agentframe-sandbox-exec-server \
    && mkdir -p /artifacts \
    && cp target/container/agentframe-sandbox-exec-server /artifacts/

FROM python:3.13-slim-bookworm AS python-builder

WORKDIR /source/agent-framework
COPY agent-framework/pyproject.toml agent-framework/README.md ./
COPY agent-framework/src ./src
RUN --mount=type=cache,id=agentframe-pip-cache,target=/root/.cache/pip,sharing=locked \
    pip wheel --wheel-dir /wheels .

FROM docker:28-cli AS docker-cli

FROM python:3.13-slim-bookworm AS runtime

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates curl git \
    && rm -rf /var/lib/apt/lists/* \
    && install -d -m 0777 /var/lib/agentframe/codex
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=python-builder /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels
COPY --from=runtime-builder \
    /artifacts/agentframe-codex-runtime \
    /usr/local/bin/agentframe-codex-runtime

ENV CODEX_HOME=/var/lib/agentframe/codex \
    PYTHONUNBUFFERED=1
EXPOSE 8080
ENTRYPOINT ["agentframe"]
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
COPY --from=sandbox-builder \
    /artifacts/agentframe-sandbox-exec-server \
    /usr/local/bin/agentframe-sandbox-exec-server

WORKDIR /workspace
EXPOSE 8081
ENTRYPOINT ["/usr/local/bin/agentframe-sandbox-exec-server"]
CMD ["--listen", "ws://0.0.0.0:8081"]
