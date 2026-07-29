# syntax=docker/dockerfile:1.7

FROM python:3.13-slim-bookworm

WORKDIR /product
COPY dist/docker/efferva-*.whl /tmp/wheels/
COPY examples/basic-local-docker/pyproject.toml ./
COPY examples/basic-local-docker/src ./src
RUN pip install --no-cache-dir /tmp/wheels/*.whl \
    && pip install --no-cache-dir . \
    && rm -rf /tmp/wheels \
    && install -d -m 0777 /var/lib/efferva/codex \
    && python -c \
        "from efferva import locate_runtime_binary; assert locate_runtime_binary().is_file()"

ENV CODEX_HOME=/var/lib/efferva/codex \
    PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["uvicorn", "basic_local_docker.main:app", "--host", "0.0.0.0", "--port", "8080"]
