.PHONY: test lint format type live-test tox man

PYTHON != command -v python3 2>/dev/null || command -v python3.12 2>/dev/null || command -v python

test:
	$(PYTHON) -m pytest tests/unit

lint:
	ruff check src tests
	ruff format --check src tests
	$(PYTHON) -m pylint --errors-only src/sylve_mcp

format:
	ruff format src tests
	ruff check --fix src tests

type:
	$(PYTHON) -m mypy

live-test:
	$(PYTHON) -m pytest -m live tests/live

tox:
	$(PYTHON) -m tox

man:
	$(PYTHON) scripts/build_man.py