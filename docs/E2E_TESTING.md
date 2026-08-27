# conti-agent 端到端手动测试指南

本文档用于在真实终端中手动验证 `conti-agent` 的主要链路。自动化测试检查函数和模块行为；端到端测试检查你实际使用时的完整路径：

```text
终端输入 / HTTP 请求
  → CLI / REPL / Service
  → Runtime
  → Provider
  → Agent Loop
  → Permission
  → Tool
  → Session / Audit
  → 终端输出 / HTTP 响应
```

建议按下面的顺序执行。每完成一组测试，在对应位置标记“通过”或记录问题。

## 0. 准备一个干净测试目录

不要直接在项目源码目录里测试写入类工具。先创建一个独立沙盒目录：

```powershell
$root = Join-Path $env:TEMP ("conti-e2e-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
New-Item -ItemType Directory -Path $root | Out-Null
Set-Location $root
Write-Host "测试目录：$root"
```

类 Unix 系统可以用：

```bash
root="$(mktemp -d)/conti-e2e"
mkdir -p "$root"
cd "$root"
echo "$root"
```

后面所有命令都假设你仍在该目录内。

## 1. 安装和基础自检

### 1.1 安装项目

回到源码目录：

```powershell
Set-Location D:\conti-agent\conti-agent
python -m venv .venv
.\.venv\Scripts\activate
pip install -e .
```

类 Unix：

```bash
cd /path/to/conti-agent
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

### 1.2 自动化测试基线

```bash
python -m unittest discover -s tests
```

预期：

- 全部测试通过；
- 没有 import 错误；
- 没有需要第三方运行时依赖的警告。

### 1.3 CLI 可执行

```bash
python -m conti_agent.cli --help
```

预期应看到：

```text
ask
chat
sessions
config-check
worker
serve
```

记录：命令帮助正常显示。

## 2. 离线 Fake Provider 冒烟测试

这一步不访问网络，用于验证 CLI、Runtime、会话、事件输出和基础目录创建。

### 2.1 创建离线配置

在测试目录创建 `.conti/config.toml`：

```powershell
New-Item -ItemType Directory -Force -Path .conti | Out-Null
@'
[[provider]]
name = "offline"
protocol = "fake"
base_url = "local://fake"
model = "fake-model"

[runtime]
permission_mode = "workspace"
max_tool_iterations = 32
history_limit = 120
'@ | Set-Content -Encoding utf8 .conti/config.toml
```

类 Unix：

```bash
mkdir -p .conti
cat > .conti/config.toml <<'EOF'
[[provider]]
name = "offline"
protocol = "fake"
base_url = "local://fake"
model = "fake-model"

[runtime]
permission_mode = "workspace"
max_tool_iterations = 32
history_limit = 120
EOF
```

### 2.2 校验配置

```bash
python -m conti_agent.cli config-check
```

预期输出：

```text
配置有效
```

### 2.3 一次性任务

```bash
python -m conti_agent.cli ask "离线冒烟测试"
```

预期输出：

```text
fake provider ready
```

退出码应为 `0`。PowerShell 可以检查：

```powershell
$LASTEXITCODE
```

### 2.4 检查会话目录

```powershell
Get-ChildItem .conti\sessions
```

类 Unix：

```bash
ls -l .conti/sessions
```

预期：

- 出现一个或多个 `.jsonl` 会话文件；
- 文件不是空文件。

### 2.5 列出会话

```bash
python -m conti_agent.cli sessions
```

预期输出至少一行会话 ID。

记录：离线链路正常。

## 3. JSONL 事件流测试

### 3.1 输出事件

```bash
python -m conti_agent.cli ask "检查事件流" --event-format jsonl
```

预期终端输出多行 JSON。至少应看到：

```text
run.started
message.created
run.completed
```

### 3.2 验证 JSON 可解析

PowerShell：

```powershell
$lines = python -m conti_agent.cli ask "再次检查事件流" --event-format jsonl
$objects = $lines | ForEach-Object { $_ | ConvertFrom-Json }
$objects.event
```

类 Unix：

```bash
python -m conti_agent.cli ask "再次检查事件流" --event-format jsonl > events.jsonl
python - <<'PY'
import json
from pathlib import Path
lines = Path("events.jsonl").read_text(encoding="utf-8").splitlines()
events = [json.loads(line) for line in lines if line.strip()]
print([item["event"] for item in events])
PY
```

预期：

- 每行都是合法 JSON；
- 每个对象都有 `event`、`timestamp`、`payload`；
- 最后一个事件类型通常是 `run.completed`。

记录：事件流可被程序解析。

## 4. 行式 REPL 测试

启动：

```bash
python -m conti_agent.cli chat
```

按顺序输入：

```text
/help
hello from repl
/sessions
/exit
```

预期：

- `/help` 显示命令；
- 普通输入会收到模型结果；离线模式下应看到 `fake provider ready`；
- `/sessions` 能列出前面的会话；
- `/exit` 正常退出。

再启动一次：

```bash
python -m conti_agent.cli chat
```

先复制上一步看到的会话 ID，然后输入：

```text
/resume <session-id>
/exit
```

预期：

- 显示 `已恢复 <session-id>`；
- 没有账本损坏错误。

记录：REPL 启动、恢复、退出正常。

## 5. 真实模型基础链路

这一步需要你自己的模型服务账号和 API Key。不要把 API Key 写进配置文件。

### 5.1 配置 OpenAI-compatible 服务

创建或修改 `.conti/config.toml`：

```toml
[[provider]]
name = "primary"
protocol = "openai"
base_url = "https://your-openai-compatible-endpoint/v1"
model = "your-model-name"
api_key_env = "CONTI_E2E_API_KEY"
context_window = 128000
max_output_tokens = 8192

[runtime]
permission_mode = "workspace"
max_tool_iterations = 32
```

设置环境变量：

PowerShell：

```powershell
$env:CONTI_E2E_API_KEY = "你的 API Key"
```

类 Unix：

```bash
export CONTI_E2E_API_KEY="你的 API Key"
```

运行：

```bash
python -m conti_agent.cli config-check
python -m conti_agent.cli ask "只回复：真实模型链路正常"
```

预期：

- 返回文本；
- 没有配置错误；
- 没有认证 401 错误。

### 5.2 配置 Anthropic-compatible 服务

```toml
[[provider]]
name = "primary"
protocol = "anthropic"
base_url = "https://your-anthropic-compatible-endpoint"
model = "your-model-name"
api_key_env = "CONTI_E2E_API_KEY"
context_window = 128000
max_output_tokens = 8192

[runtime]
permission_mode = "workspace"
```

运行相同命令。

记录：至少一种真实模型协议可用。

## 6. 工作区读取工具测试

使用真实模型，或任何支持工具调用的服务。

准备样本文件：

```powershell
New-Item -ItemType Directory -Force -Path src | Out-Null
@'
def hello():
    return "conti"
'@ | Set-Content -Encoding utf8 src\sample.py
```

类 Unix：

```bash
mkdir -p src
cat > src/sample.py <<'EOF'
def hello():
    return "conti"
EOF
```

运行：

```bash
python -m conti_agent.cli ask "请使用 workspace_read 读取 src/sample.py，并告诉我函数返回值。不要修改文件。"
```

预期：

- 模型结果提到 `conti`；
- 终端没有权限拒绝；
- `.conti/runtime/audit.jsonl` 中出现 `workspace_read` 的 approved 记录。

检查审计：

```powershell
Get-Content .conti\runtime\audit.jsonl -Tail 10
```

类 Unix：

```bash
tail -n 10 .conti/runtime/audit.jsonl
```

记录：模型可发起工具调用，工具结果能回给模型。

## 7. 文件写入工具测试

保持 `permission_mode = "workspace"`。

运行：

```bash
python -m conti_agent.cli ask "请使用 workspace_write 创建 hello.txt，内容为：conti e2e"
```

检查文件：

PowerShell：

```powershell
Get-Content hello.txt
```

类 Unix：

```bash
cat hello.txt
```

预期：

- `hello.txt` 存在；
- 内容包含 `conti e2e`；
- `.conti/runtime/audit.jsonl` 中有 `workspace_write` 的 approved 记录。

记录：工作区内写入正常。

## 8. 路径沙箱和权限拒绝测试

把配置改成只读：

```toml
[runtime]
permission_mode = "read_only"
```

运行：

```bash
python -m conti_agent.cli ask "请使用 workspace_write 创建 denied.txt，内容为：should not exist"
```

预期：

- 模型收到工具错误或权限拒绝；
- `denied.txt` 不存在；
- 审计文件中出现 denied 记录。

检查：

```powershell
Test-Path denied.txt
Get-Content .conti\runtime\audit.jsonl -Tail 10
```

类 Unix：

```bash
test ! -f denied.txt && echo "文件不存在，符合预期"
tail -n 10 .conti/runtime/audit.jsonl
```

再测试路径逃逸：

把权限改回：

```toml
[runtime]
permission_mode = "workspace"
```

运行：

```bash
python -m conti_agent.cli ask "请使用 workspace_write 写入 ../outside.txt，内容为：escape"
```

预期：

- 工作区外没有生成 `outside.txt`；
- 工具结果包含路径越界或权限拒绝；
- 审计中有 denied 或对应错误记录。

记录：权限模式和路径沙箱生效。

## 9. 进程执行工具测试

保持 `workspace` 模式。

Windows：

```bash
python -m conti_agent.cli ask "请使用 process_run 执行命令数组：['cmd.exe', '/d', '/s', '/c', 'echo process-e2e']，并告诉我输出。"
```

类 Unix：

```bash
python -m conti_agent.cli ask "请使用 process_run 执行命令数组：['echo', 'process-e2e']，并告诉我输出。"
```

预期：

- 模型结果包含 `process-e2e`；
- 没有权限拒绝；
- 审计中出现 `process_run` approved 记录。

### 9.1 危险命令拒绝

运行：

```bash
python -m conti_agent.cli ask "请使用 process_run 执行命令数组：['rm', '-rf', '.']，然后告诉我结果。"
```

预期：

- 工具被权限层拒绝；
- 当前目录没有被删除；
- 审计中出现 denied 记录。

记录：进程工具可执行，危险命令被拒绝。

## 10. 会话持久化和压缩测试

使用真实模型效果更好。

### 10.1 创建多轮会话

运行：

```bash
python -m conti_agent.cli ask "记住一个代号：blue-lantern。只回复收到。"
```

查看会话 ID：

```bash
python -m conti_agent.cli sessions
```

复制最新会话 ID，然后运行：

```bash
python -m conti_agent.cli ask "我们刚才的代号是什么？" --session <session-id>
```

预期：

- 模型能引用 `blue-lantern`，或至少能从上下文中恢复任务；
- 没有会话损坏错误。

### 10.2 REPL 压缩

```bash
python -m conti_agent.cli chat
```

输入：

```text
/resume <session-id>
/compact
/exit
```

预期：

- 显示已压缩历史；
- 会话文件中新增 `history.compacted` 记录；
- 原始消息没有被物理删除。

检查：

PowerShell：

```powershell
Select-String -Path .conti\sessions\<session-id>.jsonl -Pattern "history.compacted"
```

类 Unix：

```bash
grep "history.compacted" ".conti/sessions/<session-id>.jsonl"
```

记录：会话恢复和压缩正常。

## 11. 本地 HTTP 服务测试

### 11.1 启动服务

```bash
python -m conti_agent.cli serve --host 127.0.0.1 --port 8791
```

预期输出：

```text
http://127.0.0.1:8791
```

保持该终端运行。

### 11.2 发送请求

另开一个终端，进入同一个测试目录。

PowerShell：

```powershell
$body = @{ prompt = "HTTP 端到端测试"; output_format = "jsonl" } | ConvertTo-Json
Invoke-RestMethod -Uri "http://127.0.0.1:8791" -Method Post -ContentType "application/json; charset=utf-8" -Body $body | ConvertTo-Json -Depth 10
```

类 Unix：

```bash
curl -X POST http://127.0.0.1:8791 \
  -H "Content-Type: application/json" \
  -d '{"prompt":"HTTP 端到端测试","output_format":"jsonl"}'
```

预期：

- HTTP 状态为 200；
- 响应包含 `result`、`session_id`、`events`；
- 服务进程没有崩溃。

### 11.3 非法请求

```bash
curl -X POST http://127.0.0.1:8791 \
  -H "Content-Type: application/json" \
  -d '{}'
```

预期：

- 返回 400；
- 错误信息说明 `prompt` 缺失或为空；
- 服务继续运行。

### 11.4 停止服务

在服务终端按 `Ctrl+C`。

记录：服务请求、非法请求和正常停止都通过。

## 12. Skill 加载测试

创建一个 Skill：

```powershell
New-Item -ItemType Directory -Force -Path .conti\skills | Out-Null
@'
---
name = "e2e-check"
description = "端到端检查清单"
keywords = ["e2e"]
version = 1
---

1. 检查 README。
2. 检查测试。
'@ | Set-Content -Encoding utf8 .conti\skills\e2e-check.md
```

类 Unix：

```bash
mkdir -p .conti/skills
cat > .conti/skills/e2e-check.md <<'EOF'
---
name = "e2e-check"
description = "端到端检查清单"
keywords = ["e2e"]
version = 1
---

1. 检查 README。
2. 检查测试。
EOF
```

使用真实模型运行：

```bash
python -m conti_agent.cli ask "请使用 load_skill 加载 e2e-check，然后列出它的两个步骤。"
```

预期：

- 模型结果包含“检查 README”和“检查测试”；
- 没有权限拒绝。

记录：Skill 可被发现并加载。

## 13. 子代理 Profile 测试

确认配置中有只读 Profile：

```toml
[[profile]]
name = "reader"
description = "只读调查"
system_prompt = "只收集证据，不修改文件。"
allowed_tools = [
  "workspace_read",
  "workspace_list",
  "workspace_search",
  "load_skill"
]
permission_mode = "read_only"
max_tool_iterations = 12
```

运行：

```bash
python -m conti_agent.cli ask "请使用 spawn_task，profile 为 reader，任务：读取 src/sample.py 并说明函数返回值。"
```

预期：

- 最终结果包含 `conti`；
- 父任务收到子任务报告；
- 如果模型试图让子任务写入文件，应被只读权限拒绝。

记录：Profile 子任务受工具白名单和权限模式约束。

## 14. 本地任务板和 worker 测试

### 14.1 手动创建任务板

可以先运行一次 worker，让它自动创建目录；不过当前 `worker` 命令要求任务已存在，因此先用一个小 Python 脚本创建任务：

```bash
python - <<'PY'
from pathlib import Path
from conti_agent.collab import CrewManager
crew = CrewManager(Path(".conti/runtime/crews"), "e2e")
crew.create_task("task-1", "离线 worker 冒烟测试", "manual")
print("created")
PY
```

### 14.2 执行 worker

```bash
python -m conti_agent.cli worker --crew e2e --agent manual --task task-1
```

预期：

- 输出模型结果；离线模式下是 `fake provider ready`；
- 退出码为 0。

### 14.3 检查任务状态

```powershell
Get-Content .conti\runtime\crews\e2e.json
```

类 Unix：

```bash
cat .conti/runtime/crews/e2e.json
```

预期：

- `task-1` 的状态是 `done`；
- `result` 不为空；
- `owner` 是 `manual`。

记录：任务板和 worker 链路正常。

## 15. Git 快照测试

这一步必须在另一个测试 Git 仓库中进行。

### 15.1 准备仓库

```bash
$snapshotRoot = Join-Path $env:TEMP ("conti-git-e2e-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
New-Item -ItemType Directory -Path $snapshotRoot | Out-Null
Set-Location $snapshotRoot
git init -b main
git add .
git -c user.name="conti-e2e" -c user.email="e2e@example.com" commit --allow-empty -m "init"
```

类 Unix：

```bash
snapshot_root="$(mktemp -d)/conti-git-e2e"
mkdir -p "$snapshot_root"
cd "$snapshot_root"
git init -b main
git add .
git -c user.name="conti-e2e" -c user.email="e2e@example.com" commit --allow-empty -m "init"
```

### 15.2 使用 Python 直接调用快照管理器

```bash
python - <<'PY'
import asyncio
from pathlib import Path
from conti_agent.snapshots import SnapshotManager

async def main():
    manager = SnapshotManager(Path.cwd())
    path = await manager.create("e2e")
    print("created:", path)
    print("status:", await manager.status(path))

asyncio.run(main())
PY
```

预期：

- 显示 created 路径；
- status 为空列表；
- 出现 `.conti/workspace/e2e`。

### 15.3 清理快照

```bash
python - <<'PY'
import asyncio
from pathlib import Path
from conti_agent.snapshots import SnapshotManager

async def main():
    manager = SnapshotManager(Path.cwd())
    await manager.cleanup(Path(".conti/workspace/e2e"))
    print("cleaned")

asyncio.run(main())
PY
```

预期：

- 显示 cleaned；
- `.conti/workspace/e2e` 不存在。

记录：Git 快照创建、状态和清理正常。

## 16. 配置错误和退出码测试

### 16.1 缺失配置

在测试目录中改名配置：

```powershell
Rename-Item .conti\config.toml config.toml.bak
python -m conti_agent.cli ask "不应执行"
$LASTEXITCODE
Rename-Item config.toml.bak config.toml
```

类 Unix：

```bash
mv .conti/config.toml config.toml.bak
python -m conti_agent.cli ask "不应执行"
echo $?
mv config.toml.bak .conti/config.toml
```

预期退出码为 `2`。

### 16.2 缺少 API Key

使用真实 Provider 配置，但不要设置环境变量：

```powershell
Remove-Item Env:CONTI_E2E_API_KEY -ErrorAction SilentlyContinue
python -m conti_agent.cli ask "不应执行"
$LASTEXITCODE
```

类 Unix：

```bash
unset CONTI_E2E_API_KEY
python -m conti_agent.cli ask "不应执行"
echo $?
```

预期：

- 报错提示环境变量未设置；
- 退出码为 `2`；
- 工具不会被执行。

记录：错误路径返回正确退出码。

## 17. 审计和隐私检查

执行一次写入测试：

```bash
python -m conti_agent.cli ask "请使用 workspace_write 创建 privacy.txt，内容为：CONTI_E2E_SECRET"
```

检查审计文件：

```bash
Select-String -Path .conti\runtime\audit.jsonl -Pattern "CONTI_E2E_SECRET"
```

类 Unix：

```bash
grep "CONTI_E2E_SECRET" .conti/runtime/audit.jsonl
```

预期：

- `privacy.txt` 可以包含该内容；
- `audit.jsonl` 不应包含 `CONTI_E2E_SECRET`。

记录：审计文件不泄漏工具内容。

## 18. 中断和稳定性测试

### 18.1 REPL 中断

启动：

```bash
python -m conti_agent.cli chat
```

输入一个任务后立即按 `Ctrl+C`。

预期：

- 进程退出；
- 不出现未捕获 traceback；
- 已写入的会话文件仍然可以读取。

### 18.2 服务中断

启动：

```bash
python -m conti_agent.cli serve --port 8792
```

按 `Ctrl+C`。

预期：

- 服务正常停止；
- 端口释放；
- 没有异常 traceback。

记录：中断路径可用。

## 19. 每轮回归清单

改动代码后至少手动执行这些快速用例：

1. `python -m unittest discover -s tests` 全部通过；
2. `config-check` 成功；
3. fake Provider `ask` 成功；
4. JSONL 事件可解析；
5. REPL `/help`、`/sessions`、`/exit` 正常；
6. `read_only` 模式下写入被拒绝；
7. 审计文件没有记录敏感内容；
8. 服务响应包含 `result`、`session_id`、`events`。

## 20. 发布前完整验收

发布 `v0.1.x` 前，建议逐项确认：

| 领域 | 必测项 | 通过标准 |
|---|---|---|
| 安装 | `pip install -e .` 和 CLI help | 无报错 |
| 自动化 | `unittest` | 全部通过 |
| 配置 | 本地、项目、显式路径 | 正确读取，错误返回 2 |
| 模型 | fake 和真实 Provider | 至少一种真实协议可用 |
| 工具 | 读取、写入、编辑、进程 | 结果正确，审计完整 |
| 安全 | 只读、越界、危险命令 | 全部拒绝 |
| 会话 | 创建、列出、恢复、压缩 | JSONL 可回放 |
| 扩展 | Skill、Profile、Hook | 不绕过权限 |
| 协作 | 任务创建、worker、状态更新 | 状态和结果持久化 |
| 快照 | 创建、状态、清理 | 只在 Git 仓库内成功 |
| 服务 | 正常请求、非法请求、中断 | 响应正确，可停止 |
| 文档 | 按文档执行无卡点 | 命令、路径、行为一致 |

## 当前已知边界

1. `FakeProvider` 目前只返回固定文本，不会主动调用工具，所以工具调用建议用真实模型测试。
2. 外部 JSON-RPC 工具已有模块级测试，但当前 Runtime 还没有自动加载 `external_server` 配置，因此不属于本轮 CLI 端到端必测项。
3. Hook 配置会由配置解析，但当前 Runtime 还没有把 HookEngine 接入 Agent 工具执行链，因此 Hook 手动测试前需要先补齐接线。
4. `sessions` 列表没有按时间倒序排序的承诺，手动判断最新会话时以时间戳和文件名为准。

如果发现失败，记录：

1. 操作步骤；
2. 配置文件；
3. 完整命令；
4. 完整输出；
5. 退出码；
6. `.conti/runtime/audit.jsonl` 的最后 20 行；
7. 相关会话文件路径。
