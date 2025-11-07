# WARP.md

This file provides guidance to WARP (warp.dev) when working with code in this repository.

Project overview
- Repository: spark-coach — AI coach for social media (see README.md)

Commands
- Environment setup
  - python3 -m venv .venv
  - source .venv/bin/activate
  - pip install -U pip
  - pip install -r requirements.txt -r requirements-dev.txt
  - pre-commit install
- Run the API (from repo root)
  - uvicorn spark_coach.app:app --reload
- Run tests
  - PYTHONPATH=src pytest
  - Single test: PYTHONPATH=src pytest tests/test_health.py::test_health_ok
- Lint and format
  - Ruff lint: ruff check .
  - Black check: black --check .
  - Format code: black .
- Typecheck
  - mypy src
- Environment
  - Required for Slack webhooks: export SLACK_WEBHOOK_URL="https://hooks.slack.com/services/..."
- Manual Slack test
  - curl -X POST http://127.0.0.1:8000/v1/slack/notify -H 'Content-Type: application/json' -d '{"text":"Hello from spark-coach"}'

Architecture (high-level)
- make venv && make deps
- make run
- make test
- make lint
- make fmt / make fmt-check
- make typecheck

Architecture (high-level)
- Backend: FastAPI app with entrypoint at spark_coach.app:app
  - Health endpoint at GET /health returning {"status": "ok"}
- Layout
  - Application code lives under src/spark_coach/
  - Tests under tests/ (pytest configured via pyproject.toml)
- Tooling
  - Tool configs in pyproject.toml (black, ruff, mypy, pytest)
  - Dependencies in requirements.txt and requirements-dev.txt
  - Pre-commit hooks for black/ruff in .pre-commit-config.yaml

Assistant rules and other guidance
- No assistant rules were found (e.g., CLAUDE.md, .cursor/rules, .cursorrules, .github/copilot-instructions.md). Add any such rules as they are created and reference their key points here.
