# conti-agent

**conti-agent 是一个带全屏 TUI 的终端编程助手（coding agent）**。指定一个工作区目录，接入一个兼容 OpenAI 或 Anthropic 协议的模型，它就能在你的项目里读代码、改文件、跑命令、搜索定位——全程在终端完成，行为可审计、可中断、可恢复。

```text
  ____    ___    _   _    _____  ___
 / ___|  / _ \  | \ | |  |_   _| |_ _|
| |     | | | | |  \| |    | |   | |
| |___  | |_| | | |\  |    | |   | |
 \____|  \___/  |_| \_|   |___| |___|
```

## 功能

**终端界面（TUI）**

- 全屏工作台：对话流 + 运行状态侧栏 + 多行输入框；
- 流式输出，Markdown 基础渲染（标题/加粗/代码块/表格）；
- 工具调用内联显示，`Ctrl+O` 展开查看参数与结果；
- 实时上下文用量（当前占用 / 窗口大小），接近上限自动压缩；
- `↑↓` 选择的交互式提问（模型需要澄清时给出预设选项）；
- 斜杠命令自动补全，`Esc` 随时中断任务。

**Agent 能力**

- 工具：读/写/编辑文件、目录浏览、正则搜索、受限进程执行、任务笔记；
- 子任务（spawn_task）：把独立工作派发给受限子代理；
- Skill：Markdown 格式的技能包，按需加载；
- 会话：JSONL 账本持久化，`/resume` 完整恢复，中断安全（工具配对自动补齐）。

**模型与上下文**

- 多 Provider：OpenAI / Anthropic / DeepSeek 等兼容协议，`/model` 随时切换；
- 长上下文：窗口大小可配置（如 DeepSeek v4 的 1M）；
- 自动压缩：接近窗口上限时，旧历史由模型生成摘要，近期原文保留，
  超大工具结果自动落盘为文件（上下文只留预览 + 路径，不丢信息）；
- 全程流式输出。

**安全**

- 四级权限模式：`read_only` / `workspace` / `approved` / `trusted`；
- 路径沙箱（拒绝工作区外访问）、危险命令检测、审批回调、审计日志（`.conti/runtime/audit.jsonl`）。

## 快速开始

需要 Python 3.11+。

```bash
pip install -e .[tui]
```

配置分两层，项目级覆盖全局：

- **全局**：`~/.conti-agent/config.toml`（可选 `config.local.toml` 存密钥）——配一次，任何目录都能运行；
- **项目**：`<工作区>/.conti/config.toml`（+ `config.local.toml`）——按名称覆盖全局的 provider/profile。

```toml
[[provider]]
name = "deepseek"
protocol = "openai-compat"
base_url = "https://api.deepseek.com"
model = "deepseek-v4-flash"
api_key_env = "DEEPSEEK_API_KEY"   # 或在 config.local.toml 存明文 api_key
context_window = 1000000
max_output_tokens = 8192

[runtime]
permission_mode = "workspace"
```

启动：

```bash
conti-agent            # 进入全屏 TUI
conti-agent ask "..."  # 一次性任务
conti-agent chat --line   # 无 TTY 环境的行式模式
```

也可以直接运行构建好的单文件 `conti-agent.exe`（放到工作目录，与 `.conti` 同级），或用脚本自己构建：

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File scripts\build_windows_exe.ps1
```

## 快捷键

| 按键 | 作用 |
|---|---|
| `Enter` | 发送 |
| `Ctrl+J` | 输入框内换行 |
| `Esc` / `Ctrl+C` | 中断当前任务 / 跳过提问 |
| `Ctrl+O` | 展开/收起工具调用详情 |
| `Ctrl+B` | 显示/隐藏侧栏 |
| `PageUp` / `PageDown` / `Home` / `End` | 滚动对话 |
| `Ctrl+Q` | 退出 |

## 斜杠命令

`/help` `/models` `/model <name>` `/status` `/sessions` `/resume <id>` `/compact` `/activity` `/panel` `/new` `/clear` `/exit`

## 文档

- [规格说明](docs/SPEC.md) · [架构](docs/ARCHITECTURE.md) · [配置示例](docs/CONFIG_EXAMPLE.toml)
- [打包发布](docs/PACKAGING.md) · [端到端测试](docs/E2E_TESTING.md)
- 实现进度与设计推导见 [docs/IMPLEMENTATION.md](docs/IMPLEMENTATION.md)

## 说明

- 核心运行时零第三方依赖；TUI 只依赖 `prompt-toolkit`；
- API Key 建议放环境变量；本地明文配置放 `.conti/config.local.toml`（已被 gitignore，不会入库）；
- `serve` 只绑定 `127.0.0.1`。
