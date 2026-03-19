# Agent Rules For This Repository

## Required workflow

1. Use uv for Python environment and command execution.
2. For Python code changes outside tests/, run:
   - uv run ruff check .
   - uv run ruff format .
3. Before finishing work, run smoke tests:
   - uv run pytest

## Notes

- The Ruff requirement is for non-test Python code.
- tests/ can keep test-oriented style, but the repository should still pass uv run pytest.
