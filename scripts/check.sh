#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
uv sync --frozen
uv run --frozen ruff check src tests
uv run --frozen ruff format --check src tests
uv run --frozen pytest -q
