FROM python:3.13-slim-bookworm

ARG AI_BERKSHIRE_REPOSITORY=https://github.com/xbtlin/ai-berkshire.git
ARG AI_BERKSHIRE_REF=cd933eb2bb94f9f96f20b5b0a98790bab4f0a1a4

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        ca-certificates \
        curl \
        git \
        jq \
        ripgrep \
    && rm -rf /var/lib/apt/lists/*

# xueqiu_scraper.py uses Playwright. Keep the browser and its system
# dependencies in the image instead of installing them when a Session starts.
RUN pip install --no-cache-dir playwright==1.54.0 \
    && playwright install --with-deps chromium \
    && chmod -R a+rX "${PLAYWRIGHT_BROWSERS_PATH}" \
    && rm -rf /var/lib/apt/lists/* /root/.cache/pip

RUN git init /tmp/ai-berkshire \
    && git -C /tmp/ai-berkshire remote add origin "${AI_BERKSHIRE_REPOSITORY}" \
    && git -C /tmp/ai-berkshire fetch --depth 1 origin "${AI_BERKSHIRE_REF}" \
    && git -C /tmp/ai-berkshire checkout --detach FETCH_HEAD \
    && CODEX_HOME=/opt/efferva/session-template/.codex \
        /tmp/ai-berkshire/scripts/install-codex-skills.sh \
    && mkdir -p /opt/efferva/session-template/workspace \
    && cp /tmp/ai-berkshire/AGENTS.md \
        /opt/efferva/session-template/workspace/AGENTS.md \
    && cp /tmp/ai-berkshire/LICENSE \
        /opt/efferva/session-template/workspace/AI_BERKSHIRE_LICENSE \
    && cp -R /tmp/ai-berkshire/tools \
        /opt/efferva/session-template/workspace/tools \
    && chown -R 1000:1000 /opt/efferva/session-template \
    && rm -rf /tmp/ai-berkshire
COPY examples/scheduled-investment-research/sandbox-entrypoint.sh \
    /usr/local/bin/scheduled-investment-research-sandbox

RUN chmod 755 /usr/local/bin/scheduled-investment-research-sandbox

WORKDIR /home/sandbox/workspace

ENTRYPOINT ["scheduled-investment-research-sandbox"]
CMD ["sleep", "infinity"]
