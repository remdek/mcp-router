FROM python:3.12-slim

ARG PLAYWRIGHT_INSTALL=false
ARG PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# Node.js for npx-based downstream MCP servers (github, etc.)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    procps \
    findutils \
    tini \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Playwright MCP + Chrome for Testing (only when PLAYWRIGHT_INSTALL=true at build time)
# Stable symlink /ms-playwright/chromium → chromium-<rev> so registry paths survive browser upgrades.
RUN if [ "$PLAYWRIGHT_INSTALL" = "true" ]; then \
      export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH}" \
      && npm install -g @playwright/mcp@latest \
      && npx @playwright/mcp install-browser chrome-for-testing \
      && npx -y playwright install-deps chrome-for-testing \
      && CHROME_DIR="$(ls -d "${PLAYWRIGHT_BROWSERS_PATH}"/chromium-* 2>/dev/null | grep -v headless | sort -V | tail -1)" \
      && test -n "$CHROME_DIR" \
      && ln -sfn "$(basename "$CHROME_DIR")" "${PLAYWRIGHT_BROWSERS_PATH}/chromium"; \
    fi

ENV PLAYWRIGHT_BROWSERS_PATH=${PLAYWRIGHT_BROWSERS_PATH}
ENV PLAYWRIGHT_MCP_OUTPUT_DIR=/tmp/playwright-output

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY config ./config
COPY scripts ./scripts

RUN pip install --no-cache-dir .

# Downstream stdio CLIs used by live registry (bare command and/or npx fallback).
RUN pip install --no-cache-dir "mcp-atlassian==0.23.1" \
    && npm install -g mcp-searxng@latest

# Pre-download ONNX embedding model (adds ~120MB; avoids first-start download)
ARG EMBEDDING_MODEL=sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
RUN python -c "from fastembed import TextEmbedding; TextEmbedding(model_name='${EMBEDDING_MODEL}')"

RUN sed -i 's/\r$//' /app/scripts/*.sh \
    && mkdir -p /app/data \
    && chmod +x /app/scripts/entrypoint.sh

ENV REGISTRY_PATH=/app/config/registry.json
ENV INDEX_DB_PATH=/app/data/tools.db
ENV ALIASES_PATH=/app/config/aliases.json
ENV MCP_ROUTER_HOST=0.0.0.0
ENV MCP_ROUTER_PORT=8000
ENV REINDEX_ON_START=true
ENV REINDEX_BACKGROUND=true
ENV SHADOW_RANKER=false

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
  CMD curl -f http://localhost:8000/health || exit 1

# tini as PID 1 reaps orphans from npx/sh wrappers (WOR-1030).
ENTRYPOINT ["tini", "-g", "--", "sh", "/app/scripts/entrypoint.sh"]
