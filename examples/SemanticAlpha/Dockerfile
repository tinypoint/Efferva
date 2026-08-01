FROM node:22-alpine AS web

WORKDIR /web
COPY examples/SemanticAlpha/web/package.json \
     examples/SemanticAlpha/web/package-lock.json ./
RUN npm ci
COPY examples/SemanticAlpha/web/ ./
RUN npm run build

FROM python:3.13-slim-bookworm

WORKDIR /product
COPY dist/efferva-*.whl /tmp/wheels/
COPY examples/SemanticAlpha/pyproject.toml ./
COPY examples/SemanticAlpha/src ./src
COPY --from=web /web/dist ./src/semantic_alpha/static
RUN pip install --no-cache-dir /tmp/wheels/*.whl \
    && pip install --no-cache-dir . \
    && rm -rf /tmp/wheels

CMD ["uvicorn", "semantic_alpha.main:app", "--host", "0.0.0.0", "--port", "8080"]
