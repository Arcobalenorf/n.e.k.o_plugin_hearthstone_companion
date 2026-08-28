from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_SPACE_RE = re.compile(r"\s+")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]+")

_BATTLEGROUNDS_TERMS = (
    "酒馆战棋",
    "酒馆",
    "战棋",
    "鲍勃",
    "bob",
    "商店",
    "酒馆法术",
    "战团",
    "升本",
    "刷新",
    "冻结",
    "金币",
    "酒馆等级",
    "流派",
)
_CONSTRUCTED_TERMS = (
    "标准",
    "狂野",
    "竞技场",
    "法力",
    "水晶",
    "牌库",
    "奥秘",
    "英雄技能",
    "武器",
    "出牌",
)
_LIVE_QUERY_TERMS = (
    "第几回合",
    "多少回合",
    "轮到谁",
    "谁的回合",
    "当前回合",
    "现在回合",
    "对面场上",
    "对手场上",
    "我场上",
    "我的场上",
    "手牌",
    "手里有什么",
    "场上有什么",
    "有什么随从",
    "买什么",
    "卖什么",
    "怎么站位",
    "怎么打",
    "怎么走",
    "怎么出",
    "出什么",
    "选哪个",
    "能不能升本",
    "可以升本",
    "能不能刷新",
    "可以刷新",
    "当前商店",
    "现在商店",
    "商店有什么",
    "现在玩什么",
    "当前流派",
    "对手是谁",
    "下一家",
    "上一家",
    "current turn",
    "whose turn",
    "opponent board",
    "my hand",
    "what should i buy",
)
_QUESTION_TERMS = (
    "什么",
    "多少",
    "哪",
    "谁",
    "怎么",
    "如何",
    "能否",
    "能不能",
    "可以吗",
    "该不该",
    "吗",
    "?",
    "？",
)
_GAME_OBJECT_TERMS = (
    "回合",
    "随从",
    "场上",
    "手牌",
    "法力",
    "水晶",
    "商店",
    "战团",
    "阵容",
    "站位",
    "对手",
    "对面",
    "升本",
    "刷新",
    "冻结",
    "金币",
    "酒馆",
    "战棋",
    "炉石",
    "英雄",
    "武器",
    "奥秘",
    "发现",
    "choice",
    "turn",
    "board",
    "hand",
    "shop",
)
_FOLLOW_UP_TERMS = (
    "告诉我",
    "回答我",
    "快点",
    "赶快",
    "所以呢",
    "然后呢",
    "结果呢",
    "那呢",
    "你倒是说",
    "查一下",
    "看一下",
)
@dataclass(frozen=True, slots=True)
class LiveQueryIntent:
    mode_hint: str = "auto"
    focus: str = "strategy"
    topic: str = "current_strategy"
    opponent_relation: str = "auto"
    follow_up: bool = False


def normalize_query_text(value: Any, *, limit: int = 240) -> str:
    text = _CONTROL_RE.sub(" ", str(value or ""))
    return _SPACE_RE.sub(" ", text).strip()[:limit]


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)


def classify_live_query(
    value: Any,
    *,
    previous_intent: LiveQueryIntent | None = None,
) -> LiveQueryIntent | None:
    text = normalize_query_text(value).casefold()
    if not text:
        return None

    battlegrounds = _contains_any(text, _BATTLEGROUNDS_TERMS)
    constructed = _contains_any(text, _CONSTRUCTED_TERMS)
    explicit = _contains_any(text, _LIVE_QUERY_TERMS)
    game_question = _contains_any(text, _GAME_OBJECT_TERMS) and _contains_any(
        text, _QUESTION_TERMS
    )
    follow_up = bool(previous_intent and _contains_any(text, _FOLLOW_UP_TERMS))
    if not explicit and not game_question and not follow_up:
        return None

    if battlegrounds and not constructed:
        mode_hint = "battlegrounds"
    elif constructed and not battlegrounds:
        mode_hint = "constructed"
    else:
        mode_hint = "auto"

    if _contains_any(text, ("对手", "对面", "下一家", "上一家", "opponent")):
        focus = "opponent"
    elif _contains_any(text, ("发现", "选哪个", "候选", "choice")):
        focus = "choice"
    elif _contains_any(text, ("手牌", "手里", "hand")):
        focus = "hand"
    elif _contains_any(text, ("金币", "费用", "经济", "gold", "cost")):
        focus = "economy"
    elif _contains_any(text, ("商店", "买", "卖", "升本", "刷新", "冻结", "shop")):
        focus = "shop"
    elif _contains_any(text, ("场上", "随从", "阵容", "站位", "战团", "board")):
        focus = "board"
    elif _contains_any(text, ("回合", "轮到", "生命", "血量", "法力", "水晶", "turn")):
        focus = "overview"
    else:
        focus = previous_intent.focus if follow_up and previous_intent else "strategy"

    if _contains_any(text, ("上一轮", "上轮", "上一家", "上一位", "previous", "last")):
        opponent_relation = "last"
    elif _contains_any(text, ("下一轮", "下轮", "下一家", "下一位", "next")):
        opponent_relation = "next"
    elif _contains_any(text, ("当前对手", "正在打", "current opponent")):
        opponent_relation = "current"
    else:
        opponent_relation = "auto"

    return LiveQueryIntent(
        mode_hint=mode_hint,
        focus=focus,
        opponent_relation=opponent_relation,
        follow_up=follow_up,
    )


__all__ = [
    "LiveQueryIntent",
    "classify_live_query",
    "normalize_query_text",
]
