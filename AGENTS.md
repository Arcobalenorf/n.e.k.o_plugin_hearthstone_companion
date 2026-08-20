# AGENTS.md

本文件适用于整个 `n.e.k.o_plugin_hearthstone_companion` 独立仓库。所有代理和贡献者在修改代码、文档、构建配置或发布资产前都必须遵守以下约定。

## 项目定位

- 本项目是独立的 N.E.K.O 插件仓库，最终产物是可导入和可发布到 N.E.K.O 插件市场的 `.neko-plugin`，不是 N.E.K.O 主项目内置功能。
- N.E.K.O 的核心价值是陪伴。局势解析、酒馆建议和数据能力应服务于当前 N.E.K.O 角色的自然陪玩，不应退化成纯工具面板或本地规则模板解说。
- 原始 `Power.log`、玩家名称、隐藏牌和本机绝对路径不得进入 LLM、网络请求、Store 或发布证据。真实日志只允许在本机用于脱敏回放验证，不得提交或上传。
- 不得声称拥有未授权的全服胜率、排名或 meta 数据；没有可靠来源时必须明确不可用，不得编造。
- 不得为了未发布或无人使用的旧版本增加插件 ID、配置或数据兼容逻辑，除非用户明确要求。
- 插件市场投稿属于单独的外部发布动作。除非用户明确授权，不得自动提交市场审核。

## 分支职责

- `main` 是稳定发布分支。它只接受已经完成验证、准备发布或已经发布的变更。
- `dev` 是日常集成分支。常规开发、修复、测试和发布准备都必须基于 `dev` 进行，不得直接在 `main` 上开发。
- 较大或可并行的工作从 `dev` 创建 `feature/<name>`、`fix/<name>` 或 `refactor/<name>`，验证后合回 `dev`。
- 紧急线上修复从 `main` 创建 `hotfix/<name>`。发布完成后必须将 `main` 同步回 `dev`，防止修复丢失。
- 开始工作前必须执行 `git status --short --branch` 并确认当前分支。不得覆盖、清理或回退用户已有的未提交改动。
- 不得强推、删除远端分支或改写 `main` 历史。建议在 GitHub 保护 `main`，要求 PR、必需检查通过，并禁止 force push。

## 日常开发流程

1. 从最新 `dev` 开始；先获取远端状态并确认工作树情况。
2. 先阅读相关实现、测试、N.E.K.O Plugin SDK 契约和本仓库文档，再确定修改范围。
3. 保持改动集中，不把无关重构、格式化或主项目修改混入插件提交。
4. 开发过程中先运行与改动直接相关的测试；涉及共享解析器、生命周期、隐私、配置或 Hosted UI 时扩大覆盖范围。
5. 修复必须增加能够在旧代码上失败、在新代码上通过的回归测试。不得只修改断言来迁就实现。
6. 使用真实 `Power.log` 验证时，只记录行数、大小、模式、阶段、公开快照和脱敏事件结果，不输出或保存原始敏感内容。
7. 完成后运行全量质量门禁，并在 `dev` 上提交。没有用户明确授权时，不得合并 `main`、创建 tag、发布 Release 或申请市场投稿。

## 必需验证

根据风险执行定向测试，并在准备合入或发布前至少完成：

```powershell
uv run pytest -q
uv run ruff check .
uv run python -m compileall -q -f . -x "[\\/](\.git|\.venv|\.pytest_cache|\.ruff_cache|build|dist)[\\/]"
git diff --check
```

还必须完成以下项目相关门禁：

- 使用受支持的稳定 N.E.K.O SDK 运行 `tests/sdk_runtime_smoke.py`。
- 使用固定的 N.E.K.O SDK 检查 `ui/panel.tsx`，确保 Hosted TSX 能通过官方编译器。
- Verify 必须先通过全量 pytest、Ruff、compileall、`git diff --check` 和 Hosted UI，才能进入官方 market verify；不得用定向测试代替全量质量 job。
- tag Release 必须在同一个 workflow run 内显式依赖全量质量、Hosted UI 和稳定 SDK smoke 全部成功；另一个 workflow 的成功状态不能替代 `needs` 依赖，也不能在前置检查仍运行或失败时先创建 Release。
- Hosted UI 的定时器、异步队列、刷新、焦点或草稿行为发生变化时，必须使用真实 Hosted runtime 做动态测试，不能只做源码字符串断言。
- 日志定位、bootstrap、解析器、局势快照、酒馆工具或事件生命周期发生变化时，必须使用代表性真实日志做增量或完整回放，并确认不会重放历史主动解说、历史统计或隐藏数据。
- 涉及设置保存、启动或停止时，必须覆盖炉石未运行、已在对局、停止后重启、配置事务和稳定 SDK 的生命周期边界。

## 发布准备

只有当 `dev` 已通过全部门禁且用户确认需要正式发布时，才能准备发布：

1. 按 SemVer 确定版本号。
2. 同步更新 `plugin.toml`、`pyproject.toml`、`uv.lock`、网络 User-Agent 中的版本，以及 README 中明确写出的当前版本。
3. 在 `docs/marketplace.md` 顶部新增本版本说明，记录用户可感知变化、重要修复、隐私边界和验证证据；不得覆盖历史版本说明。
4. Release 说明必须准确描述实际能力和限制，不得把计划中、未验证或无数据来源的能力写成已支持。
5. 运行官方发布门禁，且 tag 名必须与包内版本一致：

```powershell
$env:GITHUB_REF_NAME = "v<version>"
uv run python -m plugin.neko_plugin_cli.cli check --release --market-release "<plugin-repo>"
uv run python -m plugin.neko_plugin_cli.cli build "<plugin-repo>" --out "<plugin-repo>\dist\hearthstone_companion-v<version>-release.neko-plugin"
uv run python -m plugin.neko_plugin_cli.cli inspect "<package>"
uv run python -m plugin.neko_plugin_cli.cli verify "<package>"
```

6. 从构建产物执行一次干净目录安装，随后再次运行稳定 SDK 生命周期、Hosted UI 和必要的真实日志回放验证。
7. 确认构建排除开发文件 `AGENTS.md`，并从干净 checkout 构建正式资产；不得从含未跟踪文件的工作树直接发布。
8. 提交 `dev` 并等待其 GitHub Actions 全部成功。

## 合并与正式发布

1. 通过 PR 将 `dev` 合并到 `main`；合并前再次确认版本、发布说明、测试和构建资产一致。
2. `main` 上的发布提交必须等于计划打 tag 的提交。不得在未经验证的提交上打 tag。
3. 在 `main` 的发布提交创建 annotated tag：`v<version>`。不得从 `dev`、feature 分支或脏工作树打正式 tag。
4. 推送 `main` 和 tag 后等待 Verify、Stable SDK Smoke 和 Release 工作流全部完成；任何失败都必须先修复，不能手工绕过。
5. 从 GitHub Release 下载实际生成的 `.neko-plugin`，不要用本地包代替线上资产完成最终结论。
6. 核对 GitHub API digest、下载文件 SHA-256、文件大小和 payload hash；再次执行 `inspect`、`verify`、干净安装和稳定 SDK 生命周期测试。
7. 对影响日志读取或局势理解的发布，使用下载后的线上包执行真实日志回放，并验证模式、阶段、公开快照、事件顺序和无历史副作用。
8. 最终交付信息至少包含 commit、tag、Release 链接、线上资产 SHA-256、CI 结果和关键实机验证结论。
9. 发布成功后将 `main` 合并或快进同步回 `dev`，确保下一轮开发包含发布提交与 hotfix。

## Hotfix 流程

1. 从最新 `main` 创建 `hotfix/<name>`，只处理线上阻断问题。
2. 完成与风险相称的回归测试和全部发布门禁。
3. 经用户确认后合入 `main`，更新 patch 版本和发布说明，创建新 tag 并完成完整线上资产复验。
4. 立即将发布后的 `main` 同步回 `dev`，并解决冲突；不得只修 `main`。

## 提交与说明

- 提交信息使用清晰的 Conventional Commit 风格，例如 `fix: sync live Battlegrounds context`。
- 一次提交只表达一个可理解的目的；不要把生成缓存、临时安装目录、真实日志或本机配置提交进仓库。
- 未完成的开发状态只存在于 `dev` 或功能分支；`main` 的每个提交都应保持可构建、可验证、可发布。
- 发现当前流程、SDK 契约或 CI 配置与本文件不一致时，先停止发布并修正文档或流程，不能默默跳过门禁。
