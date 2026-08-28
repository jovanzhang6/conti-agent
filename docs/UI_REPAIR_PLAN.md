# TUI 缺陷总结与修复策略

本文档记录 2026-08-28 用户反馈的全屏 TUI 问题。修复原则是：**先补运行时能力，再改视觉布局**。没有完成 M1 前，不继续给 TUI 增加新外观。

## 1. 缺陷清单

### A. 布局与滚动

#### UI-001 小窗口无法完整显示对话

**现象**

- 终端缩小时，对话内容被截断；
- Windows 窗口最大化后才可能看到完整内容；
- 继续发送消息后，最新内容仍然可能不完整。

**根因**

当前 `TuiState.render_conversation()` 渲染全部文本后交给 `ScrollablePane(Window(...))` 显示。窗口尺寸变化后没有稳定的 viewport 模型，也没有明确的最小高度、换行行数、滚动偏移和“跟随最新消息”状态。

**修复方向**

1. 建立显式 `ConversationViewport`；
2. 保存渲染行、`scroll_offset`、`follow_bottom`；
3. resize 时重新计算换行和可视行；
4. 新任务或流式输出时，只有用户没有主动上翻才跟随底部；
5. 用户上翻后进入 manual scroll 模式；
6. 低于最小尺寸时显示“窗口过小”页面，或自动切换 `--line`。

**验收**

1. 在 80x24、100x30、120x40、最大化之间切换，消息不丢；
2. 长回复可以翻页阅读；
3. 用户没有上翻时，新回复停在底部；
4. 用户上翻时，新回复不强行拉走视图；
5. 回到底部后继续跟随。

#### UI-002 右侧滚动条不能可靠拖动

**现象**

右侧滚动条可见，但不能稳定拖动或下拉。

**根因**

滚动位置被渲染逻辑强制到底，覆盖用户滚动意图；同时 `mouse_support=False`，也没有完整的 PageUp/PageDown/Home/End 绑定。

**修复方向**

1. 移除“每次渲染强制到底”；
2. 增加 `follow_bottom`；
3. 支持滚轮、拖动、PageUp/PageDown/Home/End；
4. 滚动条反映真实 viewport；
5. 提供“回到底部”快捷方式。

#### UI-003 终端过小时布局没有保护

**现象**

小窗口下 Header、对话流、输入框、侧栏互相挤压。

**修复方向**

1. 定义最低 TUI 尺寸；
2. 宽度不足自动收起侧栏；
3. 高度不足隐藏次要区域；
4. 小于最低尺寸提示或退回 `chat --line`；
5. resize 后重建 viewport。

### B. 运行时能力缺失

#### UI-010 没有 Slash 命令候选列表

**现象**

输入 `/` 后没有候选；用户必须记住命令。

**根因**

命令只是 `handle_command()` 的字符串分支，没有命令注册表，也没有给输入框接 `Completer`。

**修复方向**

1. 新建 `CommandRegistry`；
2. 每个命令注册名称、参数、帮助、是否需要空闲；
3. `/` 后显示候选；
4. 参数处显示候选值；
5. Tab、方向键、Enter 可选择；
6. `/help` 自动生成。

候选至少包括：

```text
/help
/models
/model <name>
/new
/status
/sessions
/resume <session-id>
/compact
/activity
/panel
/clear
/exit
```

#### UI-011 没有 `/models`

**现象**

无法查看模型列表，也无法切换模型。

**根因**

Runtime 只使用 `config.providers[0]`，没有 provider registry、active provider 状态和切换 API。

**修复方向**

1. Runtime 增加 provider registry；
2. `/models` 列出 provider、protocol、model、active、Key 状态；
3. `/model <name>` 切换；
4. busy 时禁止切换；
5. Header、输入框下方、侧栏、`/status` 同步；
6. Profile 子代理跟随 active provider；
7. session 元数据记录当时模型。

#### UI-012 命令逻辑和 TUI 耦合

**现象**

命令逻辑写在 `ContiTui.handle_command()`，HTTP、CLI、测试不能复用。

**修复方向**

1. 建立统一 `CommandRegistry`；
2. TUI、行式 REPL、HTTP 调用同一执行器；
3. 命令返回 `CommandResult`；
4. UI 只负责渲染。

#### UI-013 `/resume` 没有回填历史

**现象**

恢复会话后，TUI 只从新事件开始显示。

**修复方向**

1. Runtime 提供 resume API；
2. TUI 回填 user/assistant/system 消息；
3. 工具结果进入活动记录；
4. 显示“历史已回填”。

### C. 信息架构问题

#### UI-020 当前模型位置错误

**现象**

模型放在 Header，用户发消息时注意力要在顶部和底部来回移动。

**修复方向**

1. Header 只保留品牌；
2. 当前模型、权限、状态放输入框正下方；
3. 模型切换后立即同步；
4. Header 不显示 token、错误数、运行状态。

目标结构：

```text
┌ 对话流 ─────────────────────────────┐
│ ...                                 │
├ 任务输入 ───────────────────────────┤
│ >                                   │
├ Status ─────────────────────────────┤
│ deepseek-v4-flash │ workspace │ ready │
└─────────────────────────────────────┘
```

#### UI-021 右侧栏默认干扰注意力

**现象**

右侧栏固定展开，状态和活动过多。

**修复方向**

1. 默认收起；
2. `Ctrl+B` 或 `/panel` 切换；
3. 展开宽度固定或自适应；
4. 收起时状态合并到输入框下方；
5. 偏好保存到 `.conti/runtime/ui-state.json`。

#### UI-022 活动记录不可读

**现象**

出现类似 `15:32:34 工具请求 workspace_li` 的内容，用户不知道实际做了什么。

**根因**

侧栏直接渲染事件名和工具名，没有语义格式化器，也没有参数摘要。

**修复方向**

新增 `ActivityFormatter`，翻译成用户动作：

| 工具 | 用户可读文案 |
|---|---|
| `workspace_read` | 读取文件 `src/main.py` |
| `workspace_write` | 写入文件 `README.md` |
| `workspace_edit` | 编辑文件 `src/app.py` |
| `workspace_list` | 查看目录 `src/` |
| `workspace_search` | 搜索 `keyword` |
| `process_run` | 执行命令 `python -m pytest` |
| `request_input` | 等待你补充信息 |
| `task_note` | 保存任务笔记 |
| `spawn_task` | 派发子任务 |
| `load_skill` | 加载技能 |

每个动作分三段：

```text
开始读取 src/main.py
读取完成，0.2 秒
被权限阻止
```

侧栏只显示最近 5 条关键状态；完整记录用 `/activity`。

#### UI-023 Header / Footer / Sidebar 信息重复

**修复方向**

1. Header：只显示品牌；
2. 输入框下方：当前模型、权限、状态；
3. 侧栏：会话、token、最近活动；
4. Footer：快捷键；
5. `/status`：完整状态。

#### UI-024 缺少用户友好的错误提示

**修复方向**

1. 区分错误、警告、提示；
2. 错误显示在输入框下方 toast 区；
3. 保留最近一条；
4. 完整错误进入 activity / log。

### D. 其他基础交互缺口

#### UI-030 缺少命令历史

输入框应支持 Up / Down 浏览历史。

#### UI-031 缺少鼠标滚轮

TUI 必须支持滚轮滚动对话。

#### UI-032 缺少完整活动视图

应增加 `/activity`，显示本次任务完整工具过程。

#### UI-033 缺少面板状态记忆

侧栏展开 / 收起和窗口偏好应保存。

#### UI-034 UI 状态与 Runtime 状态耦合

应拆分：

```text
RuntimeState：模型、权限、session、busy
ConversationState：消息、viewport、scroll
ActivityState：工具过程
CommandState：命令、历史、候选
```

#### UI-035 approved 权限模式缺少 TUI 审批交互

先补 Runtime 审批 API，再实现 TUI 弹窗。

## 2. 修复顺序

### M1：先补核心能力

1. `CommandRegistry`；
2. Provider registry；
3. `/models` 和 `/model`；
4. `ActivityFormatter`；
5. Session resume API；
6. Runtime approval API；
7. Runtime state 分离。

**完成标准**

- `/models` 和 `/model` 在行式模式也可用；
- 命令候选来自 Runtime；
- 工具活动可格式化成用户可读语句；
- 既有测试全部通过。

### M2：修复滚动和 resize

1. `ConversationViewport`；
2. 停止强制底部；
3. PageUp/PageDown/Home/End；
4. 滚轮；
5. 滚动条拖动；
6. 小窗口响应式。

### M3：重构信息架构

1. Header 精简；
2. 模型和状态移到输入框下方；
3. 侧栏默认收起；
4. Slash 候选；
5. 命令历史；
6. `/activity`。

### M4：回归与发布

1. 行式模式回归；
2. TUI 测试回归；
3. 真实模型回归；
4. Python 入口回归；
5. exe 重建；
6. exe 真实 TUI 测试；
7. 更新 E2E 和 RELEASE 文档。

## 3. 明确不做的事

1. 不再在旧 `handle_command()` 里堆命令；
2. 不让 TUI 直接决定 provider；
3. 不把未格式化事件名直接给用户；
4. 不无条件强制滚动到底部；
5. M1 未完成前不加新视觉装饰。

## 4. 验收矩阵

| 编号 | 场景 | 通过标准 |
|---|---|---|
| R-001 | 80x24 | 输入框可见，消息可翻页 |
| R-002 | 最大化 | resize 后消息完整 |
| R-003 | 长回复 | PageUp/PageDown 可读 |
| R-004 | 手动上翻 | 新消息不抢滚动位置 |
| R-005 | 回到底部 | 恢复跟随最新消息 |
| R-006 | `/` | 弹出命令候选 |
| R-007 | `/models` | 列出全部 provider/model |
| R-008 | `/model` | 切换模型成功 |
| R-009 | 模型切换 | 输入框下方立即同步 |
| R-010 | 侧栏 | 默认收起，可切换 |
| R-011 | 工具活动 | 显示“读取文件 / 执行命令”等文案 |
| R-012 | `/resume` | 历史消息回填 |
| R-013 | busy | 禁止切换模型，可取消任务 |
| R-014 | line 模式 | 核心命令同样可用 |
| R-015 | exe | 打包后全部回归通过 |

## 5. 当前优先级

1. P0：UI-010、UI-011、UI-012、UI-001、UI-002；
2. P1：UI-020、UI-021、UI-022、UI-013；
3. P2：UI-030、UI-031、UI-032、UI-033；
4. P3：UI-035、UI-024、UI-034。

## 6. 当前进度

已完成：

1. `CommandRegistry`；
2. `/models`；
3. `/model <name>`；
4. Slash 命令候选；
5. `/model <name>` 参数候选；
6. TUI / 行式模式共用命令执行器；
7. Runtime busy 状态和模型切换保护。
8. `ActivityFormatter`。
9. `/activity` 当前界面活动列表。

进行中：

1. ConversationViewport；
2. 侧栏可收起已有基础实现，交互打磨未完成；
3. 模型和状态已移到输入框下方，但滚动行为仍需按 M2 重做。
