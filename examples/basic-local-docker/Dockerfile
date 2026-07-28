FROM python:3.13-slim-bookworm

ARG TARGETARCH
ARG EFFERVA_VERSION=0.1.0
ARG EFFERVA_WHEEL_URL

WORKDIR /product
COPY pyproject.toml ./
COPY src ./src
RUN if [ -n "${EFFERVA_WHEEL_URL}" ]; then \
        wheel_source="${EFFERVA_WHEEL_URL}"; \
    else \
        case "${TARGETARCH}" in \
          amd64) wheel_arch=x86_64 ;; \
          arm64) wheel_arch=aarch64 ;; \
          *) echo "Unsupported Docker architecture: ${TARGETARCH}" >&2; exit 1 ;; \
        esac; \
        wheel_source="https://github.com/tinypoint/Efferva/releases/download/v${EFFERVA_VERSION}/efferva-${EFFERVA_VERSION}-py3-none-linux_${wheel_arch}.whl"; \
    fi \
    && pip install --no-cache-dir "${wheel_source}" .

CMD ["uvicorn", "basic_local_docker.main:app", "--host", "0.0.0.0", "--port", "8080"]
