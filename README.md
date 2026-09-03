# 炉石猫娘陪玩

面向 N.E.K.O 的炉石传说与酒馆战棋陪伴插件。插件只读本机 `Power.log`，把玩家当前可见局势提炼成结构化事实、情绪信号和低打扰的发言时机，再由用户当前的 N.E.K.O 角色自然回应。它不使用本地模板扮演角色，也不把陪伴降格成数据通知。

> 本项目是非官方社区插件，与 Blizzard Entertainment、网易及其关联公司无隶属或背书关系。

## 陪伴体验

- 监听到新鲜对局后，本机持续维护一份权威结构化快照；状态变化会覆盖一个不可见、逻辑原子的 `hearthstone_live_segment_v2` 被动分段包，不生成可见聊天消息。每段用 `bundle=<revision>@i/n` 绑定同一包，并且整包只有一个回答契约和一个字段 schema；酒馆卡牌显式携带类型、实际费用、金色状态、关键词完整度和可无损解码的当前关键词集合。每段经真实宿主 parser 后不超过 180 tokens，并以目标和 segment 组成稳定 `coalesce_key`；整包按当前阶段控制在宿主 3000-token selector 预算内。v1、缺段、混合 revision 或 tombstone 后不得作为当前事实。显式配置目标时定向发布；未配置目标时省略 `target_lanlan`，宿主只会在恰好一个连接会话时接收，零个或多个会话都会丢弃。
- 当前事实只通过三条官方链路到达角色：逻辑原子的 revisioned passive segment bundle、两个职责清晰的首答同轮 `@llm_tool`，以及单一 Agent 查询入口。LLM 工具 callback 不携带可信角色、会话或 turn 身份，插件只返回当刻快照结果，不会再另发一条 tool-result `respond`；这避免把独立主动 turn 误当作原回答的可靠续写。
- 三连、低血量、升本、逐轮战果与最终名次等公开事实会形成结构化情绪信号；真正的台词始终由当前 N.E.K.O 角色生成。
- 新对局开始、插件重新接上进行中的对局和最终结算属于独立生命周期：默认各回应一次，不受中局解说冷却或聊天静默窗影响；未配置目标时只提交 targetless 请求，并仅依赖宿主恰好一个在线会话时的路由，零个或多个在线会话都不会投递。
- 免打扰模式默认关闭；中局解说仍只选择稀疏且有情绪价值的事件，并受普通/关键冷却与 30 秒用户聊天静默窗约束；希望安静游玩时可随时开启免打扰。
- 用户只问当前回合或行动方时，模型调用无参数 `hearthstone_current_turn`；场面、手牌、Choice、商店、战团、经济或决策调用 `hearthstone_live_state`。综合工具只保留可选 `query`，插件自动识别模式和聚焦。
- 酒馆快照在本机建立逐卡 `known_affordable`、`known_unaffordable` 或 `unknown_cost_may_be_zero` 的决策面，再按问题焦点返回必要局势、证据和规则依据。
- 回合、场面、手牌、商店、经济、Choice、对手或综合策略先在插件内生成最多 4096 bytes 的 canonical 聚焦事实，再按官方 callback 契约把确定性纯文本放入 `output` 给模型；除逐卡事实外，建议查询还携带所问 capability、相关公共规则、当前购买判断和决策护栏，综合策略携带当前阶段所需的多个视图。模型无需遍历深层 JSON，也不会收到无关区域的完整酒馆状态。
- 启用插件即默认允许问答工具按需提供过滤后的玩家可见状态，并回应对局开始、重连和结算；关闭局势共享会一并停止这些能力。免打扰模式默认关闭。

公开 Plugin SDK 不提供 `respond` 最终文本回调；`submitted=true` 也只表示 SDK 本地提交路径已接管请求，不证明宿主已消费、模型已生成回答或音频已播放。因此插件不会伪称能把角色实际台词复制到自己的窗口。NEKO 的角色回复、语音和宿主界面是主输出；随包提供的透明浮层只用于用户显式执行诊断测试。

## 酒馆战棋支持

`v0.4.0` 支持普通对战与单排/双排酒馆的可验证公开状态：

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
- 不上传原始日志，不保留玩家名、BattleTag、账号 ID 或完整单局历史；默认共享用可替换的后台 `read` 上下文同步有限的近期公开状态。未配置角色时不附目标，宿主仅在恰好一个在线会话时接收；工具查询、Agent、生命周期回应和免打扰关闭后的中局解说也只使用过滤后状态，可随时在面板中关闭。
- 首次接入已有日志默认且最多只在本机恢复解析末尾 64 MiB，这些日志字节不会整体发送给模型。
- 卡牌目录更新只发送固定的公共目录 GET，不发送牌局、卡牌 ID 或玩家信息；可用 `card_catalog_network_enabled=false` 关闭。
- 本机统计只保存按赛季、模式和英雄聚合的场次与名次计数。
- LLM 工具和生命周期回应默认可使用过滤后的玩家可见局势；用户明确关闭共享后，工具、生命周期和中局主动解说都会停止，旁观模式也不触发主动回应。
- “配置日志”是明确的用户操作，只备份并更新 `%LOCALAPPDATA%\Blizzard\Hearthstone\log.config`。

完整字段和数据去向见[隐私与安全](docs/privacy-security.md)。

## 系统要求与安装

- Windows 10/11；
- Hearthstone 桌面客户端；
- Python `>=3.11,<3.12`；
- 仅诊断浮层需要标准库 `tkinter`。

当前版本请使用 GitHub Release 的 `.neko-plugin` 文件；正式上架后也可从 N.E.K.O 插件市场安装。源码 ZIP 不是插件包。

1. 启动“炉石猫娘陪玩”；局势问答与开场/重连/结算回应默认可用，免打扰模式默认关闭。
2. 打开炉石，插件会自动识别安装位置和正在更新的会话日志。
3. 确认日志状态从 `waiting_for_log` 变为 `watching`；只有一直等待时才在底部诊断区配置日志或保存自定义位置。
4. 直接在聊天中询问当前局势和酒馆建议；希望安静游玩时再开启免打扰，中局主动回应会暂停，生命周期回应和按需问答不受影响。

面板的“局势查询链路健康”会分别显示日志新鲜度、权威快照 revision、同轮工具注册，以及最近一次同轮工具、Agent 和生命周期投递状态。“导出脱敏诊断”只导出这些计数、状态码和区域完整度，不包含日志路径、玩家/角色身份、原问题、模型回复或逐卡局势。

`plugin.toml` 只提供安装默认值；N.E.K.O 原生配置服务是唯一运行时设置来源。只有用户显式填写的 `log_path` 会作为设置保存。自动发现的进程路径和实际日志路径只保留在当前运行状态与本机诊断中，不进入 LLM、Plugin Store 或插件遥测。Plugin Store 长期只保存聚合酒馆统计，其 client 生命周期由 N.E.K.O 宿主管理。

详细步骤见[快速开始](docs/quickstart.md)。

## 开发与打包

Python 测试：

```powershell
python -m pytest
python -m compileall -q .
```

真实日志回归使用开发专用的精确检查点探针。路径和行号只通过命令行传入；输出使用检查点别名，不打印路径、原始日志或完整卡牌状态。第一层验证解析器与生产工具 serializer：

```powershell
uv run python tests/real_log_checkpoint_probe.py `
  --checkpoint <alias> <checkpoint-kind> <line-number> <Power.log>
```

第二层是隔离真实宿主验证：每条 lane 创建全新的 N.E.K.O 主服务、记忆服务、会话、浏览器和正式插件服务。探针验证隔离身份后，从官方 `/api/tools` 读取远程注册并向登记的 loopback callback 发出标准请求，确定性证明两个工具能够精确执行并返回对应实时事实，同时核对被动包、生命周期提交、环境稳定和资源清理。浏览器另行记录模型是否选择工具、是否形成可见回答以及生命周期是否产生角色台词；这些模型行为是诊断信息，不决定矩阵顶层结果。插件进程树和临时文件始终在 `finally` 清理，任何同名冲突都会 `SKIP`，不会覆盖已安装插件：

```powershell
uv run python tests/neko_answer_isolated_matrix.py `
  --neko-root <N.E.K.O源码根目录> `
  --neko-python <N.E.K.O虚拟环境python.exe> `
  --runtime-assets-dir <预构建的N.E.K.O聊天前端资产目录> `
  --storage-template <只读配置模板目录> `
  --role <隔离测试角色> `
  --case constructed_round_v1 <line-number> <constructed-Power.log> `
  --case constructed_opponent_v1 <line-number> <constructed-Power.log> `
  --case bg_shop_v1 <line-number> <battlegrounds-Power.log> `
  --case bg_upgrade_blocked_v1 <line-number> <battlegrounds-Power.log> `
  --case bg_upgrade_affordable_v1 <line-number> <battlegrounds-Power.log> `
  --lifecycle-edge constructed_started_v1 458 1143 <constructed-lifecycle-Power.log> `
  --lifecycle-edge constructed_ended_v1 20346 20347 <constructed-lifecycle-Power.log> `
  --evidence-output .github/e2e-evidence/v0.4.0.json
```

支持的固定查询用例为 `constructed_round_v1`、`constructed_opponent_v1`、`bg_shop_v1`、`bg_upgrade_blocked_v1` 和 `bg_upgrade_affordable_v1`，固定生命周期边界为 `constructed_started_v1` 与 `constructed_ended_v1`。完整矩阵只接受 release workflow 固定的干净 N.E.K.O commit；runner 从该 commit 的 Git 对象导出允许的 tracked 文件，ignored、untracked 或已修改的工作树字节不会进入一次性 runtime。`--runtime-assets-dir` 必须提供与 `tests/neko_runtime_assets.json` 中固定 commit、前端 Git tree、大小和 SHA-256 全部匹配的预构建 `neko-chat-window.iife.js` 与 `neko-chat-window.css`；外部目录自带的 revision 或 manifest 不受信任。runner 不修改宿主工作树，也不会自行构建前端。缺少隔离环境、真实日志、角色或浏览器时报告 `SKIP`。每个 case 的 `answer_observation_status` 可以因模型未回答、未选择工具或遗漏事实而是 `FAIL`，但日志检查点、SDK 注册、精确一次 callback、被动包完整性、生命周期提交、固定宿主 revision、环境稳定和清理结果决定矩阵顶层状态。

正式发布门禁覆盖可确定验证的插件职责：全量 Python 测试、Ruff、字节码编译、固定 SDK 生命周期、Hosted UI 编译、版本/tag/说明一致性、source-bound 真实宿主链路证据，以及官方 `check`、`build`、`inspect`、`verify`。证据验证器只检查注册、callback、被动包、生命周期提交、环境稳定和清理，不检查模型最终措辞或工具选择。

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

Hearthstone Catgirl Companion is a read-only N.E.K.O plugin for constructed Hearthstone and Battlegrounds. It maintains one authoritative local snapshot and exposes a zero-argument `hearthstone_current_turn` tool, a compact `hearthstone_live_state` tool, and one Agent entry. Its supported answer paths are a logically atomic `hearthstone_live_segment_v2` passive bundle, the official same-turn `@llm_tool` callback, and the Agent entry. Every v2 segment is independently parseable, uses `bundle=<revision>@i/n`, and stays at or below 180 host tokens; one complete same-revision bundle contains exactly one answer contract and one schema and remains within the host's 3000-token selector budget. Tool callbacks contain no trusted role, conversation, or turn identity, so the plugin returns the current snapshot inline and never emits a separate tool-result `respond`. Passive context, lifecycle messages, and sparse commentary are targetless when no role is configured and therefore depend on the host having exactly one online session; zero or multiple online sessions are not routed. An SDK `submitted` receipt is not proof of host consumption or a final answer. Users can disable data sharing without disabling local log monitoring. Do Not Disturb is off by default, allowing sparse mid-match commentary; users can turn it on without disabling on-demand answers or match lifecycle reactions. It supports Power.log-observed local hero choices, solo and Duos Battlegrounds state, per-combat outcomes, aggregate-only local results, and versioned official season rules. N.E.K.O owns the actual character response; the separate transparent overlay is diagnostic-only because the public SDK does not return generated reply text. The plugin never claims access to unlicensed global win-rate telemetry.

## 许可证

[MIT License](LICENSE)
