.PHONY: dev check lint format typecheck test lint-imports context-drift

dev:
	@./scripts/setup-dev.sh

check:
	@.venv/bin/pre-commit run --all-files

test:
	@.venv/bin/pytest -q

lint:
	@.venv/bin/ruff check src tests

format:
	@.venv/bin/ruff format src tests

typecheck:
	@.venv/bin/mypy

lint-imports:
	@.venv/bin/lint-imports

context-drift:
	@.venv/bin/python scripts/context_drift.py
