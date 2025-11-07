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
- Script runs
  - Suggest + monitor: python coach.py --task suggest
  - Afternoon prompts: python coach.py --task afternoon
  - Opportunity scan: python coach.py --task scan
  - Daily summary: python coach.py --task summary
  - Weekly brief: python coach.py --task weekly
- Tests
  - PYTHONPATH=src pytest
  - Single test: PYTHONPATH=src pytest tests/test_health.py::test_health_ok
- Lint and format
  - Ruff lint: ruff check .
  - Black check: black --check .
  - Format code: black .
- Typecheck
  - mypy src
- Environment
  - Required: export ANTHROPIC_API_KEY=..., SLACK_BOT_TOKEN=..., SLACK_CHANNEL_ID=...
  - Optional: export ANTHROPIC_MODEL=... (e.g., claude-3-5-sonnet-20241022 for production content)
  - Required for X posting: export TWITTER_API_KEY=..., TWITTER_API_SECRET=..., TWITTER_ACCESS_TOKEN=..., TWITTER_ACCESS_SECRET=...

Shortcuts (Makefile)
- make venv && make deps
- make run
- make test
- make lint
- make fmt / make fmt-check
- make typecheck

Automation (GitHub Actions)
- coach.yml runs every 30 minutes (UTC). The job selects tasks:
  - 07:30 UTC (08:30 CET): suggest
  - 13:00 UTC (14:00 CET): afternoon
  - 17:00 UTC (18:00 CET): summary
  - Sunday 19:00 UTC (20:00 CET): weekly
  - Otherwise: scan
- Secrets to set in repo settings:
  - ANTHROPIC_API_KEY
  - SLACK_BOT_TOKEN
  - SLACK_CHANNEL_ID
  - TWITTER_API_KEY, TWITTER_API_SECRET, TWITTER_ACCESS_TOKEN, TWITTER_ACCESS_SECRET
- Manual dispatch supported (workflow_dispatch)

Manual runs
- Suggest + monitor: python coach.py --task suggest
- Summary (evening): python coach.py --task summary

Architecture (high-level)
- Simple automation oriented around a single script: coach.py
  - Tasks: suggest (morning content), afternoon (build-in-public), scan (opportunity radar), replies/recs (reply engine and follow/DM), summary (daily), weekly (Sunday)
  - Slack-driven control via reactions (👍 to auto-post); no web server
- Optional dev code under src/ and tests/ remains for future API needs (can be ignored)
- Tooling
  - Tool configs in pyproject.toml (black, ruff, mypy, pytest)
  - Dependencies in requirements.txt and requirements-dev.txt
  - Pre-commit hooks for black/ruff in .pre-commit-config.yaml

Assistant rules and other guidance
- No assistant rules were found (e.g., CLAUDE.md, .cursor/rules, .cursorrules, .github/copilot-instructions.md). Add any such rules as they are created and reference their key points here.
