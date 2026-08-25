from __future__ import annotations

from hearthstone_companion_under_test.live_query import (
    LiveQueryIntent,
    classify_live_query,
    normalize_query_text,
)


def test_normalize_query_text_removes_controls_collapses_space_and_bounds() -> None:
    assert normalize_query_text("  现在\x00\n 第几回合？  ", limit=8) == "现在 第几回合？"


def test_classifies_constructed_round_and_opponent_queries() -> None:
    round_intent = classify_live_query("标准模式现在第几回合？")
    opponent_intent = classify_live_query("对面场上有什么随从？")

    assert round_intent == LiveQueryIntent(mode_hint="constructed", focus="overview")
    assert opponent_intent == LiveQueryIntent(focus="opponent")


def test_classifies_battlegrounds_shop_economy_and_opponent_relation() -> None:
    shop_intent = classify_live_query("酒馆商店有什么，我应该买什么？")
    economy_intent = classify_live_query("战棋现在的金币和实际费用是多少？")
    opponent_intent = classify_live_query("酒馆下一家是谁？")

    assert shop_intent == LiveQueryIntent(mode_hint="battlegrounds", focus="shop")
    assert economy_intent == LiveQueryIntent(mode_hint="battlegrounds", focus="economy")
    assert opponent_intent == LiveQueryIntent(
        mode_hint="battlegrounds",
        focus="opponent",
        opponent_relation="next",
    )


def test_rejects_unrelated_chat_and_supports_explicit_follow_up_context() -> None:
    previous = LiveQueryIntent(mode_hint="battlegrounds", focus="shop")

    assert classify_live_query("今天天气怎么样？") is None
    assert classify_live_query("快点告诉我") is None
    assert classify_live_query("快点告诉我", previous_intent=previous) == LiveQueryIntent(
        focus="shop",
        follow_up=True,
    )
