# conti-agent 架构说明

## 1. 总览

`conti-agent` 的核心不是界面，而是一条可测试的执行链：

```text
CLI / REPL / HTTP
      ↓
    Runtime
      ↓
   Agent Loop
   ↓       ↓
Provider  Tool Registry
          ↓
Permission Checker
          ↓
    Tool Implementation
          ↓
Session / Audit / Events
```

## 2. 模块职责

| 模块 | 职责 |
|---|---|
| `messages.py` | 消息和 ToolCall |
| `events.py` | 统一事件和 JSONL 序列化 |
| `tools.py` | 工具接口、注册表、执行器 |
| `providers.py` | 模型协议适配和 Fake Provider |
| `agent.py` | Agent 循环、事件、重试、迭代上限 |
| `workspace.py` | 工作区路径边界 |
| `tools_local.py` | 文件、搜索、列表、进程工具 |
| `permissions.py` | 权限、规则、危险命令、批准、审计 |
| `sessions.py` | 会话账本和恢复 |
| `context.py` | token 估算、窗口规划、压缩 |
| `config.py` | TOML 配置和合并 |
| `skills.py` | Skill 发现和加载 |
| `hooks.py` | Hook 执行和决策 |
| `profiles.py` | Profile 子代理 |
| `external.py` | 外部 JSON-RPC 工具 |
| `collab.py` | 任务板和邮箱 |
| `snapshots.py` | Git worktree 快照 |
| `runtime.py` | 组合根 |
| `cli.py` | CLI、REPL、HTTP 入口 |

## 3. 一次请求数据流

1. 外部适配器收到 prompt。
2. `Runtime.ask()` 创建或恢复会话。
3. `ContextManager` 检查预算，必要时压缩。
4. `Agent.run()` 调用 Provider。
5. Provider 产生文本、工具调用和用量。
6. Agent 发出事件。
7. 每个工具调用先进入权限检查。
8. 通过后执行工具。
9. `ToolResult` 变成 tool 消息。
10. Provider 继续推理。
11. 会话和审计持续追加。
12. 最后的 assistant 文本成为结果。

## 4. 安全边界

```text
ToolCall
  → schema validation
  → rules
  → permission mode
  → dangerous command detector
  → path sandbox
  → approver
  → audit
  → Tool.execute()
```

权限层统一在工具执行前，新增工具不能绕过模式、规则、沙箱和审计。

## 5. 扩展边界

- Skill 只扩展知识；
- Profile 扩展受限子代理；
- Hook 扩展组织策略，失败默认拒绝；
- External Tool 扩展能力，但仍要过权限层。

## 6. 并发模型

当前工具按顺序执行，事件顺序确定。接口已经是异步的，后续可以做受控并发，但不能破坏消息顺序、工具 ID 对应关系、权限先后顺序和账本追加语义。

## 7. 为什么适合学习

1. 每层可以单独阅读；
2. Fake Provider 让全链路离线可测；
3. 事件流解释 Agent 内部行为；
4. 权限检查是显式函数；
5. 会话和审计是普通 JSONL；
6. Git 提交和实现阶段对应。
