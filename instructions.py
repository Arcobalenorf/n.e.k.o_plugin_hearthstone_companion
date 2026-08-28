from __future__ import annotations

HEARTHSTONE_CONTEXT_INSTRUCTIONS = """\
# 炉石当前局势查询
用户询问当前炉石对局的回合、行动方、法力、手牌、场面、Choice、出牌建议，或酒馆战棋的
商店、战团、金币、升本、刷新和购买建议时，必须读取本轮工具结果。仅问当前回合或行动方时
调用无参数 hearthstone_current_turn；其他当前局势调用 hearthstone_live_state，query 可省略，
传入时使用用户原问题。只依据本轮工具结果；回答“第几回合”使用 round，不把 action_turn 当作完整轮次。费用等动态字段
为 null、工具不可用或证据不完整时如实说明，不猜测默认费用、隐藏信息或缺失事实。
"""

__all__ = ["HEARTHSTONE_CONTEXT_INSTRUCTIONS"]
