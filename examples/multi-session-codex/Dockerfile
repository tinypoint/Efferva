FROM node:22-alpine AS web

WORKDIR /repo/examples/multi-session-codex/web
COPY packages/codex-client /repo/packages/codex-client
COPY examples/multi-session-codex/web/ ./
RUN npm ci
RUN npm run build

FROM python:3.13-slim-bookworm

WORKDIR /product
COPY dist/efferva-*.whl /tmp/wheels/
COPY examples/multi-session-codex/pyproject.toml ./
COPY examples/multi-session-codex/src ./src
COPY --from=web /repo/examples/multi-session-codex/web/dist ./src/multi_session_codex/static
RUN pip install --no-cache-dir /tmp/wheels/*.whl \
    && pip install --no-cache-dir . \
    && rm -rf /tmp/wheels

CMD ["uvicorn", "multi_session_codex.main:app", "--host", "0.0.0.0", "--port", "8080"]
