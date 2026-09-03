from __future__ import annotations

from hearthstone_companion_under_test.live_query import (
    LiveQueryIntent,
    classify_live_query,
    normalize_query_text,
    requests_live_advice,
    requests_live_rules,
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


def test_classifies_the_five_official_answer_matrix_questions() -> None:
    cases = {
        "现在第几回合？只回答游戏里的完整回合数。": "overview",
        "对面场上现在有哪些随从？只用 CardID 完整列出。": "opponent",
        (
            "请查询当前炉石酒馆战棋的商店有哪些牌。按 CardID 分组，逐组说出类型、"
            "实际费用、金色状态和完整的当前关键词。"
        ): "shop",
        "请查询当前炉石酒馆战棋局面：我能升本吗？升完还剩多少金币？": "economy",
    }

    for question, expected_focus in cases.items():
        intent = classify_live_query(question)
        assert intent is not None
        assert intent.focus == expected_focus


def test_shop_subject_wins_over_embedded_cost_terms() -> None:
    intent = classify_live_query(
        "商店每张牌的实际费用是多少，我现在应该买哪张酒馆法术？"
    )

    assert intent == LiveQueryIntent(mode_hint="battlegrounds", focus="shop")


def test_distinguishes_factual_live_queries_from_advice_requests() -> None:
    assert requests_live_advice(
        "商店有哪些牌？逐组说出类型、实际费用、金色状态和当前关键词。"
    ) is False
    assert requests_live_advice("商店里我应该买哪个随从？") is True
    assert requests_live_advice("这回合整体怎么操作？") is True
    assert requests_live_advice("我能升本吗？") is False
    assert requests_live_advice("商店还是只有三张牌吗？") is False
    assert requests_live_advice("现在还是第3回合吗？") is False
    for question in (
        "这几个选哪个？",
        "当前战团怎么站位？",
        "这回合怎么出？",
        "商店里哪张最好？",
        "我现在是升本还是刷新？",
        "买这个还是买那个？",
        "三连还是升本？",
        "升本还是不升？",
        "刷新还是不刷？",
        "这两张哪个好？",
        "有没有必要升本？",
    ):
        assert requests_live_advice(question) is True


def test_comparison_and_necessity_queries_are_classified_for_live_routing() -> None:
    cases = {
        "我现在是升本还是刷新？": "economy",
        "买这个还是买那个？": "shop",
        "这两张哪个好？": "shop",
        "有没有必要升本？": "economy",
    }

    for question, expected_focus in cases.items():
        intent = classify_live_query(question)
        assert intent is not None
        assert intent.focus == expected_focus


def test_distinguishes_card_rule_lookups_from_dynamic_fact_queries() -> None:
    assert requests_live_rules("商店里每张牌的效果是什么？") is True
    assert requests_live_rules("这张牌的规则文本是什么？") is True
    assert requests_live_rules("商店有哪些牌和当前关键词？") is False


def test_general_turn_decision_wins_over_round_overview() -> None:
    intent = classify_live_query("这回合整体应该怎么操作？")

    assert intent is not None
    assert intent.focus == "strategy"


def test_upgrade_refresh_and_freeze_queries_use_economy_focus() -> None:
    for question in (
        "我能升本吗？",
        "现在刷新实际费用是多少？",
        "当前冻结需要费用吗？",
    ):
        intent = classify_live_query(question)
        assert intent is not None
        assert intent.focus == "economy"


def test_rejects_unrelated_chat_and_supports_explicit_follow_up_context() -> None:
    previous = LiveQueryIntent(mode_hint="battlegrounds", focus="shop")

    assert classify_live_query("今天天气怎么样？") is None
    assert classify_live_query("快点告诉我") is None
    assert classify_live_query("快点告诉我", previous_intent=previous) == LiveQueryIntent(
        focus="shop",
        follow_up=True,
    )
