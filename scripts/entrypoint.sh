#!/bin/sh
set -e

# Ensure stable Chromium path for registry (survives image recreate before rebuild).
if [ -d "${PLAYWRIGHT_BROWSERS_PATH:-/ms-playwright}" ] && [ ! -e "${PLAYWRIGHT_BROWSERS_PATH:-/ms-playwright}/chromium" ]; then
  CHROME_DIR="$(ls -d "${PLAYWRIGHT_BROWSERS_PATH:-/ms-playwright}"/chromium-* 2>/dev/null | grep -v headless | sort -V | tail -1 || true)"
  if [ -n "$CHROME_DIR" ]; then
    ln -sfn "$(basename "$CHROME_DIR")" "${PLAYWRIGHT_BROWSERS_PATH:-/ms-playwright}/chromium"
  fi
fi

if [ "${REINDEX_ON_START}" = "true" ] && [ "${REINDEX_BACKGROUND}" != "true" ]; then
  mcp-router index-tools || echo "Warning: initial index-tools failed, starting server anyway"
fi

exec mcp-router serve
