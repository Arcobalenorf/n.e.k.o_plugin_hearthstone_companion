from __future__ import annotations

import json

import pytest
from hearthstone_companion_under_test.commentary import build_llm_prompt
from hearthstone_companion_under_test.models import BattlegroundsCardSnapshot, Entity, GameEvent
from hearthstone_companion_under_test.powerlog import PowerLogParser

PREFIX = "D 12:00:00.0000000 GameState.DebugPrintPower() - "


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

    turn_events = feed(parser, "TAG_CHANGE Entity=GameEntity tag=TURN value=3")
    result_events = feed(parser, "TAG_CHANGE Entity=3 tag=PLAYSTATE value=WON")
    snapshot = parser.snapshot()

    assert [(event.kind, event.details) for event in turn_events] == [("turn_started", {"turn": 3})]
    assert len(result_events) == 1
    assert result_events[0].kind == "game_ended"
    assert result_events[0].details["result"] == "won"
    assert snapshot.turn == 3
    assert snapshot.phase == "ended"
    assert snapshot.result == "won"


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


def test_battlegrounds_rehidden_known_card_never_exposes_card_id_or_stats() -> None:
    parser = PowerLogParser()
    feed(
        parser,
        "CREATE_GAME",
        "Player EntityID=8 PlayerID=3 GameAccountId=[hi=0 lo=0]",
        "Player EntityID=9 PlayerID=11 GameAccountId=[hi=0 lo=0]",
        "    tag=BACON_DUMMY_PLAYER value=1",
        "TAG_CHANGE Entity=8 tag=CURRENT_PLAYER value=1",
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
        "TAG_CHANGE Entity=8 tag=CURRENT_PLAYER value=1",
    )
    add_entity(parser, 420, "BG_SPELL", controller=11, zone="PLAY", card_type="42")

    battlegrounds = parser.snapshot().battlegrounds

    assert battlegrounds is not None
    assert [card.card_id for card in battlegrounds.shop] == ["BG_SPELL"]


def test_mirrored_power_task_list_stream_does_not_start_the_same_game_twice() -> None:
    parser = PowerLogParser()

    first = parser.feed_line(source_line("GameState", "CREATE_GAME"), now=100.0)
    mirrored = parser.feed_line(source_line("PowerTaskList", "CREATE_GAME"), now=101.0)

    assert [event.kind for event in first] == ["game_started"]
    assert mirrored == []
    assert parser.snapshot().game_number == 1


def test_midgame_bootstrap_without_create_game_locks_first_power_source() -> None:
    parser = PowerLogParser()

    parser.feed_line(source_line("GameState", "GameEntity EntityID=1"), now=100.0)
    parser.feed_line(
        source_line("PowerTaskList", "TAG_CHANGE Entity=GameEntity tag=TURN value=99"),
        now=101.0,
    )
    parser.feed_line(
        source_line("GameState", "TAG_CHANGE Entity=GameEntity tag=TURN value=7"),
        now=102.0,
    )

    assert parser._power_source == "GameState"
    assert parser.snapshot().turn == 7


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

    ended = feed(parser, "TAG_CHANGE Entity=GameEntity tag=STATE value=COMPLETE")
    duplicate = feed(parser, "TAG_CHANGE Entity=GameEntity tag=STATE value=COMPLETE")

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
