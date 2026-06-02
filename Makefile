# JIGGA developer convenience targets. The canonical install path is
# scripts/install.sh (works on a fresh machine before `jigga` exists).
.PHONY: install dev test lint check clean

VENV ?= .venv
PY := $(VENV)/bin/python

install:        ## Create the venv and install JIGGA (editable)
	./scripts/install.sh

dev: install    ## Install plus the dev toolchain (pytest, ruff)
	$(PY) -m pip install --quiet pytest ruff

test:           ## Run the test suite
	$(PY) -m pytest -q

lint:           ## Lint with ruff
	$(PY) -m ruff check .

check: lint test  ## Lint then test

clean:          ## Remove the virtual environment
	rm -rf $(VENV)
