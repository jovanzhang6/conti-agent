# WP-11 续工交接：TUI 滚动、Resize 与模型切换历史

> **状态更新（2026-08-28）：本文档描述的目标 1/2/3 与 Step 1-6 已全部实现并验证。**
>
> - 单元测试 64 个全部通过（新增 viewport / model-switch / session 事件 / resume 回填覆盖）；
> - 真实模型验证：`fake↔deepseek` 往返切换后，真实模型凭同一 session 历史答出约定代号；
> - session JSONL 记录 `session.started`（provider/model）+ `model.switched` 轨迹；
> - `/resume` 历史回填已实现（`Runtime.load_session_history` + TUI 回填 + 行式提示）；
> - 真实终端手测：小窗口布局、流式对话、PageUp/Home/End/Ctrl+Up、
>   `/model` 切换提示、Ctrl+Q 退出恢复全部通过；
> - 实现要点与“为什么必须这样做”（prompt_toolkit wrap 模式的游标驱动滚动、
>   vertical_scroll 粘性、片段缓存按 render_counter）记录在
>   `docs/IMPLEMENTATION.md` 的 WP-11B / WP-11C 小节；
> - 遗留：鼠标滚轮与滚动条拖动的真实终端点击级验证（事件链路已按原生分发路径
>   无头验证）、小窗口“降级到行式模式”（已实现提示页）、WP-11D 信息架构与 WP-09E 压测。
>
> 以下原文保留作为方案与验收标准记录。

这份文档是独立交接说明。新会话只需先读这份文件，就可以继续当前工作，不需要重新推导上下文。

## 1. 当前状态

截至交接：

- `WP-11A CommandRegistry` 已完成；
- `WP-11B Provider Registry` 已完成；
- `WP-11E ActivityFormatter` 已完成基础版；
- `WP-11C ConversationViewport` 未完成；
- `WP-11D 信息架构重构` 部分完成；
- 最新实现提交是 `ca735a0 feat: add command registry model switching and readable activity`；
- `v0.1.0` tag 指向较早的本地配置支持提交，不一定等于当前 HEAD。

当前全量测试：

```text
Ran 51 tests
OK
```

当前目录：

```text
D:\conti-agent\conti-agent
```

本地 exe：

```text
dist\conti-agent.exe
```

本地未提交配置：

```text
.conti/config.local.toml
```

该文件包含本地 API Key，已被 `.gitignore` 忽略。不要把其中的 Key 写入任何 Git 跟踪文件、日志、测试输出或交接文档。

## 2. 用户反馈的问题

### 问题 A：缩放后对话显示不完整

用户观察到：

1. 终端缩小时，对话内容显示不完整；
2. Windows 窗口最大化后才能看到完整内容；
3. 继续发送新对话后，内容又可能显示不全；
4. 右侧滚动条无法稳定拖动下拉。

当前根因在 `src/conti_agent/tui.py`：

```python
def _conversation_scroll(self, window: Any) -> int:
    info = window.render_info
    return max(0, int(info.content_height) - int(info.window_height))
```

这个函数每次都把滚动位置强制到底部。它解决了“最新消息看不见”的问题，但破坏了用户主动上翻、拖动滚动条和 resize 后保持阅读位置的能力。

另外，当前没有独立的 viewport 状态：

- 不知道用户是否主动上翻；
- 不知道当前滚动偏移；
- 不知道 resize 后应保留哪个锚点；
- 无法区分“跟随最新消息”和“手动阅读历史”。

### 问题 B：切换模型后历史体验不完整

用户需要切换模型，但当前体验不能明确保证“切换模型后对话历史继续存在”。

当前实现：

1. `/model <name>` 调用 `Runtime.set_active_provider(name)`；
2. 该方法只替换 active provider，不清理 TUI 消息；
3. 如果 TUI 当前已有 `session_id`，下一次 `Runtime.ask(..., session_id=...)` 会从 `SessionStore` 加载旧消息；
4. 但如果仍是“新会话”，还没有第一条用户消息，就没有 session id，也没有磁盘历史锚点。

仍然存在的真实缺口：

1. TUI `/resume` 没有回填历史消息；
2. session 账本没有记录模型切换事件；
3. 切换模型后，对话流没有系统提示说明“历史会继续保留”；
4. 用户无法确认下一次请求确实带着旧上下文；
5. session 元数据没有记录当前 provider/model 和模型切换轨迹。

因此下一阶段必须实现“模型切换保留并可视化历史”。

## 3. 当前相关架构

### Runtime

文件：`src/conti_agent/runtime.py`

关键状态：

```python
self.provider_configs = {item.name: item for item in config.providers}
self.provider_config = config.providers[0]
self.provider = create_provider(self.provider_config)
self.profile_runner = ProfileRunner(...)
self.context_manager = ContextManager(...)
```

关键方法：

```python
Runtime.list_providers()
Runtime.get_provider_info(name)
Runtime.active_provider_name()
Runtime.set_active_provider(name)
Runtime.ask(prompt, session_id=..., text_callback=..., event_callback=...)
```

`set_active_provider()` 当前行为：

1. `busy` 时拒绝；
2. 未知 provider 拒绝；
3. 创建新 provider 成功后才替换；
4. 替换 `self.provider`；
5. 替换 `self.profile_runner.provider`；
6. 调用 `_update_context_window(config)`。

它当前不会创建新 session，也不会清空 TUI 消息。

### 命令层

文件：`src/conti_agent/commands.py`

`CommandRegistry` 已经统一处理：

```text
/help
/models
/model <name>
/status
/sessions
/resume <session-id>
/compact
/activity
/panel
/new
/clear
/exit
```

`/model` handler 当前调用：

```python
context.runtime.set_active_provider(name)
```

成功后返回 `CommandResult`，TUI 更新 `state.runtime_info`。

### TUI

文件：`src/conti_agent/tui.py`

关键对象：

```python
ContiTui.state
ContiTui.sidebar_visible
ContiTui.conversation_control
ContiTui.input_control
ContiTui.command_registry
ContiTui.current_task
```

关键方法：

```python
TuiState.render_conversation()
ContiTui._conversation_scroll()
ContiTui.handle_command()
ContiTui.run_prompt()
ContiTui._ask_runtime()
```

当前对话渲染仍然是一次性生成完整 fragments，再交给：

```python
ScrollablePane(Window(...))
```

显示。

当前侧栏默认收起：

```python
self.sidebar_visible = False
```

`/panel` 或 `Ctrl+B` 可以切换。

### Session

文件：`src/conti_agent/sessions.py`

当前 JSONL 记录类型：

```text
session.started
message.appended
history.compacted
```

`SessionStore.load(session_id)` 可以还原消息。

当前限制：

1. session 元数据没有记录 provider/model；
2. 没有模型切换事件；
3. TUI `/resume` 没有把历史消息回填到 `state.messages`；
4. 新会话还没有第一条用户消息时没有 session id，因此模型切换前没有磁盘历史锚点。

## 4. 下一阶段目标

### 目标 1：实现 ConversationViewport

目标是把“全部渲染 + 强制滚到底”改成显式视口状态。

建议新增：

```python
@dataclass
class ViewportState:
    scroll_offset: int = 0
    follow_bottom: bool = True
    content_height: int = 0
    window_height: int = 0
```

放在 `ContiTui` 或 `TuiState` 中：

```python
self.viewport = ViewportState()
```

核心规则：

1. `follow_bottom == True` 时，每次渲染后显示最底部；
2. 用户 PageUp / 滚轮上 / 拖动滚动条向上时，设置：

```python
viewport.follow_bottom = False
```

3. 用户 PageDown 到底、滚轮到底、拖到底、按 End 时，设置：

```python
viewport.follow_bottom = True
```

4. 用户发送新消息时，重新设为 `follow_bottom = True`；
5. assistant 流式输出时，如果已经在底部则继续跟随；如果用户在上方阅读，则不要跳到底部；
6. resize 时重新计算 `content_height`，并尽量保留当前阅读锚点；
7. `_conversation_scroll()` 不再无条件返回底部。

建议改法：

```python
def _conversation_scroll(self, window):
    info = window.render_info
    content_height = int(info.content_height)
    window_height = int(info.window_height)
    max_scroll = max(0, content_height - window_height)

    self.viewport.content_height = content_height
    self.viewport.window_height = window_height

    if self.viewport.follow_bottom:
        return max_scroll

    return max(0, min(self.viewport.scroll_offset, max_scroll))
```

同时提供：

```python
scroll_up(amount=5)
scroll_down(amount=5)
scroll_to_top()
scroll_to_bottom()
```

键盘推荐：

```text
PageUp       向上翻页
PageDown     向下翻页
Home         回到顶部
End          回到底部
Alt+Up       上滚
Alt+Down     下滚
```

不要让普通 `Up` / `Down` 抢走输入框的光标移动。

### 目标 2：切换模型保留历史

“切换模型”不应该等于“开新会话”。

必须实现：

1. 已有 session id 时，切换模型后继续使用同一个 session id；
2. TUI 消息列表不清空；
3. 下一次请求继续加载同一个 session 账本；
4. session 账本追加模型切换事件；
5. TUI 对话流显示系统提示：

```text
已切换到 deepseek-v4-pro。当前会话历史继续保留。
```

6. session 元数据记录模型轨迹；
7. `/status` 显示当前模型和当前 session；
8. `/models` 显示 active 状态。

### 目标 3：新会话切换模型时提供 session 锚点

当前只有发送第一条消息后才会创建 session。  
这导致在新会话里切换模型时没有磁盘 session 可记录。

建议：

1. `/model <name>` 在还没有 session id 时创建轻量 session；
2. session 起始记录增加：

```json
{
  "provider": "deepseek",
  "model": "deepseek-v4-flash"
}
```

3. 后续模型切换追加：

```json
{
  "kind": "model.switched",
  "from_provider": "deepseek",
  "from_model": "deepseek-v4-flash",
  "to_provider": "deepseek-v4-pro",
  "to_model": "deepseek-v4-pro"
}
```

JSONL 每行必须是合法 JSON，不要使用 TOML 写事件。

## 5. 推荐实现步骤

### Step 1：扩展 SessionStore

文件：`src/conti_agent/sessions.py`

1. `create()` 增加 metadata 参数；
2. `session.started` 保存初始 provider/model；
3. 新增 `append_model_switch()`；
4. 新增通用 `append_event()`；
5. `load()` 兼容旧账本：旧 session 没有 provider/model 字段时不得报错；
6. 暂时保持 `SESSION_SCHEMA_VERSION = 1`，除非确有必要升级。

### Step 2：Runtime 保存切换上下文

文件：`src/conti_agent/runtime.py`

修改：

```python
def set_active_provider(
    self,
    name: str,
    *,
    session_id: str | None = None,
) -> None:
```

行为：

1. 切换前记录 `from_provider` / `from_model`；
2. 创建并切换 provider；
3. 如果 `session_id` 存在，调用：

```python
self.sessions.append_model_switch(...)
```

4. 返回切换结果字典，方便命令层显示。

`set_active_provider()` 和 `SessionStore._append()` 目前都是同步方法，这里可以直接调用，不需要改成 async。

### Step 3：命令层传递 session

文件：`src/conti_agent/commands.py`

修改 `model_handler()`：

```python
session_id = context.session_id
context.runtime.set_active_provider(name, session_id=session_id)
```

输出：

```text
模型已切换为 xxx。当前会话历史继续保留。
```

如果 `context.session_id is None`，输出：

```text
模型已切换为 xxx。发送第一条消息后开始保存会话。
```

### Step 4：TUI 保持消息

文件：`src/conti_agent/tui.py`

确认 `handle_command()` 的 `/model` 分支没有：

```python
self.state.messages.clear()
```

也不应该设置：

```python
self.state.session_id = "新会话"
```

除非用户显式执行 `/new`。

切换成功后追加系统消息：

```text
已切换到 <model>。当前会话历史继续保留。
```

### Step 5：实现 Viewport

文件：`src/conti_agent/tui.py`

新增 `ViewportState`，替换 `_conversation_scroll()`。

不要再使用当前这段逻辑作为最终方案：

```python
return max(0, int(info.content_height) - int(info.window_height))
```

必须加入 `follow_bottom` 和 `scroll_offset`。

### Step 6：`/resume` 历史回填

文件：

- `src/conti_agent/runtime.py`
- `src/conti_agent/tui.py`

推荐给 Runtime 增加：

```python
def load_session_history(self, session_id: str) -> list[dict[str, Any]]:
    _, messages = self.sessions.load(session_id)
    return messages
```

TUI `/resume` 后：

1. 清空当前显示；
2. 遍历历史消息；
3. `user` / `assistant` 消息加入对话；
4. `tool` 消息转成 activity；
5. 显示“历史已回填”。

## 6. 测试计划

### 单元测试

新增或扩展：

```text
tests/test_commands.py
tests/test_sessions.py
tests/test_tui.py
```

至少覆盖：

1. `/models` 列出全部 provider；
2. `/model <name>` 切换成功；
3. busy 时 `/model` 拒绝；
4. 未知 provider 拒绝；
5. 切换 provider 后 `profile_runner.provider` 更新；
6. 切换 provider 后 context window 更新；
7. session 记录 `model.switched`；
8. 旧 session 没有 provider 元数据也能加载；
9. `follow_bottom` 初始为 True；
10. 手动上翻后 `follow_bottom` 为 False；
11. 到底部后 `follow_bottom` 恢复 True；
12. resize 不导致消息丢失。

### 手工 TUI 测试

使用真实终端，不要用普通管道测全屏 TUI：

```powershell
cd D:\conti-agent\conti-agent
.\dist\conti-agent.exe chat --tui
```

场景：

1. 发送：`我的代号是 blue-lantern，请只回复收到。`
2. 切换模型：`/model deepseek-v4-pro`
3. 发送：`我们刚才约定的代号是什么？`
4. 预期新模型能回答 `blue-lantern`；
5. 对话流显示模型切换提示；
6. 上翻查看历史时，新消息不强行拉到底部；
7. `Ctrl+Q` 后终端恢复正常。

### 真实模型测试

本地已有配置：

```text
.conti/config.local.toml
```

不要输出其中 Key。

测试：

```powershell
.\dist\conti-agent.exe ask "请只回复四个汉字：本地就绪"
```

再测试多 provider：

```powershell
.\dist\conti-agent.exe chat --line
```

输入：

```text
/models
/model <另一个名字>
/models
/exit
```

## 7. 构建注意

构建脚本：

```powershell
$env:PYTHON='D:\conti-agent\.venv\Scripts\python.exe'
pwsh -NoProfile -ExecutionPolicy Bypass -File scripts\build_windows_exe.ps1
```

产物：

```text
dist\conti-agent.exe
```

如果遇到 PyInstaller `PermissionError`：

1. 先确认没有残留 `python.exe` / `conti-agent.exe`；
2. 不要同时开两个构建；
3. 关闭可能锁住 `dist\conti-agent.exe` 的终端；
4. 优先使用脚本里的独立 `build-release` 路径；
5. 不要盲目重装依赖。

## 8. 完成定义

本次续工完成必须满足：

1. 51+ 个既有测试全部通过；
2. 新增 viewport / history / model-switch 测试通过；
3. 小窗口、正常窗口、最大化三种尺寸都手测；
4. PageUp/PageDown/Home/End 可用；
5. 鼠标滚轮可用；
6. 用户上翻时新消息不抢视图；
7. 模型切换后同一 session 继续保留历史；
8. session JSONL 记录模型切换事件；
9. `/status`、`/models`、TUI 状态行显示一致；
10. 重建 exe 后真实模型测试通过；
11. `.conti/config.local.toml` 中的 Key 不进入 Git。

## 9. 明确不要做

1. 不要在 `/model` 时清空 `TuiState.messages`；
2. 不要把切换模型实现成 `/new`；
3. 不要删除现有 session JSONL；
4. 不要把 API Key 写入 Git；
5. 不要把 schema version 直接升到 2，除非确有必要；
6. 不要让 TUI 自己维护第二份会话真相；
7. 不要继续把所有命令塞进 `handle_command()`；
8. 不要用裸字符串判断新增命令，应扩展 `CommandRegistry`。
