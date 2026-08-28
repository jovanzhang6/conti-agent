# conti-agent

`conti-agent` 是一个独立实现的 Python coding-agent 运行时。它把兼容 OpenAI 或 Anthropic Messages 协议的模型接入本地工作区工具，并用统一的权限层、事件流、会话账本和审计日志约束模型行为。

这个仓库同时面向使用和学习：

- 核心循环不依赖网络，可使用 `fake` Provider 离线运行；
- 模型输出和工具活动使用同一套 JSONL 事件；
- 配置、会话、审计、任务板都是普通本地文件；
- 核心 Runtime 不要求第三方依赖；
- `chat` 默认进入独立设计的全屏 TUI；
- 自有 `CONTI` ASCII 启动图，没有继承任何项目的界面风格。

## 功能总览

- `ask`：一次性任务，可输出文本或 JSONL 事件；
- `chat`：全屏 TUI 对话，支持流式输出、会话、压缩和活动侧栏；
- `sessions`：列出持久化会话；
- `worker`：执行本地协作任务板中的任务；
- `serve`：默认只绑定 `127.0.0.1` 的本地 HTTP 服务；
- 工具：读写编辑、搜索列表、受限进程执行、Skill、任务笔记、子任务；
- 安全：权限模式、路径沙箱、规则引擎、危险命令检测、批准回调和审计；
- 扩展：Profile 子代理、Markdown Skill、Hook、外部 JSON-RPC 工具；
- 协作：本地任务板和邮箱；
- 快照：显式 Git worktree。

## 安装

需要 Python 3.11 或更高版本。

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -e .
```

推荐安装 TUI extra：

```bash
pip install -e .[tui]
```

TUI 使用 `prompt-toolkit`；核心 Runtime 仍可单独使用。无 TTY 环境可用 `chat --line`。
类 Unix 系统中激活脚本路径改为：

```bash
source .venv/bin/activate
```

也可以不安装，直接从仓库根目录运行：

```bash
python -m conti_agent.cli --help
```

## 快速开始

### 1. 推荐入口：进入终端与真实 AI 对话

先复制真实模型示例：

```bash
mkdir -p .conti
cp examples/live.deepseek.toml .conti/config.toml
```

配置使用环境变量：

```toml
api_key_env = "CONTI_AGENT_E2E_API_KEY"
```

设置环境变量：

```bash
export CONTI_AGENT_E2E_API_KEY="你的真实 API Key"
```

进入终端对话：

```bash
python -m conti_agent.cli --config .conti/config.toml chat
```

界面先显示独立设计的 `CONTI` ASCII 启动图，然后进入全屏工作台：

```text
  ____  ____  _   _  _____ _    ___
 / ___|/ ___|| \ | ||_   _| |  / _ \
| |    | |   |  \| |  | |  | | | | | |
| |___ | |___| |\  |  | |  | |_| |_| |
 \____| \____|_| \_|  |_|   \___/\___/

CONTI-AGENT | deepseek-v4-flash | workspace | 准备就绪
对话流                        │ 运行状态
任务输入 — Enter 发送         │ tokens / activity
Enter 发送 | Ctrl+C 取消 | Ctrl+Q 退出
```

直接输入任务即可连续对话。`Enter` 发送，`Ctrl+C` 取消当前任务，`Ctrl+Q` 退出。

### 2. 离线冒烟

把 provider 换成：

```toml
[[provider]]
name = "offline"
protocol = "fake"
base_url = "local://fake"
model = "fake-model"

[runtime]
permission_mode = "workspace"
```

运行：

```bash
python -m conti_agent.cli ask "你好"
```

### 3. 其他真实模型

OpenAI-compatible：

```toml
[[provider]]
name = "primary"
protocol = "openai"
base_url = "https://api.example.com/v1"
model = "example-model"
api_key_env = "EXAMPLE_API_KEY"
```

Anthropic-compatible：

```toml
[[provider]]
name = "primary"
protocol = "anthropic"
base_url = "https://api.example.com"
model = "example-model"
api_key_env = "EXAMPLE_API_KEY"
```

设置环境变量后运行：

```bash
python -m conti_agent.cli --config .conti/config.toml ask "检查当前项目，并解释测试结构"
```

### 4. 观察事件

```bash
python -m conti_agent.cli ask "检查项目" --event-format jsonl
```

### 5. 会话命令

```bash
python -m conti_agent.cli chat
```

命令：

```text
/help
/new
/status
/sessions
/resume <session-id>
/compact
/exit
```

端到端手动验收见 [`docs/E2E_TESTING.md`](docs/E2E_TESTING.md)。

## 配置

配置使用 TOML。默认加载顺序从低到高是：

1. `~/.conti-agent/config.toml`
2. `.conti/config.toml`
3. `.conti/config.local.toml`

显式指定：

```bash
python -m conti_agent.cli --config .conti/config.toml ask "你好"
```

完整示例见 [`docs/CONFIG_EXAMPLE.toml`](docs/CONFIG_EXAMPLE.toml)。

重要约束：

- API Key 只允许放在环境变量中，通过 `api_key_env` 引用；
- 权限模式使用 `read_only`、`workspace`、`approved`、`trusted`；
- 模型能力不能绕过本地权限策略。

## 目录布局

```text
.conti/
  config.toml
  config.local.toml
  sessions/<session-id>.jsonl
  runtime/audit.jsonl
  runtime/crews/<crew>.json
  runtime/tasks/notes.json
  skills/*.md
```

审计文件会记录工具决策，但不会记录参数中的 `content` 和 `env`。

## 开发与测试

核心 Runtime 零第三方依赖；全屏 TUI 使用 `prompt-toolkit` extra。测试使用标准库 `unittest`。

```bash
python -m unittest discover -s tests
```

文档：

- [`docs/SPEC.md`](docs/SPEC.md)：功能规格；
- [`docs/IMPLEMENTATION.md`](docs/IMPLEMENTATION.md)：分阶段实现；
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)：架构；
- [`docs/LEARNING.md`](docs/LEARNING.md)：学习路线；
- [`docs/E2E_TESTING.md`](docs/E2E_TESTING.md)：端到端手动测试；
- [`docs/RELEASE.md`](docs/RELEASE.md)：发布清单。

## 当前版本

`v0.1.0` 的目标是一个可用、可审计、可学习的本地 coding-agent 运行时。后续可以在不改变核心事件模型和权限边界的前提下继续扩展工具、Provider、协作和持久化能力。

## 许可

MIT。
