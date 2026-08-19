from __future__ import annotations

from hearthstone_companion_under_test.season import load_current_battlegrounds_season


def test_bundled_battlegrounds_season_is_versioned_and_not_win_rate_data() -> None:
    season = load_current_battlegrounds_season()

    assert season["key"] == "S14-36.2"
    assert season["season"] == 14
    assert season["patch"] == "36.2.2"
    assert season["status"] == "bundled_static"
    assert season["is_win_rate_data"] is False
    assert season["source_url"].startswith("https://hearthstone.blizzard.com/")
    assert season["patch_source_url"].endswith("/3622-patch-notes")
    assert len(season["source_urls"]) == 2
    assert len(season["mechanics"]) >= 4
