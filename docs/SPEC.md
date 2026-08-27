# conti-agent Functional Specification

## 1. Product Goal

`conti-agent` is an independent, embeddable Python runtime for coding agents. It
connects a language model to a local workspace through a controlled tool system.
The project favors four properties:

1. **Deterministic core** — model calls, tools, and persistence are ordinary
   Python objects that can be tested without network access.
2. **Explicit safety** — every effectful action is checked by one permission
   pipeline and recorded in an audit trail.
3. **Observable execution** — a single event stream describes text, thinking,
   tool calls, results, retries, usage, and completion.
4. **Incremental capability** — skills, custom agent profiles, external tool
   servers, and collaborative workers attach without changing the core.

The first release intentionally ships a terminal REPL rather than a graphical
terminal UI. There is no logo or visual identity beyond plain text.

## 2. Operating Modes

### 2.1 One-shot mode

`conti-agent ask "<prompt>"` starts a task from the current directory, runs the
agent loop until the model finishes or reaches a safety limit, prints the final
answer, and exits with:

- `0` on successful completion;
- `2` when input or configuration is invalid;
- `3` when the model/tool loop fails;
- `130` on user interruption.

`--event-format jsonl` emits one JSON object per execution event. The stream
must remain machine-readable and must include event, timestamp, payload, and, where applicable, tool IDs.

### 2.2 Interactive mode

`conti-agent chat` uses a minimal line-oriented REPL:

- a prompt is submitted with Enter;
- multi-line input is opened by ending a line with `\`;
- `/help` lists commands;
- `/sessions` lists saved sessions;
- `/resume <id>` resumes a session;
- `/compact [instruction]` summarizes older history;
- `/exit` quits.

The REPL must not own runtime policy. It is an adapter over the same event API
as one-shot mode.

### 2.3 Service mode

`conti-agent serve --host 127.0.0.1 --port 8791` exposes a local HTTP endpoint
for task submission and event retrieval. Default binding is loopback. Service
mode must require explicit consent before binding outside loopback.

## 3. Configuration

Configuration is layered from low to high precedence:

1. built-in defaults;
2. `$CONTI_AGENT_HOME/config.toml` or `~/.conti-agent/config.toml`;
3. `.conti/config.toml` in the workspace;
4. `.conti/config.local.toml` for user-specific overrides;
5. command-line arguments.

Required configuration concepts:

```toml
[[provider]]
name = "local-openai"
protocol = "openai"
base_url = "https://api.example.com/v1"
model = "example-model"
api_key_env = "EXAMPLE_API_KEY"
context_window = 128000
max_output_tokens = 8192

[runtime]
permission_mode = "workspace"
max_tool_iterations = 32
history_limit = 120

[extensions]
skills = true
hooks = true
profiles = true
external_tools = true
collaboration = true
```

Additional provider protocols:

- `anthropic`: Anthropic Messages wire format;
- `openai`: OpenAI-compatible Chat Completions wire format.

An API key is resolved only from `api_key_env`; direct secrets in config are not
accepted. A missing key produces a provider-configuration error before tool
execution. The context window resolution order is explicit config, protocol
metadata if available, a conservative model-name table, then 128000.

## 4. Conversation Model

The canonical message roles are:

- `system`: durable operating instructions;
- `user`: human or external input;
- `assistant`: model text and tool requests;
- `tool`: a deterministic tool result tied to a request ID.

A conversation is an append-only list. User-visible messages and internal tool
events are kept together so a replay produces the same state.

## 5. Agent Execution Loop

For one user task the agent:

1. builds the system prompt from runtime instructions, enabled skill summaries,
   and workspace policy;
2. selects tools visible to the current profile;
3. sends messages to the provider;
4. streams text, thinking, usage, and tool requests as events;
5. executes each approved tool request through the permission pipeline;
6. returns normalized tool results to the provider;
7. repeats until no tool request remains or the iteration limit is reached;
8. emits completion and persists the final ledger.

Execution events:

| Event | Requirement |
|---|---|
| `run.started` / `run.completed` | identify a run and completion state |
| `message.created` | expose a complete assistant message |
| `text.delta`, `thinking.delta` | preserve streamed model output |
| `tool.requested` | expose tool name, arguments, and request ID |
| `tool.approved`, `tool.denied` | expose permission outcome |
| `tool.completed` | expose output, error flag, and duration |
| `usage.recorded` | expose input/output tokens when available |
| `run.failed` | expose a recoverable error and optional retry hint |

The loop must be deterministic when supplied a fake provider. Model provider
errors use bounded exponential backoff and are retried only for transient
classes (connection, timeout, rate limit, HTTP 5xx).

## 6. Tool System

Every tool implements a common contract:

- stable `name`;
- natural-language `description`;
- JSON-schema-like `parameters`;
- `validate(args)` before safety checks;
- async `execute(args, context)`;
- result normalization to text plus structured metadata;
- declared effects: `read`, `write`, `execute`, `network`, or `control`.

The first release includes:

| Tool | Purpose |
|---|---|
| `workspace_read` | read a UTF-8 file with bounded size |
| `workspace_write` | create or replace a file with parent creation |
| `workspace_edit` | replace an exact old segment and refuse ambiguous matches |
| `workspace_list` | enumerate paths with ignores and depth |
| `workspace_search` | literal or regular-expression text search |
| `process_run` | execute a command with timeout, capture, and environment allowlist |
| `task_note` | maintain a durable task note for plan reminders |
| `request_input` | ask an interactive user a clarifying question |

`process_run` defaults to the workspace directory and enforces:

- hard timeout and killed-process cleanup;
- combined stdout/stderr capture with bounded size;
- shell only when explicitly requested;
- environment allow/deny rules;
- permission checks before execution.

Tool results must be non-blocking at the architecture level; a slow tool may be
executed by a managed future, but event order remains deterministic.

## 7. Permission and Sandbox Pipeline

Permission modes are:

1. `read_only`: read tools allowed; effects denied by default;
2. `workspace`: reads allowed; writes and commands allowed only inside the
   approved workspace;
3. `approved`: first use of a capability requires approval, then the exact rule
   may be remembered for one session;
4. `trusted`: explicitly user-selected broad mode; dangerous command detection
   still applies.

Order of checks:

1. tool schema validation;
2. declared effect and mode policy;
3. path normalization and workspace boundary check;
4. command policy and dangerous-pattern detection;
5. project/local rules;
6. interactive or configured approval;
7. audit record.

Path checks must resolve symlinks where available and reject absolute paths,
parent traversal, or symlink escapes outside approved roots. Windows paths use
case-insensitive comparison after lexical normalization.

Command policy must block, or require a dangerous override for, operations such
as recursive deletion, disk formatting, privileged installation, credential
exfiltration patterns, and remote destructive execution. Rules support `allow`
and `deny`, exact or regex command matching, comments, and source precedence:
local project > project > user > defaults.

All denied or approved effects are written to `.conti/runtime/audit.jsonl`
without secret values.

## 8. Persistence and Sessions

The state root is `.conti`. It contains:

```text
.conti/
  config.toml
  config.local.toml
  sessions/<session-id>.jsonl
  runtime/audit.jsonl
  runtime/tasks/
  skills/
  profiles/
  memory/
```

A session ledger records:

- schema version;
- session ID, creation/update time, title, and workspace;
- every user, assistant, and tool message;
- tool approvals and denials;
- compaction records.

`resume` must rebuild an in-memory conversation from a ledger and reject
corrupt or unknown schema versions rather than silently dropping data.

## 9. Context Management

Context management uses estimated tokens and provider-declared windows. It must:

- reserve room for output and tool schemas;
- retain the system prompt, latest user request, and recent messages;
- mark older tool results as compactible;
- compact old turns into an explicit summary message;
- preserve compacted-span boundaries and usage metadata;
- expose a `/compact` action and an automatic trigger.

Compaction is provider-backed when configured and deterministic/fake-backed in
tests. The original ledger remains immutable.

## 10. Instruction and Skill Packs

Instructions are loaded from:

1. built-in runtime guidance;
2. `.conti/memory/instructions.md`;
3. enabled skill files under `.conti/skills`;
4. profile-specific instructions.

A skill is a Markdown file with front matter:

```markdown
---
name = "release-checklist"
description = "Check project readiness before a release."
keywords = ["release", "version"]
version = 1
---

1. Run tests.
2. Update the changelog.
```

The runtime advertises only names and descriptions to the model. A
`load_skill` action loads one full skill body after validation. Skills cannot
execute code by themselves and cannot widen permissions.

## 11. Agent Profiles and Subtasks

Profiles define reusable specialist behavior:

```toml
[[profile]]
name = "explorer"
description = "Read-only repository investigation."
system_prompt = "Investigate carefully and cite paths."
allowed_tools = ["workspace_read", "workspace_list", "workspace_search"]
permission_mode = "read_only"
max_tool_iterations = 12
```

The parent agent can delegate a bounded subtask through a `spawn_task` tool.
A subtask:

- has a unique task ID;
- receives its own conversation and profile;
- cannot modify parent history directly;
- returns exactly one final report;
- emits nested-task events with a parent task ID;
- cannot recursively spawn unless explicitly permitted.

## 12. Collaboration

Crew mode is a local coordination layer over bounded subtasks:

- a shared task board persists status, owner, summary, and result;
- workers can be in-process tasks or external `conti-agent worker` processes;
- a mailbox routes messages between lead and workers;
- a worker stops on stop-token, stop command, or parent shutdown;
- task IDs and worker names are unique within a crew.

Collaboration must never bypass permissions. Each worker enforces its own
profile and sandbox.

## 13. Workspace Snapshots

For Git repositories, snapshot sessions provide isolated workspaces:

- create a branch and Git worktree from a clean base;
- mirror selected cache directories by symlink or junction;
- run tools with the active snapshot as workspace root;
- report created, modified, and deleted files;
- merge only with an explicit user-approved action;
- retain or remove snapshots only through explicit cleanup policy.

Snapshot functionality must degrade safely outside a Git repository.

## 14. External Tool Protocol

The project uses a JSON-RPC stdio protocol for external tool servers:

1. initialize with runtime and capability metadata;
2. request `tools/list`;
3. call a tool through `tools/call`;
4. normalize JSON Schema to the native tool contract;
5. shut down cleanly and kill a process on timeout.

Each server has its own namespace (for example `docs.search`) to avoid collisions.
Loading strategy can start in metadata-only mode and expose an
`external_tool_load` action when schema size is large.

## 15. Hooks

Hooks are short declarative entries:

```toml
[[hook]]
event = "tool.before"
match_tool = "process_run"
command = ["python", ".conti/hooks/check.py"]
timeout_ms = 5000
continue_on_error = false
```

Supported events are `run.before`, `run.after`, `tool.before`, and `tool.after`.
Hook input is JSON on stdin. A hook may succeed, fail, or return JSON with:

- `decision`: `allow` or `deny`;
- `message`: human-readable reason;
- `replace_output`: optional normalized tool result.

Hook errors are never silently converted into permission grants.

## 16. Memory

Memory is local, inspectable plain text:

- `instructions.md`: durable user/workspace preferences;
- `facts.md`: concise remembered facts;
- `sessions/<id>.summary.md`: session summaries.

The runtime may propose a memory update, but durable writes require write
permission and are recorded in the audit trail. Retrieval is keyword-first;
there is no hidden network memory service.

## 17. Reliability

- No stack trace is required for normal permission denials.
- Tool errors become model-visible results and are also logged.
- A crash report may be written to `.conti/runtime/crash/last.json`.
- Logging avoids prompt bodies and API keys by default.
- Interrupt handling attempts tool termination and session flush.

## 18. Testing Requirements

- Unit-test configuration parsing and precedence.
- Unit-test all permission mode transitions and path escapes.
- Use fake providers to cover text, tool, retry, and limit behavior.
- Use temporary workspaces for tools and sessions.
- Test hooks, skills, profiles, external-tool protocol, and collaboration with
  deterministic local processes/fakes.
- Ensure every public CLI mode has a test.
- Run the full standard-library test suite with `python -m unittest discover`.

## 19. Non-Goals

- No closed-source cloud control plane.
- No telemetry.
- No proprietary visual identity.
- No automatic execution of unreviewed remote code.
- No silent permission escalation.
