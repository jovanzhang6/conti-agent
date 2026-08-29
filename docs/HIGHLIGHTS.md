# 亮点进阶

本文档记录 conti-agent 的差异化亮点：每一项先对标生产级 agent 的实现，
再确定我们自己的实施方案，最后统一落地。每项亮点独立成节，含调研、
选型与实施计划。

- 亮点 1：上下文压缩（本文档，方案已选定，待实施）
- 后续候选：会话检索、任务评估基准、子代理并行……（陆续补充）

---

## 亮点 1：上下文压缩

### 1.1 现状（已实现）

两层防线 + 兜底，检查点为"每次发请求前"：

```
检查点（pre_request_hook，agent 循环内）
  ├─ 第一层：工具结果落盘（与阈值无关）
  │    单结果 >50K 字符或单轮累计 >200K → 写 .conti/spill/，
  │    上下文只留 2K 预览 + 文件路径（不调模型、不丢信息）
  ├─ 第二层：模型压缩
  │    投影 > 窗口 − max_output_tokens − 10%×窗口 时触发；
  │    模型生成摘要，保留约 10K token 近期原文，
  │    切点回退 user 边界（tool 配对不被切断）
  └─ 兜底
       finish_reason=length（截断型）→ 丢残缺回复，强制压缩重生成
       上下文超限错误（拒绝型）→ 静默压缩重试一次
```

支撑机制：精确基线（服务商返回的 prompt/completion tokens）+ 增量估算
（CJK 感知：宽字符 1 token/字，其余 4 字符/token）；压缩后精确基线失效、
下次响应自动恢复；账本记录压缩标记与摘要，原始消息永不删除。

### 1.2 四家生产级 agent 的策略调研

参考实现：`D:\coding-agent\terminal-agent-references\` 下的 codex（OpenAI，
Rust）、deepseek-harness（TS）、opencode（TS）、pi（TS）。

#### 触发时机与阈值

| 实现 | 检查时机 | 阈值 |
|---|---|---|
| codex | 每轮采样前 + 每轮循环中 + 手动 + 模型切换到更小窗口 | `窗口 × 90%`，可与配置项取小；超限错误不当场压缩，记满用量由下一轮前置检查触发 |
| deepseek-harness | 每步前（`agent/pre-step`）+ 溢出错误重试 | `窗口 × 80%` 压力阈值；保留 `窗口 × 16%` verbatim 尾部 |
| opencode | 每轮 assistant 结束 + 流式中途 + 溢出错误兜底 | `tokens.total ≥ usable`（model 输入上限 − 20K 缓冲） |
| pi | agent 结束后 + 提交新 prompt 前（兜住被中断的响应） | `tokens > 窗口 − 16384`（固定预留，非百分比） |
| **conti-agent** | **每次发请求前（agent 循环内统一 hook）** | **`窗口 − max_output − 10%×窗口`（≈92%）** |

各家都同时响应"上下文超限错误"做兜底压缩；pi 额外覆盖三种溢出形态
（错误消息正则 30+ 条、静默溢出 input>窗口、length 截断且 output=0）。

#### 压缩执行

| 实现 | 摘要生成 | 历史重组 | 保留策略 |
|---|---|---|---|
| codex | 模型生成 handoff 摘要（"另一个模型将接手任务"），前缀 `SUMMARY_PREFIX` | **只保留用户消息**（最新往旧，预算 20K，超长截断），工具调用/输出/assistant 全部丢弃，工具状态由 world_state 系统重建；摘要以 user 角色置末 | mid-turn 摘要插在最后一条真实 user 消息前（模型训练预期） |
| deepseek-harness | 模型生成 8 节固定结构（Intent/Concepts/Files/Errors/Pending/Current/Next/Critical）；**摘要请求 = 上次请求的真前缀 + 末尾压缩指令，命中 KV cache** | 选区 `replace` 为一条 user 角色 checkpoint 消息（`<compacted-summary>` 标签包裹）；shadowed 原文留账本 | 尾部按 `retainTokens`（16% 窗口）verbatim 保留 |
| opencode | 专用隐藏 agent（工具全禁），固定模板（Objective/Details/Work State/Next Move/Files）；prior-summary 增量合并（"对话比摘要新，冲突以对话为准"） | **原始消息不覆盖**，读取侧 `filterCompacted` 重组视图 `[compaction-user, summary-assistant, 尾部, 续接user]`；overflow 后插入 synthetic user "Continue if you have next steps…" | 2K~15K token（usable×25%），turn 内二分切分 |
| pi | 模型生成固定节（Goal/Constraints/Progress/Decisions/Next/Critical），输出上限 0.8×预留；**摘要末尾追加 `<read-files>`/`<modified-files>` 文件操作清单**（从历史工具调用提取，跨压缩继承） | compaction entry + 从切点到压缩前被保留的条目重组；摘要转 user 角色消息（`<summary>` 标签包裹） | 20K token，切点永不落在 toolResult 上 |
| **conti-agent** | 模型生成自由摘要 + 固定规则回退 | `[system, 摘要(user), 近期原文]`，切点回退 user 边界 | 约 10K token 近期原文 |

#### 避免压缩带来问题的手段

| 手段 | codex | deepseek | opencode | pi | conti-agent |
|---|---|---|---|---|---|
| tool 配对保护 | 回填历史时工具项一律丢弃（world_state 重建） | **切点平衡器**：不得穿越未应答的 tool call | 悬空 tool_use 转 "[interrupted]" 文本 | 切点永不落在 toolResult | 切点回退 user 边界；中断时合成 tool_result |
| 压缩期间新事件 | pending 输入推迟到压缩后 | 自动路径要求 surface 整体不变；手动路径只要求选区稳定；**压缩锁** | 压缩任务队列化，流被截断 | **压缩中禁止提交 prompt** | hook 与请求串行（自动路径天然安全） |
| 摘要质量守卫 | 空摘要回退占位 | **摘要 ≥ 原文则拒绝**；空/超限/含图 fail-closed | 摘要请求自身溢出→明确报错 | length 截断/含 toolCall 判失败 | 无（来者不拒） |
| 免模型的预削减 | 工具输出写入历史时按策略截断 | **工具结果 >8K 先剪（头 4K+尾 1K）再复测，可能免掉模型压缩** | prune：旧工具输出只清输出留调用（默认关） | 工具输出进上下文前限 2000 行/50KB | 落盘层（50K/200K 阈值） |
| 失败回退 | 压缩失败注入 2K 兜底提示；模型回退重试 | 溢出恢复限次；失败不中断回合 | 明确报错停止 | 失败不改历史；stale usage 防误触发 | 静默压缩重试一次 |
| 多次压缩提醒 | **警告用户"多次压缩降准确率，建议开新会话"** | — | — | — | — |

#### 与四家的差异总结

我们已对齐：两层防线、检查点频率、推导式阈值、切点协议保护、
截断/超限双兜底、精确用量基线。
明显差距（按含金量）：① 摘要请求未做前缀复用（cache miss，压缩成本高）；
② 摘要无结构化模板（信息保真度靠运气）；③ 压缩期间无锁（并发一致性）；
④ 无 split-turn / 质量守卫 / 文件清单继承 / stale 防护（次优先）。

### 1.3 选定方案（本轮实施范围）

在现有两层策略之上叠加三项，全部围绕"压缩执行质量"：

#### A. KV cache 前缀复用（对标 deepseek-harness）

**问题**：当前摘要调用用空 registry + 独立提示词，与主请求的前缀完全
不同 → 服务商 prompt cache 全部 miss。摘要输入≈触发点大小（可达窗口
90%+），每次压缩都是全价长请求。

**方案**：摘要请求重放 `[真实 system prompt, 相同工具 schema, 被压缩的
旧消息原文]`，把压缩指令作为**最后一条 user 消息**追加——与最近一次
路由请求构成真正的公共前缀，命中服务商 KV cache（DeepSeek 缓存命中
价格约为全价的 1/10）。

**关键约束**：
- 工具 schema 必须原样传入（它在前缀里），但设 `tool_choice: "none"`
  禁止摘要模型实际调用工具；`tool_choice` 是请求参数、不属于消息前缀，
  不影响缓存命中；
- 被压缩区域的消息必须与主请求中逐字节一致（不做任何序列化改写），
  否则前缀断裂；
- 摘要输出上限沿用 `max_output_tokens`。

#### B. 结构化摘要模板（对标四家通用做法 + pi 的文件清单）

**问题**：当前提示词让模型自由发挥，压缩后"丢什么留什么"不可控、
不可测。

**方案**：压缩指令要求输出固定 Markdown 结构：

```text
## 目标与约束
## 关键决策
## 文件与代码        （精确路径、函数名、签名）
## 错误与修复
## 当前进度          （已完成 / 进行中 / 受阻）
## 下一步
## 关键上下文
```

规则：保留精确文件路径/命令/错误串/标识符/数值；已有
`<compacted-summary>` 时视为前次摘要，**增量合并**（对话内容比旧摘要
新，冲突以对话为准）；输出包裹在 `<compacted-summary>` 标签中便于识别；
不提及压缩行为本身。

#### C. 压缩期间禁止新请求的锁（对标 deepseek-harness / pi）

**问题**：手动 `/compact` 与运行中任务可并发——压缩的是账本副本，
运行中任务用的是内存消息副本，两者状态分裂；压缩完成前用户继续
对话会让"压缩后上下文"立即失效。

**方案**：`Runtime` 增加 `compacting` 状态锁：
- 手动 `/compact` 在任务运行中（`busy`）或已有压缩进行中时直接拒绝
  并提示推迟；
- 自动压缩在 pre_request_hook 内与当前请求天然串行，无需额外锁；
- TUI 在 `compacting` 期间拦截新输入并提示"正在压缩上下文，请稍候"；
- 锁的释放与异常安全：`compact_messages` 用 try/finally 保证任何路径
  都解锁。

#### 明确不做（本轮）

- split-turn 切分、摘要质量守卫（摘要≥原文拒绝）、文件清单继承、
  stale usage 防护、免模型预剪枝——记入 1.5 备选，按效果另立迭代。

### 1.4 实施计划

| 改动 | 文件 | 内容 |
|---|---|---|
| 前缀复用摘要 | `runtime.py` `_summarize_old` | 重放 system + 真实 registry schema + 被压缩旧消息；压缩指令为末尾 user 消息 |
| tool_choice 覆盖 | `providers.py` `complete` | 请求参数支持 `tool_choice="none"`（消息前缀不变） |
| 结构化模板 | `runtime.py` 压缩指令常量 | 固定章节 + 合并规则 + `<compacted-summary>` 识别 |
| 压缩锁 | `runtime.py` + `tui.py` + `commands.py` | `compacting` 状态；busy 拒绝 /compact；TUI 拦截新输入 |
| 测试 | `tests/` | ①摘要请求与主请求的前缀一致性断言；②`tool_choice=none` 断言；③模板章节断言（fake provider 固定输出）；④压缩锁拒绝断言；⑤既有 85+ 测试不回归 |

验收标准：压缩功能行为不变（触发/回放/配对），新增前缀一致性、
模板结构、锁三个可测断言；全量测试通过；重建 exe 冒烟。

### 1.5 备选（后续迭代候选）

1. split-turn 切分（pi）：超长 turn 的前缀单独摘要、后缀保留；
2. 摘要质量守卫（deepseek）：摘要 ≥ 被压缩原文、空输出、含 toolCall → 拒绝；
3. 文件操作清单继承（pi）：`<read-files>/<modified-files>` 跨压缩累计；
4. stale usage 防护（pi）：压缩前的旧用量不触发第二次压缩；
5. 免模型预剪枝（deepseek）：>8K 工具结果保头 4K+尾 1K，压缩前先剪再复测；
6. 多次压缩用户警告（codex）。
