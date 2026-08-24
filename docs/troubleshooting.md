# 故障排查

## 找不到 Power.log

插件会从正在运行的 `Hearthstone.exe` 自动定位安装目录，并从多个会话文件夹中选择最后更新的 `Power.log`。通常无需填写路径。

1. 在面板点击“配置日志”；
2. 有改动时完全退出并重启 Hearthstone；
3. 进入一局游戏；
4. 查看 `source_state` 是否为 `watching`、`lines_seen` 是否增长；
5. 仍失败时把 `Power.log` 文件或包含会话文件夹的 `Logs` 目录填入自定义游戏数据位置，然后点击输入框旁的“保存自定义位置”。

默认可用以下命令定位：

```powershell
Get-ChildItem "$env:LOCALAPPDATA\Blizzard\Hearthstone" -Filter Power.log -Recurse -ErrorAction SilentlyContinue |
  Sort-Object LastWriteTime -Descending |
  Select-Object -First 10 FullName, Length, LastWriteTime
```

不要填写游戏安装目录、可执行文件或 Deck Tracker 数据目录。

## 有日志但没有角色主动说话

依次检查：

1. `llm_data_consent` 与 `llm_commentary_enabled` 是否都开启；
2. N.E.K.O 当前角色是否配置了可用模型；
3. 当前是否在旁观模式；
4. 最近是否有用户聊天，普通事件会安静 30 秒；
5. 是否仍在 25 秒普通冷却或 8 秒关键冷却内；
6. 事件优先级是否达到默认阈值 5；
7. 点击“测试解说”检查 SDK 是否接受提交。

插件不会为每个回合或商店变化都发言。稀疏、情绪相关的主动回应是设计行为。

## 主动解说关闭后还能问酒馆问题吗

可以。保持 `llm_data_consent=true`；插件会把最新玩家可见状态以隐藏 `read` 分段交给当前会话，模型也可在首答同一轮调用 `hearthstone_battlegrounds_advice`，或由 Agent 使用酒馆查询入口。主动解说开关只控制插件是否主动发起角色回复。

若查询返回 `llm_data_sharing_not_authorized`，说明数据共享未开启。若状态为空，确认已经进入酒馆且日志正在增长。若面板已有商店/战团但角色仍声称没有炉石能力，依次确认主日志出现 `game_live_state` 被当前会话以 passive callback 接收、`/api/tools` 已注册 `hearthstone_battlegrounds_advice`，以及 Agent 插件目录包含两个 `query_*` 入口。插件必须保持 `passive=false`，否则 Agent 会完全跳过查询入口；同轮工具注册和 `read` 状态流仍是独立链路。

## 为什么候选英雄不完整或不能给出具体出牌

候选列表只显示 `Power.log` 明确归属于本地玩家、未隐藏且未锁定的英雄。日志尚未建立本地 controller、某个选项未公开或选择阶段已经结束时，列表可能为空；插件不会根据常见候选数或卡池补猜。列表可用时，`hearthstone_battlegrounds_advice` 可以结合公共英雄规则和本机样本供角色比较，但不会提供没有来源的全局胜率。“哪张更值得考虑”等定性酒馆建议要求工具当次读到招募阶段、正在更新的完整商店和规则证据；个别费用缺失不会关闭定性比较。若询问可负担性、剩余金币或购买顺序，当前金币和商店所有卡牌的实际费用也必须完整，角色不得用默认 3 费补算，也不得在金币为 0 时把未知费用卡牌直接判为买不起。战斗阶段或缓存状态只能解释已观察信息。

普通对战可在用户主动提问时同轮调用 `hearthstone_current_state`，共享本地玩家当前可见的具体手牌。若角色仍答不出回合或手牌，先确认局势问答授权已开启、状态为实时，再核对 `/api/tools` 中的注册项、对应 tool call 和 callback；这类当前问题应重新查询，不能沿用较早对话。工具不会提供完整合法操作与目标枚举；字段缺失时应如实说明。对手隐藏手牌、奥秘身份和牌序是永久边界，重新配置日志也不会开放。

排查模型链路时分别核对四项：`/api/tools` 中目标角色可见正确工具；模型请求实际携带工具 schema；模型产生带 `focus` 的 tool call；user-plugin-server callback 返回 `is_error=false` 且 `output.format=hearthstone_compact_v1`。模型选择工具本身是概率行为，验收重点是这四段链路可用、明确问题能正常取得对应视图，而不是要求所有自由表达都 100% 触发。

## 为什么没有全服胜率

插件没有 HSReplay Tier7 或 Firestone 私有遥测的授权 API，不会抓取其网页。随包赛季资料只描述官方玩法规则，本机统计只代表用户自己的有效结算样本。角色应明确说明此限制；若它给出没有来源的全服胜率，请附上对话和插件状态报告问题，但不要上传完整日志。

## 卡牌目录不可用或已陈旧

查看 `card_catalog.degraded_reason`、`dataset.patch`、`checked_at` 和 `stale`。首次安装需要短暂下载公共当前卡池；超时、HTTP 429/5xx 或离线时，插件会保留旧缓存，不会中断日志监听。确认网络允许访问 `https://hsbg.cards`，并检查 `card_catalog_network_enabled=true`。不希望插件直连该服务时可关闭此项；`hearthstone_battlegrounds_advice` 仍会返回日志实际观测的实时局势，但卡牌规则依据会减少。少量旧式或不规则金卡 CardID 可能出现在 `missing_ids`；这表示没有可靠规则补全，不影响 Power.log 中的实时身材。

## 对手阵容看起来过时

这是正常边界。Power.log 只能在公开观察到对手战团时记录，UI 和工具会显示 `last_seen_round` 并标记非当前。插件不会推测对手之后的购买、出售或强化。

## 独立诊断浮层问题

常见错误包括 `windows_required`、`tkinter_unavailable`、`python_probe_failed`、`overlay_script_missing` 和 `overlay_exited_early`。诊断浮层不是自动陪玩输出，失败不会影响角色回复。

浮层运行但不可见时，确认炉石窗口未最小化，标题包含 `Hearthstone`、`炉石传说`、`爐石戰記` 或 `하스스톤`，然后点击“测试解说”。只有这类显式诊断消息会进入浮层；真实陪玩由当前 N.E.K.O 角色界面与语音输出。

## 本地统计存储处于降级状态

面板出现 `stats:load_store_err`、`stats:load:<异常类型>` 或 `stats:load_invalid` 时，表示启动时未能可靠加载已有统计。核心陪伴仍可工作，但本次运行会禁止统计记录和清空，避免用空基线覆盖未知历史。`stats:writer_unavailable`、`stats:store_err`、其他 `stats:<异常类型>` 或 `stats:clear_compensation_unconfirmed` 则表示最近一次聚合统计写入未能确认；当前内存统计可能尚未持久化，清空补偿无法确认时会先恢复清空前的内存统计。

不要连续重复清空。先记录错误码，停止并重新启动插件，再刷新面板核对场次；若问题持续，提交插件/N.E.K.O 版本和错误码，不要附带完整 `Power.log`。插件只停止自己拥有的统计 writer，SDK Store client 的生命周期由 N.E.K.O 宿主管理。

## 状态或卡牌不准确

游戏更新可能改变日志格式。记录插件/N.E.K.O 版本、模式、阶段、`source_state`、`lines_seen`、`last_event_kind`、`last_error_code` 和是否发生日志轮换。不要直接上传完整 `Power.log`；按[隐私说明](privacy-security.md#问题报告与合规)裁剪并脱敏。

## 恢复 log.config

首次修改已有配置时会创建 `%LOCALAPPDATA%\Blizzard\Hearthstone\log.config.neko.bak`。插件不会覆盖已有备份或卸载时自动恢复。退出 Hearthstone 后比较当前文件与备份，确认没有其他工具新增配置，再手动处理。
