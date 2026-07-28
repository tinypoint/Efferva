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
COPY codex /source/codex
COPY Efferva/Cargo.toml Efferva/Cargo.lock Efferva/rust-toolchain.toml \
    /source/Efferva/
COPY Efferva/crates /source/Efferva/crates
WORKDIR /source/Efferva

RUN --mount=type=cache,id=efferva-cargo-registry,target=/usr/local/cargo/registry,sharing=locked \
    --mount=type=cache,id=efferva-cargo-git,target=/usr/local/cargo/git,sharing=locked \
    --mount=type=cache,id=efferva-rust-target-${TARGETARCH},target=/source/Efferva/target,sharing=locked \
    cargo build --locked --profile container --jobs 4 \
        --package efferva-codex-runtime \
    && mkdir -p /artifacts \
    && cp target/container/efferva-codex-runtime /artifacts/

FROM python:3.13-slim-bookworm AS wheel-builder

ARG CODEX_REVISION
ARG EFFERVA_REVISION

WORKDIR /source/Efferva
COPY Efferva/pyproject.toml Efferva/README.md ./
COPY Efferva/build_hooks ./build_hooks
COPY Efferva/src ./src
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
