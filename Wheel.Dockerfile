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
COPY codex-fork /source/codex
WORKDIR /source/codex/codex-rs

RUN --mount=type=cache,id=efferva-cargo-registry,target=/usr/local/cargo/registry,sharing=locked \
    --mount=type=cache,id=efferva-cargo-git,target=/usr/local/cargo/git,sharing=locked \
    --mount=type=cache,id=efferva-rust-target-${TARGETARCH},target=/source/codex/codex-rs/target,sharing=locked \
    CARGO_PROFILE_RELEASE_LTO=false \
    CARGO_PROFILE_RELEASE_CODEGEN_UNITS=16 \
    CARGO_PROFILE_RELEASE_STRIP=symbols \
    cargo build --locked --release --jobs 4 \
        --package codex-cli \
        --bin codex \
    && mkdir -p /artifacts \
    && cp target/release/codex /artifacts/efferva-codex-runtime

FROM python:3.13-slim-bookworm AS wheel-builder

ARG CODEX_REVISION
ARG EFFERVA_REVISION

WORKDIR /source/agent-framework
COPY agent-framework/pyproject.toml agent-framework/README.md ./
COPY agent-framework/build_hooks ./build_hooks
COPY agent-framework/src ./src
COPY --from=runtime-builder \
    /artifacts/efferva-codex-runtime \
    /artifacts/efferva-codex-runtime
RUN --mount=type=cache,id=efferva-pip-cache,target=/root/.cache/pip,sharing=locked \
    EFFERVA_BUILD_RUNTIME_BINARY=/artifacts/efferva-codex-runtime \
    EFFERVA_BUILD_CODEX_REVISION="${CODEX_REVISION}" \
    EFFERVA_BUILD_REVISION="${EFFERVA_REVISION}" \
    EFFERVA_BUILD_PLATFORM_TAG="linux_$(uname -m)" \
    pip wheel --no-deps --wheel-dir /dist .

FROM scratch AS wheel
COPY --from=wheel-builder /dist /
