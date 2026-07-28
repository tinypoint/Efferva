ARG AGENTFRAME_BASE_IMAGE=agentframe-runtime:local
FROM ${AGENTFRAME_BASE_IMAGE}

WORKDIR /product
COPY auth.py main.py ./

CMD ["main:app", "--host", "0.0.0.0", "--port", "8080"]
