# 炉石猫娘陪玩

面向 N.E.K.O 的炉石传说与酒馆战棋陪伴插件。插件只读本机 `Power.log`，把玩家当前可见局势提炼成结构化事实、情绪信号和低打扰的发言时机，再由用户当前的 N.E.K.O 角色自然回应。它不使用本地模板扮演角色，也不把陪伴降格成数据通知。

> 本项目是非官方社区插件，与 Blizzard Entertainment、网易及其关联公司无隶属或背书关系。

## 陪伴体验

- 监听到新鲜对局后，以隐藏 `read` 按固定 key 覆盖更新过滤后的实时公开状态；普通对战为一个精简分段，酒馆为二至三个分段，每段不超过 900 UTF-8 字节。酒馆分段保留模式、阶段、回合、金币、刷新/升本费用，以及商店、手牌、战团中实际观测的 CardID、类型、费用、金色状态和当前关键词。
- 显式配置固定 `target_lanlan` 时，额外建立稳定炉石场景并定向投递；目标为空时，隐藏 `read` 省略目标，由 N.E.K.O 只在恰好一个角色会话已连接时安全接收，多会话歧义时宿主会丢弃而不会广播。插件不会从消息上下文或宿主私有接口猜测角色；需要稳定多会话路由或主动解说时必须填写角色名，工具结果则自动返回实际发起调用的对话。
- 三连、低血量、升本、逐轮战果与最终名次等公开事实会形成结构化情绪信号；真正的台词始终由当前 N.E.K.O 角色生成。
- 主动解说只选择稀疏且有情绪价值的事件，并受普通/关键冷却与 30 秒用户聊天静默窗约束；静默窗内只有优先级 `>=9` 的关键事件可以绕过。
- 用户问“现在酒馆玩什么流派”“这局怎么走”或任何酒馆当前事实时，Agent 路由调用 `query_battlegrounds_state`，其内部读取 `hearthstone_battlegrounds_advice` 的最新动态局势、规则依据、统计边界和逐项 evidence gate。
- 用户问普通对战“第几回合”“轮到谁”、具体手牌、Choice 或出牌取舍时，Agent 路由调用 `query_constructed_state`，其内部重新读取 `hearthstone_current_state`；不能只沿用聊天历史中的快照或短评。
- 通用状态工具在酒馆模式只返回专用工具重定向，不重复发送整份酒馆快照；酒馆结果先给出逐卡 `known_affordable`、`known_unaffordable` 或 `unknown_cost_may_be_zero` 的紧凑决策面，再附完整局势与规则依据。
- 启用插件即默认允许过滤后的实时公开状态和问答查询入口供当前角色使用，用户仍可随时关闭；主动解说默认关闭，其开关只控制角色是否主动插话。

公开 Plugin SDK 不提供 `respond` 最终文本回调，因此插件不会伪称能把角色实际台词复制到自己的窗口。NEKO 的角色回复、语音和宿主界面是主输出；随包提供的透明浮层只用于用户显式执行诊断测试。

## 酒馆战棋支持

`v0.3.2` 支持普通对战与单排/双排酒馆的可验证公开状态：

- 战棋模式、Bob、本地玩家与最多八名英雄；
- 英雄选择阶段实际观测到的本地候选、选择完成后的我方英雄、招募/战斗阶段、逐轮胜负、回合、当前对手；
- 金币、酒馆等级、冻结、商店、手牌与战团；卡牌包含日志实际观测的类型、当前费用、金色状态、站位以及嘲讽/圣盾/复生等当前关键词；
- 当前刷新/升本按钮的实际费用、三连/发现/任务/饰品等本地 Choice，以及商店、手牌、战团、经济和 Choice 各区域的完整度、回合、阶段、观测时间与 revision；
- 英雄血量/护甲、淘汰、最终名次；
- 每场公开战斗开始时首次确认的对手阵容会保留随从 CardID、名称、攻血和星级；插件按对手保留最近一次观察，并始终标记为“上次观察，第 N 回合，非当前阵容”；
- 任务、饰品、畸变、伙伴等赛季机制的公开 ID/进度；
- 单排 Top 4、双排 Top 2、第一名率、平均名次和英雄维度的本机聚合统计。

当前能力有三条明确边界：候选英雄只来自 `Power.log` 为本地玩家实际公开的可选英雄，不推断未观测或已锁定的选项；定性选牌依据新鲜完整的商店、手牌、战团、卡牌类型和目录规则，个别 `current_cost` 缺失时仍可比较方向，但精确可负担性、花费与购买顺序必须同时具备当前金币和当前商店所有卡牌的实际费用。只要任一卡牌费用未知，即使金币为 0，也不能断言整家商店全部买不起。升本、刷新、Choice 和站位继续按各自证据检查，证据不全会返回 `partial`/`missing_evidence`，战斗阶段或缓存状态不会被当作可执行购买依据；普通对战可以按需提供本地玩家可见的具体手牌、动态费用、英雄/技能、武器、随从、地标和发现选项，但 `Power.log` 不保证给出完整合法操作与目标枚举，角色必须说明不完整处而不能伪装成确定的最优解。所有未由日志实际观测的动态字段保持 `null`，公共目录只补带来源的规则事实，不冒充实时值。

随包赛季资料当前固定为战棋第 14 赛季、补丁 36.2.2、`Dark Gifts of Dalaran`，来源链包含 Blizzard 36.2 赛季说明和 36.2.2 平衡补丁，验证日期为 2026-08-19。它是版本化规则资料，不是胜率数据。

插件每日从公开的 [hsbg.cards API](https://hsbg.cards/api-docs) 更新当前卡池与卡牌规则，并在 N.E.K.O 分配的插件数据目录离线降级。咨询工具只返回牌面中已观察卡牌的去重事实和不针对具体大厅的卡池计数；不在本地计算流派评分，也不生成角色台词。Card data: [hsbg.cards](https://hsbg.cards/about), subject to its [terms](https://hsbg.cards/terms).

HSReplay Tier7 与 Firestone 的全局表现数据属于各自的私有遥测；本项目没有获得可再发布的授权 API。工具会明确返回全局数据不可用，不抓取网页，也不编造档位、胜率或样本。

## 安全边界

- 只读 Hearthstone 自己生成的 `Power.log`，不注入、不读内存、不抓包、不模拟协议。
- 自动连接仅查询进程列表中名称精确为 `Hearthstone.exe` 的可执行文件路径，再检查同目录 `Logs`；不扫描磁盘、不读取进程内存。
- 不自动点击、出牌或代打，不推断隐藏手牌、未揭示奥秘或未来商店。
- 不上传原始日志，不保留玩家名、BattleTag、账号 ID 或完整单局历史；默认共享仅发送隐私文档列明的有限近期公开事实，包括有界实时上下文、按需查询结果和已启用主动解说的事件上下文，并可在面板中关闭。
- 首次接入已有日志默认且最多只在本机恢复解析末尾 64 MiB，这些日志字节不会整体发送给模型。
- 卡牌目录更新只发送固定的公共目录 GET，不发送牌局、卡牌 ID 或玩家信息；可用 `card_catalog_network_enabled=false` 关闭。
- 本机统计只保存按赛季、模式和英雄聚合的场次与名次计数。
- LLM 工具默认可使用过滤后的玩家可见局势；用户明确关闭后工具和主动解说都会停止，旁观模式也不触发主动解说。
- “配置日志”是明确的用户操作，只备份并更新 `%LOCALAPPDATA%\Blizzard\Hearthstone\log.config`。

完整字段和数据去向见[隐私与安全](docs/privacy-security.md)。

## 系统要求与安装

- Windows 10/11；
- Hearthstone 桌面客户端；
- Python `>=3.11,<3.12`；
- 仅诊断浮层需要标准库 `tkinter`。

当前版本请使用 GitHub Release 的 `.neko-plugin` 文件；正式上架后也可从 N.E.K.O 插件市场安装。源码 ZIP 不是插件包。

1. 启动“炉石猫娘陪玩”；局势问答默认可用，是否开启主动陪伴可按需保存。
2. 打开炉石，插件会自动识别安装位置和正在更新的会话日志。
3. 确认日志状态从 `waiting_for_log` 变为 `watching`；只有一直等待时才在底部诊断区配置日志或保存自定义位置。
4. 直接在聊天中询问当前局势和酒馆建议；需要角色主动陪玩时，再开启主动陪伴。

`plugin.toml` 只提供安装默认值；N.E.K.O 原生配置服务是唯一运行时设置来源。只有用户显式填写的 `log_path` 会作为设置保存。自动发现的进程路径和实际日志路径只保留在当前运行状态与本机诊断中，不进入 LLM、Plugin Store 或插件遥测。Plugin Store 长期只保存聚合酒馆统计，其 client 生命周期由 N.E.K.O 宿主管理。

详细步骤见[快速开始](docs/quickstart.md)。

## 开发与打包

Python 测试：

```powershell
python -m pytest
python -m compileall -q .
```

从 N.E.K.O 仓库根目录执行官方 CLI：

```powershell
uv run python -m plugin.neko_plugin_cli.cli check --release --market-release "<plugin-repo>"
uv run python -m plugin.neko_plugin_cli.cli build "<plugin-repo>" --out "<plugin-repo>\dist\hearthstone_companion.neko-plugin"
uv run python -m plugin.neko_plugin_cli.cli inspect "<plugin-repo>\dist\hearthstone_companion.neko-plugin"
uv run python -m plugin.neko_plugin_cli.cli verify "<plugin-repo>\dist\hearthstone_companion.neko-plugin"
```

## 参考与证据

- [N.E.K.O](https://github.com/Project-N-E-K-O/N.E.K.O)
- [N.E.K.O 插件文档](https://project-neko.online/zh-CN/plugins/)
- [Blizzard 36.2 Patch Notes](https://hearthstone.blizzard.com/en-us/news/24290432/36-2-patch-notes)
- [Blizzard 36.2.2 Patch Notes](https://hearthstone.blizzard.com/en-us/news/24293284/3622-patch-notes)
- [HS Battlegrounds Cards API](https://hsbg.cards/api-docs)
- [Hearthstone Deck Tracker](https://github.com/HearthSim/Hearthstone-Deck-Tracker)
- [python-hslog](https://github.com/HearthSim/python-hslog)
- [hsreplay-test-data](https://github.com/HearthSim/hsreplay-test-data)

## English summary

Hearthstone Catgirl Companion is a read-only N.E.K.O plugin for constructed Hearthstone and Battlegrounds. Hidden `read` messages keep a bounded, filtered live public snapshot available to the active role, while Agent-visible query entries fetch fresh, richer facts and evidence gates on demand. Users can disable data sharing without disabling local log monitoring, while proactive commentary remains off by default. It supports Power.log-observed local hero choices, solo and Duos Battlegrounds state, per-combat outcomes, aggregate-only local results, and versioned official season rules. N.E.K.O owns the actual character response; the separate transparent overlay is diagnostic-only because the public SDK does not return generated reply text. The plugin never claims access to unlicensed global win-rate telemetry.

## 许可证

[MIT License](LICENSE)
