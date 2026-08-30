# 亮点进阶

本文档记录 conti-agent 的差异化亮点：每一项先对标生产级 agent 的实现，
再确定我们自己的实施方案，最后统一落地。每项亮点独立成节，含调研、
选型与实施计划。

- 亮点 1：上下文压缩（本文档，方案已选定，待实施）
- 亮点 2：长期记忆系统（本文档，方案已选定，待实施）
- 亮点 3：权限控制（本文档，方案已选定，待实施；在既有初版权限模块上重构升级）
- 亮点 4：Skill 激活（本文档，方案已选定，待实施；改动量小）
- 亮点 5：Agent Team 多代理协作（本文档，方案已选定，待实施；在既有 profiles/collab 骨架上升级）
- 后续候选：会话检索、任务评估基准……（陆续补充）

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

参考实现：四个生产级终端 Agent 的开源代码库（OpenAI Codex CLI / Rust、
deepseek-harness / TS、opencode / TS、pi / TS），逐一提取其压缩相关源码
常量与执行流程。

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
运行中任务用的是内存消息副本，两者状态分裂。更严重的丢对话场景：
压缩流程"读账本快照 → 模型生成摘要（数秒）→ 写回压缩标记"期间事件
循环空闲，用户新消息照常开任务并写入账本；压缩标记的回放语义是
"清空标记之前的所有消息"，而摘要生成于 T0 不包含期间新写入的对话——
这段对话既不在摘要里又被回放丢弃，从模型上下文永久消失（文件字节还在）。

**方案**：`Runtime` 增加 `compacting` 状态锁：
- 手动 `/compact` 在任务运行中（`busy`）或已有压缩进行中时直接拒绝
  并提示推迟；
- 自动压缩在 pre_request_hook 内与当前请求天然串行（busy 拦截新任务、
  requires_idle 拦截 /compact、hook 与请求同协程链三层既有保护），
  无需额外锁；
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

---

## 亮点 2：长期记忆系统

### 2.1 现状

无任何跨会话记忆。会话 JSONL 只在 `/resume` 时回放本会话；换会话即
失忆，用户偏好（编码风格、常用命令、纠错习惯）每次都要重新告知。

### 2.2 四家生产级 agent 的策略调研

| 实现 | 指令文件 | 记忆存储 | 记忆工具 | 自动提炼 |
|---|---|---|---|---|
| codex | 全局 + 项目根→cwd 逐层发现（`AGENTS.override.md` 遮蔽同层候选），带 `project_doc_max_bytes` 字节预算，注入首条 user 消息 `<INSTRUCTIONS>` 块 | `~/.codex/memories/`：`MEMORY.md`（注册表）、`memory_summary.md`（注入用概要）、`raw_memories.md`、`rollout_summaries/`、`skills/`，整体是 git 基线仓库 | 默认走"读文件 + `<oai-mem-citation>` 引用协议"（引用反哺使用计数）；可选 `memories.list/read/search/add_ad_hoc_note` 四工具（默认关） | ✅ 唯一真实现：两阶段后台管线。Phase 1 提取模型逐 rollout 产出结构化 JSON；Phase 2 整合子代理基于 git diff 增量合并、带使用计数排序与 `max_unused_days` 过期遗忘。默认关闭（成本高） |
| deepseek-harness | AGENTS.md/CLAUDE.md 逐层加载，带字节预算（超限先整文件省略再二分截断）；**动态注入**：模型 read/write 触碰内嵌指令文件后异步注入"新增/更新/删除"增量，指令文件是活的 | ❌（持久化仅 session JSONL+zstd，按 projectKey 目录组织） | ❌ | ❌（compaction 8 节 checkpoint 只存会话内） |
| opencode | AGENTS.md 逐层 findUp，无预算截断；`instructions` 配置数组支持 glob/URL | ❌（SQLite 只存会话/todo） | ❌，但有 `/init` 命令生成/改进 AGENTS.md（目标明说"让未来会话避免踩坑"） | ❌（compact 摘要仅会话内） |
| pi | `AGENTS.override.md > AGENTS.md > CLAUDE.md` 每目录取一个，注入 system prompt `<project_context>` 块，无截断；worktree 去重 | ❌ | ❌（extension 机制留生态位） | ❌（compaction/branch_summary 仅会话内；`/handoff` 示例生成新会话开场 prompt 但不落盘） |

结论：四家里只有 codex 有真正的记忆系统，但它是重基建方案
（后台任务队列 + state DB + 整合子代理 + git 基线），且默认关闭。
其余三家实质上把 AGENTS.md 当记忆用。

### 2.3 选定方案：三层记忆，成本优先

设计原则：**写路径分三层分担成本（离线批量 / 顺带白嫖 / 零调用），
读路径只有一条（会话启动注入一次，中途只写不读——记忆更新永不打断
当前会话的前缀 KV cache，新记忆下个会话生效）**。

记忆文件：workspace 级 `.conti/memory/MEMORY.md`，人类可读可编辑，
结构化分区（用户偏好 / 项目事实 / 踩过的坑），条目带来源与日期。

#### 第一层：auto dream（定时批量提炼，用户可选开启）

- 触发：**启动时补跑**而非守护进程定时——TUI 非常驻进程，开启 dream
  后每次启动检查状态文件里的 `last_dream_at`，超过 24h 即后台跑一次；
- 提取输入：**用户消息全文 + 相邻 assistant 回复摘要（截断几百字，
  提供指代上下文）**，不含任何工具结果。纯 user 输入的遗漏风险：
  ① 应答式输入（"别用那个库"）脱离上下文无法提炼；② 行为化偏好
  （request_input 选项选择、直接改文件）完全不可见。带相邻 assistant
  摘要后成本仍仅约全量回放的 20%（token 大头是工具结果），遗漏大幅缓解；
- 游标：状态文件一行 `last_dream_at`，按文件 mtime 晚于游标选取
  session JSONL，无需逐文件记账；跑完全部成功才推进游标，中途崩溃
  重跑无害（合并模块去重兜底）；正在写的文件拍快照跳过；首次开启
  只回溯最近 7 天；
- 防过度泛化：同一信号计数封顶，低频条目带日期可衰减。

#### 第二层：压缩顺带（白嫖压缩调用）

亮点 1.B 的结构化摘要模板增加一节"值得长期记住的事"，压缩流程把
该节增量合并进 MEMORY.md，与压缩共用同一次模型调用，边际成本≈0。
提取提示词必须严格区分"长期偏好"与"本次任务细节"，防止会话级信息
污染记忆库。

#### 第三层：主动记忆（用户要求 + 手动编辑）

- 模型侧：`memory_write` 工具（合并语义写入，禁止整文件重写），
  用户说"记住这个"时使用；
- 人工侧：系统提示词告知 MEMORY.md 路径，用户直接编辑是合法通道。

#### 共用的合并模块

三个写入口汇到同一个合并纯函数：不调模型、只按 key 做机械折叠
（同 key 计数 +1、无 key 新建、幂等可单测——dream 崩溃重跑无害依赖它）。

key 一致性不靠合并时猜语义，靠**提取时对表**：

- MEMORY.md 每条记忆有稳定 id（`#P01`，写入文件持久存在）；
- 三个提取入口（dream 分批、压缩顺带、memory_write）调模型时把
  当前条目 id + 一句话索引塞进提示词，要求输出
  `{matches: P03}`（命中）/ `{new: true}`（新建）/
  `{supersedes: P03}`（偏好反转覆盖）三选一；
- 语义判断（是否同一偏好、是否改主意）全部发生在提取调用里——
  那个模型看得见旧条目原文与新证据；合并函数只信任 key 折叠。

护栏：幻觉 id（不存在的 matches/supersedes）按 new 降级处理不崩；
漏匹配产生的重复条目靠合并函数内的廉价归一化（小写/去标点/同
category 比对）做二道防线 + 人工编辑合并兜底；人工编辑破坏 id 时
加载器顺带补号。模型负责理解，代码负责账本，账本永远可重建可校验。

#### dream 的拆分装填（防上下文超限）

- 处理单位是**对话单元**（用户消息 + 相邻 assistant 摘要），切分点
  天然对齐用户消息边界；
- 复用 `context.py` 的 CJK 感知 `estimate_tokens` 装填，超过预算
  （约 24K token）封一个请求，离线批处理不需精确 tokenizer；
- 单条超长用户消息（粘贴大段日志）截头去尾各 1K——偏好信号不会埋在
  超长粘贴的正文里；
- 各批返回结构化候选偏好 JSON，直接进合并模块折叠，不做二次归纳链；
- 两道成本闸：会话用户输入合计 < 500 token 跳过；单次 dream 运行设
  总预算（约 20 会话 / 200K 估算 token），超出的留给下次（游标按
  会话推进，不丢不重）；
- 失败隔离：dream 挂了不影响正常使用；压缩层记忆更新失败随压缩
  fallback 放弃，不为记忆重试。

### 2.4 实施计划

| 改动 | 文件 | 内容 |
|---|---|---|
| 记忆存储与合并 | 新 `memory.py` | MEMORY.md 结构、合并纯函数、读写 |
| 启动注入 | `runtime.py` / `agent.py` | 会话启动把 MEMORY.md（截断预算约 2K token）注入 system prompt |
| dream 提取 | `memory.py` + `runtime.py` | 对话单元装填、分批、mtime 游标、启动补跑、状态文件 |
| 压缩顺带提炼 | `runtime.py` 压缩指令 | 模板加"值得长期记住的事"一节 + 调用记忆合并 |
| memory_write 工具 | `tools_misc.py` | 合并语义写入，注册进工具表 |
| 测试 | `tests/` | 单元装填预算断言、游标推进/重跑幂等、合并去重与偏好反转、超长消息截断、注入截断 |

验收标准：三层写入各自可测（fake provider），合并模块幂等，dream
崩溃重跑不产生重复条目；全量测试通过；重建 exe 冒烟。

---

## 亮点 3：权限控制

### 3.1 现状（已有初版权限模块，本轮为重构升级）

仓库已存在 245 行的 `permissions.py` 与完整接线，并非从零开始：

- 4 档模式（read_only/workspace/approved/trusted）+ 8 条危险命令正则
  （含凭据泄露检测）+ 路径沙箱 + 分层规则文件（正则 pattern 首中即决）
  + JSONL 审计（content/env 脱敏）+ APPROVED 模式按工具首次批准
  （session_approvals 会话内记忆）；
- deny 理由已作为错误工具结果回传模型（对标 opencode deny feedback
  的雏形已有）；
- 审批入口 `_approve` 接通提问 UI，但只是 yes/no 文本问答。

对照本轮方案的差距：无三档预设打包；无"不透明命令"检测（bash -c
包装直接漏过）；路径沙箱只检查 `path` 键（bash 参数中的文件路径
不提取，围栏有洞）；危险命令命中直接拒绝而非"弹窗问你"；规则为
正则首中即决而非 last-match-wins 通配符；无 git 检查点 /undo；
无 /permission 命令；批准无"一次/本会话"区分。

### 3.2 四家生产级 agent 的策略调研

| 实现 | 权限模型 | OS 沙箱 | 命令分析 | "总是允许" |
|---|---|---|---|---|
| codex | 双旋钮：审批策略 4 档（untrusted/on-request/granular/never）+ 沙箱 3 档（read-only/workspace-write/danger-full-access），预设打包（Read Only / Auto / Full Access） | ✅ 三平台真沙箱（macOS Seatbelt 规则文件、Linux bwrap+Landlock/seccomp、Windows 受限令牌+ACL，独立 Rust 工程） | execpolicy 规则引擎（Allow/Prompt/Forbidden 前缀规则，持久化到 rules 文件）+ 危险命令启发式 + BANNED_PREFIX 表 | ✅ 会话级审批缓存 + 前缀规则持久化 |
| deepseek-harness | 双旋钮：沙箱档 + 审批策略（ask/never），预设打包 | ✅ 三平台真沙箱，fail-closed（无后端拒绝裸跑） | ❌ 刻意不做——靠内核执行期拦截，不解析命令文本 | ❌ 明文只有 allowed-once，无授权存储（拒绝撤销复杂度） |
| opencode | 纯规则引擎：ask/allow/deny × 工具 × 通配符 pattern，last-match-wins，无匹配默认 ask | ❌ | ✅ tree-sitter 解析 bash AST + BashArity 字典截命令前缀（`git checkout *`） | 会话级内存（V2 迁 SQLite） |
| pi | 无（README 原话 "No permission popups"，建议跑容器） | ❌ | ❌（示例扩展用危险正则演示如何自建） | ❌ |

四家关键细节：

- **双旋钮架构**（codex/deepseek 共有）：权限 = "能做什么"（沙箱/策略
  范围）和"什么时候问"（审批策略）两个正交旋钮，预设把旋钮打包成
  用户听得懂的档位；
- **升级协议**（deepseek）：模型必须先真的吃到沙箱拒绝标记，才允许
  带 justification 重试一次走人工审批——禁止投机升级，拒绝即最终；
  fail-closed 贯穿全线（无审批 UI → 拒绝；无沙箱后端 → 拒绝裸跑）；
- **deny 的反馈价值**（opencode）：用户拒绝附带的消息作为工具结果
  回传模型，模型下一轮知道为什么被拒、不会原样重试；
- **plan mode = 权限预设**（opencode）：只读规划模式不是独立系统，
  就是一个"编辑工具全 deny、只许写计划文件"的内置 agent；
- **圈内只读子路径**（codex）：可写根内可再设 read_only_subpaths，
  工作区整体可写但 `.git/`、自身配置目录单独标只读——保证"后悔药"
  本身删不掉；
- **pi 的坦诚定位**："项目信任只是输入加载守卫，不是安全边界"。

### 3.3 关键认知（威胁模型与 OS 沙箱论证）

**OS 沙箱的原理**：不在文本层检查命令说了什么（base64 解码管道、
`python -c`、脚本内藏命令都能绕过字符串分析），而是创建进程时通过
系统调用告诉内核"此进程只许写这些目录、禁网"，之后进程的每次真实
读写/联网都由内核拦截——伪装骗得过正则，骗不过内核。

**沙箱的能力边界**：防越界不防圈内破坏。`rm -rf` 删工作区内文件在
内核眼里与"改第三行代码"同为合法写操作；圈内破坏靠审批层（危险
意图）+ git（撤销兜底）解决，codex 亦如此。

**不做 OS 沙箱的论证**（选型而非缺省）：
1. 主战场 Windows 恰好最难——没有 macOS/Linux 那样的声明式内核
   接口，实现 = 手搓受限令牌 + ACL + 进程树继承（codex 为此维护
   独立 Rust 工程，团队级投入）；无官方 Python 库；
2. Python 子进程模型下镣铐继承边界情况多，半吊子沙箱比不做更糟
   （制造"已隔离"假象）；
3. 真沙箱默认全拒、逐项放行，配置不全大量误伤正常开发命令；
4. 单用户本地工具的威胁模型是模型失误与提示注入，非恶意逃逸——
   事前拦截 + 不透明即问 + git 可撤销 + 全程审计即可覆盖；若产品
   形态变为暴露在不可信代码下，第一优先级上 Linux Landlock。

**定位**：策略围栏（containment），不是安全边界——文档明示
"防误伤不防恶意"，人工审批是最终边界。四层防线无单点依赖：
事前拦得住的拦，看不懂的问，问完放行的可撤销，全程可审计。

### 3.4 选定方案（不做沙箱的四层防线）

执行模型说明：四层是纵深防御的逻辑分类，执行是按优先级裁剪的
决策树（规则 > 档位短路 > 白名单 > 风险门 > 档位默认），风险门内
四检测器并列评估、理由合并为一次审批；放行的写/执行操作统一先打
git 检查点。

```
                      工具调用 (tool, arguments)
                               │
                ┌──────────────▼───────────────┐
                │ 0. 参数 schema 校验            │─失败─→ ✗ 拒绝（错误回传模型）
                └──────────────┬───────────────┘
                ┌──────────────▼───────────────┐
                │ 1. 规则表 last-match-wins      │─命中─→ ✓/✗ 终审（用户显式规则最高）
                └──────────────┬───────────────┘
                       未命中  │
                ┌──────────────▼───────────────┐
                │ 2. 档位短路：放行档(trusted)？   │─是─→ ✓ 放行（危险命令仅标记检查点）
                └──────────────┬───────────────┘
                       否      │
                ┌──────────────▼───────────────┐
                │ 3. 无害白名单（git status / pytest…）│─命中─→ ✓ 放行（零打扰）
                └──────────────┬───────────────┘
                       否      │
                ┌──────────────▼───────────────────────────────┐
                │ 4. 风险门：四个检测器【并列】评估，任一命中即触发   │
                │   [危险黑名单] [不透明命令] [.git/.conti围栏] [越界路径] │
                └──────┬──────────────────────────────┬─────────┘
                     命中│                           未命中│
              ┌─────────▼─────────────┐    ┌─────────────▼───────────┐
              │ ⑤ 人工审批（合并理由）     │    │ 只读档 且 工具有写/执行效应？ │
              │  允许一次/本会话允许/拒绝   │    │   是 → 审批   否 → ✓ 默认放行│
              └───┬───────────────┬────┘    └─────────────┬───────────┘
                允许│            拒绝│                       │
                    │               ✗ 拒绝理由回传模型（防原样重试）
              ┌─────▼─────────────┐                         │
              │ git 检查点（写/执行类）│                        │
              └─────┬─────────────┘                         │
                    ▼                                       ▼
                 执行工具                                 执行工具
```

```
agent 想执行工具
   ↓
① 放行档？ ── 是 → 直接执行
   ↓ 否
② 规则表命中（last-match-wins）── allow/deny 按表办事
   ↓ 没命中
③ 危险黑名单 / 不透明命令 / 路径越界 / 动 .git、.conti？
   ── 是 → 弹窗问用户
   ↓ 否
④ 无害白名单命令？ ── 是 → 直接执行
   ↓ 否
⑤ 按档位默认：只读档 → 问；标准档 → 工作区内普通操作放行
```

#### 第一层：事前拦截

- **三档预设**（对标 codex 预设）：`只读`（改任何东西、跑任何命令
  都问）/ `标准`（默认；工作区内普通操作放行，危险与越界才问）/
  `放行`（全 allow，用户自担风险）；`/permission` 命令切换；
- **规则引擎**（对标 opencode）：ask/allow/deny × 工具 × pattern，
  last-match-wins，无匹配走档位默认；bash pattern 用 shlex 分词
  截命令前缀（不引 tree-sitter）；存 config.toml 可按项目覆盖；
- **危险黑名单**：`rm -rf`、强推、格式化磁盘类正则清单；
  **无害白名单**：`ls`/`git status`/测试命令类短清单直接放行，
  免打扰。

#### 第二层：不透明即问（把"分析不动"变成策略，对标 codex BANNED_PREFIX）

凡被解释器/外壳包装的命令——`bash -c`、`sh`、`python -c`、
`node -e`、`eval`、管道接 `sh`、`base64` 解码接管道——不尝试看穿
内部，直接升级为人工审批。逻辑：能看懂的按规则裁决，看不懂的不赌。
base64/脚本藏毒等绕过手段全部撞在这堵墙上。

#### 第三层：路径围栏与圈内保护

- write/edit 目标路径 + bash 参数中的绝对路径做工作区外检测，
  越界触发问（对标 opencode external_directory）；
- **`.git/` 与 `.conti/` 强制问**：不许 agent 删掉自己的版本历史
  与账本（对标 codex read_only_subpaths 的无沙箱等价实现）。

#### 第四层：审批交互与事后撤销

- 审批 UI 复用 request_input 选项式提问：显示要执行的命令与原因，
  三选项——允许一次 / 本会话都允许（内存缓存，关程序即忘，不做
  跨会话持久授权，学 deepseek 的克制）/ 拒绝（理由作为工具结果
  回传模型，对标 opencode deny feedback）；
- **git 检查点 + `/undo`**：git 仓库内执行写文件/危险命令前记录
  HEAD 检查点，`/undo` 一键回滚——事前防线全部漏过时的兜底，
  也是无沙箱形态下的主力防线；
- 审批请求与裁决写入会话账本，全程可审计。

### 3.5 实施计划（基于既有 `permissions.py` 重构）

| 改动 | 文件 | 内容 |
|---|---|---|
| 三档预设 | `permissions.py` 重构 | 四档模式收敛为三档预设，打包审批/规则行为 |
| 规则引擎升级 | `permissions.py` | 正则首中即决 → last-match-wins 通配符（shlex 分词前缀），config.toml 读写 |
| 命令分析增强 | `permissions.py` | 黑名单补充、无害白名单、不透明命令检测（包装前缀表） |
| 围栏补洞 | `permissions.py` | bash 参数路径提取进 PathSandbox；`.git/.conti` 保护规则 |
| 危险=问 | `permissions.py` | 危险命中由拒绝改为走审批弹窗（TRUSTED 之外） |
| 审批交互升级 | `runtime.py` `_approve` / `tui.py` | yes/no → 三选项（一次/会话/拒绝带理由），复用 request_input |
| git 检查点 | 新 `git_snapshot.py` + `/undo` | 危险操作前记 HEAD，回滚实现 |
| 斜杠命令 | `commands.py` | `/permission` 切换档位、`/undo` 回滚 |
| 审计 | `sessions.py` | permission.asked/decided 记入会话账本（现有 runtime/audit.jsonl 之外） |
| 测试 | `tests/` | 规则匹配（last-match-wins 与通配符）、黑白名单、不透明命令分类、路径围栏补洞、审批三选项、undo 幂等 |

验收标准：默认档下日常操作（读文件、跑测试）零打扰，危险操作必问，
黑名单/不透明命令绕过样例全部落入问询；全量测试通过；重建 exe 冒烟。

### 3.6 备选（后续迭代候选）

1. OS 沙箱：Linux Landlock 优先（内核接口成熟），产品形态变为
   多用户/不可信代码执行时立项；
2. 跨会话持久授权规则（对标 codex 前缀规则持久化）；
3. plan 只读会话模式（权限预设的 agent 化，对标 opencode）；
4. 子代理权限继承（对标 deepseek delegation seed）。

---

## 亮点 4：Skill 激活（补全既有半成品，改动量小）

### 4.1 现状

核心链路已存在且完整：`skills.py` 的 SkillLibrary 扫描运行时根
`skills/` 下 `*.md`，TOML front matter（name/description/keywords/
version）校验完备；`load_skill` 工具已注册，模型可按名加载正文；
活动流有展示分支。

致命缺口：**模型永远不知道有哪些 skill 存在**——skill 目录从不
注入 system prompt，只能靠用户在对话里手动报出名字。另有：
`keywords` 解析了从未使用；`skills_enabled` 配置是摆设（注册工具
时不检查）；只支持单层平铺目录。

### 4.2 选定方案（最小改动激活）

1. **目录注入**：会话启动把 skill 目录（name + description 列表，
   带截断预算）注入 system prompt，并告知模型"用户提到相关任务时
   可用 load_skill 加载完整正文"——激活整条链路的一处改动；
2. **开关生效**：`skills_enabled=False` 时跳过注册 load_skill
   并跳过目录注入；
3. **`/skills` 命令**（可选）：列出已安装 skill 与描述。

明确不做（本轮）：keywords 自动建议、全局/项目分层目录、
skill 即斜杠命令、嵌套 SKILL.md 发现——记入备选。

### 4.3 实施计划

| 改动 | 文件 | 内容 |
|---|---|---|
| 目录注入 | `runtime.py`（system prompt 组装处） | skill 列表渲染 + 截断预算 |
| 开关生效 | `runtime.py` | skills_enabled 控制注册与注入 |
| /skills 命令 | `commands.py` | 列出 skill 与描述 |
| 测试 | `tests/` | 注入包含 skill 名与描述、开关关闭时既不注册也不注入、截断预算 |

验收标准：模型在不知情提问下能从 system prompt 获知并加载
skill；全量测试通过；重建 exe 冒烟。

### 4.4 备选（后续迭代候选）

1. keywords 自动建议（用户消息命中关键词时提示模型有相关 skill）；
2. 全局/项目分层 skill 目录（对标 opencode/pi 的发现顺序）；
3. skill 即斜杠命令（对标 opencode skill 注册为 command）。

---

## 亮点 5：Agent Team 多代理协作（具备通信机制）

### 5.1 现状（既有骨架，本轮升级）

- `profiles.py`：单发委托链路可用且有测试（spawn_task → ProfileRunner
  按白名单/独立权限跑一个受限子代理，回传最后一条 assistant 文本）。
  但是个"哑"子代理：①模型不知道有哪些 profile（ProfileConfig.description
  解析了从未使用，同 skill 的病）；②`allow_spawn` 死配置（子代理工具表
  无条件剔除 spawn_task，嵌套派生不可能，深度限制永不触发）；③无 model
  字段；④无并发（工具调用串行 await）；⑤无后台模式；⑥子代理事件被
  丢弃，TUI 只有一行"派发子任务"；⑦不落账本。
- `collab.py`（CrewManager）：持久化任务板 + 邮箱，数据层完整、原子写、
  测试覆盖好；驱动只有 CLI worker 演示子命令，TUI/运行时未接。
- `snapshots.py`（SnapshotManager）：git worktree 显式快照，实现完整
  有测试但**零生产接线，死代码**。处置：删除（测试骨架留给亮点 3 的
  git 检查点参考）。

### 5.2 四家调研

**worktree**（结论：四家均不用于任务隔离）：

| 实现 | 机制 |
|---|---|
| codex | CLI 不创建；worktree crate 只做识别与绑定（`codex-thread.json` 唯一线程绑定），创建/清理在闭源 Desktop；CLI 被动兼容（信任归并回主仓库防伪造越权） |
| deepseek | 明确无（Agent Teams README："one shared checkout—no worktree"）；workflow 的 `isolation:'worktree'` 是被拒绝的保留字 |
| opencode | 有 Worktree 服务但是**工作区级**特性（用户并行开窗口的"新建工作区"选项，分支 `opencode/<name>`，无自动清理）；另有 snapshot 影子 git 仓库（step 边界记 tree hash，支撑 undo/revert）——与亮点 3 git 检查点同源 |
| pi | 无；仅被动识别（嵌套 worktree 的 AGENTS.md 去重、`.git` 为文件的分支显示兼容） |

**子代理 / agent team**（四种形态）：

| 实现 | 形态 | 上下文 | 权限收敛 | 回传 |
|---|---|---|---|---|
| codex | 内置最重：collab 工具组（spawn/wait/send_input/close），并发槽 6、深度 1 | 继承父 model/指令/沙箱，可选 fork 全历史 | 逐项继承父 approval/cwd/sandbox；内部 delegate 钉死审批 Never | wait_agent 阻塞汇聚，最后一条消息即结果 |
| deepseek | subagent 工具 + provider 家族（spawn=fresh/fork=父已完成轮前缀/进程外）；后台 job 与 continuable；workflow 脚本编排 + ralph 循环 | spawn 全新/fork 到最后 turn/end | **审批策略无条件钉 'never'**（子代理审批无人看，fail-closed）；沙箱 override 同步捕获 | 结算通知注入父会话（steer 到 step 边界）；或最后 assistant 文本 |
| opencode | task 工具 + agent 配置实体（build/plan/explore/general/隐藏辅助） | 同 instance 新 session | 继承父 deny 规则，task/todowrite 默认 deny 防嵌套，subagent_depth=1；task 工具描述**动态列出可用 agent** | 最后 text part 包 `<task_result>`；后台完成注入合成消息 |
| pi | 核心声明"No sub-agents"；官方示例：spawn 独立进程（`--mode json --no-session`） | task 文本 + agents/*.md system prompt + --tools 白名单，零历史共享 | 进程边界控制；项目级 agent 定义需项目信任 | JSON 事件流解析，最后 assistant 文本 |

**四家共同规律**：①隔离靠独立上下文不靠文件系统（无人给子代理建
worktree）；②子代理权限 ≤ 父（继承 deny 或审批钉死）；③agent =
可配置实体（name/description/tools/model/prompt），description 是
主模型选型唯一依据；④结果 = 最后一条 assistant 消息；⑤后台模式 +
完成通知是共同演进方向。

### 5.3 选定方案：持久化黑板 + 信箱的对等协作

架构三件套：**TeamHub（协调中心）/ Worker（子代理运行时）/ 唤醒式
调度器**。与 codex/opencode 星型拓扑（子代理互不相识、一切经队长
中转）的本质区别：子代理之间可直达通信——情报传递不绕队长，队长
上下文不被中转噪音污染。

#### TeamHub（CrewManager 升级）

内存对象 + 两个文件（`.conti/team/<team_id>/`）：

```
roster:  {agent: running|parked|done}        # 状态由 runner 直接观察
                                              # agent.run 生命周期维护，永不推断
mailbox: {agent: deque[Message]}             # 每人一个 FIFO 收件箱
tasks:   {task_id: Task}                     # 任务板：预指派负责人，交付即更新
                                              # （不做机械依赖解锁——开工顺序
                                              # 由 leader 智能调度，交付自动唤醒
                                              # leader 后由它 team_send 分派后续
                                              # 任务并携带汇总上下文，比机械的
                                              # 系统通知信息质量更高、软硬依赖
                                              # 更灵活）
wake:    {agent: asyncio.Event}              # 唤醒事件（纯内存）
```

- `state.json` 状态快照（原子重写）+ `journal.jsonl` 操作日志
  （append-only：message.sent/delivered、task.*、agent.parked/resumed）；
  **写入纪律：先追加日志再改状态**——日志是事实源（审计+恢复），
  快照写失败仅降级；
- Message = {id, seq, from, to, type(chat|delivery|system), body,
  task_id?}；seq 全队单调递增；
- **消息正文上限 16K**：超限走 ResultSpiller 落盘，留 2K 预览+路径
  （复用亮点 1 资产），防大消息炸队友上下文。

#### 通信面：子代理 1 个工具，队长 3 个

```
子代理：team_send(to, body)   发消息/广播(to="*")/交付(to="lead"+task_id)
队长：  team_create(members, tasks)   建队：花名册+任务清单(依赖+预指派)
                                     +一次并发拉起
        team_send(to, body)           中途指派/纠偏/补充上下文
                                     （成员挂起中被唤醒，运行中下个步边界可见）
        team_close                    收队
收消息：没有工具。结束回合 = 挂起，被唤醒时收件箱内容自动注入；
leader 空闲时子代理交付立即显示为对话流被动通知行（不启动新回合
——唤醒权单向，用户是 leader 的唯一唤醒者），下次输入时第一个
步边界全量注入，零丢失。
```

砍掉 claim/list/complete/wait 四个候选工具的机制替代：任务归属=
建队预指派（依赖就绪时 hub 自动投"可开工"通知）；看板快照搭消息
注入的便车（尾部附一行任务板摘要）；交付=带 task_id 的消息（hub
自动更新任务板并唤醒依赖者）；等待=调度器职责，不发生在模型里。

#### 消息投递：双通道，检测全在代码

- **通道 A（步边界注入）**：子代理每轮工具调用之间（下一个模型请求
  前）查收件箱，有消息则以合成 user 消息注入——**消息永不打断工具
  执行，但绝不迟到一个步骤**；leader 同样享受（交付消息在 leader
  下个步边界注入，可边看边纠偏）；
- **通道 B（挂起唤醒）**：收件人 parked 时，投递点直接 set 其
  asyncio.Event，调度器带消息重新拉起它的回合；
- **检测是 O(1) 状态表查询，不是轮询**：状态变化的代码路径只有两处
  （team_send 落 hub、agent.run 生命周期），每次投递顺手查一次收件人
  状态；全员挂起时 runner 停在 `await event.wait()`，零 CPU。
  模型不参与任何"谁闲谁忙"的判断（那会把确定性问题变成概率问题）。

#### 消费语义

投递原子三步：drain 全量取空 → **先注入对话（写账本）→ 再从收件箱
删除 + journal 记 delivered(seq)**。对话本身就是消费凭证——恢复时
消息 id 已在对话中即视为已消费，跳过重投（先注入后删除保证崩溃
不丢，重投靠 id 对账去重）。收件箱"取空制"使消费位置天然 = 队列
清空点，无游标。语义层"看了没理会"不是传输漏洞：消息不重投，
靠提示词约束（每条团队消息须回应或声明搁置）+ leader 看任务板
不动可催办。

#### 唤醒权结构（用户优先的自动唤醒）

子代理由 hub 唤醒（asyncio.Event）；leader 由团队事件自动唤醒——
成员交付/消息到达时若 leader 空闲，立即拉起一个"无用户消息的续回合"
（只消费步边界注入的收件箱），对交付作出回应、质检、需要用户决策时
request_input 中继（成员不持有 request_input，提问一律 team_send 给
leader 转达）；任务板收尾后由 leader 语义化 team_close（可打回返工）
而非机械散伙。防失控：自动回合计数上限（用户输入随时接管并重置
计数）、收件箱合并处理、团队超时保险丝。leader 被用户 Esc 中断时
团队不散伙：交付持续落 hub，下次输入第一个步边界全量注入。
**团队寿命 > 单回合**。

#### 结果呈现与收队

最终结果 = **leader 的汇总输出**（现有主对话流 markdown 渲染），
不转发子代理原文（交付是 leader 的输入不是用户的答案，过程留在
子代理对话里）。过程全程围观：队内通信与任务状态变化透传到活动行
（可 Ctrl+O 展开），`/team` 看任务板。正常结束状态：任务板无
doing/todo、全员 done、leader 生成收尾输出、team_close 写
终态归档（journal 永久可审计）。防呆：任务板全清且 leader 回合
结束而未 close → 调度器自动归档。

#### 安全与终止

- 权限收敛：子代理权限 ≤ 队长（ProfileConfig 白名单），审批确定性
  拒绝（deepseek 论证：子代理的审批没人看），扁平一层不许再开团队；
- 预算闸：每子代理轮数上限、全队消息总量上限、全局超时；被唤醒后
  无产出又收工不重复唤醒（防抖）；
- 死锁检测：全员 parked 且有未完成任务 → 在状态转移瞬间惰性判定，
  "谁在等谁"快照投 leader 人工裁决。

#### 异常恢复矩阵（两级：机制级自动，任务级交用户）

| 异常 | 发现者 | 恢复 |
|---|---|---|
| 子代理回合异常终止（限流/异常逃逸） | runner try/except | roster 标 failed → 通知 leader；**自动重试一次**（全新对话+journal 交付摘要续断点），再败交 leader 裁决 |
| 漏交付就收工 | 调度器 park 时检查 | system 消息提醒再唤醒一次；仍无交付 → 任务 failed + 通知 leader |
| 单工具调用失败 | agent 现有机制 | 错误回模型自处理，不升级 |
| 写盘失败 | hub try/except | 降级不阻断：journal 追加失败才停写告警；state.json 失败无碍（可由 journal 重放） |
| 消息超限且 spill 失败 | hub | 硬截断+标记，不卡队列 |
| 死锁 | 调度器惰性判定 | 快照投 leader 裁决 |
| Esc 中断 leader | TUI 中断路径 | 团队不散伙（见唤醒权结构） |
| 进程崩溃 | 重启检测 | **不自动复活**：启动扫 `.conti/team/` 未收队团队 → 提示用户选"任务板+交付摘要注入新会话接续指挥"或归档。任务板与交付在 journal 与 leader 对话账本中，现场完整保全 |

### 5.4 实施计划

| 改动 | 文件 | 内容 |
|---|---|---|
| TeamHub | `collab.py` 升级 | 花名册/任务板/收件箱 + journal/state 双写 + asyncio 唤醒事件 + seq/dedup |
| Worker 运行时 | `profiles.py` 升级 | 激活-挂起-再激活循环、步边界注入、team_send、并发槽位、交付防呆 |
| 队长工具 | `profiles.py` / `runtime.py` | team_create/team_close；spawn_task 退役或收编 |
| 描述可见性 | 同上 | profile/agent 目录动态注入 team_create 描述（解锁选型）；ProfileConfig 增 model 字段 |
| 事件透传 | `agent.py` / `tui.py` | 子代理事件→活动行；`/team` 命令 |
| 账本 | `sessions.py` | 团队事件入会话账本 |
| 清理 | 删 `snapshots.py` | 死代码移除 |
| 测试 | `tests/` | 投递双通道、消费幂等与崩溃对账、防呆/防抖/死锁/预算、恢复矩阵各分支、并发槽位 |

验收标准：两队协作场景（并行调研→汇总、A 递情报 B 写作）端到端
可跑；单代理故障自动重试；进程崩溃后现场保全可接续；全量测试
通过；重建 exe 冒烟。

### 5.5 备选（后续迭代候选）

1. 动态抢任务（claim 制替代预指派）；
2. 团队全复活（进程崩溃后恢复子代理对话内存状态，对标 deepseek
   continuable）；
3. 跨进程/跨机团队（journal 已是消息总线形态，可换传输层）；
4. worktree 文件隔离——调研结论：四家均不用于任务隔离，不立项。
