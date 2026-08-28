# 实时状态交付重构

## 目标

本次重构解决的不是单一解析字段，而是从游戏日志到当前 N.E.K.O 角色的完整交付链路：

```text
Power.log -> immutable GameSnapshot -> semantic state publisher
                                         |-> read context
Recent user utterance -> query coordinator|-> focused respond fallback
GameEvent -> commentary arbiter ----------|-> sparse respond commentary
                                         `-> hearthstone_live_state tool
```

验收必须分别证明：

1. 真实日志能恢复正确的普通对战和酒馆公开状态；
2. 状态变化会产生一次新的语义发布，纯时间戳或 revision 变化不会重复发布；
3. 发布会定向到实际提问或当前角色，不会每秒制造无目标消息；
4. 模型调用工具时能取得同一权威快照；模型未调用工具时，明确炉石问题仍会收到一次带聚焦事实的定向兜底；
5. 主动陪伴只对稀疏事件开口，并且每次 `respond` 自带所需事实；
6. 停止、换源、撤销共享或对局过期后，旧上下文会被同键失效消息覆盖。

## 边界与原则

- 插件保持独立项目，不修改 N.E.K.O 宿主来适配炉石。
- 只使用公开 Plugin SDK：`@llm_tool`、`push_message`、`ctx.bus.memory`、生命周期和配置服务。
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

### 3. 会话与查询协调器

协调器每秒从公开的 `ctx.bus.memory.get(bucket_id="default")` 读取一个很小的近期窗口，只在内存中保留：

- 话语时间；
- 角色名；
- 最多 240 字的归一化原问题及其 fingerprint；
- 是否已由 tool、Agent 或 fallback 认领。

这些内容不持久化，默认 90 秒后从内存账本清除；共享关闭或插件停止时立即清空。

角色解析来源为：显式 `target_lanlan`、Agent 调用携带的公开 `_ctx`，以及最近 user-context 记录。背景状态只使用显式配置或已观察到的 user-context 目标；无法解析唯一目标时 fail-closed，不发送无目标消息，也不得导入宿主内部配置管理器。

明确炉石问题经本地分类后进入短延迟认领窗口：

- `hearthstone_live_state` 同轮工具或兼容 Agent 先执行时，标记 query 已认领；
- 否则协调器从同一权威快照构建聚焦结果，将原问题和事实放进同一条定向 `respond`；
- 同一角色、同一话语只允许一个回答路径主动提交；
- fallback 以原始 user-context 记录中的角色为权威；生命周期、日志源或显式目标变化时清空查询账本；
- user-context 不可用时不影响日志监听、工具或已知目标的主动陪伴。

### 4. 生命周期与中局免打扰

开场/重连/结算回应是数据共享开启时的固有行为，没有独立开关；免打扰默认关闭，开启后只抑制中局主动陪伴。生命周期按来源 epoch、局数和阶段去重，不受中局事件冷却与聊天静默窗影响；bootstrap 只产生 resumed，不伪装为新局开始。现有事件优先级、普通/关键冷却和用户聊天静默窗在免打扰关闭时用于中局主动陪伴，同时保留以下交付约束：

- `respond` 必须包含事件事实及当刻必要局势，不能只引用之前的被动上下文；
- 同一角色使用固定 commentary `coalesce_key`；
- 过期事件在插件侧丢弃，提交成功后才推进 arbiter；
- 终局事件先携带最终快照提交，再覆盖实时状态上下文；无目标或提交拒绝时可在内存短暂重试，但不会广播。

### 5. 工具与兼容入口

保留唯一模型工具 `hearthstone_live_state`，因为它能在同一用户轮次提供更完整的聚焦事实。兼容 Agent 入口复用同一查询服务和认领账本，但不再作为基础读取前提。工具注册健康检查只处理官方文档明确存在的 main-server registry 丢失问题，不参与状态发布。

## 失败策略

| 失败点 | 行为 |
| --- | --- |
| 日志未找到或过期 | 查询 fail-closed，覆盖旧上下文，不使用缓存建议 |
| user-context 暂时不可用 | 保留上次已知角色；工具继续可用；不重复制造无目标消息 |
| `read` 提交被拒绝 | 不推进 fingerprint/时间游标，下次刷新重试 |
| `respond` 提交被拒绝 | 不认领 query、不推进 arbiter，仍受查询 TTL/事件 TTL 限制 |
| 工具 registry 丢失 | 按官方接口有界检查并重注册，不改宿主 |
| 角色切换 | 先覆盖旧目标上下文，再向新目标发布最新快照 |

## 测试矩阵

- 单元：fingerprint 稳定性、真实变化检测、目标优先级、query 认领、拒绝后重试、过期覆盖；
- 解析回放：用户指定的 Hearthstone `Logs` 目录中的真实普通对战和酒馆日志，只输出脱敏摘要；
- SDK：工具调用、`read/respond` payload、目标和 coalesce 行为；
- 后端：安装包/独立插件进程启动、工具 registry、user-context 查询和最终主动消息路由；
- 全量：pytest、compileall、ruff、官方 release check/build/inspect/verify。
