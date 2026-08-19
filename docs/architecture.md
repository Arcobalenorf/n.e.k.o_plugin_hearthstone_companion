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

User question --> hearthstone_battlegrounds_advice
        |
        +--> live public snapshot + attributed card facts + official rules + local sample stats
        +--> tool result returns to NEKO; the character writes the answer

hsbg.cards public API --> fixed-origin background GET --> atomic cache --> observed-card fact lookup
```

## 日志与状态机

`PowerLogTailer` 增量跟随最新 `Power.log`，处理轮换、截断和首次接入上限。首次恢复默认且最多读取末尾 64 MiB；恢复字节只在本机逐行解析，不会进入模型请求。`PowerLogParser` 解析实体和 tag 变化，`CompanionMonitor` 是状态唯一写入者；UI、工具和统计只取得不可变快照。

战棋实现不假设本地 `PlayerID=1`。Bob 通过 `BACON_DUMMY_PLAYER` 识别；大厅由带 `PLAYER_ID` 的英雄实体组成；单排和双排分别使用可验证的战斗状态 tag；最终名次来自本地英雄的 `PLAYER_LEADERBOARD_PLACE`。

对手战团是过去战斗中看到的公开信息，快照始终携带 `last_seen_round` 和 `is_last_observed`，禁止把它描述成当前阵容。

## 陪伴调度

`CommentaryArbiter` 只仲裁 LLM 请求，不生成任何可见文本。主动事件必须同时满足：

- 主动解说已开启；
- 公开局势共享已同意；
- 当前不是旁观模式；
- 事件达到最低优先级；
- 普通或关键冷却结束；
- 用户最近 30 秒没有聊天，除非事件优先级达到 9。

单次请求包含事件事实、公开快照和 `emotion_cue`。提示要求保持当前角色人设、只依据公开事实、避免机械报字段，并限制主动发言为一句。低血量、三连、升本、战斗和结算只决定情绪方向，不决定角色具体措辞。

显式配置非空 `target_lanlan` 时，场景进入发送 `visibility=[] + ai_behavior="read"`；关键事件发送定向的 `visibility=[] + ai_behavior="respond"`；场景结束、停止监听、关闭插件、撤销同意或更换显式目标时发送恢复 `read`。目标为空时不解析或冻结宿主的活动角色 ID、不注入跨消息场景，也不发送 `target_lanlan` 或 `coalesce_key`；每条 `respond` 都内嵌完整陪伴约束，由宿主逐消息选择当时的活动角色。

公开 SDK 的 push receipt 只确认提交，不确认宿主已消费、生成或播放，也没有返回最终角色文本的正式回调。因此独立浮层不能承接自动角色台词，也不会自动显示解析器事件；它只接受用户显式触发的诊断文本。

## 酒馆建议工具

`hearthstone_battlegrounds_advice` 是只读 LLM tool，不直接生成台词。它返回：

- 当前战棋公开局势；
- `hsbg.cards` 当前卡池摘要，以及按 `card_id` 去重的当前商店/手牌/战团/英雄规则事实；
- 带来源、补丁和验证时间的赛季规则；
- 当前赛季的本机聚合统计与样本量；
- 全局 meta 数据的可用状态和禁止编造契约。

数据同意开启后工具即可使用，不要求同时开启主动解说。这让用户可以安静游玩，只在主动提问时获得角色回答。

卡牌目录不做流派评分、胜率排序或本地推荐。远端 `rules_text` 经过 HTML 清洗和长度限制，仍被标记为不可信参考数据；角色必须核对 provider、patch、checked_at、stale 和覆盖率。常规 `*_G` 金卡会映射到金色规则，少量旧式或不规则 CardID 会进入 `missing_ids`，角色不得猜测缺失元数据。目录不可用不会令实时局势整体不可用。

## 持久化与线程

单局快照、玩家名和完整提示上下文不持久化。`plugin.toml` 只声明安装默认值，N.E.K.O 原生配置服务是唯一运行时设置来源；用户显式填写的 `log_path` 会随设置持久化，自动发现的实际日志路径只属于运行状态。Plugin Store 只用于酒馆聚合统计，不读取或保存插件设置。

Plugin Store 长期只保存赛季/模式/英雄维度的聚合计数。N.E.K.O `startup()` 所在 asyncio loop 不是长期后台 loop，因此 `AsyncStoreWriter` 拥有自己的 event-loop 线程，并用 `run_coroutine_threadsafe()` 串行提交这些统计写入。插件停机时只停止自己拥有的 writer 并等待提交，不关闭或销毁 SDK Store；Store client 和生命周期由 N.E.K.O 宿主管理。

统计初始读取只有明确成功后才开放后续写入。Store 返回 `Err`、抛异常或已有数据校验失败时，核心日志监听和陪伴仍会启动，但统计保持降级且禁止记录、清空或覆盖未知的历史值，等待插件重启后重新加载。

`BattlegroundsCardCatalog` 使用独立后台线程，固定访问 `https://hsbg.cards/api/v1`，限制响应、条目和字段长度，并以临时文件加 `os.replace()` 写入 N.E.K.O `cache_path()`。网络、JSON 或磁盘失败时保留已有快照并公开错误码；Power.log 监听线程从不联网。

## 主要配置

| 配置项 | 默认值 | 作用 |
| --- | --- | --- |
| `monitor_on_start` | `true` | 启动后监听日志 |
| `initial_read_max_bytes` | `67108864` | 首次本地恢复最多读取 64 MiB |
| `llm_data_consent` | `false` | 允许工具/上下文读取过滤后的公开局势 |
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
