# syntax=docker/dockerfile:1.7

FROM node:22-bookworm-slim

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
