FROM node:22-alpine AS web

WORKDIR /repo/examples/multi-session-claude-code/web
COPY examples/multi-session-claude-code/web/package.json \
     examples/multi-session-claude-code/web/package-lock.json ./
RUN npm ci
COPY examples/multi-session-claude-code/web/ ./
RUN npm run build

FROM python:3.13-slim-bookworm

WORKDIR /product
COPY dist/efferva-*.whl /tmp/wheels/
COPY examples/multi-session-claude-code/pyproject.toml ./
COPY examples/multi-session-claude-code/src ./src
COPY --from=web /repo/examples/multi-session-claude-code/web/dist ./src/multi_session_claude_code/static
RUN pip install --no-cache-dir /tmp/wheels/*.whl \
    && pip install --no-cache-dir . \
    && rm -rf /tmp/wheels

CMD ["uvicorn", "multi_session_claude_code.main:app", "--host", "0.0.0.0", "--port", "8080"]
