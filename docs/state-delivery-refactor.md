# 实时状态交付重构

## 目标

本次重构解决的不是单一解析字段，而是从游戏日志到当前 N.E.K.O 角色的完整交付链路：

```text
Power.log -> immutable GameSnapshot -> semantic state publisher
                                         |-> read context
GameEvent -> commentary arbiter ----------|-> sparse respond commentary
GameSnapshot -----------------------------|-> same-turn @llm_tool callback
                                         `-> Agent entry
```

验收必须分别证明：

1. 真实日志能恢复正确的普通对战和酒馆公开状态；
2. 状态变化会产生一次新的语义发布，纯时间戳或 revision 变化不会重复发布；
3. 显式配置目标时发布会定向到该角色；未配置目标时使用宿主限定的单活动会话路由，并以 1 秒租约重试而不广播到多个会话；
4. 模型调用工具或 Agent 入口时能取得同一权威快照；工具 callback 缺少可信角色、会话和 turn 身份，只能把结果返回原调用，插件不得另发 tool-result `respond`；
5. 主动陪伴只对稀疏事件开口，并且每次 `respond` 自带所需事实；
6. 停止、换源、撤销共享或对局过期后，旧上下文会被同键失效消息覆盖。

## 边界与原则

- 插件保持独立项目，不修改 N.E.K.O 宿主来适配炉石。
- 只使用公开 Plugin SDK：`@llm_tool`、`@plugin_entry`、`push_message`、生命周期和配置服务。
- 模型工具是增强路径，不是基础读取的唯一条件。模型是否选择工具不可控，数据链路是否可达必须可控。
- 原始日志、玩家身份、本机路径和隐藏信息不得进入模型上下文、Store 或测试报告。
- 公共卡牌目录只补规则事实，不能替代日志中的当前费用、关键词、金色状态或完整度证据。
- `push_message` 回执只证明 SDK 接受提交；拒绝时不得推进本地已交付游标。

## 组件设计

### 1. 权威快照

`CompanionMonitor` 继续是解析状态的唯一写入者。工具、面板、发布器和主动事件只读取同一份不可变 `GameSnapshot`。日志换源、bootstrap、旁观和新鲜度门禁仍由 monitor/parser 层负责。

### 2. 语义状态发布器

发布器为当前快照构建规范化表示并计算 fingerprint：

- 保留模式、阶段、局号、回合、行动方、血量、场面、手牌、Choice、商店、战团、经济和动态卡牌字段；
- 递归忽略 `observed_at`、`revision` 等仅描述采集过程的字段；
- 卡牌和区域仍按稳定站位/键排序；
- fingerprint 变化时立即发布；完全相同时只在续租间隔到期后重发；
- 每个分段使用稳定 `coalesce_key`，目标或分段集合变化时覆盖旧分段；
- 分段按同一 revision 最终一致地覆盖；全部接受后才提交完整 delivery cursor。中途拒绝时立即用 tombstone 清理已提交分段，清理不完整则记录 partial cursor 并在下一次刷新先清理、再重试。
- 商店、手牌、战团和逐项经济字段只有在区域完整、回合/阶段匹配且 observation 仍新鲜时才进入被动上下文；酒馆等级作为同局持久状态在新局/换源时重置，不依赖刷新和升本费用同时完整。工具查询复用同一证据语义。
- Power.log 没有酒馆商店刷新的原子结束标记。新变化的招募阶段商店先保留已观察卡牌但标记为不完整；同一商店签名经过 500ms trailing quiet window 且后续 poll 仍未变化后才恢复完整，避免分批日志中的首张卡被误认为完整商店。这个窗口是保守 settle 策略，不宣称日志提供了原子事务证明。

这会取代“每秒比较包含时间字段的完整快照并全部重发”的行为。

### 3. 会话路由与查询契约

背景 `read`、生命周期和中局主动解说只从显式 `target_lanlan` 取得定向目标。配置为空时省略 `target_lanlan` 并使用 `active-session` coalesce key；宿主只在恰好一个在线会话时路由，零个或多个在线会话直接丢弃。插件用 1 秒未解析租约重试背景状态，因为 SDK `submitted` 回执不等于宿主已消费。生命周期和主动解说可在自身 TTL 内重试同一 targetless 请求，但不会猜测角色或导入宿主内部配置管理器。

普通问答只有两条执行入口：

- `@llm_tool` 在原用户 turn 内返回当前快照结果；公开 callback 只向插件传工具参数，不传可信 `lanlan_name`、conversation ID 或 turn ID；
- `query_hearthstone_live_state` Agent 入口复用同一查询服务，并通过 Agent 路由返回结果。

插件不读取近期用户话语，不维护角色级问题账本，也不在工具 callback 后创建独立主动 turn。原工具 continuation 是否形成可见回答由宿主管理；公开 SDK 没有可供插件验证的最终回答回执。

### 4. 生命周期与中局免打扰

开场/重连/结算回应是数据共享开启时的固有行为，没有独立开关；免打扰默认关闭，开启后只抑制中局主动陪伴。生命周期按来源 epoch、局数和阶段去重，不受中局事件冷却与聊天静默窗影响；bootstrap 只产生 resumed，不伪装为新局开始。现有事件优先级、普通/关键冷却和用户聊天静默窗在免打扰关闭时用于中局主动陪伴，同时保留以下交付约束：

- `respond` 必须包含事件事实及当刻必要局势，不能只引用之前的被动上下文；
- 配置目标时定向；未配置时只依赖宿主恰好一个在线会话的 targetless 路由；
- 同一路由目标使用固定 commentary `coalesce_key`；
- 过期事件在插件侧丢弃，提交成功后才推进 arbiter；
- 终局事件先携带最终快照提交，再覆盖实时状态上下文；targetless 无法路由或提交拒绝时可在内存短暂重试，但不会向多个会话广播。

### 5. 工具与兼容入口

模型工具收敛为无参数 `hearthstone_current_turn` 与仅含可选 `query` 的 `hearthstone_live_state`：前者在模型可见文本中只给出 `round`、行动方和阶段，原始 `action_turn` 仅留在内部 canonical 与诊断，后者在同一用户轮次提供完整的聚焦事实。插件内部先构建并校验浅层 canonical 契约，再按官方 callback 规则以 `{"output": <纯文本>, "is_error": false}` 返回同源模型契约；建议类文本还包含对应 capability、相关公共规则、购买判断和护栏，综合策略使用当前阶段多视图。正式 user-plugin-server 不会把内部 `_canonical` 或展开字段重复送给模型。工具和 Agent 入口复用同一个 4096-byte 模型序列化器，均从当前权威快照构建事实。工具注册健康检查只处理官方文档明确存在的 main-server registry 丢失问题，不参与状态发布。

## 失败策略

| 失败点 | 行为 |
| --- | --- |
| 日志未找到或过期 | 查询 fail-closed，覆盖旧上下文，不使用缓存建议 |
| 未配置目标 | 工具和 Agent 继续可用；被动快照、生命周期和主动解说只尝试宿主单在线会话的 targetless 路由 |
| `read` 提交被拒绝 | 不推进 fingerprint/时间游标，下次刷新重试 |
| 生命周期/主动 `respond` 提交被拒绝 | 不推进 arbiter，仍受事件 TTL 限制 |
| 工具 registry 丢失 | 按官方接口有界检查并重注册，不改宿主 |
| 角色切换 | 先覆盖旧目标上下文，再向新目标发布最新快照 |

## 测试矩阵

- 单元：fingerprint 稳定性、真实变化检测、目标路由、拒绝后重试、过期覆盖；
- 解析回放：用户指定的 Hearthstone `Logs` 目录中的真实普通对战和酒馆日志，只输出脱敏摘要；
- SDK：工具调用、`read/respond` payload、目标和 coalesce 行为；
- 后端：独立插件进程启动、完整进程树清理、工具 registry、标准 callback、Agent 和主动消息 targetless 路由；模型是否选择工具与最终措辞作为单独观测，不阻断插件质量门禁；
- 全量：pytest、compileall、ruff、官方 release check/build/inspect/verify。
