.PHONY: install check format build clean

install:
	uv venv --python 3.13 .venv
	uv pip install --python .venv/bin/python -e ".[dev]"

check:
	.venv/bin/ruff check src tests
	.venv/bin/mypy
	.venv/bin/pytest

format:
	.venv/bin/ruff check --fix src tests
	.venv/bin/ruff format src tests

build:
	uv build

clean:
	rm -rf dist build *.egg-info .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
