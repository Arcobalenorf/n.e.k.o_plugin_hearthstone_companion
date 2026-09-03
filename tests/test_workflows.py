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


def test_release_does_not_gate_on_probabilistic_model_answers() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )

    assert "Require deterministic real N.E.K.O chain evidence" in workflow
    assert "python tests/release_evidence.py" in workflow
    assert '.github/e2e-evidence/${RELEASE_TAG}.json' in workflow
    assert "answer evidence" not in workflow
    assert "Run full Python test suite" in workflow
    assert "Exercise stable SDK lifecycle" in workflow
