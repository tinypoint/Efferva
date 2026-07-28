# syntax=docker/dockerfile:1.7

FROM rust:1.95-bookworm AS runtime-builder

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

RUN --mount=type=cache,id=agentframe-cargo-registry,target=/usr/local/cargo/registry,sharing=locked \
    --mount=type=cache,id=agentframe-cargo-git,target=/usr/local/cargo/git,sharing=locked \
    --mount=type=cache,id=agentframe-rust-target-${TARGETARCH},target=/source/agent-framework/target,sharing=locked \
    cargo build --locked --profile container --jobs 4 \
        --package agentframe-codex-runtime \
    && mkdir -p /artifacts \
    && cp target/container/agentframe-codex-runtime /artifacts/

FROM python:3.13-slim-bookworm AS wheel-builder

ARG CODEX_REVISION
ARG AGENTFRAME_REVISION

WORKDIR /source/agent-framework
COPY agent-framework/pyproject.toml agent-framework/README.md ./
COPY agent-framework/build_hooks ./build_hooks
COPY agent-framework/src ./src
COPY --from=runtime-builder \
    /artifacts/agentframe-codex-runtime \
    /artifacts/agentframe-codex-runtime
RUN --mount=type=cache,id=agentframe-pip-cache,target=/root/.cache/pip,sharing=locked \
    AGENTFRAME_BUILD_RUNTIME_BINARY=/artifacts/agentframe-codex-runtime \
    AGENTFRAME_BUILD_CODEX_REVISION="${CODEX_REVISION}" \
    AGENTFRAME_BUILD_REVISION="${AGENTFRAME_REVISION}" \
    AGENTFRAME_BUILD_PLATFORM_TAG="linux_$(uname -m)" \
    pip wheel --no-deps --wheel-dir /dist .

FROM scratch AS wheel
COPY --from=wheel-builder /dist /
