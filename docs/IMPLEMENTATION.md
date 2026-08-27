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
自动化测试：40 passed
真实 ask：passed
真实 chat：passed
workspace_read：passed
workspace_write：passed
hook allow：passed
hook deny：passed
external docs.echo：passed
真实密钥入库：not found
```
