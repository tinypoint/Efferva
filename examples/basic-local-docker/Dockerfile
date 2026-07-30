FROM python:3.13-slim-bookworm

WORKDIR /product
COPY dist/efferva-*.whl /tmp/wheels/
COPY examples/basic-local-docker/pyproject.toml ./
COPY examples/basic-local-docker/src ./src
RUN pip install --no-cache-dir /tmp/wheels/*.whl \
    && pip install --no-cache-dir . \
    && rm -rf /tmp/wheels

CMD ["uvicorn", "basic_local_docker.main:app", "--host", "0.0.0.0", "--port", "8080"]
