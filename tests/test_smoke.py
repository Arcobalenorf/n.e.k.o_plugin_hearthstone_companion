from __future__ import annotations

import tomllib
from pathlib import Path

from hearthstone_companion_under_test.config import CompanionConfig
from hearthstone_companion_under_test.powerlog import PowerLogParser

FIXTURES = Path(__file__).parent / "fixtures"


def test_smoke_parse_fixture_into_public_snapshot() -> None:
    parser = PowerLogParser()
    lines = (FIXTURES / "basic_game.power.txt").read_text(encoding="utf-8").splitlines()

    events = parser.feed_lines(lines, now=100.0)
    public = parser.snapshot().to_public_dict()

    assert {event.kind for event in events} >= {"game_started", "turn_started"}
    assert public["phase"] == "playing"
    assert public["turn"] == 1
    assert public["player"]["health"] == 30
    assert public["opponent"]["hand_count"] == 1
    assert "GameAccountId" not in repr(public)
    assert "111" not in repr(public)


def test_smoke_default_config_enables_questions_not_proactive_commentary() -> None:
    config = CompanionConfig()

    assert config.monitor_on_start is True
    assert config.llm_commentary_enabled is False
    assert config.llm_data_consent is True


def test_manifest_is_passive_so_admin_entries_do_not_enter_agent_routing() -> None:
    manifest = tomllib.loads((Path(__file__).parents[1] / "plugin.toml").read_text(encoding="utf-8"))

    assert manifest["plugin"]["passive"] is True
