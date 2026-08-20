from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
COMPILE_EXCLUDE = "-x '[/\\\\](.git|.venv|.pytest_cache|.ruff_cache|build|dist)[/\\\\]'"


@pytest.mark.parametrize("workflow_name", ["verify.yml", "release.yml"])
def test_compileall_exclude_regex_is_single_quoted_for_bash(workflow_name: str) -> None:
    workflow = (ROOT / ".github" / "workflows" / workflow_name).read_text(
        encoding="utf-8"
    )

    assert COMPILE_EXCLUDE in workflow
    assert '-x "[/\\\\]' not in workflow
