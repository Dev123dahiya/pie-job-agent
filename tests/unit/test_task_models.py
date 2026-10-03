"""E1: Claude model per task, from config/automation.yaml."""

from pathlib import Path

import pytest

from agent.analysis.analyzers import resolve_analyzer, resolve_structured_claude
from backend.core.config import REPO_ROOT, Settings
from backend.services.limits import LimitsConfigError, model_for


def test_shipped_models() -> None:
    path = REPO_ROOT / "config" / "automation.yaml"
    assert model_for(path, "scoring") == "claude-sonnet-5-5"
    assert model_for(path, "tailoring") == "claude-opus-5-5"
    assert model_for(path, "answers") == "claude-opus-5-5"


def test_invalid_model_names_are_refused(tmp_path: Path) -> None:
    path = tmp_path / "automation.yaml"
    path.write_text("models: {scoring: 'gpt-4; rm -rf'}", encoding="utf-8")
    with pytest.raises(LimitsConfigError):
        model_for(path, "scoring")


def test_scoring_and_tailoring_use_their_own_model(tmp_path: Path) -> None:
    path = tmp_path / "automation.yaml"
    path.write_text(
        "models: {scoring: claude-haiku-4-5, tailoring: claude-opus-5-5}", encoding="utf-8"
    )
    settings = Settings(
        analyzer_backend="claude_api",
        anthropic_api_key="sk-ant-test-key-not-real",
        automation_path=path,
    )
    assert resolve_analyzer(settings).model == "claude-haiku-4-5"  # type: ignore[attr-defined]
    claude = resolve_structured_claude(settings)
    assert claude.model == "claude-opus-5-5"  # type: ignore[attr-defined]
