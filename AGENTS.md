# AGENTS.md

## Project Guidance

- Read the repository structure before making framework or tooling assumptions.
- Prefer codebase-memory-mcp graph tools for code discovery:
  `search_graph`, then `trace_path`, then `get_code_snippet`, then
  `query_graph` or `get_architecture` for broader questions.
- Graphify output may exist in `graphify-out/`. Use it as a secondary local
  map after codebase-memory-mcp, especially for broad architecture navigation,
  relationship queries, and visual graph review. Do not let Graphify override
  codebase-memory-mcp as the first source for code discovery.
- Fall back to file search or direct reads for non-code files, exact strings,
  config values, docs, Docker files, and shell scripts.
- Do not expose secrets or private data. Avoid printing `.env`, credentials,
  tokens, private config values, generated storage contents, or user data.
- Follow the existing project style, module boundaries, naming, and test
  patterns. Keep changes small and focused.
- Before running tests, formatting, or build commands, discover the current
  commands from package and config files such as `pyproject.toml`, `uv.lock`,
  `requirements.txt`, `.github/workflows/*.yml`, and `test/README.md`.
- Current CI uses `uv sync --frozen`, Python compile checks, and selected
  `unittest` modules. Re-check config before relying on these commands.
- For UI or frontend changes, read `DESIGN.md` first and keep Streamlit screens
  calm, practical, and consistent with that design system.
- The user generally speaks Turkish, so explain work in Turkish. Code,
  comments, commit messages, and branch names should follow the repository's
  existing language and style.

## Project Skills

- Repo-specific skills live in `.agents/skills/`.
- Use `using-agent-skills` to decide which workflow applies.
- For non-trivial changes, prefer:
  `planning-and-task-breakdown`, `incremental-implementation`,
  `test-driven-development`, and `code-review-and-quality`.

## Reasoning Effort Selection

- Keep routine, narrow tasks on the default reasoning effort. Do not ask the
  user to switch modes for ordinary edits, UI wiring, validation, or simple
  glue code.
- For complex algorithms, API integrations, performance work, difficult
  debugging, or broad multi-file changes, tell the user that the task looks
  complex and suggest Plan Mode or `--profile deep` before implementation.
- If a task starts simple but turns into architectural work, pause briefly and
  tell the user that higher reasoning effort would be useful instead of
  continuing silently.
