# conti-agent Implementation Plan

The runtime is implemented bottom-up. Each phase has a reviewable commit, tests,
and a concrete exit criterion. Later phases may add adapters but must not make
the deterministic core depend on them.

## Phase 0 — Repository and Design Baseline

**Deliverables**

- Python package layout under `src/conti_agent`;
- functional specification;
- staged implementation plan;
- standard-library-only test entry point;
- MIT project metadata.

**Exit criteria**

```bash
python -m unittest discover -s tests
```

succeeds, and design documents are committed before runtime code.

## Phase 1 — Deterministic Agent Core

**Modules**

`messages.py`, `events.py`, `tools.py`, `providers.py`, `agent.py`

**Implementation notes**

- Model messages are immutable dictionaries normalized at the boundary.
- Events are dataclasses serialized through `asdict`.
- Tools expose parameters and effects; the registry rejects duplicate names.
- Providers implement async `complete(messages, tools, stream_handler)`.
- An OpenAI-compatible provider builds Chat Completions requests; an Anthropic
  provider builds Messages requests. HTTP transport is injected and optional.
- `FakeProvider` enables deterministic tests without sockets.
- The agent loop streams deltas, requests tools, executes them, appends results,
  and terminates on a final assistant message.

**Tests**

- tool schema conversion and duplicate rejection;
- final-answer-only run;
- one tool-call round;
- maximum-iteration failure;
- provider retry for transient errors;
- JSONL event serialization.

**Exit criteria**

A fake-provider run can answer directly and can complete one read-only tool
round with event-level assertions.

## Phase 2 — Local Workspace Tools

**Modules**

`workspace.py`, `tools_local.py`

**Implementation notes**

- All paths resolve against the active workspace and are rejected on escape.
- Reads enforce an explicit maximum file size.
- Writes create parent directories and record bytes written.
- Edits require an exact old segment and reject zero or ambiguous matches.
- Listing ignores common dependency/cache directories by default.
- Search returns bounded path/line/text tuples and supports literal mode.
- Process execution uses platform-neutral async subprocesses with timeout,
  output cap, environment policy, and explicit shell opt-in.

**Tests**

- normal read/write/edit/list/search;
- newline and CRLF preservation;
- traversal and symlink escape rejection;
- ambiguous edit rejection;
- process timeout and output truncation;
- workspace-root confinement.

**Exit criteria**

Every built-in tool can execute in a temporary workspace and fails closed on
invalid paths.

## Phase 3 — Safety, Sessions, and Context

**Modules**

`permissions.py`, `sessions.py`, `context.py`

**Implementation notes**

- Permission decisions are separate pure objects.
- Rules are loaded from defaults and TOML files with local-over-project-over-user
  precedence.
- Dangerous command detection is conservative and testable.
- Tool executor wraps every effectful tool after validation and emits
  approval/denial events.
- Sessions append canonical JSONL records and rebuild conversations from them.
- Context planning keeps recent turns and compacts compactible history behind an
  explicit summary message.

**Tests**

- every permission mode;
- allow/deny rule precedence;
- dangerous command rejection;
- denial audit record;
- session append/resume/corruption rejection;
- automatic and manual compaction boundaries.

**Exit criteria**

A denied write or command never reaches the tool and is recorded; a saved session
can be replayed into an equivalent conversation.

## Phase 4 — Extensibility

**Modules**

`config.py`, `skills.py`, `hooks.py`, `profiles.py`, `external.py`

**Implementation notes**

- TOML configuration supports providers, runtime, skills, profiles, hooks, and
  external servers; secrets remain environment references.
- Skills advertise metadata and require explicit load for the full body.
- Hooks receive JSON, have a timeout, and can deny but never broaden permission.
- Profiles compile into a constrained agent factory.
- `spawn_task` invokes a child agent with its own history and permission mode.
- The external JSON-RPC client namespaces tool names and kills timed-out
  processes.

**Tests**

- config layer merge and secret resolution;
- skill discovery, front matter validation, and load;
- hook allow/deny/error behavior;
- profile tool restriction and recursion block;
- external initialize/list/call through a fake transport.

**Exit criteria**

A specialist profile can run a bounded subtask, and an external tool can be
listed and called without coupling the core to a vendor SDK.

## Phase 5 — Crews, Snapshots, Service, and CLI

**Modules**

`collab.py`, `snapshots.py`, `service.py`, `cli.py`, `repl.py`

**Implementation notes**

- Crew state is persisted as JSON and updated through a single coordinator.
- In-process workers share the provider but never share mutable history.
- External workers receive task JSON and return final JSON.
- Git snapshot commands are checked before worktree creation and fail cleanly
  outside a repository.
- Service handlers translate HTTP requests into runtime calls without global
  mutable state.
- The REPL and one-shot command call the same runtime facade.
- `/compact`, `/sessions`, and `/resume` operate on persisted state.

**Tests**

- task board lifecycle and mailbox routing;
- concurrent worker completion and stop;
- snapshot create/status/cleanup with a temporary repository;
- service request validation and event serialization;
- CLI exit codes and REPL commands using injected streams.

**Exit criteria**

A user can install the entry point, run `ask`, `chat`, `worker`, and `serve`,
and complete a local coding task with session recovery.

## Phase 6 — Learning and Release Hardening

**Deliverables**

- architecture document explaining the data flow;
- configuration examples;
- threat model and permission examples;
- release checklist;
- full test run;
- tagged `v0.1.0` release commit.

**Definition of done**

1. `python -m unittest discover -s tests` passes on Python 3.11+.
2. No required third-party runtime dependency exists.
3. Configuration, safety, persistence, extension, collaboration, and CLI tests
   all have explicit assertions.
4. Documentation contains no visual branding and no inherited style.
5. A new user can understand the runtime from `README.md` and `docs/`.
