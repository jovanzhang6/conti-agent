# conti-agent 实施计划与改动点

本文档不是抽象路线图，而是按工作包记录“改哪个文件、改哪些函数、怎么验收、当前状态”。后续升级必须先在这里登记工作包，再改代码。

## 状态总览

| 工作包 | 状态 | 目标 |
|---|---:|---|
| WP-00 独立仓库与规格 | 已完成 | 独立 Python 包、Spec、测试入口 |
| WP-01 Agent 核心协议 | 已完成 | 事件流、Provider、工具、重试 |
| WP-02 本地工作区工具 | 已完成 | 读/写/编辑/列表/搜索/进程 |
| WP-03 权限、会话、上下文 | 已完成 | 沙箱、规则、审计、恢复、压缩 |
| WP-04 扩展协议 | 已完成 | 配置、Skill、Hook、Profile、外部工具 |
| WP-05 Runtime 与终端对话 | 已完成 | 真实流式 `chat`、状态栏、会话命令 |
| WP-06 真实模型闭环 | 已完成 | 一次性、多轮、工具、Hook、外部工具 |
| WP-07 发布工程 | 进行中 | 文档一致性、发布检查、tag 移动 |
| WP-09 独立全屏 TUI | 已完成 | 自有 ASCII 启动图、三栏工作台、流式对话 |
| WP-10 Windows exe 发布 | 已完成 | PyInstaller 单文件、真实模型/TUI 验证 |
| WP-11 TUI 缺陷修复 | 进行中 | WP-11A/B/E 已完成；滚动与布局重构进行中 |
| WP-08 后续能力 | 未开始 | 记忆检索、Anthropic 流式、服务鉴权、OS 沙箱 |

## WP-00：独立仓库与规格

### 改动点

1. 新建 `src/conti_agent/`，所有可导入代码放在 `src layout`。
2. 新建 `tests/`，只依赖标准库 `unittest`。
3. 新建 `docs/SPEC.md`，先定义行为再实现。
4. `pyproject.toml` 定义入口：
   - `project.scripts.conti-agent = "conti_agent.cli:main"`；
   - `packages.find.where = ["src"]`。

### 验收

```bash
python -m unittest discover -s tests
```

## WP-01：Agent 核心协议

### 目标

模型请求、工具请求、事件输出、重试和终止条件必须可测试，不依赖网络。

### 已完成改动点

1. `src/conti_agent/messages.py`
   - 定义 `ToolCall`、`Usage`；
   - 提供 `system_message`、`user_message`、`assistant_message`、`tool_message`。
2. `src/conti_agent/events.py`
   - 统一 `AgentEvent`；
   - `AgentEvent.to_json()` 输出稳定 JSONL。
3. `src/conti_agent/schema.py`
   - 实现参数类型、required、enum、array、object 校验。
4. `src/conti_agent/tools.py`
   - `Tool.parameters`；
   - `Tool.effects`；
   - `ToolRegistry.register/get/filter/all()`；
   - `execute_tool()` 统一计时、异常转结果。
5. `src/conti_agent/providers.py`
   - `Provider.complete()` 抽象；
   - `FakeProvider` 按脚本返回响应；
   - OpenAI-compatible 非 streaming 请求；
   - Anthropic-compatible Messages 请求；
   - `urllib_transport()` 可被测试替换。
6. `src/conti_agent/agent.py`
   - `Agent.run()` 是 async generator；
   - Provider delta 转成事件；
   - 工具调用转成 `tool` 消息；
   - `AgentIterationLimit` 防止无限循环；
   - `AgentRunConfig.retry_attempts` 控制瞬时错误重试。

### 补充改动点：真实流式

文件：`src/conti_agent/providers.py`

1. `OpenAICompatibleProvider.complete()` 在传入 `stream_handler` 且使用默认 transport 时设置 `stream=true`。
2. 新增 `OpenAICompatibleProvider._stream_request()`：
   - 逐行读取 SSE；
   - 处理 `data: [DONE]`；
   - 发出 `text.delta`；
   - 聚合 `tool_calls` 的分片 JSON；
   - 解析 `finish_reason` 和 `usage`；
   - 流结束但无内容时抛出可重试 `ProviderError`。
3. HTTP 408/409/425/429/5xx 标记为 transient。

### 验收

1. `tests/test_core.py::test_agent_direct_answer`；
2. `tests/test_core.py::test_agent_tool_round`；
3. `tests/test_core.py::test_provider_transient_retry`；
4. `tests/test_core.py::test_openai_stream_request_mapping`；
5. 真实模型 `ask` 输出中文结果，退出码 `0`。

## WP-02：本地工作区工具

### 已完成改动点

1. `src/conti_agent/workspace.py`
   - `Workspace.resolve()`：
     - 拼接相对路径；
     - `Path.resolve()` 处理真实路径；
     - 拒绝工作区外路径和符号链接逃逸；
   - `Workspace.read_text()`：
     - 限制大小；
     - `newline=""` 保留 CRLF/LF；
   - `Workspace.write_text()`；
   - `Workspace.edit_text()`：
     - 精确匹配；
     - 歧义时不写文件；
   - `Workspace.list_paths()`：
     - 忽略 `.git`、`.venv`、`node_modules` 等。
2. `src/conti_agent/tools_local.py`
   - `WorkspaceReadTool`；
   - `WorkspaceWriteTool`；
   - `WorkspaceEditTool`；
   - `WorkspaceListTool`；
   - `WorkspaceSearchTool`；
   - `ProcessRunTool`：
     - `command` 或 `command_line` 二选一；
     - 超时 kill；
     - stdout/stderr 合并；
     - 输出截断；
     - 环境变量只有显式 `inherit_env` 才继承；
     - Windows 注入最小 `SystemRoot` / `COMSPEC`。

### 验收

1. `tests/test_local_tools.py::test_read_write_edit`；
2. `tests/test_local_tools.py::test_write_preserves_crlf_and_rejects_ambiguous_edit`；
3. `tests/test_local_tools.py::test_path_traversal_is_rejected`；
4. `tests/test_local_tools.py::test_process_timeout`；
5. `tests/test_local_tools.py::test_process_output_and_env_policy`。

## WP-03：权限、会话、上下文

### 权限

文件：`src/conti_agent/permissions.py`

1. `PermissionMode`：
   - `read_only`；
   - `workspace`；
   - `approved`；
   - `trusted`。
2. `PermissionChecker.check()` 顺序：
   - schema；
   - 规则；
   - 模式；
   - 危险命令；
   - 路径沙箱；
   - approved 模式批准；
   - 返回 `Decision`。
3. `RuleEngine`：
   - 传入路径顺序为低优先级到高优先级；
   - 读取时反向遍历；
   - 本地规则覆盖项目规则。
4. `DangerousCommandDetector`：
   - 保守匹配递归删除、格式化、提权、远程执行、密钥赋值等模式。
5. `AuditLogger`：
   - 写 `.conti/runtime/audit.jsonl`；
   - 删除 `content`；
   - 替换 `env` 为 `[已省略]`。

### 会话

文件：`src/conti_agent/sessions.py`

1. `SessionStore.create()` 写 `session.started`。
2. `append_message()` 写 canonical message。
3. `append_compaction()` 写 `history.compacted`。
4. `load()`：
   - 校验 schema version；
   - 校验记录类型；
   - 还原 `ToolCall`；
   - 损坏行抛出 `SessionError`。
5. 修正点：
   - `Agent` 现在把带 `tool_calls` 的 assistant 消息写入会话；
   - 工具结果随后写入；
   - 最终 assistant 结果由 `Runtime.ask()` 写入；
   - 因此真实多轮会话可以完整回放。

### 上下文

文件：`src/conti_agent/context.py`

1. `estimate_tokens()` 做保守估算。
2. `ContextManager.budget`：
   - `context_window - max_output_tokens - tool_schema_tokens`。
3. `ContextManager.compact()`：
   - 保留 system；
   - 保留最近消息；
   - 生成显式历史摘要；
   - 不删除原始会话记录。

### 验收

1. `tests/test_safety_state.py::test_permission_modes_and_dangerous_commands`；
2. `tests/test_safety_state.py::test_rule_precedence_and_audit`；
3. `tests/test_safety_state.py::test_agent_denial_does_not_execute_tool`；
4. `tests/test_safety_state.py::test_session_append_and_resume`；
5. `tests/test_safety_state.py::test_session_corruption_is_rejected`；
6. `tests/test_safety_state.py::test_context_budget_and_compaction`；
7. `tests/test_safety_state.py::test_tool_call_assistant_message_is_persisted`。

## WP-04：扩展协议

### 配置

文件：`src/conti_agent/config.py`

1. `load_single()` 解析 TOML。
2. `merge_config()` 合并 provider、profile、hook、external server。
3. `load_config()` 顺序：
   - `~/.conti-agent/config.toml`；
   - `.conti/config.toml`；
   - `.conti/config.local.toml`。
4. 本次补充：
   - `VALID_PROTOCOLS` 增加 `openai-compat`；
   - 兼容常见 OpenAI-compatible 服务命名。

### Skill

文件：`src/conti_agent/skills.py`

1. `SkillLibrary.discover()` 扫描 `.conti/skills/*.md`。
2. front matter 使用 TOML。
3. `find(name)` 按名称加载。
4. Skill 只提供文本，不提供权限。

### Profile

文件：`src/conti_agent/profiles.py`

1. `ProfileRunner.get()` 查找 profile。
2. `ProfileRunner.run()`：
   - 创建独立 messages；
   - 过滤工具注册表；
   - 使用 profile 权限模式创建 `PermissionChecker`；
   - 只返回最终文本报告。
3. `SpawnTaskTool` 调用 runner。

### Hook

文件：

- `src/conti_agent/hooks.py`；
- `src/conti_agent/agent.py`；
- `src/conti_agent/runtime.py`。

本次完成接线：

1. `Runtime` 创建 `HookEngine`。
2. `Agent` 接收 `hook_engine`。
3. 新增 `Agent._execute_call()`，固定顺序：
   ```text
   registry lookup
     → permission check
     → audit
     → tool.before hook
     → tool execution
     → tool.after hook
     → normalized ToolResult
   ```
4. Hook 失败、超时、无效 JSON 默认拒绝。
5. 权限拒绝的操作不会再进入前置 Hook。

### 外部工具

文件：

- `src/conti_agent/external.py`；
- `src/conti_agent/runtime.py`；
- `src/conti_agent/providers.py`；
- `examples/external_echo_server.py`。

本次完成接线：

1. `Runtime.start_external_tools()`：
   - 读取 `[[external_server]]`；
   - 启动 JSON-RPC 子进程；
   - `initialize`；
   - `tools/list`；
   - 注册到 Runtime registry。
2. `Runtime.close_external_tools()`：
   - CLI 退出时关闭子进程；
   - 避免管道泄漏。
3. `OpenAICompatibleProvider` 新增 `_wire_tool_name()`：
   - 注册表名 `docs.echo`；
   - OpenAI wire name 变成 `docs__echo`；
   - 响应后映射回 `docs.echo`。

### 验收

1. `tests/test_extensions.py::test_config_parse_merge_and_secret`；
2. `tests/test_extensions.py::test_skill_discovery_and_load`；
3. `tests/test_extensions.py::test_hook_can_deny`；
4. `tests/test_extensions.py::test_hook_bad_process_denies_by_default`；
5. `tests/test_safety_state.py::test_agent_hook_denies_after_permission`；
6. `tests/test_extensions.py::test_external_manager_list_and_call`；
7. 真实模型可调用 `docs.echo`，返回 `external-echo:external-ok`；
8. 真实 Hook 拒绝后目标文件保持不存在。

## WP-05：Runtime 与终端对话

### 目标

用户进入一个终端界面后直接与真实 AI 连续对话；界面不使用第三方 TUI 框架，也不复制任何既有视觉风格。

### 已完成改动点

1. `src/conti_agent/runtime.py`
   - `Runtime` 是唯一组合根；
   - `create_provider()`：
     - `fake`；
     - `openai` / `openai-compat`；
     - `anthropic`；
   - `Runtime._system_prompt()` 注入：
     - 助手身份；
     - 当前工作区；
     - 权限模式；
     - 工具清单；
     - `.conti/memory/instructions.md` 用户附加指令；
   - `Runtime.ask()`：
     - 创建或恢复 session；
     - 注入 system prompt；
     - 记录用户消息；
     - 自动压缩；
     - `text_callback` 把 `text.delta` 交给终端；
     - 返回 final、session_id、events；
   - `Runtime.describe()`：
     - 显示 provider、model、权限、工作区、扩展状态；
     - 不显示密钥。
2. `src/conti_agent/cli.py`
   - `run_chat()` 是终端对话入口；
   - 启动时显示模型和权限；
   - `你 >` 提示输入；
   - `助手：` 标签输出；
   - 流式输出；
   - 命令：
     - `/help`；
     - `/new`；
     - `/status`；
     - `/sessions`；
     - `/resume <id>`；
     - `/compact`；
     - `/exit`；
   - `run_cli()` 在退出前调用 `close_external_tools()`。

### 验收

1. 自动化：40 个测试全部通过；
2. 真实模型一次性调用成功；
3. 真实模型 `chat` 多轮对话成功；
4. 真实模型工具调用成功；
5. Hook 拒绝成功；
6. 外部工具调用成功；
7. CLI 退出无资源泄漏警告。

## WP-06：真实模型闭环

### 本轮已验证的真实链路

以下验证使用本地已有模型服务配置中的密钥导入进程环境；密钥没有写入新仓库。

| 编号 | 场景 | 命令形态 | 结果 |
|---|---|---|---|
| L-01 | OpenAI-compatible 一次性对话 | `ask "请只回复四个汉字：链路正常"` | 通过，返回 `链路正常` |
| L-02 | 流式连续对话 | 输入自我介绍任务和代号任务，然后 `/exit` | 通过，逐字输出并保持会话 |
| L-03 | 本地读取工具 | 必须调用 `workspace_read` 读取 `src/sample.py` | 通过，返回 `conti-live-ok` |
| L-04 | Hook 放行 | `tool.before` 允许 `workspace_write` | 通过，文件创建 |
| L-05 | Hook 拒绝 | Hook 返回 `deny` | 通过，文件不存在，模型如实报告拒绝 |
| L-06 | 外部工具 | 必须调用 `docs.echo`，参数 `external-ok` | 通过，返回 `external-echo:external-ok` |

### 本轮发现并修复的问题

| 问题 | 根因 | 修复点 |
|---|---|---|
| `openai-compat` 配置被拒绝 | `VALID_PROTOCOLS` 缺少别名 | `config.py` 增加协议别名 |
| `chat` 输出不是真实流式 | OpenAI Provider 只支持非 streaming | `providers.py` 增加 SSE `_stream_request()` |
| 终端会话缺少系统边界 | Runtime 没注入 system prompt | `runtime.py` 增加 `_system_prompt()` |
| Hook 有模型和测试，但没进主链路 | Agent 直接执行工具 | `agent.py` 增加 `_execute_call()` 统一顺序 |
| 外部工具没进 Runtime | 只有 Manager，没有启动/注册 | `runtime.py` 增加 start/close |
| `docs.echo` 被 OpenAI 拒绝 | wire 工具名不允许点号 | `providers.py` 增加 `_wire_tool_name()` 映射 |
| 工具多轮会话恢复不完整 | assistant tool-call 消息未持久化 | `agent.py` 在工具调用时持久化 assistant 消息 |
| 退出时出现管道资源警告 | 外部子进程未显式关闭 | `runtime.py` / `cli.py` 增加 close 生命周期 |

## WP-07：发布工程

### 当前结论

`v0.1.0` 不应停留在纯文档提交上。完成真实模型闭环后，发布 tag 只能指向包含以下内容的提交：

1. 40 个自动化测试通过；
2. 真实模型 `ask` 通过；
3. 真实模型 `chat` 通过；
4. 真实工具调用通过；
5. Hook 拒绝通过；
6. 外部工具通过；
7. 中文文档与实现一致。

### 发布前固定检查

```bash
python -m unittest discover -s tests
git status --short
git grep -nE "sk-[A-Za-z0-9]{16,}" -- .
```

预期：

1. 测试全部通过；
2. 工作区干净；
3. 没有真实密钥格式命中。

### Tag 规则

1. 发布完成前不保留旧 `v0.1.0` tag 的语义；
2. tag 必须移动到最新发布验收提交；
3. 下一能力开发应从 `v0.1.1-dev` 或 `v0.2.0-dev` 开始，而不是继续修改已发布语义。

## WP-09：独立全屏 TUI

### 结论修正

早期把“风格不能一样”误解为“不做 TUI”。正确目标是：不做继承式视觉，但必须做一个全新的、更好用的自有 TUI。`conti-agent chat` 在真实终端默认进入全屏 TUI；`chat --line` 仅作为管道和无 TTY 的兼容模式。

### 改动点

1. `pyproject.toml`
   - 新增 optional dependency：
     ```toml
     tui = ["prompt-toolkit>=3.0,<4"]
     ```
   - `all` 包含 `tui`；
   - 核心 runtime 仍保持零第三方依赖。
2. `src/conti_agent/tui.py` 新建。
   - `STARTUP_LOGO`：自有 `CONTI` ASCII 启动图；
   - `show_startup_logo()`：清屏、显示启动图、进入 alternate screen；
   - `TuiState`：纯状态层，保存 messages、activity、status、session、usage、tool count 和 error count；
   - `TuiState.stream_delta()` 实时聚合 AI 文本；
   - `TuiState.record_event()` 处理工具请求、完成、重试、失败和用量；
   - `ContiTui`：prompt_toolkit full-screen `Application`；
   - Header：品牌、模型、权限、工具数、运行状态；
   - 左侧：可滚动对话流和任务输入框；
   - 右侧：状态/活动栏；
   - Footer：快捷键和状态；
   - Enter 发送；
   - Ctrl+C 只取消当前任务；
   - Ctrl+Q 取消任务并退出。
3. `src/conti_agent/runtime.py`
   - `Runtime.ask()` 新增 `event_callback`；
   - `text.delta` 走 `text_callback`；
   - 所有事件额外交给 TUI 活动栏；
   - usage 事件更新 sidebar。
4. `src/conti_agent/cli.py`
   - `chat --tui` 强制 TUI；
   - `chat --line` 强制行式；
   - 真实 TTY 下默认 TUI；
   - 无 TTY 或 `--line` 时使用原行式界面；
   - 缺少 `prompt-toolkit` 时给出明确安装命令。
5. `tests/test_tui.py` 新建。
   - 校验启动图；
   - 校验流式占位、聚合、结束替换；
   - 校验工具、用量、活动事件；
   - 校验 `/help`、`/sessions`；
   - 用 DummyOutput / DummyInput 构造 Application，不要求测试进程有 TTY。

### 与旧 TUI 要求的边界

1. 不使用任何继承项目的样式、配色、Logo 或布局；
2. 不把 Logo 做成普通一行文字；
3. 不把流式输出混在输入行里；
4. 不让工具活动刷掉用户输入；
5. 不把取消误绑定成退出。

### 验收

```bash
pip install -e .[tui]
python -m unittest discover -s tests
python -m conti_agent.cli --config .conti/config.toml chat
```

自动化预期 44 个测试全部通过。手动必须看到：

1. 自有 `CONTI` ASCII 启动图；
2. 全屏三栏工作台；
3. Header 显示 provider/model/权限/状态；
4. 对话流出现用户和助手消息；
5. AI 输出有 streaming 标记；
6. 工具活动进入右侧栏；
7. token 用量累加；
8. `/help`、`/status`、`/sessions` 可用；
9. `Ctrl+C` 取消但留在界面；
10. `Ctrl+Q` 退出并恢复正常终端。

### 已知限制

1. 当前对话 pane 提供滚动视图，但尚未做完整视觉选择/复制模式；
2. `/resume` 恢复后先从新事件继续显示，历史消息回填属于 WP-09A；
3. TUI 依赖 `prompt-toolkit`，但它是显式 extra，不是核心 Runtime 的强制依赖。

### WP-09A：TUI 历史回填

改动点：

1. `TuiState.load_session()`；
2. `SessionStore.load()` 后把 user/assistant/tool 消息映射成界面消息；
3. 工具消息进入 activity 而不是对话正文；
4. `/resume` 后显示“历史回填完成”。

### WP-09B：TUI 会话列表选择器

改动点：

1. 增加会话列表面板；
2. 支持上下选择；
3. Enter 确认恢复；
4. Esc 返回对话。

### WP-09C：TUI 权限审批弹窗

改动点：

1. approved 模式触发时暂停任务；
2. 显示工具名、参数摘要、风险原因；
3. `y/n` 或 Tab 切换；
4. 决定写回 `PermissionChecker` 批准回调。

### WP-09D：TUI 主题系统

改动点：

1. 配置新增 `[tui] theme`；
2. 内置 `dark`、`light`、`mono`；
3. 样式从硬编码字典迁移到主题对象；
4. 文档给出颜色示例。

### WP-09E：TUI 压力测试

改动点：

1. 生成 1000 条消息和 100 个工具事件；
2. 限制对话 pane 只保留必要 fragments；
3. 测量 60 秒流式刷新 CPU；
4. 验证窗口 resize。

## WP-10：Windows exe 发布

### 目标

最终用户不需要安装 Python 或创建 venv，直接运行：

```powershell
.\conti-agent.exe
```

即可进入 TUI。

### 改动点

1. `scripts/build_windows_exe.ps1`
   - 自动查找项目 venv、上级 venv 或 `PYTHON`；
   - 安装 `.[tui]` 与 PyInstaller；
   - 运行全量测试；
   - 构建单文件控制台 exe；
   - 输出 `dist\conti-agent.exe`。
2. `scripts/exe_entry.py`
   - 使用绝对导入 `from conti_agent.cli import main`；
   - 避免把 `cli.py` 当作顶层脚本导致相对导入失败；
   - Windows 下设置控制台输入/输出代码页为 UTF-8；
   - stdout/stderr reconfigure 为 UTF-8。
3. `pyproject.toml`
   - 新增 `build-exe` extra；
4. `src/conti_agent/cli.py`
   - 无子命令时默认执行 `chat`；
   - exe 直接双击或无参数运行即进入 TUI。
5. `conti-agent.spec`
   - onefile；
   - console；
   - 收集 prompt_toolkit 数据和 hidden imports。

### 验证记录

| 场景 | 结果 |
|---|---|
| 自动化测试 | 51 passed |
| exe 离线 ask | passed |
| exe 真实 ask | passed，返回“程序正常” |
| exe UTF-8 中文 | passed，无乱码 |
| exe TUI 启动 | passed，ASCII Logo + alternate screen |
| exe TUI `/status` | passed |
| exe TUI 流式任务 | passed，USER/ASSISTANT 消息和 token 更新 |
| exe TUI 退出 | passed，`Ctrl+Q` 后终端恢复 |

### 已知限制

1. 当前只发布 Windows x64；
2. exe 未签名，SmartScreen 可能提示；
3. onefile 首次启动略慢；
4. Linux/macOS 仍使用 Python 安装方式；
5. 后续签名、图标、版本资源和安装器属于 WP-10A。

### WP-10A：签名与安装器

改动点：

1. 引入代码签名证书；
2. 加入版本信息和图标；
3. 生成 SHA-256 清单；
4. 可选构建 zip 或 MSI。

## WP-11：TUI 缺陷修复与交互重构

详细缺陷清单、根因、修复顺序和验收矩阵见 [`docs/UI_REPAIR_PLAN.md`](UI_REPAIR_PLAN.md)。

### 修复原则

先补 Runtime 和命令能力，再改 TUI 布局。M1 完成前不新增视觉装饰。

### WP-11A：CommandRegistry

改动点：

1. 新建 `src/conti_agent/commands.py`；
2. 统一定义 `/help`、`/models`、`/model`、`/new`、`/status`、`/sessions`、`/resume`、`/compact`、`/activity`、`/panel`、`/clear`、`/exit`；
3. TUI 和行式模式共用执行器；
4. Slash 候选来自命令注册表；
5. `/help` 自动生成。

已完成：

1. 新增 `src/conti_agent/commands.py`；
2. 新增 `CommandSpec`、`CommandContext`、`CommandResult`、`CommandSuggestion`；
3. Runtime、TUI 和行式模式共用 `CommandRegistry`；
4. TUI 输入 `/` 时显示候选；
5. `/model` 参数候选显示 provider 名称；
6. `/resume` 参数候选显示 session id；
7. 测试覆盖候选、未知命令、缺少参数、模型列表和模型切换。

### WP-11B：Provider Registry 和模型切换

改动点：

1. `Runtime.list_providers()`；
2. `Runtime.set_active_provider()`；
3. busy 状态禁止切换；
4. Profile 子代理跟随 active provider；
5. session 元数据记录 provider/model；
6. `/models` 列出全部，`/model <name>` 切换。

已完成：

1. Runtime 保存全部 `provider_configs`；
2. Runtime 不再固定使用第一个 provider；
3. 新增 `list_providers()`；
4. 新增 `get_provider_info()`；
5. 新增 `active_provider_name()`；
6. 新增 `set_active_provider()`；
7. busy 时禁止切换；
8. 切换成功后更新主 Agent、Profile 子代理和上下文窗口；
9. 切换失败时保留原模型。

行式模式和 TUI 均已接入：

1. `/models`；
2. `/model <name>`；
3. `/activity`；
4. 命令候选。

### WP-11C：ConversationViewport

改动点：

1. 停止每次渲染强制到底；
2. 增加 `follow_bottom` 和 manual scroll；
3. PageUp/PageDown/Home/End；
4. 鼠标滚轮和滚动条；
5. resize 后重建；
6. 小窗口降级到行式模式或提示页。

### WP-11D：信息架构重构

改动点：

1. Header 只保留品牌；
2. 当前模型和状态移到输入框下方；
3. 侧栏默认收起；
4. `Ctrl+B` / `/panel` 切换；
5. 侧栏偏好持久化。

### WP-11E：ActivityFormatter

改动点：

1. 新建 `src/conti_agent/activity.py`；
2. 工具名和参数翻译为用户动作；
3. 关联请求、完成、拒绝和耗时；
4. `/activity` 查看完整记录；
5. 侧栏只显示最近关键状态。

已完成：

1. 新增 `src/conti_agent/activity.py`；
2. 内建 `workspace_read/write/edit/list/search`、`process_run`、`request_input`、`task_note`、`spawn_task`、`load_skill` 的用户文案；
3. 工具请求显示“开始读取 / 开始写入 / 开始执行”；
4. 工具完成显示“已完成 / 失败”和耗时；
5. `/activity` 可查看本次界面的活动列表；
6. 测试覆盖读取、写入、执行和失败状态。

### 验收

见 [`docs/UI_REPAIR_PLAN.md`](UI_REPAIR_PLAN.md) 的 R-001 到 R-015。

## WP-08：后续能力工作包

这些不属于本次 `v0.1.0` 必须发布项，但必须按工作包推进。

### WP-08A：Anthropic 流式

改动点：

1. `src/conti_agent/providers.py`
   - `AnthropicCompatibleProvider.complete()`；
   - 新增 `_stream_request()`；
   - 解析 `message_start`、`content_block_delta`、`message_delta`；
   - 聚合 `tool_use`。

验收：

1. fake SSE 测试；
2. 真实 Anthropic-compatible 服务测试；
3. 断流后重试策略测试。

### WP-08B：模型摘要压缩

当前 `/compact` 使用确定性启发式摘要，不是模型摘要。

改动点：

1. `src/conti_agent/context.py`
   - `Summarizer` 保持协议不变。
2. `src/conti_agent/runtime.py`
   - `Runtime._model_summarizer()`；
   - 单独 Provider 调用；
   - 禁用工具；
   - 限制输出长度；
   - 失败时回退 `_default_summarizer()`。

验收：

1. 长会话压缩；
2. 摘要保留目标、路径、结论、待办；
3. 原始会话仍完整；
4. Provider 失败时 `/compact` 仍可用。

### WP-08C：记忆检索

改动点：

1. `src/conti_agent/memory.py` 新建；
2. `Runtime._system_prompt()` 注入 top-k 记忆；
3. `.conti/memory/facts.md` 按关键词和最近时间评分；
4. 审计记录记忆来源；
5. 不引入向量数据库。

### WP-08D：外部工具加载分层

当前外部工具全部进入模型工具 schema。

改动点：

1. `src/conti_agent/external.py`
   - 新增 `ExternalToolLoadTool`；
2. `Runtime.start_external_tools()`
   - 小 schema 直接注册；
   - 大 schema 只注册 load 工具；
3. `tests/test_extensions.py`
   - 大 schema 阈值测试。

### WP-08E：OS 级进程沙箱

当前 `process_run` 有路径、超时、输出、环境和危险命令控制，但不是 OS 沙箱。

改动点：

1. `src/conti_agent/sandbox.py` 新建；
2. Windows：
   - 限制工作令牌或 Job Object；
   - 禁止桌面交互；
3. Unix：
   - `preexec_fn` 或子进程 helper；
   - 限制网络和文件系统写入；
4. `ProcessRunTool` 接入 `SandboxPolicy`；
5. 破坏性命令仍由权限层先拒绝。

### WP-08F：服务鉴权

当前 `serve` 只绑定 loopback，无 token。

改动点：

1. `src/conti_agent/service.py`
   - `RuntimeService` 接收 token provider；
2. `src/conti_agent/cli.py`
   - `--auth-env`；
   - 校验 `Authorization: Bearer`；
3. 禁止日志输出 token。

## 缺陷处理规则

任何真实端到端发现的问题必须记录四项：

1. 复现命令；
2. 期望结果；
3. 实际结果；
4. 根因和改动文件。

不允许只改测试让问题消失。

## 当前验收快照

```text
自动化测试：51 passed
真实 ask：passed
真实 chat：passed
TUI module/Application：passed
TUI startup ASCII：passed
workspace_read：passed
workspace_write：passed
hook allow：passed
hook deny：passed
external docs.echo：passed
exe offline ask：passed
exe live ask：passed
exe live TUI：passed
真实密钥入库：not found
```
