FROM python:3.13-slim-bookworm

WORKDIR /product
COPY dist/docker/agentframe-*.whl /tmp/wheels/
RUN pip install --no-cache-dir /tmp/wheels/*.whl && rm -rf /tmp/wheels
COPY examples/basic-product/auth.py examples/basic-product/main.py ./

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
