# Copilot Workspace Instructions

## Python tooling policy

This repository uses uv for Python environment and dependency management.

Always follow these rules when adding or modifying Python code:

1. Use uv for environment and command execution.
2. For Python files outside tests/, run Ruff lint and format before finishing:
   - uv run ruff check .
   - uv run ruff format .
3. After code changes, run pytest as a smoke test:
   - uv run pytest

## Scope notes

- The Ruff requirement applies to Python code outside tests/.
- tests/ can keep test-focused style, but the project should still pass the smoke test command above.

## PR/Task completion checklist

Before marking work done, confirm:

- [ ] Used uv-based workflow
- [ ] Ran Ruff check and format for non-test Python changes
- [ ] Ran pytest smoke test and reported result
