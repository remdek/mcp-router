#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

python -m pip install -U pip
pip install -e ".[dev]"
pre-commit install --install-hooks

echo ""
echo "Dev environment ready."
echo "Git pre-commit hook: .git/hooks/pre-commit"
echo "Run checks manually: pre-commit run --all-files"
