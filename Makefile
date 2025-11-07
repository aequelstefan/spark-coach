PYTEST=PYTHONPATH=src .venv/bin/pytest
RUFF=.venv/bin/ruff
BLACK=.venv/bin/black
MYPY=.venv/bin/mypy
UVICORN=.venv/bin/uvicorn
PIP=.venv/bin/pip
PYTHON=.venv/bin/python

.PHONY: venv deps precommit-install run test lint fmt fmt-check typecheck clean

venv:
	python3 -m venv .venv

deps: venv
	$(PYTHON) -m pip install -U pip
	$(PIP) install -r requirements.txt -r requirements-dev.txt

precommit-install:
	.venv/bin/pre-commit install

run:
	$(UVICORN) spark_coach.app:app --reload

test:
	$(PYTEST) -q

lint:
	$(RUFF) check .

fmt:
	$(RUFF) check --fix .
	$(BLACK) .

fmt-check:
	$(BLACK) --check .
	$(RUFF) check .

typecheck:
	$(MYPY) src

clean:
	rm -rf .venv .mypy_cache .ruff_cache .pytest_cache __pycache__
