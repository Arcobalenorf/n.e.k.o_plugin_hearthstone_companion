from __future__ import annotations

import pytest
from hearthstone_companion_under_test.config import CompanionConfig


@pytest.mark.parametrize("value", ["false", "true", 0, 1, None, [], {}])
def test_explicit_non_boolean_security_settings_fail_closed(value: object) -> None:
    config = CompanionConfig.from_mapping(
        {
            "llm_commentary_enabled": value,
            "llm_data_consent": value,
            "card_catalog_network_enabled": value,
        }
    )

    assert config.llm_commentary_enabled is False
    assert config.llm_data_consent is False
    assert config.card_catalog_network_enabled is False


def test_missing_boolean_settings_keep_declared_defaults() -> None:
    config = CompanionConfig.from_mapping({})

    assert config.monitor_on_start is True
    assert config.card_catalog_network_enabled is True
    assert config.overlay_enabled is True
    assert config.llm_data_consent is True
    assert config.initial_read_max_bytes == 64 * 1024 * 1024
