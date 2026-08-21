from __future__ import annotations

from hearthstone_companion_under_test.models import (
    BattlegroundsCardSnapshot,
    Entity,
    RuntimeStatus,
)


def test_public_name_filters_unknown_entity_placeholder() -> None:
    entity = Entity(entity_id=7, card_id="CS2_029", name="UNKNOWN ENTITY [cardType=INVALID]")

    assert entity.public_name() == "CS2_029"


def test_hidden_entity_has_no_public_name_even_with_known_identity() -> None:
    entity = Entity(entity_id=8, card_id="CS2_029", name="火球术", hidden=True)

    assert entity.public_name() == ""


def test_visibility_revoked_entity_has_no_public_name_even_if_not_marked_hidden() -> None:
    entity = Entity(
        entity_id=9,
        card_id="CS2_029",
        name="火球术",
        visibility_revoked=True,
    )

    assert entity.public_name() == ""


def test_runtime_status_exposes_source_modified_time() -> None:
    status = RuntimeStatus(source_modified_at=123.5)

    assert status.to_dict()["source_modified_at"] == 123.5


def test_unobserved_battlegrounds_card_fields_remain_null() -> None:
    public = BattlegroundsCardSnapshot(card_id="BG_UNKNOWN").to_public_dict()

    assert public["card_type"] is None
    assert public["current_cost"] is None
    assert public["premium"] is None
