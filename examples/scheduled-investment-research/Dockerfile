FROM node:22-alpine AS web

WORKDIR /web
COPY examples/scheduled-investment-research/web/ ./
RUN npm ci
RUN npm run build

FROM python:3.13-slim-bookworm

WORKDIR /product
COPY dist/efferva-*.whl /tmp/wheels/
COPY examples/scheduled-investment-research/pyproject.toml ./
COPY examples/scheduled-investment-research/src ./src
COPY --from=web /web/dist ./src/scheduled_investment_research/static
RUN pip install --no-cache-dir /tmp/wheels/*.whl \
    && pip install --no-cache-dir . \
    && rm -rf /tmp/wheels

CMD ["uvicorn", "scheduled_investment_research.main:app", "--host", "0.0.0.0", "--port", "8080"]
