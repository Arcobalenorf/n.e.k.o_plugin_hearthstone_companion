from __future__ import annotations

import json

import pytest
from hearthstone_companion_under_test.commentary import (
    CommentaryArbiter,
    build_emotion_cue,
    build_llm_prompt,
)
from hearthstone_companion_under_test.config import CompanionConfig
from hearthstone_companion_under_test.models import (
    BattlegroundsCardSnapshot,
    BattlegroundsHeroChoiceSnapshot,
    BattlegroundsPlayerSnapshot,
    BattlegroundsSnapshot,
    ConstructedCardSnapshot,
    ConstructedSideSnapshot,
    ConstructedSnapshot,
    GameEvent,
    GameSnapshot,
    SideSnapshot,
)


def event(*, priority: int = 5, suffix: str = "") -> GameEvent:
    return GameEvent("hero_damaged", priority, f"受到伤害{suffix}", 100.0, {"amount": 3, "side": "player"})


def config(**overrides: object) -> CompanionConfig:
    values = CompanionConfig().to_dict()
    values.update(overrides)
    return CompanionConfig.from_mapping(values)


def test_llm_requires_both_feature_enablement_and_explicit_consent() -> None:
    snapshot = GameSnapshot(phase="playing")

    assert CommentaryArbiter(config()).allow_llm(event(), snapshot, now=100.0) is False
    assert CommentaryArbiter(config(llm_commentary_enabled=True)).allow_llm(
        event(), snapshot, now=100.0
    ) is False
    assert CommentaryArbiter(config(llm_data_consent=True)).allow_llm(
        event(), snapshot, now=100.0
    ) is False
    assert CommentaryArbiter(
        config(llm_commentary_enabled=True, llm_data_consent=True)
    ).allow_llm(event(), snapshot, now=100.0) is True


def test_llm_rate_limits_normal_and_critical_events() -> None:
    arbiter = CommentaryArbiter(
        config(
            llm_commentary_enabled=True,
            llm_data_consent=True,
            llm_cooldown_seconds=25.0,
            llm_critical_cooldown_seconds=8.0,
        )
    )
    snapshot = GameSnapshot(phase="playing")

    first = event(suffix="a")
    assert arbiter.allow_llm(first, snapshot, now=100.0) is True
    arbiter.mark_llm_submitted(first, snapshot, now=100.0)
    assert arbiter.allow_llm(event(suffix="b"), snapshot, now=124.9) is False
    assert arbiter.allow_llm(event(suffix="c"), snapshot, now=125.0) is True
    arbiter.mark_llm_submitted(event(suffix="c"), snapshot, now=125.0)
    assert arbiter.allow_llm(event(priority=9, suffix="d"), snapshot, now=132.9) is False
    assert arbiter.allow_llm(event(priority=9, suffix="e"), snapshot, now=133.0) is True


def test_llm_rejects_low_priority_and_spectator_events() -> None:
    arbiter = CommentaryArbiter(
        config(llm_commentary_enabled=True, llm_data_consent=True, llm_min_priority=5)
    )

    assert arbiter.allow_llm(event(priority=4), GameSnapshot(phase="playing"), now=100.0) is False
    assert arbiter.allow_llm(event(priority=10), GameSnapshot(phase="spectator"), now=100.0) is False


def test_emotion_cue_uses_public_low_health_as_tension_signal() -> None:
    snapshot = GameSnapshot(phase="playing", player=SideSnapshot(health=8, armor=1))

    assert build_emotion_cue(event(), snapshot) == {
        "tone": "tense_support",
        "arousal": 8,
        "reason": "low_health",
    }


def test_llm_prompt_delegates_visible_wording_to_current_neko_character() -> None:
    prompt = build_llm_prompt(event(), GameSnapshot(phase="playing"))

    assert "保持当前 N.E.K.O 角色的人设" in prompt
    assert '"emotion_cue"' in prompt
    assert "公开局势 JSON" in prompt
    assert "这一击真疼" not in prompt


def test_proactive_constructed_prompt_omits_specific_hand_identity() -> None:
    snapshot = GameSnapshot(
        mode="constructed",
        phase="playing",
        game_number=2,
        turn=5,
        round=3,
        constructed=ConstructedSnapshot(
            game_type="GT_RANKED",
            variant="ranked",
            player=ConstructedSideSnapshot(
                mana_available=4,
                mana_max=5,
                hand_count=1,
                known_hand=(
                    ConstructedCardSnapshot(
                        card_id="PRIVATE_VISIBLE_CARD",
                        name="仅按需提供的手牌",
                        card_type="SPELL",
                        cost=4,
                    ),
                ),
                hand_identities_complete=True,
            ),
        ),
    )

    prompt = build_llm_prompt(event(), snapshot, max_prompt_chars=10_000)

    assert "PRIVATE_VISIBLE_CARD" not in prompt
    assert "仅按需提供的手牌" not in prompt
    assert '\"count\":1' in prompt
    assert '\"turn\":5' in prompt
    assert '\"round\":3' in prompt


def test_full_constructed_board_still_fits_proactive_prompt_budget() -> None:
    oversized = "公开但超长的随从名" * 30
    cards = tuple(
        ConstructedCardSnapshot(
            card_id=f"PUBLIC_BOARD_{index}_{oversized}",
            name=oversized,
            card_type="MINION",
            attack=99,
            health=99,
            max_health=99,
            keywords=("taunt", "divine_shield", "lifesteal"),
        )
        for index in range(7)
    )
    side = ConstructedSideSnapshot(
        mana_available=10,
        mana_max=10,
        hand_count=10,
        deck_count=30,
        secret_count=5,
        board=cards,
        weapon=cards[0],
        hero_power=cards[1],
        locations=(cards[2], cards[3]),
    )
    snapshot = GameSnapshot(
        mode="constructed",
        phase="playing",
        turn=19,
        round=10,
        active_side="player",
        constructed=ConstructedSnapshot(
            game_type="GT_RANKED_STANDARD",
            format="standard",
            variant="ranked",
            player=side,
            opponent=side,
        ),
    )

    prompt = build_llm_prompt(event(), snapshot, max_prompt_chars=1800)
    encoded = prompt.split("公开局势 JSON：", 1)[1]

    assert len(prompt) <= 1800
    assert isinstance(json.loads(encoded), dict)


def test_battlegrounds_prompt_includes_hero_choices_and_observed_opponent_board() -> None:
    snapshot = GameSnapshot(
        mode="battlegrounds",
        phase="hero_select",
        game_number=1,
        battlegrounds=BattlegroundsSnapshot(
            phase="hero_select",
            hero_choices=(
                BattlegroundsHeroChoiceSnapshot(card_id="BG_HERO_A", name="候选英雄"),
            ),
            lobby=(
                BattlegroundsPlayerSnapshot(player_id=1, is_local=True),
                BattlegroundsPlayerSnapshot(
                    player_id=2,
                    next_opponent=True,
                    last_seen_round=2,
                    board_count=1,
                    board_cards=("见过的随从",),
                    board_minions=(
                        BattlegroundsCardSnapshot(
                            card_id="BG_MINION_A",
                            name="见过的随从",
                            attack=4,
                            health=5,
                            tier=2,
                        ),
                    ),
                ),
            ),
        ),
    )

    prompt = build_llm_prompt(
        GameEvent("battlegrounds_detected", 7, "进入酒馆", 100.0, {}),
        snapshot,
    )

    assert "BG_HERO_A" in prompt
    assert "BG_MINION_A" in prompt
    assert "last_seen_round" in prompt


def test_rejected_delivery_does_not_burn_cooldown_or_semantic_key() -> None:
    arbiter = CommentaryArbiter(
        config(llm_commentary_enabled=True, llm_data_consent=True, llm_cooldown_seconds=25.0)
    )
    snapshot = GameSnapshot(phase="playing", game_number=3)
    candidate = event(suffix="retry")

    assert arbiter.allow_llm(candidate, snapshot, now=100.0) is True
    assert arbiter.allow_llm(candidate, snapshot, now=101.0) is True
    arbiter.mark_llm_submitted(candidate, snapshot, now=101.0)
    assert arbiter.allow_llm(candidate, snapshot, now=200.0) is False
    assert arbiter.allow_llm(candidate, snapshot, now=221.0) is True


def test_semantic_dedupe_is_scoped_to_game_number() -> None:
    arbiter = CommentaryArbiter(config(llm_commentary_enabled=True, llm_data_consent=True))
    candidate = event(priority=9, suffix="same")
    first_game = GameSnapshot(phase="playing", game_number=4)
    next_game = GameSnapshot(phase="playing", game_number=5)

    arbiter.mark_llm_submitted(candidate, first_game, now=100.0)

    assert arbiter.allow_llm(candidate, first_game, now=130.0) is False
    assert arbiter.allow_llm(candidate, next_game, now=130.0) is True


def test_confirmed_terminal_event_bypasses_prior_nonterminal_cooldown() -> None:
    arbiter = CommentaryArbiter(config(llm_commentary_enabled=True, llm_data_consent=True))
    snapshot = GameSnapshot(phase="playing", game_number=6)
    arbiter.mark_llm_submitted(event(priority=8, suffix="damage"), snapshot, now=100.0)
    terminal = GameEvent(
        "battlegrounds_game_ended", 10, "placement confirmed", 101.0, {"placement": 1}
    )

    assert arbiter.allow_llm(terminal, snapshot, now=101.0) is True


def test_duos_third_place_uses_comfort_not_top_finish_pride() -> None:
    cue = build_emotion_cue(
        GameEvent(
            "battlegrounds_game_ended",
            10,
            "duos third",
            100.0,
            {"placement": 3, "variant": "duos"},
        ),
        GameSnapshot(mode="battlegrounds", phase="ended"),
    )

    assert cue == {"tone": "gentle_comfort", "arousal": 3, "reason": "loss_or_low_finish"}


def test_terminal_prompt_closes_game_context_after_the_last_character_line() -> None:
    prompt = build_llm_prompt(
        GameEvent(
            "battlegrounds_game_ended",
            10,
            "first",
            100.0,
            {"placement": 1, "variant": "solo"},
        ),
        GameSnapshot(mode="battlegrounds", phase="ended", result="won"),
    )

    assert "这是本局最后一句" in prompt
    assert "后续普通对话恢复日常语境" in prompt


def test_llm_prompt_is_valid_json_and_never_exceeds_hard_limit() -> None:
    oversized = "超长不可信卡名" * 100
    cards = tuple(
        BattlegroundsCardSnapshot(card_id=oversized, name=oversized, attack=999, health=999)
        for _ in range(10)
    )
    lobby = tuple(
        BattlegroundsPlayerSnapshot(
            player_id=index,
            is_local=index == 1,
            hero_card_id=oversized,
            hero_name=oversized,
            board_cards=(oversized,) * 7,
        )
        for index in range(1, 9)
    )
    snapshot = GameSnapshot(
        mode="battlegrounds",
        phase="recruit",
        battlegrounds=BattlegroundsSnapshot(
            round=15,
            phase="recruit",
            shop=cards,
            hand=cards,
            warband=cards,
            lobby=lobby,
            mechanics={oversized: oversized},
        ),
    )

    prompt = build_llm_prompt(
        GameEvent("battlegrounds_recruit_started", 7, oversized, 100.0, {oversized: oversized}),
        snapshot,
        max_prompt_chars=1800,
    )
    encoded = prompt.split("公开局势 JSON：", 1)[1]

    assert len(prompt) <= 1800
    assert isinstance(json.loads(encoded), dict)


def test_llm_prompt_rejects_impossible_limit_instead_of_truncating_json() -> None:
    with pytest.raises(ValueError, match="too small"):
        build_llm_prompt(event(), GameSnapshot(), max_prompt_chars=100)
