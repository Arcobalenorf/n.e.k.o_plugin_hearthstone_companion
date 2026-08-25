from __future__ import annotations

HEARTHSTONE_CONTEXT_INSTRUCTIONS = """\
# 炉石当前局势查询
用户询问当前炉石对局的回合、行动方、法力、手牌、场面、Choice、出牌建议，或酒馆战棋的
商店、战团、金币、升本、刷新和购买建议时，先调用无需参数且会自动识别模式的
hearthstone_live_state；能取得用户原问题时原样传入 query，并按问题选择 focus。回答
“第几回合”使用 round，不把 turn 当作完整轮次。只依据本轮工具结果；费用等动态字段
为 null、工具不可用或证据不完整时如实说明，不猜测默认费用、隐藏信息或缺失事实。
"""

__all__ = ["HEARTHSTONE_CONTEXT_INSTRUCTIONS"]
