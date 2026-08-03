FROM python:3.13-slim-bookworm

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

COPY --chown=1000:1000 \
    examples/scheduled-investment-research/sandbox/skills/ \
    /opt/efferva/session-template/.codex/skills/
COPY --chown=1000:1000 \
    examples/scheduled-investment-research/sandbox/workspace/ \
    /opt/efferva/session-template/workspace/
COPY examples/scheduled-investment-research/sandbox-entrypoint.sh \
    /usr/local/bin/scheduled-investment-research-sandbox

RUN chmod 755 /usr/local/bin/scheduled-investment-research-sandbox

WORKDIR /home/sandbox/workspace

ENTRYPOINT ["scheduled-investment-research-sandbox"]
CMD ["sleep", "infinity"]
