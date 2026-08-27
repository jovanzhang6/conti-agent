# conti-agent 学习路线

## 第 1 天：核心类型

阅读：

1. `src/conti_agent/messages.py`
2. `src/conti_agent/events.py`
3. `src/conti_agent/tools.py`
4. `tests/test_core.py`

目标：

- 理解消息如何组成对话；
- 理解 ToolCall 和 ToolResult；
- 理解事件为什么统一成 JSON。

## 第 2 天：Agent 循环

阅读：

- `src/conti_agent/agent.py`
- `src/conti_agent/providers.py`
- `tests/test_core.py`

重点问题：

1. Provider 为什么要注入 transport？
2. 工具结果为什么要作为 tool 消息回传？
3. `max_tool_iterations` 如何防止失控？
4. 哪些 Provider 错误适合重试？

## 第 3 天：安全链

阅读：

- `src/conti_agent/workspace.py`
- `src/conti_agent/permissions.py`
- `tests/test_safety_state.py`

练习：

1. 添加禁止编辑 `.env` 的规则；
2. 在 `read_only` 模式观察写入被拒绝；
3. 查看 `.conti/runtime/audit.jsonl`。

## 第 4 天：会话与上下文

阅读：

- `src/conti_agent/sessions.py`
- `src/conti_agent/context.py`
- `src/conti_agent/runtime.py`

重点问题：

1. 为什么压缩不删除原始账本？
2. token 估算为什么保守？
3. 恢复会话时如何处理摘要？

## 第 5 天：扩展机制

阅读：

- `src/conti_agent/config.py`
- `src/conti_agent/skills.py`
- `src/conti_agent/hooks.py`
- `src/conti_agent/profiles.py`
- `src/conti_agent/external.py`
- `tests/test_extensions.py`

练习：

1. 写一个 Skill；
2. 写一个 Hook 拒绝 `rm -rf`；
3. 定义只读 Profile；
4. 用 Fake Connector 理解外部工具协议。

## 第 6 天：运行时组合

阅读：

- `src/conti_agent/runtime.py`
- `src/conti_agent/cli.py`
- `src/conti_agent/service.py`
- `tests/test_integration.py`

重点问题：

1. Runtime 如何避免 CLI 和 HTTP 各实现一套策略？
2. `/compact` 改写了什么，没改写什么？
3. JSONL 事件如何用于调试？

## 第 7 天：小型迭代

选择一个任务：

1. 给搜索增加 glob 过滤；
2. 给进程工具增加命令白名单；
3. 给任务板增加 worker 心跳；
4. 给外部工具增加超时配置；
5. 给 Profile 增加输出格式约束。

要求：

1. 先写失败测试；
2. 最小实现；
3. 用中文更新文档；
4. 单独提交。

## 常用调试命令

```bash
python -m unittest discover -s tests
python -m conti_agent.cli --config .conti/config.toml ask "列出当前项目结构"
python -m conti_agent.cli --config .conti/config.toml sessions
python -m conti_agent.cli --config .conti/config.toml ask "检查测试" --event-format jsonl
```
