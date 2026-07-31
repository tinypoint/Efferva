FROM node:22-alpine AS web

WORKDIR /web
COPY examples/basic-local-docker/web/package.json \
     examples/basic-local-docker/web/package-lock.json ./
RUN npm ci
COPY examples/basic-local-docker/web/ ./
RUN npm run build

FROM python:3.13-slim-bookworm

WORKDIR /product
COPY dist/efferva-*.whl /tmp/wheels/
COPY examples/basic-local-docker/pyproject.toml ./
COPY examples/basic-local-docker/src ./src
COPY --from=web /web/dist ./src/basic_local_docker/static
RUN pip install --no-cache-dir /tmp/wheels/*.whl \
    && pip install --no-cache-dir . \
    && rm -rf /tmp/wheels

CMD ["uvicorn", "basic_local_docker.main:app", "--host", "0.0.0.0", "--port", "8080"]
