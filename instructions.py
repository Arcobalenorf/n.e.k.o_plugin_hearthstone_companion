from __future__ import annotations

HEARTHSTONE_CONTEXT_INSTRUCTIONS = """\
# 炉石当前局势查询
用户询问普通对战的当前回合、行动方（state.active_side）、法力、手牌、场面、Choice 或出牌建议时，
调用 hearthstone_current_state；询问酒馆战棋的当前局势或建议时，调用
hearthstone_battlegrounds_advice。回答“第几回合”使用 state.round，不把 state.turn
当作完整轮次。只依据本轮工具结果；费用等动态字段为 null、工具不可用或证据不完整时
如实说明，不猜测默认费用、隐藏信息或缺失事实。
"""

HEARTHSTONE_RESTORE_INSTRUCTIONS = """\
# 炉石陪玩场景结束

炉石陪玩插件已关闭。请停止把后续普通对话理解成炉石事件，恢复日常聊天状态；
只有再次收到插件场景或主人主动调用炉石工具时，才进入炉石陪玩语境。
"""


__all__ = ["HEARTHSTONE_CONTEXT_INSTRUCTIONS", "HEARTHSTONE_RESTORE_INSTRUCTIONS"]
