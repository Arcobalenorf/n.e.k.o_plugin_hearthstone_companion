from __future__ import annotations

import json

import pytest
from hearthstone_companion_under_test.commentary import build_llm_prompt
from hearthstone_companion_under_test.models import BattlegroundsCardSnapshot, Entity, GameEvent
from hearthstone_companion_under_test.powerlog import PowerLogParser

PREFIX = "D 12:00:00.0000000 PowerTaskList.DebugPrintPower() - "


def line(payload: str) -> str:
    return PREFIX + payload


def source_line(source: str, payload: str) -> str:
    return f"D 12:00:00.0000000 {source}.DebugPrintPower() - {payload}"


def feed(parser: PowerLogParser, *payloads: str, now: float = 100.0) -> list[GameEvent]:
    events: list[GameEvent] = []
    for payload in payloads:
        events.extend(parser.feed_line(line(payload), now=now))
    return events


def add_entity(
    parser: PowerLogParser,
    entity_id: int,
    card_id: str,
    *,
    controller: int,
    zone: str,
    card_type: str = "",
) -> None:
    feed(parser, f"FULL_ENTITY - Creating ID={entity_id} CardID={card_id}")
    feed(parser, f"    tag=CONTROLLER value={controller}")
    if card_type:
        feed(parser, f"    tag=CARDTYPE value={card_type}")
    feed(parser, f"    tag=ZONE value={zone}")


@pytest.mark.parametrize("verb", ["Creating", "Updating"])
def test_full_entity_accepts_creating_and_id_updating_forms(verb: str) -> None:
    parser = PowerLogParser()

    feed(parser, f"FULL_ENTITY - {verb} ID=42 CardID=CS2_182")
    feed(parser, "    tag=CONTROLLER value=1", "    tag=ZONE value=HAND")

    assert parser.entities[42].card_id == "CS2_182"
    assert parser.entities[42].zone == "HAND"
    assert parser.local_controller is None


def test_full_entity_accepts_current_updating_entity_reference_form() -> None:
    parser = PowerLogParser()

    feed(
        parser,
        "FULL_ENTITY - Updating Entity=[entityName=工程师学徒 id=43 zone=DECK zonePos=1 cardId= player=1] CardID=CS2_172",
    )
    feed(parser, "    tag=CONTROLLER value=1", "    tag=ZONE value=HAND")

    assert parser.entities[43].card_id == "CS2_172"
    assert parser.entities[43].zone == "HAND"
    assert parser.local_controller is None


def test_first_hand_show_infers_non_default_local_controller() -> None:
    parser = PowerLogParser()
    feed(
        parser,
        "CREATE_GAME",
        "Player EntityID=2 PlayerID=1 GameAccountId=[hi=11 lo=22]",
        "Player EntityID=3 PlayerID=3 GameAccountId=[hi=33 lo=44]",
    )

    assert parser.local_controller is None
    feed(
        parser,
        "SHOW_ENTITY - Updating Entity=[entityName=幸运币 id=40 zone=HAND zonePos=1 cardId= player=3] CardID=GAME_005",
    )

    assert parser.local_controller == 3
    assert parser.snapshot().player.hand_count == 1


@pytest.mark.parametrize("opcode", ["SHOW_ENTITY", "CHANGE_ENTITY"])
def test_show_and_change_entity_reveal_unicode_named_card(opcode: str) -> None:
    parser = PowerLogParser()
    add_entity(parser, 20, "", controller=2, zone="HAND")

    feed(
        parser,
        f"{opcode} - Updating Entity=[entityName=火球术 id=20 zone=HAND zonePos=1 cardId= player=2] CardID=CS2_029",
    )

    entity = parser.entities[20]
    assert entity.revealed is True
    assert entity.hidden is False
    assert entity.card_id == "CS2_029"
    assert entity.public_name() == "火球术"


def test_hide_entity_revokes_visibility_and_llm_payload_does_not_leak() -> None:
    parser = PowerLogParser()
    add_entity(parser, 20, "", controller=2, zone="HAND")
    feed(
        parser,
        "SHOW_ENTITY - Updating Entity=[entityName=火球术 id=20 zone=HAND zonePos=1 cardId= player=2] CardID=CS2_029",
        "HIDE_ENTITY - Entity=[entityName=火球术 id=20 zone=HAND zonePos=1 cardId=CS2_029 player=2] tag=1068 value=1",
    )

    entity = parser.entities[20]
    public_json = json.dumps(parser.snapshot().to_public_dict(), ensure_ascii=False)
    prompt = build_llm_prompt(
        GameEvent("turn_started", 5, "回合开始", 100.0, {"turn": 2}),
        parser.snapshot(),
    )

    assert entity.card_id == "CS2_029"  # retained only for state reconciliation
    assert entity.hidden is True
    assert entity.public_name() == ""
    assert "CS2_029" not in public_json
    assert "火球术" not in public_json
    assert "CS2_029" not in prompt
    assert "火球术" not in prompt


def test_hidden_entity_stays_private_when_stale_reference_moves_to_public_zone() -> None:
    parser = PowerLogParser()
    add_entity(parser, 20, "", controller=2, zone="HAND", card_type="MINION")
    feed(
        parser,
        "SHOW_ENTITY - Updating Entity=[entityName=火球术 id=20 zone=HAND zonePos=1 cardId= player=2] CardID=CS2_029",
        "    tag=ATK value=9",
        "    tag=HEALTH value=9",
        "HIDE_ENTITY - Entity=[entityName=火球术 id=20 zone=HAND zonePos=1 cardId=CS2_029 player=2] tag=1068 value=1",
        "TAG_CHANGE Entity=[entityName=火球术 id=20 zone=HAND zonePos=1 cardId=CS2_029 player=2] tag=ZONE value=PLAY",
    )

    entity = parser.entities[20]
    public_json = json.dumps(parser.snapshot().to_public_dict(), ensure_ascii=False)

    assert entity.hidden is True
    assert entity.revealed is False
    assert "CS2_029" not in public_json
    assert "火球术" not in public_json


def test_hidden_public_zone_entity_stays_private_when_play_block_ends() -> None:
    parser = PowerLogParser()
    feed(
        parser,
        "SHOW_ENTITY - Updating Entity=[entityName=幸运币 id=40 zone=HAND zonePos=1 cardId= player=1] CardID=GAME_005",
    )
    add_entity(parser, 20, "CS2_029", controller=2, zone="HAND", card_type="MINION")
    feed(
        parser,
        "    tag=ATK value=9",
        "    tag=HEALTH value=9",
        "HIDE_ENTITY - Entity=[entityName=火球术 id=20 zone=HAND zonePos=1 cardId=CS2_029 player=2] tag=1068 value=1",
        "TAG_CHANGE Entity=20 tag=ZONE value=PLAY",
        "BLOCK_START BlockType=PLAY Entity=[entityName=火球术 id=20 zone=PLAY zonePos=1 cardId=CS2_029 player=2] EffectCardId= EffectIndex=0 Target=0 SubOption=-1",
    )

    events = feed(parser, "BLOCK_END")
    encoded = json.dumps(
        {
            "events": [event.details for event in events],
            "snapshot": parser.snapshot().to_public_dict(),
        },
        ensure_ascii=False,
    )

    assert parser.entities[20].hidden is True
    assert events[0].details["card"] == "一张牌"
    assert parser.snapshot().recent_cards[-1]["card"] == "一张牌"
    assert "card_id" not in parser.snapshot().recent_cards[-1]
    assert "CS2_029" not in encoded
    assert "火球术" not in encoded


def test_explicit_show_can_reveal_an_entity_after_it_was_hidden() -> None:
    parser = PowerLogParser()
    add_entity(parser, 20, "CS2_029", controller=2, zone="PLAY", card_type="MINION")
    feed(
        parser,
        "HIDE_ENTITY - Entity=[entityName=火球术 id=20 zone=PLAY zonePos=1 cardId=CS2_029 player=2] tag=1068 value=1",
        "TAG_CHANGE Entity=20 tag=ZONE value=PLAY",
        "SHOW_ENTITY - Updating Entity=[entityName=火球术 id=20 zone=PLAY zonePos=1 cardId= player=2] CardID=CS2_029",
    )

    assert parser.entities[20].hidden is False
    assert parser.entities[20].public_name() == "火球术"


def test_explicit_full_entity_can_reveal_an_entity_after_it_was_hidden() -> None:
    parser = PowerLogParser()
    add_entity(parser, 20, "CS2_029", controller=2, zone="PLAY", card_type="MINION")
    feed(
        parser,
        "HIDE_ENTITY - Entity=[entityName=火球术 id=20 zone=PLAY zonePos=1 cardId=CS2_029 player=2] tag=1068 value=1",
        "FULL_ENTITY - Updating ID=20 CardID=CS2_029",
    )

    assert parser.entities[20].hidden is False
    assert parser.entities[20].card_id == "CS2_029"


def test_change_entity_cannot_override_visibility_revoked_by_hide_entity() -> None:
    parser = PowerLogParser()
    add_entity(parser, 20, "CS2_029", controller=2, zone="PLAY", card_type="MINION")
    feed(
        parser,
        "HIDE_ENTITY - Entity=[entityName=火球术 id=20 zone=PLAY zonePos=1 cardId=CS2_029 player=2] tag=1068 value=1",
        "CHANGE_ENTITY - Updating Entity=[entityName=变形后 id=20 zone=PLAY zonePos=1 cardId= player=2] CardID=CS2_032",
    )

    public_json = json.dumps(parser.snapshot().to_public_dict(), ensure_ascii=False)

    assert parser.entities[20].hidden is True
    assert parser.entities[20].visibility_revoked is True
    assert "CS2_029" not in public_json
    assert "CS2_032" not in public_json
    assert "变形后" not in public_json


def test_opponent_secret_never_exposes_known_card_id_in_public_events() -> None:
    parser = PowerLogParser()
    feed(
        parser,
        "SHOW_ENTITY - Updating Entity=[entityName=幸运币 id=40 zone=HAND zonePos=1 cardId= player=1] CardID=GAME_005",
    )
    add_entity(parser, 25, "EX1_287", controller=2, zone="HAND", card_type="SPELL")

    assert feed(
        parser,
        "BLOCK_START BlockType=PLAY Entity=[entityName=法术反制 id=25 zone=HAND zonePos=1 cardId=EX1_287 player=2] EffectCardId= EffectIndex=0 Target=0 SubOption=-1",
        "TAG_CHANGE Entity=25 tag=ZONE value=SECRET",
    ) == []
    events = feed(parser, "BLOCK_END")

    played = next(event for event in events if event.kind == "card_played")
    encoded = json.dumps([event.details for event in events], ensure_ascii=False)
    assert played.details["card"] == "奥秘"
    assert "EX1_287" not in encoded
    assert "法术反制" not in encoded


def test_nested_blocks_defer_events_and_coalesce_hero_damage() -> None:
    parser = PowerLogParser()
    feed(
        parser,
        "SHOW_ENTITY - Updating Entity=[entityName=幸运币 id=40 zone=HAND zonePos=1 cardId= player=1] CardID=GAME_005",
    )
    add_entity(parser, 10, "HERO_01", controller=1, zone="PLAY", card_type="HERO")
    feed(parser, "    tag=HEALTH value=30", "    tag=DAMAGE value=0")
    add_entity(parser, 30, "CS2_029", controller=2, zone="HAND", card_type="SPELL")

    assert feed(
        parser,
        "BLOCK_START BlockType=PLAY Entity=[entityName=火球术 id=30 zone=HAND zonePos=1 cardId=CS2_029 player=2] EffectCardId= EffectIndex=0 Target=10 SubOption=-1",
        "BLOCK_START BlockType=POWER Entity=[entityName=火球术 id=30 zone=PLAY zonePos=0 cardId=CS2_029 player=2] EffectCardId= EffectIndex=0 Target=10 SubOption=-1",
        "TAG_CHANGE Entity=10 tag=DAMAGE value=2",
        "TAG_CHANGE Entity=10 tag=DAMAGE value=5",
    ) == []
    assert feed(parser, "BLOCK_END") == []

    events = feed(parser, "BLOCK_END")
    damage = [event for event in events if event.kind == "hero_damaged"]

    assert [event.kind for event in events].count("card_played") == 1
    assert len(damage) == 1
    assert damage[0].details["amount"] == 5
    assert damage[0].details["health"] == 25


def test_turn_and_local_win_update_snapshot_and_emit_events() -> None:
    parser = PowerLogParser()
    feed(parser, "CREATE_GAME", "GameEntity EntityID=1")
    feed(parser, "Player EntityID=2 PlayerID=1 GameAccountId=[hi=1 lo=2]")
    feed(parser, "Player EntityID=3 PlayerID=3 GameAccountId=[hi=3 lo=4]")
    feed(
        parser,
        "SHOW_ENTITY - Updating Entity=[entityName=幸运币 id=40 zone=HAND zonePos=1 cardId= player=3] CardID=GAME_005",
    )
    feed(
        parser,
        "TAG_CHANGE Entity=3 tag=CURRENT_PLAYER value=1",
        "TAG_CHANGE Entity=GameEntity tag=STEP value=MAIN_READY",
    )

    turn_events = feed(parser, "TAG_CHANGE Entity=GameEntity tag=TURN value=3")
    result_events = feed(parser, "TAG_CHANGE Entity=3 tag=PLAYSTATE value=WON")
    snapshot = parser.snapshot()

    assert [(event.kind, event.details) for event in turn_events] == [
        (
            "turn_started",
            {
                "turn": 3,
                "action_turn": 3,
                "round": 2,
                "active_side": "player",
            },
        )
    ]
    assert len(result_events) == 1
    assert result_events[0].kind == "game_ended"
    assert result_events[0].details["result"] == "won"
    assert snapshot.turn == 3
    assert snapshot.phase == "ended"
    assert snapshot.result == "won"


def test_constructed_timeline_stays_in_mulligan_then_publishes_first_turn_edge() -> None:
    parser = PowerLogParser()
    feed(
        parser,
        "CREATE_GAME",
        "GameEntity EntityID=1",
        "Player EntityID=2 PlayerID=1 GameAccountId=[hi=0 lo=0]",
        "Player EntityID=3 PlayerID=3 GameAccountId=[hi=0 lo=0]",
        "TAG_CHANGE Entity=3 tag=MULLIGAN_STATE value=INPUT",
        "TAG_CHANGE Entity=3 tag=CURRENT_PLAYER value=1",
    )

    early_events = feed(parser, "TAG_CHANGE Entity=GameEntity tag=TURN value=1")
    early = parser.snapshot()

    assert early_events == []
    assert early.phase == "mulligan"
    assert early.turn == 1
    assert early.round == 0
    assert early.active_side == "unknown"

    feed(parser, "TAG_CHANGE Entity=3 tag=MULLIGAN_STATE value=DONE")
    assert parser.snapshot().round == 0
    first_turn = feed(parser, "TAG_CHANGE Entity=2 tag=MULLIGAN_STATE value=DONE")
    ready = parser.snapshot()

    assert [(event.kind, event.summary, event.details) for event in first_turn] == [
        (
            "turn_started",
            "第1轮，轮到我方",
            {
                "turn": 1,
                "action_turn": 1,
                "round": 1,
                "active_side": "player",
            },
        )
    ]
    assert ready.phase == "playing"
    assert ready.round == 1
    assert ready.active_side == "player"

    assert feed(parser, "TAG_CHANGE Entity=3 tag=CURRENT_PLAYER value=0") == []
    assert parser.snapshot().active_side == "unknown"
    assert feed(parser, "TAG_CHANGE Entity=GameEntity tag=TURN value=2") == []
    second_turn = feed(parser, "TAG_CHANGE Entity=2 tag=CURRENT_PLAYER value=1")

    assert second_turn[0].details == {
        "turn": 2,
        "action_turn": 2,
        "round": 1,
        "active_side": "opponent",
    }
    assert parser.snapshot().round == 1
    assert parser.snapshot().active_side == "opponent"


def test_missing_resource_tag_is_reported_as_unknown_mana() -> None:
    parser = PowerLogParser()
    feed(parser, "CREATE_GAME", "Player EntityID=2 PlayerID=3 GameAccountId=[hi=1 lo=2]")
    feed(
        parser,
        "SHOW_ENTITY - Updating Entity=[entityName=幸运币 id=40 zone=HAND zonePos=1 cardId= player=3] CardID=GAME_005",
    )

    snapshot = parser.snapshot()

    assert snapshot.player.mana_available is None
    assert snapshot.player.mana_max is None


def test_constructed_metadata_and_private_player_refs_drive_live_turn_and_mana() -> None:
    parser = PowerLogParser()
    private_local = "PRIVATE_LOCAL_PLAYER#1234"
    private_opponent = "PRIVATE_OPPONENT_PLAYER#5678"

    parser.feed_line(source_line("GameState", "CREATE_GAME"), now=100.0)
    parser.feed_line(
        "D 12:00:00.0000000 GameState.DebugPrintGame() - GameType=GT_RANKED",
        now=100.1,
    )
    parser.feed_line(
        f"D 12:00:00.0000000 GameState.DebugPrintGame() - PlayerID=1, PlayerName={private_opponent}",
        now=100.2,
    )
    parser.feed_line(
        f"D 12:00:00.0000000 GameState.DebugPrintGame() - PlayerID=2, PlayerName={private_local}",
        now=100.3,
    )
    parser.feed_line(
        source_line("GameState", "GameEntity EntityID=1"),
        now=100.4,
    )
    parser.feed_line(
        source_line(
            "GameState",
            "Player EntityID=2 PlayerID=1 GameAccountId=[hi=0 lo=0]",
        ),
        now=100.5,
    )
    parser.feed_line(
        source_line(
            "GameState",
            "Player EntityID=3 PlayerID=2 GameAccountId=[hi=0 lo=0]",
        ),
        now=100.6,
    )
    started = parser.feed_line(source_line("PowerTaskList", "CREATE_GAME"), now=101.0)
    feed(
        parser,
        "SHOW_ENTITY - Updating Entity=[entityName=幸运币 id=40 zone=HAND zonePos=1 cardId= player=2] CardID=GAME_005",
        f"TAG_CHANGE Entity={private_local} tag=RESOURCES value=5",
        f"TAG_CHANGE Entity={private_local} tag=RESOURCES_USED value=2",
        f"TAG_CHANGE Entity={private_local} tag=TEMP_RESOURCES value=1",
        f"TAG_CHANGE Entity={private_local} tag=OVERLOAD_LOCKED value=1",
        f"TAG_CHANGE Entity={private_local} tag=CURRENT_PLAYER value=1",
        "TAG_CHANGE Entity=GameEntity tag=TURN value=9",
        "TAG_CHANGE Entity=GameEntity tag=STEP value=MAIN_READY",
    )

    snapshot = parser.snapshot()
    public_json = json.dumps(snapshot.to_public_dict(), ensure_ascii=False)

    assert [event.kind for event in started] == ["game_started", "constructed_detected"]
    assert snapshot.mode == "constructed"
    assert snapshot.constructed is not None
    assert snapshot.constructed.variant == "ranked"
    assert snapshot.turn == 9
    assert snapshot.round == 5
    assert snapshot.active_side == "player"
    assert snapshot.player.mana_max == 5
    assert snapshot.player.mana_available == 4
    assert snapshot.constructed.player.overload_locked == 1
    assert private_local not in public_json
    assert private_opponent not in public_json
    assert all(private_local not in entity.name for entity in parser.entities.values())


def test_constructed_snapshot_exposes_visible_local_cards_and_public_board_details() -> None:
    parser = PowerLogParser()
    feed(
        parser,
        "CREATE_GAME",
        "Player EntityID=2 PlayerID=1 GameAccountId=[hi=0 lo=0]",
        "Player EntityID=3 PlayerID=2 GameAccountId=[hi=0 lo=0]",
        "SHOW_ENTITY - Updating Entity=[entityName=幸运币 id=40 zone=HAND zonePos=1 cardId= player=2] CardID=GAME_005",
    )
    parser.feed_line(
        "D 12:00:00.0000000 GameState.DebugPrintGame() - GameType=GT_RANKED",
        now=100.0,
    )
    add_entity(parser, 10, "HERO_08", controller=2, zone="PLAY", card_type="HERO")
    feed(parser, "    tag=HEALTH value=30", "    tag=DAMAGE value=4", "    tag=ARMOR value=2")
    add_entity(parser, 11, "CS2_034", controller=2, zone="PLAY", card_type="HERO_POWER")
    feed(parser, "    tag=COST value=2", "    tag=EXHAUSTED value=0")
    add_entity(parser, 12, "WEAPON_001", controller=2, zone="PLAY", card_type="WEAPON")
    feed(parser, "    tag=ATK value=3", "    tag=HEALTH value=2", "    tag=DAMAGE value=1")
    add_entity(parser, 20, "CS2_029", controller=2, zone="HAND", card_type="SPELL")
    feed(parser, "    tag=ZONE_POSITION value=2", "    tag=COST value=3")
    add_entity(parser, 21, "MINION_001", controller=2, zone="PLAY", card_type="MINION")
    feed(
        parser,
        "    tag=ZONE_POSITION value=1",
        "    tag=ATK value=4",
        "    tag=HEALTH value=6",
        "    tag=DAMAGE value=1",
        "    tag=TAUNT value=1",
    )
    add_entity(parser, 22, "LOCATION_001", controller=2, zone="PLAY", card_type="LOCATION")
    feed(
        parser,
        "    tag=ZONE_POSITION value=2",
        "    tag=HEALTH value=3",
        "    tag=DAMAGE value=1",
        "    tag=EXHAUSTED value=1",
    )
    add_entity(parser, 30, "OPPONENT_PUBLIC", controller=1, zone="PLAY", card_type="MINION")
    feed(parser, "    tag=ATK value=2", "    tag=HEALTH value=3")
    add_entity(parser, 31, "OPPONENT_REVOKED", controller=1, zone="HAND", card_type="MINION")
    feed(
        parser,
        "    tag=ATK value=99",
        "    tag=HEALTH value=99",
        "HIDE_ENTITY - Entity=[entityName=Secret id=31 zone=HAND zonePos=1 cardId=OPPONENT_REVOKED player=1] tag=1068 value=1",
        "TAG_CHANGE Entity=31 tag=ZONE value=PLAY",
    )

    public = parser.snapshot().to_public_dict()
    player = public["constructed"]["player"]
    opponent = public["constructed"]["opponent"]

    assert player["hero"]["card_id"] == "HERO_08"
    assert player["hero"]["health"] == 26
    assert player["hero_power"]["card_id"] == "CS2_034"
    assert player["hero_power"]["cost"] == 2
    assert player["weapon"]["attack"] == 3
    assert player["weapon"]["durability"] == 1
    hand = {card["card_id"]: card for card in player["hand"]["known_cards"]}
    assert hand["CS2_029"]["cost"] == 3
    assert [card["card_type"] for card in player["board"]["minions"]] == ["MINION"]
    assert player["board"]["minions"][0]["keywords"] == ["taunt"]
    assert player["locations"][0]["durability"] == 2
    assert opponent["board"]["count"] == 1
    assert opponent["board"]["attack"] == 2
    assert [card["card_id"] for card in opponent["board"]["minions"]] == [
        "OPPONENT_PUBLIC"
    ]
    assert opponent["hand"]["known_cards"] == []
    assert "OPPONENT_REVOKED" not in json.dumps(public)


def test_constructed_local_choice_is_exposed_then_cleared_without_player_name() -> None:
    parser = PowerLogParser()
    private_local = "PRIVATE_CHOICE_PLAYER#1234"
    feed(
        parser,
        "CREATE_GAME",
        "Player EntityID=2 PlayerID=1 GameAccountId=[hi=0 lo=0]",
        "Player EntityID=3 PlayerID=2 GameAccountId=[hi=0 lo=0]",
        "SHOW_ENTITY - Updating Entity=[entityName=幸运币 id=40 zone=HAND zonePos=1 cardId= player=2] CardID=GAME_005",
    )
    parser.feed_line(
        f"D 12:00:00.0000000 GameState.DebugPrintGame() - PlayerID=2, PlayerName={private_local}",
        now=101.0,
    )
    parser.feed_line(
        "D 12:00:00.0000000 GameState.DebugPrintGame() - GameType=GT_RANKED",
        now=101.1,
    )
    add_entity(parser, 50, "CHOICE_A", controller=2, zone="SETASIDE", card_type="SPELL")
    feed(parser, "    tag=COST value=1")
    add_entity(parser, 51, "CHOICE_B", controller=2, zone="SETASIDE", card_type="MINION")
    feed(parser, "    tag=ATK value=2", "    tag=HEALTH value=3")

    parser.feed_line(
        f"D 12:00:00.0000000 GameState.DebugPrintEntityChoices() - id=7 Player={private_local} TaskList=9 ChoiceType=GENERAL CountMin=1 CountMax=1",
        now=102.0,
    )
    parser.feed_line(
        "D 12:00:00.0000000 GameState.DebugPrintEntityChoices() -   Source=GameEntity",
        now=102.1,
    )
    parser.feed_line(
        "D 12:00:00.0000000 GameState.DebugPrintEntityChoices() -   Entities[0]=[entityName=Option A id=50 zone=SETASIDE zonePos=0 cardId=CHOICE_A player=2]",
        now=102.2,
    )
    parser.feed_line(
        "D 12:00:00.0000000 GameState.DebugPrintEntityChoices() -   Entities[1]=[entityName=Option B id=51 zone=SETASIDE zonePos=0 cardId=CHOICE_B player=2]",
        now=102.3,
    )

    public = parser.snapshot().to_public_dict()
    assert public["choice"]["choice_type"] == "general"
    assert public["choice"]["count_min"] == 1
    assert public["choice"]["count_max"] == 1
    assert public["choice"]["source"] is None
    assert [card["card_id"] for card in public["choice"]["options"]] == [
        "CHOICE_A",
        "CHOICE_B",
    ]
    assert [card["card_type"] for card in public["choice"]["options"]] == [
        "SPELL",
        "MINION",
    ]
    assert private_local not in json.dumps(public)

    parser.feed_line(
        f"D 12:00:00.0000000 GameState.DebugPrintEntitiesChosen() - id=7 Player={private_local} EntitiesCount=1",
        now=103.0,
    )
    assert parser.snapshot().choice is None


def test_constructed_opponent_choice_identity_is_never_retained_or_exposed() -> None:
    parser = PowerLogParser()
    private_local = "PRIVATE_LOCAL#1000"
    private_opponent = "PRIVATE_OPPONENT#2000"
    feed(
        parser,
        "CREATE_GAME",
        "Player EntityID=2 PlayerID=1 GameAccountId=[hi=0 lo=0]",
        "Player EntityID=3 PlayerID=2 GameAccountId=[hi=0 lo=0]",
        "SHOW_ENTITY - Updating Entity=[entityName=幸运币 id=40 zone=HAND zonePos=1 cardId= player=2] CardID=GAME_005",
    )
    parser.feed_line(
        "D 12:00:00.0000000 GameState.DebugPrintGame() - GameType=GT_RANKED",
        now=101.0,
    )
    for controller, player_name in ((1, private_opponent), (2, private_local)):
        parser.feed_line(
            f"D 12:00:00.0000000 GameState.DebugPrintGame() - PlayerID={controller}, PlayerName={player_name}",
            now=101.1,
        )
    parser.feed_line(
        f"D 12:00:00.0000000 GameState.DebugPrintEntityChoices() - id=9 Player={private_opponent} TaskList= ChoiceType=GENERAL CountMin=1 CountMax=1",
        now=102.0,
    )
    parser.feed_line(
        "D 12:00:00.0000000 GameState.DebugPrintEntityChoices() -   Entities[0]=[entityName=Opponent Secret Choice id=99 zone=SETASIDE zonePos=0 cardId=OPPONENT_CHOICE player=1]",
        now=102.1,
    )

    public_json = json.dumps(parser.snapshot().to_public_dict(), ensure_ascii=False)

    assert parser.snapshot().choice is None
    assert 99 not in parser.entities
    assert private_local not in repr(parser.__dict__)
    assert private_opponent not in repr(parser.__dict__)
    assert "OPPONENT_CHOICE" not in public_json
    assert "Opponent Secret Choice" not in public_json


def test_constructed_local_choice_can_use_option_controller_without_name_metadata() -> None:
    parser = PowerLogParser()
    feed(
        parser,
        "CREATE_GAME",
        "Player EntityID=2 PlayerID=1 GameAccountId=[hi=0 lo=0]",
        "Player EntityID=3 PlayerID=2 GameAccountId=[hi=0 lo=0]",
        "SHOW_ENTITY - Updating Entity=[entityName=幸运币 id=40 zone=HAND zonePos=1 cardId= player=2] CardID=GAME_005",
    )
    parser.feed_line(
        "D 12:00:00.0000000 GameState.DebugPrintGame() - GameType=GT_RANKED",
        now=101.0,
    )
    parser.feed_line(
        "D 12:00:00.0000000 GameState.DebugPrintEntityChoices() - id=11 Player=Unmapped TaskList=3 ChoiceType=GENERAL CountMin=1 CountMax=1",
        now=102.0,
    )
    parser.feed_line(
        "D 12:00:00.0000000 GameState.DebugPrintEntityChoices() -   Entities[0]=[entityName=Visible Option id=98 zone=SETASIDE zonePos=0 cardId=VISIBLE_CHOICE player=2]",
        now=102.1,
    )

    choice = parser.snapshot().choice

    assert choice is not None
    assert [card.card_id for card in choice.options] == ["VISIBLE_CHOICE"]


def test_constructed_game_type_builds_structured_public_state() -> None:
    parser = PowerLogParser()
    feed(
        parser,
        "CREATE_GAME",
        "GameEntity EntityID=1",
        "Player EntityID=2 PlayerID=3 GameAccountId=[hi=1 lo=2]",
        "    tag=RESOURCES value=5",
        "    tag=RESOURCES_USED value=2",
        "Player EntityID=3 PlayerID=5 GameAccountId=[hi=3 lo=4]",
    )
    parser.feed_line(
        "D 12:00:00.0000000 GameState.DebugPrintGame() - GameType=GT_RANKED_STANDARD",
        now=100.0,
    )
    add_entity(parser, 40, "", controller=3, zone="HAND", card_type="SPELL")
    feed(
        parser,
        "    tag=COST value=4",
        "    tag=ZONE_POSITION value=1",
        "SHOW_ENTITY - Updating Entity=[entityName=火球术 id=40 zone=HAND zonePos=1 cardId= player=3] CardID=CS2_029",
    )
    add_entity(parser, 10, "HERO_08", controller=3, zone="PLAY", card_type="HERO")
    feed(parser, "    tag=HEALTH value=30", "    tag=DAMAGE value=4", "    tag=ARMOR value=2")
    add_entity(parser, 20, "CS2_182", controller=3, zone="PLAY", card_type="MINION")
    feed(
        parser,
        "    tag=ZONE_POSITION value=1",
        "    tag=ATK value=3",
        "    tag=HEALTH value=2",
        "    tag=TAUNT value=1",
    )
    add_entity(parser, 21, "CS2_097", controller=3, zone="PLAY", card_type="WEAPON")
    feed(parser, "    tag=ATK value=3", "    tag=DURABILITY value=2")
    add_entity(parser, 22, "CS2_034", controller=3, zone="PLAY", card_type="HERO_POWER")
    feed(parser, "    tag=COST value=2", "    tag=EXHAUSTED value=1")
    add_entity(parser, 23, "REV_990", controller=3, zone="PLAY", card_type="LOCATION")
    feed(parser, "    tag=ZONE_POSITION value=2", "    tag=DURABILITY value=3")
    add_entity(parser, 50, "", controller=5, zone="HAND", card_type="SPELL")
    add_entity(parser, 51, "", controller=5, zone="HAND", card_type="SPELL")
    feed(
        parser,
        "SHOW_ENTITY - Updating Entity=[entityName=已揭示的牌 id=51 zone=HAND zonePos=2 cardId= player=5] CardID=EX1_001",
    )

    state = parser.snapshot().to_public_dict()

    assert state["mode"] == "constructed"
    assert state["constructed"]["game_type"] == "GT_RANKED_STANDARD"
    assert state["constructed"]["format"] == "standard"
    player = state["constructed"]["player"]
    assert player["hero"]["health"] == 26
    assert player["hero"]["armor"] == 2
    assert player["mana"] == {
        "available": 3,
        "maximum": 5,
        "overload_owed": 0,
        "overload_locked": 0,
    }
    assert player["hand"]["count"] == 1
    assert player["hand"]["identities_complete"] is True
    assert player["hand"]["known_cards"][0]["card_id"] == "CS2_029"
    assert player["hand"]["known_cards"][0]["cost"] == 4
    assert player["board"]["minions"][0]["card_id"] == "CS2_182"
    assert player["board"]["minions"][0]["keywords"] == ["taunt"]
    assert player["weapon"]["durability"] == 2
    assert player["hero_power"]["states"] == ["exhausted"]
    assert player["locations"][0]["card_id"] == "REV_990"
    opponent_hand = state["constructed"]["opponent"]["hand"]
    assert opponent_hand["count"] == 2
    assert opponent_hand["identities_complete"] is False
    assert [card["card_id"] for card in opponent_hand["known_cards"]] == ["EX1_001"]


def test_constructed_aggregates_exclude_hidden_entities_and_old_heroes() -> None:
    parser = PowerLogParser()
    feed(
        parser,
        "CREATE_GAME",
        "Player EntityID=2 PlayerID=1 GameAccountId=[hi=1 lo=2]",
        "Player EntityID=3 PlayerID=2 GameAccountId=[hi=3 lo=4]",
        "SHOW_ENTITY - Updating Entity=[entityName=幸运币 id=40 zone=HAND zonePos=1 cardId= player=1] CardID=GAME_005",
    )
    parser.feed_line(
        "D 12:00:00.0000000 GameState.DebugPrintGame() - GameType=GT_CASUAL",
        now=100.0,
    )
    add_entity(parser, 10, "HERO_OLD", controller=1, zone="PLAY", card_type="HERO")
    feed(parser, "    tag=HEALTH value=30", "    tag=DAMAGE value=20")
    add_entity(parser, 11, "HERO_NEW", controller=1, zone="PLAY", card_type="HERO")
    feed(parser, "    tag=HEALTH value=40", "    tag=DAMAGE value=3")
    add_entity(parser, 20, "PRIVATE_MINION", controller=2, zone="PLAY", card_type="MINION")
    feed(
        parser,
        "    tag=ATK value=9",
        "    tag=HEALTH value=9",
        "HIDE_ENTITY - Entity=[entityName=Private id=20 zone=PLAY zonePos=1 cardId=PRIVATE_MINION player=2] tag=1068 value=1",
        "HIDE_ENTITY - Entity=[entityName=Old Hero id=10 zone=PLAY zonePos=0 cardId=HERO_OLD player=1] tag=1068 value=1",
    )

    state = parser.snapshot().to_public_dict()

    assert state["player"]["health"] == 37
    assert state["opponent"]["board"] == {
        "count": 0,
        "attack": 0,
        "health": 0,
        "cards": [],
    }
    assert state["constructed"]["player"]["hero"]["card_id"] == "HERO_NEW"
    assert state["constructed"]["opponent"]["board"]["minions"] == []
    assert "PRIVATE_MINION" not in json.dumps(state, ensure_ascii=False)


def test_constructed_opponent_ignores_controller_zero_system_entities() -> None:
    parser = PowerLogParser()
    feed(
        parser,
        "CREATE_GAME",
        "Player EntityID=2 PlayerID=1 GameAccountId=[hi=0 lo=0]",
        "Player EntityID=3 PlayerID=2 GameAccountId=[hi=0 lo=0]",
        "SHOW_ENTITY - Updating Entity=[entityName=幸运币 id=40 zone=HAND zonePos=1 cardId= player=2] CardID=GAME_005",
    )
    parser.feed_line(
        "D 12:00:00.0000000 GameState.DebugPrintGame() - GameType=GT_RANKED",
        now=100.0,
    )
    add_entity(parser, 30, "OPPONENT_BOARD", controller=1, zone="PLAY", card_type="MINION")
    feed(parser, "    tag=ATK value=3", "    tag=HEALTH value=4")
    add_entity(parser, 90, "SYSTEM_EFFECT", controller=0, zone="PLAY", card_type="ENCHANTMENT")

    snapshot = parser.snapshot()

    assert parser._opponent_controller() == 1
    assert snapshot.opponent.board_count == 1
    assert snapshot.constructed is not None
    assert [card.card_id for card in snapshot.constructed.opponent.board] == [
        "OPPONENT_BOARD"
    ]


def test_spectator_state_survives_create_game_until_explicit_end() -> None:
    parser = PowerLogParser()

    feed(parser, "Begin Spectating 1st player", "CREATE_GAME")
    assert parser.snapshot().phase == "spectator"
    feed(
        parser,
        "SHOW_ENTITY - Updating Entity=[entityName=幸运币 id=40 zone=HAND zonePos=1 cardId= player=3] CardID=GAME_005",
    )
    assert parser.local_controller is None

    feed(parser, "End Spectator Mode", "CREATE_GAME")
    assert parser.snapshot().phase == "starting"


def test_unknown_and_oversized_lines_are_ignored_without_state_corruption() -> None:
    parser = PowerLogParser()
    feed(parser, "CREATE_GAME")

    assert parser.feed_line(line("FUTURE_PACKET key=value"), now=100.0) == []
    assert parser.feed_line("x" * (256 * 1024 + 1), now=100.0) == []
    assert parser.snapshot().game_number == 1


def test_battlegrounds_tracks_eight_hero_lobby_shop_phase_and_result() -> None:
    parser = PowerLogParser()
    feed(
        parser,
        "CREATE_GAME",
        "GameEntity EntityID=1",
        "Player EntityID=8 PlayerID=3 GameAccountId=[hi=0 lo=0]",
        "Player EntityID=9 PlayerID=11 GameAccountId=[hi=0 lo=0]",
    )
    feed(parser, "    tag=BACON_DUMMY_PLAYER value=1")
    assert parser.local_controller == 3
    feed(parser, "GameState.DebugPrintGame() - GameType=GT_BATTLEGROUNDS")

    for player_id in range(1, 9):
        entity_id = 100 + player_id
        controller = 3 if player_id == 3 else 11
        zone = "PLAY" if player_id == 3 else "SETASIDE"
        add_entity(
            parser,
            entity_id,
            f"TB_BaconShop_HERO_{player_id:02d}",
            controller=controller,
            zone=zone,
            card_type="HERO",
        )
        feed(
            parser,
            f"    tag=PLAYER_ID value={player_id}",
            "    tag=HEALTH value=40",
            "    tag=DAMAGE value=0",
            "    tag=PLAYER_TECH_LEVEL value=1",
            f"    tag=PLAYER_LEADERBOARD_PLACE value={player_id}",
        )

    feed(
        parser,
        "TAG_CHANGE Entity=8 tag=RESOURCES value=5",
        "TAG_CHANGE Entity=8 tag=RESOURCES_USED value=2",
        "TAG_CHANGE Entity=8 tag=TEMP_RESOURCES value=1",
        "TAG_CHANGE Entity=8 tag=NEXT_OPPONENT_PLAYER_ID value=5",
        "TAG_CHANGE Entity=GameEntity tag=TURN value=3",
        "TAG_CHANGE Entity=8 tag=CURRENT_PLAYER value=1",
    )
    add_entity(parser, 300, "BG_MINION_001", controller=11, zone="PLAY", card_type="MINION")
    feed(parser, "    tag=ATK value=3", "    tag=HEALTH value=4", "    tag=FROZEN value=1")

    snapshot = parser.snapshot()
    battlegrounds = snapshot.battlegrounds
    assert snapshot.mode == "battlegrounds"
    assert snapshot.phase == "recruit"
    assert battlegrounds is not None
    assert battlegrounds.round == 2
    assert battlegrounds.gold == 4
    assert battlegrounds.frozen is True
    assert snapshot.opponent.board_count == 0
    assert battlegrounds.next_opponent_player_id == 5
    assert len(battlegrounds.lobby) == 8
    assert len([player for player in battlegrounds.lobby if player.is_local]) == 1
    assert battlegrounds.shop[0].card_id == "BG_MINION_001"

    feed(parser, "TAG_CHANGE Entity=GameEntity tag=2022 value=1")
    events = feed(parser, "TAG_CHANGE Entity=GameEntity tag=2022 value=0")
    assert parser.snapshot().phase == "combat"
    assert any(event.kind == "battlegrounds_combat_started" for event in events)

    feed(parser, "TAG_CHANGE Entity=103 tag=PLAYER_LEADERBOARD_PLACE value=4")
    end_events = feed(parser, "TAG_CHANGE Entity=GameEntity tag=STATE value=COMPLETE")
    ended = next(event for event in end_events if event.kind == "battlegrounds_game_ended")
    assert ended.details["placement"] == 4
    assert ended.details["hero_card_id"] == "TB_BaconShop_HERO_03"
    assert parser.snapshot().battlegrounds.placement == 4


@pytest.mark.parametrize(
    ("tag", "event_kind", "first_value", "second_value"),
    [
        ("PLAYER_TECH_LEVEL", "battlegrounds_tavern_upgraded", 2, 3),
        ("PLAYER_TRIPLES", "battlegrounds_triple", 1, 2),
    ],
)
def test_battlegrounds_mirrored_player_counters_emit_once_per_real_increase(
    tag: str,
    event_kind: str,
    first_value: int,
    second_value: int,
) -> None:
    parser = PowerLogParser()
    feed(
        parser,
        "CREATE_GAME",
        "Player EntityID=8 PlayerID=3 GameAccountId=[hi=0 lo=0]",
        "Player EntityID=9 PlayerID=11 GameAccountId=[hi=0 lo=0]",
        "    tag=BACON_DUMMY_PLAYER value=1",
    )
    add_entity(parser, 103, "TB_BaconShop_HERO_03", controller=3, zone="PLAY", card_type="HERO")
    feed(parser, "    tag=PLAYER_ID value=3")

    baseline = first_value - 1
    feed(parser, f"TAG_CHANGE Entity=8 tag={tag} value={baseline}")
    feed(parser, f"TAG_CHANGE Entity=103 tag={tag} value={baseline}")

    first_events = feed(
        parser,
        f"TAG_CHANGE Entity=8 tag={tag} value={first_value}",
        f"TAG_CHANGE Entity=103 tag={tag} value={first_value}",
    )
    second_events = feed(
        parser,
        f"TAG_CHANGE Entity=103 tag={tag} value={second_value}",
        f"TAG_CHANGE Entity=8 tag={tag} value={second_value}",
    )

    assert [event.kind for event in first_events] == [event_kind]
    assert [event.kind for event in second_events] == [event_kind]


def test_battlegrounds_counter_deduplication_is_scoped_to_player_and_game() -> None:
    parser = PowerLogParser()
    feed(parser, "CREATE_GAME")
    parser.mode = "battlegrounds"

    add_entity(parser, 103, "TB_BaconShop_HERO_03", controller=3, zone="PLAY", card_type="HERO")
    feed(parser, "    tag=PLAYER_ID value=3", "    tag=PLAYER_TECH_LEVEL value=1")
    parser.local_controller = 3
    player_three_events = feed(parser, "TAG_CHANGE Entity=103 tag=PLAYER_TECH_LEVEL value=2")

    add_entity(parser, 104, "TB_BaconShop_HERO_04", controller=4, zone="PLAY", card_type="HERO")
    feed(parser, "    tag=PLAYER_ID value=4", "    tag=PLAYER_TECH_LEVEL value=1")
    parser.local_controller = 4
    player_four_events = feed(parser, "TAG_CHANGE Entity=104 tag=PLAYER_TECH_LEVEL value=2")

    feed(parser, "CREATE_GAME")
    parser.mode = "battlegrounds"
    add_entity(parser, 203, "TB_BaconShop_HERO_03", controller=3, zone="PLAY", card_type="HERO")
    feed(parser, "    tag=PLAYER_ID value=3", "    tag=PLAYER_TECH_LEVEL value=1")
    parser.local_controller = 3
    next_game_events = feed(parser, "TAG_CHANGE Entity=203 tag=PLAYER_TECH_LEVEL value=2")

    assert [event.kind for event in player_three_events] == ["battlegrounds_tavern_upgraded"]
    assert [event.kind for event in player_four_events] == ["battlegrounds_tavern_upgraded"]
    assert [event.kind for event in next_game_events] == ["battlegrounds_tavern_upgraded"]


def test_battlegrounds_duos_detection_uses_game_type_and_3533_transition() -> None:
    parser = PowerLogParser()
    feed(parser, "CREATE_GAME", "GameEntity EntityID=1")
    events = feed(parser, "GameState.DebugPrintGame() - GameType=GT_BATTLEGROUNDS_DUO")
    assert events[0].details["variant"] == "duos"

    feed(parser, "TAG_CHANGE Entity=GameEntity tag=3533 value=1")
    events = feed(parser, "TAG_CHANGE Entity=GameEntity tag=3533 value=0")

    assert parser.snapshot().battlegrounds.variant == "duos"
    assert parser.snapshot().phase == "combat"
    assert any(event.kind == "battlegrounds_combat_started" for event in events)


def test_battlegrounds_public_snapshot_never_exposes_hidden_shop_entity() -> None:
    parser = PowerLogParser()
    feed(
        parser,
        "CREATE_GAME",
        "Player EntityID=8 PlayerID=3 GameAccountId=[hi=0 lo=0]",
        "Player EntityID=9 PlayerID=11 GameAccountId=[hi=0 lo=0]",
    )
    feed(parser, "    tag=BACON_DUMMY_PLAYER value=1", "TAG_CHANGE Entity=8 tag=CURRENT_PLAYER value=1")
    add_entity(parser, 400, "", controller=11, zone="HAND", card_type="MINION")

    encoded = json.dumps(parser.snapshot().to_public_dict(), ensure_ascii=False)

    assert "UNKNOWN ENTITY" not in encoded
    assert parser.snapshot().battlegrounds is not None
    assert parser.snapshot().battlegrounds.shop == ()


def test_battlegrounds_shop_uses_newest_public_entity_per_position() -> None:
    parser = PowerLogParser()
    feed(
        parser,
        "CREATE_GAME",
        "Player EntityID=8 PlayerID=3 GameAccountId=[hi=0 lo=0]",
        "Player EntityID=9 PlayerID=11 GameAccountId=[hi=0 lo=0]",
        "    tag=BACON_DUMMY_PLAYER value=1",
        "TAG_CHANGE Entity=8 tag=MULLIGAN_STATE value=DONE",
    )
    for entity_id, card_id, position in (
        (300, "BG_OLD_SLOT_ONE", 1),
        (301, "BG_OLD_SLOT_TWO", 2),
        (500, "BG_CURRENT_SLOT_ONE", 1),
        (501, "BG_CURRENT_SLOT_TWO", 2),
        (502, "Bacon_TagTransferPlayerE", 0),
    ):
        add_entity(
            parser,
            entity_id,
            card_id,
            controller=11,
            zone="PLAY",
            card_type="MINION",
        )
        feed(parser, f"    tag=ZONE_POSITION value={position}")

    battlegrounds = parser.snapshot().battlegrounds

    assert battlegrounds is not None
    assert [card.card_id for card in battlegrounds.shop] == [
        "BG_CURRENT_SLOT_ONE",
        "BG_CURRENT_SLOT_TWO",
    ]


def test_battlegrounds_setup_signals_do_not_close_live_hero_selection() -> None:
    parser = PowerLogParser()
    feed(
        parser,
        "CREATE_GAME",
        "Player EntityID=8 PlayerID=3 GameAccountId=[hi=0 lo=0]",
        "Player EntityID=9 PlayerID=11 GameAccountId=[hi=0 lo=0]",
        "    tag=BACON_DUMMY_PLAYER value=1",
    )
    add_entity(
        parser,
        91,
        "BG_HERO_CHOICE",
        controller=3,
        zone="HAND",
        card_type="HERO",
    )
    feed(parser, "    tag=BACON_HERO_CAN_BE_DRAFTED value=1")

    feed(
        parser,
        "TAG_CHANGE Entity=8 tag=CURRENT_PLAYER value=1",
        "TAG_CHANGE Entity=GameEntity tag=STEP value=BEGIN_MULLIGAN",
    )

    battlegrounds = parser.snapshot().battlegrounds
    assert parser.snapshot().phase == "hero_select"
    assert battlegrounds is not None
    assert [choice.card_id for choice in battlegrounds.hero_choices] == ["BG_HERO_CHOICE"]


def test_battlegrounds_combat_edge_does_not_record_previous_bob_shop_as_opponent() -> None:
    parser = PowerLogParser()
    feed(
        parser,
        "CREATE_GAME",
        "GameEntity EntityID=1",
        "Player EntityID=8 PlayerID=3 GameAccountId=[hi=0 lo=0]",
        "Player EntityID=9 PlayerID=11 GameAccountId=[hi=0 lo=0]",
        "    tag=BACON_DUMMY_PLAYER value=1",
        "TAG_CHANGE Entity=8 tag=NEXT_OPPONENT_PLAYER_ID value=5",
        "TAG_CHANGE Entity=8 tag=CURRENT_PLAYER value=1",
    )
    add_entity(parser, 105, "TB_BaconShop_HERO_05", controller=5, zone="SETASIDE", card_type="HERO")
    feed(parser, "    tag=PLAYER_ID value=5", "    tag=HEALTH value=40")
    add_entity(parser, 300, "BG_SHOP_MINION", controller=11, zone="PLAY", card_type="MINION")
    feed(parser, "    tag=ATK value=3", "    tag=HEALTH value=4", "    tag=ZONE_POSITION value=1")

    feed(
        parser,
        "TAG_CHANGE Entity=GameEntity tag=2022 value=1",
        "TAG_CHANGE Entity=GameEntity tag=2022 value=0",
    )

    opponent = next(player for player in parser.snapshot().battlegrounds.lobby if player.player_id == 5)
    assert opponent.board_count == 0
    assert opponent.board_cards == ()


def test_battlegrounds_observes_new_bob_controlled_combat_proxy_after_phase_edge() -> None:
    parser = PowerLogParser()
    feed(
        parser,
        "CREATE_GAME",
        "GameEntity EntityID=1",
        "Player EntityID=8 PlayerID=3 GameAccountId=[hi=0 lo=0]",
        "Player EntityID=9 PlayerID=11 GameAccountId=[hi=0 lo=0]",
        "    tag=BACON_DUMMY_PLAYER value=1",
        "TAG_CHANGE Entity=8 tag=NEXT_OPPONENT_PLAYER_ID value=5",
        "TAG_CHANGE Entity=8 tag=CURRENT_PLAYER value=1",
    )
    add_entity(parser, 105, "TB_BaconShop_HERO_05", controller=5, zone="SETASIDE", card_type="HERO")
    feed(parser, "    tag=PLAYER_ID value=5", "    tag=HEALTH value=40")
    add_entity(parser, 300, "BG_SHOP_MINION", controller=11, zone="PLAY", card_type="MINION")
    feed(parser, "    tag=ATK value=3", "    tag=HEALTH value=4", "    tag=ZONE_POSITION value=1")
    feed(
        parser,
        "TAG_CHANGE Entity=GameEntity tag=2022 value=1",
        "TAG_CHANGE Entity=GameEntity tag=2022 value=0",
    )

    add_entity(parser, 301, "BG_COMBAT_PROXY", controller=11, zone="PLAY", card_type="MINION")
    feed(parser, "    tag=ATK value=7", "    tag=HEALTH value=8", "    tag=ZONE_POSITION value=1")

    before_marker = next(
        player for player in parser.snapshot().battlegrounds.lobby if player.player_id == 5
    )
    assert before_marker.board_count == 0

    feed(parser, "TAG_CHANGE Entity=301 tag=DEFENDING value=1")

    opponent = next(player for player in parser.snapshot().battlegrounds.lobby if player.player_id == 5)
    assert opponent.board_count == 1
    assert opponent.board_cards == ("BG_COMBAT_PROXY",)
    assert opponent.board_attack == 7
    assert opponent.board_health == 8
    assert [card.to_public_dict() for card in opponent.board_minions] == [
        {
            "card_id": "BG_COMBAT_PROXY",
            "name": "BG_COMBAT_PROXY",
            "attack": 7,
            "health": 8,
            "tier": 0,
            "frozen": False,
        }
    ]


def test_next_opponent_seen_before_local_controller_is_used_for_board_history() -> None:
    parser = PowerLogParser()
    feed(
        parser,
        "CREATE_GAME",
        "GameEntity EntityID=1",
        "Player EntityID=8 PlayerID=3 GameAccountId=[hi=0 lo=0]",
        "TAG_CHANGE Entity=8 tag=NEXT_OPPONENT_PLAYER_ID value=5",
        "Player EntityID=9 PlayerID=11 GameAccountId=[hi=0 lo=0]",
        "    tag=BACON_DUMMY_PLAYER value=1",
    )
    # Battlegrounds can expose remote hero entities under the local controller;
    # PLAYER_ID remains the authoritative lobby identity.
    add_entity(parser, 105, "TB_BaconShop_HERO_05", controller=3, zone="SETASIDE")
    feed(parser, "    tag=PLAYER_ID value=5", "    tag=HEALTH value=40")
    parser.phase = "combat"
    parser.battlegrounds_round = 1
    add_entity(parser, 301, "BG_OBSERVED", controller=5, zone="PLAY", card_type="MINION")
    feed(
        parser,
        "    tag=ATK value=4",
        "    tag=HEALTH value=5",
        "    tag=ZONE_POSITION value=1",
        "TAG_CHANGE Entity=301 tag=ATTACKING value=1",
    )

    battlegrounds = parser.snapshot().battlegrounds
    assert battlegrounds is not None
    assert battlegrounds.next_opponent_player_id == 5
    opponent = next(player for player in battlegrounds.lobby if player.player_id == 5)
    assert opponent.next_opponent is True
    assert opponent.board_cards == ("BG_OBSERVED",)


def test_battlegrounds_combat_marker_confirms_reused_bob_proxy_cohort() -> None:
    parser = PowerLogParser()
    feed(
        parser,
        "CREATE_GAME",
        "GameEntity EntityID=1",
        "Player EntityID=8 PlayerID=3 GameAccountId=[hi=0 lo=0]",
        "Player EntityID=9 PlayerID=11 GameAccountId=[hi=0 lo=0]",
        "    tag=BACON_DUMMY_PLAYER value=1",
        "TAG_CHANGE Entity=8 tag=NEXT_OPPONENT_PLAYER_ID value=5",
        "TAG_CHANGE Entity=8 tag=CURRENT_PLAYER value=1",
    )
    add_entity(parser, 105, "TB_BaconShop_HERO_05", controller=5, zone="SETASIDE", card_type="HERO")
    feed(parser, "    tag=PLAYER_ID value=5", "    tag=HEALTH value=40")
    for entity_id, card_id, attack, health, position in (
        (300, "BG_REUSED_PROXY_1", 3, 4, 1),
        (301, "BG_REUSED_PROXY_2", 5, 6, 2),
    ):
        add_entity(parser, entity_id, card_id, controller=11, zone="PLAY", card_type="MINION")
        feed(
            parser,
            f"    tag=ATK value={attack}",
            f"    tag=HEALTH value={health}",
            f"    tag=ZONE_POSITION value={position}",
        )
    feed(
        parser,
        "TAG_CHANGE Entity=GameEntity tag=2022 value=1",
        "TAG_CHANGE Entity=GameEntity tag=2022 value=0",
    )

    assert parser._observed_boards == {}
    feed(parser, "TAG_CHANGE Entity=300 tag=ATTACKING value=1")

    assert parser._observed_boards[5][0] == parser.battlegrounds_round
    opponent = next(player for player in parser.snapshot().battlegrounds.lobby if player.player_id == 5)
    assert opponent.board_cards == ("BG_REUSED_PROXY_1", "BG_REUSED_PROXY_2")
    assert opponent.board_attack == 8
    assert opponent.board_health == 10


def test_observed_opponent_board_freezes_first_public_combat_lineup() -> None:
    parser = PowerLogParser()
    parser.mode = "battlegrounds"
    parser.phase = "combat"
    parser.battlegrounds_round = 2
    parser.local_controller = 3
    parser.next_opponent_player_id = 5

    add_entity(parser, 301, "BG_FIRST", controller=5, zone="PLAY")
    feed(
        parser,
        "    tag=ATK value=2",
        "    tag=HEALTH value=3",
        "    tag=ZONE_POSITION value=1",
        "TAG_CHANGE Entity=301 tag=ATTACKING value=1",
    )
    assert len(parser._observed_boards[5][1]) == 1

    add_entity(parser, 302, "BG_SECOND", controller=5, zone="PLAY", card_type="MINION")
    feed(
        parser,
        "    tag=ATK value=4",
        "    tag=HEALTH value=5",
        "    tag=ZONE_POSITION value=2",
        "TAG_CHANGE Entity=302 tag=DEFENDING value=1",
    )

    assert [card.card_id for card in parser._observed_boards[5][1]] == ["BG_FIRST"]


def test_observed_opponent_board_deduplicates_direct_and_bob_positions() -> None:
    parser = PowerLogParser()
    parser.mode = "battlegrounds"
    parser.phase = "combat"
    parser.battlegrounds_round = 2
    parser.local_controller = 3
    parser.bob_controller = 11
    parser.next_opponent_player_id = 5

    add_entity(parser, 301, "DIRECT_POS1", controller=5, zone="PLAY", card_type="MINION")
    feed(parser, "    tag=ZONE_POSITION value=1", "    tag=ATK value=2", "    tag=HEALTH value=3")
    add_entity(parser, 401, "BOB_POS1", controller=11, zone="PLAY", card_type="MINION")
    feed(parser, "    tag=ZONE_POSITION value=1", "    tag=ATK value=2", "    tag=HEALTH value=3")
    add_entity(parser, 402, "BOB_POS2", controller=11, zone="PLAY", card_type="MINION")
    feed(
        parser,
        "    tag=ZONE_POSITION value=2",
        "    tag=ATK value=4",
        "    tag=HEALTH value=5",
        "TAG_CHANGE Entity=402 tag=ATTACKING value=1",
    )

    cards = parser._observed_boards[5][1]
    assert [card.card_id for card in cards] == ["DIRECT_POS1", "BOB_POS2"]


def test_snapshot_is_pure_for_observed_boards_and_recruit_warband_cache() -> None:
    parser = PowerLogParser()
    parser.mode = "battlegrounds"
    parser.phase = "combat"
    parser.battlegrounds_round = 2
    parser.local_controller = 3
    parser.bob_controller = 11
    parser.next_opponent_player_id = 5
    parser.entities[300] = Entity(
        300,
        card_id="BG_UNCONFIRMED_PROXY",
        controller=11,
        zone="PLAY",
        card_type="MINION",
        tags={"ATK": "3", "HEALTH": "4", "ZONE_POSITION": "1"},
        revealed=True,
    )

    first = parser.snapshot().to_public_dict()
    second = parser.snapshot().to_public_dict()

    assert first == second
    assert parser._observed_boards == {}
    assert parser._last_recruit_warband == ()

    parser.phase = "recruit"
    parser.entities[301] = Entity(
        301,
        card_id="BG_LOCAL_MINION",
        controller=3,
        zone="PLAY",
        card_type="MINION",
        tags={"ATK": "5", "HEALTH": "6", "ZONE_POSITION": "1"},
        revealed=True,
    )
    recruit_first = parser.snapshot().to_public_dict()
    recruit_second = parser.snapshot().to_public_dict()

    assert recruit_first == recruit_second
    assert parser._observed_boards == {}
    assert parser._last_recruit_warband == ()


def test_battlegrounds_hero_choices_include_local_skin_but_exclude_locked_and_remote() -> None:
    parser = PowerLogParser()
    feed(
        parser,
        "CREATE_GAME",
        "Player EntityID=8 PlayerID=3 GameAccountId=[hi=0 lo=0]",
        "Player EntityID=9 PlayerID=11 GameAccountId=[hi=0 lo=0]",
        "    tag=BACON_DUMMY_PLAYER value=1",
    )
    add_entity(parser, 91, "BG_HERO_CHOICE", controller=3, zone="HAND", card_type="HERO")
    feed(parser, "    tag=BACON_HERO_CAN_BE_DRAFTED value=1")
    add_entity(parser, 92, "BG_HERO_LOCKED", controller=3, zone="HAND", card_type="HERO")
    feed(
        parser,
        "    tag=BACON_HERO_CAN_BE_DRAFTED value=1",
        "    tag=BACON_LOCKED_MULLIGAN_HERO value=1",
    )
    add_entity(parser, 93, "BG_HERO_SKIN", controller=3, zone="PLAY", card_type="HERO")
    feed(parser, "    tag=BACON_SKIN value=1", "    tag=PLAYER_ID value=3")
    add_entity(parser, 105, "BG_ASSIGNED_HERO", controller=11, zone="SETASIDE", card_type="HERO")
    feed(parser, "    tag=BACON_HERO_CAN_BE_DRAFTED value=1", "    tag=PLAYER_ID value=5")

    battlegrounds = parser.snapshot().battlegrounds

    assert battlegrounds is not None
    assert [choice.card_id for choice in battlegrounds.hero_choices] == [
        "BG_HERO_CHOICE",
        "BG_HERO_SKIN",
    ]
    assert battlegrounds.to_public_dict()["hero_choices"] == [
        {"card_id": "BG_HERO_CHOICE", "name": "BG_HERO_CHOICE"},
        {"card_id": "BG_HERO_SKIN", "name": "BG_HERO_SKIN"},
    ]
    assert [player.hero_card_id for player in battlegrounds.lobby] == ["BG_ASSIGNED_HERO"]

    feed(parser, "TAG_CHANGE Entity=8 tag=MULLIGAN_STATE value=DONE")
    recruit = parser.snapshot().battlegrounds

    assert recruit.hero_choices == ()
    assert [player.hero_card_id for player in recruit.lobby] == [
        "BG_HERO_SKIN",
        "BG_ASSIGNED_HERO",
    ]


def test_battlegrounds_hero_choice_tag_is_authoritative_without_cardtype_packet() -> None:
    parser = PowerLogParser()
    feed(
        parser,
        "CREATE_GAME",
        "Player EntityID=8 PlayerID=3 GameAccountId=[hi=0 lo=0]",
        "Player EntityID=9 PlayerID=11 GameAccountId=[hi=0 lo=0]",
        "    tag=BACON_DUMMY_PLAYER value=1",
    )
    add_entity(parser, 91, "BG_HERO_WITHOUT_TYPE", controller=3, zone="SETASIDE")
    feed(parser, "    tag=BACON_HERO_CAN_BE_DRAFTED value=1")

    battlegrounds = parser.snapshot().battlegrounds

    assert battlegrounds is not None
    assert [choice.card_id for choice in battlegrounds.hero_choices] == [
        "BG_HERO_WITHOUT_TYPE"
    ]


def test_battlegrounds_duos_marks_only_same_explicit_team_id_as_teammate() -> None:
    parser = PowerLogParser()
    feed(
        parser,
        "CREATE_GAME",
        "Player EntityID=8 PlayerID=3 GameAccountId=[hi=0 lo=0]",
        "Player EntityID=9 PlayerID=11 GameAccountId=[hi=0 lo=0]",
        "    tag=BACON_DUMMY_PLAYER value=1",
        "GameState.DebugPrintGame() - GameType=GT_BATTLEGROUNDS_DUO",
        "TAG_CHANGE Entity=8 tag=BACON_DUO_TEAM_ID value=7",
    )
    for player_id, team_id in ((3, 7), (4, 7), (5, 8), (6, 0)):
        add_entity(
            parser,
            100 + player_id,
            f"BG_DUO_HERO_{player_id}",
            controller=player_id,
            zone="SETASIDE",
            card_type="HERO",
        )
        feed(
            parser,
            f"    tag=PLAYER_ID value={player_id}",
            f"    tag=BACON_DUO_TEAM_ID value={team_id}",
        )

    lobby = {player.player_id: player for player in parser.snapshot().battlegrounds.lobby}

    assert lobby[3].is_local is True
    assert lobby[3].is_teammate is False
    assert lobby[4].is_teammate is True
    assert lobby[5].is_teammate is False
    assert lobby[6].is_teammate is False
    public_lobby = {
        player["player_id"]: player for player in parser.snapshot().battlegrounds.to_public_dict()["lobby"]
    }
    assert public_lobby[4]["is_teammate"] is True
    assert public_lobby[5]["is_teammate"] is False


def test_battlegrounds_does_not_guess_hero_choices_without_local_controller() -> None:
    parser = PowerLogParser()
    parser.mode = "battlegrounds"
    parser.phase = "hero_select"
    add_entity(parser, 91, "BG_HERO_CHOICE", controller=7, zone="HAND", card_type="HERO")
    feed(parser, "    tag=BACON_HERO_CAN_BE_DRAFTED value=1")

    assert parser.snapshot().battlegrounds.hero_choices == ()


def test_battlegrounds_rehidden_known_card_never_exposes_card_id_or_stats() -> None:
    parser = PowerLogParser()
    feed(
        parser,
        "CREATE_GAME",
        "Player EntityID=8 PlayerID=3 GameAccountId=[hi=0 lo=0]",
        "Player EntityID=9 PlayerID=11 GameAccountId=[hi=0 lo=0]",
        "    tag=BACON_DUMMY_PLAYER value=1",
        "TAG_CHANGE Entity=8 tag=MULLIGAN_STATE value=DONE",
    )
    add_entity(parser, 400, "BG_SECRET", controller=11, zone="PLAY", card_type="MINION")
    feed(
        parser,
        "    tag=ATK value=9",
        "    tag=HEALTH value=9",
        "HIDE_ENTITY - Entity=[entityName=Secret id=400 zone=PLAY zonePos=1 cardId=BG_SECRET player=11] tag=1068 value=1",
    )

    public = json.dumps(parser.snapshot().to_public_dict(), ensure_ascii=False)

    assert "BG_SECRET" not in public
    assert parser.snapshot().battlegrounds is not None
    assert parser.snapshot().battlegrounds.shop == ()


@pytest.mark.parametrize(
    ("raw_type", "expected"),
    [("39", "LOCATION"), ("42", "BATTLEGROUND_SPELL"), ("BATTLEGROUND_SPELL", "BATTLEGROUND_SPELL")],
)
def test_current_numeric_card_types_are_normalized(raw_type: str, expected: str) -> None:
    parser = PowerLogParser()
    add_entity(parser, 42, "BG_CARD", controller=11, zone="PLAY", card_type=raw_type)

    assert parser.entities[42].card_type == expected


def test_numeric_battleground_spell_appears_in_recruit_shop() -> None:
    parser = PowerLogParser()
    feed(
        parser,
        "CREATE_GAME",
        "Player EntityID=8 PlayerID=3 GameAccountId=[hi=0 lo=0]",
        "Player EntityID=9 PlayerID=11 GameAccountId=[hi=0 lo=0]",
        "    tag=BACON_DUMMY_PLAYER value=1",
        "TAG_CHANGE Entity=8 tag=MULLIGAN_STATE value=DONE",
    )
    add_entity(parser, 420, "BG_SPELL", controller=11, zone="PLAY", card_type="42")

    battlegrounds = parser.snapshot().battlegrounds

    assert battlegrounds is not None
    assert [card.card_id for card in battlegrounds.shop] == ["BG_SPELL"]


def test_power_task_list_is_the_canonical_realtime_power_stream() -> None:
    parser = PowerLogParser()

    mirrored = parser.feed_line(source_line("GameState", "CREATE_GAME"), now=100.0)
    realtime = parser.feed_line(source_line("PowerTaskList", "CREATE_GAME"), now=101.0)

    assert mirrored == []
    assert [event.kind for event in realtime] == ["game_started"]
    assert parser.snapshot().game_number == 1


def test_game_state_power_mirror_cannot_override_realtime_task_list_state() -> None:
    parser = PowerLogParser()

    parser.feed_line(source_line("GameState", "GameEntity EntityID=1"), now=100.0)
    parser.feed_line(
        source_line("PowerTaskList", "CREATE_GAME"),
        now=101.0,
    )
    parser.feed_line(
        source_line("PowerTaskList", "GameEntity EntityID=1"),
        now=102.0,
    )
    parser.feed_line(
        source_line("PowerTaskList", "TAG_CHANGE Entity=GameEntity tag=TURN value=2"),
        now=103.0,
    )
    parser.feed_line(
        source_line("GameState", "TAG_CHANGE Entity=GameEntity tag=TURN value=99"),
        now=104.0,
    )

    assert parser.snapshot().turn == 2


def test_game_state_static_entity_packets_enrich_battlegrounds_realtime_state() -> None:
    parser = PowerLogParser()
    parser.mode = "battlegrounds"
    parser.phase = "combat"
    parser.local_controller = 3
    parser.bob_controller = 11
    parser.next_opponent_player_id = 5
    parser.battlegrounds_round = 2

    parser.feed_line(
        source_line("GameState", "FULL_ENTITY - Creating ID=301 CardID=BG_PROXY"),
        now=100.0,
    )
    for payload in (
        "    tag=CONTROLLER value=11",
        "    tag=ZONE value=PLAY",
        "    tag=ZONE_POSITION value=1",
    ):
        parser.feed_line(source_line("GameState", payload), now=101.0)

    parser.feed_line(
        source_line("GameState", "TAG_CHANGE Entity=GameEntity tag=TURN value=99"),
        now=102.0,
    )
    parser.feed_line(
        source_line("PowerTaskList", "TAG_CHANGE Entity=301 tag=DEFENDING value=1"),
        now=103.0,
    )

    assert parser.snapshot().turn == 0
    assert [card.card_id for card in parser._observed_boards[5][1]] == ["BG_PROXY"]


def test_delayed_game_state_static_packet_cannot_rollback_realtime_ref_fields() -> None:
    parser = PowerLogParser()
    parser.mode = "battlegrounds"

    feed(
        parser,
        "FULL_ENTITY - Updating Entity=[entityName=Live id=301 zone=PLAY zonePos=1 cardId=BG_REAL player=11] CardID=BG_REAL",
        "    tag=ATK value=7",
        "    tag=HEALTH value=8",
        "    tag=FROZEN value=0",
    )
    parser.feed_line(
        source_line("GameState", "FULL_ENTITY - Creating ID=301 CardID=BG_OLD"),
        now=101.0,
    )
    for payload in (
        "    tag=CONTROLLER value=3",
        "    tag=ZONE value=SETASIDE",
        "    tag=ATK value=1",
        "    tag=HEALTH value=2",
        "    tag=FROZEN value=1",
    ):
        parser.feed_line(source_line("GameState", payload), now=102.0)

    entity = parser.entities[301]
    assert entity.card_id == "BG_REAL"
    assert entity.controller == 11
    assert entity.zone == "PLAY"
    assert entity.attack == 7
    assert entity.health == 8
    assert entity.tag_int("FROZEN") == 0


def test_ignored_game_state_entity_packet_clears_static_inline_target() -> None:
    parser = PowerLogParser()
    parser.mode = "battlegrounds"
    parser.bob_controller = 11
    parser.feed_line(
        source_line("GameState", "FULL_ENTITY - Creating ID=301 CardID=BG_PUBLIC"),
        now=101.0,
    )
    parser.feed_line(
        source_line(
            "GameState",
            "SHOW_ENTITY - Updating Entity=[entityName=Hidden id=302 zone=PLAY zonePos=1 cardId= player=11] CardID=BG_OTHER",
        ),
        now=102.0,
    )
    for payload in (
        "    tag=CONTROLLER value=11",
        "    tag=ZONE value=PLAY",
        "    tag=ZONE_POSITION value=1",
        "    tag=ATK value=9",
        "    tag=HEALTH value=9",
    ):
        parser.feed_line(source_line("GameState", payload), now=103.0)

    entity = parser.entities[301]
    assert entity.controller is None
    assert entity.zone == ""
    assert entity.attack == 0
    assert entity.health is None
    assert 302 not in parser.entities
    assert parser._battlegrounds_board_entities(11) == []


def test_unknown_battlegrounds_play_entity_requires_public_minion_evidence() -> None:
    parser = PowerLogParser()
    parser.mode = "battlegrounds"
    parser.phase = "combat"
    parser.local_controller = 3
    parser.next_opponent_player_id = 5
    add_entity(parser, 301, "INTERNAL_EFFECT", controller=5, zone="PLAY")

    assert parser._battlegrounds_board_entities(5) == []

    feed(parser, "TAG_CHANGE Entity=301 tag=CARDTYPE value=MINION")

    assert [entity.entity_id for entity in parser._battlegrounds_board_entities(5)] == [301]


def test_delayed_game_state_static_tag_cannot_rollback_realtime_value() -> None:
    parser = PowerLogParser()
    parser.mode = "battlegrounds"
    add_entity(parser, 301, "BG_MINION", controller=3, zone="PLAY", card_type="MINION")
    feed(parser, "    tag=HEALTH value=10")

    parser.feed_line(
        source_line("GameState", "FULL_ENTITY - Creating ID=301 CardID=BG_OLD"),
        now=101.0,
    )
    parser.feed_line(
        source_line("GameState", "    tag=HEALTH value=40"),
        now=102.0,
    )

    assert parser.entities[301].card_id == "BG_MINION"
    assert parser.entities[301].health == 10


def test_delayed_game_state_static_packet_cannot_reveal_hidden_entity() -> None:
    parser = PowerLogParser()
    parser.mode = "battlegrounds"
    add_entity(parser, 301, "BG_PRIVATE", controller=5, zone="PLAY", card_type="MINION")
    feed(
        parser,
        "HIDE_ENTITY - Entity=[entityName=Private id=301 zone=PLAY zonePos=1 cardId=BG_PRIVATE player=5] tag=1068 value=1",
    )

    parser.feed_line(
        source_line("GameState", "FULL_ENTITY - Creating ID=301 CardID=BG_PRIVATE"),
        now=101.0,
    )
    parser.feed_line(
        source_line("GameState", "    tag=ZONE value=PLAY"),
        now=102.0,
    )

    public_state = json.dumps(parser.snapshot().to_public_dict(), ensure_ascii=False)
    assert parser.entities[301].hidden is True
    assert parser.entities[301].visibility_revoked is True
    assert "BG_PRIVATE" not in public_state


def test_delayed_game_state_ref_cannot_retain_hidden_name_for_later_reveal() -> None:
    parser = PowerLogParser()
    parser.mode = "battlegrounds"
    add_entity(parser, 20, "BG_SECRET", controller=5, zone="PLAY", card_type="MINION")
    feed(
        parser,
        "HIDE_ENTITY - Entity=[entityName=Private id=20 zone=PLAY zonePos=1 cardId=BG_SECRET player=5] tag=1068 value=1",
    )
    parser.feed_line(
        source_line(
            "GameState",
            "FULL_ENTITY - Updating Entity=[entityName=Hidden Replay Name id=20 zone=PLAY zonePos=1 cardId=BG_SECRET player=5] CardID=BG_SECRET",
        ),
        now=101.0,
    )

    assert parser.entities[20].name == ""
    assert parser.entities[20].visibility_revoked is True

    feed(parser, "SHOW_ENTITY - Updating Entity=20 CardID=BG_NEW_PUBLIC")

    assert parser.entities[20].public_name() == "BG_NEW_PUBLIC"
    assert "Hidden Replay Name" not in json.dumps(
        parser.snapshot().to_public_dict(),
        ensure_ascii=False,
    )


def test_game_state_full_entity_ref_with_empty_tail_identity_stays_hidden() -> None:
    parser = PowerLogParser()
    parser.mode = "battlegrounds"
    parser.phase = "combat"
    parser.local_controller = 3
    parser.next_opponent_player_id = 5
    parser.feed_line(
        source_line(
            "GameState",
            "FULL_ENTITY - Updating Entity=[entityName=Ref Only Secret id=20 zone=PLAY zonePos=1 cardId=BG_REF_SECRET player=5] CardID=",
        ),
        now=101.0,
    )
    for payload in (
        "    tag=CONTROLLER value=5",
        "    tag=ZONE value=PLAY",
        "    tag=ZONE_POSITION value=1",
        "    tag=ATK value=9",
        "    tag=HEALTH value=9",
    ):
        parser.feed_line(source_line("GameState", payload), now=102.0)
    parser.feed_line(
        source_line("PowerTaskList", "TAG_CHANGE Entity=20 tag=ATTACKING value=1"),
        now=103.0,
    )

    entity = parser.entities[20]
    public_state = json.dumps(parser.snapshot().to_public_dict(), ensure_ascii=False)
    assert entity.card_id == ""
    assert entity.name == ""
    assert entity.hidden is True
    assert parser._observed_boards == {}
    assert "BG_REF_SECRET" not in public_state
    assert "Ref Only Secret" not in public_state


def test_hidden_battlegrounds_hero_is_removed_from_public_lobby() -> None:
    parser = PowerLogParser()
    parser.mode = "battlegrounds"
    add_entity(parser, 105, "BG_HIDDEN_HERO", controller=5, zone="PLAY", card_type="HERO")
    feed(parser, "    tag=PLAYER_ID value=5")

    assert [player.player_id for player in parser.snapshot().battlegrounds.lobby] == [5]

    feed(
        parser,
        "HIDE_ENTITY - Entity=[entityName=Private Hero id=105 zone=PLAY zonePos=0 cardId=BG_HIDDEN_HERO player=5] tag=1068 value=1",
    )

    public_state = json.dumps(parser.snapshot().to_public_dict(), ensure_ascii=False)
    assert parser.snapshot().battlegrounds.lobby == ()
    assert "BG_HIDDEN_HERO" not in public_state


def test_game_state_debug_print_game_cannot_mutate_power_entities() -> None:
    parser = PowerLogParser()
    parser.mode = "battlegrounds"
    add_entity(parser, 301, "BG_PRIVATE", controller=5, zone="PLAY", card_type="MINION")
    feed(
        parser,
        "HIDE_ENTITY - Entity=[entityName=Private id=301 zone=PLAY zonePos=1 cardId=BG_PRIVATE player=5] tag=1068 value=1",
    )

    debug_game_prefix = "D 12:00:00.0000000 GameState.DebugPrintGame() - "
    parser.feed_line(
        debug_game_prefix
        + "SHOW_ENTITY - Updating Entity=[entityName=Private id=301 zone=PLAY zonePos=1 cardId=BG_PRIVATE player=5] CardID=BG_PRIVATE",
        now=101.0,
    )
    parser.feed_line(
        debug_game_prefix + "TAG_CHANGE Entity=301 tag=ZONE value=PLAY",
        now=102.0,
    )

    entity = parser.entities[301]
    public_state = json.dumps(parser.snapshot().to_public_dict(), ensure_ascii=False)
    assert entity.hidden is True
    assert entity.visibility_revoked is True
    assert "BG_PRIVATE" not in public_state


def test_game_state_game_metadata_remains_available_with_task_list_power() -> None:
    parser = PowerLogParser()

    started = parser.feed_line(source_line("PowerTaskList", "CREATE_GAME"), now=100.0)
    detected = parser.feed_line(
        "D 12:00:00.0000000 GameState.DebugPrintGame() - GameType=GT_BATTLEGROUNDS",
        now=101.0,
    )

    assert [event.kind for event in started] == ["game_started"]
    assert [event.kind for event in detected] == ["battlegrounds_detected"]
    assert parser.snapshot().mode == "battlegrounds"


def test_game_state_metadata_before_task_list_create_is_committed_to_new_game() -> None:
    parser = PowerLogParser()

    boundary = parser.feed_line(source_line("GameState", "CREATE_GAME"), now=100.0)
    metadata = parser.feed_line(
        "D 12:00:00.0000000 GameState.DebugPrintGame() - GameType=GT_BATTLEGROUNDS",
        now=101.0,
    )
    started = parser.feed_line(source_line("PowerTaskList", "CREATE_GAME"), now=102.0)

    assert boundary == []
    assert metadata == []
    assert [event.kind for event in started] == ["game_started", "battlegrounds_detected"]
    assert parser.snapshot().mode == "battlegrounds"
    assert parser.snapshot().phase == "hero_select"


def test_pending_game_state_entities_are_isolated_then_committed_to_new_game() -> None:
    parser = PowerLogParser()
    feed(parser, "CREATE_GAME", "GameEntity EntityID=1")
    old_game_number = parser.game_number

    parser.feed_line(source_line("GameState", "CREATE_GAME"), now=100.0)
    parser.feed_line(
        "D 12:00:00.0000000 GameState.DebugPrintGame() - GameType=GT_BATTLEGROUNDS",
        now=101.0,
    )
    parser.feed_line(source_line("GameState", "GameEntity EntityID=10"), now=102.0)
    parser.feed_line(
        source_line("GameState", "Player EntityID=18 PlayerID=3 GameAccountId=[hi=0 lo=0]"),
        now=103.0,
    )
    parser.feed_line(
        source_line("GameState", "    tag=MULLIGAN_STATE value=DONE"),
        now=104.0,
    )
    parser.feed_line(
        source_line("GameState", "Player EntityID=19 PlayerID=11 GameAccountId=[hi=0 lo=0]"),
        now=105.0,
    )
    parser.feed_line(
        source_line("GameState", "    tag=BACON_DUMMY_PLAYER value=1"),
        now=106.0,
    )

    assert parser.game_number == old_game_number
    assert parser.game_entity_id == 1
    assert 18 not in parser.entities
    assert parser.snapshot().mode == "unknown"

    started = parser.feed_line(source_line("PowerTaskList", "CREATE_GAME"), now=107.0)

    assert [event.kind for event in started] == ["game_started", "battlegrounds_detected"]
    assert parser.game_number == old_game_number + 1
    assert parser.game_entity_id == 10
    assert parser.player_entities == {3: 18, 11: 19}
    assert parser.local_controller == 3
    assert parser.bob_controller == 11
    assert parser._battlegrounds_hero_selection_complete is True
    assert parser.snapshot().phase == "recruit"


def test_game_state_terminal_signals_end_constructed_game_once() -> None:
    parser = PowerLogParser()
    feed(
        parser,
        "CREATE_GAME",
        "GameEntity EntityID=1",
        "Player EntityID=2 PlayerID=1 GameAccountId=[hi=0 lo=0]",
        "Player EntityID=3 PlayerID=2 GameAccountId=[hi=0 lo=0]",
        "FULL_ENTITY - Creating ID=40 CardID=",
        "    tag=CONTROLLER value=1",
        "    tag=ZONE value=HAND",
        "SHOW_ENTITY - Updating Entity=[entityName=Known id=40 zone=HAND zonePos=1 cardId= player=1] CardID=CS2_029",
    )

    ended = parser.feed_line(
        source_line("GameState", "TAG_CHANGE Entity=2 tag=PLAYSTATE value=WON"),
        now=101.0,
    )
    duplicate = parser.feed_line(
        source_line("GameState", "TAG_CHANGE Entity=2 tag=PLAYSTATE value=WON"),
        now=102.0,
    )
    completed = parser.feed_line(
        source_line("GameState", "TAG_CHANGE Entity=GameEntity tag=STATE value=COMPLETE"),
        now=103.0,
    )

    assert [event.kind for event in ended] == ["game_ended"]
    assert duplicate == []
    assert completed == []
    assert parser.snapshot().phase == "ended"
    assert parser.snapshot().result == "won"


def test_game_state_terminal_playstate_does_not_guess_unknown_numeric_entity() -> None:
    parser = PowerLogParser()
    feed(
        parser,
        "CREATE_GAME",
        "GameEntity EntityID=1",
        "Player EntityID=2 PlayerID=1 GameAccountId=[hi=0 lo=0]",
    )
    parser.local_controller = 1

    events = parser.feed_line(
        source_line("GameState", "TAG_CHANGE Entity=999 tag=PLAYSTATE value=WON"),
        now=101.0,
    )

    assert events == []
    assert parser.snapshot().phase != "ended"
    assert parser.snapshot().result == ""

    parser.local_controller = None
    no_controller = parser.feed_line(
        source_line("GameState", "TAG_CHANGE Entity=999 tag=PLAYSTATE value=WON"),
        now=102.0,
    )

    assert no_controller == []
    assert parser.snapshot().phase != "ended"
    assert parser.snapshot().result == ""

    parser.local_controller = 1
    unresolved_name = parser.feed_line(
        source_line("GameState", "TAG_CHANGE Entity=UnresolvedName tag=PLAYSTATE value=WON"),
        now=103.0,
    )

    assert unresolved_name == []
    assert parser.snapshot().phase != "ended"
    assert parser.snapshot().result == ""


def test_game_state_terminal_tags_reject_non_terminal_entity_types() -> None:
    parser = PowerLogParser()
    feed(
        parser,
        "CREATE_GAME",
        "GameEntity EntityID=1",
        "Player EntityID=2 PlayerID=1 GameAccountId=[hi=0 lo=0]",
    )
    parser.local_controller = 1
    add_entity(parser, 40, "CS2_029", controller=1, zone="PLAY", card_type="MINION")

    playstate = parser.feed_line(
        source_line("GameState", "TAG_CHANGE Entity=40 tag=PLAYSTATE value=WON"),
        now=101.0,
    )
    state = parser.feed_line(
        source_line("GameState", "TAG_CHANGE Entity=40 tag=STATE value=COMPLETE"),
        now=102.0,
    )
    unresolved_state = parser.feed_line(
        source_line("GameState", "TAG_CHANGE Entity=ArbitraryName tag=STATE value=COMPLETE"),
        now=103.0,
    )

    assert playstate == []
    assert state == []
    assert unresolved_state == []
    assert parser.snapshot().phase != "ended"
    assert parser.snapshot().result == ""


def test_delayed_local_inference_makes_hero_selection_completion_monotonic() -> None:
    parser = PowerLogParser()
    parser.mode = "battlegrounds"
    parser.phase = "hero_select"
    feed(
        parser,
        "Player EntityID=8 PlayerID=3 GameAccountId=[hi=0 lo=0]",
        "    tag=MULLIGAN_STATE value=DONE",
        "Player EntityID=9 PlayerID=11 GameAccountId=[hi=0 lo=0]",
        "    tag=BACON_DUMMY_PLAYER value=1",
    )

    assert parser.local_controller == 3
    assert parser._battlegrounds_hero_selection_complete is True
    assert parser.snapshot().phase == "recruit"


def test_late_battlegrounds_hint_reconciles_completed_hero_selection() -> None:
    parser = PowerLogParser()
    feed(
        parser,
        "Player EntityID=8 PlayerID=3 GameAccountId=[hi=0 lo=0]",
        "    tag=MULLIGAN_STATE value=INPUT",
        "    tag=MULLIGAN_STATE value=DONE",
        "TAG_CHANGE Entity=8 tag=BACON_TRINKETS_ACTIVE value=1",
    )

    assert parser.snapshot().mode == "battlegrounds"
    assert parser.local_controller == 3
    assert parser._battlegrounds_hero_selection_complete is True
    assert parser.snapshot().phase == "recruit"

    feed(parser, "TAG_CHANGE Entity=8 tag=MULLIGAN_STATE value=INPUT")

    assert parser._battlegrounds_hero_selection_complete is True
    assert parser.snapshot().phase == "recruit"


def test_terminal_event_escapes_stale_block_and_is_emitted_once() -> None:
    parser = PowerLogParser()
    feed(
        parser,
        "CREATE_GAME",
        "GameEntity EntityID=1",
        "Player EntityID=8 PlayerID=3 GameAccountId=[hi=0 lo=0]",
        "Player EntityID=9 PlayerID=11 GameAccountId=[hi=0 lo=0]",
        "    tag=BACON_DUMMY_PLAYER value=1",
    )
    add_entity(parser, 103, "TB_BaconShop_HERO_03", controller=3, zone="PLAY", card_type="HERO")
    feed(parser, "    tag=PLAYER_ID value=3", "    tag=PLAYER_LEADERBOARD_PLACE value=2")
    feed(
        parser,
        "BLOCK_START BlockType=TRIGGER Entity=GameEntity EffectCardId= EffectIndex=0 Target=0 SubOption=-1",
    )

    ended = parser.feed_line(
        source_line("GameState", "TAG_CHANGE Entity=GameEntity tag=STATE value=COMPLETE"),
        now=101.0,
    )
    duplicate = parser.feed_line(
        source_line("GameState", "TAG_CHANGE Entity=GameEntity tag=STATE value=COMPLETE"),
        now=102.0,
    )

    assert [event.kind for event in ended] == ["battlegrounds_game_ended"]
    assert duplicate == []
    assert parser._block_stack == []


def test_battlegrounds_round_event_is_emitted_once_per_computed_round() -> None:
    parser = PowerLogParser()
    feed(parser, "CREATE_GAME", "GameEntity EntityID=1", "TAG_CHANGE Entity=GameEntity tag=2022 value=1")

    events = feed(
        parser,
        "TAG_CHANGE Entity=GameEntity tag=TURN value=1",
        "TAG_CHANGE Entity=GameEntity tag=TURN value=2",
        "TAG_CHANGE Entity=GameEntity tag=TURN value=3",
        "TAG_CHANGE Entity=GameEntity tag=TURN value=4",
    )

    rounds = [event.details["round"] for event in events if event.kind == "battlegrounds_round"]
    assert rounds == [1, 2]
    assert parser.snapshot().battlegrounds.round == 2


@pytest.mark.parametrize(
    ("variant", "phase_tag"),
    [("solo", "2022"), ("duos", "3533")],
)
def test_first_active_battlegrounds_phase_signal_recovers_recruit_then_transitions_to_combat(
    variant: str,
    phase_tag: str,
) -> None:
    parser = PowerLogParser()
    feed(parser, "CREATE_GAME", "GameEntity EntityID=1")
    parser.mode = "battlegrounds"
    parser.battlegrounds_variant = variant
    parser.phase = "hero_select"

    first = feed(parser, f"TAG_CHANGE Entity=GameEntity tag={phase_tag} value=1")
    phase_after_first = parser.snapshot().phase
    second = feed(parser, f"TAG_CHANGE Entity=GameEntity tag={phase_tag} value=0")

    assert first == []
    assert phase_after_first == "recruit"
    assert parser.snapshot().phase == "combat"
    assert [event.kind for event in second] == ["battlegrounds_combat_started"]


@pytest.mark.parametrize(
    ("variant", "phase_tag"),
    [("solo", "2022"), ("duos", "3533")],
)
def test_first_in_progress_battlegrounds_phase_zero_recovers_combat(
    variant: str,
    phase_tag: str,
) -> None:
    parser = PowerLogParser()
    feed(parser, "CREATE_GAME", "GameEntity EntityID=1")
    parser.mode = "battlegrounds"
    parser.battlegrounds_variant = variant
    parser.phase = "hero_select"
    parser.turn = 3
    parser.battlegrounds_round = 2

    events = feed(parser, f"TAG_CHANGE Entity=GameEntity tag={phase_tag} value=0")

    assert parser.snapshot().phase == "combat"
    assert [event.kind for event in events] == ["battlegrounds_combat_started"]


def test_battlegrounds_caches_authoritative_recruit_board_and_filters_ui_helpers() -> None:
    parser = PowerLogParser()
    feed(
        parser,
        "CREATE_GAME",
        "GameEntity EntityID=1",
        "Player EntityID=8 PlayerID=3 GameAccountId=[hi=0 lo=0]",
        "Player EntityID=9 PlayerID=11 GameAccountId=[hi=0 lo=0]",
        "    tag=BACON_DUMMY_PLAYER value=1",
        "TAG_CHANGE Entity=8 tag=CURRENT_PLAYER value=1",
    )
    add_entity(parser, 200, "BG_REAL_MINION", controller=3, zone="PLAY", card_type="MINION")
    feed(
        parser,
        "    tag=ATK value=7",
        "    tag=HEALTH value=8",
        "    tag=ZONE_POSITION value=1",
        "SHOW_ENTITY - Updating Entity=[entityName=Real Minion id=200 zone=SETASIDE zonePos=1 cardId=BG_REAL_MINION player=11] CardID=BG_REAL_MINION",
    )
    add_entity(parser, 201, "TB_BaconShop_DragBuy", controller=3, zone="PLAY", card_type="MINION")
    ui_events = feed(
        parser,
        "    tag=ATK value=1",
        "    tag=HEALTH value=1",
        "    tag=ZONE_POSITION value=2",
        "SHOW_ENTITY - Updating Entity=[entityName=Drag To Buy id=201 zone=PLAY zonePos=2 cardId=TB_BaconShop_DragBuy player=3] CardID=TB_BaconShop_DragBuy",
        "BLOCK_START BlockType=PLAY Entity=[entityName=Drag To Buy id=201 zone=PLAY zonePos=2 cardId=TB_BaconShop_DragBuy player=3] EffectCardId= EffectIndex=0 Target=0 SubOption=-1 TriggerKeyword=TAG_NOT_SET",
        "BLOCK_END",
        "TAG_CHANGE Entity=GameEntity tag=2022 value=1",
        "TAG_CHANGE Entity=GameEntity tag=2022 value=0",
        "TAG_CHANGE Entity=200 tag=ZONE value=REMOVEDFROMGAME",
    )

    battlegrounds = parser.snapshot().battlegrounds
    assert battlegrounds is not None
    assert [card.card_id for card in battlegrounds.warband] == ["BG_REAL_MINION"]
    assert not any(event.kind == "card_played" for event in ui_events)
    assert parser.snapshot().recent_cards == ()
    assert parser.entities[200].controller == 3
    assert parser.entities[200].zone == "REMOVEDFROMGAME"


@pytest.mark.parametrize(
    "card_id",
    ["TB_BaconShop_DragBuy", "TB_BaconShop_DragSell", "TB_BaconShop_DragFreeze"],
)
def test_battlegrounds_filters_card_id_only_ui_helpers(card_id: str) -> None:
    parser = PowerLogParser()
    feed(parser, "CREATE_GAME", "GameEntity EntityID=1", "TAG_CHANGE Entity=GameEntity tag=2022 value=1")
    add_entity(parser, 201, card_id, controller=3, zone="PLAY", card_type="MINION")

    events = feed(
        parser,
        f"BLOCK_START BlockType=PLAY Entity=[entityName= id=201 zone=PLAY zonePos=1 cardId={card_id} player=3] EffectCardId= EffectIndex=0 Target=0 SubOption=-1 TriggerKeyword=TAG_NOT_SET",
        "BLOCK_END",
    )

    assert events == []
    assert parser.snapshot().recent_cards == ()


def test_battlegrounds_combat_result_summarizes_local_damage() -> None:
    parser = PowerLogParser()
    feed(
        parser,
        "CREATE_GAME",
        "GameEntity EntityID=1",
        "Player EntityID=8 PlayerID=3 GameAccountId=[hi=0 lo=0]",
        "Player EntityID=9 PlayerID=11 GameAccountId=[hi=0 lo=0]",
        "    tag=BACON_DUMMY_PLAYER value=1",
        "TAG_CHANGE Entity=8 tag=NEXT_OPPONENT_PLAYER_ID value=5",
        "TAG_CHANGE Entity=GameEntity tag=TURN value=3",
    )
    add_entity(parser, 103, "TB_BaconShop_HERO_03", controller=3, zone="PLAY", card_type="HERO")
    feed(parser, "    tag=PLAYER_ID value=3", "    tag=HEALTH value=40", "    tag=DAMAGE value=0")
    feed(
        parser,
        "TAG_CHANGE Entity=GameEntity tag=2022 value=1",
        "TAG_CHANGE Entity=GameEntity tag=2022 value=0",
        "TAG_CHANGE Entity=103 tag=DAMAGE value=8",
    )

    events = feed(parser, "TAG_CHANGE Entity=GameEntity tag=2022 value=1")
    result = next(event for event in events if event.kind == "battlegrounds_combat_result")

    assert result.details["outcome"] == "lost"
    assert result.details["damage_taken"] == 8
    assert result.details["damage_dealt"] == 0
    assert any(event.kind == "battlegrounds_recruit_started" for event in events)


def test_battlegrounds_combat_result_counts_damage_absorbed_only_by_armor() -> None:
    parser = PowerLogParser()
    feed(
        parser,
        "CREATE_GAME",
        "GameEntity EntityID=1",
        "Player EntityID=8 PlayerID=3 GameAccountId=[hi=0 lo=0]",
        "Player EntityID=9 PlayerID=11 GameAccountId=[hi=0 lo=0]",
        "    tag=BACON_DUMMY_PLAYER value=1",
        "TAG_CHANGE Entity=8 tag=NEXT_OPPONENT_PLAYER_ID value=5",
        "TAG_CHANGE Entity=GameEntity tag=TURN value=3",
    )
    add_entity(parser, 103, "TB_BaconShop_HERO_03", controller=3, zone="PLAY", card_type="HERO")
    feed(
        parser,
        "    tag=PLAYER_ID value=3",
        "    tag=HEALTH value=40",
        "    tag=DAMAGE value=0",
        "    tag=ARMOR value=5",
    )
    feed(
        parser,
        "TAG_CHANGE Entity=GameEntity tag=2022 value=1",
        "TAG_CHANGE Entity=GameEntity tag=2022 value=0",
        "TAG_CHANGE Entity=103 tag=ARMOR value=2",
    )

    events = feed(parser, "TAG_CHANGE Entity=GameEntity tag=2022 value=1")
    result = next(event for event in events if event.kind == "battlegrounds_combat_result")

    assert result.details["outcome"] == "lost"
    assert result.details["damage_taken"] == 3


def test_confirmed_empty_recruit_warband_replaces_older_cache() -> None:
    parser = PowerLogParser()
    parser.mode = "battlegrounds"
    parser.phase = "recruit"
    parser.local_controller = 3
    parser._last_recruit_warband = (BattlegroundsCardSnapshot(card_id="OLD"),)

    parser._cache_recruit_warband()
    parser.phase = "combat"

    assert parser.snapshot().battlegrounds is not None
    assert parser.snapshot().battlegrounds.warband == ()


def test_entity_capacity_evicts_stale_entities_for_late_public_state() -> None:
    parser = PowerLogParser(max_entities=256)
    parser.entities = {
        entity_id: Entity(entity_id, zone="GRAVEYARD", card_type="ENCHANTMENT")
        for entity_id in range(1, 257)
    }

    late = parser._entity(999)

    assert late is not None
    assert late.entity_id == 999
    assert len(parser.entities) == 256
    assert parser.entities_evicted == 1
    assert parser.entity_capacity_exceeded is False


def test_legacy_battlegrounds_current_player_edges_drive_one_combat_cycle() -> None:
    parser = PowerLogParser()
    feed(
        parser,
        "CREATE_GAME",
        "GameEntity EntityID=1",
        "Player EntityID=8 PlayerID=3 GameAccountId=[hi=0 lo=0]",
        "Player EntityID=9 PlayerID=11 GameAccountId=[hi=0 lo=0]",
        "    tag=BACON_DUMMY_PLAYER value=1",
        "TAG_CHANGE Entity=GameEntity tag=TURN value=1",
        "TAG_CHANGE Entity=Local Player tag=CURRENT_PLAYER value=1",
        "TAG_CHANGE Entity=GameEntity tag=STATE value=RUNNING",
    )

    started = feed(parser, "TAG_CHANGE Entity=Bob's Tavern tag=CURRENT_PLAYER value=1")
    duplicate = feed(parser, "TAG_CHANGE Entity=Bob's Tavern tag=CURRENT_PLAYER value=1")
    feed(parser, "TAG_CHANGE Entity=GameEntity tag=TURN value=2")
    recruit = feed(parser, "TAG_CHANGE Entity=Local Player tag=CURRENT_PLAYER value=1")

    assert [event.kind for event in started] == ["battlegrounds_combat_started"]
    assert not any(event.kind == "battlegrounds_combat_started" for event in duplicate)
    assert [event.kind for event in recruit] == [
        "battlegrounds_combat_result",
        "battlegrounds_recruit_started",
    ]


def test_legacy_battlegrounds_unresolved_mulligan_done_starts_first_recruit() -> None:
    parser = PowerLogParser()
    feed(
        parser,
        "CREATE_GAME",
        "GameEntity EntityID=1",
        "Player EntityID=8 PlayerID=3 GameAccountId=[hi=0 lo=0]",
        "Player EntityID=9 PlayerID=11 GameAccountId=[hi=0 lo=0]",
        "    tag=BACON_DUMMY_PLAYER value=1",
    )

    events = feed(parser, "TAG_CHANGE Entity=Local Player tag=MULLIGAN_STATE value=DONE")

    assert parser.snapshot().phase == "recruit"
    assert [event.kind for event in events] == ["battlegrounds_hero_selected"]
