# airtap-sync: build and development tasks.
#
#   make              build the executables (creates .venv and installs on the first run)
#   make help         list all targets
#
# On Windows run it from Git Bash (or any shell that provides sh) with GNU make.

.DEFAULT_GOAL := build
.PHONY: help install venv build package icon test lint format check version clean distclean

ifeq ($(OS),Windows_NT)
  PYTHON ?= python
  VENV_PY := .venv/Scripts/python.exe
else
  PYTHON ?= python3
  VENV_PY := .venv/bin/python
endif

# Packages installed into the virtual environment (see [project.optional-dependencies]).
EXTRAS ?= dev,tray,build
STAMP := .venv/.installed

# `make PY=python build` uses that interpreter as is (e.g. in CI) and skips the .venv.
ifeq ($(origin PY),command line)
  DEPS :=
else
  PY := $(VENV_PY)
  DEPS := $(STAMP)
endif

help:
	@echo "airtap-sync make targets:"
	@echo "  make / make build   build dist/airtap-sync/ (airtap-sync + airtap-sync-agent) and a zip"
	@echo "  make install        create .venv and install the project with $(EXTRAS)"
	@echo "  make icon           create build/icon.ico from the PNG icon"
	@echo "  make test           run the tests (pytest)"
	@echo "  make lint           ruff check and format check"
	@echo "  make format         apply ruff fixes and formatting"
	@echo "  make check          lint + test"
	@echo "  make version        print the version from pyproject.toml"
	@echo "  make clean          remove build/, dist/ and caches"
	@echo "  make distclean      clean, and also remove .venv"
	@echo "variables: PYTHON=<interpreter for creating .venv>  PY=<use this interpreter, no .venv>  EXTRAS=<extras>"

$(VENV_PY):
	$(PYTHON) -m venv .venv

# Reinstall whenever pyproject.toml changes.
$(STAMP): pyproject.toml | $(VENV_PY)
	$(VENV_PY) -m pip install --upgrade pip
	$(VENV_PY) -m pip install -e ".[$(EXTRAS)]"
	$(VENV_PY) -c "import pathlib; pathlib.Path(r\".venv/.installed\").touch()"

install venv: $(STAMP)

build package: $(DEPS)
	$(PY) scripts/build_exe.py

icon: $(DEPS)
	$(PY) scripts/make_icon.py

test: $(DEPS)
	$(PY) -m pytest

lint: $(DEPS)
	$(PY) -m ruff check .
	$(PY) -m ruff format --check .

format: $(DEPS)
	$(PY) -m ruff check --fix .
	$(PY) -m ruff format .

check: lint test

version: $(DEPS)
	@$(PY) -c "import airtap_sync; print(airtap_sync.__version__)"

clean:
	$(PYTHON) scripts/clean.py

distclean:
	$(PYTHON) scripts/clean.py --all
