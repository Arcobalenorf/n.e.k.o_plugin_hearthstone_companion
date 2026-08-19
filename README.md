# 炉石猫娘陪玩

面向 N.E.K.O 的炉石传说与酒馆战棋陪伴插件。插件只读本机 `Power.log`，把公开局势提炼成事实、情绪信号和低打扰的发言时机，再由用户当前的 N.E.K.O 角色自然回应。它不使用本地模板扮演角色，也不把陪伴降格成数据通知。

> 本项目是非官方社区插件，与 Blizzard Entertainment、网易及其关联公司无隶属或背书关系。

## 陪伴体验

- 显式配置固定 `target_lanlan` 时，进入牌局以隐藏 `read` 建立炉石场景，并在结束、停止或撤销授权时恢复；目标为空时不建立跨消息场景，每条 `respond` 自带完整陪伴约束且不携带角色 ID，由 N.E.K.O 宿主逐消息路由给当时的活动角色。
- 三连、低血量、升本、逐轮战果与最终名次等公开事实会形成结构化情绪信号；真正的台词始终由当前 N.E.K.O 角色生成。
- 主动解说只选择稀疏且有情绪价值的事件，并受普通/关键冷却与 30 秒用户聊天静默窗约束；静默窗内只有优先级 `>=9` 的关键事件可以绕过。
- 用户问“现在酒馆玩什么流派”“这局怎么走”时，角色可调用 `hearthstone_battlegrounds_advice`，先查询当前公开局势、带来源的当前卡池/规则事实、官方赛季资料和带样本量的本机统计，再回答。
- 主动解说可以关闭而保留问答工具。数据同意控制是否允许工具读取公开局势；主动解说开关只控制角色是否主动插话。

公开 Plugin SDK 不提供 `respond` 最终文本回调，因此插件不会伪称能把角色实际台词复制到自己的窗口。NEKO 的角色回复、语音和宿主界面是主输出；随包提供的透明浮层只用于用户显式执行诊断测试。

## 酒馆战棋支持

v0.1.1 支持单排和双排的可验证公开状态：

- 战棋模式、Bob、本地玩家与最多八名英雄；
- 英雄选择、招募/战斗阶段、逐轮胜负、回合、当前对手；
- 金币、酒馆等级、冻结、商店、手牌与战团；
- 英雄血量/护甲、淘汰、最终名次；
- 对手阵容仅标记为“上次观察，第 N 回合，非当前阵容”；
- 任务、饰品、畸变、伙伴等赛季机制的公开 ID/进度；
- 单排 Top 4、双排 Top 2、第一名率、平均名次和英雄维度的本机聚合统计。

随包赛季资料当前固定为战棋第 14 赛季、补丁 36.2.2、`Dark Gifts of Dalaran`，来源链包含 Blizzard 36.2 赛季说明和 36.2.2 平衡补丁，验证日期为 2026-08-19。它是版本化规则资料，不是胜率数据。

插件每日从公开的 [hsbg.cards API](https://hsbg.cards/api-docs) 更新当前卡池与卡牌规则，并在 N.E.K.O 分配的插件数据目录离线降级。咨询工具只返回牌面中已观察卡牌的去重事实和不针对具体大厅的卡池计数；不在本地计算流派评分，也不生成角色台词。Card data: [hsbg.cards](https://hsbg.cards/about), subject to its [terms](https://hsbg.cards/terms).

HSReplay Tier7 与 Firestone 的全局表现数据属于各自的私有遥测；本项目没有获得可再发布的授权 API。工具会明确返回全局数据不可用，不抓取网页，也不编造档位、胜率或样本。

## 安全边界

- 只读 Hearthstone 自己生成的 `Power.log`，不注入、不读内存、不抓包、不模拟协议。
- 不自动点击、出牌或代打，不推断隐藏手牌、未揭示奥秘或未来商店。
- 不上传原始日志，不保留玩家名、BattleTag、账号 ID 或完整单局历史；授权时只发送隐私文档列明的有限近期公开事实。
- 首次接入已有日志默认且最多只在本机恢复解析末尾 64 MiB，这些日志字节不会整体发送给模型。
- 卡牌目录更新只发送固定的公共目录 GET，不发送牌局、卡牌 ID 或玩家信息；可用 `card_catalog_network_enabled=false` 关闭。
- 本机统计只保存按赛季、模式和英雄聚合的场次与名次计数。
- LLM 工具和主动解说必须获得公开局势数据同意；旁观模式不触发主动解说。
- “配置日志”是明确的用户操作，只备份并更新 `%LOCALAPPDATA%\Blizzard\Hearthstone\log.config`。

完整字段和数据去向见[隐私与安全](docs/privacy-security.md)。

## 系统要求与安装

- Windows 10/11；
- Hearthstone 桌面客户端；
- Python `>=3.11,<3.12`；
- 仅诊断浮层需要标准库 `tkinter`。

从 N.E.K.O 插件市场安装，或使用 GitHub Release 的 `.neko-plugin` 文件。源码 ZIP 不是插件包。

1. 启动“炉石猫娘陪玩”，在面板点击“配置日志”，有改动时重启炉石。
2. 确认日志状态从 `waiting_for_log` 变为 `watching`。
3. 开启“共享过滤后的公开局势”，即可在聊天中询问当前局势和酒馆建议。
4. 需要角色主动陪玩时，再开启“猫娘 LLM 实时短解说”。

`plugin.toml` 只提供安装默认值；N.E.K.O 原生配置服务是唯一运行时设置来源。只有用户显式填写的 `log_path` 会作为设置保存，自动发现的实际日志路径只保留在当前运行状态中。Plugin Store 长期只保存聚合酒馆统计，其 client 生命周期由 N.E.K.O 宿主管理。

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

Hearthstone Catgirl Companion is a read-only N.E.K.O plugin for constructed Hearthstone and Battlegrounds. It turns privacy-filtered public state into emotion cues and sparse speaking opportunities for the active N.E.K.O character. It supports solo and Duos Battlegrounds state, per-combat outcomes, aggregate-only local results, versioned official season rules, and a read-only advice tool. N.E.K.O owns the actual character response; the separate transparent overlay is diagnostic-only because the public SDK does not return generated reply text. The plugin never claims access to unlicensed global win-rate telemetry.

## 许可证

[MIT License](LICENSE)
