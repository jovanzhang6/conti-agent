# conti-agent 功能规格

## 1. 项目目标

`conti-agent` 是一个独立、可嵌入、可学习的 Python coding-agent 运行时。它把兼容 OpenAI 或 Anthropic Messages 协议的模型接入本地工作区，并用统一权限层控制所有副作用。

第一公民体验是 `chat` 全屏终端 TUI：用户进入终端后连续输入任务，助手输出流式回答。TUI 使用本项目自己的视觉设计，不复制任何既有项目的 Logo、布局或配色。核心 Runtime 不依赖第三方包，TUI 可通过 `prompt-toolkit` extra 安装。

设计原则：

1. **确定性核心**：模型、工具、事件、持久化都可用假实现测试。
2. **显式安全**：所有写入、执行和控制操作先经过权限检查，并写入审计账本。
3. **可观测**：文本流、工具请求、工具结果、重试、用量和完成状态使用同一事件模型。
4. **可扩展**：Skill、Profile、Hook 和外部工具进程不侵入核心。
5. **本地优先**：无遥测、无强制第三方依赖、无隐藏云端控制面。

## 2. 运行模式

### 2.1 一次性任务

```bash
conti-agent --config .conti/config.toml ask "修复测试"
conti-agent --config .conti/config.toml ask "检查项目" --event-format jsonl
```

要求：

- `ask` 执行完整 Agent 循环；
- `--event-format jsonl` 输出机器可读事件；
- 成功返回 `0`，配置或输入错误返回 `2`，运行失败返回 `3`；
- CLI、REPL 和 HTTP 共用 Runtime 门面。

### 2.2 全屏终端 TUI

```bash
conti-agent chat
```

`chat` 在真实终端中默认启动全屏 TUI；`chat --line` 保留兼容行式界面，便于管道、容器和无 TTY 环境。

启动流程：

1. 显示独立设计的 `CONTI` ASCII 启动图；
2. 初始化 Runtime、工具和外部工具服务；
3. 进入 alternate screen；
4. 显示三栏工作台。

TUI 必须包含 Header、对话流、活动侧栏、任务输入区和 Footer。对话流显示用户、助手、系统消息和流式标记；活动侧栏显示 provider/model/权限/工作区、会话、工具数、token 用量、错误数和工具活动。

交互要求：

1. `Enter` 发送任务；
2. 流式回答实时写入对话流并显示 streaming 标记；
3. 工具请求、工具完成、重试和用量进入活动栏；
4. `Ctrl+C` 取消当前任务，不退出界面；
5. `Ctrl+Q` 退出；
6. 会话命令可直接在输入框执行。

支持 `/help`、`/new`、`/status`、`/sessions`、`/resume <id>`、`/compact`、`/clear`、`/exit`。行式界面仍支持行尾反斜杠续行。界面只负责输入输出，不拥有运行时策略。

### 2.3 本地服务

```bash
conti-agent serve --host 127.0.0.1 --port 8791
```

默认只绑定 loopback。请求体：

```json
{"prompt": "检查项目", "session_id": null, "output_format": "jsonl"}
```

返回最终结果、会话 ID 和事件列表。

## 3. 配置

配置使用 TOML，默认按以下优先级合并：

1. `.conti/config.local.toml`
2. `.conti/config.toml`
3. `~/.conti-agent/config.toml`

核心配置：

```toml
[[provider]]
name = "primary"
protocol = "openai"
base_url = "https://api.example.com/v1"
model = "example-model"
api_key_env = "EXAMPLE_API_KEY"
context_window = 128000
max_output_tokens = 8192

[runtime]
permission_mode = "workspace"
max_tool_iterations = 32
history_limit = 120

[extensions]
skills = true
hooks = true
profiles = true
external_tools = true
collaboration = true
```

本地直用时，可在 Git 外的 `.conti/config.local.toml` 中直写 Key：

```toml
[[provider]]
name = "deepseek"
protocol = "openai-compat"
base_url = "https://api.deepseek.com"
model = "deepseek-v4-flash"
api_key = "你的本地 API Key"
```

解析优先级是 `api_key` > `api_key_env`。`config.local.toml` 不进入 Git。

约束：

- 支持 `openai`、`openai-compat`、`anthropic`、`fake`；
- Git 内示例只使用 `api_key_env`；Git 外本地配置可使用 `api_key`；
- Provider 名称必须唯一；
- 权限模式只允许 `read_only`、`workspace`、`approved`、`trusted`。

## 4. Agent 循环

一次任务流程：

1. 构建 messages 和工具 schema；
2. 调用 Provider；
3. 发出文本增量、用量和 assistant 消息事件；
4. 对每个工具调用执行参数校验、权限检查、工具执行和审计；
5. 把工具结果写回 messages；
6. 循环直到没有工具调用或达到上限；
7. 输出 `run.completed` 或 `run.failed`。

事件：

```text
run.started
run.completed
run.failed
run.retry
message.created
text.delta
tool.requested
tool.approved
tool.completed
usage.recorded
```

连接、超时、限流和 5xx 属于可重试错误；重试次数和指数退避由 `AgentRunConfig` 控制。

## 5. 内建工具

| 工具 | 效果 | 功能 |
|---|---|---|
| `workspace_read` | read | 读取有大小限制的 UTF-8 文件 |
| `workspace_write` | write | 创建或替换文件 |
| `workspace_edit` | write | 精确替换文本，歧义时拒绝 |
| `workspace_list` | read | 列出路径并忽略依赖目录 |
| `workspace_search` | read | 字面量或正则搜索 |
| `process_run` | execute/write | 有超时、输出上限和环境策略的命令 |
| `load_skill` | read | 显式加载 Skill 正文 |
| `task_note` | write | 持久化任务笔记 |
| `request_input` | control | 向本地用户请求澄清 |
| `spawn_task` | control | 执行受限 Profile 子任务 |

所有路径必须停留在活动工作区内。`process_run` 必须有超时、输出截断和显式环境继承。

## 6. 权限与审计

权限模式：

1. `read_only`：只允许读取；
2. `workspace`：允许工作区内副作用；
3. `approved`：非读取操作首次需要批准；
4. `trusted`：宽松，但危险命令仍需批准。

检查顺序：

1. JSON 参数校验；
2. 用户规则；
3. 模式策略；
4. 危险命令检测；
5. 路径沙箱；
6. 批准；
7. 审计。

规则示例：

```toml
[[rule]]
tool = "workspace_write"
decision = "deny"
pattern = '\.env$'
```

审计写入 `.conti/runtime/audit.jsonl`，`content` 和 `env` 被移除。

## 7. 会话与上下文

`.conti/sessions/<session-id>.jsonl` 是追加式账本。恢复时逐行校验 schema version 和记录类型；损坏账本必须报错。

上下文预算：

```text
context_window - max_output_tokens - tool_schema_tokens
```

压缩保留 system 提示、最近消息和显式历史摘要；原始账本仍保留压缩前记录。

## 8. Skill

Skill 是 `.conti/skills/*.md`，front matter 使用 TOML：

```markdown
---
name = "release"
description = "发布前检查清单"
keywords = ["release"]
version = 1
---

1. 运行测试。
2. 检查变更记录。
```

模型默认只看见元数据；必须通过 `load_skill` 显式加载正文。Skill 不能执行代码，也不能扩大权限。

## 9. Profile 与子任务

Profile 定义专家子代理的系统提示、工具白名单、权限模式和最大迭代数。`spawn_task` 创建独立子消息列表和子上下文，只返回最终文本报告。子任务不能修改父消息列表，也不能绕过权限检查。

## 10. 外部工具协议

外部工具进程使用行分隔 JSON-RPC：

1. `initialize`
2. `tools/list`
3. `tools/call`
4. 关闭或超时清理

工具名会加命名空间，例如 `docs.echo`。发送到 OpenAI-compatible 服务前，`docs.echo` 映射为合法的 `docs__echo`；返回请求后映射回注册表名。外部工具仍必须通过 schema 校验和权限层。Runtime 启动配置中的服务器，并在 CLI 退出时关闭子进程。

## 11. Hook

Hook 在权限通过后、工具执行前运行 `tool.before`；工具执行后运行 `tool.after`。Hook 收到 JSON stdin，可返回：

```json
{"decision": "deny", "message": "阻止危险命令"}
```

Hook 超时、非零退出码和无效 JSON 默认导致拒绝。Hook 只能拒绝或替换输出，不能放行未批准操作。权限拒绝的操作不会再进入前置 Hook。

## 12. 协作任务板

`CrewManager` 提供本地任务板和邮箱。任务有唯一 ID、负责人、状态、结果和时间。状态为 `todo`、`doing`、`done`、`failed`。数据原子写入 `.conti/runtime/crews/<crew>.json`。每个 worker 仍通过 Runtime 使用自己的权限检查。

## 13. Git 快照

`SnapshotManager` 提供显式 Git worktree：

- `create(slug)` 创建 `conti/<slug>` 分支和工作树；
- `status(path)` 返回变更；
- `cleanup(path)` 只能显式触发；
- 非 Git 仓库必须安全失败。

## 14. 非目标

- 不做遥测；
- 不做自动权限提升；
- 不内置 Logo 或品牌视觉；
- 不主动安装或执行远程代码；
- 核心运行时不要求第三方依赖；全屏 TUI 通过 `prompt-toolkit` extra 提供。

## 15. v0.1.0 发布门槛

除自动化测试外，发布前必须完成以下真实端到端验证：

1. 真实 OpenAI-compatible 模型一次性调用成功；
2. 真实模型进入 `chat` 后连续多轮对话成功；
3. `chat` 默认进入全屏 TUI，并显示独立 ASCII 启动图；
4. TUI 对话流实时显示流式文本；
5. TUI 活动栏显示工具请求、完成、重试和用量；
6. 真实模型调用 `workspace_read` 或 `workspace_write` 成功；
7. 前置 Hook 能拒绝写入并让目标文件保持不存在；
8. 外部 `namespace.tool` 能从 JSON-RPC 子进程加载并被模型调用；
9. 密钥只出现在环境变量，不出现在源码、配置示例、会话或审计文件；
10. `python -m unittest discover -s tests` 全部通过；
11. `chat --line` 在无 TTY 环境仍可用；
12. 退出 TUI 后终端状态恢复，没有子进程或管道资源泄漏警告。
13. Windows x64 构建生成单文件 `dist/conti-agent.exe`；
14. exe 离线 `ask`、真实模型 `ask` 和 `chat --tui` 全部通过；
15. exe 无参数时默认进入 `chat`。
