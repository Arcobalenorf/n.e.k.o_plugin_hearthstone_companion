from __future__ import annotations

import json

import pytest
from hearthstone_companion_under_test.commentary import (
    CommentaryArbiter,
    build_atomic_live_state_segment,
    build_emotion_cue,
    build_live_state_context,
    build_live_state_contexts,
    build_llm_prompt,
)
from hearthstone_companion_under_test.config import CompanionConfig
from hearthstone_companion_under_test.models import (
    BattlegroundsAreaSnapshot,
    BattlegroundsCardSnapshot,
    BattlegroundsChoiceSnapshot,
    BattlegroundsEconomySnapshot,
    BattlegroundsHeroChoiceSnapshot,
    BattlegroundsPlayerSnapshot,
    BattlegroundsSnapshot,
    ChoiceSnapshot,
    ConstructedCardSnapshot,
    ConstructedSideSnapshot,
    ConstructedSnapshot,
    GameEvent,
    GameSnapshot,
    SideSnapshot,
)

_ATOMIC_JSON_MARKER = "过滤后的实时局势 JSON："


def _atomic_payload(prompt: str) -> dict[str, object]:
    payload = json.loads(prompt.split(_ATOMIC_JSON_MARKER, 1)[1])
    assert isinstance(payload, dict)
    assert list(payload)[-1] == "answer_checklist"
    return payload


def event(*, priority: int = 5, suffix: str = "") -> GameEvent:
    return GameEvent("hero_damaged", priority, f"受到伤害{suffix}", 100.0, {"amount": 3, "side": "player"})


def test_atomic_live_state_is_one_bounded_replaceable_context() -> None:
    snapshot = GameSnapshot(
        mode="constructed",
        phase="playing",
        game_number=4,
        turn=21,
        round=11,
        active_side="player",
        constructed=ConstructedSnapshot(
            player=ConstructedSideSnapshot(board_identities_complete=True),
            opponent=ConstructedSideSnapshot(board_identities_complete=True),
        ),
    )

    segments = build_atomic_live_state_segment(
        snapshot,
        observed_at=1235.0,
        max_prompt_bytes=900,
    )

    assert len(segments) == 1
    assert segments[0][0] == "core"
    assert len(segments[0][1].encode("utf-8")) <= 900
    assert '"round":11' in segments[0][1]
    assert '"turn":21' in segments[0][1]
    assert "证据完整的字段可直接回答" in segments[0][1]
    assert "不得仅因本轮未调用工具而拒答" in segments[0][1]
    assert "禁止用旧对话或公共目录补猜当前事实" in segments[0][1]
    assert "answer_checklist 是唯一回答清单" in segments[0][1]
    assert "delivery=full 时须覆盖全部 group 与 slot" in segments[0][1]
    assert "hearthstone_current_turn" in segments[0][1]
    assert "hearthstone_live_state" in segments[0][1]


def test_atomic_constructed_state_leads_with_direct_round_and_opponent_facts() -> None:
    snapshot = GameSnapshot(
        mode="constructed",
        phase="playing",
        game_number=4,
        turn=21,
        round=11,
        active_side="player",
        opponent=SideSnapshot(board_count=1),
        constructed=ConstructedSnapshot(
            player=ConstructedSideSnapshot(board_identities_complete=True),
            opponent=ConstructedSideSnapshot(
                board=(
                    ConstructedCardSnapshot(
                        card_id="OPPONENT_PUBLIC_1",
                        name="公开对手随从",
                        card_type="MINION",
                        zone_position=1,
                        attack=6,
                        health=7,
                        keywords=("taunt", "divine_shield"),
                        keywords_complete=True,
                    ),
                ),
                board_identities_complete=True,
            ),
        ),
    )

    prompt = build_atomic_live_state_segment(
        snapshot,
        observed_at=1235.0,
        max_prompt_bytes=4096,
    )[0][1]

    payload = _atomic_payload(prompt)
    checklist = payload["answer_checklist"]
    assert isinstance(checklist, dict)
    assert checklist["current"] == {
        "round": 11,
        "action_turn": 21,
        "action_turn_is_not_round": True,
        "active_side": "player",
        "phase": "playing",
    }
    opponent = checklist["areas"]["opponent_board"]
    assert opponent["delivery"] == "full"
    assert opponent["slot_count"] == 1
    assert opponent["group_count"] == 1
    assert opponent["completion_check"] == {"groups": "1/1", "slots": "1/1"}
    assert opponent["groups"] == [
        {
            "ordinal": "1/1",
            "positions": [1],
            "count": 1,
            "card_id": "OPPONENT_PUBLIC_1",
            "name": "公开对手随从",
            "card_type": "MINION",
            "card_type_zh": "随从",
            "current_cost": None,
            "attack": 6,
            "health": 7,
            "tier": None,
            "premium": None,
            "keywords_complete": True,
            "active_keywords": ["嘲讽", "圣盾"],
        }
    ]


def test_atomic_constructed_state_does_not_claim_unknown_keyword_baseline() -> None:
    snapshot = GameSnapshot(
        mode="constructed",
        phase="playing",
        game_number=4,
        turn=21,
        round=11,
        constructed=ConstructedSnapshot(
            opponent=ConstructedSideSnapshot(
                board=(
                    ConstructedCardSnapshot(
                        card_id="OPPONENT_PARTIAL",
                        name="观测未完成的随从",
                        card_type="MINION",
                        zone_position=1,
                        attack=2,
                        health=3,
                    ),
                ),
                board_identities_complete=True,
            ),
        ),
    )

    prompt = build_atomic_live_state_segment(
        snapshot,
        observed_at=1235.0,
        max_prompt_bytes=4096,
    )[0][1]

    group = _atomic_payload(prompt)["answer_checklist"]["areas"]["opponent_board"][
        "groups"
    ][0]
    assert group["keywords_complete"] is False
    assert group["active_keywords"] == []


def test_atomic_battlegrounds_state_leads_with_direct_shop_and_economy_facts() -> None:
    observed = BattlegroundsAreaSnapshot(
        complete=True,
        revision=1,
        observed_at=1234.0,
        round=3,
        phase="recruit",
    )
    snapshot = GameSnapshot(
        mode="battlegrounds",
        phase="recruit",
        game_number=5,
        round=3,
        battlegrounds=BattlegroundsSnapshot(
            round=3,
            phase="recruit",
            gold=5,
            max_gold=5,
            refresh_cost=1,
            upgrade_cost=3,
            shop=(
                BattlegroundsCardSnapshot(
                    card_id="BG_SPELL_DIRECT",
                    name="测试酒馆法术",
                    card_type="BATTLEGROUND_SPELL",
                    position=1,
                    current_cost=1,
                    premium=False,
                    keywords={"divine_shield": False},
                ),
                BattlegroundsCardSnapshot(
                    card_id="BG_GOLDEN_DIRECT",
                    name="金色圣盾随从",
                    card_type="MINION",
                    attack=8,
                    health=9,
                    tier=3,
                    position=2,
                    current_cost=3,
                    premium=True,
                    keywords={"divine_shield": True},
                ),
            ),
            warband=(),
            economy=BattlegroundsEconomySnapshot(
                upgrade_cost=3,
                refresh_cost=1,
                revision=1,
                observed_at=1234.0,
                gold_observation=observed,
                refresh_observation=observed,
                upgrade_observation=observed,
            ),
            areas={
                "shop": observed,
                "warband": observed,
                "economy": observed,
            },
        ),
    )

    prompt = build_atomic_live_state_segment(
        snapshot,
        observed_at=1235.0,
        max_prompt_bytes=4096,
    )[0][1]

    payload = _atomic_payload(prompt)
    checklist = payload["answer_checklist"]
    assert isinstance(checklist, dict)
    assert checklist["current"] == {"round": 3, "phase": "recruit"}
    assert checklist["economy"] == {
        "source_complete": True,
        "gold": 5,
        "refresh_actual_cost": 1,
        "upgrade_actual_cost": 3,
        "can_upgrade": True,
        "remaining_after_upgrade": 2,
        "remaining_status": "applicable",
    }
    shop = checklist["areas"]["shop"]
    assert shop["delivery"] == "full"
    assert shop["slot_count"] == 2
    assert shop["group_count"] == 2
    assert shop["completion_check"] == {"groups": "2/2", "slots": "2/2"}
    assert [group["ordinal"] for group in shop["groups"]] == ["1/2", "2/2"]
    assert shop["groups"][0]["card_type_zh"] == "酒馆法术"
    assert shop["groups"][0]["current_cost"] == 1
    assert shop["groups"][1]["premium"] is True
    assert shop["groups"][1]["active_keywords"] == ["圣盾"]

    blocked_snapshot = GameSnapshot(
        mode="battlegrounds",
        phase="recruit",
        game_number=6,
        round=3,
        battlegrounds=BattlegroundsSnapshot(
            round=3,
            phase="recruit",
            gold=5,
            refresh_cost=1,
            upgrade_cost=6,
            economy=BattlegroundsEconomySnapshot(
                upgrade_cost=6,
                refresh_cost=1,
                revision=1,
                observed_at=1234.0,
                gold_observation=observed,
                refresh_observation=observed,
                upgrade_observation=observed,
            ),
            areas={"economy": observed},
        ),
    )
    blocked_prompt = build_atomic_live_state_segment(
        blocked_snapshot,
        observed_at=1235.0,
        max_prompt_bytes=4096,
    )[0][1]
    blocked = _atomic_payload(blocked_prompt)["answer_checklist"]
    assert isinstance(blocked, dict)
    assert blocked["economy"]["can_upgrade"] is False
    assert blocked["economy"]["remaining_after_upgrade"] is None
    assert blocked["economy"]["remaining_status"] == (
        "not_applicable_insufficient_gold"
    )


def test_atomic_battlegrounds_rejects_stale_and_invalid_observations() -> None:
    def snapshot(observed_at: float) -> GameSnapshot:
        observed = BattlegroundsAreaSnapshot(
            complete=True,
            revision=1,
            observed_at=observed_at,
            round=3,
            phase="recruit",
        )
        return GameSnapshot(
            mode="battlegrounds",
            phase="recruit",
            round=3,
            battlegrounds=BattlegroundsSnapshot(
                round=3,
                phase="recruit",
                gold=5,
                refresh_cost=1,
                upgrade_cost=3,
                shop=(BattlegroundsCardSnapshot(card_id="BG_FRESHNESS_CARD", position=1),),
                economy=BattlegroundsEconomySnapshot(
                    refresh_cost=1,
                    upgrade_cost=3,
                    revision=1,
                    observed_at=observed_at,
                    gold_observation=observed,
                    refresh_observation=observed,
                    upgrade_observation=observed,
                ),
                areas={"shop": observed, "economy": observed},
            ),
        )

    boundary = _atomic_payload(
        build_atomic_live_state_segment(
            snapshot(100.0),
            observed_at=400.0,
            max_prompt_bytes=4096,
        )[0][1]
    )["answer_checklist"]
    stale = _atomic_payload(
        build_atomic_live_state_segment(
            snapshot(100.0),
            observed_at=400.001,
            max_prompt_bytes=4096,
        )[0][1]
    )["answer_checklist"]
    invalid = _atomic_payload(
        build_atomic_live_state_segment(
            snapshot(float("nan")),
            observed_at=400.0,
            max_prompt_bytes=4096,
        )[0][1]
    )["answer_checklist"]

    assert boundary["areas"]["shop"]["delivery"] == "full"
    assert boundary["economy"]["source_complete"] is True
    for checklist in (stale, invalid):
        assert checklist["areas"]["shop"]["delivery"] == "missing_evidence"
        assert checklist["areas"]["shop"]["groups"] == []
        assert checklist["economy"]["source_complete"] is False
        assert checklist["economy"]["gold"] is None
        assert checklist["economy"]["refresh_actual_cost"] is None
        assert checklist["economy"]["upgrade_actual_cost"] is None


def test_atomic_shop_uses_complete_dynamic_groups_without_duplicate_card_rows() -> None:
    observed = BattlegroundsAreaSnapshot(
        complete=True,
        revision=8,
        observed_at=1234.0,
        round=6,
        phase="recruit",
    )
    repeated = dict(
        card_id="BG_REPEAT",
        name="重复随从",
        card_type="MINION",
        attack=4,
        health=5,
        tier=2,
        current_cost=3,
        premium=False,
        keywords={"taunt": True, "divine_shield": False},
    )
    snapshot = GameSnapshot(
        mode="battlegrounds",
        phase="recruit",
        game_number=9,
        round=6,
        battlegrounds=BattlegroundsSnapshot(
            round=6,
            phase="recruit",
            shop=(
                BattlegroundsCardSnapshot(position=1, **repeated),
                BattlegroundsCardSnapshot(position=2, **{**repeated, "current_cost": 2}),
                BattlegroundsCardSnapshot(position=3, **repeated),
                BattlegroundsCardSnapshot(
                    card_id="BG_SPELL_OTHER",
                    name="另一法术",
                    card_type="BATTLEGROUND_SPELL",
                    position=4,
                    current_cost=1,
                    premium=False,
                    keywords={"taunt": False},
                ),
            ),
            hand=(
                BattlegroundsCardSnapshot(
                    card_id="BG_HAND_VISIBLE",
                    position=1,
                    card_type="MINION",
                    current_cost=3,
                    premium=False,
                    keywords={"reborn": True},
                ),
            ),
            areas={"shop": observed, "hand": observed},
        ),
    )

    prompt = build_atomic_live_state_segment(
        snapshot,
        observed_at=1235.0,
        max_prompt_bytes=4096,
    )[0][1]
    payload = _atomic_payload(prompt)
    checklist = payload["answer_checklist"]
    assert isinstance(checklist, dict)
    shop = checklist["areas"]["shop"]

    assert shop["delivery"] == "full"
    assert shop["slot_count"] == 4
    assert shop["group_count"] == 3
    assert shop["completion_check"] == {"groups": "3/3", "slots": "4/4"}
    assert [group["ordinal"] for group in shop["groups"]] == ["1/3", "2/3", "3/3"]
    assert shop["groups"][0]["card_id"] == "BG_REPEAT"
    assert shop["groups"][0]["positions"] == [1, 3]
    assert shop["groups"][0]["count"] == 2
    assert shop["groups"][1]["card_id"] == "BG_REPEAT"
    assert shop["groups"][1]["positions"] == [2]
    assert shop["groups"][1]["current_cost"] == 2
    assert shop["groups"][2]["card_id"] == "BG_SPELL_OTHER"
    assert prompt.count('"card_id":"BG_REPEAT"') == 2
    assert checklist["areas"]["hand"]["delivery"] == "full"
    assert checklist["areas"]["hand"]["groups"][0]["card_id"] == "BG_HAND_VISIBLE"


def test_atomic_tight_budget_marks_card_details_tool_required_without_partial_groups() -> None:
    observed = BattlegroundsAreaSnapshot(
        complete=True,
        revision=1,
        observed_at=1234.0,
        round=2,
        phase="recruit",
    )
    snapshot = GameSnapshot(
        mode="battlegrounds",
        phase="recruit",
        round=2,
        battlegrounds=BattlegroundsSnapshot(
            round=2,
            phase="recruit",
            shop=tuple(
                BattlegroundsCardSnapshot(
                    card_id=f"BG_LONG_CARD_{index}",
                    name="预算测试长名称",
                    card_type="MINION",
                    position=index,
                    current_cost=3,
                    keywords={"taunt": index % 2 == 0},
                )
                for index in range(1, 8)
            ),
            areas={"shop": observed},
        ),
    )

    prompt = build_atomic_live_state_segment(
        snapshot,
        observed_at=1235.0,
        max_prompt_bytes=900,
    )[0][1]
    payload = _atomic_payload(prompt)
    checklist = payload["answer_checklist"]
    assert isinstance(checklist, dict)
    assert len(prompt.encode("utf-8")) <= 900
    assert checklist.get("details") == "tool_required:hearthstone_live_state"
    assert "BG_LONG_CARD_1" not in prompt


def test_llm_prompt_omits_incomplete_battlegrounds_regions_and_economy() -> None:
    hidden_id = "BG_INCOMPLETE_SHOP_PREFIX"
    snapshot = GameSnapshot(
        mode="battlegrounds",
        phase="recruit",
        game_number=3,
        battlegrounds=BattlegroundsSnapshot(
            round=4,
            phase="recruit",
            gold=5,
            refresh_cost=1,
            upgrade_cost=4,
            frozen=True,
            shop=(BattlegroundsCardSnapshot(card_id=hidden_id, position=1),),
            areas={
                "shop": BattlegroundsAreaSnapshot(
                    complete=False,
                    revision=9,
                    observed_at=100.0,
                    round=4,
                    phase="recruit",
                ),
            },
            economy=BattlegroundsEconomySnapshot(
                refresh_cost=1,
                upgrade_cost=4,
            ),
        ),
    )

    prompt = build_llm_prompt(
        GameEvent(
            "battlegrounds_recruit_started",
            7,
            "招募开始",
            100.0,
            {"round": 4},
        ),
        snapshot,
        max_prompt_chars=10_000,
    )

    assert hidden_id not in prompt
    assert '"gold":null' in prompt
    assert '"refresh_cost":null' in prompt
    assert '"upgrade_cost":null' in prompt


def test_llm_prompt_redacts_complete_but_stale_battlegrounds_facts() -> None:
    observed = BattlegroundsAreaSnapshot(
        complete=True,
        revision=1,
        observed_at=100.0,
        round=4,
        phase="recruit",
    )
    snapshot = GameSnapshot(
        mode="battlegrounds",
        phase="recruit",
        round=4,
        battlegrounds=BattlegroundsSnapshot(
            round=4,
            phase="recruit",
            gold=5,
            refresh_cost=1,
            upgrade_cost=4,
            shop=(BattlegroundsCardSnapshot(card_id="BG_STALE_ACTIVE_CARD", position=1),),
            areas={"shop": observed, "economy": observed},
            economy=BattlegroundsEconomySnapshot(
                refresh_cost=1,
                upgrade_cost=4,
                revision=1,
                observed_at=100.0,
                gold_observation=observed,
                refresh_observation=observed,
                upgrade_observation=observed,
            ),
        ),
    )

    def prompt_at(timestamp: float) -> str:
        return build_llm_prompt(
            GameEvent(
                "battlegrounds_recruit_started",
                7,
                "招募开始",
                timestamp,
                {"round": 4},
            ),
            snapshot,
            max_prompt_chars=10_000,
        )

    boundary = prompt_at(400.0)
    stale = prompt_at(400.001)

    assert "BG_STALE_ACTIVE_CARD" in boundary
    assert "BG_STALE_ACTIVE_CARD" not in stale
    assert '"gold":null' in stale
    assert '"refresh_cost":null' in stale
    assert '"upgrade_cost":null' in stale


def config(**overrides: object) -> CompanionConfig:
    values = CompanionConfig().to_dict()
    values.update(overrides)
    return CompanionConfig.from_mapping(values)


def test_llm_defaults_to_commentary_but_respects_explicit_opt_out() -> None:
    snapshot = GameSnapshot(phase="playing")

    assert CommentaryArbiter(config()).allow_llm(event(), snapshot, now=100.0) is True
    assert CommentaryArbiter(config(llm_do_not_disturb=True)).allow_llm(
        event(), snapshot, now=100.0
    ) is False
    assert CommentaryArbiter(config(llm_data_consent=True)).allow_llm(
        event(), snapshot, now=100.0
    ) is True
    assert CommentaryArbiter(
        config(llm_do_not_disturb=False, llm_data_consent=False)
    ).allow_llm(event(), snapshot, now=100.0) is False


def test_llm_rate_limits_normal_and_critical_events() -> None:
    arbiter = CommentaryArbiter(
        config(
            llm_do_not_disturb=False,
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
        config(llm_do_not_disturb=False, llm_data_consent=True, llm_min_priority=5)
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
                board_identities_complete=True,
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
        config(llm_do_not_disturb=False, llm_data_consent=True, llm_cooldown_seconds=25.0)
    )
    snapshot = GameSnapshot(phase="playing", game_number=3)
    candidate = event(suffix="retry")

    assert arbiter.allow_llm(candidate, snapshot, now=100.0) is True
    assert arbiter.allow_llm(candidate, snapshot, now=101.0) is True
    arbiter.mark_llm_submitted(candidate, snapshot, now=101.0)
    assert arbiter.allow_llm(candidate, snapshot, now=200.0) is False
    assert arbiter.allow_llm(candidate, snapshot, now=221.0) is True


def test_semantic_dedupe_is_scoped_to_game_number() -> None:
    arbiter = CommentaryArbiter(config(llm_do_not_disturb=False, llm_data_consent=True))
    candidate = event(priority=9, suffix="same")
    first_game = GameSnapshot(phase="playing", game_number=4)
    next_game = GameSnapshot(phase="playing", game_number=5)

    arbiter.mark_llm_submitted(candidate, first_game, now=100.0)

    assert arbiter.allow_llm(candidate, first_game, now=130.0) is False
    assert arbiter.allow_llm(candidate, next_game, now=130.0) is True


def test_source_reset_clears_commentary_cooldown_and_semantic_history() -> None:
    arbiter = CommentaryArbiter(
        config(
            llm_do_not_disturb=False,
            llm_data_consent=True,
            llm_cooldown_seconds=25.0,
        )
    )
    candidate = event(priority=9, suffix="same")
    snapshot = GameSnapshot(phase="playing", game_number=4)
    arbiter.mark_llm_submitted(candidate, snapshot, now=100.0)

    assert arbiter.allow_llm(candidate, snapshot, now=101.0) is False
    arbiter.reset()

    assert arbiter.allow_llm(candidate, snapshot, now=101.0) is True


def test_terminal_event_never_falls_back_to_midgame_commentary() -> None:
    arbiter = CommentaryArbiter(
        config(
            llm_do_not_disturb=False,
            llm_data_consent=True,
        )
    )
    snapshot = GameSnapshot(phase="playing", game_number=6)
    arbiter.mark_llm_submitted(event(priority=8, suffix="damage"), snapshot, now=100.0)
    terminal = GameEvent(
        "battlegrounds_game_ended", 10, "placement confirmed", 101.0, {"placement": 1}
    )

    assert arbiter.allow_llm(terminal, snapshot, now=101.0) is False


def test_lifecycle_owned_events_do_not_enter_regular_commentary() -> None:
    arbiter = CommentaryArbiter(config(llm_do_not_disturb=False))
    snapshot = GameSnapshot(phase="ended", game_number=6)

    assert (
        arbiter.allow_llm(
            GameEvent("game_ended", 10, "won", 101.0, {"result": "won"}),
            snapshot,
            now=101.0,
        )
        is False
    )


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


def test_live_battlegrounds_context_preserves_decision_critical_runtime_fields() -> None:
    snapshot = GameSnapshot(
        mode="battlegrounds",
        phase="recruit",
        game_number=9,
        round=7,
        battlegrounds=BattlegroundsSnapshot(
            round=7,
            phase="recruit",
            gold=8,
            max_gold=10,
            tavern_tier=4,
            frozen=True,
            refresh_cost=0,
            upgrade_cost=7,
            shop=(
                BattlegroundsCardSnapshot(
                    card_id="BG_SPELL_001",
                    name="测试酒馆法术",
                    card_type="SPELL",
                    tier=3,
                    position=1,
                    premium=False,
                    current_cost=1,
                    keywords={"taunt": False, "divine_shield": None},
                ),
            ),
            hand=(
                BattlegroundsCardSnapshot(
                    card_id="BG_MINION_HAND",
                    name="手牌随从",
                    card_type="MINION",
                    attack=6,
                    health=7,
                    tier=4,
                    position=1,
                    premium=True,
                    current_cost=2,
                    keywords={"reborn": True},
                ),
            ),
            warband=(
                BattlegroundsCardSnapshot(
                    card_id="BG_MINION_BOARD",
                    name="战团随从",
                    card_type="MINION",
                    attack=12,
                    health=13,
                    tier=5,
                    position=1,
                    premium=False,
                    keywords={"taunt": True, "divine_shield": True},
                ),
            ),
            economy=BattlegroundsEconomySnapshot(
                upgrade_cost=7,
                refresh_cost=0,
                revision=11,
                observed_at=1234.5,
            ),
            areas={
                "shop": BattlegroundsAreaSnapshot(
                    complete=True,
                    revision=12,
                    observed_at=1234.5,
                    round=7,
                    phase="recruit",
                )
            },
        ),
    )

    prompt = build_live_state_context(snapshot, observed_at=1235.0)
    payload = json.loads(prompt.split("过滤后的实时局势 JSON：", 1)[1])
    battlegrounds = payload["state"]["battlegrounds"]
    card_fields = payload["schema"]["card"].split("|")

    def card_at(zone: str, index: int) -> dict[str, str]:
        return dict(zip(card_fields, battlegrounds[zone][index].split("|"), strict=True))

    cost_fields = payload["schema"]["costs"].split("|")
    costs = dict(zip(cost_fields, battlegrounds["costs"], strict=True))
    area_fields = payload["schema"]["area"].split("|")
    shop_area = dict(
        zip(area_fields, battlegrounds["areas"]["shop"].split("|"), strict=True)
    )

    assert payload["kind"] == "hearthstone_live_state"
    assert payload["observed_at"] == 1235.0
    assert costs["refresh"] == 0
    assert costs["upgrade"] == 7
    assert costs["revision"] == 11
    assert shop_area["complete"] == "1"
    assert shop_area["round"] == "7"
    assert card_at("shop", 0) == {
        "id": "BG_SPELL_001",
        "name": "测试酒馆法术",
        "type": "S",
        "attack": "0",
        "health": "?",
        "tier": "3",
        "position": "1",
        "premium": "0",
        "current_cost": "1",
        "keyword_set_index": "0",
    }
    assert payload["keyword_sets"][0] == "?"
    assert card_at("hand", 0)["premium"] == "1"
    assert card_at("hand", 0)["current_cost"] == "2"
    assert payload["keyword_sets"][int(card_at("hand", 0)["keyword_set_index"])] == "r"
    assert payload["keyword_sets"][int(card_at("warband", 0)["keyword_set_index"])] == "td"


def test_live_constructed_context_never_shares_hand_or_choice_identities() -> None:
    snapshot = GameSnapshot(
        mode="constructed",
        phase="playing",
        game_number=4,
        constructed=ConstructedSnapshot(
            player=ConstructedSideSnapshot(
                known_hand=(
                    ConstructedCardSnapshot(
                        card_id="PRIVATE_HAND_SENTINEL",
                        name="私有手牌",
                        card_type="SPELL",
                        cost=4,
                    ),
                ),
                hand_count=1,
                hand_identities_complete=True,
            )
        ),
        choice=ChoiceSnapshot(
            choice_type="discover",
            options=(
                ConstructedCardSnapshot(
                    card_id="PRIVATE_CHOICE_SENTINEL",
                    name="私有发现选项",
                ),
            ),
        ),
    )

    prompt = build_live_state_context(snapshot, observed_at=1235.0)

    assert "PRIVATE_HAND_SENTINEL" not in prompt
    assert "PRIVATE_CHOICE_SENTINEL" not in prompt
    assert '"count":1' in prompt
    assert '"choice_type":"discover"' in prompt


def test_live_constructed_delivery_includes_turn_owner_and_public_boards() -> None:
    snapshot = GameSnapshot(
        mode="constructed",
        phase="playing",
        game_number=4,
        turn=7,
        round=4,
        active_side="opponent",
        player=SideSnapshot(board_count=1),
        opponent=SideSnapshot(board_count=1),
        constructed=ConstructedSnapshot(
            variant="ranked",
            player=ConstructedSideSnapshot(
                hand_count=1,
                known_hand=(
                    ConstructedCardSnapshot(
                        card_id="PRIVATE_PLAYER_HAND",
                        name="私有手牌",
                        card_type="SPELL",
                        keywords_complete=True,
                    ),
                ),
                hand_identities_complete=True,
                board=(
                    ConstructedCardSnapshot(
                        card_id="PLAYER_PUBLIC_MINION",
                        name="我方公开随从",
                        card_type="MINION",
                        zone_position=2,
                        attack=4,
                        health=5,
                        keywords=("taunt", "divine_shield"),
                        keywords_complete=True,
                    ),
                ),
                board_identities_complete=True,
            ),
            opponent=ConstructedSideSnapshot(
                hand_count=5,
                known_hand=(
                    ConstructedCardSnapshot(
                        card_id="PRIVATE_OPPONENT_HAND",
                        name="对手隐藏手牌",
                        card_type="SPELL",
                    ),
                ),
                board=(
                    ConstructedCardSnapshot(
                        card_id="OPPONENT_PUBLIC_MINION",
                        name="对方公开随从",
                        card_type="MINION",
                        zone_position=1,
                        attack=3,
                        health=4,
                        keywords=("reborn", "stealth"),
                    ),
                ),
                board_identities_complete=True,
            ),
        ),
        choice=ChoiceSnapshot(
            choice_type="discover",
            options=(ConstructedCardSnapshot(card_id="PRIVATE_CHOICE"),),
        ),
    )

    prompts = build_live_state_contexts(
        snapshot,
        observed_at=1_770_000_000.123,
        max_prompt_bytes=900,
    )
    payloads = [json.loads(prompt.split(":", 1)[1]) for prompt in prompts]
    by_segment = {payload["segment"]: payload for payload in payloads}
    serialized = json.dumps(payloads, ensure_ascii=False)

    assert set(by_segment) == {
        "core",
        "contract",
        "schema",
        "opponent_board_1",
        "player_board_1",
        "player_hand_1",
    }
    assert all(len(prompt.encode("utf-8")) <= 900 for prompt in prompts)
    assert by_segment["core"]["action_turn"] == 7
    assert by_segment["core"]["round"] == 4
    assert by_segment["core"]["active_side"] == "opponent"
    assert by_segment["core"]["choice"] == {
        "type": "discover",
        "min": 0,
        "max": 0,
        "option_count": 1,
    }
    assert "我方公开随从" in serialized
    assert "对方公开随从" in serialized
    assert by_segment["player_board_1"]["cards"][0] == [
        "PLAYER_PUBLIC_MINION",
        "我方公开随从",
        2,
        4,
        5,
        True,
        "td",
        "",
    ]
    assert by_segment["opponent_board_1"]["cards"][0] == [
        "OPPONENT_PUBLIC_MINION",
        "对方公开随从",
        1,
        3,
        4,
        False,
        "rs",
        "",
    ]
    assert by_segment["player_hand_1"]["cards"][0][5] is True
    assert "keywords_complete" in by_segment["schema"]["card_columns"]
    assert "私有手牌" in serialized
    assert "PRIVATE_OPPONENT_HAND" not in serialized
    assert "PRIVATE_CHOICE" not in serialized


def test_live_constructed_delivery_does_not_mark_unknown_empty_sides_complete() -> None:
    snapshot = GameSnapshot(
        mode="constructed",
        phase="playing",
        game_number=1,
        constructed=ConstructedSnapshot(),
    )

    prompts = build_live_state_contexts(snapshot, max_prompt_bytes=900)
    payloads = [json.loads(prompt.split(":", 1)[1]) for prompt in prompts]
    core = next(payload for payload in payloads if payload["segment"] == "core")

    assert "player_board" not in core["complete_counts"]
    assert "opponent_board" not in core["complete_counts"]
    assert not any(
        payload["segment"].startswith(("player_board_", "opponent_board_"))
        for payload in payloads
    )


def test_live_constructed_delivery_keeps_full_public_board_under_byte_limit() -> None:
    oversized_name = "公开但超长的随从名称" * 50
    player_board = tuple(
        ConstructedCardSnapshot(
            card_id=f"PLAYER_BOARD_{index}",
            name=oversized_name,
            card_type="MINION",
            zone_position=(1, 3, 4, 6, 8, 9, 12)[index],
            attack=index + 1,
            health=index + 2,
            keywords=("taunt", "lifesteal", "rush"),
            states=("frozen", "silenced", "future_state") if index == 0 else (),
        )
        for index in range(7)
    )
    opponent_board = tuple(
        ConstructedCardSnapshot(
            card_id=f"OPPONENT_BOARD_{index}",
            name=oversized_name,
            card_type="MINION",
            zone_position=index + 1,
            attack=index + 3,
            health=index + 4,
            keywords=("divine_shield", "reborn", "charge"),
        )
        for index in range(7)
    )
    snapshot = GameSnapshot(
        mode="constructed",
        phase="playing",
        game_number=6,
        turn=19,
        round=10,
        active_side="player",
        player=SideSnapshot(board_count=7),
        opponent=SideSnapshot(board_count=7),
        constructed=ConstructedSnapshot(
            player=ConstructedSideSnapshot(
                board=player_board,
                board_identities_complete=True,
            ),
            opponent=ConstructedSideSnapshot(
                board=opponent_board,
                board_identities_complete=True,
            ),
        ),
    )

    prompts = build_live_state_contexts(snapshot, max_prompt_bytes=900)
    serialized = "\n".join(prompts)

    payloads = [json.loads(prompt.split(":", 1)[1]) for prompt in prompts]
    segment_names = {payload["segment"] for payload in payloads}
    assert "core" in segment_names
    assert any(name.startswith("opponent_board_") for name in segment_names)
    assert any(name.startswith("player_board_") for name in segment_names)
    assert all(len(prompt.encode("utf-8")) <= 900 for prompt in prompts)
    assert all(
        not isinstance(payload.get("cards"), list)
        or len(payload["cards"]) <= 7
        for payload in payloads
    )
    assert all(f"PLAYER_BOARD_{index}" in serialized for index in range(7))
    assert all(f"OPPONENT_BOARD_{index}" in serialized for index in range(7))
    assert oversized_name not in serialized
    rows = [
        card
        for payload in payloads
        for card in payload.get("cards", [])
    ]
    rows_by_id = {card[0]: card for card in rows}
    assert rows_by_id["PLAYER_BOARD_0"][1] is None
    assert rows_by_id["PLAYER_BOARD_0"][2] == 1
    assert rows_by_id["PLAYER_BOARD_6"][2] == 12
    assert rows_by_id["PLAYER_BOARD_0"][5] is False
    assert rows_by_id["PLAYER_BOARD_0"][6] == "tlu"
    assert rows_by_id["PLAYER_BOARD_0"][7] == "fs?"
    assert rows_by_id["OPPONENT_BOARD_0"][5] is False
    assert rows_by_id["OPPONENT_BOARD_0"][6] == "drc"


def test_live_state_context_is_valid_json_and_respects_host_safe_hard_limit() -> None:
    oversized = "超长不可信卡名" * 200
    cards = tuple(
        BattlegroundsCardSnapshot(
            card_id=f"BG_{index}_{oversized}",
            name=oversized,
            card_type="MINION",
            attack=999,
            health=999,
            tier=6,
            position=index + 1,
            premium=True,
            current_cost=99,
            keywords={f"keyword_{item}_{oversized}": True for item in range(20)},
        )
        for index in range(10)
    )
    snapshot = GameSnapshot(
        mode="battlegrounds",
        phase="recruit",
        game_number=1,
        battlegrounds=BattlegroundsSnapshot(
            phase="recruit",
            shop=cards[:7],
            hand=cards,
            warband=cards[:7],
            current_choice=BattlegroundsChoiceSnapshot(
                choice_type="discover",
                count_min=1,
                count_max=1,
                source=cards[0],
                options=cards[:8],
            ),
            areas={
                area: BattlegroundsAreaSnapshot(
                    complete=True,
                    revision=1,
                    observed_at=1_770_000_000.0,
                    round=6,
                    phase="recruit",
                )
                for area in ("shop", "hand", "warband", "choice")
            },
        ),
    )

    prompt = build_live_state_context(snapshot, observed_at=1235.0, max_prompt_chars=2600)

    assert len(prompt) <= 2600
    payload = json.loads(prompt.split("过滤后的实时局势 JSON：", 1)[1])
    battlegrounds = payload["state"]["battlegrounds"]
    assert len(battlegrounds["shop"]) == 7
    assert len(battlegrounds["hand"]) == 10
    assert len(battlegrounds["warband"]) == 7
    assert battlegrounds["current_choice"]["option_count"] == 8
    assert battlegrounds["current_choice"]["detail_status"] == "tool_required"
    assert "choice_details" in battlegrounds["omitted"]
    assert "id" in payload["schema"]["card"].split("|")
    assert "current_cost" in payload["schema"]["card"].split("|")


def test_live_state_contexts_survive_packaged_host_byte_fallback_with_dynamic_state() -> None:
    oversized = "超长不可信卡名" * 200
    cards = tuple(
        BattlegroundsCardSnapshot(
            card_id=f"BG_RUNTIME_CARD_{index}",
            name=oversized,
            card_type="BATTLEGROUND_SPELL" if index == 0 else "MINION",
            attack=999,
            health=999,
            tier=6,
            position=index + 1,
            premium=index % 2 == 0,
            current_cost=index,
            keywords={
                "taunt": index % 2 == 0,
                "divine_shield": index % 3 == 0,
                "reborn": index % 5 == 0,
            },
        )
        for index in range(10)
    )
    snapshot = GameSnapshot(
        mode="battlegrounds",
        phase="recruit",
        game_number=1,
        turn=12,
        battlegrounds=BattlegroundsSnapshot(
            round=6,
            phase="recruit",
            gold=8,
            max_gold=10,
            tavern_tier=4,
            frozen=True,
            refresh_cost=0,
            upgrade_cost=6,
            economy=BattlegroundsEconomySnapshot(
                upgrade_cost=6,
                refresh_cost=0,
                revision=1,
                observed_at=1_770_000_000.0,
                gold_observation=BattlegroundsAreaSnapshot(
                    complete=True,
                    revision=1,
                    observed_at=1_770_000_000.0,
                    round=6,
                    phase="recruit",
                ),
                upgrade_observation=BattlegroundsAreaSnapshot(
                    complete=True,
                    revision=1,
                    observed_at=1_770_000_000.0,
                    round=6,
                    phase="recruit",
                ),
                refresh_observation=BattlegroundsAreaSnapshot(
                    complete=True,
                    revision=1,
                    observed_at=1_770_000_000.0,
                    round=6,
                    phase="recruit",
                ),
            ),
            shop=cards[:7],
            hand=cards,
            warband=cards[:7],
            current_choice=BattlegroundsChoiceSnapshot(
                choice_type="discover",
                count_min=1,
                count_max=1,
                source=cards[0],
                options=cards[:8],
            ),
            areas={
                area: BattlegroundsAreaSnapshot(
                    complete=True,
                    revision=1,
                    observed_at=1_770_000_000.0,
                    round=6,
                    phase="recruit",
                )
                for area in ("shop", "hand", "warband", "economy", "choice")
            },
        ),
    )

    prompts = build_live_state_contexts(
        snapshot,
        observed_at=1_770_000_000.123,
        max_prompt_bytes=900,
    )

    assert all(len(prompt.encode("utf-8")) <= 900 for prompt in prompts)
    payloads = [json.loads(prompt.split(":", 1)[1]) for prompt in prompts]
    assert {payload["segment"] for payload in payloads} >= {
        "core",
        "shop_1",
        "hand_1",
        "warband_1",
    }
    assert all(
        not isinstance(payload.get("cards"), list)
        or len(payload["cards"]) <= 10
        for payload in payloads
    )
    serialized = json.dumps(payloads, ensure_ascii=False)
    assert "BG_RUNTIME_CARD" in serialized
    assert oversized not in serialized
    assert '"phase": "recruit"' in serialized
    assert '"gold": 8' in serialized
    assert '"refresh_actual_cost": 0' in serialized
    assert '"upgrade_actual_cost": 6' in serialized
    rows = [
        card
        for payload in payloads
        for card in payload.get("cards", [])
    ]
    runtime_rows = [card for card in rows if card[0] == "BG_RUNTIME_CARD_0"]
    assert runtime_rows
    assert all(card[1] is None for card in runtime_rows)
    assert all(f"BG_RUNTIME_CARD_{index}" in serialized for index in range(10))
    assert all(card[2] == 1 for card in runtime_rows)
    assert any(card[7] == "tavern_spell" and card[8] is True for card in runtime_rows)
    schema = next(payload for payload in payloads if payload["segment"] == "schema")
    assert any(
        "divine_shield" in schema["keyword_sets"][card[10]]
        for card in runtime_rows
    )


def test_live_state_contexts_change_with_mode_and_observation_time() -> None:
    constructed_prompt = build_live_state_contexts(
        GameSnapshot(mode="constructed", phase="playing", game_number=3, turn=5),
        observed_at=1235.0,
        max_prompt_bytes=900,
    )[0]
    battlegrounds_prompts = build_live_state_contexts(
        GameSnapshot(
            mode="battlegrounds",
            phase="recruit",
            game_number=99,
            battlegrounds=BattlegroundsSnapshot(
                round=12,
                phase="recruit",
                gold=10,
                refresh_cost=0,
                upgrade_cost=1,
                shop=(BattlegroundsCardSnapshot(card_id="PRIVATE_RUNTIME_CARD"),),
                areas={
                    "shop": BattlegroundsAreaSnapshot(
                        complete=True,
                        revision=1,
                        observed_at=9999.0,
                        round=12,
                        phase="recruit",
                    )
                },
            ),
        ),
        observed_at=9999.0,
        max_prompt_bytes=900,
    )
    battlegrounds_prompt = battlegrounds_prompts[0]

    assert constructed_prompt != battlegrounds_prompt
    assert '"mode":"constructed"' in constructed_prompt
    assert '"turn":5' in constructed_prompt
    assert "PRIVATE_RUNTIME_CARD" in "\n".join(battlegrounds_prompts)
    revisions = {
        json.loads(prompt.split(":", 1)[1])["bundle"].split("@", 1)[0]
        for prompt in battlegrounds_prompts
    }
    assert len(revisions) == 1
    assert next(iter(revisions)).startswith("g2r:")


def test_live_state_contexts_omit_stale_battlegrounds_areas() -> None:
    snapshot = GameSnapshot(
        mode="battlegrounds",
        phase="recruit",
        game_number=3,
        battlegrounds=BattlegroundsSnapshot(
            round=3,
            phase="recruit",
            shop=(
                BattlegroundsCardSnapshot(
                    card_id="STALE_SHOP_CARD",
                    card_type="MINION",
                    position=1,
                ),
            ),
            areas={
                "shop": BattlegroundsAreaSnapshot(
                    complete=True,
                    revision=10,
                    observed_at=100.0,
                    round=2,
                    phase="recruit",
                )
            },
        ),
    )

    contexts = build_live_state_contexts(
        snapshot,
        observed_at=102.0,
        max_prompt_bytes=900,
    )
    payloads = [json.loads(text.split(":", 1)[1]) for text in contexts]

    assert [payload["segment"] for payload in payloads] == [
        "core",
        "contract",
        "schema",
    ]
    assert "shop" not in payloads[0]["complete_counts"]
    assert "STALE_SHOP_CARD" not in json.dumps(payloads)


def test_live_state_contexts_omit_stale_battlegrounds_economy() -> None:
    stale = BattlegroundsAreaSnapshot(
        complete=True,
        revision=10,
        observed_at=100.0,
        round=3,
        phase="recruit",
    )
    snapshot = GameSnapshot(
        mode="battlegrounds",
        phase="recruit",
        game_number=3,
        battlegrounds=BattlegroundsSnapshot(
            round=3,
            phase="recruit",
            gold=8,
            max_gold=10,
            tavern_tier=4,
            frozen=True,
            refresh_cost=0,
            upgrade_cost=3,
            economy=BattlegroundsEconomySnapshot(
                upgrade_cost=3,
                refresh_cost=0,
                revision=10,
                observed_at=100.0,
                gold_observation=stale,
                upgrade_observation=stale,
                refresh_observation=stale,
            ),
            areas={"shop": stale, "economy": stale},
        ),
    )

    contexts = build_live_state_contexts(
        snapshot,
        observed_at=500.1,
        max_prompt_bytes=900,
    )
    core = json.loads(contexts[0].split(":", 1)[1])

    assert "gold" not in core
    assert "max_gold" not in core
    assert core["tavern_tier"] == 4
    assert "frozen" not in core
    assert "refresh_actual_cost" not in core
    assert "upgrade_actual_cost" not in core
    assert "shop" not in core["complete_counts"]
