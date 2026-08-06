FROM node:22-alpine AS web

WORKDIR /repo/examples/multi-session/web
COPY packages/codex-client /repo/packages/codex-client
COPY examples/multi-session/web/ ./
RUN npm ci
RUN npm run build

FROM python:3.13-slim-bookworm

WORKDIR /product
COPY dist/efferva-*.whl /tmp/wheels/
COPY examples/multi-session/pyproject.toml ./
COPY examples/multi-session/src ./src
COPY --from=web /repo/examples/multi-session/web/dist ./src/multi_session/static
RUN pip install --no-cache-dir /tmp/wheels/*.whl \
    && pip install --no-cache-dir . \
    && rm -rf /tmp/wheels

CMD ["uvicorn", "multi_session.main:app", "--host", "0.0.0.0", "--port", "8080"]
