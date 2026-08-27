# conti-agent 真实端到端测试

这份文档只做一件事：让你在真实终端中进入对话界面，用真实模型连续对话，并验证 AI 能安全地操作本地工作区。

自动化测试是开发者的回归检查；端到端的验收标准是下面这个画面：

```text
conti-agent 终端对话
模型：deepseek / deepseek-v4-flash
权限：workspace    工作区：...
直接输入任务；/help 查看命令，/exit 退出。
你 >
```

你输入任务后，应看到：

```text
助手：
模型流式返回的回答......
```

不要用固定假的 `fake provider ready` 作为最终验收。它只能证明进程没坏，不能证明真实 AI 对话可用。

## 0. 准备

### 0.1 安装项目

在项目目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -e .
```

类 Unix：

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

### 0.2 准备 API Key

密钥只能放在环境变量。下面统一使用：

```text
CONTI_AGENT_E2E_API_KEY
```

PowerShell：

```powershell
$env:CONTI_AGENT_E2E_API_KEY = "你的真实 API Key"
```

类 Unix：

```bash
export CONTI_AGENT_E2E_API_KEY="你的真实 API Key"
```

不要把 API Key 写进 `.toml`、README、session、audit 或示例配置。

### 0.3 准备独立测试工作区

不要在源码目录测试写入。

PowerShell：

```powershell
$root = Join-Path $env:TEMP "conti-live-e2e"
New-Item -ItemType Directory -Force -Path (Join-Path $root ".conti") | Out-Null
Set-Location $root
Copy-Item D:\path\to\conti-agent\examples\live.deepseek.toml .\.conti\config.toml
```

类 Unix：

```bash
root="${TMPDIR:-/tmp}/conti-live-e2e"
mkdir -p "$root/.conti"
cd "$root"
cp /path/to/conti-agent/examples/live.deepseek.toml ./.conti/config.toml
```

检查配置：

```bash
python -m conti_agent.cli --config .\.conti\config.toml config-check
```

类 Unix：

```bash
python -m conti_agent.cli --config ./.conti/config.toml config-check
```

预期输出：

```text
配置有效
```

## 1. 必测一：进入终端并真实对话

这是最终验收入口。

```bash
python -m conti_agent.cli --config .conti/config.toml chat
```

PowerShell：

```powershell
python -m conti_agent.cli --config .\.conti\config.toml chat
```

依次输入：

```text
请用一句中文介绍你自己。
我的代号是 blue-lantern，请只回复收到。
我们刚才约定的代号是什么？
/exit
```

通过标准：

1. 启动后显示 provider、model、权限和工作区；
2. 出现 `你 >`；
3. 助手输出带 `助手：` 标签；
4. 回答是流式输出；
5. 第二轮模型能回复 `收到`；
6. 第三轮模型能说出 `blue-lantern`；
7. `/exit` 正常退出；
8. 没有 traceback；
9. 没有 API Key 泄漏。

记录会话 ID：

```bash
python -m conti_agent.cli --config .conti/config.toml sessions
```

检查：

```powershell
Get-ChildItem .\.conti\sessions
```

类 Unix：

```bash
ls -l .conti/sessions
```

必须至少生成一个 `.jsonl` 会话。

## 2. 必测二：一次性真实模型调用

```bash
python -m conti_agent.cli --config .conti/config.toml ask "请只回复四个汉字：链路正常"
```

通过标准：

1. 输出包含 `链路正常`；
2. 退出码是 `0`；
3. 没有认证错误；
4. 没有网络代理或 SSL 错误。

## 3. 必测三：真实模型调用本地文件工具

创建样本：

PowerShell：

```powershell
New-Item -ItemType Directory -Force -Path src | Out-Null
Set-Content -Encoding utf8 src\sample.py @'
def hello():
    return "conti-live-ok"
'@
```

类 Unix：

```bash
mkdir -p src
cat > src/sample.py <<'EOF'
def hello():
    return "conti-live-ok"
EOF
```

运行：

```bash
python -m conti_agent.cli --config .conti/config.toml ask "请必须调用 workspace_read 读取 src/sample.py，然后告诉我 hello 函数返回的字符串。"
```

通过标准：

1. 回答包含 `conti-live-ok`；
2. `.conti/runtime/audit.jsonl` 有 `workspace_read`；
3. 对应审计事件的 `decision.allowed` 是 `true`。

检查：

```powershell
Get-Content .\.conti\runtime\audit.jsonl -Tail 5
```

类 Unix：

```bash
tail -n 5 .conti/runtime/audit.jsonl
```

## 4. 必测四：真实模型写入文件

```bash
python -m conti_agent.cli --config .conti/config.toml ask "请使用 workspace_write 创建 hello.txt，内容为：conti e2e"
```

通过标准：

1. `hello.txt` 存在；
2. 内容包含 `conti e2e`；
3. 审计有 `workspace_write` approved 记录。

## 5. 必测五：只读权限必须拒绝写入

把 `.conti/config.toml` 里的权限改为：

```toml
[runtime]
permission_mode = "read_only"
```

重新运行：

```bash
python -m conti_agent.cli --config .conti/config.toml ask "请使用 workspace_write 创建 denied.txt，内容为：should not exist"
```

通过标准：

1. `denied.txt` 不存在；
2. 模型收到拒绝；
3. 审计出现 denied。

测完把权限改回：

```toml
permission_mode = "workspace"
```

## 6. 必测六：Hook 拒绝写入

在测试目录追加 Hook：

```toml
[[hook]]
event = "tool.before"
match_tool = "workspace_write"
command = ["python", "D:/path/to/conti-agent/examples/hook_deny.py"]
timeout_ms = 5000
continue_on_error = false
```

Windows 中 `python` 建议写成绝对路径，例如：

```toml
command = ["D:/path/to/project/.venv/Scripts/python.exe", "D:/path/to/conti-agent/examples/hook_deny.py"]
```

运行：

```bash
python -m conti_agent.cli --config .conti/config.toml ask "请使用 workspace_write 创建 hook.txt，内容为：test"
```

通过标准：

1. `hook.txt` 不存在；
2. 模型报告 Hook 拒绝；
3. 审计出现 denied；
4. 没有绕过 Hook 直接写文件。

## 7. 必测七：外部工具

在 `.conti/config.toml` 追加：

```toml
[[external_server]]
name = "docs"
command = ["python", "D:/path/to/conti-agent/examples/external_echo_server.py"]
```

Windows 建议写绝对路径：

```toml
[[external_server]]
name = "docs"
command = ["D:/path/to/project/.venv/Scripts/python.exe", "D:/path/to/conti-agent/examples/external_echo_server.py"]
```

运行：

```bash
python -m conti_agent.cli --config .conti/config.toml ask "请必须调用 docs.echo 工具，参数 text 设置为 external-ok，然后返回它给出的完整结果。"
```

通过标准：

1. 模型调用的是 `docs.echo`；
2. 输出包含：

```text
external-echo:external-ok
```

3. 退出码是 `0`；
4. 退出后没有管道或子进程资源泄漏警告。

## 8. 必测八：危险命令拒绝

```bash
python -m conti_agent.cli --config .conti/config.toml ask "请使用 process_run 执行命令数组：['rm', '-rf', '.']"
```

通过标准：

1. 当前目录没有被删除；
2. 工具被拒绝；
3. 审计出现 denied。

## 9. 必测九：JSONL 事件

```bash
python -m conti_agent.cli --config .conti/config.toml ask "检查事件流" --event-format jsonl
```

通过标准：

1. 输出是多行 JSON；
2. 至少有 `run.started`；
3. 至少有 `message.created`；
4. 最后是 `run.completed`；
5. 每行都能被 JSON 解析。

PowerShell 快速验证：

```powershell
$lines = python -m conti_agent.cli --config .\.conti\config.toml ask "检查事件流" --event-format jsonl
$lines | ForEach-Object { $_ | ConvertFrom-Json } | Select-Object -ExpandProperty event
```

## 10. 必测十：配置错误退出码

临时移走配置：

PowerShell：

```powershell
Rename-Item .\.conti\config.toml config.toml.bak
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

通过标准：

1. 不执行任务；
2. 退出码是 `2`；
3. 错误信息清楚。

## 11. 发布前自动检查

在源码目录执行：

```bash
python -m unittest discover -s tests
```

再检查密钥格式：

```bash
git grep -nE "sk-[A-Za-z0-9]{16,}" -- .
```

通过标准：

1. 全部测试通过；
2. 没有密钥格式命中；
3. `git status --short` 干净。

## 12. 测试结果记录模板

每轮端到端测试记录：

```text
日期：
commit：
模型：
权限模式：
测试目录：

1. chat 流式多轮：passed / failed
2. ask：passed / failed
3. workspace_read：passed / failed
4. workspace_write：passed / failed
5. read_only deny：passed / failed
6. hook deny：passed / failed
7. external tool：passed / failed
8. dangerous command deny：passed / failed
9. JSONL events：passed / failed
10. config error exit code：passed / failed
11. unittest：passed / failed
12. secret scan：passed / failed
```

失败时补充：

```text
完整命令：
完整输出：
退出码：
审计最后 20 行：
会话文件：
根因：
修复 commit：
```

## 当前已知边界

1. `FakeProvider` 只适合离线冒烟，不作为真实 AI 验收。
2. `/compact` 当前使用确定性摘要，不是模型摘要；模型摘要计划在 WP-08B。
3. Anthropic-compatible 已能非流式对话，但流式仍属 WP-08A。
4. `process_run` 是应用级控制，不是完整 OS 级沙箱；OS 沙箱计划在 WP-08E。
5. `serve` 只绑定 loopback，尚未实现 token 鉴权；鉴权计划在 WP-08F。
