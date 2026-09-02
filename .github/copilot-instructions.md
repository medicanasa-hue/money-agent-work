# MoneyPrinterTurbo development instructions

Read `AGENTS.md` and applicable repository skills before changing code. Explain
work to the user in Turkish; follow the existing language and style in source
files. Preserve uncommitted work and keep each task focused on one behavior.

## Repository map

This Python 3.11+ application generates videos using FastAPI, Streamlit,
MoviePy/FFmpeg, LLM and speech providers. `app/controllers` owns HTTP boundaries;
`app/services` owns the media pipeline and provider adapters; `app/models` owns
schemas; `app/utils` owns shared utilities. Entrypoints are `main.py`, `cli.py`
and `webui/Main.py`. Tests live in `test/`, not `tests/`.

Read `DESIGN.md` before UI changes. Preserve local module boundaries when
adapting upstream patches; do not replace this fork with upstream files.
Use `codex/` for new branch names unless the user requests another name.

## Setup and validation

Re-check `pyproject.toml`, `uv.lock`, `.github/workflows/ci.yml` and
`test/README.md` for current commands. The supported development path is
`uv sync --frozen --python 3.11`. CI also tests Python 3.13 on Linux.
`pyproject.toml` and `uv.lock` define the development environment; the legacy
Docker `requirements.txt` path does not automatically inherit uv overrides.
Do not update dependencies as a side effect of an unrelated fix.

Set these environment variables before deterministic tests:
`MPT_RUN_INTEGRATION_TESTS=0`, `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`.
Use temporary files and synthetic media, not the user's config or storage.
Live provider tests require separate authorization and credentials.

- Focused tests: `uv run --no-sync python -X utf8 -m pytest -q test/<path>`.
- All tests: `uv run --no-sync python -X utf8 -m pytest -q test`.
- Coverage: `uv run --no-sync python -X utf8 -m coverage run -m pytest -q test`,
  then `uv run --no-sync python -m coverage report`.
- Lint: `uv run --no-sync ruff check app cli.py main.py webui test`.
- Compile: `uv run --no-sync python -m compileall -q app cli.py main.py webui test`.
- Diff hygiene: `git diff --check`.

Write meaningful regression tests for changed behavior. Tests may use pytest
functions or unittest classes; pytest collects both. Do not lower coverage
thresholds, weaken assertions or skip failures to make CI green. State failed
checks and platform skips explicitly. A passing test command does not imply
the separate coverage check passed. Do not claim a remote CI run from local logs.

## Security and collaboration

Never include `.env`, real `config.toml`, tokens, private configuration,
generated storage or user media in prompts, logs or commits. Ignore files and
these instructions are not an enforced AI data-exclusion boundary.
Validate inputs at API boundaries; preserve authentication, upload size limits,
path containment, subprocess argument lists, timeouts and resource cleanup.
Keep request tracing IDs separate from filesystem task IDs.

Use one writer per file. For review tasks, report reproducible findings with
file/line references and do not silently edit. Validate AI suggestions with
tests and an independent review before integration. Do not commit unless asked.
Without explicit authorization, do not push, publish images, merge, change
account settings or credentials, dispatch remote agents or call paid providers.
Copilot Student and Google AI Pro are development subscriptions, not automatic
runtime API credentials or transferable quotas for this Codex session.
