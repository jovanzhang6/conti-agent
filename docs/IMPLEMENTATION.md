# conti-agent 分阶段实现记录

本文档记录实施路线、验收口径和当前完成状态。每个阶段都有独立提交和测试。

## 阶段 0：独立仓库与设计基线

已完成：

- 建立 `src/conti_agent` 包结构；
- 持久化功能规格；
- 持久化分阶段实施计划；
- 使用标准库 `unittest`；
- 采用 MIT 许可；
- 不引入 TUI、Logo 或继承视觉风格。

验证：

```bash
python -m unittest discover -s tests
```

## 阶段 1：确定性核心

文件：

- `messages.py`
- `events.py`
- `schema.py`
- `tools.py`
- `providers.py`
- `agent.py`

实现：

- 消息、工具调用和用量模型；
- JSON 参数校验；
- 工具注册表和 `ToolResult`；
- OpenAI-compatible、Anthropic-compatible、Fake Provider；
- 注入式 HTTP transport；
- 事件流 Agent 循环；
- 重试和最大迭代限制。

验收：

- Fake Provider 可直接回答；
- 可完成一次工具调用；
- 超过迭代上限会失败；
- 瞬时错误会重试；
- 两种 wire format 有映射测试。

## 阶段 2：本地工作区工具

文件：

- `workspace.py`
- `tools_local.py`

实现：

- 路径归一化和边界检查；
- 读、写、精确编辑、列表、搜索；
- CRLF/LF 保留；
- 依赖目录和隐藏文件忽略；
- 进程超时、输出截断和环境策略。

验收：

- 路径越界失败；
- 歧义编辑不写入；
- 超时终止进程；
- 环境变量只有显式继承才可见；
- CRLF 文件编辑后保留 CRLF。

## 阶段 3：安全、会话与上下文

文件：

- `permissions.py`
- `sessions.py`
- `context.py`

实现：

- 四种权限模式；
- 规则引擎和优先级；
- 危险命令检测；
- 路径沙箱；
- 批准回调；
- JSONL 审计；
- 追加式会话账本；
- 上下文预算和历史压缩。

验收：

- 拒绝的工具不执行；
- 审计不记录敏感内容；
- 本地规则覆盖项目规则；
- approved 模式同一能力只批准一次；
- 会话可重建消息列表。

## 阶段 4：扩展机制

文件：

- `config.py`
- `skills.py`
- `hooks.py`
- `profiles.py`
- `external.py`

实现：

- TOML Provider/Runtime/Profile/Hook/External Server；
- 配置合并和环境变量密钥；
- Skill 元数据与显式加载；
- Hook 超时、失败拒绝和输出替换；
- Profile 子代理；
- 外部 JSON-RPC 工具命名空间。

验收：

- 配置可解析、合并、校验；
- Skill 错误会失败；
- Hook 失败不会放行；
- Profile 子任务独立运行；
- 外部工具可用 fake connector 测试。

## 阶段 5：运行时门面与产品入口

文件：

- `runtime.py`
- `tools_misc.py`
- `collab.py`
- `snapshots.py`
- `service.py`
- `cli.py`

实现：

- Runtime 门面；
- Provider 工厂；
- 自动上下文压缩；
- 会话审计和恢复；
- Skill/任务笔记/请求输入工具；
- 协作任务板和邮箱；
- Git 快照；
- `ask`、`chat`、`sessions`、`config-check`、`worker`、`serve`；
- 本地 HTTP 服务。

验收：

- CLI 支持文本和 JSONL；
- REPL 可压缩退出；
- 服务请求返回事件；
- 任务板可持久化；
- 非 Git 仓库不能创建快照。

## 阶段 6：学习与发布

已完成：

- 中文 README；
- 中文架构说明；
- 中文学习路线；
- 示例配置；
- 发布检查清单；
- 全量测试。

发布前检查：

```bash
python -m unittest discover -s tests
git status --short
git log --oneline
```

条件：

1. 全部测试通过；
2. 无第三方运行时依赖；
3. 文档、注释和示例一致；
4. 无明文密钥；
5. 无未解释的破坏性操作；
6. 标记 `v0.1.0`。
