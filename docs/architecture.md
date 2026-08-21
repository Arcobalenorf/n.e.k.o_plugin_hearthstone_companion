# 架构说明

## 产品原则

N.E.K.O 的核心是关系与陪伴。插件负责理解游戏现场，不负责用本地模板扮演角色。实现遵循四条边界：

1. 本地层只做公开事实提炼、情绪信号、节奏仲裁和隐私过滤；
2. 所有主动可见台词由当前 N.E.K.O 角色通过 `ai_behavior="respond"` 生成；
3. 只有显式固定目标角色时才用隐藏 `read` 建立稳定场景，并在结束时用相同 `coalesce_key` 恢复；
4. 面板和独立浮层只承担透明诊断，不参与自动陪伴输出。

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
        +--> fixed target scene enter/leave --> [] + read --> context / restore
        +--> active role per message --> inline context + [] + respond

Constructed question --> hearthstone_current_state --> fresh player-visible snapshot --> NEKO answer

Battlegrounds question --> hearthstone_battlegrounds_advice
        |
        +--> live public snapshot + attributed card facts + official rules + local sample stats
        +--> tool result returns to NEKO; the character writes the answer

hsbg.cards public API --> fixed-origin background GET --> atomic cache --> observed-card fact lookup
```

## 日志与状态机

`PowerLogTailer` 以 100 ms 周期增量跟随最新 `Power.log`，处理轮换、截断和首次接入上限。首次恢复默认且最多读取末尾 64 MiB，并从窗口内最新的完整 `GameState CREATE_GAME` 边界开始，兼容 LF 与 CRLF；恢复字节只在本机逐行解析，不会进入模型请求。`PowerLogParser` 解析实体和 tag 变化，`CompanionMonitor` 是状态唯一写入者；UI、工具和统计只取得不可变快照。Hosted UI 打开时每 500 ms 串行拉取一次不可变状态，刷新失败不会覆盖用户尚未保存的草稿。

每次日志换源、读取器重建或停止后重启都会进入新的 source generation，并清空上一代的行/事件时间。bootstrap 只恢复当前公开状态，不重放主动解说、终局事件或统计；日志超过实时窗口后会退出角色场景，只有同一代来源重新出现新数据才恢复。工具可用性与场景 context 使用同一套新鲜度判定，并以不可变 `GameSnapshot` 的实际变化时间为准；无关日志增长只更新 `last_line_at`，不会给旧商店或旧战团续命。

实时链路按日志职责合并而不是二选一：`PowerTaskList.DebugPrintPower` 是动态实体、tag 和 block 的权威实时流；`GameState.DebugPrintGame` 提供模式元数据；`GameState.DebugPrintPower` 只提供最早的新局边界、受限静态实体补全和 `STATE=COMPLETE`/终局 `PLAYSTATE`。新局静态包先进入隔离暂存区，直到 PowerTaskList 确认 `CREATE_GAME` 后才提交；进行中的静态补全只能填空，不能覆盖 PowerTaskList 已观察字段，也不能恢复被 `HIDE_ENTITY` 撤销的可见性。

普通对战中的 `CURRENT_PLAYER`、`RESOURCES` 等 tag 可能用临时玩家显示名引用 entity。解析器只在进程内用随机密钥生成摘要并映射到 `PlayerID`；原始显示名不会写入 Entity、快照、日志、Store 或模型上下文。`TURN` 保留为原始行动回合，用户口语中的完整轮次为 `(TURN+1)//2`。换牌阶段可能已经出现初始 `TURN=1` 和首手玩家标记，因此在双方 `MULLIGAN_STATE=DONE` 或 `STEP=MAIN_READY` 前，公开轮次固定为 `0`、行动方为 `unknown`；完成边沿会立即补发首回合状态，不等待下一次 `TURN`。行动方的 `CURRENT_PLAYER=0` 边沿会先清空旧值，再由下一方的正边沿重新建立，工具不会在切换空窗沿用上一方。

战棋实现不假设本地 `PlayerID=1`。Bob 通过 `BACON_DUMMY_PLAYER` 识别；大厅由带 `PLAYER_ID` 的英雄实体组成；单排和双排分别使用可验证的战斗状态 tag；最终名次来自本地英雄的 `PLAYER_LEADERBOARD_PLACE`。

对手关系按阶段拆成 `next`、`current` 和 `last`，本地玩家与 Bob 永远不能成为对手；战斗开始时锁定本轮对手，结束后再转为带回合号的上轮对手。对手战团是战斗中看到的公开信息，快照始终携带 `last_seen_round`、`observed_in_combat` 和 `observed_round`，禁止把历史阵容描述成当前阵容。战斗开始时隔离上一轮 Bob 商店，战斗结束时也隔离 Bob 控制器下的战斗镜像，直到招募日志明确刷新对应实体后才重新进入当前商店。只有实体出现明确的 `ATTACKING/DEFENDING` 战斗标记后，才在每场公开战斗开始时冻结首次确认的阵容。插件按对手保留最近一次观察，同一场战斗中产生的召唤物、变形或死亡不会改写这份记录。记录保留随从 CardID、名称、攻血、星级和站位，供后续陪伴回忆和规则事实查询。缺失 `CARDTYPE` 的实体必须额外具备合法站位或攻血联合证据，内部效果实体不能仅凭 CardID 进入公开战团。`snapshot()` 与 `to_public_dict()` 必须保持纯读，UI 刷新或工具查询频率不能改变解析结果。

英雄选择快照只收集 `Power.log` 明确归属于本地 controller、未隐藏、未锁定且带可选/皮肤标记的英雄。候选持续保留到本地玩家明确出现 `MULLIGAN_STATE=DONE`；选择完成标记在一局内单调，迟到的 INPUT/DEALING 镜像不会重新打开候选，普通招募阶段信号也不会提前清空。它不根据卡池、远端玩家或缺失日志补猜候选；选择完成后的我方英雄仍由大厅实体识别。

普通对战使用独立 `ConstructedSnapshot`。它包含对局类型、模式变体、双方英雄与公开资源、我方当前可见手牌、公开场面、英雄技能、武器、地标、过载、疲劳、最近公开出牌及本地 Choice 选项。动态费用只在实体具有实时 `COST` 时提供；缺失时为 `null`，不以静态卡库猜测。对手手牌只公开数量和确实揭示且尚未撤销的 identity；未揭示手牌、奥秘身份、牌序和完整合法操作集合始终不可用。Choice 流按本地玩家摘要映射过滤，对手选项不进入公开快照。

## 陪伴调度

`CommentaryArbiter` 只仲裁 LLM 请求，不生成任何可见文本。主动事件必须同时满足：

- 主动解说已开启；
- 公开局势共享已同意；
- 当前不是旁观模式；
- 事件达到最低优先级；
- 普通或关键冷却结束；
- 用户最近 30 秒没有聊天，除非事件优先级达到 9。

单次请求包含事件事实、适合情绪陪伴的精简快照和 `emotion_cue`。普通对战主动短评只携带手牌数量，不持续发送具体手牌或 Choice identity；用户主动提问时才由工具按需提供完整玩家可见状态。提示要求保持当前角色人设、只依据已给事实、避免机械报字段，并限制主动发言为一句。低血量、三连、升本、战斗和结算只决定情绪方向，不决定角色具体措辞。

## 普通对战状态工具

`hearthstone_current_state` 是普通对战当前事实与决策问题的只读入口。角色回答回合、行动方、资源、当前手牌/场面、应打哪张牌或当前选择项前必须重新调用，不能依赖之前的主动短评。结果携带逐项 capability：是否读到回合、行动方、我方可见手牌、完整手牌 identity 和 Choice；`complete_legal_actions` 固定为 `false`，防止把局势分析说成完整求解器结论。酒馆策略仍必须路由到专用工具。

显式配置非空 `target_lanlan` 时，场景进入发送 `visibility=[] + ai_behavior="read"`；关键事件发送定向的 `visibility=[] + ai_behavior="respond"`；场景结束、停止监听、关闭插件、撤销同意或更换显式目标时发送恢复 `read`。目标为空时不解析或冻结宿主的活动角色 ID、不注入跨消息场景，也不发送 `target_lanlan` 或 `coalesce_key`；每条 `respond` 都内嵌完整陪伴约束，由宿主逐消息选择当时的活动角色。

公开 SDK 的 push receipt 只确认提交，不确认宿主已消费、生成或播放，也没有返回最终角色文本的正式回调。因此独立浮层不能承接自动角色台词，也不会自动显示解析器事件；它只接受用户显式触发的诊断文本。

## 酒馆建议工具

`hearthstone_battlegrounds_advice` 是只读 LLM tool，不直接生成台词。它返回：

- 当前战棋公开局势；
- `hsbg.cards` 当前卡池摘要，以及按 `card_id` 去重的当前商店/手牌/战团/英雄和上次观察对手战团规则事实；
- 带来源、补丁和验证时间的赛季规则；
- 当前赛季的本机聚合统计与样本量；
- 全局 meta 数据的可用状态和禁止编造契约。

数据同意开启后工具即可使用，不要求同时开启主动解说。这让用户可以安静游玩，只在主动提问时获得角色回答。

`current_strategy` 在新鲜的英雄选择阶段可以依据实际观测候选和带来源的英雄规则回答“这几个英雄选哪个”，但必须说明没有授权的全局胜率。只有新鲜招募阶段才允许把当前商店用于具体购买、刷新、冻结或升本建议；战斗阶段没有当前商店决策，缓存状态也只能用于说明最近观察，均不得输出成可执行的即时购买建议。赛季规则、本机英雄聚合表现和对局复盘使用各自独立的可用性条件，不能用其他历史样本冒充当前英雄或刚结束的一局。

酒馆卡牌快照保存日志实际观测的 `card_type`、`current_cost`、`premium`、当前位置和当前关键词；刷新/升本费用优先读取对应 `GAME_MODE_BUTTON_SLOT` 按钮实体。商店、手牌、战团、经济和 Choice 分别携带完整度、revision、回合、阶段与观测时间。未观测的动态值保持 `null`，不使用公共目录或默认规则补猜。购买、升本可负担性、升本策略、刷新、Choice 与站位都有独立 capability；只有 `available=true` 才能给对应具体建议，`partial` 只能陈述事实、缺口和条件式思路。

两个 `@llm_tool` 仍由 SDK 在插件构造时自动注册。插件启动后另起有界后台任务，按官方 Tool Calling 文档定期读取 loopback `GET /api/tools`；若首启竞态或 main server 重启导致任一角色缺少本插件工具，则通过公开的 `unregister_llm_tool` / `register_llm_tool` 重新发出注册。健康注册不会重复写入，shutdown 会先取消恢复任务。

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
| `llm_data_consent` | `false` | 允许工具/上下文读取过滤后的玩家可见局势 |
| `llm_commentary_enabled` | `false` | 允许角色主动解说 |
| `llm_min_priority` | `5` | 主动事件最低优先级 |
| `llm_cooldown_seconds` | `25` | 普通主动解说冷却 |
| `llm_critical_cooldown_seconds` | `8` | 关键主动解说冷却 |
| `user_chat_quiet_window_seconds` | `30` | 用户聊天后的安静时间 |
| `target_lanlan` | 空 | 空时不发送角色 ID，由宿主逐消息路由；非空时固定目标并管理场景 context |
| `card_catalog_network_enabled` | `true` | 每日更新公共卡牌目录；关闭后只读旧缓存 |
| `card_catalog_refresh_hours` | `24` | 公共卡牌目录刷新间隔（6-168 小时） |
| `overlay_auto_start` | `false` | 诊断浮层默认不自动启动 |

## 扩展要求

- 新字段先证明其公开性，再进入 `to_public_dict()`；
- 新情绪规则只能输出结构化 cue，不能输出角色台词；
- 不加入操作建议模板、自动操作、注入、抓包或隐藏信息推断；
- 远程统计必须有明确授权、来源、版本和失败降级，不能抓取私有网页；
- 新日志协议必须有脱敏回放测试。
