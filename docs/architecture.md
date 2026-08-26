# 架构说明

## 产品原则

N.E.K.O 的核心是关系与陪伴。插件负责理解游戏现场，不负责用本地模板扮演角色。实现遵循五条边界：

1. 本地层只做公开事实提炼、情绪信号、节奏仲裁和隐私过滤；
2. 所有主动可见台词由当前 N.E.K.O 角色通过 `ai_behavior="respond"` 生成；
3. 新鲜对局只在本机维护一份权威状态；分段 `read` 快照以固定 `coalesce_key` 持续覆盖当前会话，同轮 `@llm_tool` 查询提供更完整、按问题聚焦的事实；
4. `bus.memory` 只识别最近明确的炉石问题和实际角色，未被工具或 Agent 认领时才定向触发一次 `respond` 兜底；
5. 面板和独立浮层只承担透明诊断，不参与自动陪伴输出。

```text
Hearthstone Power.log
        |
        v
PowerLogLocator + PowerLogTailer
        |
        v
PowerLogParser -- 公开性/隐私过滤 --> GameEvent + immutable GameSnapshot
        |                                  |
        |                                  +--> aggregate-only Store statistics
        v
CompanionMonitor single-owner pipeline
        |
        +--> emotion cue + priority/cooldown/chat quiet window
        |                    |
        |                    +--> [] + respond --> active NEKO character
        |
        +--> authoritative immutable snapshot
                             |
                             +--> hearthstone_live_state @llm_tool --> current NEKO turn
                             +--> query_hearthstone_live_state Agent --> post-turn fallback

Recent user utterance --> bus.memory bounded polling --> explicit Hearthstone intent
        |
        +--> role/query ledger --> unclaimed after delay --> targeted [] + respond
        +--> claimed by tool/Agent -----------------------> no duplicate fallback

Constructed or Battlegrounds question --> same-turn hearthstone_live_state tool
        |
        +--> live public snapshot + focused evidence + attributed rules/local samples
        +--> tool result returns to NEKO; the character writes the answer

hsbg.cards public API --> fixed-origin background GET --> atomic cache --> observed-card fact lookup
```

## 日志与状态机

`PowerLogTailer` 以 100 ms 周期增量跟随最新 `Power.log`，处理轮换、截断和首次接入上限。首次恢复默认且最多读取末尾 64 MiB，并从窗口内最新的完整 `GameState CREATE_GAME` 边界开始，兼容 LF 与 CRLF；恢复字节只在本机逐行解析，不会进入模型请求。`PowerLogParser` 解析实体和 tag 变化，`CompanionMonitor` 是状态唯一写入者；UI、工具和统计只取得不可变快照。LLM 工具对监控锁使用 50 ms 有界读取：大日志初始化尚未提交完整快照时立即 fail-closed 为 `state_refresh_in_progress`，不会等待到宿主 5 秒超时，也不会退回上一代来源。Hosted UI 打开时每 500 ms 串行拉取一次不可变状态，刷新失败不会覆盖用户尚未保存的草稿。

每次日志换源、读取器重建或停止后重启都会进入新的 source generation，并清空上一代的行/事件时间。bootstrap 只恢复当前公开状态，不重放主动解说、终局事件或统计；日志超过实时窗口或活动对局切入旁观后，查询会 fail-closed，只有同一代来源重新出现新鲜的活动对局数据才恢复。工具、Agent 和定向兜底使用同一套新鲜度判定，并以不可变 `GameSnapshot` 的实际变化时间为准；无关日志增长只更新 `last_line_at`，不会给旧商店或旧战团续命。

实时链路按日志职责合并而不是二选一：`PowerTaskList.DebugPrintPower` 是动态实体、tag 和 block 的权威实时流；`GameState.DebugPrintGame` 提供模式元数据；`GameState.DebugPrintPower` 只提供最早的新局边界、受限静态实体补全和 `STATE=COMPLETE`/终局 `PLAYSTATE`。新局静态包先进入隔离暂存区，直到 PowerTaskList 确认 `CREATE_GAME` 后才提交；进行中的静态补全只能填空，不能覆盖 PowerTaskList 已观察字段，也不能恢复被 `HIDE_ENTITY` 撤销的可见性。

普通对战中的 `CURRENT_PLAYER`、`RESOURCES` 等 tag 可能用临时玩家显示名引用 entity。解析器只在进程内用随机密钥生成摘要并映射到 `PlayerID`；原始显示名不会写入 Entity、快照、日志、Store 或模型上下文。PowerTaskList 中一次性的 `PLAYSTATE` 别名不会阻塞后续持续匿名引用；只有已确认本地 controller、恰好两名注册玩家且标签属于严格玩家白名单时，未知别名才会保守绑定到唯一对手。`TURN` 保留为原始行动回合，用户口语中的完整轮次为 `(TURN+1)//2`。换牌阶段可能已经出现初始 `TURN=1` 和首手玩家标记，因此在双方 `MULLIGAN_STATE=DONE` 或 `STEP=MAIN_READY` 前，公开轮次固定为 `0`、行动方为 `unknown`；完成边沿会立即补发首回合状态，不等待下一次 `TURN`。行动方的 `CURRENT_PLAYER=0` 边沿会先清空旧值，再由下一方的正边沿重新建立，工具不会在切换空窗沿用上一方。同一轮询批次中若回合边沿早于 `RESOURCES`，`turn_started` 会在批次结束时合并同一局、同一回合、同一行动方的最终法力快照。普通对战中行动方明确后，非行动方的当前可用法力固定为 `0`，不把上一回合的 `RESOURCES_USED` 余量继续当作当前法力；双方 `mana_max` 仍以日志观测为准。

战棋实现不假设本地 `PlayerID=1`。Bob 通过 `BACON_DUMMY_PLAYER` 识别；大厅由带 `PLAYER_ID` 的英雄实体组成；PowerTaskList 全局 GameEntity 的 `TURN` 是阶段强锚点，奇数为招募、偶数为战斗，单排/双排状态 tag 与 `CURRENT_PLAYER` 只在缺少全局 `TURN` 的旧日志中回退使用；最终名次来自本地英雄的 `PLAYER_LEADERBOARD_PLACE`。

对手关系按阶段拆成 `next`、`current` 和 `last`，本地玩家与 Bob 永远不能成为对手；战斗开始时锁定本轮对手，结束后再转为带回合号的上轮对手。对手战团是战斗中看到的公开信息，快照始终携带 `last_seen_round`、`observed_in_combat` 和 `observed_round`，禁止把历史阵容描述成当前阵容。战斗开始时隔离上一轮 Bob 商店，战斗结束时也隔离 Bob 控制器下的战斗镜像，直到招募日志明确刷新对应实体后才重新进入当前商店。只有实体出现明确的 `ATTACKING/DEFENDING` 战斗标记后，才在每场公开战斗开始时冻结首次确认的阵容。插件按对手保留最近一次观察，同一场战斗中产生的召唤物、变形或死亡不会改写这份记录。记录保留随从 CardID、名称、攻血、星级和站位，供后续陪伴回忆和规则事实查询。缺失 `CARDTYPE` 的实体必须额外具备合法站位与攻血或费用证据；符合证据的商店/手牌会以 `card_type=null` 保留，交给带来源目录补规则，内部效果实体不能仅凭 CardID 进入公开区域。`snapshot()` 与 `to_public_dict()` 必须保持纯读，UI 刷新或工具查询频率不能改变解析结果。

英雄选择快照只收集 `Power.log` 明确归属于本地 controller、未隐藏、未锁定且带可选/皮肤标记的英雄。候选持续保留到本地玩家明确出现 `MULLIGAN_STATE=DONE`；选择完成标记在一局内单调，迟到的 INPUT/DEALING 镜像不会重新打开候选，普通招募阶段信号也不会提前清空。它不根据卡池、远端玩家或缺失日志补猜候选；选择完成后的我方英雄仍由大厅实体识别。

普通对战使用独立 `ConstructedSnapshot`。它包含对局类型、模式变体、双方英雄与公开资源、我方当前可见手牌、公开场面、英雄技能、武器、地标、过载、疲劳、最近公开出牌及本地 Choice 选项。动态费用只在实体具有实时 `COST` 时提供；缺失时为 `null`，不以静态卡库猜测。对手手牌只公开数量和确实揭示且尚未撤销的 identity；未揭示手牌、奥秘身份、牌序和完整合法操作集合始终不可用。Choice 流按本地玩家摘要映射过滤，对手选项不进入公开快照。

## 陪伴调度

`CommentaryArbiter` 只仲裁 LLM 请求，不生成任何可见文本。主动事件必须同时满足：

- 主动解说已开启；
- 公开局势共享已开启；
- 当前不是旁观模式；
- 事件达到最低优先级；
- 普通或关键冷却结束；
- 用户最近 30 秒没有聊天，除非事件优先级达到 9。

主动解说请求包含事件事实、适合情绪陪伴的精简快照和 `emotion_cue`。它不承担随问随查；查询始终在调用时从同一权威快照构建聚焦结果。主动提示要求保持当前角色人设、只依据已给事实、避免机械报字段，并限制主动发言为一句。低血量、三连、升本、战斗和结算只决定情绪方向，不决定角色具体措辞。

## 统一实时查询

`hearthstone_live_state` 是唯一模型可见的同轮只读工具。每次询问回合、行动方、公开场面、具体手牌、Choice、商店、战团、经济或决策都应重新查询，不能依赖主动短评或更早聊天历史。工具必须收到用户原始 `query` 以便和兜底链路关联；`mode`、`focus`、`topic` 和 `opponent_relation` 可省略，插件会从快照和问题自动选择。宿主若违反 schema 传入空问题，工具 fail-closed，不读取局势，由定向 memory 兜底处理。

普通对战支持 `overview/board/hand/opponent/choice/strategy`，酒馆额外支持 `shop/economy`；对手查询可声明 `current/next/last`。插件先在本机建立完整事实和 capability，再只把所问视图、对应 evidence gate 与相关卡牌规则编码为 `hearthstone_compact_v1`。聚焦 JSON 限制为 4096 bytes，超限时明确标记 `truncated` 并进一步收敛。`complete_legal_actions` 固定为 `false`，防止把局势分析说成完整求解器结论。

`query_hearthstone_live_state` 是唯一 Agent 入口，复用同一快照构建和紧凑 serializer。Agent 属于主回答后的独立路由，因此不能代替同轮工具，只作为模型未调用工具时的第二路径。入口和工具会在能精确匹配最近查询时认领 query ledger，并取消尚未发送的兜底。

`live_query_watch` 每秒通过官方 `bus.memory.get(bucket_id="default")` 有界读取近期用户话语。它只接受明确的炉石查询，按记录中的实际 `lanlan` 路由；配置固定 `target_lanlan` 时过滤其他角色。未被工具或 Agent 认领的查询才通过 `visibility=[] + ai_behavior="respond"` 发送包含当刻聚焦快照的定向兜底。SDK 回执只确认本地提交，不证明模型已生成回答，因此验收必须检查最终角色回复。

公开 SDK 的 push receipt 只确认提交，不确认宿主已消费、生成或播放，也没有返回最终角色文本的正式回调。因此独立浮层不能承接自动角色台词，也不会自动显示解析器事件；它只接受用户显式触发的诊断文本。

## 酒馆建议数据

统一工具在酒馆模式调用内部只读构建器，返回：

- 当前战棋公开局势；
- `hsbg.cards` 当前卡池摘要，以及按 `card_id` 去重的当前商店/手牌/战团/英雄和上次观察对手战团规则事实；
- 带来源、补丁和验证时间的赛季规则；
- 当前赛季的本机聚合统计与样本量；
- 全局 meta 数据的可用状态和禁止编造契约。

数据共享开启后，同轮只读工具即可使用，不要求同时开启主动解说或配置固定角色。这让用户可以安静游玩，只在普通聊天提问时获得角色回答；工具是完整动态字段、规则补充和 evidence gate 的权威路径。

`current_strategy` 在新鲜的英雄选择阶段可以依据实际观测候选和带来源的英雄规则回答“这几个英雄选哪个”，但必须说明没有授权的全局胜率。只有新鲜招募阶段和当前完整商店才允许定性比较“哪张更值得考虑”；精确可负担性与购买顺序还要检查当前金币和当前商店所有卡牌的实际费用，冻结、刷新和升本则继续检查各自经济 capability。战斗阶段没有当前商店决策，缓存状态也只能用于说明最近观察，均不得输出成可执行的即时购买建议。赛季规则、本机英雄聚合表现和对局复盘使用各自独立的可用性条件，不能用其他历史样本冒充当前英雄或刚结束的一局。

酒馆卡牌快照保存日志实际观测的 `card_type`、`current_cost`、`premium`、当前位置、冻结和当前关键词；刷新/升本费用优先读取对应 `GAME_MODE_BUTTON_SLOT` 按钮实体。客户端不会在每个招募阶段重发恒定按钮费用，因此当前可见 `PLAY` 按钮只要同一实体在本轮本阶段有明确 tag observation，或完成了当前 GameState 基线，就可沿用其持久 `COST`；该规则不放宽玩家经济 tag、隐藏按钮或已移出场按钮。完整实体包中的 boolean 标签缺失按默认 false，包尚未收尾时保持 `null`，防止截断包伪装成完整状态，也防止旧冻结/金色/关键词续命。商店、手牌、战团、经济和 Choice 分别携带完整度、revision、回合、阶段与观测时间；金币、刷新费用和升本费用还分别保存自己的 observation。金币以当前 `RESOURCES` 建立基线；历史 `RESOURCES_USED/TEMP_RESOURCES` 会过期并按未发生处理，只有同回合同阶段重报后才加入当前值。未观测或已过期的动态值保持 `null`，不使用公共目录或默认规则补猜。`CHANGE_ENTITY` 真正换 CardID 时先撤销旧类型、费用、攻血、星级、金色和关键词，直到新身份重新提供证据。购买拆为 `shop_card_priority_advice`、`purchase_affordability` 和 `specific_purchase_advice`：费用缺失不污染已经具备完整实时商店与规则证据的定性选牌，但会让可负担性与精确购买顺序降为 `partial`。工具结果最前面的 `current_recruit_decision` 按卡标记 `known_affordable`、`known_unaffordable` 或 `unknown_cost_may_be_zero`，并将整店可负担性保持为 `unknown`；`decision_guardrails` 再提供完整证据边界，禁止模型因金币为 0 就把未知费用卡牌判为买不起。升本可负担性、升本策略、刷新、Choice 与站位也有独立 capability，角色必须按被问事项检查对应状态，不能因一个子能力不可用而覆盖另一个已可用能力。

唯一 `@llm_tool` 由公开 SDK 在插件构造时自动注册并排队提交给宿主，可在生成首答的同一轮调用。`plugin.toml` 使用 `passive=false`，让用户插件 Agent 在模型未选择工具时仍能发现唯一查询入口。设置、监听、浮层和清空统计入口继续以 `metadata.agent_auto=false` 隐藏。分段 `read` 使用同键覆盖，语义变化立即更新、完全相同的状态每 30 秒续租；目标只能来自显式配置或官方近期话语记录，无法解析目标时 fail-closed。三条查询链路互补，不假设模型必然调用某个工具，也不把 Agent 的后置结果冒充首答工具结果。

官方工具文档明确说明 `/api/tools` 注册只存在于当前角色的 `LLMSessionManager` 内，主服务重启、首启竞态或会话管理器重建后不会由 `@llm_tool` 自动回灌。插件因此使用官方 `@timer_interval` 在独立定时线程里读取公开的 `GET /api/tools`：只有某个当前角色缺少本插件的远程工具，或 source、loopback callback、remote 标志不一致时，才通过公开 `unregister_llm_tool()` / `register_llm_tool()` 原子持锁恢复并再次确认；健康注册不做任何变更。官方 SDK 会拒绝同名重复注册，因此不能省略本地注销步骤。失败使用有界退避且只记录脱敏错误码，缓存的本地 schema 允许后续定时器再次恢复。这能自动收敛运行期注册丢失，但定时检查仍存在最多一个检查间隔的窗口，不能冒充宿主提供的逐 turn 前同步保证。

卡牌目录不做流派评分、胜率排序或本地推荐。远端 `rules_text` 经过 HTML 清洗和长度限制，仍被标记为不可信参考数据；角色必须核对 provider、patch、checked_at、stale 和覆盖率。常规 `*_G` 金卡会映射到金色规则，少量旧式或不规则 CardID 会进入 `missing_ids`，角色不得猜测缺失元数据。目录不可用不会令实时局势整体不可用。

## 持久化与线程

单局快照、玩家名和完整提示上下文不持久化。`plugin.toml` 只声明安装默认值，N.E.K.O 原生配置服务是唯一运行时设置来源；用户显式填写的 `log_path` 会随设置持久化，自动发现的实际日志路径只属于运行状态。Plugin Store 只用于酒馆聚合统计，不读取或保存插件设置。

Plugin Store 长期只保存赛季/模式/英雄维度的聚合计数。N.E.K.O `startup()` 所在 asyncio loop 不是长期后台 loop，因此 `AsyncStoreWriter` 拥有自己的 event-loop 线程，并用 `run_coroutine_threadsafe()` 串行提交这些统计写入。插件停机时只停止自己拥有的 writer 并等待提交，不关闭或销毁 SDK Store；Store client 和生命周期由 N.E.K.O 宿主管理。

统计初始读取只有明确成功后才开放后续写入。Store 返回 `Err`、抛异常或已有数据校验失败时，核心日志监听和陪伴仍会启动，但统计保持降级且禁止记录、清空或覆盖未知的历史值，等待插件重启后重新加载。

`BattlegroundsCardCatalog` 使用独立后台线程，固定访问 `https://hsbg.cards/api/v1`，限制响应、条目和字段长度，并以临时文件加 `os.replace()` 写入 N.E.K.O `data_path()`。网络、JSON 或磁盘失败时保留已有快照并公开错误码；Power.log 监听线程从不联网。

## 主要配置

| 配置项 | 默认值 | 作用 |
| --- | --- | --- |
| `monitor_on_start` | `true` | 启动后监听日志 |
| `initial_read_max_bytes` | `67108864` | 首次本地恢复最多读取 64 MiB |
| `llm_data_consent` | `true` | 允许工具、Agent 和明确问题兜底读取过滤后的玩家可见局势；用户可显式关闭 |
| `llm_commentary_enabled` | `false` | 允许角色主动解说 |
| `llm_min_priority` | `5` | 主动事件最低优先级 |
| `llm_cooldown_seconds` | `25` | 普通主动解说冷却 |
| `llm_critical_cooldown_seconds` | `8` | 关键主动解说冷却 |
| `user_chat_quiet_window_seconds` | `30` | 用户聊天后的安静时间 |
| `target_lanlan` | 空 | 空时查询兜底使用 memory 记录的实际角色；非空时只处理和定向该角色，主动事件也使用该显式目标 |
| `card_catalog_network_enabled` | `true` | 每日更新公共卡牌目录；关闭后只读旧缓存 |
| `card_catalog_refresh_hours` | `24` | 公共卡牌目录刷新间隔（6-168 小时） |
| `overlay_auto_start` | `false` | 诊断浮层默认不自动启动 |

## 扩展要求

- 新字段先证明其公开性，再进入 `to_public_dict()`；
- 新情绪规则只能输出结构化 cue，不能输出角色台词；
- 不加入操作建议模板、自动操作、注入、抓包或隐藏信息推断；
- 远程统计必须有明确授权、来源、版本和失败降级，不能抓取私有网页；
- 新日志协议必须有脱敏回放测试。
