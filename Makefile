PYTHON ?= python3

.PHONY: build lint preflight test

lint:
	$(PYTHON) -m ruff check .

test:
	$(PYTHON) -m pytest -q

build:
	$(PYTHON) -m build
	$(PYTHON) -m twine check dist/*

preflight: lint test build
